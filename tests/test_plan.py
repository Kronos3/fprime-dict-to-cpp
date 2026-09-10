"""Resolving a plan: local ids, packet layouts, and the warnings that matter."""

from __future__ import annotations

from dataclasses import replace

import pytest

from fprime_dict_to_cpp.model import (
    Channel,
    ComponentKind,
    Dictionary,
    NamingError,
    QualifiedName,
    TelemetryPacket,
    TelemetryPacketSet,
    TelemetryUpdate,
    TypeKind,
    TypeRef,
)
from fprime_dict_to_cpp.naming import Naming
from fprime_dict_to_cpp.plan import (
    DIAGNOSTIC_CHANNELS,
    DIAGNOSTIC_EVENTS,
    RESERVED_MEMBER_NAMES,
    Config,
    Plan,
    resolve,
)

U32 = TypeRef(TypeKind.INTEGER, "U32", 32, False)


def config_for(**overrides: object) -> Config:
    base = dict(
        component="RefProxy",
        namespace="RefProxy",
        kind=ComponentKind.ACTIVE,
        naming=Naming.preset("instance"),
        include_prefix="RefProxy",
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


class TestLocalIds:
    def test_are_dense_from_zero(self, plan: Plan) -> None:
        assert [c.local_opcode for c in plan.commands] == list(
            range(len(plan.commands))
        )
        assert [e.local_id for e in plan.events] == list(range(len(plan.events)))
        assert [c.local_id for c in plan.channels] == list(range(len(plan.channels)))

    def test_are_ordered_by_the_remote_value(self, plan: Plan) -> None:
        remote = [e.remote_id for e in plan.events]
        assert remote == sorted(remote)

    def test_remote_values_are_kept_separately(self, plan: Plan) -> None:
        # The remote's ids are absolute and go on the wire; the local ones are dense and
        # get offset by the topology's base id.  Conflating them is the central hazard.
        command = next(c for c in plan.commands if c.identifier == "dpDemo_SelectColor")
        assert command.remote_opcode == 2576
        assert command.local_opcode != command.remote_opcode

    def test_ordering_does_not_depend_on_section_order(self, ref: Dictionary) -> None:
        shuffled = replace(ref, events=tuple(reversed(ref.events)))
        first = {e.identifier: e.local_id for e in resolve(ref, config_for()).events}
        second = {
            e.identifier: e.local_id for e in resolve(shuffled, config_for()).events
        }
        assert first == second


class TestGeneratedNames:
    def test_an_event_and_a_channel_may_share_a_dictionary_name(
        self, plan: Plan
    ) -> None:
        # Svc::Version declares both an event and a channel called FrameworkVersion, and
        # FPP keeps the two in separate name groups.  Mangling per kind preserves that.
        name = QualifiedName.parse("CdhCore.version.FrameworkVersion")
        event = next(e for e in plan.events if e.source.name == name)
        channel = next(c for c in plan.channels if c.source.name == name)
        assert event.identifier == channel.identifier == "version_FrameworkVersion"

    def test_no_member_takes_a_reserved_name(self, plan: Plan) -> None:
        reserved = {name.casefold() for name in RESERVED_MEMBER_NAMES}
        for group in (plan.commands, plan.events, plan.channels):
            for member in group:
                assert member.identifier.casefold() not in reserved

    def test_emitter_names_follow_the_autocoder(self, plan: Plan) -> None:
        event = next(e for e in plan.events if e.identifier == "dpDemo_ColorSelected")
        assert event.log_function == "log_ACTIVITY_HI_dpDemo_ColorSelected"
        channel = next(
            c for c in plan.channels if c.identifier == "cmdDisp_CommandsDispatched"
        )
        assert channel.write_function == "tlmWrite_cmdDisp_CommandsDispatched"


class TestDiagnostics:
    def test_ids_come_after_every_mirrored_member(self, plan: Plan) -> None:
        channel_ids = plan.diagnostic_channel_ids
        event_ids = plan.diagnostic_event_ids
        assert min(channel_ids.values()) == len(plan.channels)
        assert min(event_ids.values()) == len(plan.events)
        assert set(channel_ids) == set(DIAGNOSTIC_CHANNELS)
        assert set(event_ids) == set(DIAGNOSTIC_EVENTS)

    def test_can_be_turned_off(self, ref: Dictionary) -> None:
        off = resolve(ref, config_for(diagnostics=False))
        assert off.diagnostic_channel_ids == {}
        assert off.diagnostic_event_ids == {}

    def test_turning_them_off_does_not_renumber_anything(self, ref: Dictionary) -> None:
        on = resolve(ref, config_for(diagnostics=True))
        off = resolve(ref, config_for(diagnostics=False))
        assert [e.local_id for e in on.events] == [e.local_id for e in off.events]


class TestPackets:
    def test_channels_are_in_wire_order(self, plan: Plan) -> None:
        packet = next(p for p in plan.packets if p.name == "SystemRes1")
        assert [c.source.name.member for c in packet.channels] == [
            "MEMORY_TOTAL",
            "MEMORY_USED",
            "NON_VOLATILE_TOTAL",
            "NON_VOLATILE_FREE",
        ]

    def test_packets_are_ordered_by_id(self, plan: Plan) -> None:
        ids = [p.id for p in plan.packets]
        assert ids == sorted(ids)

    def test_can_be_turned_off(self, ref: Dictionary) -> None:
        assert resolve(ref, config_for(packetized=False)).packets == ()

    def test_a_packet_naming_an_unknown_channel_is_skipped_with_a_warning(
        self, ref: Dictionary
    ) -> None:
        # Every channel after the unknown one sits at an unknown offset, so the packet
        # is not partially decodable -- it is not decodable at all.
        broken = TelemetryPacket(
            name="Broken",
            id=9999,
            group=1,
            members=(QualifiedName.parse("Nowhere.at.All"),),
        )
        original = ref.packet_sets[0]
        patched = replace(
            ref,
            packet_sets=(
                TelemetryPacketSet(
                    name=original.name, packets=(*original.packets, broken)
                ),
            ),
        )
        plan = resolve(patched, config_for())
        assert "Broken" not in {p.name for p in plan.packets}
        assert any("cannot be decoded" in w for w in plan.warnings)

    def test_duplicate_packet_ids_are_reported(self, ref: Dictionary) -> None:
        original = ref.packet_sets[0]
        clone = replace(original.packets[0], name="Clone")
        patched = replace(
            ref,
            packet_sets=(
                TelemetryPacketSet(
                    name=original.name, packets=(*original.packets, clone)
                ),
            ),
        )
        plan = resolve(patched, config_for())
        assert any("declared more than once" in w for w in plan.warnings)


class TestComponentKind:
    @pytest.mark.parametrize(
        "kind, port, command",
        [
            (ComponentKind.PASSIVE, "sync input port", "guarded"),
            (ComponentKind.QUEUED, "async input port", "async"),
            (ComponentKind.ACTIVE, "async input port", "async"),
        ],
    )
    def test_port_and_command_kinds(
        self, kind: ComponentKind, port: str, command: str
    ) -> None:
        from fprime_dict_to_cpp.model import CommandKind

        assert kind.port_kind == port
        # A passive component has no queue, so it cannot declare an async command
        # however the remote declared it.
        assert kind.command_kind(CommandKind.ASYNC) == command

    def test_only_passive_lacks_a_queue(self) -> None:
        assert not ComponentKind.PASSIVE.has_queue
        assert ComponentKind.QUEUED.has_queue
        assert ComponentKind.ACTIVE.has_queue


class TestWarnings:
    def test_the_ref_dictionary_needs_no_re_qualification(self, plan: Plan) -> None:
        assert not any("qualification" in w for w in plan.warnings)
        assert not any("reuses" in w for w in plan.warnings)

    def test_oversized_strings_are_reported_per_context(self, plan: Plan) -> None:
        # Ref declares 240- and 256-character strings, which F Prime's defaults clamp.
        # Not something the generator can fix, but the two deployments have to agree.
        commands = [w for w in plan.warnings if "FW_CMD_STRING_MAX_SIZE" in w]
        events = [w for w in plan.warnings if "FW_LOG_STRING_MAX_SIZE" in w]
        assert len(commands) == 1 and "20 command argument(s)" in commands[0]
        assert len(events) == 1 and "77 event argument(s)" in events[0]

    def test_channels_within_the_limit_are_not_reported(self, plan: Plan) -> None:
        # Every Ref string channel is exactly 40, which is the limit, not over it.
        assert not any("FW_TLM_STRING_MAX_SIZE" in w for w in plan.warnings)

    def test_reused_ids_are_reported(self, ref: Dictionary) -> None:
        clash = Channel(
            name=QualifiedName.parse("M.i.Clash"),
            id=ref.channels[0].id,
            type=U32,
            update=TelemetryUpdate.ALWAYS,
        )
        patched = replace(ref, channels=(*ref.channels, clash))
        plan = resolve(patched, config_for())
        assert any("reuses channel id" in w for w in plan.warnings)

    def test_re_qualification_is_reported(self, ref: Dictionary) -> None:
        plan = resolve(ref, config_for(naming=Naming.preset("member")))
        assert any("extra qualification" in w for w in plan.warnings)


SELECT_COLOR = QualifiedName.parse("Ref.dpDemo.SelectColor")
COLOR_ENUM = "Ref.DpDemo.ColorEnum"


class TestChosenNames:
    """Overrides: the members and types the caller named instead of the scheme."""

    def test_a_member_gets_the_name_it_was_given(self, ref: Dictionary) -> None:
        plan = resolve(
            ref, config_for(name_overrides={SELECT_COLOR: "SelectDemoColor"})
        )
        command = next(c for c in plan.commands if c.source.name == SELECT_COLOR)
        assert command.identifier == "SelectDemoColor"
        # Its emitters follow the chosen name, since they are derived from it.
        event = next(e for e in plan.events if e.identifier == "dpDemo_ColorSelected")
        assert event.log_function == "log_ACTIVITY_HI_dpDemo_ColorSelected"

    def test_only_that_member_moves(self, ref: Dictionary) -> None:
        plain = resolve(ref, config_for())
        chosen = resolve(ref, config_for(name_overrides={SELECT_COLOR: "Whatever"}))
        before = {str(c.source.name): c.identifier for c in plain.commands}
        after = {str(c.source.name): c.identifier for c in chosen.commands}
        differ = {name for name in before if before[name] != after[name]}
        assert differ == {str(SELECT_COLOR)}

    def test_local_ids_do_not_move_with_a_name(self, ref: Dictionary) -> None:
        # Ordering is by remote opcode, so renaming must not renumber anything: the
        # local ids reach the target's ground dictionary.
        plain = resolve(ref, config_for())
        chosen = resolve(ref, config_for(name_overrides={SELECT_COLOR: "Whatever"}))
        assert [c.local_opcode for c in plain.commands] == [
            c.local_opcode for c in chosen.commands
        ]

    def test_a_chosen_name_is_not_reported_as_re_qualified(
        self, ref: Dictionary
    ) -> None:
        # It differs from what the scheme would produce by construction, which is not
        # the same thing as having been adjusted to stay unique.
        plan = resolve(ref, config_for(name_overrides={SELECT_COLOR: "Anything"}))
        assert not any("extra qualification" in w for w in plan.warnings)

    def test_an_automatic_name_gives_way_to_a_chosen_one(self, ref: Dictionary) -> None:
        no_op = QualifiedName.parse("CdhCore.cmdDisp.CMD_NO_OP")
        plan = resolve(
            ref, config_for(name_overrides={no_op: "cmdDisp_CMD_NO_OP_STRING"})
        )
        names = {c.identifier for c in plan.commands}
        assert "cmdDisp_CMD_NO_OP_STRING" in names
        assert "CdhCore_cmdDisp_CMD_NO_OP_STRING" in names
        assert any("extra qualification" in w for w in plan.warnings)

    def test_a_type_gets_the_name_it_was_given(self, ref: Dictionary) -> None:
        plan = resolve(ref, config_for(type_name_overrides={COLOR_ENUM: "DemoColor"}))
        assert plan.spelling.namer.flat(COLOR_ENUM) == "DemoColor"
        # fpp-to-cpp prefixes a component's own type definitions with the component name.
        assert plan.spelling.namer.cpp(COLOR_ENUM) == "::RefProxy::RefProxy_DemoColor"


class TestChosenNamesAreChecked:
    def test_a_member_that_is_not_in_the_dictionary(self, ref: Dictionary) -> None:
        absent = QualifiedName.parse("Ref.dpDemo.SelectColour")
        with pytest.raises(NamingError) as raised:
            resolve(ref, config_for(name_overrides={absent: "X"}))
        message = str(raised.value)
        assert "matches no command, event or telemetry channel" in message
        assert "did you mean Ref.dpDemo.SelectColor?" in message

    def test_a_type_that_is_not_in_the_dictionary(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError) as raised:
            resolve(ref, config_for(type_name_overrides={"Ref.DpDemo.Colour": "X"}))
        assert "matches no type in the dictionary" in str(raised.value)

    def test_two_members_given_one_name(self, ref: Dictionary) -> None:
        other = QualifiedName.parse("Ref.dpDemo.Dp")
        overrides = {SELECT_COLOR: "Same", other: "same"}
        with pytest.raises(NamingError) as raised:
            resolve(ref, config_for(name_overrides=overrides))
        assert "cannot share one name" in str(raised.value)

    def test_a_name_the_proxy_declares_itself(self, ref: Dictionary) -> None:
        for reserved in ("comIn", "ProxyDecodeErrors"):
            with pytest.raises(NamingError) as raised:
                resolve(ref, config_for(name_overrides={SELECT_COLOR: reserved}))
            assert "the proxy declares itself" in str(raised.value)

    def test_an_unusable_member_name(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError):
            resolve(ref, config_for(name_overrides={SELECT_COLOR: "not an identifier"}))

    def test_a_type_name_another_type_already_spells(self, ref: Dictionary) -> None:
        # Two definitions of one name is an FPP redefinition, and fpp-to-cpp would
        # abort on the duplicate file long before that.
        with pytest.raises(NamingError) as raised:
            resolve(
                ref,
                config_for(type_name_overrides={COLOR_ENUM: "Ref_DpDemo_DpReqType"}),
            )
        assert "already how Ref.DpDemo.DpReqType is spelled" in str(raised.value)

    def test_a_type_no_mirrored_member_reaches_is_a_warning(
        self, ref: Dictionary
    ) -> None:
        # It is in the dictionary, so it is not a typo, but nothing will be emitted
        # under the chosen name either.
        unreachable = next(
            definition.qualified_name
            for definition in ref.type_defs
            if definition.qualified_name
            not in {
                emitted.qualified_name
                for emitted in resolve(ref, config_for()).spelling.all_type_defs()
            }
        )
        plan = resolve(ref, config_for(type_name_overrides={unreachable: "Unused"}))
        assert any("nothing to rename" in w for w in plan.warnings)


class TestConfig:
    def test_cpp_namespace_is_a_scope_chain(self) -> None:
        assert config_for(namespace="Ref.Proxy").cpp_namespace == "Ref::Proxy"

    def test_base_header_uses_the_include_prefix(self) -> None:
        assert config_for().base_header == "RefProxy/RefProxyComponentAc.hpp"
        assert config_for(include_prefix="").base_header == "RefProxyComponentAc.hpp"

    def test_base_class_follows_the_autocoder(self) -> None:
        assert config_for().base_class == "RefProxyComponentBase"
