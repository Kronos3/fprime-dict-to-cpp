"""Type spelling: the FPP and C++ names, the slot sizes, and which types are emitted."""

from __future__ import annotations

import pytest

from fprime_dict_to_cpp.model import Dictionary, NamingError, TypeKind, TypeRef
from fprime_dict_to_cpp.plan import Plan
from fprime_dict_to_cpp.spelling import (
    CPP_KEYWORDS,
    IMPLIED_USE_NAMES,
    Context,
    Spelling,
    TypeNamer,
    check_type_identifier,
    safe_identifier,
)

U32 = TypeRef(TypeKind.INTEGER, "U32", 32, False)
F32 = TypeRef(TypeKind.FLOAT, "F32", 32)
BOOL = TypeRef(TypeKind.BOOL, "bool", 8)
STRING40 = TypeRef(TypeKind.STRING, "string", 40)
COLOR = TypeRef(TypeKind.QUALIFIED, "Ref.DpDemo.ColorEnum")
SIZE_ALIAS = TypeRef(TypeKind.QUALIFIED, "FwSizeType")
STRING_ALIAS = TypeRef(TypeKind.QUALIFIED, "Ref.DpDemo.StringAlias")


class TestTypeNamer:
    def test_flattens_a_qualified_name(self) -> None:
        namer = TypeNamer(namespace="RefProxy")
        assert namer.flat("Ref.DpDemo.ColorEnum") == "Ref_DpDemo_ColorEnum"
        assert namer.flat("TimeBase") == "TimeBase"

    def test_cpp_name_is_rooted_at_global_scope(self) -> None:
        # Inside class RefProxy the injected-class-name hides namespace RefProxy, so a
        # relative qualification would resolve into the class and fail to compile.
        namer = TypeNamer(namespace="RefProxy", component="RefProxy")
        assert (
            namer.cpp("Ref.DpDemo.ColorEnum")
            == "::RefProxy::RefProxy_Ref_DpDemo_ColorEnum"
        )

    def test_cpp_name_carries_the_component_prefix_fpp_to_cpp_gives_it(self) -> None:
        # The definitions are component members, so the autocoder prefixes every name and
        # every generated file with the component's.
        namer = TypeNamer(namespace="RefProxy", component="RefProxy")
        assert namer.cpp_name("Ref.DpDemo.ColorEnum") == "RefProxy_Ref_DpDemo_ColorEnum"

    def test_fpp_name_is_bare_inside_the_component_and_qualified_outside_it(
        self,
    ) -> None:
        # A member declaration sits inside the component; a hooked port type cannot, so it
        # names the same type through the component.
        namer = TypeNamer(namespace="RefProxy", component="RefProxy")
        assert namer.fpp("Ref.DpDemo.ColorEnum") == "Ref_DpDemo_ColorEnum"
        assert (
            namer.outer_fpp("Ref.DpDemo.ColorEnum") == "RefProxy.Ref_DpDemo_ColorEnum"
        )

    def test_nested_namespace_becomes_a_cpp_scope_chain(self) -> None:
        namer = TypeNamer(namespace="Ref.Proxy", component="Proxy")
        assert namer.cpp("A.B") == "::Ref::Proxy::Proxy_A_B"

    def test_prefix_is_applied(self) -> None:
        namer = TypeNamer(namespace="P", prefix="Rp_")
        assert namer.flat("TimeBase") == "Rp_TimeBase"

    def test_guards_names_fpp_uses_implicitly(self) -> None:
        # A definition of FwSizeStoreType inside the module shadows FPP's implied use of
        # it and breaks every string in that module.
        namer = TypeNamer(namespace="P")
        for name in IMPLIED_USE_NAMES:
            assert namer.flat(name) != name
            assert name in namer.flat(name)


class TestChosenTypeNames:
    """``TypeNamer.overrides``: types named outright instead of flattened."""

    def test_replace_the_flattened_name_everywhere(self) -> None:
        namer = TypeNamer(
            namespace="RefProxy", overrides={"Ref.DpDemo.ColorEnum": "DemoColor"}
        )
        assert namer.flat("Ref.DpDemo.ColorEnum") == "DemoColor"
        assert namer.fpp("Ref.DpDemo.ColorEnum") == "DemoColor"
        assert namer.cpp("Ref.DpDemo.ColorEnum") == "::RefProxy::DemoColor"

    def test_leave_every_other_type_alone(self) -> None:
        namer = TypeNamer(namespace="P", overrides={"A.B": "C"})
        assert namer.flat("D.E") == "D_E"

    def test_are_used_as_written_without_the_prefix(self) -> None:
        # The prefix exists to keep a derived name clear of a framework type; a name
        # somebody chose is the name they chose.
        namer = TypeNamer(namespace="P", prefix="Rp_", overrides={"TimeBase": "When"})
        assert namer.flat("TimeBase") == "When"
        assert namer.flat("Other") == "Rp_Other"


class TestCheckTypeIdentifier:
    @pytest.mark.parametrize("identifier", ["DemoColor", "Color2", "a_b_C"])
    def test_accepts_a_usable_name(self, identifier: str) -> None:
        check_type_identifier(identifier, "type name override for A.B")

    @pytest.mark.parametrize("identifier", ["", "2Color", "_Color", "a b", "A.B"])
    def test_rejects_what_is_not_an_identifier(self, identifier: str) -> None:
        with pytest.raises(NamingError):
            check_type_identifier(identifier, "type name override for A.B")

    def test_rejects_a_cpp_keyword_rather_than_escaping_it(self) -> None:
        # safe_identifier would quietly make it "delete_", which is not what was asked
        # for; a chosen name that cannot be used is worth saying so about.
        with pytest.raises(NamingError) as raised:
            check_type_identifier("delete", "type name override for A.B")
        assert "C++ keyword" in str(raised.value)

    @pytest.mark.parametrize("identifier", sorted(IMPLIED_USE_NAMES))
    def test_rejects_a_name_fpp_uses_implicitly(self, identifier: str) -> None:
        with pytest.raises(NamingError) as raised:
            check_type_identifier(identifier, "type name override for A.B")
        assert "implicitly" in str(raised.value)


class TestSafeIdentifier:
    def test_leaves_ordinary_names_alone(self) -> None:
        assert safe_identifier("ColorEnum") == "ColorEnum"
        assert safe_identifier("in") == "in"  # not a C++ keyword

    @pytest.mark.parametrize("name", ["class", "new", "template", "operator"])
    def test_escapes_cpp_keywords(self, name: str) -> None:
        assert name in CPP_KEYWORDS
        assert safe_identifier(name) == f"{name}_"

    def test_strips_a_leading_underscore(self) -> None:
        # Legal in FPP, reserved to the implementation in C++.
        assert safe_identifier("_bar") == "ubar"
        assert safe_identifier("__bar") == "ubar"

    def test_python_keywords_are_not_cpp_keywords(self) -> None:
        for name in ("def", "lambda", "pass", "elif", "None", "is"):
            assert safe_identifier(name) == name


class TestFppSpelling:
    def test_primitives_pass_through(self, plan: Plan) -> None:
        assert plan.spelling.fpp_type(U32) == "U32"
        assert plan.spelling.fpp_type(BOOL) == "bool"

    def test_string_carries_its_size(self, plan: Plan) -> None:
        # A bare "string" in FPP silently means FW_FIXED_LENGTH_STRING_SIZE.
        assert plan.spelling.fpp_type(STRING40) == "string size 40"

    def test_qualified_type_is_flattened(self, plan: Plan) -> None:
        assert plan.spelling.fpp_type(COLOR) == "Ref_DpDemo_ColorEnum"

    def test_string_without_a_size_is_an_error(self, plan: Plan) -> None:
        from fprime_dict_to_cpp.model import DictionaryError

        with pytest.raises(DictionaryError, match="no size"):
            plan.spelling.fpp_type(TypeRef(TypeKind.STRING, "string"))


class TestCppSpelling:
    def test_primitive_parameters_go_by_value(self, plan: Plan) -> None:
        assert plan.spelling.cpp_param(U32, Context.EVENT) == "U32"
        assert plan.spelling.cpp_param(F32, Context.COMMAND) == "F32"

    def test_class_types_go_by_const_reference(self, plan: Plan) -> None:
        assert (
            plan.spelling.cpp_param(COLOR, Context.COMMAND)
            == "const ::RefProxy::RefProxy_Ref_DpDemo_ColorEnum&"
        )

    def test_alias_to_a_scalar_goes_by_value(self, plan: Plan) -> None:
        # fpp-to-cpp emits a using-alias for a scalar, and passes it by value.
        assert plan.spelling.cpp_param(SIZE_ALIAS, Context.EVENT) == (
            "::RefProxy::RefProxy_FwSizeType"
        )

    def test_alias_to_a_string_goes_by_reference(self, plan: Plan) -> None:
        spelled = plan.spelling.cpp_param(STRING_ALIAS, Context.EVENT)
        assert spelled.startswith("const ") and spelled.endswith("&")

    def test_string_type_depends_on_context(self, plan: Plan) -> None:
        assert plan.spelling.cpp_type(STRING40, Context.COMMAND) == "Fw::CmdStringArg"
        assert plan.spelling.cpp_type(STRING40, Context.EVENT) == "Fw::StringBase"
        assert plan.spelling.cpp_type(STRING40, Context.TELEMETRY) == "Fw::StringBase"
        # A local has to be concrete, and sized to what the remote declared rather than
        # to whatever FW_LOG_STRING_MAX_SIZE happens to be here.
        assert (
            plan.spelling.cpp_type(STRING40, Context.LOCAL) == "Fw::StringTemplate<40>"
        )


class TestSerializedSize:
    """Spelled as fpp-to-cpp spells it in a generated telemetry packet list."""

    def test_primitive(self, plan: Plan) -> None:
        assert plan.spelling.serialized_size(U32) == "sizeof(U32)"

    def test_string(self, plan: Plan) -> None:
        assert (
            plan.spelling.serialized_size(STRING40)
            == "Fw::StringBase::STATIC_SERIALIZED_SIZE(40)"
        )

    def test_enum_array_and_struct(self, plan: Plan) -> None:
        assert (
            plan.spelling.serialized_size(COLOR)
            == "::RefProxy::RefProxy_Ref_DpDemo_ColorEnum::SERIALIZED_SIZE"
        )

    def test_alias_to_a_scalar_uses_sizeof_on_the_generated_alias(
        self, plan: Plan
    ) -> None:
        # FwSizeType is a using-alias, not a class, so it has no SERIALIZED_SIZE.  Asking
        # sizeof of the generated alias rather than of its underlying type keeps the
        # expression tied to the type the code actually uses.
        assert (
            plan.spelling.serialized_size(SIZE_ALIAS)
            == "sizeof(::RefProxy::RefProxy_FwSizeType)"
        )

    def test_alias_to_a_string_asks_the_generated_type(self, plan: Plan) -> None:
        # `type X = string size 80` generates `using X = Fw::String`, whose capacity is
        # FW_FIXED_LENGTH_STRING_SIZE and not 80.  A size computed from the dictionary
        # would disagree with the slot the sender actually left.
        assert (
            plan.spelling.serialized_size(STRING_ALIAS)
            == "::RefProxy::RefProxy_Ref_DpDemo_StringAlias::SERIALIZED_SIZE"
        )


class TestTypeSelection:
    def test_only_reachable_definitions_are_emitted(self, plan: Plan) -> None:
        emitted = {d.qualified_name for d in plan.spelling.type_defs()}
        # Referenced by an event argument.
        assert "Ref.DpDemo.ColorEnum" in emitted
        # A data-product record type: nothing this proxy mirrors refers to it.
        assert "Ref.DpDemo.StructWithEverything" not in emitted
        # Would shadow FPP's implied use if emitted, and nothing needs it.
        assert "FwSizeStoreType" not in emitted

    def test_transitive_dependencies_are_pulled_in(self, plan: Plan) -> None:
        emitted = {d.qualified_name for d in plan.spelling.type_defs()}
        # Ref.SignalInfo is a channel type; its members drag these in.
        assert "Ref.SignalInfo" in emitted
        assert "Ref.SignalSet" in emitted
        assert "Ref.SignalPairSet" in emitted

    def test_definitions_precede_their_dependants(self, plan: Plan) -> None:
        order = [d.qualified_name for d in plan.spelling.type_defs()]
        assert order.index("Ref.SignalPair") < order.index("Ref.SignalPairSet")

    def test_dictionary_defines_everything_it_references(self, plan: Plan) -> None:
        assert plan.spelling.missing_type_defs() == set()

    def test_missing_definition_is_reported(self, ref: Dictionary, config) -> None:  # type: ignore[no-untyped-def]
        from dataclasses import replace

        from fprime_dict_to_cpp.model import Channel, TelemetryUpdate

        orphan = Channel(
            name=ref.channels[0].name,
            id=999999,
            type=TypeRef(TypeKind.QUALIFIED, "Nowhere.AtAll"),
            update=TelemetryUpdate.ALWAYS,
        )
        broken = replace(ref, channels=(*ref.channels, orphan))
        spelling = Spelling(broken, TypeNamer(namespace="P"))
        assert spelling.missing_type_defs() == {"Nowhere.AtAll"}
