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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

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
    "PacketPlan",
    "Plan",
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

#: F Prime's default capacity for a string in each context, and the constant that sets it.
#:
#: ``string size N`` does not produce an N-character C++ type at a component's API
#: boundary.  A command argument arrives as ``Fw::CmdStringArg``, an event argument and a
#: telemetry value are serialized through a clamp of ``FW_MIN(<limit>, N)``.  So a
#: declared size above the limit is not fully representable -- on the remote either, which
#: is why this is a fidelity note rather than a defect, but it does mean the two
#: deployments have to agree on the limit.
STRING_LIMITS = {
    "command argument": ("FW_CMD_STRING_MAX_SIZE", 40),
    "event argument": ("FW_LOG_STRING_MAX_SIZE", 200),
    "telemetry channel": ("FW_TLM_STRING_MAX_SIZE", 40),
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


@dataclass(frozen=True)
class CommandPlan:
    """One forwarded command."""

    source: Command
    identifier: str
    """The proxy's member name for it."""

    local_opcode: int
    """The opcode in the proxy's own FPP, dense from zero."""

    @property
    def remote_opcode(self) -> int:
        """The absolute opcode to put on the wire, as the remote knows it."""
        return self.source.opcode


@dataclass(frozen=True)
class EventPlan:
    """One mirrored event."""

    source: Event
    identifier: str
    local_id: int

    @property
    def remote_id(self) -> int:
        """The absolute event id to match an inbound packet against."""
        return self.source.id

    @property
    def log_function(self) -> str:
        """The autocoded emitter, e.g. ``log_ACTIVITY_LO_dpDemo_DpStarted``."""
        return f"log_{self.source.severity.cpp}_{self.identifier}"


@dataclass(frozen=True)
class ChannelPlan:
    """One mirrored telemetry channel."""

    source: Channel
    identifier: str
    local_id: int

    @property
    def remote_id(self) -> int:
        """The absolute channel id to match an inbound packet against."""
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
        if not self.config.diagnostics:
            return {}
        start = len(self.channels)
        return {name: start + index for index, name in enumerate(DIAGNOSTIC_CHANNELS)}

    @property
    def diagnostic_event_ids(self) -> dict[str, int]:
        """Local event ids for the proxy's own events, after the mirrored ones."""
        if not self.config.diagnostics:
            return {}
        start = len(self.events)
        return {name: start + index for index, name in enumerate(DIAGNOSTIC_EVENTS)}


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

    commands = _plan_commands(dictionary, config, warnings)
    events = _plan_events(dictionary, config, warnings)
    channels = _plan_channels(dictionary, config, warnings)
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
    reserved = {name.casefold(): name for name in RESERVED_MEMBER_NAMES}
    chosen: dict[str, QualifiedName] = {}
    for name, identifier in sorted(config.name_overrides.items(), key=_by_name):
        context = f"name override for {name}"
        check_member_identifier(identifier, context)
        if name not in known:
            raise NamingError(
                f"{context}: matches no command, event or telemetry channel in the "
                f"dictionary{_suggestion(str(name), [str(k) for k in known])}"
            )
        if identifier.casefold() in reserved:
            raise NamingError(
                f"{context}: {identifier!r} is one of the names the proxy declares "
                "itself, so a member cannot take it"
            )
        first = chosen.setdefault(identifier.casefold(), name)
        if first != name:
            raise NamingError(
                f"{context}: {identifier!r} is already the name chosen for {first}; "
                "two members cannot share one name"
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
    chosen: dict[str, str] = {}
    for qualified, identifier in sorted(config.type_name_overrides.items()):
        context = f"type name override for {qualified}"
        check_type_identifier(identifier, context)
        if qualified not in known:
            raise NamingError(
                f"{context}: matches no type in the dictionary"
                f"{_suggestion(qualified, known)}"
            )
        first = chosen.setdefault(identifier.casefold(), qualified)
        if first != qualified:
            raise NamingError(
                f"{context}: {identifier!r} is already the name chosen for {first}; "
                "two types cannot share one name"
            )
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


def _by_name(item: tuple[QualifiedName, str]) -> tuple[str, ...]:
    return item[0].parts


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


def _plan_commands(
    dictionary: Dictionary, config: Config, warnings: list[str]
) -> tuple[CommandPlan, ...]:
    ordered = sorted(dictionary.commands, key=lambda c: (c.opcode, str(c.name)))
    identifiers = _mangle([c.name for c in ordered], config, "command", warnings)
    duplicates = _duplicate_ids([c.opcode for c in ordered])
    if duplicates:
        warnings.append(
            f"the dictionary reuses command opcode(s) {sorted(duplicates)}; the proxy "
            "will forward the same opcode for each, which is what the remote does too"
        )
    return tuple(
        CommandPlan(
            source=command, identifier=identifiers[command.name], local_opcode=index
        )
        for index, command in enumerate(ordered)
    )


def _plan_events(
    dictionary: Dictionary, config: Config, warnings: list[str]
) -> tuple[EventPlan, ...]:
    ordered = sorted(dictionary.events, key=lambda e: (e.id, str(e.name)))
    identifiers = _mangle([e.name for e in ordered], config, "event", warnings)
    duplicates = _duplicate_ids([e.id for e in ordered])
    if duplicates:
        warnings.append(
            f"the dictionary reuses event id(s) {sorted(duplicates)}; an inbound packet "
            "carrying one is ambiguous, and the proxy will emit only the first match"
        )
    return tuple(
        EventPlan(source=event, identifier=identifiers[event.name], local_id=index)
        for index, event in enumerate(ordered)
    )


def _plan_channels(
    dictionary: Dictionary, config: Config, warnings: list[str]
) -> tuple[ChannelPlan, ...]:
    ordered = sorted(dictionary.channels, key=lambda c: (c.id, str(c.name)))
    identifiers = _mangle([c.name for c in ordered], config, "channel", warnings)
    duplicates = _duplicate_ids([c.id for c in ordered])
    if duplicates:
        warnings.append(
            f"the dictionary reuses channel id(s) {sorted(duplicates)}; an inbound "
            "packet carrying one is ambiguous, and the proxy will write only the first"
        )
    return tuple(
        ChannelPlan(
            source=channel, identifier=identifiers[channel.name], local_id=index
        )
        for index, channel in enumerate(ordered)
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
    ids = _duplicate_ids([p.id for p in packets])
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

    def note(context: str, where: str, size: int | None) -> None:
        if size is None:
            return
        _, limit = STRING_LIMITS[context]
        if size > limit:
            oversized.setdefault(context, []).append((where, size))

    for command in dictionary.commands:
        for param in command.params:
            if param.type.kind is TypeKind.STRING:
                note(
                    "command argument", f"{command.name}.{param.name}", param.type.size
                )
    for event in dictionary.events:
        for param in event.params:
            if param.type.kind is TypeKind.STRING:
                note("event argument", f"{event.name}.{param.name}", param.type.size)
    for channel in dictionary.channels:
        if channel.type.kind is TypeKind.STRING:
            note("telemetry channel", str(channel.name), channel.type.size)

    for context, entries in sorted(oversized.items()):
        constant, limit = STRING_LIMITS[context]
        largest = max(size for _, size in entries)
        example = min(entries, key=lambda entry: entry[0])[0]
        warnings.append(
            f"{len(entries)} {context}(s) declare a string longer than F Prime's default "
            f"{constant} of {limit} (largest is {largest}, e.g. {example}); such a value "
            f"is truncated to the limit on both sides, so the remote and this deployment "
            f"must configure {constant} identically"
        )


def _duplicate_ids(ids: list[int]) -> set[int]:
    seen: set[int] = set()
    duplicates: set[int] = set()
    for identifier in ids:
        if identifier in seen:
            duplicates.add(identifier)
        seen.add(identifier)
    return duplicates
