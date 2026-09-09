"""Hooked ports and ``omit``: the two knobs that separate declaring from decoding.

The goldens pin what the generated text looks like and ``test_toolchain.py`` establishes
that a real toolchain accepts it.  What is left, and what lives here, is the reasoning in
between: which members survive, which of the survivors get a local id, what a port is
called when nobody named it, and what happens to a channel that is decoded for the sake of
the buffer rather than for the sake of anything reading it.
"""

from __future__ import annotations

import pytest

from fprime_dict_to_cpp.cpp import cpp_file_names, render_implementation
from fprime_dict_to_cpp.fpp import render_component, render_types
from fprime_dict_to_cpp.model import (
    ComponentKind,
    Dictionary,
    NamingError,
    QualifiedName,
)
from fprime_dict_to_cpp.naming import Naming
from fprime_dict_to_cpp.plan import Config, Plan, PortKind, resolve

#: Members of the Ref dictionary the tests below hook and omit by name.
COMMAND = QualifiedName.parse("Ref.dpDemo.SelectColor")
NO_OP = QualifiedName.parse("CdhCore.cmdDisp.CMD_NO_OP")
EVENT = QualifiedName.parse("CdhCore.cmdDisp.OpCodeDispatched")
CHANNEL = QualifiedName.parse("CdhCore.cmdDisp.CommandsDispatched")
OTHER_CHANNEL = QualifiedName.parse("CdhCore.cmdDisp.CommandErrors")
PACKET = "RefPackets.CDH"


def config_for(**overrides: object) -> Config:
    base: dict[str, object] = dict(
        component="RefProxy",
        namespace="RefProxy",
        kind=ComponentKind.ACTIVE,
        naming=Naming.preset("instance"),
        include_prefix="RefProxy",
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def plan_for(ref: Dictionary, **overrides: object) -> Plan:
    return resolve(ref, config_for(**overrides))


def port_for(plan: Plan, name: QualifiedName) -> object:
    """The port hooked onto ``name``, whichever kind of member it is."""
    for member in (*plan.commands, *plan.events, *plan.channels):
        if member.source.name == name and member.port is not None:
            return member.port
    raise AssertionError(f"no port is hooked onto {name}")


class TestOmit:
    def test_drops_a_member_from_the_declarations(self, ref: Dictionary) -> None:
        plan = plan_for(ref, omit=[str(COMMAND)])
        assert COMMAND not in {c.source.name for c in plan.declared_commands}
        assert "SelectColor" not in render_component(plan)

    def test_a_glob_matches_across_the_dots_of_a_dictionary_name(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, omit=["Ref.dpDemo.*"])
        remaining = {str(c.source.name) for c in plan.declared_commands}
        assert not any(name.startswith("Ref.dpDemo.") for name in remaining)
        # And it left every other instance alone.
        assert any(name.startswith("Ref.SG1.") for name in remaining)

    def test_omitting_everything_leaves_nothing_declared(self, ref: Dictionary) -> None:
        plan = plan_for(ref, omit=["*"])
        assert not plan.declared_commands
        assert not plan.declared_events
        assert not plan.declared_channels

    def test_an_omitted_member_without_a_port_is_not_decoded_either(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, omit=[str(EVENT)])
        assert EVENT not in {e.source.name for e in plan.events}

    def test_an_omitted_member_with_a_port_keeps_its_wire_handling(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, omit=[str(EVENT)], event_ports={EVENT: True})
        event = next(e for e in plan.events if e.source.name == EVENT)
        assert event.decoded and not event.declared
        assert event.local_id is None

    def test_local_ids_stay_dense_over_what_remains(self, ref: Dictionary) -> None:
        plan = plan_for(ref, omit=["Ref.dpDemo.*"], command_ports={COMMAND: True})
        ids = [c.local_id for c in plan.declared_commands]
        assert ids == list(range(len(ids)))
        # The omitted-but-ported command sits in the plan without taking an id.
        assert any(c.local_id is None for c in plan.commands)

    def test_renumbering_is_reported(self, ref: Dictionary) -> None:
        plan = plan_for(ref, omit=[str(COMMAND)])
        assert any("local ids are dense over what remains" in w for w in plan.warnings)

    def test_a_pattern_matching_nothing_is_an_error(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError, match="matches no command, event"):
            plan_for(ref, omit=["Ref.dpDemo.Nothing.At.All"])

    def test_a_near_miss_is_suggested(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError, match=r"did you mean Ref\.dpDemo\.SelectColor"):
            plan_for(ref, omit=["Ref.dpDemo.SelectColour"])

    def test_a_name_override_left_with_nothing_to_rename_is_reported(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(
            ref, omit=[str(COMMAND)], name_overrides={COMMAND: "SelectDemoColor"}
        )
        assert any("nothing left to rename" in w for w in plan.warnings)

    def test_the_types_of_a_dropped_member_are_dropped_with_it(
        self, ref: Dictionary
    ) -> None:
        # Ref.DpDemo.ColorEnum is reachable only from the dpDemo instance's SelectColor
        # command and ColorSelected event, so omitting them should stop the proxy emitting
        # a type nothing refers to -- an unused definition costs a generated C++ file pair.
        full = plan_for(ref)
        trimmed = plan_for(ref, omit=["Ref.dpDemo.*"])
        emitted = {d.qualified_name for d in trimmed.spelling.type_defs()}
        assert "Ref.DpDemo.ColorEnum" in {
            d.qualified_name for d in full.spelling.type_defs()
        }
        assert "Ref.DpDemo.ColorEnum" not in emitted

    def test_a_ported_member_keeps_its_types(self, ref: Dictionary) -> None:
        # The same omission, except that the command keeps a port -- so its argument type
        # is still needed, this time by the port's formals rather than by a declaration.
        plan = plan_for(ref, omit=["Ref.dpDemo.*"], command_ports={COMMAND: True})
        emitted = {d.qualified_name for d in plan.spelling.type_defs()}
        assert "Ref.DpDemo.ColorEnum" in emitted


class TestOmittedChannels:
    """A channelized packet carries no lengths, so an omitted channel is still read."""

    def test_an_omitted_channel_stays_in_the_plan_undecoded(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, omit=[str(OTHER_CHANNEL)])
        channel = next(c for c in plan.channels if c.source.name == OTHER_CHANNEL)
        assert not channel.decoded
        assert channel not in plan.declared_channels

    def test_its_value_is_read_and_discarded(self, ref: Dictionary) -> None:
        plan = plan_for(ref, omit=[str(OTHER_CHANNEL)])
        telemetry = render_implementation(plan).files(cpp_file_names(plan))[
            "RefProxyTelemetry.cpp"
        ]
        # The case is still there, so the channels the remote packed after it stay
        # reachable, and the value goes nowhere.
        channel = next(c for c in plan.channels if c.source.name == OTHER_CHANNEL)
        assert f"case 0x{channel.remote_id:X}: {{" in telemetry
        assert "(void)_value;" in telemetry

    def test_omitting_every_channel_stops_channelized_decoding(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, omit=["*"], packetized=False)
        assert not plan.decodes_channels
        assert "RefProxyTelemetry" not in cpp_file_names(plan)
        assert (
            "DESCRIPTOR_TELEM"
            not in render_implementation(plan).files(cpp_file_names(plan))[
                "RefProxy.cpp"
            ]
        )

    def test_one_telemetry_port_keeps_every_channel_walkable(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, omit=["*"], telemetry_ports={CHANNEL: True})
        assert len(plan.channels) == len(ref.channels)
        assert len(plan.decoded_channels) == 1


class TestPortNames:
    def test_a_derived_name_carries_its_kind(self, ref: Dictionary) -> None:
        plan = plan_for(
            ref,
            command_ports={COMMAND: True},
            event_ports={EVENT: True},
            telemetry_ports={CHANNEL: True},
            packet_ports={PACKET: True},
        )
        assert [(p.kind, p.name, p.type_name) for p in plan.ports] == [
            (
                PortKind.COMMAND,
                "dpDemo_SelectColor_cmdIn",
                "dpDemo_SelectColor_CmdPort",
            ),
            (
                PortKind.EVENT,
                "cmdDisp_OpCodeDispatched_eventOut",
                "cmdDisp_OpCodeDispatched_EventPort",
            ),
            (
                PortKind.TELEMETRY,
                "cmdDisp_CommandsDispatched_tlmOut",
                "cmdDisp_CommandsDispatched_TlmPort",
            ),
            (PortKind.PACKET, "CDH_packetOut", "CDH_PacketPort"),
        ]

    def test_an_event_and_a_channel_of_one_name_get_distinct_ports(
        self, ref: Dictionary
    ) -> None:
        # Svc::Version declares both an event and a channel called FrameworkVersion, and
        # they mangle to the same member name because FPP keeps the two in separate name
        # groups.  Ports share one group, which is what the kind suffixes are for.
        shared = QualifiedName.parse("CdhCore.version.FrameworkVersion")
        plan = plan_for(ref, event_ports={shared: True}, telemetry_ports={shared: True})
        names = [p.name for p in plan.ports]
        assert len(names) == len(set(names)) == 2
        assert not any("needed a suffix" in w for w in plan.warnings)

    def test_a_chosen_name_is_used_as_written(self, ref: Dictionary) -> None:
        plan = plan_for(ref, command_ports={NO_OP: "pingRemote"})
        assert [p.name for p in plan.ports] == ["pingRemote"]

    def test_false_asks_for_no_port(self, ref: Dictionary) -> None:
        plan = plan_for(ref, command_ports={COMMAND: False})
        assert not plan.ports

    def test_a_chosen_name_beats_a_derived_one(self, ref: Dictionary) -> None:
        # Naming one port what another would have derived makes the derived one move,
        # which is the rule mangling already follows for member names.
        plan = plan_for(
            ref,
            command_ports={
                COMMAND: True,
                NO_OP: "dpDemo_SelectColor_cmdIn",
            },
        )
        names = {p.origin: p.name for p in plan.ports}
        assert names[str(NO_OP)] == "dpDemo_SelectColor_cmdIn"
        assert names[str(COMMAND)] == "dpDemo_SelectColor_cmdIn_2"
        assert any("needed a suffix" in w for w in plan.warnings)

    def test_a_key_matching_nothing_is_an_error(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError, match="matches no command in the dictionary"):
            plan_for(ref, command_ports={QualifiedName.parse("No.Such.Command"): True})

    def test_a_packet_is_keyed_by_its_set_as_well(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError, match=r"<packet set>\.<packet>"):
            plan_for(ref, packet_ports={"CDH": True})

    def test_two_ports_cannot_share_a_chosen_name(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError, match="already the name chosen by"):
            plan_for(
                ref,
                command_ports={COMMAND: "shared"},
                event_ports={EVENT: "shared"},
            )

    def test_a_chosen_name_cannot_be_one_of_the_proxy_s_own_ports(
        self, ref: Dictionary
    ) -> None:
        with pytest.raises(NamingError, match="ports the proxy declares itself"):
            plan_for(ref, command_ports={COMMAND: "comOut"})

    def test_a_chosen_name_has_to_be_an_identifier(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError, match="not a usable member name"):
            plan_for(ref, command_ports={COMMAND: "2fast"})


class TestPortFormals:
    def test_a_command_port_carries_the_command_s_arguments(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, command_ports={COMMAND: True})
        port = port_for(plan, COMMAND)
        assert [(p.name, p.fpp) for p in port.params] == [  # type: ignore[attr-defined]
            ("color", "Ref_DpDemo_ColorEnum")
        ]

    def test_an_event_port_carries_the_event_s_arguments(self, ref: Dictionary) -> None:
        plan = plan_for(ref, event_ports={EVENT: True})
        port = port_for(plan, EVENT)
        assert [p.name for p in port.params] == ["Opcode", "port"]  # type: ignore[attr-defined]

    def test_a_telemetry_port_carries_the_remote_s_timestamp(
        self, ref: Dictionary
    ) -> None:
        # The one thing a relayed channel keeps that a relayed event cannot: tlmWrite_
        # takes an explicit Fw::Time, and so does this.
        plan = plan_for(ref, telemetry_ports={CHANNEL: True})
        port = port_for(plan, CHANNEL)
        assert [(p.name, p.fpp) for p in port.params] == [  # type: ignore[attr-defined]
            ("value", "U32"),
            ("timeTag", "Fw.Time"),
        ]

    def test_a_packet_port_carries_every_channel_then_the_one_timestamp(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, packet_ports={PACKET: True})
        packet = next(p for p in plan.packets if p.qualified_name == PACKET)
        assert packet.port is not None
        formals = [p.name for p in packet.port.params]
        assert formals[:-1] == [c.identifier for c in packet.channels]
        assert formals[-1] == "timeTag"

    def test_a_string_argument_is_clamped_to_the_remote_s_capacity(
        self, ref: Dictionary
    ) -> None:
        # A port hands over a Fw::StringBase of whatever capacity its caller had, but the
        # remote declared string size N and will read no more than that.
        send_file = QualifiedName.parse("FileHandling.fileDownlink.SendFile")
        plan = plan_for(ref, command_ports={send_file: True})
        commands = render_implementation(plan).files(cpp_file_names(plan))[
            "RefProxyCommands.cpp"
        ]
        assert (
            "const Fw::StringTemplate<100> _clampedSourceFileName(sourceFileName);"
            in commands
        )
        assert "_packet.serializeFrom(_clampedSourceFileName);" in commands

    def test_a_generated_port_type_is_declared_in_the_types_file(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, command_ports={COMMAND: True})
        assert "port dpDemo_SelectColor_CmdPort(" in render_types(plan)


class TestPortDeclarations:
    @pytest.mark.parametrize(
        ("kind", "declaration"),
        [
            (ComponentKind.ACTIVE, "async input port"),
            (ComponentKind.QUEUED, "async input port"),
            (ComponentKind.PASSIVE, "sync input port"),
        ],
    )
    def test_an_input_port_is_async_only_where_there_is_a_queue(
        self, ref: Dictionary, kind: ComponentKind, declaration: str
    ) -> None:
        plan = plan_for(ref, kind=kind, command_ports={COMMAND: True})
        assert plan.ports[0].declaration == declaration
        assert f"{declaration} dpDemo_SelectColor_cmdIn:" in render_component(plan)

    def test_an_output_port_is_invoked_only_when_it_is_connected(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, event_ports={EVENT: True})
        events = render_implementation(plan).files(cpp_file_names(plan))[
            "RefProxyEvents.cpp"
        ]
        assert (
            "if (this->isConnected_cmdDisp_OpCodeDispatched_eventOut_OutputPort(0)) {"
            in events
        )

    def test_a_declared_member_reports_through_both_paths(
        self, ref: Dictionary
    ) -> None:
        # A port is additive: hooking one does not stop the event being emitted.
        plan = plan_for(ref, event_ports={EVENT: True})
        events = render_implementation(plan).files(cpp_file_names(plan))[
            "RefProxyEvents.cpp"
        ]
        assert "this->log_COMMAND_cmdDisp_OpCodeDispatched(Opcode, port);" in events
        assert "this->cmdDisp_OpCodeDispatched_eventOut_out(0, Opcode, port);" in events


class TestForwardDiagnostics:
    def test_are_declared_only_when_an_input_port_exists(self, ref: Dictionary) -> None:
        assert "ProxyForwardErrors" not in plan_for(ref).diagnostic_channel_ids
        hooked = plan_for(ref, command_ports={COMMAND: True})
        assert "ProxyForwardErrors" in hooked.diagnostic_channel_ids
        assert "ProxyForwardError" in hooked.diagnostic_event_ids

    def test_come_after_the_ones_that_were_always_there(self, ref: Dictionary) -> None:
        # So that hooking a port renumbers nothing the ground already knows about.
        plain = plan_for(ref)
        hooked = plan_for(ref, command_ports={COMMAND: True})
        for name, identifier in plain.diagnostic_channel_ids.items():
            assert hooked.diagnostic_channel_ids[name] == identifier

    def test_are_not_declared_when_diagnostics_are_off(self, ref: Dictionary) -> None:
        plan = plan_for(ref, command_ports={COMMAND: True}, diagnostics=False)
        assert not plan.diagnostic_channel_ids
        assert "ProxyForwardError" not in render_component(plan)

    def test_no_member_may_take_one_of_their_names(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError, match="the names the proxy declares itself"):
            plan_for(ref, name_overrides={COMMAND: "ProxyForwardError"})


class TestPacketSelection:
    def test_a_packet_nobody_reads_is_not_decoded(self, ref: Dictionary) -> None:
        plan = plan_for(ref, omit=["*"], packet_ports={PACKET: True})
        assert [p.qualified_name for p in plan.packets] == [PACKET]

    def test_a_packet_is_kept_for_a_channel_that_is_still_reported(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, omit=["*"], telemetry_ports={CHANNEL: True})
        # CDH carries CommandsDispatched, so it is worth decoding even though the packet
        # itself was not hooked.
        assert PACKET in {p.qualified_name for p in plan.packets}

    def test_a_packet_port_with_packetized_off_is_an_error(
        self, ref: Dictionary
    ) -> None:
        # Otherwise the port would be declared and never invoked, which is the one thing
        # a hook must not quietly be.
        with pytest.raises(NamingError, match="packetized telemetry is turned off"):
            plan_for(ref, packetized=False, packet_ports={PACKET: True})

    def test_a_packet_port_switched_off_is_not_that_error(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, packetized=False, packet_ports={PACKET: False})
        assert not plan.ports
