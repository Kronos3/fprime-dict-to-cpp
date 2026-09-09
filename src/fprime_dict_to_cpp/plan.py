"""Resolving a dictionary plus a configuration into everything the emitters need.

Every decision that both the FPP and the C++ output have to agree on is made here,
once: what each member is called, what local id it gets, which channels sit in which
packet and in what order.  The emitters then only render.

The distinction that matters most is between a **remote** id and a **local** id.  The
remote's opcodes and ids are absolute -- they already include its components' base ids
-- and they are what appears on the wire, so a forwarded command has to carry the
remote opcode and an inbound event has to be matched on the remote id.  The proxy's own
FPP, meanwhile, declares dense local ids from zero, which F Prime offsets by whatever
base id the target topology assigns the proxy instance.  Conflating the two produces a
component that builds and then silently talks to nobody.

Two configuration keys make a member's *declaration* and its *wire handling* separable,
which is what lets a proxy be a piece of flight software rather than only a ground
interface.  :attr:`Config.omit` drops the declaration -- no command, event or channel on
the component, and so no entry in the target's ground dictionary -- while the four port
tables hook a typed port onto the same traffic: an input port that forwards a command, an
output port that fires when an event, a channel or a telemetry packet arrives.  The two
are orthogonal: a member can have both, either, or neither, and a member with neither is
not decoded at all.
"""

from __future__ import annotations

import difflib
import fnmatch
import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar

from .model import (
    Channel,
    Command,
    ComponentKind,
    Dictionary,
    Event,
    FormalParam,
    NamingError,
    QualifiedName,
    TelemetryPacket,
    TelemetryPacketSet,
    TypeKind,
    TypeRef,
)
from .naming import Naming, check_member_identifier, mangle_all
from .spelling import (
    Context,
    Mirrored,
    Spelling,
    TypeNamer,
    check_type_identifier,
)

__all__ = [
    "DIAGNOSTIC_CHANNELS",
    "DIAGNOSTIC_EVENTS",
    "FORWARD_DIAGNOSTIC_CHANNELS",
    "FORWARD_DIAGNOSTIC_EVENTS",
    "RESERVED_MEMBER_NAMES",
    "RESERVED_PORT_NAMES",
    "STRING_LIMITS",
    "ChannelPlan",
    "CommandPlan",
    "Config",
    "EventPlan",
    "Member",
    "MemberPlan",
    "PacketPlan",
    "Plan",
    "PortKind",
    "PortParam",
    "PortPlan",
    "StringLimit",
    "resolve",
]

#: The ports the generated component declares.  No member may take one of these names:
#: FPP keeps ports in their own name group, but a collision here would still be
#: confusing, and reserving them costs nothing.
RESERVED_PORT_NAMES = (
    "comIn",
    "comOut",
    "run",
    "cmdIn",
    "cmdRegOut",
    "cmdResponseOut",
    "logOut",
    "logTextOut",
    "tlmOut",
    "timeCaller",
)

#: The proxy's own telemetry, which is not mirrored from anything.
#:
#: Everything else this component reports belongs to the remote, so without these a
#: link that is dropping or garbling packets would look identical to a link that is
#: simply quiet.
DIAGNOSTIC_CHANNELS = (
    "ProxyPacketsDecoded",
    "ProxyDecodeErrors",
    "ProxyPacketsIgnored",
)

#: The proxy's own events.  Throttled, because a desynchronised link produces one per
#: packet and the point is to notice the problem, not to drown in it.
DIAGNOSTIC_EVENTS = ("ProxyDecodeError", "ProxyPacketIgnored")

#: The proxy's telemetry for the *outbound* direction, declared only when a command input
#: port exists.
#:
#: A forwarded command has a caller to answer to, so a failure there is reported as a
#: command response.  A command *port* has none -- an F Prime input port carries no status
#: back unless it is synchronous and returns one -- so without these a link that cannot
#: take the packet would swallow every port invocation silently.
FORWARD_DIAGNOSTIC_CHANNELS = ("ProxyForwardErrors",)

#: The matching event.  Throttled for the same reason the decode error is.
FORWARD_DIAGNOSTIC_EVENTS = ("ProxyForwardError",)

#: Names no mirrored member may take.
RESERVED_MEMBER_NAMES = (
    *RESERVED_PORT_NAMES,
    *DIAGNOSTIC_CHANNELS,
    *DIAGNOSTIC_EVENTS,
    *FORWARD_DIAGNOSTIC_CHANNELS,
    *FORWARD_DIAGNOSTIC_EVENTS,
)


@dataclass(frozen=True)
class StringLimit:
    """F Prime's default capacity for a string in one context, and what sets it."""

    constant: str
    size: int


#: F Prime's default capacity for a string in each context.
#:
#: ``string size N`` does not produce an N-character C++ type at a component's API
#: boundary.  A command argument arrives as ``Fw::CmdStringArg``, an event argument and a
#: telemetry value are serialized through a clamp of ``FW_MIN(<limit>, N)``.  So a
#: declared size above the limit is not fully representable -- on the remote either, which
#: is why this is a fidelity note rather than a defect, but it does mean the two
#: deployments have to agree on the limit.
STRING_LIMITS = {
    "command argument": StringLimit("FW_CMD_STRING_MAX_SIZE", 40),
    "event argument": StringLimit("FW_LOG_STRING_MAX_SIZE", 200),
    "telemetry channel": StringLimit("FW_TLM_STRING_MAX_SIZE", 40),
}


@dataclass(frozen=True)
class Config:
    """What the caller chose."""

    component: str
    """The generated component's name, e.g. ``"RefProxy"``."""

    namespace: str
    """The FPP module the component and its types live in."""

    kind: ComponentKind = ComponentKind.ACTIVE
    """Decides where an inbound packet is decoded, and what kind the commands are."""

    naming: Naming = field(default_factory=Naming)
    """How dictionary names collapse into component member names."""

    name_overrides: Mapping[QualifiedName, str] = field(default_factory=dict)
    """Members named outright, in place of whatever :attr:`naming` would produce.

    Keyed by the member's full dictionary name, so
    ``{QualifiedName.parse("Ref.dpDemo.SelectColor"): "SelectDemoColor"}`` names that one
    command and leaves every other name alone.  Mangling routes *around* these: an
    automatic name that would have collided with a chosen one is the one that grows.

    A key matching nothing in the dictionary is a :class:`~.model.NamingError` rather
    than a no-op, because the usual reason for one is a dictionary that has moved on.
    """

    type_prefix: str = ""
    """Prepended to every generated type name; see :class:`~.spelling.TypeNamer`."""

    type_name_overrides: Mapping[str, str] = field(default_factory=dict)
    """Generated types named outright, keyed by dictionary qualified name.

    ``{"Ref.DpDemo.ColorEnum": "DemoColor"}`` in place of ``Ref_DpDemo_ColorEnum``.
    Checked the same way :attr:`name_overrides` is; see :class:`~.spelling.TypeNamer`.
    """

    omit: Sequence[str] = ()
    """Members the component does not declare, as dictionary names or ``fnmatch`` globs.

    ``("Ref.dpDemo.*", "*.PRM_SET")`` leaves those commands, events and channels out of the
    generated FPP, and so out of the target deployment's ground dictionary.  A member that
    is omitted *and* has no port is not decoded at all; one that is omitted and does have a
    port keeps its wire handling and loses only its declaration, which is how a proxy ends
    up with nothing but port traffic.

    A pattern matching nothing in the dictionary is a :class:`~.model.NamingError` rather
    than a no-op, for the same reason a name override that matches nothing is: the usual
    cause is a dictionary that has moved on, and quietly declining to omit something means
    publishing a member that was asked to be private.

    Local ids are dense over what *remains*, so omitting a member does renumber the ones
    after it.  That is unavoidable -- a declaration is what carries an id -- and it is
    reported as a warning.
    """

    command_ports: Mapping[QualifiedName, bool | str] = field(default_factory=dict)
    """Commands to hook an input port onto, keyed by full dictionary name.

    ``True`` takes the derived port name (:attr:`PortKind.suffix`); a string names the port
    outright; ``False`` is the same as saying nothing, so a hook can be switched off
    without deleting the line that documents it.  Invoking the port serializes the remote's
    opcode and the port's arguments into a command packet and sends it, exactly as the
    forwarded command does -- but with no ground command, and no command response, in the
    way.

    A port is *additive*: the command is still declared unless :attr:`omit` also names it.
    """

    event_ports: Mapping[QualifiedName, bool | str] = field(default_factory=dict)
    """Events to hook an output port onto, spelled as :attr:`command_ports` is.

    The port carries the event's own arguments and is invoked when a matching event packet
    arrives from the remote, alongside the mirrored event if that is still declared.
    """

    telemetry_ports: Mapping[QualifiedName, bool | str] = field(default_factory=dict)
    """Telemetry channels to hook an output port onto, spelled as :attr:`command_ports` is.

    The port carries the channel's value and the *remote's* timestamp, which is the same
    pair the autocoded ``tlmWrite_`` helper is given.
    """

    packet_ports: Mapping[str, bool | str] = field(default_factory=dict)
    """Telemetry packets to hook an output port onto, keyed ``<set name>.<packet name>``.

    The port carries one formal per channel of the packet, in wire order, then the packet's
    single timestamp.  Unlike the three tables above there is nothing to omit: a packet is
    a layout rather than a declared member, so a packet port is the only thing that makes
    one visible to flight software.
    """

    packetized: bool = True
    """Whether to decode ``FW_PACKET_PACKETIZED_TLM`` packets as well as channelized."""

    diagnostics: bool = True
    """Whether to declare the proxy's own health telemetry and events.

    See :data:`DIAGNOSTIC_CHANNELS` and :data:`DIAGNOSTIC_EVENTS`.  Their local ids come
    *after* every mirrored member's, so turning them off does not renumber anything.
    """

    include_prefix: str = ""
    """Path prefix for the generated ``#include`` of the autocoded base class.

    F Prime's build emits ``<module path>/<Component>ComponentAc.hpp``, so this is
    normally the component's directory relative to the project root.
    """

    @property
    def cpp_namespace(self) -> str:
        """The C++ namespace chain, e.g. ``"Ref::Proxy"`` for module ``Ref.Proxy``."""
        return "::".join(p for p in self.namespace.split(".") if p)

    @property
    def base_class(self) -> str:
        """The autocoded base class name."""
        return f"{self.component}ComponentBase"

    @property
    def base_header(self) -> str:
        """The autocoded base class header, as the generated C++ includes it."""
        name = f"{self.component}ComponentAc.hpp"
        return f"{self.include_prefix}/{name}" if self.include_prefix else name


# ---------------------------------------------------------------------------
# Hooked ports
# ---------------------------------------------------------------------------


class PortKind(Enum):
    """What a hooked port hooks, which decides everything else about it.

    The suffixes are what keep the four kinds from colliding.  Port names live in one FPP
    name group -- unlike commands, events and channels, which each have their own -- so two
    members that legitimately share an identifier, as an event and a channel in the Ref
    dictionary do, would otherwise ask for the same port.  Distinct suffixes make that
    impossible rather than merely unlikely.
    """

    COMMAND = "command"
    EVENT = "event"
    TELEMETRY = "telemetry"
    PACKET = "packet"

    @property
    def suffix(self) -> str:
        """What a derived port name ends in, e.g. ``"_cmdIn"``."""
        return _PORT_SUFFIXES[self][0]

    @property
    def type_suffix(self) -> str:
        """What the generated port type's name ends in, e.g. ``"_CmdPort"``."""
        return _PORT_SUFFIXES[self][1]

    @property
    def is_input(self) -> bool:
        """Whether the port is an input port.  Only a command hook is."""
        return self is PortKind.COMMAND


#: Each kind's ``(port name suffix, port type suffix)``.
_PORT_SUFFIXES = {
    PortKind.COMMAND: ("_cmdIn", "_CmdPort"),
    PortKind.EVENT: ("_eventOut", "_EventPort"),
    PortKind.TELEMETRY: ("_tlmOut", "_TlmPort"),
    PortKind.PACKET: ("_packetOut", "_PacketPort"),
}

#: The FPP and C++ spellings of the timestamp a telemetry port carries.
_TIME_FPP = "Fw.Time"
_TIME_CPP = "const Fw::Time&"

#: What that formal is called, unless a channel of the same packet already is.
_TIME_FORMAL = "timeTag"

#: What a channel port's value formal is called.
_VALUE_FORMAL = "value"


@dataclass(frozen=True)
class PortParam:
    """One formal of a generated port, already spelled for both output languages.

    Spelled rather than kept as a :class:`~.model.TypeRef` because a port's formals are not
    all a dictionary's: a telemetry port carries an ``Fw.Time`` the dictionary never
    mentions, alongside values whose types come straight out of it.  Resolving both to text
    here leaves the emitters with nothing to decide.
    """

    name: str
    """The formal's name, unescaped; the emitters apply each language's escape."""

    fpp: str
    """Its FPP type, e.g. ``"U32"``, ``"string size 40"``, ``"Fw.Time"``."""

    cpp: str
    """Its C++ type as a formal parameter, e.g. ``"const Fw::Time&"``."""

    annotation: str = ""

    string_size: int | None = None
    """The declared capacity, for a ``string`` formal that has to be serialized.

    A port hands over a ``const Fw::StringBase&`` of whatever capacity the caller had, but
    the remote is expecting ``string size N``.  Copying through an exactly-sized string
    clamps it the way the remote's own command declaration would have.
    """


@dataclass(frozen=True)
class PortPlan:
    """One hooked port: what it is called, what type it takes, and what it carries."""

    kind: PortKind
    name: str
    """The port's name on the component, e.g. ``"dpDemo_SelectColor_cmdIn"``."""

    type_name: str
    """The generated FPP port type's name, e.g. ``"dpDemo_SelectColor_CmdPort"``.

    Generated rather than named in the configuration: a port whose formals are derived from
    the dictionary is correct by construction, where one named after a type the project
    already has would only be checked by ``fpp-check``, long after the mistake was made.
    """

    declaration: str
    """The FPP port kind, e.g. ``"async input port"`` or ``"output port"``."""

    params: tuple[PortParam, ...]
    origin: str
    """What it hooks, as the dictionary names it; used in annotations and comments."""

    annotation: str
    """What the port declaration says about itself."""

    @property
    def is_input(self) -> bool:
        return self.kind.is_input


class Member(Protocol):
    """What every mirrored member has, whichever kind it is."""

    @property
    def name(self) -> QualifiedName:
        """Its full dictionary name, instance path and all."""


Source = TypeVar("Source", bound=Member)


@dataclass(frozen=True)
class MemberPlan(Generic[Source]):
    """One mirrored dictionary member: what the proxy calls it, and its two ids.

    Every kind is planned the same way and carries the same three things, so they share
    this.  What differs is only where the remote's absolute value lives on the source --
    a command is known by its opcode and everything else by its id -- which is why
    :attr:`remote_id` is the one thing each kind says for itself.
    """

    source: Source
    identifier: str
    """The proxy's member name for it.

    Assigned even for a member the component does not declare, because a hooked port's
    derived name and type are built from it.
    """

    local_id: int | None
    """Its id in the proxy's own FPP, dense from zero, or ``None`` if it has none.

    ``None`` means the member is not declared -- :attr:`Config.omit` names it -- so there is
    nothing for an id to number.  Ids are dense over what remains, which is why omitting a
    member renumbers the ones after it.
    """

    port: PortPlan | None = None
    """The port hooked onto it, if the configuration asked for one."""

    @property
    def declared(self) -> bool:
        """Whether the component declares this member at all."""
        return self.local_id is not None

    @property
    def decoded(self) -> bool:
        """Whether the generated code does anything when this member's traffic arrives."""
        return self.declared or self.port is not None

    @property
    def remote_id(self) -> int:
        """The absolute id or opcode the remote knows it by, as it goes on the wire.

        The one thing a kind has to say for itself, since only it knows where its source
        keeps the value.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class CommandPlan(MemberPlan[Command]):
    """One forwarded command, whose ids are called opcodes wherever a command is.

    Both spellings reach the same two values, so the emitters can say ``remote_opcode``
    where that is what it is.
    """

    @property
    def remote_id(self) -> int:
        return self.source.opcode

    @property
    def local_opcode(self) -> int:
        """The opcode in the proxy's own FPP, dense from zero.

        Only an omitted command has none, and nothing that renders a declaration reaches
        one of those, so this insists rather than propagating a ``None``.
        """
        if self.local_id is None:
            raise AssertionError(
                f"{self.source.name} is omitted, so it has no local opcode"
            )
        return self.local_id

    @property
    def remote_opcode(self) -> int:
        """The absolute opcode to put on the wire, as the remote knows it."""
        return self.remote_id


@dataclass(frozen=True)
class EventPlan(MemberPlan[Event]):
    """One mirrored event."""

    @property
    def remote_id(self) -> int:
        return self.source.id

    @property
    def log_function(self) -> str:
        """The autocoded emitter, e.g. ``log_ACTIVITY_LO_dpDemo_DpStarted``."""
        return f"log_{self.source.severity.cpp}_{self.identifier}"


@dataclass(frozen=True)
class ChannelPlan(MemberPlan[Channel]):
    """One mirrored telemetry channel."""

    @property
    def remote_id(self) -> int:
        return self.source.id

    @property
    def write_function(self) -> str:
        """The autocoded emitter, e.g. ``tlmWrite_cmdDisp_CommandsDispatched``."""
        return f"tlmWrite_{self.identifier}"


@dataclass(frozen=True)
class PacketPlan:
    """One packetized-telemetry packet, with its channels in wire order."""

    source: TelemetryPacket
    channels: tuple[ChannelPlan, ...]
    set_name: str = ""
    """The packet set the packet came from, which is half of how it is named."""

    port: PortPlan | None = None
    """The port hooked onto it, if the configuration asked for one."""

    @property
    def id(self) -> int:
        return self.source.id

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def qualified_name(self) -> str:
        """``<set name>.<packet name>``, which is how :attr:`Config.packet_ports` keys it."""
        return f"{self.set_name}.{self.name}" if self.set_name else self.name


@dataclass(frozen=True)
class Plan:
    """A dictionary, resolved against a configuration and ready to emit."""

    dictionary: Dictionary
    config: Config
    spelling: Spelling
    commands: tuple[CommandPlan, ...] = ()
    events: tuple[EventPlan, ...] = ()
    channels: tuple[ChannelPlan, ...] = ()
    packets: tuple[PacketPlan, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def has_packets(self) -> bool:
        return bool(self.packets)

    @property
    def declared_commands(self) -> tuple[CommandPlan, ...]:
        """The commands the component declares; an omitted one is planned but not declared."""
        return tuple(command for command in self.commands if command.declared)

    @property
    def declared_events(self) -> tuple[EventPlan, ...]:
        return tuple(event for event in self.events if event.declared)

    @property
    def declared_channels(self) -> tuple[ChannelPlan, ...]:
        return tuple(channel for channel in self.channels if channel.declared)

    @property
    def decoded_events(self) -> tuple[EventPlan, ...]:
        """The events an inbound event packet is matched against."""
        return tuple(event for event in self.events if event.decoded)

    @property
    def decoded_channels(self) -> tuple[ChannelPlan, ...]:
        """The channels that do something when they arrive: written, ported, or both.

        Not the same as the channels the decoder has a case for.  A channelized packet is a
        run of values with no lengths, so walking past a channel nobody wants still needs
        its type -- see :attr:`Config.omit`.
        """
        return tuple(channel for channel in self.channels if channel.decoded)

    @property
    def decodes_channels(self) -> bool:
        """Whether the component decodes channelized telemetry at all."""
        return bool(self.decoded_channels)

    @property
    def ports(self) -> tuple[PortPlan, ...]:
        """Every hooked port, in the order the generated FPP declares them."""
        hooked = (
            *(member.port for member in self.commands),
            *(member.port for member in self.events),
            *(member.port for member in self.channels),
            *(packet.port for packet in self.packets),
        )
        return tuple(port for port in hooked if port is not None)

    @property
    def input_ports(self) -> tuple[PortPlan, ...]:
        return tuple(port for port in self.ports if port.is_input)

    @property
    def output_ports(self) -> tuple[PortPlan, ...]:
        return tuple(port for port in self.ports if not port.is_input)

    @property
    def diagnostic_channel_ids(self) -> dict[str, int]:
        """Local telemetry ids for the proxy's own channels, after the mirrored ones."""
        return self._diagnostics(
            (
                *DIAGNOSTIC_CHANNELS,
                *self._forward_diagnostics(FORWARD_DIAGNOSTIC_CHANNELS),
            ),
            after=len(self.declared_channels),
        )

    @property
    def diagnostic_event_ids(self) -> dict[str, int]:
        """Local event ids for the proxy's own events, after the mirrored ones."""
        return self._diagnostics(
            (*DIAGNOSTIC_EVENTS, *self._forward_diagnostics(FORWARD_DIAGNOSTIC_EVENTS)),
            after=len(self.declared_events),
        )

    def _forward_diagnostics(self, names: Sequence[str]) -> Sequence[str]:
        """``names``, or nothing when there is no command port for them to report on.

        They come last, so a component without one is numbered exactly as it was before
        this existed.
        """
        return names if self.input_ports else ()

    def _diagnostics(self, names: Sequence[str], *, after: int) -> dict[str, int]:
        """``names`` numbered from ``after``, or nothing at all if they are turned off."""
        if not self.config.diagnostics:
            return {}
        return {name: after + index for index, name in enumerate(names)}


def resolve(dictionary: Dictionary, config: Config) -> Plan:
    """Work out every generated name, id and packet layout.

    Ordering is by remote id or opcode throughout, so the local ids a given dictionary
    produces do not depend on the order its sections happened to be written in.  That
    matters because the local ids end up in the target's ground dictionary: a
    regeneration that reshuffled them would silently invalidate archived telemetry.
    """
    warnings: list[str] = []
    _check_name_overrides(dictionary, config)
    _check_port_hooks(dictionary, config)
    selection = _select(dictionary, config, warnings)
    namer = TypeNamer(
        namespace=config.namespace,
        prefix=config.type_prefix,
        overrides=dict(config.type_name_overrides),
    )
    spelling = Spelling(dictionary, namer, selection.mirrored)
    _check_type_name_overrides(dictionary, config, spelling, warnings)

    commands = _plan_members(_COMMANDS, dictionary, config, selection, warnings)
    events = _plan_members(_EVENTS, dictionary, config, selection, warnings)
    channels = _plan_members(_CHANNELS, dictionary, config, selection, warnings)
    packets = _plan_packets(dictionary, config, selection, channels, warnings)
    _check_string_sizes(dictionary, warnings)

    plan = Plan(
        dictionary=dictionary,
        config=config,
        spelling=spelling,
        commands=commands,
        events=events,
        channels=channels,
        packets=packets,
        warnings=tuple(warnings),
    )
    plan = _hook_ports(plan, warnings)
    return replace(plan, warnings=tuple(warnings))


# ---------------------------------------------------------------------------
# Explicitly chosen names
# ---------------------------------------------------------------------------


Target = TypeVar("Target")


def _checked_overrides(
    overrides: Mapping[Target, str],
    *,
    label: str,
    noun: str,
    known: Collection[Target],
    absent: str,
    check_identifier: Callable[[str, str], None],
) -> Iterator[tuple[Target, str, str]]:
    """Every override in ``overrides``, once what both tables mean is known to hold.

    Which is: that the identifier could name what it is naming, that the dictionary still
    has the thing being renamed -- the usual reason it does not is a dictionary that has
    moved on, so a near miss is suggested -- and that no two entries ask for one name.
    Whatever else a particular table means, its caller adds to what is yielded:
    ``(target, identifier, context)``, ``context`` being what a complaint opens with.
    """
    chosen: dict[str, Target] = {}
    for target, identifier in sorted(overrides.items(), key=lambda item: str(item[0])):
        context = f"{label} for {target}"
        check_identifier(identifier, context)
        if target not in known:
            raise NamingError(
                f"{context}: {absent}"
                f"{_suggestion(str(target), [str(k) for k in known])}"
            )
        first = chosen.setdefault(identifier.casefold(), target)
        if first != target:
            raise NamingError(
                f"{context}: {identifier!r} is already the name chosen for {first}; "
                f"two {noun}s cannot share one name"
            )
        yield target, identifier, context


def _check_name_overrides(dictionary: Dictionary, config: Config) -> None:
    """Check every member name override against the dictionary and against itself.

    Everything that can be known before a single name is mangled is settled here, so
    that a mistake in an override table is reported once, in full, and before anything
    is written.
    """
    if not config.name_overrides:
        return
    known = {
        *(command.name for command in dictionary.commands),
        *(event.name for event in dictionary.events),
        *(channel.name for channel in dictionary.channels),
    }
    reserved = {name.casefold() for name in RESERVED_MEMBER_NAMES}
    for _, identifier, context in _checked_overrides(
        config.name_overrides,
        label="name override",
        noun="member",
        known=known,
        absent="matches no command, event or telemetry channel in the dictionary",
        check_identifier=check_member_identifier,
    ):
        if identifier.casefold() in reserved:
            raise NamingError(
                f"{context}: {identifier!r} is one of the names the proxy declares "
                "itself, so a member cannot take it"
            )


def _check_type_name_overrides(
    dictionary: Dictionary,
    config: Config,
    spelling: Spelling,
    warnings: list[str],
) -> None:
    """The same for type name overrides, against the types the proxy will emit."""
    if not config.type_name_overrides:
        return
    known = {definition.qualified_name for definition in dictionary.type_defs}
    emitted = {definition.qualified_name for definition in spelling.type_defs()}
    for qualified, identifier, context in _checked_overrides(
        config.type_name_overrides,
        label="type name override",
        noun="type",
        known=known,
        absent="matches no type in the dictionary",
        check_identifier=check_type_identifier,
    ):
        clash = [
            other
            for other in sorted(emitted)
            if other != qualified and spelling.namer.flat(other) == identifier
        ]
        if clash:
            raise NamingError(
                f"{context}: {identifier!r} is already how {clash[0]} is spelled; "
                "override that one too, or choose another name"
            )
        if qualified not in emitted:
            warnings.append(
                f"{context}: no mirrored member reaches that type, so the override has "
                "nothing to rename"
            )


def _suggestion(wanted: str, known: Iterable[str]) -> str:
    """`` (did you mean X?)``, or nothing if nothing is close enough."""
    close = difflib.get_close_matches(wanted, sorted(known), n=1, cutoff=0.8)
    return f" (did you mean {close[0]}?)" if close else ""


# ---------------------------------------------------------------------------
# Hooked ports: what the configuration asked for
# ---------------------------------------------------------------------------

#: What each port table is called in a complaint about one of its entries.
_PORT_LABELS = {
    PortKind.COMMAND: "command port",
    PortKind.EVENT: "event port",
    PortKind.TELEMETRY: "telemetry port",
    PortKind.PACKET: "packet port",
}


def _packet_names(dictionary: Dictionary) -> set[str]:
    """Every telemetry packet, named as :attr:`Config.packet_ports` keys one."""
    return {
        f"{packet_set.name}.{packet.name}"
        for packet_set in dictionary.packet_sets
        for packet in packet_set.packets
    }


def _port_tables(
    dictionary: Dictionary, config: Config
) -> Iterator[tuple[PortKind, Mapping[Any, bool | str], Collection[Any], str]]:
    """Each port table, with what its keys may name and what to say when one does not."""
    yield (
        PortKind.COMMAND,
        config.command_ports,
        {command.name for command in dictionary.commands},
        "matches no command in the dictionary",
    )
    yield (
        PortKind.EVENT,
        config.event_ports,
        {event.name for event in dictionary.events},
        "matches no event in the dictionary",
    )
    yield (
        PortKind.TELEMETRY,
        config.telemetry_ports,
        {channel.name for channel in dictionary.channels},
        "matches no telemetry channel in the dictionary",
    )
    yield (
        PortKind.PACKET,
        config.packet_ports,
        _packet_names(dictionary),
        "matches no telemetry packet in the dictionary; a packet is named "
        "<packet set>.<packet>",
    )


def _check_port_hooks(dictionary: Dictionary, config: Config) -> None:
    """Check every port hook against the dictionary and against the other hooks.

    Port names share one FPP name group, so uniqueness is checked across all four tables at
    once -- and against the ports the proxy declares itself, which is what a hook called
    ``comOut`` would quietly take over.
    """
    if not config.packetized and _hooked(config.packet_ports):
        # Nothing would decode the packet, so the port would be declared and never
        # invoked.  Refused rather than dropped, for the reason every other unusable
        # request here is: a hook that silently does nothing is worse than one that
        # complains.
        raise NamingError(
            "packet ports were asked for but packetized telemetry is turned off, so "
            "nothing would decode the packets they hook: "
            + ", ".join(sorted(str(name) for name in _hooked(config.packet_ports)))
        )
    chosen: dict[str, str] = {}
    reserved = {name.casefold() for name in RESERVED_PORT_NAMES}
    for kind, table, known, absent in _port_tables(dictionary, config):
        for target, value in sorted(table.items(), key=lambda item: str(item[0])):
            context = f"{_PORT_LABELS[kind]} for {target}"
            if target not in known:
                raise NamingError(
                    f"{context}: {absent}"
                    f"{_suggestion(str(target), [str(k) for k in known])}"
                )
            if not isinstance(value, str):
                continue  # true takes the derived name, false is not a hook at all
            check_member_identifier(value, context)
            if value.casefold() in reserved:
                raise NamingError(
                    f"{context}: {value!r} is one of the ports the proxy declares itself, "
                    "so a hooked port cannot take it"
                )
            first = chosen.setdefault(value.casefold(), context)
            if first != context:
                raise NamingError(
                    f"{context}: {value!r} is already the name chosen by the {first}; "
                    "two ports cannot share one name"
                )


def _hooked(table: Mapping[Any, bool | str]) -> set[Any]:
    """The keys of ``table`` that ask for a port; ``false`` asks for nothing."""
    return {target for target, value in table.items() if value}


# ---------------------------------------------------------------------------
# Selection: which members the component declares, and which it decodes
# ---------------------------------------------------------------------------

#: Text that makes an :attr:`Config.omit` entry a pattern rather than a name.
_GLOB = re.compile(r"[*?\[]")


@dataclass(frozen=True)
class _KindSelection:
    """What one member kind keeps, by dictionary name."""

    present: frozenset[QualifiedName]
    """Members that reach a plan at all; the rest may as well not be in the dictionary."""

    declared: frozenset[QualifiedName]
    """Of those, the ones the component declares and so gives a local id to."""


@dataclass(frozen=True)
class _Selection:
    """What the configuration selected out of the dictionary."""

    commands: _KindSelection
    events: _KindSelection
    channels: _KindSelection
    packets: frozenset[str]
    """The packets worth decoding, by qualified name."""

    mirrored: Mirrored
    """The same thing said as :class:`~.spelling.Mirrored`, for type reachability."""


def _member_names(dictionary: Dictionary) -> set[QualifiedName]:
    """Every command, event and channel name -- what :attr:`Config.omit` matches against."""
    return {
        *(command.name for command in dictionary.commands),
        *(event.name for event in dictionary.events),
        *(channel.name for channel in dictionary.channels),
    }


def _omitted(dictionary: Dictionary, config: Config) -> frozenset[QualifiedName]:
    """The members :attr:`Config.omit` names, by name or by glob.

    A pattern that matches nothing is refused rather than ignored; see :attr:`Config.omit`.
    A glob's ``*`` crosses the dots of a dictionary name, so ``Ref.dpDemo.*`` reaches a
    deeper instance path too and ``*`` on its own omits everything.
    """
    if not config.omit:
        return frozenset()
    known = {str(name): name for name in _member_names(dictionary)}
    omitted: set[QualifiedName] = set()
    for pattern in config.omit:
        matched = {
            name
            for spelled, name in known.items()
            if fnmatch.fnmatchcase(spelled, pattern)
        }
        if not matched:
            hint = "" if _GLOB.search(pattern) else _suggestion(pattern, known)
            raise NamingError(
                f"omit entry {pattern!r}: matches no command, event or telemetry channel "
                f"in the dictionary{hint}"
            )
        omitted |= matched
    return frozenset(omitted)


def _select(dictionary: Dictionary, config: Config, warnings: list[str]) -> _Selection:
    """Work out what each member kind keeps, and what that leaves for the packets.

    Commands and events are self-contained on the wire -- one packet carries one of them --
    so a member with neither a declaration nor a port is dropped outright and its argument
    types come out of the component with it.  Channels are not: ``Svc::TlmChan`` packs them
    end to end with no lengths, so reaching the *next* channel in a buffer means knowing the
    type of the one nobody asked for.  So as long as any channel is reported at all, every
    channel stays present, and an omitted one is read and discarded.
    """
    omitted = _omitted(dictionary, config)
    commands = _kind_selection(
        (command.name for command in dictionary.commands),
        omitted,
        _hooked(config.command_ports),
    )
    events = _kind_selection(
        (event.name for event in dictionary.events),
        omitted,
        _hooked(config.event_ports),
    )
    channel_names = [channel.name for channel in dictionary.channels]
    reported = _kind_selection(channel_names, omitted, _hooked(config.telemetry_ports))
    packets = _kept_packets(dictionary, config, reported.present)
    channels = _KindSelection(
        present=(
            frozenset(channel_names)
            if reported.present
            else frozenset(_packet_members(dictionary, packets)) & set(channel_names)
        ),
        declared=reported.declared,
    )

    _report_omissions(dictionary, config, omitted, warnings)
    return _Selection(
        commands=commands,
        events=events,
        channels=channels,
        packets=packets,
        mirrored=Mirrored(
            commands=tuple(
                c for c in dictionary.commands if c.name in commands.present
            ),
            events=tuple(e for e in dictionary.events if e.name in events.present),
            channels=tuple(
                c for c in dictionary.channels if c.name in channels.present
            ),
        ),
    )


def _kind_selection(
    names: Iterable[QualifiedName],
    omitted: Collection[QualifiedName],
    ported: Collection[QualifiedName],
) -> _KindSelection:
    """One kind's selection: declared unless omitted, present unless neither applies."""
    every = list(names)
    declared = frozenset(name for name in every if name not in omitted)
    return _KindSelection(
        present=declared | frozenset(name for name in every if name in ported),
        declared=declared,
    )


def _kept_packets(
    dictionary: Dictionary, config: Config, reported: Collection[QualifiedName]
) -> frozenset[str]:
    """The packets worth decoding: ones with something to report, or a port of their own.

    A packet nobody reads is not an error, just work the component does not have to do; the
    channels it names are dropped with it unless something else wants them.
    """
    if not config.packetized:
        return frozenset()
    hooked = _hooked(config.packet_ports)
    kept: set[str] = set()
    for packet_set, packet in _packets(dictionary):
        qualified = f"{packet_set.name}.{packet.name}"
        if qualified in hooked or any(member in reported for member in packet.members):
            kept.add(qualified)
    return frozenset(kept)


def _packets(
    dictionary: Dictionary,
) -> Iterator[tuple[TelemetryPacketSet, TelemetryPacket]]:
    """Every packet, with the set it came from, ordered by id within each set."""
    for packet_set in dictionary.packet_sets:
        for packet in sorted(packet_set.packets, key=lambda packet: packet.id):
            yield packet_set, packet


def _packet_members(
    dictionary: Dictionary, kept: Collection[str]
) -> set[QualifiedName]:
    """Every channel the kept packets name, whether or not the dictionary declares it."""
    return {
        member
        for packet_set, packet in _packets(dictionary)
        if f"{packet_set.name}.{packet.name}" in kept
        for member in packet.members
    }


def _report_omissions(
    dictionary: Dictionary,
    config: Config,
    omitted: Collection[QualifiedName],
    warnings: list[str],
) -> None:
    """Say what omission cost: renumbered ids, and any override left with nothing to do."""
    if not omitted:
        return
    counts = [
        (noun, sum(1 for name in names if name in omitted))
        for noun, names in (
            ("command", [c.name for c in dictionary.commands]),
            ("event", [e.name for e in dictionary.events]),
            ("channel", [c.name for c in dictionary.channels]),
        )
    ]
    listed = ", ".join(f"{count} {noun}(s)" for noun, count in counts if count)
    warnings.append(
        f"omitted {listed}; local ids are dense over what remains, so the ids of the "
        "members after each omission move, and any ground archive keyed on the old ones "
        "no longer reads"
    )
    ported = (
        _hooked(config.command_ports)
        | _hooked(config.event_ports)
        | _hooked(config.telemetry_ports)
    )
    idle = sorted(
        str(name)
        for name in config.name_overrides
        if name in omitted and name not in ported
    )
    if idle:
        warnings.append(
            f"{len(idle)} name override(s) name a member that is omitted and has no port, "
            f"so there is nothing left to rename: {', '.join(idle[:5])}"
            + (", ..." if len(idle) > 5 else "")
        )


def _mangle(
    names: list[QualifiedName], config: Config, kind: str, warnings: list[str]
) -> dict[QualifiedName, str]:
    """Assign identifiers for one member kind, noting any that had to be adjusted."""
    wanted = {name for name in names if name in config.name_overrides}
    assigned = mangle_all(
        names,
        config.naming,
        reserved=RESERVED_MEMBER_NAMES,
        overrides={name: config.name_overrides[name] for name in wanted},
    )
    adjusted = [
        (name, identifier)
        for name, identifier in assigned.items()
        if name not in wanted and identifier != config.naming.apply(name)
    ]
    if adjusted:
        examples = ", ".join(f"{n} -> {i}" for n, i in sorted(adjusted)[:5])
        warnings.append(
            f"{len(adjusted)} {kind} name(s) needed extra qualification to stay "
            f"unique: {examples}" + (", ..." if len(adjusted) > 5 else "")
        )
    return assigned


Planned = TypeVar("Planned", bound=MemberPlan[Any])


@dataclass(frozen=True)
class _Kind(Generic[Source, Planned]):
    """One member kind, as far as planning it has to tell it from the other two.

    Commands, events and channels are planned identically -- ordered by the remote's own
    value, mangled together, numbered densely from zero, and checked for a reused value --
    so :func:`_plan_members` does all three, and this is everything it needs to know
    about which one it is doing.
    """

    noun: str
    """What one is called, e.g. ``"command"``."""

    identity: str
    """What the remote's value for one is called, e.g. ``"command opcode"``."""

    consequence: str
    """What two of them sharing one means, said after "the dictionary reuses ...;"."""

    plan: type[Planned]
    members: Callable[[Dictionary], Sequence[Source]]
    remote: Callable[[Source], int]
    selection: Callable[[_Selection], _KindSelection]


_COMMANDS: _Kind[Command, CommandPlan] = _Kind(
    noun="command",
    identity="command opcode",
    consequence="the proxy will forward the same opcode for each, which is what the "
    "remote does too",
    plan=CommandPlan,
    members=lambda dictionary: dictionary.commands,
    remote=lambda command: command.opcode,
    selection=lambda selection: selection.commands,
)

_EVENTS: _Kind[Event, EventPlan] = _Kind(
    noun="event",
    identity="event id",
    consequence="an inbound packet carrying one is ambiguous, and the proxy will emit "
    "only the first match",
    plan=EventPlan,
    members=lambda dictionary: dictionary.events,
    remote=lambda event: event.id,
    selection=lambda selection: selection.events,
)

_CHANNELS: _Kind[Channel, ChannelPlan] = _Kind(
    noun="channel",
    identity="channel id",
    consequence="an inbound packet carrying one is ambiguous, and the proxy will write "
    "only the first",
    plan=ChannelPlan,
    members=lambda dictionary: dictionary.channels,
    remote=lambda channel: channel.id,
    selection=lambda selection: selection.channels,
)


def _plan_members(
    kind: _Kind[Source, Planned],
    dictionary: Dictionary,
    config: Config,
    selection: _Selection,
    warnings: list[str],
) -> tuple[Planned, ...]:
    """Name and number one kind of member.

    Ordering is by the remote's value and then by the name, so that a dictionary's local
    ids do not depend on the order its sections happened to be written in.  Local ids
    number only the members the component *declares*, so an omitted one takes part in
    naming -- a hooked port's name is built from its identifier -- and in nothing else.
    """
    chosen = kind.selection(selection)
    ordered = sorted(
        (
            member
            for member in kind.members(dictionary)
            if member.name in chosen.present
        ),
        key=lambda member: (kind.remote(member), str(member.name)),
    )
    identifiers = _mangle([m.name for m in ordered], config, kind.noun, warnings)
    reused = _duplicates(kind.remote(member) for member in ordered)
    if reused:
        warnings.append(
            f"the dictionary reuses {kind.identity}(s) {sorted(reused)}; "
            f"{kind.consequence}"
        )
    planned: list[Planned] = []
    local_id = 0
    for member in ordered:
        assigned: int | None = None
        if member.name in chosen.declared:
            assigned = local_id
            local_id += 1
        planned.append(
            kind.plan(
                source=member,
                identifier=identifiers[member.name],
                local_id=assigned,
            )
        )
    return tuple(planned)


def _plan_packets(
    dictionary: Dictionary,
    config: Config,
    selection: _Selection,
    channels: tuple[ChannelPlan, ...],
    warnings: list[str],
) -> tuple[PacketPlan, ...]:
    """Bind each packet's member names to planned channels, preserving wire order."""
    if not config.packetized:
        return ()
    declared = {channel.name for channel in dictionary.channels}
    by_name = {plan.source.name: plan for plan in channels}
    packets: list[PacketPlan] = []
    for packet_set, packet in _packets(dictionary):
        # Checked before the selection is consulted, because a packet naming a channel
        # the dictionary does not declare is a broken dictionary either way, and saying
        # so is more use than dropping it for a second reason.
        missing = [str(member) for member in packet.members if member not in declared]
        if missing:
            # A packet whose layout we cannot fully reconstruct cannot be decoded
            # at all: every channel after the unknown one sits at an unknown offset.
            warnings.append(
                f"packet {packet_set.name}.{packet.name} (id {packet.id}) names "
                f"{len(missing)} channel(s) absent from telemetryChannels "
                f"({', '.join(missing[:3])}); the packet cannot be decoded and is "
                "skipped"
            )
            continue
        if f"{packet_set.name}.{packet.name}" not in selection.packets:
            continue
        packets.append(
            PacketPlan(
                source=packet,
                channels=tuple(by_name[member] for member in packet.members),
                set_name=packet_set.name,
            )
        )
    ids = _duplicates(p.id for p in packets)
    if ids:
        warnings.append(
            f"telemetry packet id(s) {sorted(ids)} are declared more than once; only "
            "the first layout for each will be decoded"
        )
    return tuple(packets)


# ---------------------------------------------------------------------------
# Hooked ports: naming them and attaching them
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Hook:
    """One port the configuration asked for, before it has been named.

    Naming is a separate pass because it is the one part that cannot be decided one hook at
    a time: port names share a name group, so a derived name has to see the chosen ones.
    """

    kind: PortKind
    identifier: str
    """What the derived name and the port type's name are built from."""

    origin: str
    annotation: str
    params: tuple[PortParam, ...]
    requested: bool | str
    """What the table said: ``True`` for the derived name, or the name to use."""


def _hook_ports(plan: Plan, warnings: list[str]) -> Plan:
    """Attach a :class:`PortPlan` to every member the port tables named.

    Done after the members are planned rather than during, because a port's derived name and
    type come from the member's *identifier*, which mangling only settles once it has seen
    every name of that kind.
    """
    config = plan.config
    placed: list[tuple[str, int]] = []
    hooks: list[_Hook] = []

    def collect(field_name: str, hook: _Hook | None, index: int) -> None:
        if hook is not None:
            placed.append((field_name, index))
            hooks.append(hook)

    for index, command in enumerate(plan.commands):
        collect("commands", _command_hook(plan, command), index)
    for index, event in enumerate(plan.events):
        collect("events", _event_hook(plan, event), index)
    for index, channel in enumerate(plan.channels):
        collect("channels", _channel_hook(plan, channel), index)
    for index, packet in enumerate(plan.packets):
        collect("packets", _packet_hook(plan, packet), index)

    if not hooks:
        return plan

    ports = _named_ports(plan, hooks, warnings)
    updated: dict[str, list[Any]] = {
        "commands": list(plan.commands),
        "events": list(plan.events),
        "channels": list(plan.channels),
        "packets": list(plan.packets),
    }
    for (field_name, index), port in zip(placed, ports):
        collection = updated[field_name]
        collection[index] = replace(collection[index], port=port)
    return replace(
        plan,
        commands=tuple(updated["commands"]),
        events=tuple(updated["events"]),
        channels=tuple(updated["channels"]),
        packets=tuple(updated["packets"]),
    )


def _named_ports(
    plan: Plan, hooks: Sequence[_Hook], warnings: list[str]
) -> list[PortPlan]:
    """Give every hook a port name and a port type name, both unique.

    The chosen names are registered first, so that a derived name colliding with one of them
    is the derived one that moves -- the same rule :func:`~.naming.mangle_all` follows for
    member names, and for the same reason.
    """
    taken = {name.casefold() for name in RESERVED_PORT_NAMES}
    taken.update(
        hook.requested.casefold() for hook in hooks if isinstance(hook.requested, str)
    )
    types = {
        plan.spelling.namer.flat(definition.qualified_name).casefold()
        for definition in plan.spelling.type_defs()
    }
    adjusted: list[str] = []
    input_kind = "async input port" if plan.config.kind.has_queue else "sync input port"

    ports: list[PortPlan] = []
    for hook in hooks:
        if isinstance(hook.requested, str):
            name = hook.requested
        else:
            name = _claimed(f"{hook.identifier}{hook.kind.suffix}", taken, adjusted)
        ports.append(
            PortPlan(
                kind=hook.kind,
                name=name,
                type_name=_claimed(
                    f"{hook.identifier}{hook.kind.type_suffix}", types, adjusted
                ),
                declaration=input_kind if hook.kind.is_input else "output port",
                params=hook.params,
                origin=hook.origin,
                annotation=hook.annotation,
            )
        )
    if adjusted:
        warnings.append(
            f"{len(adjusted)} generated port name(s) needed a suffix to stay unique: "
            + ", ".join(sorted(adjusted)[:5])
            + (", ..." if len(adjusted) > 5 else "")
        )
    return ports


def _claimed(base: str, taken: set[str], adjusted: list[str]) -> str:
    """``base`` if it is free, else ``base_2``, ``base_3``, ...; either way, claimed."""
    if base.casefold() not in taken:
        taken.add(base.casefold())
        return base
    index = 2
    while f"{base}_{index}".casefold() in taken:
        index += 1
    chosen = f"{base}_{index}"
    taken.add(chosen.casefold())
    adjusted.append(f"{base} -> {chosen}")
    return chosen


def _value_param(plan: Plan, param: FormalParam) -> PortParam:
    """One of a member's own formals, as a port formal."""
    return _typed_param(plan, param.name, param.type, param.annotation or "")


def _typed_param(plan: Plan, name: str, ref: TypeRef, annotation: str) -> PortParam:
    """A port formal carrying a dictionary type."""
    return PortParam(
        name=name,
        fpp=plan.spelling.fpp_type(ref),
        cpp=plan.spelling.cpp_param(ref, Context.PORT),
        annotation=annotation,
        string_size=ref.size if ref.kind is TypeKind.STRING else None,
    )


def _time_param(name: str, annotation: str) -> PortParam:
    """The ``Fw.Time`` formal a telemetry port carries."""
    return PortParam(name=name, fpp=_TIME_FPP, cpp=_TIME_CPP, annotation=annotation)


def _command_hook(plan: Plan, command: CommandPlan) -> _Hook | None:
    requested = plan.config.command_ports.get(command.source.name, False)
    if not requested:
        return None
    reporting = (
        "\nA failure to send is reported as ProxyForwardError, since a port carries no\n"
        "status back to its caller."
        if plan.config.diagnostics
        else "\nNothing is reported back: a port carries no status to its caller."
    )
    return _Hook(
        kind=PortKind.COMMAND,
        identifier=command.identifier,
        origin=str(command.source.name),
        annotation=(
            f"Forwards {command.source.name} to the remote deployment.\n"
            f"\n"
            f"Invoking this serializes remote opcode {command.remote_opcode} "
            f"(0x{command.remote_opcode:X}) and the\n"
            f"arguments below into a command packet and sends it out comOut."
            f"{reporting}"
        ),
        params=tuple(_value_param(plan, param) for param in command.source.params),
        requested=requested,
    )


def _event_hook(plan: Plan, event: EventPlan) -> _Hook | None:
    requested = plan.config.event_ports.get(event.source.name, False)
    if not requested:
        return None
    return _Hook(
        kind=PortKind.EVENT,
        identifier=event.identifier,
        origin=str(event.source.name),
        annotation=(
            f"Invoked when {event.source.name} arrives from the remote deployment.\n"
            f"\n"
            f"Remote id {event.remote_id} (0x{event.remote_id:X}), severity "
            f"{event.source.severity.value}."
        ),
        params=tuple(_value_param(plan, param) for param in event.source.params),
        requested=requested,
    )


def _channel_hook(plan: Plan, channel: ChannelPlan) -> _Hook | None:
    requested = plan.config.telemetry_ports.get(channel.source.name, False)
    if not requested:
        return None
    return _Hook(
        kind=PortKind.TELEMETRY,
        identifier=channel.identifier,
        origin=str(channel.source.name),
        annotation=(
            f"Invoked when {channel.source.name} arrives from the remote deployment.\n"
            f"\n"
            f"Remote id {channel.remote_id} (0x{channel.remote_id:X})."
        ),
        params=(
            _typed_param(
                plan,
                _VALUE_FORMAL,
                channel.source.type,
                "The value the remote reported",
            ),
            _time_param(_TIME_FORMAL, "The time the remote reported it at"),
        ),
        requested=requested,
    )


def _packet_hook(plan: Plan, packet: PacketPlan) -> _Hook | None:
    requested = plan.config.packet_ports.get(packet.qualified_name, False)
    if not requested:
        return None
    return _Hook(
        kind=PortKind.PACKET,
        identifier=packet.name,
        origin=packet.qualified_name,
        annotation=(
            f"Invoked when telemetry packet {packet.qualified_name} arrives from the "
            f"remote deployment.\n"
            f"\n"
            f"Remote id {packet.id} (0x{packet.id:X}), {len(packet.channels)} channels.  "
            f"Svc.TlmPacketizer keeps\n"
            f"one timestamp for the whole packet, so every channel in it shares the last "
            f"formal."
        ),
        params=(
            *(
                _typed_param(
                    plan,
                    channel.identifier,
                    channel.source.type,
                    f"[{channel.source.name}] remote id {channel.remote_id}",
                )
                for channel in packet.channels
            ),
            _time_param(
                _unused_formal(_TIME_FORMAL, packet.channels),
                "The time the remote reported the packet at",
            ),
        ),
        requested=requested,
    )


def _unused_formal(wanted: str, channels: Sequence[ChannelPlan]) -> str:
    """``wanted``, lengthened until no channel of the packet is already called that.

    A channel called ``timeTag`` is not something a dictionary is likely to hold, but a
    duplicated formal is an FPP error rather than something to find out about later.
    """
    used = {channel.identifier.casefold() for channel in channels}
    while wanted.casefold() in used:
        wanted += "_"
    return wanted


def _check_string_sizes(dictionary: Dictionary, warnings: list[str]) -> None:
    """Note any declared string capacity that F Prime's defaults will clamp.

    The generator cannot fix this and does not try: the limits are build configuration,
    and the remote clamped the same way when it produced the packet.  What matters is
    that the two deployments are configured alike, so the numbers are worth stating.
    """
    oversized: dict[str, list[tuple[str, int]]] = {}
    for context, where, size in _declared_strings(dictionary):
        if size > STRING_LIMITS[context].size:
            oversized.setdefault(context, []).append((where, size))

    for context, entries in sorted(oversized.items()):
        limit = STRING_LIMITS[context]
        largest = max(size for _, size in entries)
        example = min(entries, key=lambda entry: entry[0])[0]
        warnings.append(
            f"{len(entries)} {context}(s) declare a string longer than F Prime's default "
            f"{limit.constant} of {limit.size} (largest is {largest}, e.g. {example}); "
            f"such a value is truncated to the limit on both sides, so the remote and "
            f"this deployment must configure {limit.constant} identically"
        )


def _declared_strings(dictionary: Dictionary) -> Iterator[tuple[str, str, int]]:
    """Every string a member declares a capacity for: its context, where, and how big.

    The contexts are :data:`STRING_LIMITS`' keys, since a declared size only means
    anything against the limit for the place it was declared in.
    """
    for context, where, declared in _typed_members(dictionary):
        if declared.kind is TypeKind.STRING and declared.size is not None:
            yield context, where, declared.size


def _typed_members(dictionary: Dictionary) -> Iterator[tuple[str, str, TypeRef]]:
    """Every value a member carries, in the context that decides how it is serialized."""
    for command in dictionary.commands:
        for param in command.params:
            yield "command argument", f"{command.name}.{param.name}", param.type
    for event in dictionary.events:
        for param in event.params:
            yield "event argument", f"{event.name}.{param.name}", param.type
    for channel in dictionary.channels:
        yield "telemetry channel", str(channel.name), channel.type


def _duplicates(values: Iterable[int]) -> set[int]:
    """Every value that appears more than once."""
    seen: set[int] = set()
    repeated: set[int] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return repeated
