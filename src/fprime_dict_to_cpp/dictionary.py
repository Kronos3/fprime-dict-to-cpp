"""Reading an F Prime JSON dictionary into the :mod:`.model` types.

The dictionary spec version this targets is ``1.0.0``.  Parsing is strict about the
fields a generator depends on and lenient about the rest: an unknown section is
ignored, but a malformed command is an error, because silently dropping a command
would silently produce a proxy that cannot send it.

``fprime-gds`` ships loaders for the same file.  They are aimed at a *ground* system,
so they resolve every type into a Python serialization object and discard what a C++
generator needs -- an alias collapses into its underlying type, annotations go away,
and the qualified FPP type name survives only as a class ``__name__``.  This module
therefore reads the JSON directly, and :func:`cross_check` uses the GDS loaders as an
independent second opinion on the entry counts and ids.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from .model import (
    AliasDef,
    ArrayDef,
    Channel,
    Command,
    CommandKind,
    ConstantDef,
    Dictionary,
    DictionaryError,
    EnumConstant,
    EnumDef,
    Event,
    FormalParam,
    Limit,
    Limits,
    Metadata,
    PlatformTypes,
    QualifiedName,
    QueueFullBehavior,
    Severity,
    StructDef,
    StructMember,
    TelemetryPacket,
    TelemetryPacketSet,
    TelemetryUpdate,
    TypeDef,
    TypeKind,
    TypeRef,
)

__all__ = ["cross_check", "load", "parse"]

SPEC_VERSION = "1.0.0"


def load(path: str | Path) -> Dictionary:
    """Read and parse the JSON dictionary at ``path``."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DictionaryError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise DictionaryError(f"{path}: expected a JSON object at the top level")
    return parse(raw, source=str(path))


def parse(raw: dict[str, Any], *, source: str = "<dictionary>") -> Dictionary:
    """Parse an already-decoded dictionary."""
    metadata = _metadata(_section_dict(raw, "metadata", source), source)
    type_defs = tuple(
        _type_def(entry, source) for entry in _section_list(raw, "typeDefinitions")
    )
    return Dictionary(
        metadata=metadata,
        platform=_platform_types(type_defs),
        type_defs=type_defs,
        constants=tuple(
            _constant(entry, source) for entry in _section_list(raw, "constants")
        ),
        commands=tuple(
            _command(entry, source) for entry in _section_list(raw, "commands")
        ),
        events=tuple(_event(entry, source) for entry in _section_list(raw, "events")),
        channels=tuple(
            _channel(entry, source) for entry in _section_list(raw, "telemetryChannels")
        ),
        packet_sets=tuple(
            _packet_set(entry, source)
            for entry in _section_list(raw, "telemetryPacketSets")
        ),
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _section_dict(raw: dict[str, Any], key: str, source: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise DictionaryError(f"{source}: missing or malformed {key!r} section")
    return value


def _section_list(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """A dictionary section, or an empty list when it is absent.

    Absence is legal: a deployment with no data products has no ``records`` section,
    and one with no packet configuration has no ``telemetryPacketSets``.
    """
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise DictionaryError(f"{key!r} section is not a list")
    return [entry for entry in value if isinstance(entry, dict)]


def _metadata(raw: dict[str, Any], source: str) -> Metadata:
    name = raw.get("deploymentName")
    if not isinstance(name, str) or not name:
        raise DictionaryError(f"{source}: metadata has no deploymentName")
    spec = raw.get("dictionarySpecVersion", "unknown")
    return Metadata(
        deployment_name=name,
        project_version=_str(raw.get("projectVersion"), "unknown"),
        framework_version=_str(raw.get("frameworkVersion"), "unknown"),
        spec_version=_str(spec, "unknown"),
    )


def _str(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------


def _type_def(raw: dict[str, Any], source: str) -> TypeDef:
    name = raw.get("qualifiedName")
    if not isinstance(name, str) or not name:
        raise DictionaryError(f"{source}: type definition has no qualifiedName")
    kind = raw.get("kind")
    annotation = raw.get("annotation")
    annotation = annotation if isinstance(annotation, str) else None

    if kind == "enum":
        return _enum_def(name, raw, annotation, source)
    if kind == "array":
        return _array_def(name, raw, annotation, source)
    if kind == "struct":
        return _struct_def(name, raw, annotation, source)
    if kind == "alias":
        return _alias_def(name, raw, annotation, source)
    raise DictionaryError(f"{source}: type {name!r} has unknown kind {kind!r}")


def _enum_def(
    name: str, raw: dict[str, Any], annotation: str | None, source: str
) -> EnumDef:
    representation = raw.get("representationType")
    if not isinstance(representation, dict):
        raise DictionaryError(f"{source}: enum {name!r} has no representationType")
    raw_constants = raw.get("enumeratedConstants")
    if not isinstance(raw_constants, list) or not raw_constants:
        raise DictionaryError(f"{source}: enum {name!r} has no enumeratedConstants")
    constants = []
    for entry in raw_constants:
        if not isinstance(entry, dict):
            raise DictionaryError(f"{source}: enum {name!r} has a malformed constant")
        constant_name = entry.get("name")
        value = entry.get("value")
        if not isinstance(constant_name, str) or not isinstance(value, int):
            raise DictionaryError(
                f"{source}: enum {name!r} constant {entry!r} needs a name and an int value"
            )
        note = entry.get("annotation")
        constants.append(
            EnumConstant(constant_name, value, note if isinstance(note, str) else None)
        )
    default = raw.get("default")
    return EnumDef(
        qualified_name=name,
        representation=TypeRef.parse(representation),
        constants=tuple(constants),
        default=default if isinstance(default, str) else None,
        annotation=annotation,
    )


def _array_def(
    name: str, raw: dict[str, Any], annotation: str | None, source: str
) -> ArrayDef:
    element = raw.get("elementType")
    size = raw.get("size")
    if not isinstance(element, dict):
        raise DictionaryError(f"{source}: array {name!r} has no elementType")
    if not isinstance(size, int) or size <= 0:
        raise DictionaryError(f"{source}: array {name!r} has no positive size")
    # An array's format is a sibling of "size", describing how one element prints.
    # Older writers put it on the element type instead, so accept both.
    array_format = raw.get("format")
    if not isinstance(array_format, str):
        array_format = element.get("format")
    return ArrayDef(
        qualified_name=name,
        size=size,
        element=TypeRef.parse(element),
        default=raw.get("default"),
        format=array_format if isinstance(array_format, str) else None,
        annotation=annotation,
    )


def _struct_def(
    name: str, raw: dict[str, Any], annotation: str | None, source: str
) -> StructDef:
    raw_members = raw.get("members")
    if not isinstance(raw_members, dict):
        raise DictionaryError(f"{source}: struct {name!r} has no members")
    by_index: dict[int, StructMember] = {}
    for member_name, entry in raw_members.items():
        if not isinstance(entry, dict):
            raise DictionaryError(
                f"{source}: struct {name!r} member {member_name!r} is malformed"
            )
        member_type = entry.get("type")
        index = entry.get("index")
        if not isinstance(member_type, dict):
            raise DictionaryError(
                f"{source}: struct {name!r} member {member_name!r} has no type"
            )
        if not isinstance(index, int):
            raise DictionaryError(
                f"{source}: struct {name!r} member {member_name!r} has no index"
            )
        if index in by_index:
            raise DictionaryError(
                f"{source}: struct {name!r} reuses member index {index} "
                f"({by_index[index].name!r} and {member_name!r})"
            )
        array_size = entry.get("size")
        member_format = entry.get("format")
        note = entry.get("annotation")
        by_index[index] = StructMember(
            name=member_name,
            type=TypeRef.parse(member_type),
            index=index,
            array_size=array_size if isinstance(array_size, int) else None,
            format=member_format if isinstance(member_format, str) else None,
            annotation=note if isinstance(note, str) else None,
        )
    if not by_index:
        raise DictionaryError(f"{source}: struct {name!r} has no members")
    return StructDef(
        qualified_name=name,
        members=tuple(by_index[i] for i in sorted(by_index)),
        default=raw.get("default"),
        annotation=annotation,
    )


def _alias_def(
    name: str, raw: dict[str, Any], annotation: str | None, source: str
) -> AliasDef:
    target = raw.get("type")
    underlying = raw.get("underlyingType")
    if not isinstance(target, dict):
        raise DictionaryError(f"{source}: alias {name!r} has no type")
    if not isinstance(underlying, dict):
        raise DictionaryError(f"{source}: alias {name!r} has no underlyingType")
    return AliasDef(
        qualified_name=name,
        target=TypeRef.parse(target),
        underlying=TypeRef.parse(underlying),
        annotation=annotation,
    )


def _constant(raw: dict[str, Any], source: str) -> ConstantDef:
    name = raw.get("qualifiedName")
    type_raw = raw.get("type")
    if not isinstance(name, str) or not name:
        raise DictionaryError(f"{source}: constant has no qualifiedName")
    if not isinstance(type_raw, dict):
        raise DictionaryError(f"{source}: constant {name!r} has no type")
    note = raw.get("annotation")
    return ConstantDef(
        qualified_name=name,
        type=TypeRef.parse(type_raw),
        value=raw.get("value"),
        annotation=note if isinstance(note, str) else None,
    )


def _platform_types(type_defs: Sequence[TypeDef]) -> PlatformTypes:
    """Pull the remote's packet-header widths out of its type definitions.

    These are what the remote actually put on the wire.  Reading them from the
    dictionary rather than from the local ``FpConfig.h`` is what lets a proxy talk to
    a deployment whose platform configuration differs from its own.
    """
    aliases = {d.qualified_name: d for d in type_defs if isinstance(d, AliasDef)}

    def width(field: str) -> TypeRef:
        alias_name, fallback = PlatformTypes.DEFAULTS[field]
        alias = aliases.get(alias_name)
        if alias is not None:
            return alias.underlying
        bits = int(fallback[1:])
        return TypeRef(kind=TypeKind.INTEGER, name=fallback, size=bits, signed=False)

    return PlatformTypes(
        packet_descriptor=width("packet_descriptor"),
        opcode=width("opcode"),
        event_id=width("event_id"),
        channel_id=width("channel_id"),
        packetize_id=width("packetize_id"),
    )


# ---------------------------------------------------------------------------
# Commands, events, channels, packets
# ---------------------------------------------------------------------------


def _formal_params(
    raw: dict[str, Any], owner: str, source: str
) -> tuple[FormalParam, ...]:
    entries = raw.get("formalParams", [])
    if not isinstance(entries, list):
        raise DictionaryError(f"{source}: {owner} has a malformed formalParams")
    params = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise DictionaryError(f"{source}: {owner} has a malformed formal parameter")
        name = entry.get("name")
        type_raw = entry.get("type")
        if not isinstance(name, str) or not name:
            raise DictionaryError(f"{source}: {owner} has an unnamed formal parameter")
        if not isinstance(type_raw, dict):
            raise DictionaryError(f"{source}: {owner} parameter {name!r} has no type")
        note = entry.get("annotation")
        params.append(
            FormalParam(
                name=name,
                type=TypeRef.parse(type_raw),
                annotation=note if isinstance(note, str) else None,
                ref=bool(entry.get("ref", False)),
            )
        )
    return tuple(params)


def _command(raw: dict[str, Any], source: str) -> Command:
    name = _entry_name(raw, "command", source)
    opcode = raw.get("opcode")
    if not isinstance(opcode, int):
        raise DictionaryError(f"{source}: command {name} has no integer opcode")
    kind_text = raw.get("commandKind")
    try:
        kind = CommandKind(kind_text)
    except ValueError as exc:
        raise DictionaryError(
            f"{source}: command {name} has unknown commandKind {kind_text!r}"
        ) from exc
    behavior_text = raw.get("queueFullBehavior")
    behavior: QueueFullBehavior | None = None
    if isinstance(behavior_text, str):
        try:
            behavior = QueueFullBehavior(behavior_text)
        except ValueError as exc:
            raise DictionaryError(
                f"{source}: command {name} has unknown queueFullBehavior "
                f"{behavior_text!r}"
            ) from exc
    note = raw.get("annotation")
    return Command(
        name=name,
        opcode=opcode,
        kind=kind,
        params=_formal_params(raw, f"command {name}", source),
        queue_full_behavior=behavior,
        annotation=note if isinstance(note, str) else None,
    )


def _event(raw: dict[str, Any], source: str) -> Event:
    name = _entry_name(raw, "event", source)
    identifier = raw.get("id")
    if not isinstance(identifier, int):
        raise DictionaryError(f"{source}: event {name} has no integer id")
    severity_text = raw.get("severity")
    try:
        severity = Severity(severity_text)
    except ValueError as exc:
        raise DictionaryError(
            f"{source}: event {name} has unknown severity {severity_text!r}"
        ) from exc
    fmt = raw.get("format")
    if not isinstance(fmt, str):
        raise DictionaryError(f"{source}: event {name} has no format string")
    throttle = raw.get("throttle")
    note = raw.get("annotation")
    return Event(
        name=name,
        id=identifier,
        severity=severity,
        format=fmt,
        params=_formal_params(raw, f"event {name}", source),
        throttle=throttle if isinstance(throttle, int) else None,
        annotation=note if isinstance(note, str) else None,
    )


def _channel(raw: dict[str, Any], source: str) -> Channel:
    name = _entry_name(raw, "telemetry channel", source)
    identifier = raw.get("id")
    if not isinstance(identifier, int):
        raise DictionaryError(f"{source}: channel {name} has no integer id")
    type_raw = raw.get("type")
    if not isinstance(type_raw, dict):
        raise DictionaryError(f"{source}: channel {name} has no type")
    update_text = raw.get("telemetryUpdate", "always")
    try:
        update = TelemetryUpdate(update_text)
    except ValueError as exc:
        raise DictionaryError(
            f"{source}: channel {name} has unknown telemetryUpdate {update_text!r}"
        ) from exc
    fmt = raw.get("format")
    note = raw.get("annotation")
    return Channel(
        name=name,
        id=identifier,
        type=TypeRef.parse(type_raw),
        update=update,
        format=fmt if isinstance(fmt, str) else None,
        limits=_limits(raw.get("limits")),
        annotation=note if isinstance(note, str) else None,
    )


def _limits(raw: object) -> Limits:
    if not isinstance(raw, dict):
        return Limits()

    def side(key: str) -> Limit:
        value = raw.get(key)
        if not isinstance(value, dict):
            return Limit()
        return Limit(
            red=value.get("red"), orange=value.get("orange"), yellow=value.get("yellow")
        )

    return Limits(low=side("low"), high=side("high"))


def _packet_set(raw: dict[str, Any], source: str) -> TelemetryPacketSet:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise DictionaryError(f"{source}: telemetry packet set has no name")
    entries = raw.get("members", [])
    if not isinstance(entries, list):
        raise DictionaryError(f"{source}: packet set {name!r} has malformed members")
    packets = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise DictionaryError(
                f"{source}: packet set {name!r} has a malformed packet"
            )
        packet_name = entry.get("name")
        identifier = entry.get("id")
        group = entry.get("group", 0)
        members = entry.get("members", [])
        if not isinstance(packet_name, str) or not packet_name:
            raise DictionaryError(
                f"{source}: packet set {name!r} has an unnamed packet"
            )
        if not isinstance(identifier, int):
            raise DictionaryError(f"{source}: packet {packet_name!r} has no integer id")
        if not isinstance(members, list):
            raise DictionaryError(
                f"{source}: packet {packet_name!r} has malformed members"
            )
        packets.append(
            TelemetryPacket(
                name=packet_name,
                id=identifier,
                group=group if isinstance(group, int) else 0,
                members=tuple(
                    QualifiedName.parse(m) for m in members if isinstance(m, str)
                ),
            )
        )
    return TelemetryPacketSet(name=name, packets=tuple(packets))


def _entry_name(raw: dict[str, Any], what: str, source: str) -> QualifiedName:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise DictionaryError(f"{source}: a {what} has no name")
    parsed = QualifiedName.parse(name)
    if len(parsed.parts) < 2:
        raise DictionaryError(
            f"{source}: {what} name {name!r} is not of the form "
            f"'<component.instance>.<MEMBER>'"
        )
    return parsed


# ---------------------------------------------------------------------------
# Cross-checking against fprime-gds
# ---------------------------------------------------------------------------


def cross_check(path: str | Path, parsed: Dictionary) -> list[str]:
    """Compare a parse against ``fprime-gds``'s own loaders.

    Returns a list of human-readable disagreements, empty when the two agree.  Used
    by the test suite and by ``--cross-check`` to catch a drift in either reading of
    the spec.  Returns a single explanatory entry if ``fprime-gds`` is not installed.
    """
    try:
        from fprime_gds.common.loaders.ch_json_loader import ChJsonLoader
        from fprime_gds.common.loaders.cmd_json_loader import CmdJsonLoader
        from fprime_gds.common.loaders.event_json_loader import EventJsonLoader
    except ImportError:
        return ["fprime-gds is not installed, so no cross-check was performed"]

    problems: list[str] = []
    text = str(path)

    def compare(
        what: str, ours: Iterable[tuple[int, str]], theirs: dict[int, Any]
    ) -> None:
        ours_map = dict(ours)
        if len(ours_map) != len(theirs):
            problems.append(
                f"{what}: we found {len(ours_map)}, fprime-gds found {len(theirs)}"
            )
        for identifier, name in sorted(ours_map.items()):
            other = theirs.get(identifier)
            if other is None:
                problems.append(f"{what}: fprime-gds has no entry for id {identifier}")
                continue
            other_name = other.get_full_name()
            if other_name != name:
                problems.append(
                    f"{what} id {identifier}: we call it {name!r}, "
                    f"fprime-gds calls it {other_name!r}"
                )

    # Each loader re-reads the file; that is cheap next to generation and keeps this
    # honest as an independent check.
    compare(
        "commands",
        ((c.opcode, str(c.name)) for c in parsed.commands),
        CmdJsonLoader(text).construct_dicts(None)[0],
    )
    compare(
        "events",
        ((e.id, str(e.name)) for e in parsed.events),
        EventJsonLoader(text).construct_dicts(None)[0],
    )
    compare(
        "channels",
        ((c.id, str(c.name)) for c in parsed.channels),
        ChJsonLoader(text).construct_dicts(None)[0],
    )
    return problems
