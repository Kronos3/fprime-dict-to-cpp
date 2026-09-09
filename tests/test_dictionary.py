"""Reading a dictionary: what it must get right, and what it must refuse."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fprime_dict_to_cpp import dictionary
from fprime_dict_to_cpp.model import (
    AliasDef,
    ArrayDef,
    CommandKind,
    Dictionary,
    DictionaryError,
    EnumDef,
    QueueFullBehavior,
    Severity,
    StructDef,
    TelemetryUpdate,
    TypeKind,
)

MINIMAL: dict[str, Any] = {
    "metadata": {
        "deploymentName": "Demo.Demo",
        "frameworkVersion": "v4.3.0",
        "projectVersion": "v1.0.0",
        "dictionarySpecVersion": "1.0.0",
    },
    "typeDefinitions": [],
    "commands": [],
    "events": [],
    "telemetryChannels": [],
}


def write(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "dict.json"
    path.write_text(json.dumps(raw))
    return path


class TestMetadata:
    def test_reads_the_ref_dictionary(self, ref: Dictionary) -> None:
        assert ref.metadata.deployment_name == "Ref.Ref"
        assert ref.metadata.spec_version == "1.0.0"

    def test_deployment_identifier_is_the_last_segment(self, ref: Dictionary) -> None:
        assert ref.metadata.deployment_identifier == "Ref"

    def test_missing_metadata_is_an_error(self, tmp_path: Path) -> None:
        raw = dict(MINIMAL)
        del raw["metadata"]
        with pytest.raises(DictionaryError, match="metadata"):
            dictionary.load(write(tmp_path, raw))

    def test_missing_deployment_name_is_an_error(self, tmp_path: Path) -> None:
        raw = {**MINIMAL, "metadata": {"frameworkVersion": "v4.3.0"}}
        with pytest.raises(DictionaryError, match="deploymentName"):
            dictionary.load(write(tmp_path, raw))

    def test_absent_sections_are_allowed(self, tmp_path: Path) -> None:
        # A deployment with no data products has no records section; the same has to
        # hold for a deployment with no commands.
        parsed = dictionary.load(write(tmp_path, {"metadata": MINIMAL["metadata"]}))
        assert parsed.commands == ()
        assert parsed.events == ()
        assert parsed.channels == ()

    def test_malformed_json_says_so(self, tmp_path: Path) -> None:
        path = tmp_path / "dict.json"
        path.write_text("{ not json")
        with pytest.raises(DictionaryError, match="not valid JSON"):
            dictionary.load(path)


class TestCounts:
    def test_ref_section_sizes(self, ref: Dictionary) -> None:
        assert len(ref.commands) == 116
        assert len(ref.events) == 311
        assert len(ref.channels) == 163
        assert len(ref.type_defs) == 99
        assert len(ref.constants) == 18
        assert len(ref.packet_sets) == 1
        assert len(ref.packet_sets[0].packets) == 34


class TestPlatformTypes:
    def test_taken_from_the_dictionary_not_the_local_platform(
        self, ref: Dictionary
    ) -> None:
        platform = ref.platform
        assert (platform.packet_descriptor.name, platform.packet_descriptor.size) == (
            "U16",
            16,
        )
        assert platform.opcode.name == "U32"
        assert platform.event_id.name == "U32"
        assert platform.channel_id.name == "U32"
        # The packetize id defaults to U16, and Ref confirms it.
        assert platform.packetize_id.name == "U16"

    def test_falls_back_to_framework_defaults_when_absent(self, tmp_path: Path) -> None:
        parsed = dictionary.load(write(tmp_path, MINIMAL))
        assert parsed.platform.opcode.name == "U32"
        assert parsed.platform.packet_descriptor.name == "U32"
        assert parsed.platform.packetize_id.name == "U16"

    def test_chases_an_alias_to_its_underlying_width(self, ref: Dictionary) -> None:
        # FwOpcodeType -> FwIdType -> U32.  The proxy needs the resolved end.
        alias = ref.type_def("FwOpcodeType")
        assert isinstance(alias, AliasDef)
        assert alias.target.name == "FwIdType"
        assert alias.underlying.name == "U32"


class TestTypeDefinitions:
    def test_enum_keeps_constants_and_representation(self, ref: Dictionary) -> None:
        definition = ref.type_def("ComCfg.Apid")
        assert isinstance(definition, EnumDef)
        assert definition.representation.name == "U16"
        values = {c.name: c.value for c in definition.constants}
        assert values["FW_PACKET_COMMAND"] == 0
        assert values["FW_PACKET_PACKETIZED_TLM"] == 4

    def test_struct_members_are_ordered_by_declaration_index(
        self, ref: Dictionary
    ) -> None:
        definition = ref.type_def("Fw.TimeValue")
        assert isinstance(definition, StructDef)
        assert [m.name for m in definition.members] == [
            "timeBase",
            "timeContext",
            "seconds",
            "useconds",
        ]
        assert [m.index for m in definition.members] == [0, 1, 2, 3]

    def test_inline_array_member_keeps_its_size(self, ref: Dictionary) -> None:
        definition = ref.type_def("Ref.ChoiceSlurry")
        assert isinstance(definition, StructDef)
        member = next(m for m in definition.members if m.name == "choiceAsMemberArray")
        assert member.array_size == 2

    def test_array_format_is_read_from_the_array(self, ref: Dictionary) -> None:
        # The format sits beside "size", not on the element type.
        definition = ref.type_def("Ref.SignalSet")
        assert isinstance(definition, ArrayDef)
        assert definition.size == 4
        assert definition.element.name == "F32"
        assert definition.format == "{f}"

    def test_array_format_on_the_element_type_is_still_accepted(
        self, tmp_path: Path
    ) -> None:
        raw = {
            **MINIMAL,
            "typeDefinitions": [
                {
                    "kind": "array",
                    "qualifiedName": "M.Legacy",
                    "size": 2,
                    "elementType": {
                        "name": "U32",
                        "kind": "integer",
                        "size": 32,
                        "signed": False,
                        "format": "{x}",
                    },
                }
            ],
        }
        parsed = dictionary.load(write(tmp_path, raw))
        definition = parsed.type_def("M.Legacy")
        assert isinstance(definition, ArrayDef)
        assert definition.format == "{x}"

    def test_alias_to_a_string_keeps_its_size(self, ref: Dictionary) -> None:
        definition = ref.type_def("Ref.DpDemo.StringAlias")
        assert isinstance(definition, AliasDef)
        assert definition.underlying.kind is TypeKind.STRING
        assert definition.underlying.size == 80

    def test_duplicate_struct_index_is_an_error(self, tmp_path: Path) -> None:
        raw = {
            **MINIMAL,
            "typeDefinitions": [
                {
                    "kind": "struct",
                    "qualifiedName": "M.Bad",
                    "members": {
                        "a": {"type": {"name": "U32", "kind": "integer"}, "index": 0},
                        "b": {"type": {"name": "U32", "kind": "integer"}, "index": 0},
                    },
                }
            ],
        }
        with pytest.raises(DictionaryError, match="reuses member index"):
            dictionary.load(write(tmp_path, raw))

    def test_unknown_type_kind_is_an_error(self, tmp_path: Path) -> None:
        raw = {
            **MINIMAL,
            "typeDefinitions": [{"kind": "wormhole", "qualifiedName": "M.X"}],
        }
        with pytest.raises(DictionaryError, match="unknown kind"):
            dictionary.load(write(tmp_path, raw))


class TestEntries:
    def test_command_kinds_include_the_parameter_pair(self, ref: Dictionary) -> None:
        kinds = {command.kind for command in ref.commands}
        assert CommandKind.ASYNC in kinds
        assert CommandKind.SYNC in kinds
        assert CommandKind.GUARDED in kinds
        # FPP generates a set/save pair for every param declaration, and they arrive
        # in the commands section rather than the parameters one.
        assert CommandKind.SET in kinds
        assert CommandKind.SAVE in kinds
        assert all(k.is_parameter_command for k in (CommandKind.SET, CommandKind.SAVE))

    def test_queue_full_behaviour_is_optional(self, ref: Dictionary) -> None:
        behaviours = {command.queue_full_behavior for command in ref.commands}
        assert QueueFullBehavior.ASSERT in behaviours
        assert None in behaviours

    def test_every_severity_appears(self, ref: Dictionary) -> None:
        assert {event.severity for event in ref.events} == set(Severity)

    def test_severity_spellings(self) -> None:
        assert Severity.WARNING_HI.fpp == "warning high"
        assert Severity.WARNING_HI.cpp == "WARNING_HI"
        assert Severity.ACTIVITY_LO.fpp == "activity low"

    def test_telemetry_update_spellings(self, ref: Dictionary) -> None:
        assert {c.update for c in ref.channels} == set(TelemetryUpdate)
        assert TelemetryUpdate.ON_CHANGE.fpp == "update on change"

    def test_partial_limits_are_kept(self, ref: Dictionary) -> None:
        limited = [c for c in ref.channels if c.limits]
        assert limited, "the Ref dictionary has limited channels"
        channel = next(c for c in limited if c.name.member == "NoBuffs")
        assert channel.limits.high.red == 1
        assert channel.limits.high.orange is None
        assert not channel.limits.low

    def test_a_name_without_a_qualifier_is_rejected(self, tmp_path: Path) -> None:
        raw = {
            **MINIMAL,
            "commands": [
                {"name": "Bare", "commandKind": "sync", "opcode": 0, "formalParams": []}
            ],
        }
        with pytest.raises(DictionaryError, match="MEMBER"):
            dictionary.load(write(tmp_path, raw))

    def test_unknown_command_kind_is_an_error(self, tmp_path: Path) -> None:
        raw = {
            **MINIMAL,
            "commands": [
                {
                    "name": "M.i.C",
                    "commandKind": "telepathic",
                    "opcode": 0,
                    "formalParams": [],
                }
            ],
        }
        with pytest.raises(DictionaryError, match="commandKind"):
            dictionary.load(write(tmp_path, raw))

    def test_event_without_a_format_is_an_error(self, tmp_path: Path) -> None:
        raw = {
            **MINIMAL,
            "events": [
                {"name": "M.i.E", "severity": "FATAL", "id": 0, "formalParams": []}
            ],
        }
        with pytest.raises(DictionaryError, match="format"):
            dictionary.load(write(tmp_path, raw))


class TestPacketSets:
    def test_members_are_kept_in_wire_order(self, ref: Dictionary) -> None:
        packet = next(p for p in ref.packet_sets[0].packets if p.name == "SystemRes1")
        assert [m.member for m in packet.members] == [
            "MEMORY_TOTAL",
            "MEMORY_USED",
            "NON_VOLATILE_TOTAL",
            "NON_VOLATILE_FREE",
        ]

    def test_packet_ids_may_be_sparse(self, ref: Dictionary) -> None:
        ids = sorted(p.id for p in ref.packet_sets[0].packets)
        assert ids[:6] == [1, 2, 3, 4, 5, 6]
        assert 7 not in ids  # Ref skips 7 to 9; nothing may index by packet id


class TestCrossCheck:
    def test_agrees_with_fprime_gds(self, ref_path: Path, ref: Dictionary) -> None:
        problems = dictionary.cross_check(ref_path, ref)
        if problems and "not installed" in problems[0]:
            pytest.skip("fprime-gds is not installed")
        assert problems == []
