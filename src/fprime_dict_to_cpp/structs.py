"""Resolving a dictionary into named structs, for ``kind: types``.

The other kinds generate a component that *speaks* the remote's interface.  This one
generates only the shapes of it: one struct per command, holding its arguments, and one
per telemetry packet, holding its channels in wire order, with a constant for each
opcode and packet id.  No component, no C++ and no ``CMakeLists.txt`` -- just FPP.

Everything is declared inside :attr:`~.plan.Config.namespace`: each command's struct
goes in a ``Command`` module and its opcode in an ``Opcode`` module as a constant of the
same name, and each packet's in ``Packet`` and ``PacketId`` -- so
``RefTypes.Command.dpDemo_SelectColor`` and ``RefTypes.Opcode.dpDemo_SelectColor``.

The dictionary is the only interface there is, so every type a struct reaches is defined
here too, beside the structs: nothing needs the remote's own FPP.  Names are flattened,
as a component's are -- ``Ref.DpDemo.ColorEnum`` becomes ``Ref_DpDemo_ColorEnum`` --
because ``fpp-to-cpp`` names a generated file after a type's leaf name alone, and the Ref
dictionary has two ``DpReqType``\\ s and two ``Enabled``\\ s.  Nesting them in modules
of their own would also put the remote's ``Fw.Enabled`` in a ``RefTypes::Fw`` namespace,
inside which the autocoded ``Fw::Serializable`` no longer finds the framework's.

A type the remote declared at the top level -- ``FwOpcodeType``, ``FwSizeType``,
``TimeBase`` -- is the remote *platform's* definition, and this project has its own by
the same name, which need not agree: a remote built with a 64-bit ``FwSizeType`` is
still 64-bit when this one is not.  So it is declared as ``Remote_<name>``, at the
remote's width, which also keeps its generated header from shadowing the framework's.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .model import (
    AliasDef,
    ArrayDef,
    ComInput,
    Command,
    Dictionary,
    EnumDef,
    FormalParam,
    NamingError,
    PlatformTypes,
    StructDef,
    TelemetryPacket,
    TelemetryPacketSet,
    TypeDef,
    TypeKind,
    TypeRef,
)
from .naming import mangle_all
from .plan import Config, _check_name_overrides, _claimed, _omitted, _packets
from .spelling import SHADOW_ESCAPE_PREFIX, TypeNamer

__all__ = [
    "COMMAND_MODULE",
    "OPCODE_FIELD",
    "OPCODE_MODULE",
    "PACKET_ID_FIELD",
    "PACKET_ID_MODULE",
    "PACKET_MODULE",
    "REMOTE_PREFIX",
    "CommandStruct",
    "PacketStruct",
    "StructField",
    "StructsPlan",
    "TypesConfigError",
    "resolve_structs",
    "type_name",
]

#: What a re-declared top-level type of the remote's is called: ``Remote_FwSizeType``.
REMOTE_PREFIX = SHADOW_ESCAPE_PREFIX

#: The module each command's struct is declared in: ``Command.dpDemo_SelectColor``.
COMMAND_MODULE = "Command"

#: The module each command's opcode constant is declared in.
OPCODE_MODULE = "Opcode"

#: The module each telemetry packet's struct is declared in: ``Packet.CDH``.
PACKET_MODULE = "Packet"

#: The module each telemetry packet's id constant is declared in.
PACKET_ID_MODULE = "PacketId"

#: What the opcode member of a command struct is called, unless an argument already is.
OPCODE_FIELD = "opcode"

#: The remote's opcode width, which the opcode member is declared with.
OPCODE_TYPE = PlatformTypes.DEFAULTS["opcode"][0]

#: What the id member of a packet struct is called, unless a channel already is.
PACKET_ID_FIELD = "id"

#: The remote's packet id width, which the id member is declared with.
PACKET_ID_TYPE = PlatformTypes.DEFAULTS["packetize_id"][0]


@dataclass(frozen=True)
class StructField:
    """One member of a generated struct."""

    name: str
    """Its name, unescaped; the emitter applies FPP's escape."""

    type: TypeRef
    annotation: str = ""
    default: str | None = None
    """What the struct's ``default`` clause gives it, if it has one: a constant's name."""


@dataclass(frozen=True)
class CommandStruct:
    """A command's arguments, as one struct."""

    source: Command
    name: str
    """The struct's name in :data:`COMMAND_MODULE`, and its opcode constant's in
    :data:`OPCODE_MODULE`."""

    fields: tuple[StructField, ...]


@dataclass(frozen=True)
class PacketStruct:
    """A telemetry packet's channels, in wire order, as one struct."""

    source: TelemetryPacket
    set_name: str
    name: str
    """The struct's name in :data:`PACKET_MODULE`, and its id constant's in
    :data:`PACKET_ID_MODULE`."""

    fields: tuple[StructField, ...]

    @property
    def qualified_name(self) -> str:
        """``<set name>.<packet name>``, as ``packet-ports`` keys a packet elsewhere."""
        return f"{self.set_name}.{self.source.name}"


@dataclass(frozen=True)
class StructsPlan:
    """A dictionary, resolved into the structs a ``types`` run emits."""

    dictionary: Dictionary
    config: Config
    commands: tuple[CommandStruct, ...]
    packets: tuple[PacketStruct, ...]
    type_defs: tuple[TypeDef, ...]
    """Every type the structs reach, through one another too, in dependency order."""

    warnings: tuple[str, ...] = ()

    def spell(self, ref: TypeRef) -> str:
        """The FPP spelling of ``ref`` in a generated file.

        A dictionary type as its generated definition is named, and a string with its
        capacity, since FPP's bare ``string`` would silently mean the local default.
        """
        if ref.kind is TypeKind.STRING:
            return f"string size {ref.size}" if ref.size is not None else "string"
        if ref.kind is TypeKind.QUALIFIED:
            return type_name(ref.name)
        return ref.name


def type_name(qualified: str) -> str:
    """What a dictionary type is defined as: ``Ref_DpDemo_ColorEnum``, or, for one the
    remote declared at the top level, ``Remote_FwSizeType``."""
    if _top_level(qualified):
        return f"{REMOTE_PREFIX}{qualified}"
    return _FLATTEN.flat(qualified)


#: Flattens a module-qualified name, escaping a C++ keyword as a component's types do.
_FLATTEN = TypeNamer(namespace="")


def _top_level(qualified: str) -> bool:
    """Whether the remote declared ``qualified`` outside of any module."""
    return "." not in qualified


class TypesConfigError(Exception):
    """An option that only means something for a component, given to a ``types`` run."""


def _check_types_config(config: Config) -> None:
    """Refuse every option a ``types`` run would otherwise have to ignore.

    Refused rather than ignored, for the reason an override matching nothing is: a
    configuration switched to ``types`` that still hooks ports is a file that no longer
    says what it does.
    """
    ignored = [
        key
        for key, given in (
            ("command-ports", config.command_ports),
            ("event-ports", config.event_ports),
            ("telemetry-ports", config.telemetry_ports),
            ("packet-ports", config.packet_ports),
            ("omit-events", config.omit_events),
            ("omit-telemetry", config.omit_telemetry),
            ("type-names", config.type_name_overrides),
            ("type-prefix", config.type_prefix),
            ("com-in", config.com_in is not ComInput.COM),
            *((key, path) for path, key in config.custom_keys),
        )
        if given
    ]
    if ignored:
        raise TypesConfigError(
            f"kind types generates no component, so {', '.join(ignored)} would have "
            "nothing to apply to; remove them, or choose a component kind"
        )


def resolve_structs(dictionary: Dictionary, config: Config) -> StructsPlan:
    """Name every struct and every field, and find every type they reach.

    Struct names follow :attr:`Config.naming`, as a component's members do; a packet's
    is the packet's own name.  Either moves out of the way of a generated type's name,
    since the two would otherwise ask fpp-to-cpp for one file.  A :attr:`Config.name_overrides` entry for a command names
    its struct outright; one for a channel names that channel's field in every packet that
    carries it.  The omit lists reach commands only: a packet's fields are its wire
    layout, so dropping one would describe a packet the remote never sends.
    """
    _check_types_config(config)
    _check_name_overrides(dictionary, config)
    warnings: list[str] = []
    omitted = _omitted(dictionary, config).commands
    commands = sorted(
        (command for command in dictionary.commands if command.name not in omitted),
        key=lambda command: (command.opcode, str(command.name)),
    )
    identifiers = mangle_all([command.name for command in commands], config.naming)
    # Chosen names are claimed first, so a derived one colliding with one is what grows.
    chosen = {
        command.name: config.name_overrides[command.name]
        for command in commands
        if command.name in config.name_overrides
    }
    packets = list(_packets(dictionary)) if config.packetized else []
    type_defs = _type_defs(dictionary, config, commands, packets, warnings)
    # Unique across commands, packets and types, though the structs are in modules of
    # their own: fpp-to-cpp names a generated file after a leaf name alone, so a command
    # and a packet -- or a type -- called alike would ask for one file.  The types are
    # claimed first, since their names say where they came from and a struct's need not.
    taken: set[str] = {
        *(type_name(d.qualified_name).casefold() for d in type_defs),
        *(name.casefold() for name in chosen.values()),
    }
    adjusted: list[str] = []
    command_structs = []
    for command in commands:
        name = chosen.get(command.name) or _claimed(
            identifiers[command.name], taken, adjusted
        )
        command_structs.append(
            CommandStruct(
                source=command,
                name=name,
                fields=_command_fields(dictionary, config, command, name),
            )
        )
    packet_structs = tuple(
        _packet_structs(dictionary, config, packets, taken, adjusted, warnings)
    )
    if adjusted:
        warnings.append(
            f"{len(adjusted)} struct name(s) needed a suffix to stay unique: "
            + ", ".join(sorted(adjusted)[:5])
            + (", ..." if len(adjusted) > 5 else "")
        )

    clashes = sorted(
        name
        for name in chosen.values()
        if name.casefold()
        in {type_name(d.qualified_name).casefold() for d in type_defs}
    )
    if clashes:
        raise NamingError(
            f"name override(s) {', '.join(clashes)} name a struct what a generated type "
            "is already called, and fpp-to-cpp would generate one file for both; "
            "choose another name"
        )
    return StructsPlan(
        dictionary=dictionary,
        config=config,
        commands=tuple(command_structs),
        packets=packet_structs,
        type_defs=type_defs,
        warnings=tuple(warnings),
    )


def _command_fields(
    dictionary: Dictionary, config: Config, command: Command, name: str
) -> tuple[StructField, ...]:
    """A command's arguments, after its opcode if :attr:`Config.opcode_field` asks."""
    arguments = tuple(_argument(param) for param in command.params)
    if not config.opcode_field:
        return arguments
    opcode = _id_field(
        dictionary,
        OPCODE_FIELD,
        OPCODE_TYPE,
        dictionary.platform.opcode,
        {param.name for param in command.params},
        "The remote's opcode for this command",
        f"{OPCODE_MODULE}.{name}",
    )
    return (opcode, *arguments)


def _id_field(
    dictionary: Dictionary,
    wanted: str,
    alias: str,
    builtin: TypeRef,
    used: set[str],
    annotation: str,
    constant: str,
) -> StructField:
    """A leading member holding an opcode or packet id, defaulted to ``constant``.

    Declared at the remote's width: through its alias where the dictionary recorded one,
    which is then re-declared beside the structs, or else straight as the built-in F
    Prime's default resolves to.  Named ``wanted`` unless another member already is.
    """
    width = (
        TypeRef(TypeKind.QUALIFIED, alias)
        if dictionary.type_def(alias) is not None
        else builtin
    )
    taken = {name.casefold() for name in used}
    name = wanted
    while name.casefold() in taken:
        name += "_"
    return StructField(name=name, type=width, annotation=annotation, default=constant)


def _argument(param: FormalParam) -> StructField:
    return StructField(
        name=param.name, type=param.type, annotation=param.annotation or ""
    )


def _packet_structs(
    dictionary: Dictionary,
    config: Config,
    packets: list[tuple[TelemetryPacketSet, TelemetryPacket]],
    taken: set[str],
    adjusted: list[str],
    warnings: list[str],
) -> Iterator[PacketStruct]:
    """One struct per packet, skipping any that names a channel the dictionary lacks."""
    for packet_set, packet in packets:
        channels = [dictionary.channel(member) for member in packet.members]
        missing = [str(m) for m, c in zip(packet.members, channels) if c is None]
        if missing:
            warnings.append(
                f"packet {packet_set.name}.{packet.name} (id {packet.id}) names "
                f"{len(missing)} channel(s) absent from telemetryChannels "
                f"({', '.join(missing[:3])}); its layout is unknown, so it has no struct"
            )
            continue
        # A packet may carry one channel twice, but a struct cannot have one member twice.
        names = mangle_all(
            dict.fromkeys(packet.members),
            config.naming,
            overrides={
                member: config.name_overrides[member]
                for member in packet.members
                if member in config.name_overrides
            },
        )
        seen: set[str] = set()
        fields: list[StructField] = []
        for channel in channels:
            assert channel is not None
            name = _claimed(names[channel.name], seen, [])
            fields.append(
                StructField(
                    name=name,
                    type=channel.type,
                    annotation=f"[{channel.name}] id {channel.id} (0x{channel.id:X})",
                )
            )
        name = _claimed(packet.name, taken, adjusted)
        if config.packet_id_field:
            fields.insert(
                0,
                _id_field(
                    dictionary,
                    PACKET_ID_FIELD,
                    PACKET_ID_TYPE,
                    dictionary.platform.packetize_id,
                    {field.name for field in fields},
                    "The remote's id for this packet",
                    f"{PACKET_ID_MODULE}.{name}",
                ),
            )
        yield PacketStruct(
            source=packet,
            set_name=packet_set.name,
            name=name,
            fields=tuple(fields),
        )


def _type_defs(
    dictionary: Dictionary,
    config: Config,
    commands: list[Command],
    packets: list[tuple[TelemetryPacketSet, TelemetryPacket]],
    warnings: list[str],
) -> tuple[TypeDef, ...]:
    """Every type the structs reach, through one another too, in dependency order.

    The roots are each command's arguments, each packet's channels, and the opcode and
    packet id widths when a struct carries one of those.  A packet naming a channel the
    dictionary lacks has no struct, so it reaches nothing.
    """
    roots: list[TypeRef] = [p.type for command in commands for p in command.params]
    for _, packet in packets:
        channels = [dictionary.channel(member) for member in packet.members]
        if all(channels):
            roots.extend(channel.type for channel in channels if channel is not None)
    if config.opcode_field and commands:
        roots.append(TypeRef(TypeKind.QUALIFIED, OPCODE_TYPE))
    if config.packet_id_field and packets:
        roots.append(TypeRef(TypeKind.QUALIFIED, PACKET_ID_TYPE))

    emitted: list[TypeDef] = []
    placed: set[str] = set()
    missing: set[str] = set()

    def visit(name: str) -> None:
        if name in placed:
            return
        placed.add(name)
        definition = dictionary.type_def(name)
        if definition is None:
            missing.add(name)
            return
        for ref in _references(definition):
            if ref.kind is TypeKind.QUALIFIED:
                visit(ref.name)
        emitted.append(definition)

    for ref in sorted(roots, key=lambda ref: ref.name):
        if ref.kind is TypeKind.QUALIFIED:
            visit(ref.name)
    # The opcode and packet id widths are only asked for where the dictionary recorded
    # them; see _id_field.  Anything else missing is a dictionary that cannot resolve.
    missing -= {OPCODE_TYPE, PACKET_ID_TYPE}
    if missing:
        warnings.append(
            f"the dictionary references type(s) it does not define: "
            f"{', '.join(sorted(missing))}; the generated FPP will not resolve"
        )
    return tuple(emitted)


def _references(definition: TypeDef) -> list[TypeRef]:
    """The types ``definition`` uses.  An alias counts only its resolved underlying type,
    since that is what its definition here names: the remote's width, pinned."""
    if isinstance(definition, EnumDef):
        return [definition.representation]
    if isinstance(definition, ArrayDef):
        return [definition.element]
    if isinstance(definition, StructDef):
        return [member.type for member in definition.members]
    if isinstance(definition, AliasDef):
        return [definition.underlying]
    return []
