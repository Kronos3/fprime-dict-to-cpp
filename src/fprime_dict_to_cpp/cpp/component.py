"""Emitting the proxy component's C++ implementation.

The implementation does three things.  It decodes inbound ``Fw::ComBuffer`` packets from
the remote deployment and re-emits their contents through the autocoded ``log_*`` and
``tlmWrite_*`` helpers; it serializes each of its own commands into an outbound command
packet; and it reports on its own health, because everything else it reports belongs to
the remote.

Two details govern most of the code:

*Remote widths.*  Every header field is read at the width the *remote* serialized it,
taken from its dictionary rather than from the local ``FpConfig.h``.  Those widths are
declared in the model as the component's own ``FwOpcodeType`` and friends, so a header
field is read through ``RefProxy_FwOpcodeType`` rather than through a ``using`` alias this
file would have had to invent.  Where the framework does the deserializing for us --
``Fw::Time``, and the length prefix on a string -- we cannot substitute a width, so the
generated file carries ``static_assert``\\ s that turn a configuration mismatch into a
compile error instead of silent corruption.

*Fixed-width slots.*  Channelized telemetry packs values end to end, but a packetized
telemetry packet gives every channel a slot as wide as its type's *maximum* serialized
size, whatever the value took.  So a packetized decoder seeks to each slot rather than
reading straight through, which only shows up as a bug on variable-length members.
"""

from __future__ import annotations

from fprime_cpp_codegen import Body, ClassBuilder, CppDocBuilder, Output, fprime

from ..model import (
    AliasDef,
    ArrayDef,
    ComponentKind,
    EnumDef,
    FormalParam,
    StructDef,
    TypeKind,
)
from ..plan import (
    ChannelPlan,
    CommandPlan,
    EventPlan,
    PacketPlan,
    Plan,
    PortParam,
    PortPlan,
)
from ..spelling import WIRE_TYPE_NAMES, Context, safe_identifier

__all__ = ["cpp_file_names", "render_implementation"]

#: The remote packet-header widths this file reads, by what each one is a width *of*.
#:
#: The values are the dictionary's alias names, so the generated C++ spelling comes out of
#: :class:`~fprime_dict_to_cpp.spelling.TypeNamer` and honours a type name override.
_DESCRIPTOR_TYPE, _OPCODE_TYPE, _EVENT_ID_TYPE, _CHANNEL_ID_TYPE, _PACKET_ID_TYPE = (
    WIRE_TYPE_NAMES
)

#: The sections the implementation is split across, beyond its default source file.
#:
#: A dictionary the size of Ref's produces thousands of lines of decoder, and one
#: translation unit holding all of it is slow to compile and awkward to read.
_SECTIONS = ("Commands", "Events", "Telemetry", "Packets")

_OK = "Fw::FW_SERIALIZE_OK"

#: Every local and parameter the generator introduces is spelled with a leading
#: underscore, following ``fpp-to-cpp``'s own convention (``_status``, ``_logTime``).
#:
#: This is not cosmetic.  A dictionary names its formal parameters, and those names become
#: locals in a decoder: the Ref dictionary alone has 23 events with a parameter called
#: ``status``, which would shadow the deserialization status and turn every
#: ``if (status == FW_SERIALIZE_OK)`` after it into a comparison against the event's own
#: argument.  A leading underscore is legal at block scope and is exactly what
#: :func:`~fprime_dict_to_cpp.spelling.safe_identifier` strips from a dictionary name, so
#: the two namespaces cannot meet.
_STATUS = "_status"
_PACKET = "_packet"
_TIME = "_timeTag"
_ID = "_identifier"
_VALUE = "_value"
_OFFSET = "_offset"
_DESCRIPTOR = "_descriptor"

_TOOL = "fprime-dict-to-cpp"


def cpp_file_names(plan: Plan) -> list[str]:
    """The supplemental source file base names this plan populates.

    The default ``<Component>.cpp`` is not among them.  A section with nothing in it is
    left out entirely rather than emitted empty, so a dictionary with no commands does
    not produce a stray ``<Component>Commands.cpp`` for the build to compile.
    """
    populated = {
        "Commands": bool(plan.declared_commands) or bool(plan.input_ports),
        "Events": bool(plan.decoded_events),
        "Telemetry": plan.decodes_channels,
        "Packets": plan.has_packets,
    }
    return [
        f"{plan.config.component}{section}"
        for section in _SECTIONS
        if populated[section]
    ]


def _wire(plan: Plan, alias: str) -> str:
    """How the generated C++ names one of the remote's packet-header widths."""
    return plan.spelling.namer.cpp(alias)


def _section_file(plan: Plan, section: str) -> str:
    """The source file base name a section's definitions go to."""
    return f"{plan.config.component}{section}"


def render_implementation(plan: Plan) -> CppDocBuilder:
    """Build the C++ document for ``plan``."""
    config = plan.config
    doc = CppDocBuilder(
        config.component,
        description=f"proxy for the {plan.dictionary.metadata.deployment_name} dictionary",
        namespaces=[config.namespace],
        tool_name=_TOOL,
    )

    own_header = (
        f"{config.include_prefix}/{config.component}.hpp"
        if config.include_prefix
        else f"{config.component}.hpp"
    )
    doc.include(config.base_header)
    # The base class includes a generated type's header only where a declared member or a
    # hooked port's formal has that type, and the packet-header widths are declared whether
    # anything mirrors them or not.  This class names all five, so it asks for them itself.
    doc.include(*_wire_type_headers(plan))
    supplementals = cpp_file_names(plan)
    # Before the component's own header, deliberately.  fpp-to-cpp writes
    # "RefProxy::Svc_VersionStatus" inside a generated type's header, which resolves as
    # intended only while "RefProxy" still names the module and not the implementation
    # class -- and the component's header is what declares that class.
    decoders = [
        _section_file(plan, section)
        for section in ("Telemetry", "Packets")
        if _section_file(plan, section) in supplementals
    ]
    for header in _unreferenced_type_headers(plan):
        for decoder in decoders:
            doc.include(header, output=Output.CPP, cpp_file=decoder)
    doc.include(own_header, output=Output.CPP)
    for supplemental in supplementals:
        doc.include(own_header, output=Output.CPP, cpp_file=supplemental)

    # Before the namespace: these assert about the framework, not about this component.
    _platform_asserts(doc, plan)
    scope = doc.namespace(config.namespace)

    with scope.class_(
        config.component,
        extends=f"public {config.base_class}",
        final=True,
        comment=_class_comment(plan),
    ) as cls:
        _types(cls, plan)
        _constructors(cls, plan)
        _port_handlers(cls, plan)
        _command_handlers(cls, plan)
        _decoders(cls, plan)
        _helpers(cls, plan)
        _members(cls, plan)

    return doc


#: How ``fpp-to-cpp`` names the header it generates for each kind of type definition.
_GENERATED_HEADERS: tuple[tuple[type, str], ...] = (
    (EnumDef, "EnumAc"),
    (ArrayDef, "ArrayAc"),
    (StructDef, "SerializableAc"),
    (AliasDef, "AliasAc"),
)


def _wire_type_headers(plan: Plan) -> list[str]:
    """The generated headers for the remote's packet-header aliases."""
    prefix = plan.config.include_prefix
    names = [
        f"{plan.spelling.namer.cpp_name(alias)}AliasAc.hpp" for alias in WIRE_TYPE_NAMES
    ]
    return [f"{prefix}/{name}" if prefix else name for name in names]


def _unreferenced_type_headers(plan: Plan) -> list[str]:
    """Headers for types the decoders use but the base class does not include.

    Normally there are none: every type the proxy touches is the type of a member it
    declares or of a hooked port's formal, so the autocoded base class has already included
    it -- directly, or through the port type's own generated header.  A channel that is
    *omitted and unported* is the exception: it appears only as a local the decoder reads
    into and throws away, which is enough to need the type and not enough for the model to
    mention it anywhere.  See
    :meth:`~fprime_dict_to_cpp.plan.Plan.decoded_channels` for why it is read at all.
    """
    prefix = plan.config.include_prefix
    through_a_packet_port = {
        channel.source.name
        for packet in plan.packets
        if packet.port is not None
        for channel in packet.channels
    }
    headers: dict[str, None] = {}
    for channel in plan.channels:
        if channel.decoded or channel.source.name in through_a_packet_port:
            continue
        if channel.source.type.kind is not TypeKind.QUALIFIED:
            continue
        definition = plan.dictionary.type_def(channel.source.type.name)
        suffix = next(
            (s for kind, s in _GENERATED_HEADERS if isinstance(definition, kind)), None
        )
        if suffix is None:
            continue
        name = f"{plan.spelling.namer.cpp_name(channel.source.type.name)}{suffix}.hpp"
        headers[f"{prefix}/{name}" if prefix else name] = None
    return list(headers)


def _class_comment(plan: Plan) -> str:
    reported = (
        f"Decodes Fw::ComBuffer packets from the remote deployment into this "
        f"component's own {len(plan.declared_events)} events and "
        f"{len(plan.declared_channels)} telemetry channels, and serializes each of its "
        f"{len(plan.declared_commands)} commands into an outbound command packet."
    )
    hooked = (
        f"{len(plan.input_ports)} of the remote's commands and "
        f"{len(plan.output_ports)} of its reports are hooked onto ports of this "
        f"component as well."
    )
    lines = [
        f"Proxies the {plan.dictionary.metadata.deployment_name} dictionary.",
        "",
        reported,
    ]
    if plan.ports:
        lines += ["", hooked]
    lines += ["", f"Generated by {_TOOL}; do not edit."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Platform agreement
# ---------------------------------------------------------------------------


def _platform_asserts(doc: CppDocBuilder, plan: Plan) -> None:
    """Assert the assumptions the generated code cannot express as a width.

    Packet header fields are read at the remote's recorded widths, so they need no
    assertion.  These two are different: the framework's own code does the
    deserializing, using the *local* configuration, and there is no way to ask it for
    another.  A mismatch would misread every timestamp or every string, so it is worth a
    compile error.
    """
    dictionary = plan.dictionary
    time_size = _remote_time_size(plan)
    size_store = _alias_width(plan, "FwSizeStoreType")

    lines = [
        "",
        "// Fw::Time and Fw::StringBase deserialize themselves using this build's",
        "// configuration, which the generated code cannot override.  The remote's own",
        f"// widths are recorded in {dictionary.metadata.deployment_name}'s dictionary,",
        "// so a disagreement is a build error rather than silent corruption.",
    ]
    if time_size is not None:
        lines += [
            f"static_assert(",
            f"    Fw::Time::SERIALIZED_SIZE == {time_size},",
            f'    "the remote serializes Fw::Time in {time_size} bytes; this build '
            f'disagrees, so every packet timestamp would be misread"',
            f");",
        ]
    if size_store is not None:
        lines += [
            f"static_assert(",
            f"    sizeof(FwSizeStoreType) == {size_store},",
            f'    "the remote prefixes strings with a {size_store}-byte length; this '
            f'build disagrees, so every string would be misread"',
            f");",
        ]
    doc.lines("\n".join(lines), output=Output.CPP)


def _remote_time_size(plan: Plan) -> int | None:
    """The remote's ``Fw::Time`` serialized size, from its recorded store widths.

    ``Fw::Time`` serializes as ``Fw.TimeValue``: a ``TimeBase`` at
    ``FwTimeBaseStoreType``, a context at ``FwTimeContextStoreType``, then two ``U32``.
    """
    base = _alias_width(plan, "FwTimeBaseStoreType")
    context = _alias_width(plan, "FwTimeContextStoreType")
    if base is None or context is None:
        return None
    return base + context + 4 + 4


def _alias_width(plan: Plan, name: str) -> int | None:
    """The byte width the dictionary records for a platform alias, if it records one."""
    definition = plan.dictionary.type_def(name)
    if not isinstance(definition, AliasDef) or definition.underlying.size is None:
        return None
    return definition.underlying.size // 8


# ---------------------------------------------------------------------------
# Class structure
# ---------------------------------------------------------------------------


def _types(cls: ClassBuilder, plan: Plan) -> None:
    """The remote's descriptor values, and the one size derived from its header widths.

    The widths themselves are not here: they are the component's own ``FwOpcodeType`` and
    friends, declared in the model and reached through the autocoded aliases, so this class
    has nothing to define for them.
    """
    with cls.private("Remote wire format"):
        descriptors = cls.enum(
            comment="Packet descriptors, valued as the remote's ComCfg.Apid defines "
            "them.\nThose values are project-configurable, so the local enum is not "
            "necessarily\nthe same one.",
            comment_above=True,
        )
        for name, value in _descriptor_values(plan).items():
            descriptors.constant(f"DESCRIPTOR_{name}", value, radix=None)

        cls.enum(comment_above=True).constant(
            "PACKET_HEADER_SIZE",
            f"sizeof({_wire(plan, _DESCRIPTOR_TYPE)}) + "
            f"sizeof({_wire(plan, _PACKET_ID_TYPE)}) + "
            "Fw::Time::SERIALIZED_SIZE",
            comment="Bytes before the first channel slot of a packetized telemetry "
            "packet",
        )


#: The descriptor names this component cares about, and F Prime's default values.
_DEFAULT_DESCRIPTORS = {
    "COMMAND": 0x0000,
    "TELEM": 0x0001,
    "LOG": 0x0002,
    "PACKETIZED_TLM": 0x0004,
}


def _descriptor_values(plan: Plan) -> dict[str, int]:
    """Descriptor values, from the remote's ``ComCfg.Apid`` where it recorded them."""
    values = dict(_DEFAULT_DESCRIPTORS)
    definition = plan.dictionary.type_def("ComCfg.Apid")
    if isinstance(definition, EnumDef):
        recorded = {c.name: c.value for c in definition.constants}
        for name in values:
            found = recorded.get(f"FW_PACKET_{name}")
            if found is not None:
                values[name] = found
    return values


def _constructors(cls: ClassBuilder, plan: Plan) -> None:
    with cls.public("Construction and destruction"):
        constructor = cls.constructor(
            explicit=True, comment=f"Construct {plan.config.component}"
        )
        constructor.param("const char*", "compName", comment="The component name")
        constructor.init(f"{plan.config.base_class}(compName)")
        if plan.config.diagnostics:
            constructor.init(*(f"{name}(0)" for name, _ in _counters(plan)))
        cls.destructor(override=True, comment=f"Destroy {plan.config.component}")


def _port_handlers(cls: ClassBuilder, plan: Plan) -> None:
    kind = plan.config.kind
    with cls.private("Handlers for typed input ports"):
        handler = cls.function(
            "comIn_handler",
            override=True,
            comment="Decode one packet received from the remote deployment",
        )
        handler.param("FwIndexType", "portNum", comment="The port number")
        handler.param("Fw::ComBuffer&", "data", comment="Buffer containing the packet")
        handler.param("U32", "context", comment="Call context; unused")
        _com_in_body(handler.body, plan)

        if kind is ComponentKind.QUEUED:
            run = cls.function(
                "run_handler",
                override=True,
                comment="Drain the message queue",
            )
            run.param("FwIndexType", "portNum", comment="The port number")
            run.param("U32", "context", comment="Call context; unused")
            with run.body as b:
                b.line("(void)portNum;")
                b.line("(void)context;")
                b.comment(
                    "A queued component owns a queue but no thread of its own, so this\n"
                    "scheduled call is what dispatches its messages.  The base function\n"
                    "stops at the number queued on entry, so a producer faster than the\n"
                    "rate group cannot hold this handler indefinitely."
                )
                b.line(
                    "const MsgDispatchStatus status = this->dispatchCurrentMessages();"
                )
                b.raw(
                    fprime.write_assert(
                        "(status == MsgDispatchStatus::MSG_DISPATCH_OK) || "
                        "(status == MsgDispatchStatus::MSG_DISPATCH_EMPTY)",
                        "static_cast<FwAssertArgType>(status)",
                    )
                )


def _com_in_body(body: Body, plan: Plan) -> None:
    with body as b:
        b.line("(void)portNum;")
        b.line("(void)context;")
        b.comment(
            "With FW_DIRECT_PORT_CALLS a synchronous caller hands over its own buffer,\n"
            "cursor and all, so where deserialization starts is not ours to assume."
        )
        b.line("data.resetDeser();")
        b.blank()
        b.line(f"{_wire(plan, _DESCRIPTOR_TYPE)} {_DESCRIPTOR} = 0;")
        b.line("const Fw::SerializeStatus _status = data.deserializeTo(_descriptor);")
        with b.if_(f"{_STATUS} != {_OK}"):
            b.line(f"this->recordDecodeError(0, 0, {_STATUS});")
            b.line("return;")
        b.blank()
        with b.switch(_DESCRIPTOR) as sw:
            arms = []
            if plan.decoded_events:
                arms.append(("DESCRIPTOR_LOG", "this->decodeEvent(data);"))
            if plan.decodes_channels:
                arms.append(("DESCRIPTOR_TELEM", "this->decodeChannels(data);"))
            if plan.has_packets:
                arms.append(
                    ("DESCRIPTOR_PACKETIZED_TLM", "this->decodeTelemetryPacket(data);")
                )
            for label, call in arms:
                with sw.case(label):
                    b.line(call)
            with sw.default():
                b.comment(
                    "A file, data product or parameter packet, or a descriptor this\n"
                    "dictionary does not describe.  Not an error: this proxy mirrors\n"
                    "events and telemetry only."
                )
                b.line(
                    f"this->recordPacketIgnored(static_cast<U32>({_DESCRIPTOR}), 0);"
                )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _command_handlers(cls: ClassBuilder, plan: Plan) -> None:
    if plan.declared_commands:
        with (
            cls.private("Command handlers"),
            cls.cpp_file(_section_file(plan, "Commands")),
        ):
            for command in plan.declared_commands:
                _command_handler(cls, plan, command)
    if plan.input_ports:
        with (
            cls.private("Handlers for hooked input ports"),
            cls.cpp_file(_section_file(plan, "Commands")),
        ):
            for command in plan.commands:
                if command.port is not None:
                    _command_port_handler(cls, plan, command, command.port)


def _command_handler(cls: ClassBuilder, plan: Plan, command: CommandPlan) -> None:
    source = command.source
    handler = cls.function(
        f"{command.identifier}_cmdHandler",
        override=True,
        comment=_command_comment(command),
    )
    handler.param("FwOpcodeType", "opCode", comment="The opcode")
    handler.param("U32", "cmdSeq", comment="The command sequence number")
    for param in source.params:
        handler.param(
            plan.spelling.cpp_param(param.type, Context.COMMAND),
            _param_name(param),
            comment=param.annotation,
        )

    opcode = f"0x{command.remote_opcode:X}"
    with handler.body as b:
        b.line(f"Fw::ComBuffer {_PACKET};")
        qualifier = "" if source.params else "const "
        b.line(
            f"{qualifier}Fw::SerializeStatus {_STATUS} = "
            f"this->beginCommandPacket({_PACKET}, {opcode});"
        )
        for param in source.params:
            with b.if_(f"{_STATUS} == {_OK}"):
                b.line(f"{_STATUS} = {_PACKET}.serializeFrom({_param_name(param)});")
        b.line(f"this->sendCommandPacket(opCode, cmdSeq, {_PACKET}, {_STATUS});")


def _command_port_handler(
    cls: ClassBuilder, plan: Plan, command: CommandPlan, port: PortPlan
) -> None:
    """The handler for an input port hooked onto a command.

    The same packet as the command handler builds, minus the command response: an input
    port has no caller to answer, so a failure goes to ``ProxyForwardError`` instead.
    """
    handler = cls.function(
        f"{port.name}_handler",
        override=True,
        comment=_port_comment(port),
    )
    handler.param("FwIndexType", "portNum", comment="The port number")
    for param in port.params:
        handler.param(param.cpp, safe_identifier(param.name), comment=param.annotation)

    opcode = f"0x{command.remote_opcode:X}"
    with handler.body as b:
        b.line("(void)portNum;")
        b.line(f"Fw::ComBuffer {_PACKET};")
        qualifier = "" if port.params else "const "
        b.line(
            f"{qualifier}Fw::SerializeStatus {_STATUS} = "
            f"this->beginCommandPacket({_PACKET}, {opcode});"
        )
        for param in port.params:
            with b.if_(f"{_STATUS} == {_OK}"):
                _serialize_port_argument(b, param)
        b.line(f"this->forwardCommandPacket({opcode}, {_PACKET}, {_STATUS});")


def _serialize_port_argument(b: Body, param: PortParam) -> None:
    """Serialize one port argument into the outgoing packet.

    A string goes through an exactly-sized copy first.  The port hands over a
    ``Fw::StringBase`` of whatever capacity its caller had, but the remote declared
    ``string size N`` and will read at most that many characters; clamping here is what the
    remote's own command declaration would have done to a longer one.
    """
    name = safe_identifier(param.name)
    if param.string_size is None:
        b.line(f"{_STATUS} = {_PACKET}.serializeFrom({name});")
        return
    clamped = f"_clamped{name[:1].upper()}{name[1:]}"
    b.line(f"const Fw::StringTemplate<{param.string_size}> {clamped}({name});")
    b.line(f"{_STATUS} = {_PACKET}.serializeFrom({clamped});")


def _port_comment(port: PortPlan) -> str:
    """A hooked port handler's or invocation's comment, from what the port declares."""
    return f"{port.annotation}\n\nHooked onto {port.origin}."


def _command_comment(command: CommandPlan) -> str:
    source = command.source
    lines = [f"Forward {source.name} to the remote deployment"]
    if source.annotation:
        lines += ["", *source.annotation.splitlines()]
    lines += [
        "",
        f"Remote opcode {source.opcode} (0x{source.opcode:X}), declared "
        f"{source.kind.value} there.",
    ]
    return "\n".join(lines)


def _param_name(param: FormalParam) -> str:
    """A formal parameter's C++ name.

    FPP's ``$`` escape is lexical and never part of the name, so it does not appear here.
    :func:`~fprime_dict_to_cpp.spelling.safe_identifier` does the rest: it strips a
    leading underscore, which both keeps out of the implementation's reserved space and
    guarantees a dictionary name can never collide with one of the generator's own locals.
    """
    return safe_identifier(param.name)


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------


def _decoders(cls: ClassBuilder, plan: Plan) -> None:
    if plan.decoded_events:
        with cls.private("Event decoding"), cls.cpp_file(_section_file(plan, "Events")):
            _decode_event(cls, plan)
    if plan.decodes_channels:
        with (
            cls.private("Channelized telemetry decoding"),
            cls.cpp_file(_section_file(plan, "Telemetry")),
        ):
            _decode_channels(cls, plan)
    if plan.has_packets:
        with (
            cls.private("Packetized telemetry decoding"),
            cls.cpp_file(_section_file(plan, "Packets")),
        ):
            _decode_telemetry_packet(cls, plan)
            for packet in plan.packets:
                _decode_one_packet(cls, plan, packet)


def _decode_event(cls: ClassBuilder, plan: Plan) -> None:
    function = cls.function(
        "decodeEvent",
        comment="Decode one event packet and re-emit it as this component's own event\n"
        "\n"
        "Layout: the descriptor is already consumed, leaving\n"
        f"[{_EVENT_ID_TYPE}][Fw::Time][argument...].",
    )
    function.param(
        "Fw::ComBuffer&", _PACKET, comment="The packet, positioned after the descriptor"
    )
    with function.body as b:
        b.line(f"{_wire(plan, _EVENT_ID_TYPE)} {_ID} = 0;")
        b.line(f"Fw::SerializeStatus {_STATUS} = {_PACKET}.deserializeTo({_ID});")
        with b.if_(f"{_STATUS} != {_OK}"):
            b.line(f"this->recordDecodeError(DESCRIPTOR_LOG, 0, {_STATUS});")
            b.line("return;")
        b.blank()
        b.line(f"Fw::Time {_TIME};")
        b.line(f"{_STATUS} = {_PACKET}.deserializeTo({_TIME});")
        with b.if_(f"{_STATUS} != {_OK}"):
            b.line(
                f"this->recordDecodeError(DESCRIPTOR_LOG, "
                f"static_cast<U32>({_ID}), {_STATUS});"
            )
            b.line("return;")
        b.comment(
            "Read to advance the cursor, then discarded: the autocoded log_* helpers\n"
            "stamp every event from this component's own time port, so the remote's\n"
            "timestamp has nowhere to go."
        )
        b.line(f"(void){_TIME};")
        b.blank()
        with b.switch(_ID) as sw:
            for event in plan.decoded_events:
                with sw.case(f"0x{event.remote_id:X}"):
                    b.comment(str(event.source.name))
                    _read_arguments(b, plan, event.source.params)
                    _emit_call(b, plan, event)
            with sw.default():
                b.line(
                    f"this->recordPacketIgnored(DESCRIPTOR_LOG, "
                    f"static_cast<U32>({_ID}));"
                )
                b.line("return;")
        b.blank()
        b.line(
            f"this->recordDecodeResult(DESCRIPTOR_LOG, "
            f"static_cast<U32>({_ID}), {_STATUS});"
        )


def _emit_call(b: Body, plan: Plan, event: EventPlan) -> None:
    """Report one decoded event: through the autocoded helper, through its port, or both.

    An omitted event has no ``log_*`` helper to call, which is the whole of what omitting it
    means here -- the arguments were still read, because a port may want them.
    """
    arguments = ", ".join(_param_name(p) for p in event.source.params)

    def report() -> None:
        if event.declared:
            b.line(f"this->{event.log_function}({arguments});")
        if event.port is not None:
            _invoke_port(b, event.port, arguments)

    if event.source.params:
        with b.if_(f"{_STATUS} == {_OK}"):
            report()
    else:
        report()


def _invoke_port(b: Body, port: PortPlan, arguments: str) -> None:
    """Invoke one hooked output port, if anything is connected to it.

    Checked rather than assumed: a hooked port is a hook for whoever wants it, and a
    topology that leaves one unconnected is a topology that did not want this member --
    not a reason to assert.
    """
    with b.if_(f"this->isConnected_{port.name}_OutputPort(0)"):
        b.line(f"this->{port.name}_out(0{', ' if arguments else ''}{arguments});")


def _read_arguments(b: Body, plan: Plan, params: tuple[FormalParam, ...]) -> None:
    """Declare a local per argument and deserialize into it, short-circuiting on error."""
    for index, param in enumerate(params):
        local = plan.spelling.cpp_type(param.type, Context.LOCAL)
        name = _param_name(param)
        b.line(f"{local} {name}{_initializer(plan, param)};")
        read = f"{_STATUS} = {_PACKET}.deserializeTo({name});"
        if index == 0:
            b.line(read)
        else:
            with b.if_(f"{_STATUS} == {_OK}"):
                b.line(read)


def _initializer(plan: Plan, param: FormalParam) -> str:
    """``= 0`` for a scalar local, nothing for a class type that constructs itself."""
    kind = param.type.kind
    if kind in (TypeKind.INTEGER, TypeKind.FLOAT):
        return " = 0"
    if kind is TypeKind.BOOL:
        return " = false"
    if kind is TypeKind.QUALIFIED and plan.spelling.resolves_to_builtin(
        param.type.name
    ):
        return " = 0"
    return ""


def _decode_channels(cls: ClassBuilder, plan: Plan) -> None:
    function = cls.function(
        "decodeChannels",
        comment="Decode a channelized telemetry packet and re-emit its channels\n"
        "\n"
        f"Svc::TlmChan fills one buffer with as many\n"
        f"[{_CHANNEL_ID_TYPE}][Fw::Time][value] triples as fit, so this reads until the\n"
        f"buffer runs out rather than once.",
    )
    function.param(
        "Fw::ComBuffer&", _PACKET, comment="The packet, positioned after the descriptor"
    )
    with function.body as b:
        with b.while_(f"{_PACKET}.getDeserializeSizeLeft() > 0"):
            b.line(f"{_wire(plan, _CHANNEL_ID_TYPE)} {_ID} = 0;")
            b.line(f"Fw::SerializeStatus {_STATUS} = {_PACKET}.deserializeTo({_ID});")
            with b.if_(f"{_STATUS} != {_OK}"):
                b.line(f"this->recordDecodeError(DESCRIPTOR_TELEM, 0, {_STATUS});")
                b.line("return;")
            b.blank()
            b.line(f"Fw::Time {_TIME};")
            b.line(f"{_STATUS} = {_PACKET}.deserializeTo({_TIME});")
            with b.if_(f"{_STATUS} != {_OK}"):
                b.line(
                    f"this->recordDecodeError(DESCRIPTOR_TELEM, "
                    f"static_cast<U32>({_ID}), {_STATUS});"
                )
                b.line("return;")
            b.blank()
            with b.switch(_ID) as sw:
                for channel in plan.channels:
                    with sw.case(f"0x{channel.remote_id:X}"):
                        b.comment(str(channel.source.name))
                        if not channel.decoded:
                            b.comment(
                                "Omitted, so nothing reports it -- but a channelized\n"
                                "packet carries no lengths, so the value is still read to\n"
                                "reach whatever the remote packed after it."
                            )
                        _write_channel(b, plan, channel, time=_TIME)
                with sw.default():
                    b.comment(
                        "A value's length is only knowable from its channel's type, so\n"
                        "an unrecognised id leaves the rest of the buffer unreadable."
                    )
                    b.line(
                        f"this->recordPacketIgnored(DESCRIPTOR_TELEM, "
                        f"static_cast<U32>({_ID}));"
                    )
                    b.line("return;")
            b.blank()
            b.line(
                f"this->recordDecodeResult(DESCRIPTOR_TELEM, "
                f"static_cast<U32>({_ID}), {_STATUS});"
            )
            with b.if_(f"{_STATUS} != {_OK}"):
                b.line("return;")


def _write_channel(
    b: Body, plan: Plan, channel: ChannelPlan, *, time: str, local_name: str = _VALUE
) -> None:
    """Deserialize one channel value and report it, if anything is there to report it to.

    A channel that is neither declared nor hooked onto a port is read and dropped; see
    :meth:`~fprime_dict_to_cpp.plan.Plan.decoded_channels` for why it is read at all.
    """
    local = plan.spelling.cpp_type(channel.source.type, Context.LOCAL)
    initializer = _initializer(
        plan, FormalParam(name=local_name, type=channel.source.type)
    )
    b.line(f"{local} {local_name}{initializer};")
    b.line(f"{_STATUS} = {_PACKET}.deserializeTo({local_name});")
    if not channel.decoded:
        b.line(f"(void){local_name};")
        return
    with b.if_(f"{_STATUS} == {_OK}"):
        if channel.declared:
            b.line(f"this->{channel.write_function}({local_name}, {time});")
        if channel.port is not None:
            _invoke_port(b, channel.port, f"{local_name}, {time}")


def _decode_telemetry_packet(cls: ClassBuilder, plan: Plan) -> None:
    function = cls.function(
        "decodeTelemetryPacket",
        comment="Dispatch a packetized telemetry packet to its layout\n"
        "\n"
        "Layout: the descriptor is already consumed, leaving\n"
        f"[{_PACKET_ID_TYPE}][Fw::Time][channel slot...].",
    )
    function.param(
        "Fw::ComBuffer&", _PACKET, comment="The packet, positioned after the descriptor"
    )
    with function.body as b:
        b.line(f"{_wire(plan, _PACKET_ID_TYPE)} {_ID} = 0;")
        b.line(f"Fw::SerializeStatus {_STATUS} = {_PACKET}.deserializeTo({_ID});")
        with b.if_(f"{_STATUS} != {_OK}"):
            b.line(f"this->recordDecodeError(DESCRIPTOR_PACKETIZED_TLM, 0, {_STATUS});")
            b.line("return;")
        b.blank()
        b.line(f"Fw::Time {_TIME};")
        b.line(f"{_STATUS} = {_PACKET}.deserializeTo({_TIME});")
        with b.if_(f"{_STATUS} != {_OK}"):
            b.line(
                f"this->recordDecodeError(DESCRIPTOR_PACKETIZED_TLM, "
                f"static_cast<U32>({_ID}), {_STATUS});"
            )
            b.line("return;")
        b.comment(
            "Svc::TlmPacketizer keeps one timestamp for the whole packet, so every\n"
            "channel in it is reported with that time."
        )
        b.blank()
        with b.switch(_ID) as sw:
            for packet in plan.packets:
                with sw.case(f"0x{packet.id:X}"):
                    b.comment(f"{packet.name}: {len(packet.channels)} channels")
                    b.line(f"this->{_packet_function(packet)}({_PACKET}, {_TIME});")
            with sw.default():
                b.line(
                    f"this->recordPacketIgnored(DESCRIPTOR_PACKETIZED_TLM, "
                    f"static_cast<U32>({_ID}));"
                )


def _packet_function(packet: PacketPlan) -> str:
    """The per-packet decoder's name."""
    return f"decodePacket_{packet.name}"


def _decode_one_packet(cls: ClassBuilder, plan: Plan, packet: PacketPlan) -> None:
    """One packet layout's decoder.

    Every channel gets a slot as wide as its type's maximum serialized size, so the
    cursor is positioned explicitly for each rather than left to fall where the previous
    value ended.  For a fixed-width type the two agree; for a string or a struct
    containing one they do not.
    """
    function = cls.function(
        _packet_function(packet),
        comment=f"Decode telemetry packet {packet.name} (remote id {packet.id})\n"
        f"\n"
        f"Every channel occupies a fixed-width slot of its type's maximum serialized\n"
        f"size, so each read seeks to its slot instead of continuing from the last.",
    )
    function.param(
        "Fw::ComBuffer&",
        _PACKET,
        comment="The packet, positioned after the header",
    )
    function.param("const Fw::Time&", _TIME, comment="The packet's timestamp")

    with function.body as b:
        b.line(f"FwSizeType {_OFFSET} = PACKET_HEADER_SIZE;")
        b.line(f"Fw::SerializeStatus {_STATUS} = {_OK};")
        if packet.port is not None:
            # A packet port carries every channel at once, so the values have to outlive
            # the block each was read in; a per-channel local would be gone by then.
            b.comment(
                f"Declared here rather than per channel because {packet.port.name}\n"
                f"carries the whole packet in one call."
            )
            for index, channel in enumerate(packet.channels):
                local = plan.spelling.cpp_type(channel.source.type, Context.LOCAL)
                initializer = _initializer(
                    plan, FormalParam(name=_slot(index), type=channel.source.type)
                )
                b.line(f"{local} {_slot(index)}{initializer};")
        for index, channel in enumerate(packet.channels):
            b.blank()
            b.comment(str(channel.source.name))
            with b.if_(f"{_STATUS} == {_OK}"):
                b.line(f"{_STATUS} = {_PACKET}.moveDeserToOffset({_OFFSET});")
                with b.if_(f"{_STATUS} == {_OK}"):
                    if packet.port is None:
                        _write_channel(b, plan, channel, time=_TIME)
                    else:
                        _read_slot(b, plan, channel, _slot(index))
            size = plan.spelling.serialized_size(channel.source.type)
            b.line(f"{_OFFSET} += {size};")
        if packet.port is not None:
            b.blank()
            b.comment(
                "Every slot read, so the packet can go out whole.  A partial read leaves\n"
                "the status set, and a caller given half a packet is worse than one given\n"
                "none."
            )
            with b.if_(f"{_STATUS} == {_OK}"):
                arguments = ", ".join(
                    [*(_slot(index) for index in range(len(packet.channels))), _TIME]
                )
                _invoke_port(b, packet.port, arguments)
        b.blank()
        b.line(
            f"this->recordDecodeResult(DESCRIPTOR_PACKETIZED_TLM, "
            f"0x{packet.id:X}, {_STATUS});"
        )


def _slot(index: int) -> str:
    """The local one channel slot of a ported packet is read into."""
    return f"{_VALUE}{index}"


def _read_slot(b: Body, plan: Plan, channel: ChannelPlan, local_name: str) -> None:
    """Read one slot of a ported packet into its already-declared local, and report it."""
    b.line(f"{_STATUS} = {_PACKET}.deserializeTo({local_name});")
    if not channel.declared and channel.port is None:
        return
    with b.if_(f"{_STATUS} == {_OK}"):
        if channel.declared:
            b.line(f"this->{channel.write_function}({local_name}, {_TIME});")
        if channel.port is not None:
            _invoke_port(b, channel.port, f"{local_name}, {_TIME}")


# ---------------------------------------------------------------------------
# Helpers and state
# ---------------------------------------------------------------------------


def _helpers(cls: ClassBuilder, plan: Plan) -> None:
    with cls.private("Helpers"):
        _begin_command_packet(cls, plan)
        if plan.declared_commands:
            _send_command_packet(cls, plan)
        if plan.input_ports:
            _forward_command_packet(cls, plan)
        _record_decode_result(cls, plan)
        _record_decode_error(cls, plan)
        _record_packet_ignored(cls, plan)
        if plan.input_ports:
            _record_forward_error(cls, plan)


def _begin_command_packet(cls: ClassBuilder, plan: Plan) -> None:
    function = cls.function(
        "beginCommandPacket",
        ret="Fw::SerializeStatus",
        const=True,
        comment="Write a command packet's descriptor and opcode\n"
        "\n"
        "The opcode is the remote's absolute one, which is what its command dispatcher\n"
        "matches on.",
    )
    function.param("Fw::ComBuffer&", "packet", comment="The packet to write into")
    function.param(_wire(plan, _OPCODE_TYPE), "opcode", comment="The remote's opcode")
    with function.body as b:
        b.line(
            "Fw::SerializeStatus status = packet.serializeFrom("
            f"static_cast<{_wire(plan, _DESCRIPTOR_TYPE)}>(DESCRIPTOR_COMMAND));"
        )
        with b.if_(f"status == {_OK}"):
            b.line("status = packet.serializeFrom(opcode);")
        b.line("return status;")


def _send_command_packet(cls: ClassBuilder, plan: Plan) -> None:
    function = cls.function(
        "sendCommandPacket",
        comment="Send a finished command packet and respond to the caller\n"
        "\n"
        "An OK response means the packet was sent, not that the remote ran the command:\n"
        "the remote's own dispatcher reports that as one of its events, which this\n"
        "component relays separately.",
    )
    function.param("FwOpcodeType", "opCode", comment="The local opcode")
    function.param("U32", "cmdSeq", comment="The command sequence number")
    function.param("Fw::ComBuffer&", "packet", comment="The packet to send")
    function.param(
        "Fw::SerializeStatus", "status", comment="How serialization turned out"
    )
    with function.body as b:
        with b.if_(f"status != {_OK}"):
            b.comment(
                "The arguments did not fit, which means the remote could not have\n"
                "accepted them either."
            )
            b.line(
                "this->cmdResponse_out(opCode, cmdSeq, Fw::CmdResponse::FORMAT_ERROR);"
            )
            b.line("return;")
        with b.if_("!this->isConnected_comOut_OutputPort(0)"):
            b.line(
                "this->cmdResponse_out(opCode, cmdSeq, "
                "Fw::CmdResponse::EXECUTION_ERROR);"
            )
            b.line("return;")
        b.line("this->comOut_out(0, packet, 0);")
        b.line("this->cmdResponse_out(opCode, cmdSeq, Fw::CmdResponse::OK);")


def _forward_command_packet(cls: ClassBuilder, plan: Plan) -> None:
    function = cls.function(
        "forwardCommandPacket",
        comment="Send a command packet built by a hooked input port\n"
        "\n"
        "The same send the command handlers do, without the command response: an input\n"
        "port carries no status back to whoever invoked it, so a failure is reported as\n"
        "ProxyForwardError instead of to a caller.",
    )
    function.param("U32", "opcode", comment="The remote opcode being forwarded")
    function.param("Fw::ComBuffer&", "packet", comment="The packet to send")
    function.param(
        "Fw::SerializeStatus", "status", comment="How serialization turned out"
    )
    with function.body as b:
        with b.if_(f"status != {_OK}"):
            b.comment(
                "The arguments did not fit, which means the remote could not have\n"
                "accepted them either."
            )
            b.line("this->recordForwardError(opcode, status);")
            b.line("return;")
        with b.if_("!this->isConnected_comOut_OutputPort(0)"):
            b.comment(
                "Nothing downstream to take the packet.  Reported with an OK status,\n"
                "because serialization is not what went wrong."
            )
            b.line(f"this->recordForwardError(opcode, {_OK});")
            b.line("return;")
        b.line("this->comOut_out(0, packet, 0);")


def _record_forward_error(cls: ClassBuilder, plan: Plan) -> None:
    function = cls.function(
        "recordForwardError",
        comment="Report a command packet a hooked input port could not send",
    )
    function.param("U32", "opcode", comment="The remote opcode being forwarded")
    function.param(
        "Fw::SerializeStatus",
        "status",
        comment="The failing status, or OK if comOut is unconnected",
    )
    with function.body as b:
        if plan.config.diagnostics:
            b.line("this->m_forwardErrors++;")
            b.line("this->tlmWrite_ProxyForwardErrors(this->m_forwardErrors);")
            b.line(
                "this->log_WARNING_HI_ProxyForwardError("
                "opcode, static_cast<I32>(status));"
            )
        else:
            b.line("(void)opcode;")
            b.line("(void)status;")


def _record_decode_result(cls: ClassBuilder, plan: Plan) -> None:
    function = cls.function(
        "recordDecodeResult",
        comment="Count one decode attempt, successful or not",
    )
    function.param("U32", "descriptor", comment="The packet descriptor")
    function.param("U32", "identifier", comment="The event, channel or packet id")
    function.param("Fw::SerializeStatus", "status", comment="How the decode turned out")
    with function.body as b:
        with b.if_(f"status != {_OK}"):
            b.line("this->recordDecodeError(descriptor, identifier, status);")
            b.line("return;")
        if plan.config.diagnostics:
            b.line("this->m_packetsDecoded++;")
            b.line("this->tlmWrite_ProxyPacketsDecoded(this->m_packetsDecoded);")
        else:
            b.line("(void)descriptor;")
            b.line("(void)identifier;")


def _record_decode_error(cls: ClassBuilder, plan: Plan) -> None:
    function = cls.function(
        "recordDecodeError",
        comment="Report a packet that did not deserialize",
    )
    function.param("U32", "descriptor", comment="The packet descriptor")
    function.param(
        "U32", "identifier", comment="The id, or zero if the decode failed before it"
    )
    function.param("Fw::SerializeStatus", "status", comment="The failing status")
    with function.body as b:
        if plan.config.diagnostics:
            b.line("this->m_decodeErrors++;")
            b.line("this->tlmWrite_ProxyDecodeErrors(this->m_decodeErrors);")
            b.line(
                "this->log_WARNING_HI_ProxyDecodeError("
                "descriptor, identifier, static_cast<I32>(status));"
            )
        else:
            b.line("(void)descriptor;")
            b.line("(void)identifier;")
            b.line("(void)status;")


def _record_packet_ignored(cls: ClassBuilder, plan: Plan) -> None:
    function = cls.function(
        "recordPacketIgnored",
        comment="Report a well-formed packet this proxy does not mirror",
    )
    function.param("U32", "descriptor", comment="The packet descriptor")
    function.param("U32", "identifier", comment="The id, or zero if none was read")
    with function.body as b:
        if plan.config.diagnostics:
            b.line("this->m_packetsIgnored++;")
            b.line("this->tlmWrite_ProxyPacketsIgnored(this->m_packetsIgnored);")
            b.line("this->log_DIAGNOSTIC_ProxyPacketIgnored(descriptor, identifier);")
        else:
            b.line("(void)descriptor;")
            b.line("(void)identifier;")


def _members(cls: ClassBuilder, plan: Plan) -> None:
    if not plan.config.diagnostics:
        return
    with cls.private("Member variables"):
        for name, comment in _counters(plan):
            cls.var("U32", name, comment=comment)


def _counters(plan: Plan) -> tuple[tuple[str, str], ...]:
    """The diagnostic counters this component keeps, and what each one counts."""
    counters = (
        ("m_packetsDecoded", "Packets decoded into an event or a channel"),
        ("m_decodeErrors", "Packets that did not deserialize"),
        ("m_packetsIgnored", "Packets this proxy does not mirror"),
    )
    if not plan.input_ports:
        return counters
    return (
        *counters,
        ("m_forwardErrors", "Command packets a hooked input port could not send"),
    )
