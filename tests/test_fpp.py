"""The FPP writer, the keyword escape, and the model the generator emits."""

from __future__ import annotations

import re

import pytest
from conftest import COMPONENT_KINDS

from fprime_dict_to_cpp.fpp import (
    escape,
    is_reserved,
    render_component,
    render_fpp,
    render_types,
)
from fprime_dict_to_cpp.fpp.keywords import RESERVED_WORDS
from fprime_dict_to_cpp.fpp.writer import FppWriter, annotation_lines, string_literal
from fprime_dict_to_cpp.model import ComponentKind, Dictionary
from fprime_dict_to_cpp.naming import Naming
from fprime_dict_to_cpp.plan import Config, Plan, resolve


class TestKeywords:
    def test_the_list_matches_the_compiler(self) -> None:
        # 118 from fpp v3.3.0's Lexer.scala, plus `vector` from an older release.  A
        # short list means a stale transcription and an identifier that will not parse.
        assert len(RESERVED_WORDS) == 119
        for word in ("time", "type", "size", "id", "severity", "format", "block"):
            assert is_reserved(word)

    def test_the_built_in_type_names_are_keywords_too(self) -> None:
        # Easy to miss: they are the only entries containing a digit.
        for word in ("U8", "U32", "I16", "F64"):
            assert is_reserved(word)

    def test_escapes_only_reserved_words(self) -> None:
        assert escape("size") == "$size"
        assert escape("records") == "records"

    def test_escape_is_lexical_not_a_rename(self) -> None:
        # $foo and foo are the same name, so the escape cannot be used to disambiguate.
        assert escape("size").lstrip("$") == "size"


class TestStringLiteral:
    def test_wraps_in_quotes(self) -> None:
        assert string_literal("hello {}") == '"hello {}"'

    def test_escapes_quotes_and_backslashes(self) -> None:
        assert string_literal('a "b" c') == '"a \\"b\\" c"'
        assert string_literal("a\\b") == '"a\\\\b"'

    def test_escapes_newlines_rather_than_emitting_them(self) -> None:
        # A literal newline would end the declaration.
        assert "\n" not in string_literal("a\nb")
        assert string_literal("a\nb") == '"a\\nb"'


class TestAnnotationLines:
    def test_one_line_per_input_line(self) -> None:
        assert annotation_lines("a\nb") == ["@ a", "@ b"]

    def test_empty_input_produces_nothing(self) -> None:
        assert annotation_lines(None) == []
        assert annotation_lines("") == []


class TestWriter:
    def test_nests_a_dotted_module_path(self) -> None:
        writer = FppWriter()
        with writer.module("A.B"):
            writer.line("constant x = 1")
        assert writer.render() == (
            "module A {\n  module B {\n    constant x = 1\n  }\n}\n"
        )

    def test_continues_a_multi_clause_declaration(self) -> None:
        writer = FppWriter()
        writer.declaration("event E", (), ["severity fatal", "id 0"])
        assert writer.render() == ("event E \\\n  severity fatal \\\n  id 0\n")

    def test_wraps_a_parameter_list(self) -> None:
        writer = FppWriter()
        writer.declaration("event E", ["a: U32", "b: F32 @< note"], ["id 0"])
        rendered = writer.render()
        assert "event E(" in rendered
        assert "a: U32," in rendered
        # A post-comment binds to the whole entry, so the comma precedes it.
        assert "b: F32 @< note" in rendered
        assert ") \\" in rendered

    def test_collapses_repeated_blank_lines(self) -> None:
        writer = FppWriter()
        writer.line("a")
        writer.blank()
        writer.blank()
        writer.line("b")
        assert writer.render() == "a\n\nb\n"


def _model(plan: Plan) -> str:
    """Every FPP file the plan produces, joined.

    The members live in ``.fppi`` fragments the component includes, so a spot check for one
    declaration has to look across the set rather than at the model file alone.
    """
    return "\n".join(render_fpp(plan).values())


class TestFileSet:
    def test_the_component_includes_a_fragment_per_section(self, plan: Plan) -> None:
        names = list(render_fpp(plan))
        assert names[0] == "RefProxy.fpp"
        assert set(names[1:]) == {
            "RefProxyTypes.fppi",
            "RefProxyCommands.fppi",
            "RefProxyEvents.fppi",
            "RefProxyTelemetry.fppi",
        }
        model = render_fpp(plan)["RefProxy.fpp"]
        for name in names[1:]:
            assert f'include "{name}"' in model

    def test_a_fragment_with_nothing_in_it_is_not_written(
        self, ref: Dictionary
    ) -> None:
        # Every command omitted, so there is no commands fragment and nothing includes one.
        plan = resolve(
            ref,
            Config(
                component="P",
                namespace="P",
                naming=Naming.preset("instance"),
                omit_commands=("*",),
            ),
        )
        files = render_fpp(plan)
        assert "PCommands.fppi" not in files
        assert "PCommands.fppi" not in files["P.fpp"]


class TestGeneratedModel:
    def test_types_are_declared_inside_the_component(self, plan: Plan) -> None:
        # A fragment, so no module or component header of its own -- the component that
        # includes it supplies both, which is what gives every generated name and file the
        # RefProxy_ prefix.
        text = render_types(plan)
        assert "module RefProxy {" not in text
        assert 'include "RefProxyTypes.fppi"' in render_fpp(plan)["RefProxy.fpp"]
        # I32 because that is FPP's default enum representation, which the dictionary
        # records rather than leaving implicit.
        assert "enum Ref_DpDemo_ColorEnum: I32 {" in text
        assert "RED = 0" in text
        # An explicitly narrowed representation survives too.
        assert "enum Fw_CmdResponse: U8 {" in text

    def test_the_remote_wire_widths_are_always_declared(self, plan: Plan) -> None:
        # The decoders read every packet header at these widths, so all five are declared
        # whether or not a mirrored member happens to use one.
        text = render_types(plan)
        for alias, width in (
            ("FwPacketDescriptorType", "U16"),
            ("FwOpcodeType", "U32"),
            ("FwEventIdType", "U32"),
            ("FwChanIdType", "U32"),
            ("FwTlmPacketizeIdType", "U16"),
        ):
            assert f"type {alias} = {width}" in text

    def test_aliases_resolve_to_the_remote_width(self, plan: Plan) -> None:
        text = render_types(plan)
        # FwSizeType is PlatformSizeType is U64 on the remote; the local platform may
        # differ, which is exactly why it is re-declared.
        assert "type FwSizeType = U64" in text

    def test_types_record_where_they_came_from(self, plan: Plan) -> None:
        assert "@ [Ref.DpDemo.ColorEnum]" in render_types(plan)

    def test_no_defaults_are_emitted(self, plan: Plan) -> None:
        # A default is a literal tree naming enum constants by their original path, and
        # it affects neither the wire format nor anything the proxy does.
        assert " default " not in render_types(plan)

    def test_component_declares_the_expected_ports(self, plan: Plan) -> None:
        text = render_component(plan)
        for declaration in (
            "async input port comIn: Fw.Com",
            "output port comOut: Fw.Com",
            "command recv port cmdIn",
            "event port logOut",
            "text event port logTextOut",
            "telemetry port tlmOut",
            "time get port timeCaller",
        ):
            assert declaration in text

    def test_a_component_with_events_declares_a_text_event_port(
        self, plan: Plan
    ) -> None:
        # fpp-check rejects a component with event specifiers and no text event port.
        assert "text event port" in render_component(plan)

    def test_queued_component_gets_a_run_port(self, ref: Dictionary) -> None:
        queued = resolve(
            ref,
            Config(
                component="P",
                namespace="P",
                kind=ComponentKind.QUEUED,
                naming=Naming.preset("instance"),
            ),
        )
        assert "sync input port run: Svc.Sched" in render_component(queued)

    def test_passive_component_has_no_run_port(self, ref: Dictionary) -> None:
        passive = resolve(
            ref,
            Config(
                component="P",
                namespace="P",
                kind=ComponentKind.PASSIVE,
                naming=Naming.preset("instance"),
            ),
        )
        assert "sync input port run" not in render_component(passive)
        assert "sync input port comIn: Fw.Com" in render_component(passive)
        assert "guarded command" in _model(passive)

    def test_members_carry_local_ids_and_note_the_remote_ones(self, plan: Plan) -> None:
        text = _model(plan)
        assert "async command dpDemo_SelectColor(" in text
        assert "opcode 0" in text
        assert "remote opcode 2576 (0xA10)" in text

    def test_severity_throttle_and_format_are_carried_over(self, plan: Plan) -> None:
        text = _model(plan)
        assert "severity activity high" in text
        assert "throttle " in text
        assert 'format "Color selected {}"' in text

    def test_limits_are_carried_over(self, plan: Plan) -> None:
        assert "high { red 1 }" in _model(plan)

    def test_update_on_change_is_carried_over(self, plan: Plan) -> None:
        assert "update on change" in _model(plan)

    def test_diagnostics_are_declared(self, plan: Plan) -> None:
        text = _model(plan)
        assert "telemetry ProxyPacketsDecoded: U32" in text
        assert "event ProxyDecodeError" in text

    def test_keyword_parameters_are_escaped(self, plan: Plan) -> None:
        # 59 formal parameters in the Ref dictionary are FPP keywords.
        text = _model(plan)
        assert "$size: U32" in text or "$size:" in text
        assert re.search(r"\$status\b|\$size\b|\$block\b", text)

    @pytest.mark.parametrize("kind", COMPONENT_KINDS)
    def test_no_tabs_anywhere(self, ref: Dictionary, kind: ComponentKind) -> None:
        # A tab is a lexical error in FPP outside strings and comments.
        plan = resolve(
            ref,
            Config(
                component="P",
                namespace="P",
                kind=kind,
                naming=Naming.preset("instance"),
            ),
        )
        assert "\t" not in _model(plan)

    def test_format_field_counts_match_argument_counts(self, plan: Plan) -> None:
        # fpp-check requires exactly one field per argument.
        for event in plan.events:
            stripped = event.source.format.replace("{{", "").replace("}}", "")
            fields = len(re.findall(r"\{[^{}]*\}", stripped))
            assert fields == len(event.source.params), event.source.name
