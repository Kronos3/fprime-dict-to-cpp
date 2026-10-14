"""The dictionary model: what a JSON dictionary says, in the shape a generator wants.

This is deliberately a *lossless* view of the dictionary sections the generator
consumes.  Nothing here knows about FPP or C++ -- see :mod:`.spelling` for that --
and nothing here mangles a name, which is :mod:`.naming`'s job.

The one piece of interpretation this module does perform is resolving type
references, because a proxy component has to agree with the *remote* system's type
widths rather than its own platform's.  A dictionary carries the remote's
``FwOpcodeType``, ``FwEventIdType`` and friends in its ``typeDefinitions``, so the
widths a packet parser needs are all knowable from the file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union

__all__ = [
    "AliasDef",
    "ArrayDef",
    "Channel",
    "Command",
    "CommandKind",
    "ComponentKind",
    "ConstantDef",
    "Dictionary",
    "EnumConstant",
    "EnumDef",
    "Event",
    "FormalParam",
    "Limit",
    "Limits",
    "Metadata",
    "PlatformTypes",
    "QualifiedName",
    "QueueFullBehavior",
    "Severity",
    "StructDef",
    "StructMember",
    "TelemetryPacket",
    "TelemetryPacketSet",
    "TelemetryUpdate",
    "TypeDef",
    "TypeRef",
]


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class QualifiedName:
    """A dotted dictionary name, split into its qualifier and its final segment.

    Dictionary commands, events and channels are all named
    ``<deployment>.<instance>.<MEMBER>`` -- the qualifier identifies the component
    *instance* in the remote topology, and the last segment is the member as its
    component declared it.  Both halves matter to a name mangler, so both are kept.
    """

    parts: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> QualifiedName:
        return cls(tuple(p for p in text.split(".") if p))

    @property
    def member(self) -> str:
        """The final segment: the member name as the remote component declared it."""
        return self.parts[-1] if self.parts else ""

    @property
    def qualifier(self) -> tuple[str, ...]:
        """Everything before the member -- the remote component instance path."""
        return self.parts[:-1]

    @property
    def instance(self) -> str:
        """The innermost qualifier segment, e.g. ``"dpDemo"``.  Empty if unqualified."""
        return self.parts[-2] if len(self.parts) >= 2 else ""

    def __str__(self) -> str:
        return ".".join(self.parts)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class TypeKind(Enum):
    """The ``kind`` of a type reference in the dictionary."""

    INTEGER = "integer"
    FLOAT = "float"
    BOOL = "bool"
    STRING = "string"
    QUALIFIED = "qualifiedIdentifier"


@dataclass(frozen=True)
class TypeRef:
    """A type as referenced at a use site: an event argument, a channel, a member.

    ``name`` is the dictionary's spelling -- ``"U32"``, ``"string"``, or a qualified
    identifier such as ``"Ref.DpDemo.ColorEnum"``.  ``size`` is a width in bits for
    the numeric kinds and a capacity in characters for :attr:`TypeKind.STRING`.
    """

    kind: TypeKind
    name: str
    size: int | None = None
    signed: bool | None = None

    @classmethod
    def parse(cls, raw: dict[str, object]) -> TypeRef:
        name = raw.get("name")
        if not isinstance(name, str):
            raise DictionaryError(f"type reference has no name: {raw!r}")
        kind_text = raw.get("kind")
        if not isinstance(kind_text, str):
            raise DictionaryError(f"type reference {name!r} has no kind: {raw!r}")
        try:
            kind = TypeKind(kind_text)
        except ValueError as exc:
            raise DictionaryError(
                f"type reference {name!r} has unknown kind {kind_text!r}"
            ) from exc
        size = raw.get("size")
        signed = raw.get("signed")
        return cls(
            kind=kind,
            name=name,
            size=size if isinstance(size, int) else None,
            signed=signed if isinstance(signed, bool) else None,
        )

    @property
    def is_primitive(self) -> bool:
        """Whether this names a built-in type rather than a ``typeDefinitions`` entry."""
        return self.kind is not TypeKind.QUALIFIED


@dataclass(frozen=True)
class EnumConstant:
    """One enumerated constant."""

    name: str
    value: int
    annotation: str | None = None


@dataclass(frozen=True)
class EnumDef:
    """An FPP ``enum`` definition."""

    qualified_name: str
    representation: TypeRef
    constants: tuple[EnumConstant, ...]
    default: str | None = None
    annotation: str | None = None


@dataclass(frozen=True)
class ArrayDef:
    """An FPP ``array`` definition."""

    qualified_name: str
    size: int
    element: TypeRef
    default: object | None = None
    format: str | None = None
    annotation: str | None = None


@dataclass(frozen=True)
class StructMember:
    """One member of an FPP ``struct``, in declaration order.

    ``array_size`` is set when the member was declared as an inline array, e.g.
    ``arrayMemberU32: [2] Ref.DpDemo.U32Array``.
    """

    name: str
    type: TypeRef
    index: int
    array_size: int | None = None
    format: str | None = None
    annotation: str | None = None


@dataclass(frozen=True)
class StructDef:
    """An FPP ``struct`` definition, members already sorted by declaration index."""

    qualified_name: str
    members: tuple[StructMember, ...]
    default: object | None = None
    annotation: str | None = None


@dataclass(frozen=True)
class AliasDef:
    """An FPP ``type`` alias.

    ``target`` is what the alias was written as and ``underlying`` is that chased
    down to a built-in.  A proxy wants ``underlying``: it pins the width the *remote*
    actually put on the wire, which is not necessarily what the local platform uses.
    """

    qualified_name: str
    target: TypeRef
    underlying: TypeRef
    annotation: str | None = None


@dataclass(frozen=True)
class ConstantDef:
    """An FPP ``constant`` definition."""

    qualified_name: str
    type: TypeRef
    value: object
    annotation: str | None = None


#: Any entry of the dictionary's ``typeDefinitions`` section.
TypeDef = Union[EnumDef, ArrayDef, StructDef, AliasDef]


# ---------------------------------------------------------------------------
# Dictionary entries
# ---------------------------------------------------------------------------


class CommandKind(Enum):
    """How the *remote* component declared the command.

    ``SET`` and ``SAVE`` are the command pair FPP generates for a ``param``
    declaration.  A proxy is not the parameter's owner -- it only forwards the opcode
    -- so it re-declares those as ordinary commands; see
    :meth:`ComponentKind.command_kind`.
    """

    SYNC = "sync"
    GUARDED = "guarded"
    ASYNC = "async"
    SET = "set"
    SAVE = "save"

    @property
    def is_parameter_command(self) -> bool:
        """Whether this is an FPP-generated parameter ``set``/``save`` command."""
        return self in (CommandKind.SET, CommandKind.SAVE)


class ComponentKind(Enum):
    """The proxy component's own threading model.

    This decides *where* an inbound packet is decoded, and it constrains what the
    proxy's own commands may be: a passive component has no queue, so it cannot
    declare an ``async`` command or an ``async`` input port.
    """

    PASSIVE = "passive"
    QUEUED = "queued"
    ACTIVE = "active"

    @property
    def has_queue(self) -> bool:
        """Whether the component owns a message queue."""
        return self is not ComponentKind.PASSIVE

    @property
    def port_kind(self) -> str:
        """The FPP kind of the packet input port.

        Passive decodes on the caller's thread; queued and active take the message
        onto the queue, to be drained by the scheduler port or the component's own
        thread respectively.
        """
        return "async input port" if self.has_queue else "sync input port"

    def command_kind(self, remote: CommandKind) -> str:
        """The FPP kind for a forwarded command, given how the remote declared it.

        The proxy's handler only serializes an opcode and its arguments into a com
        buffer, so the remote's own kind carries no obligation here -- what matters is
        that the declaration is legal for this component kind.  A queue means
        ``async``, so commanding never blocks the caller; no queue means ``guarded``,
        since a passive proxy may still be shared between the thread decoding packets
        and the thread dispatching commands.
        """
        del remote  # the remote's threading model is not the proxy's
        return "async" if self.has_queue else "guarded"


class ComInput(Enum):
    """The port type packets from the remote arrive on.

    ``com`` is ``Fw.Com``: a ``Fw::ComBuffer`` passed by value, as ``Svc.FprimeRouter``'s
    ``commandOut`` emits.  ``buffer`` is ``Fw.BufferSend``: a ``Fw::Buffer`` whose ownership
    is passed in and has to be handed back, as ``Svc.PassThroughRouter``'s
    ``allPacketsOut`` emits.
    """

    COM = "com"
    BUFFER = "buffer"

    @property
    def port_type(self) -> str:
        """The FPP port type of ``comIn``."""
        return "Fw.Com" if self is ComInput.COM else "Fw.BufferSend"


class QueueFullBehavior(Enum):
    """What an async command does when the receiving queue is full."""

    ASSERT = "assert"
    BLOCK = "block"
    DROP = "drop"


class Severity(Enum):
    """Event severity.  The value is the dictionary spelling; :attr:`fpp` and
    :attr:`cpp` give the two spellings a generator needs."""

    FATAL = "FATAL"
    WARNING_HI = "WARNING_HI"
    WARNING_LO = "WARNING_LO"
    COMMAND = "COMMAND"
    ACTIVITY_HI = "ACTIVITY_HI"
    ACTIVITY_LO = "ACTIVITY_LO"
    DIAGNOSTIC = "DIAGNOSTIC"

    @property
    def fpp(self) -> str:
        """The FPP keyword sequence, e.g. ``"activity high"``."""
        return _SEVERITY_FPP[self]

    @property
    def cpp(self) -> str:
        """The infix of the generated ``log_<SEVERITY>_<Name>`` function."""
        return self.value


_SEVERITY_FPP = {
    Severity.FATAL: "fatal",
    Severity.WARNING_HI: "warning high",
    Severity.WARNING_LO: "warning low",
    Severity.COMMAND: "command",
    Severity.ACTIVITY_HI: "activity high",
    Severity.ACTIVITY_LO: "activity low",
    Severity.DIAGNOSTIC: "diagnostic",
}


class TelemetryUpdate(Enum):
    """When a channel is emitted."""

    ALWAYS = "always"
    ON_CHANGE = "on change"

    @property
    def fpp(self) -> str:
        return f"update {self.value}"


@dataclass(frozen=True)
class Limit:
    """One side of a channel's limit set."""

    red: object | None = None
    orange: object | None = None
    yellow: object | None = None

    def __bool__(self) -> bool:
        return any(v is not None for v in (self.red, self.orange, self.yellow))


@dataclass(frozen=True)
class Limits:
    """A channel's low and high limit sets."""

    low: Limit = field(default_factory=Limit)
    high: Limit = field(default_factory=Limit)

    def __bool__(self) -> bool:
        return bool(self.low) or bool(self.high)


@dataclass(frozen=True)
class FormalParam:
    """One formal parameter of a command or event."""

    name: str
    type: TypeRef
    annotation: str | None = None
    ref: bool = False


@dataclass(frozen=True)
class Command:
    """One entry of the dictionary's ``commands`` section."""

    name: QualifiedName
    opcode: int
    kind: CommandKind
    params: tuple[FormalParam, ...] = ()
    queue_full_behavior: QueueFullBehavior | None = None
    annotation: str | None = None


@dataclass(frozen=True)
class Event:
    """One entry of the dictionary's ``events`` section."""

    name: QualifiedName
    id: int
    severity: Severity
    format: str
    params: tuple[FormalParam, ...] = ()
    throttle: int | None = None
    annotation: str | None = None


@dataclass(frozen=True)
class Channel:
    """One entry of the dictionary's ``telemetryChannels`` section."""

    name: QualifiedName
    id: int
    type: TypeRef
    update: TelemetryUpdate
    format: str | None = None
    limits: Limits = field(default_factory=Limits)
    annotation: str | None = None


@dataclass(frozen=True)
class TelemetryPacket:
    """One packet of a ``telemetryPacketSets`` entry.

    ``members`` is the wire order: ``Svc::TlmPacketizer`` writes each channel's value
    back to back with no per-channel id, so this order is the only thing that makes
    the packet decodable.
    """

    name: str
    id: int
    group: int
    members: tuple[QualifiedName, ...]


@dataclass(frozen=True)
class TelemetryPacketSet:
    """One entry of the dictionary's ``telemetryPacketSets`` section."""

    name: str
    packets: tuple[TelemetryPacket, ...]


@dataclass(frozen=True)
class Metadata:
    """The dictionary's ``metadata`` section."""

    deployment_name: str
    project_version: str = "unknown"
    framework_version: str = "unknown"
    spec_version: str = "unknown"

    @property
    def deployment_identifier(self) -> str:
        """The deployment name reduced to one identifier, e.g. ``"Ref.Ref"`` -> ``"Ref"``.

        A deployment name is conventionally ``<project>.<deployment>``; the two are
        usually equal, and where they are not the deployment half is the useful one.
        """
        parts = [p for p in self.deployment_name.split(".") if p]
        return parts[-1] if parts else "Unknown"


@dataclass(frozen=True)
class PlatformTypes:
    """The remote platform's type widths, as recorded in the dictionary.

    A proxy parses packet headers and string lengths with *these* widths, not the local
    platform's.  A
    dictionary that omits one gets the F Prime default, which is what the remote
    would have used had it not been reconfigured.
    """

    packet_descriptor: TypeRef
    opcode: TypeRef
    event_id: TypeRef
    channel_id: TypeRef
    packetize_id: TypeRef
    size_store: TypeRef
    """The width of a string's length prefix, which the proxy writes and reads itself."""

    #: The names looked up in ``typeDefinitions``, and the built-in each falls back to.
    DEFAULTS = {
        "packet_descriptor": ("FwPacketDescriptorType", "U32"),
        "opcode": ("FwOpcodeType", "U32"),
        "event_id": ("FwEventIdType", "U32"),
        "channel_id": ("FwChanIdType", "U32"),
        "packetize_id": ("FwTlmPacketizeIdType", "U16"),
        "size_store": ("FwSizeStoreType", "U16"),
    }


@dataclass(frozen=True)
class Dictionary:
    """A whole parsed JSON dictionary."""

    metadata: Metadata
    platform: PlatformTypes
    type_defs: tuple[TypeDef, ...] = ()
    constants: tuple[ConstantDef, ...] = ()
    commands: tuple[Command, ...] = ()
    events: tuple[Event, ...] = ()
    channels: tuple[Channel, ...] = ()
    packet_sets: tuple[TelemetryPacketSet, ...] = ()

    def type_def(self, qualified_name: str) -> TypeDef | None:
        """Look up a ``typeDefinitions`` entry by qualified name."""
        return self._by_name.get(qualified_name)

    @property
    def _by_name(self) -> dict[str, TypeDef]:
        cached = getattr(self, "_type_def_index", None)
        if cached is None:
            cached = {d.qualified_name: d for d in self.type_defs}
            object.__setattr__(self, "_type_def_index", cached)
        return cached

    def channel(self, name: QualifiedName) -> Channel | None:
        """Look up a channel by its full dictionary name."""
        cached = getattr(self, "_channel_index", None)
        if cached is None:
            cached = {c.name: c for c in self.channels}
            object.__setattr__(self, "_channel_index", cached)
        return cached.get(name)


class DictionaryError(Exception):
    """A dictionary could not be understood."""


class NamingError(Exception):
    """A name that was asked for explicitly cannot be used.

    Raised for an override -- see :attr:`~fprime_dict_to_cpp.plan.Config.name_overrides`
    and :attr:`~fprime_dict_to_cpp.plan.Config.type_name_overrides` -- that names nothing
    in the dictionary, that is not a usable identifier, or that collides with another
    generated name.  Declining such a request quietly would produce a component whose
    members are not the ones that were asked for.
    """
