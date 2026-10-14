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
from fprime_dict_to_cpp.fpp import render_component
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

#: A name the Ref dictionary gives both an event and a telemetry channel, which is what
#: makes omitting one kind of it distinguishable from omitting the member.
SHARED = QualifiedName.parse("CdhCore.version.FrameworkVersion")

#: An instance whose commands include some that a second pattern can pick back out: a
#: parameter set command, which is the usual thing a whole-instance hook does not want.
TYPE_DEMO = "Ref.typeDemo.*"
PRM_SET = "*_PRM_SET"


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


def hooked(plan: Plan) -> set[str]:
    """What every port of the plan hooks, as the dictionary names it."""
    return {port.origin for port in plan.ports}


def key(text: str) -> QualifiedName:
    """A port table key, which is a dictionary name or a pattern over one."""
    return QualifiedName.parse(text)


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


class TestOmitByKind:
    """``omit-commands``, ``omit-events`` and ``omit-telemetry``: one kind at a time."""

    def test_a_kind_list_leaves_the_other_kinds_alone(self, ref: Dictionary) -> None:
        plan = plan_for(ref, omit_commands=["Ref.dpDemo.*"])
        declared = {str(c.source.name) for c in plan.declared_commands}
        assert not any(name.startswith("Ref.dpDemo.") for name in declared)
        events = {str(e.source.name) for e in plan.declared_events}
        assert any(name.startswith("Ref.dpDemo.") for name in events)

    def test_a_star_takes_out_only_its_own_kind(self, ref: Dictionary) -> None:
        plan = plan_for(ref, omit_events=["*"])
        assert not plan.declared_events
        assert len(plan.declared_commands) == len(ref.commands)
        assert len(plan.declared_channels) == len(ref.channels)

    def test_each_kind_has_its_own_list(self, ref: Dictionary) -> None:
        plan = plan_for(
            ref,
            omit_commands=[str(NO_OP)],
            omit_events=[str(EVENT)],
            omit_telemetry=[str(CHANNEL)],
        )
        assert NO_OP not in {c.source.name for c in plan.declared_commands}
        assert EVENT not in {e.source.name for e in plan.declared_events}
        assert CHANNEL not in {c.source.name for c in plan.declared_channels}
        # And nothing else went with them.
        assert len(plan.declared_commands) == len(ref.commands) - 1
        assert len(plan.declared_events) == len(ref.events) - 1
        assert len(plan.declared_channels) == len(ref.channels) - 1

    def test_a_shared_name_is_omitted_one_kind_at_a_time(self, ref: Dictionary) -> None:
        # An event and a channel share this name, so omitting the event has to leave the
        # channel declared -- which omit, matching every kind, would not.
        plan = plan_for(ref, omit_events=[str(SHARED)])
        assert SHARED not in {e.source.name for e in plan.declared_events}
        assert SHARED in {c.source.name for c in plan.declared_channels}

    def test_the_two_lists_are_a_union(self, ref: Dictionary) -> None:
        plan = plan_for(ref, omit=[str(EVENT)], omit_events=[str(SHARED)])
        omitted = {str(EVENT), str(SHARED)}
        assert not omitted & {str(e.source.name) for e in plan.declared_events}

    def test_a_kind_list_matches_that_kind_only(self, ref: Dictionary) -> None:
        # The name is a command, so naming it in omit-events is the mistake it looks like
        # -- even though omit would have found it.
        with pytest.raises(NamingError, match="omit-events entry .*: matches no event"):
            plan_for(ref, omit_events=[str(NO_OP)])

    def test_a_pattern_matching_nothing_is_an_error(self, ref: Dictionary) -> None:
        with pytest.raises(
            NamingError, match="omit-telemetry entry .*: matches no telemetry channel"
        ):
            plan_for(ref, omit_telemetry=["CdhCore.cmdDisp.NothingAtAll"])

    def test_a_near_miss_is_suggested(self, ref: Dictionary) -> None:
        with pytest.raises(
            NamingError, match=r"did you mean CdhCore\.cmdDisp\.CMD_NO_OP"
        ):
            plan_for(ref, omit_commands=["CdhCore.cmdDisp.CMD_NO_0P"])

    def test_renumbering_is_reported_by_kind(self, ref: Dictionary) -> None:
        plan = plan_for(ref, omit_commands=[str(NO_OP)])
        note = next(w for w in plan.warnings if "local ids are dense" in w)
        assert "1 command(s)" in note
        assert "event(s)" not in note

    def test_an_override_on_a_name_still_declared_elsewhere_is_not_reported(
        self, ref: Dictionary
    ) -> None:
        # The channel of the same name keeps the override, so there is nothing idle here.
        plan = plan_for(
            ref, omit_events=[str(SHARED)], name_overrides={SHARED: "FrameworkRev"}
        )
        assert not any("nothing left to rename" in w for w in plan.warnings)

    def test_an_override_on_a_name_dropped_from_every_kind_is_reported(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(
            ref,
            omit_events=[str(SHARED)],
            omit_telemetry=[str(SHARED)],
            name_overrides={SHARED: "FrameworkRev"},
        )
        assert any("nothing left to rename" in w for w in plan.warnings)


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


class TestPortPatterns:
    """A port table's keys are matched as ``omit``'s entries are, globs and all."""

    def test_a_glob_hooks_every_member_it_reaches(self, ref: Dictionary) -> None:
        plan = plan_for(ref, command_ports={key("Ref.SG1.*"): True})
        assert hooked(plan) == {
            str(c.name) for c in ref.commands if ".SG1." in str(c.name)
        }
        # And each got the name it would have got had it been keyed by name.
        assert "SG1_Toggle_cmdIn" in {port.name for port in plan.ports}

    def test_a_star_hooks_every_member_of_that_kind_only(self, ref: Dictionary) -> None:
        plan = plan_for(ref, command_ports={key("*"): True})
        assert len(plan.input_ports) == len(ref.commands)
        assert not plan.output_ports

    def test_derived_names_stay_unique_across_a_whole_kind(
        self, ref: Dictionary
    ) -> None:
        # The stress case for the kind suffixes: every event and every channel at once,
        # which includes the pair that share a member name.
        plan = plan_for(
            ref, event_ports={key("*"): True}, telemetry_ports={key("*"): True}
        )
        names = [port.name for port in plan.ports]
        assert len(names) == len(set(names)) == len(ref.events) + len(ref.channels)
        assert not any("needed a suffix" in w for w in plan.warnings)

    def test_a_glob_matching_nothing_is_an_error(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError, match="matches no event in the dictionary"):
            plan_for(ref, event_ports={key("Ref.noSuchInstance.*"): True})

    def test_a_glob_cannot_name_a_port_outright(self, ref: Dictionary) -> None:
        # Every command it reached would ask to be called pingRemote, which is the one
        # thing port names cannot do.
        with pytest.raises(NamingError, match="a pattern cannot name a port outright"):
            plan_for(ref, command_ports={key("Ref.SG1.*"): "pingRemote"})

    def test_a_name_beats_a_glob_that_asks(self, ref: Dictionary) -> None:
        plan = plan_for(
            ref,
            command_ports={key("Ref.SG1.*"): True, key("Ref.SG1.Toggle"): False},
        )
        assert "Ref.SG1.Toggle" not in hooked(plan)
        assert len(hooked(plan)) == 3

    def test_a_name_beats_a_glob_that_refuses(self, ref: Dictionary) -> None:
        # The other direction, which is what lets one member be picked out of a kind that
        # was switched off wholesale.
        plan = plan_for(
            ref,
            command_ports={key("*"): False, key("Ref.SG1.Toggle"): True},
        )
        assert hooked(plan) == {"Ref.SG1.Toggle"}

    def test_a_name_may_name_the_port_a_glob_would_have_derived(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(
            ref,
            command_ports={
                key("Ref.SG1.*"): True,
                key("Ref.SG1.Toggle"): "toggleRemote",
            },
        )
        names = {port.origin: port.name for port in plan.ports}
        assert names["Ref.SG1.Toggle"] == "toggleRemote"
        assert names["Ref.SG1.Skip"] == "SG1_Skip_cmdIn"

    @pytest.mark.parametrize("reversed_", [False, True])
    def test_among_globs_a_refusal_beats_a_request(
        self, ref: Dictionary, reversed_: bool
    ) -> None:
        # Hook an instance, except the parameter set commands in it -- and the answer does
        # not depend on which of the two lines was written first.
        entries = [(key(TYPE_DEMO), True), (key(PRM_SET), False)]
        plan = plan_for(
            ref, command_ports=dict(reversed(entries) if reversed_ else entries)
        )
        reached = {
            str(c.name) for c in ref.commands if str(c.name).startswith("Ref.typeDemo.")
        }
        assert hooked(plan) == {n for n in reached if not n.endswith("_PRM_SET")}

    def test_a_packet_glob_matches_over_the_set_as_well(self, ref: Dictionary) -> None:
        plan = plan_for(ref, packet_ports={"RefPackets.SigGen1*": True})
        assert hooked(plan) == {"RefPackets.SigGen1", "RefPackets.SigGen1Info"}

    def test_a_glob_keeps_the_channels_it_reaches_decoded(
        self, ref: Dictionary
    ) -> None:
        # The combination the patterns are really for: declare nothing, and report one
        # instance's telemetry through ports.
        plan = plan_for(
            ref, omit=["*"], telemetry_ports={key("CdhCore.cmdDisp.*"): True}
        )
        assert not plan.declared_channels
        assert {str(c.source.name) for c in plan.decoded_channels} == {
            str(c.name)
            for c in ref.channels
            if str(c.name).startswith("CdhCore.cmdDisp.")
        }

    def test_a_glob_switching_a_whole_kind_off_is_not_a_hook(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, packetized=False, packet_ports={"RefPackets.*": False})
        assert not plan.ports


class TestPortFormals:
    def test_a_command_port_carries_the_command_s_arguments(
        self, ref: Dictionary
    ) -> None:
        plan = plan_for(ref, command_ports={COMMAND: True})
        port = port_for(plan, COMMAND)
        # Qualified through the component: a port definition cannot be a component member,
        # so it names the mirrored type from module scope.
        assert [(p.name, p.fpp) for p in port.params] == [  # type: ignore[attr-defined]
            ("color", "RefProxy.Ref_DpDemo_ColorEnum")
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
        assert (
            "_status = serializeRemoteString(_packet, _clampedSourceFileName);"
            in commands
        )

    def test_a_generated_port_type_is_declared_beside_the_component(
        self, ref: Dictionary
    ) -> None:
        # Beside rather than inside: FPP has no port definition among a component's
        # members, so these are the one thing the model file declares itself.
        plan = plan_for(ref, command_ports={COMMAND: True})
        assert "port dpDemo_SelectColor_CmdPort(" in render_component(plan)


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


class TestChannelizedOff:
    """``channelized: false``: a channel arrives only in a telemetry packet's slot."""

    @staticmethod
    def _carried(ref: Dictionary) -> set[QualifiedName]:
        return {
            member
            for packet_set in ref.packet_sets
            for packet in packet_set.packets
            for member in packet.members
        }

    def test_no_channelized_decoder_is_generated(self, ref: Dictionary) -> None:
        plan = plan_for(ref, channelized=False)
        assert not plan.decodes_channels
        assert "RefProxyTelemetry" not in cpp_file_names(plan)
        assert (
            "DESCRIPTOR_TELEM"
            not in render_implementation(plan).files(cpp_file_names(plan))[
                "RefProxy.cpp"
            ]
        )
        assert plan.has_packets

    def test_only_channels_a_packet_carries_are_declared(self, ref: Dictionary) -> None:
        carried = self._carried(ref)
        assert any(channel.name not in carried for channel in ref.channels)
        plan = plan_for(ref, channelized=False)
        assert {c.source.name for c in plan.declared_channels} == carried
        assert any("carried by no decoded telemetry packet" in w for w in plan.warnings)

    def test_packet_ports_alone_declare_no_channel(self, ref: Dictionary) -> None:
        # What a proxy that only feeds flight software from packets looks like.
        plan = plan_for(
            ref, channelized=False, omit_telemetry=["*"], packet_ports={PACKET: True}
        )
        assert not plan.declared_channels
        assert not plan.decodes_channels
        assert {p.qualified_name for p in plan.packets} == {PACKET}
        assert not any("carried by no" in w for w in plan.warnings)

    def test_a_telemetry_port_no_packet_feeds_is_an_error(
        self, ref: Dictionary
    ) -> None:
        loose = next(c.name for c in ref.channels if c.name not in self._carried(ref))
        with pytest.raises(NamingError, match="channelized telemetry is turned off"):
            plan_for(ref, channelized=False, telemetry_ports={loose: True})

    def test_a_telemetry_port_a_packet_feeds_is_kept(self, ref: Dictionary) -> None:
        plan = plan_for(
            ref, channelized=False, omit=["*"], telemetry_ports={CHANNEL: True}
        )
        assert hooked(plan) == {str(CHANNEL)}
        assert PACKET in {p.qualified_name for p in plan.packets}

    def test_omitting_every_channel_without_a_port_is_warned_about(
        self, ref: Dictionary
    ) -> None:
        # Nothing is left to decode a packet into, which is otherwise a silent
        # disappearance of the whole packetized decoder.
        plan = plan_for(ref, channelized=False, omit_telemetry=["*"])
        assert not plan.packets
        assert any("no telemetry packet is decoded" in w for w in plan.warnings)
