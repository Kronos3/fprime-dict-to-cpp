"""The generated C++: the shapes it has to have, and the hazards it has to avoid."""

from __future__ import annotations

import pytest

from fprime_dict_to_cpp.cpp import cpp_file_names, render_implementation
from fprime_dict_to_cpp.model import ComponentKind, Dictionary
from fprime_dict_to_cpp.naming import Naming
from fprime_dict_to_cpp.plan import Config, Plan, resolve


@pytest.fixture(scope="module")
def files(plan: Plan) -> dict[str, str]:
    """Every generated C++ file, rendered once."""
    return render_implementation(plan).files()


def plan_for(ref: Dictionary, **overrides: object) -> Plan:
    base = dict(
        component="RefProxy",
        namespace="RefProxy",
        kind=ComponentKind.ACTIVE,
        naming=Naming.preset("instance"),
        include_prefix="RefProxy",
    )
    base.update(overrides)
    return resolve(ref, Config(**base))  # type: ignore[arg-type]


class TestFileLayout:
    def test_sections_go_to_separate_translation_units(self, plan: Plan) -> None:
        assert cpp_file_names(plan) == [
            "RefProxyCommands",
            "RefProxyEvents",
            "RefProxyTelemetry",
            "RefProxyPackets",
        ]

    def test_files_are_named_after_the_component(self, files: dict[str, str]) -> None:
        assert set(files) == {
            "RefProxy.hpp",
            "RefProxy.cpp",
            "RefProxyCommands.cpp",
            "RefProxyEvents.cpp",
            "RefProxyTelemetry.cpp",
            "RefProxyPackets.cpp",
        }

    def test_an_empty_section_produces_no_file(self, ref: Dictionary) -> None:
        from dataclasses import replace

        plan = plan_for(replace(ref, commands=()))
        assert "RefProxyCommands" not in cpp_file_names(plan)

    def test_every_source_includes_the_implementation_header(
        self, files: dict[str, str]
    ) -> None:
        for name, text in files.items():
            if name.endswith(".cpp"):
                assert '#include "RefProxy/RefProxy.hpp"' in text, name

    def test_the_header_includes_the_autocoded_base(
        self, files: dict[str, str]
    ) -> None:
        assert '#include "RefProxy/RefProxyComponentAc.hpp"' in files["RefProxy.hpp"]


class TestRemoteWireFormat:
    def test_header_widths_are_the_components_own_types(
        self, files: dict[str, str]
    ) -> None:
        # The widths are declared in the model, so this class defines no aliases of its own
        # and reads each field through the autocoded type.  Ref's FwPacketDescriptorType is
        # U16 while the framework default is U32; a proxy has to agree with the sender, and
        # what pins that is the model, not a using-declaration here.
        header = files["RefProxy.hpp"]
        assert "using PacketDescriptor" not in header
        assert "using RemoteOpcode" not in header
        for alias in (
            "FwPacketDescriptorType",
            "FwOpcodeType",
            "FwEventIdType",
            "FwChanIdType",
            "FwTlmPacketizeIdType",
            "Remote_FwSizeStoreType",
        ):
            assert f'#include "RefProxy/RefProxy_{alias}AliasAc.hpp"' in header
        assert (
            "::RefProxy::RefProxy_FwOpcodeType opcode" in header
        ), "beginCommandPacket takes the remote's opcode width"
        events = files["RefProxyEvents.cpp"]
        assert "::RefProxy::RefProxy_FwEventIdType _identifier = 0;" in events

    def test_descriptor_values_come_from_the_remotes_apid_enum(
        self, files: dict[str, str]
    ) -> None:
        header = files["RefProxy.hpp"]
        assert "DESCRIPTOR_COMMAND = 0," in header
        assert "DESCRIPTOR_TELEM = 1," in header
        assert "DESCRIPTOR_LOG = 2," in header
        assert "DESCRIPTOR_PACKETIZED_TLM = 4," in header

    def test_packet_header_size_is_symbolic(self, files: dict[str, str]) -> None:
        # Fw::Time::SERIALIZED_SIZE is configuration-dependent, so it must not be a
        # literal even though this dictionary makes it 11.
        assert (
            "PACKET_HEADER_SIZE = "
            "sizeof(::RefProxy::RefProxy_FwPacketDescriptorType) + "
            "sizeof(::RefProxy::RefProxy_FwTlmPacketizeIdType) "
            "+ Fw::Time::SERIALIZED_SIZE," in files["RefProxy.hpp"]
        )

    def test_asserts_the_widths_it_cannot_control(self, files: dict[str, str]) -> None:
        # Fw::Time and an autocoded struct deserialize themselves with the local
        # configuration; a mismatch has to be a build error, not silent corruption.
        # Ref's Svc.CustomVersionDb holds a string, which is what keeps the second.
        source = files["RefProxy.cpp"]
        assert "static_assert(" in source
        assert "Fw::Time::SERIALIZED_SIZE == 11," in source
        assert "sizeof(FwSizeStoreType) == 2," in source

    def test_strings_are_read_and_written_at_the_remotes_width(
        self, files: dict[str, str]
    ) -> None:
        remote = "::RefProxy::RefProxy_Remote_FwSizeStoreType"
        source = files["RefProxy.cpp"]
        assert f"{remote} length = 0;" in source
        assert f"packet.serializeFrom(static_cast<{remote}>(length));" in source
        assert "deserializeRemoteString(_packet, " in files["RefProxyEvents.cpp"]
        assert "serializeRemoteString(_packet, " in files["RefProxyCommands.cpp"]
        assert "_packet.deserializeTo(message)" not in files["RefProxyEvents.cpp"]

    def test_a_proxy_with_no_nested_string_does_not_assert_the_width(
        self, ref: Dictionary
    ) -> None:
        # Every top-level string goes through the proxy's own helpers, so a build whose
        # FwSizeStoreType differs from the remote's has nothing left to misread.
        plan = plan_for(ref, omit=["*"])
        assert not plan.spelling.nested_strings()
        source = render_implementation(plan).files()["RefProxy.cpp"]
        assert "sizeof(FwSizeStoreType)" not in source
        assert "Fw::Time::SERIALIZED_SIZE == 11," in source


class TestShadowing:
    """A dictionary names its parameters, and those names become locals."""

    def test_generator_locals_are_underscore_prefixed(
        self, files: dict[str, str]
    ) -> None:
        events = files["RefProxyEvents.cpp"]
        assert (
            "Fw::SerializeStatus _status = _packet.deserializeTo(_identifier);"
            in events
        )
        assert "if (_status == Fw::FW_SERIALIZE_OK) {" in events

    def test_an_argument_called_status_does_not_shadow_the_check(
        self, files: dict[str, str], plan: Plan
    ) -> None:
        events = files["RefProxyEvents.cpp"]
        clashing = [
            event
            for event in plan.events
            if any(p.name == "status" for p in event.source.params)
        ]
        assert clashing, "the Ref dictionary has events with a status argument"
        # The argument keeps its own name; the deserialization status keeps its.
        assert "_status = _packet.deserializeTo(status);" in events
        assert "Fw::SerializeStatus status" not in events

    def test_no_generated_local_can_collide_with_a_dictionary_name(
        self, plan: Plan
    ) -> None:
        # safe_identifier strips a leading underscore, so no dictionary name can reach
        # the space the generator reserves for itself.
        from fprime_dict_to_cpp.cpp.component import _param_name

        for group in (plan.commands, plan.events):
            for member in group:
                for param in member.source.params:
                    assert not _param_name(param).startswith("_")


class TestCommandForwarding:
    def test_serializes_the_remote_opcode(self, files: dict[str, str]) -> None:
        commands = files["RefProxyCommands.cpp"]
        assert "this->beginCommandPacket(_packet, 0xA10);" in commands

    def test_arguments_are_serialized_in_order(self, files: dict[str, str]) -> None:
        commands = files["RefProxyCommands.cpp"]
        assert "_status = _packet.serializeFrom(color);" in commands

    def test_response_is_sent_through_one_helper(self, files: dict[str, str]) -> None:
        commands = files["RefProxyCommands.cpp"]
        assert "this->sendCommandPacket(opCode, cmdSeq, _packet, _status);" in commands

    def test_the_helper_reports_a_disconnected_port(
        self, files: dict[str, str]
    ) -> None:
        source = files["RefProxy.cpp"]
        assert "if (!this->isConnected_comOut_OutputPort(0)) {" in source
        assert "Fw::CmdResponse::EXECUTION_ERROR" in source
        assert "Fw::CmdResponse::FORMAT_ERROR" in source


class TestDecoders:
    def test_dispatches_on_the_descriptor(self, files: dict[str, str]) -> None:
        source = files["RefProxy.cpp"]
        assert "data.resetDeser();" in source
        assert "case DESCRIPTOR_LOG: {" in source
        assert "this->decodeEvent(data);" in source

    def test_event_time_is_read_then_discarded(self, files: dict[str, str]) -> None:
        # The autocoded log_* helpers stamp from the local time port, so the remote's
        # timestamp has nowhere to go -- but it still has to be stepped over.
        events = files["RefProxyEvents.cpp"]
        assert "_status = _packet.deserializeTo(_timeTag);" in events
        assert "(void)_timeTag;" in events

    def test_channel_time_is_preserved(self, files: dict[str, str]) -> None:
        # tlmWrite_ does take an explicit Fw::Time, so telemetry keeps the remote's.
        telemetry = files["RefProxyTelemetry.cpp"]
        assert (
            "this->tlmWrite_cmdDisp_CommandsDispatched(_value, _timeTag);" in telemetry
        )

    def test_channelized_decoding_loops_over_the_buffer(
        self, files: dict[str, str]
    ) -> None:
        # TlmChan packs as many triples as fit into one buffer, not one.
        telemetry = files["RefProxyTelemetry.cpp"]
        assert "while (_packet.getDeserializeSizeLeft() > 0) {" in telemetry

    def test_unknown_channel_id_stops_rather_than_guessing(
        self, files: dict[str, str]
    ) -> None:
        telemetry = files["RefProxyTelemetry.cpp"]
        assert "recordPacketIgnored(DESCRIPTOR_TELEM" in telemetry

    def test_string_locals_are_sized_to_the_remote_declaration(
        self, files: dict[str, str]
    ) -> None:
        assert "Fw::StringTemplate<40>" in files["RefProxyEvents.cpp"]

    def test_scalar_locals_are_initialised(self, files: dict[str, str]) -> None:
        assert "U32 records = 0;" in files["RefProxyEvents.cpp"]


class TestPacketizedDecoding:
    def test_seeks_to_each_fixed_width_slot(self, files: dict[str, str]) -> None:
        # A packetized channel occupies a slot as wide as its type's maximum serialized
        # size, so reading straight through would drift on any variable-length value.
        packets = files["RefProxyPackets.cpp"]
        assert "FwSizeType _offset = PACKET_HEADER_SIZE;" in packets
        assert "_status = _packet.moveDeserToOffset(_offset);" in packets

    def test_advances_by_the_maximum_serialized_size(
        self, files: dict[str, str]
    ) -> None:
        packets = files["RefProxyPackets.cpp"]
        assert "_offset += sizeof(U32);" in packets
        assert (
            "_offset += ::RefProxy::RefProxy_Ref_SignalInfo::SERIALIZED_SIZE;"
            in packets
        )

    def test_one_function_per_packet_layout(
        self, files: dict[str, str], plan: Plan
    ) -> None:
        packets = files["RefProxyPackets.cpp"]
        for packet in plan.packets:
            assert f"decodePacket_{packet.name}(" in packets

    def test_can_be_turned_off(self, ref: Dictionary) -> None:
        plan = plan_for(ref, packetized=False)
        assert "RefProxyPackets" not in cpp_file_names(plan)
        assert "decodeTelemetryPacket" not in render_implementation(plan).files().get(
            "RefProxy.cpp", ""
        )


class TestComponentKinds:
    def test_queued_drains_its_own_queue(self, ref: Dictionary) -> None:
        source = render_implementation(
            plan_for(ref, kind=ComponentKind.QUEUED)
        ).files()["RefProxy.cpp"]
        assert "run_handler(" in source
        assert "this->dispatchCurrentMessages();" in source

    @pytest.mark.parametrize("kind", [ComponentKind.PASSIVE, ComponentKind.ACTIVE])
    def test_others_have_no_run_handler(
        self, ref: Dictionary, kind: ComponentKind
    ) -> None:
        source = render_implementation(plan_for(ref, kind=kind)).files()["RefProxy.cpp"]
        assert "run_handler(" not in source


class TestDiagnostics:
    def test_counters_are_written_and_initialised(self, files: dict[str, str]) -> None:
        source = files["RefProxy.cpp"]
        assert "m_packetsDecoded(0)," in source
        assert "this->tlmWrite_ProxyPacketsDecoded(this->m_packetsDecoded);" in source
        assert "this->log_WARNING_HI_ProxyDecodeError(" in source

    def test_can_be_turned_off_without_leaving_unused_parameters(
        self, ref: Dictionary
    ) -> None:
        source = render_implementation(plan_for(ref, diagnostics=False)).files()[
            "RefProxy.cpp"
        ]
        assert "m_packetsDecoded" not in source
        # The helpers keep their signatures, so their arguments have to be consumed or
        # -Wunused-parameter fires.
        assert "(void)descriptor;" in source
