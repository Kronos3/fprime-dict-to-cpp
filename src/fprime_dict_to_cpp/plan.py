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
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from .model import (
    Channel,
    Command,
    ComponentKind,
    Dictionary,
    Event,
    NamingError,
    QualifiedName,
    TelemetryPacket,
    TypeKind,
    TypeRef,
)
from .naming import Naming, check_member_identifier, mangle_all
from .spelling import Spelling, TypeNamer, check_type_identifier

__all__ = [
    "DIAGNOSTIC_CHANNELS",
    "DIAGNOSTIC_EVENTS",
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

#: Names no mirrored member may take.
RESERVED_MEMBER_NAMES = (
    *RESERVED_PORT_NAMES,
    *DIAGNOSTIC_CHANNELS,
    *DIAGNOSTIC_EVENTS,
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
    """The proxy's member name for it."""

    local_id: int
    """Its id in the proxy's own FPP, dense from zero."""

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
        """The opcode in the proxy's own FPP, dense from zero."""
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

    @property
    def id(self) -> int:
        return self.source.id

    @property
    def name(self) -> str:
        return self.source.name


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
    def diagnostic_channel_ids(self) -> dict[str, int]:
        """Local telemetry ids for the proxy's own channels, after the mirrored ones."""
        return self._diagnostics(DIAGNOSTIC_CHANNELS, after=len(self.channels))

    @property
    def diagnostic_event_ids(self) -> dict[str, int]:
        """Local event ids for the proxy's own events, after the mirrored ones."""
        return self._diagnostics(DIAGNOSTIC_EVENTS, after=len(self.events))

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
    namer = TypeNamer(
        namespace=config.namespace,
        prefix=config.type_prefix,
        overrides=dict(config.type_name_overrides),
    )
    spelling = Spelling(dictionary, namer)
    _check_type_name_overrides(dictionary, config, spelling, warnings)

    commands = _plan_members(_COMMANDS, dictionary, config, warnings)
    events = _plan_members(_EVENTS, dictionary, config, warnings)
    channels = _plan_members(_CHANNELS, dictionary, config, warnings)
    packets = _plan_packets(dictionary, channels, warnings) if config.packetized else ()
    _check_string_sizes(dictionary, warnings)

    return Plan(
        dictionary=dictionary,
        config=config,
        spelling=spelling,
        commands=commands,
        events=events,
        channels=channels,
        packets=packets,
        warnings=tuple(warnings),
    )


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


_COMMANDS: _Kind[Command, CommandPlan] = _Kind(
    noun="command",
    identity="command opcode",
    consequence="the proxy will forward the same opcode for each, which is what the "
    "remote does too",
    plan=CommandPlan,
    members=lambda dictionary: dictionary.commands,
    remote=lambda command: command.opcode,
)

_EVENTS: _Kind[Event, EventPlan] = _Kind(
    noun="event",
    identity="event id",
    consequence="an inbound packet carrying one is ambiguous, and the proxy will emit "
    "only the first match",
    plan=EventPlan,
    members=lambda dictionary: dictionary.events,
    remote=lambda event: event.id,
)

_CHANNELS: _Kind[Channel, ChannelPlan] = _Kind(
    noun="channel",
    identity="channel id",
    consequence="an inbound packet carrying one is ambiguous, and the proxy will write "
    "only the first",
    plan=ChannelPlan,
    members=lambda dictionary: dictionary.channels,
    remote=lambda channel: channel.id,
)


def _plan_members(
    kind: _Kind[Source, Planned],
    dictionary: Dictionary,
    config: Config,
    warnings: list[str],
) -> tuple[Planned, ...]:
    """Name and number one kind of member.

    Ordering is by the remote's value and then by the name, so that a dictionary's local
    ids do not depend on the order its sections happened to be written in.
    """
    ordered = sorted(
        kind.members(dictionary),
        key=lambda member: (kind.remote(member), str(member.name)),
    )
    identifiers = _mangle([m.name for m in ordered], config, kind.noun, warnings)
    reused = _duplicates(kind.remote(member) for member in ordered)
    if reused:
        warnings.append(
            f"the dictionary reuses {kind.identity}(s) {sorted(reused)}; "
            f"{kind.consequence}"
        )
    return tuple(
        kind.plan(source=member, identifier=identifiers[member.name], local_id=index)
        for index, member in enumerate(ordered)
    )


def _plan_packets(
    dictionary: Dictionary,
    channels: tuple[ChannelPlan, ...],
    warnings: list[str],
) -> tuple[PacketPlan, ...]:
    """Bind each packet's member names to planned channels, preserving wire order."""
    by_name = {plan.source.name: plan for plan in channels}
    packets: list[PacketPlan] = []
    for packet_set in dictionary.packet_sets:
        for packet in sorted(packet_set.packets, key=lambda p: p.id):
            members: list[ChannelPlan] = []
            missing: list[str] = []
            for member in packet.members:
                plan = by_name.get(member)
                if plan is None:
                    missing.append(str(member))
                else:
                    members.append(plan)
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
            packets.append(PacketPlan(source=packet, channels=tuple(members)))
    ids = _duplicates(p.id for p in packets)
    if ids:
        warnings.append(
            f"telemetry packet id(s) {sorted(ids)} are declared more than once; only "
            "the first layout for each will be decoded"
        )
    return tuple(packets)


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
