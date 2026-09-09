"""Name mangling: the shapes it produces, and the uniqueness it has to guarantee."""

from __future__ import annotations

import pytest

from fprime_dict_to_cpp.model import Dictionary, QualifiedName
from fprime_dict_to_cpp.naming import (
    PRESETS,
    Naming,
    Qualification,
    Style,
    join_camel,
    join_screaming,
    mangle_all,
    split_words,
)

CMD = QualifiedName.parse("Ref.dpDemo.SelectColor")
NO_OP = QualifiedName.parse("CdhCore.cmdDisp.CMD_NO_OP")
MIXED = QualifiedName.parse("DataProducts.dpBufferAccumulator.BA_DrainBuffers")
LOWER = QualifiedName.parse("ComCcsds.comQueue.comQueueDepth")


class TestQualifiedName:
    def test_splits_into_qualifier_and_member(self) -> None:
        assert CMD.member == "SelectColor"
        assert CMD.instance == "dpDemo"
        assert CMD.qualifier == ("Ref", "dpDemo")
        assert str(CMD) == "Ref.dpDemo.SelectColor"

    def test_unqualified_name_has_no_instance(self) -> None:
        bare = QualifiedName.parse("Alone")
        assert bare.member == "Alone"
        assert bare.instance == ""
        assert bare.qualifier == ()

    def test_ignores_empty_segments(self) -> None:
        assert QualifiedName.parse("a..b").parts == ("a", "b")


class TestSplitWords:
    @pytest.mark.parametrize(
        "segment, expected",
        [
            ("dpDemo", ["dp", "Demo"]),
            ("CdhCore", ["Cdh", "Core"]),
            ("CDHCore", ["CDH", "Core"]),
            ("CMD_NO_OP", ["CMD", "NO", "OP"]),
            ("SG1", ["SG", "1"]),
            ("systemResources", ["system", "Resources"]),
            ("Info", ["Info"]),
        ],
    )
    def test_splits_on_case_and_underscore(
        self, segment: str, expected: list[str]
    ) -> None:
        assert split_words(segment) == expected

    def test_joins_screaming(self) -> None:
        assert join_screaming(["CdhCore", "cmdDisp"]) == "CDH_CORE_CMD_DISP"

    def test_joins_camel_preserving_acronyms(self) -> None:
        assert join_camel(["Ref", "SG1", "Info"]) == "RefSG1Info"

    def test_joins_lower_camel(self) -> None:
        assert join_camel(["Ref", "dpDemo"], lower_first=True) == "refDpDemo"


class TestPresets:
    @pytest.mark.parametrize(
        "preset, name, expected",
        [
            ("instance", CMD, "dpDemo_SelectColor"),
            ("qualified", CMD, "Ref_dpDemo_SelectColor"),
            ("source-case", CMD, "RefDpDemoSelectColor"),
            ("member", CMD, "SelectColor"),
            ("instance", NO_OP, "cmdDisp_CMD_NO_OP"),
            ("qualified", NO_OP, "CdhCore_cmdDisp_CMD_NO_OP"),
            ("source-case", NO_OP, "CDH_CORE_CMD_DISP_CMD_NO_OP"),
            ("source-case", LOWER, "comCcsdsComQueueComQueueDepth"),
        ],
    )
    def test_known_shapes(
        self, preset: str, name: QualifiedName, expected: str
    ) -> None:
        assert Naming.preset(preset).apply(name) == expected

    def test_source_case_falls_back_for_mixed_conventions(self) -> None:
        # BA_DrainBuffers is neither screaming nor camel, so there is nothing to
        # preserve and the underscore join is the honest answer.
        assert Naming.preset("source-case").apply(MIXED) == (
            "DataProducts_dpBufferAccumulator_BA_DrainBuffers"
        )

    def test_unknown_preset_names_the_known_ones(self) -> None:
        with pytest.raises(ValueError, match="qualified"):
            Naming.preset("nonsense")

    def test_every_preset_is_reachable_from_the_knobs(self) -> None:
        for qualification, style in PRESETS.values():
            assert isinstance(qualification, Qualification)
            assert isinstance(style, Style)


class TestQualification:
    def test_escalates_to_full_then_stops(self) -> None:
        assert Qualification.MEMBER.escalated() is Qualification.INSTANCE
        assert Qualification.INSTANCE.escalated() is Qualification.FULL
        assert Qualification.FULL.escalated() is None

    def test_segments_kept_per_level(self) -> None:
        assert Qualification.MEMBER.segments(CMD) == ("SelectColor",)
        assert Qualification.INSTANCE.segments(CMD) == ("dpDemo", "SelectColor")
        assert Qualification.FULL.segments(CMD) == ("Ref", "dpDemo", "SelectColor")


class TestMangleAll:
    def test_leaves_distinct_names_short(self) -> None:
        names = [CMD, NO_OP]
        assigned = mangle_all(names, Naming.preset("member"))
        assert assigned[CMD] == "SelectColor"
        assert assigned[NO_OP] == "CMD_NO_OP"

    def test_escalates_only_the_colliding_group(self) -> None:
        shared_a = QualifiedName.parse("Ref.SG1.Info")
        shared_b = QualifiedName.parse("Ref.SG2.Info")
        assigned = mangle_all([shared_a, shared_b, CMD], Naming.preset("member"))
        assert assigned[shared_a] == "SG1_Info"
        assert assigned[shared_b] == "SG2_Info"
        # The name that never collided keeps the short form it asked for.
        assert assigned[CMD] == "SelectColor"

    def test_suffixes_when_full_qualification_still_collides(self) -> None:
        # Two names that are identical are only separable by a suffix.
        one = QualifiedName.parse("A.b.C")
        assigned = mangle_all([one], Naming.preset("qualified"), reserved=["A_b_C"])
        assert assigned[one] == "A_b_C_2"

    def test_avoids_reserved_names(self) -> None:
        run = QualifiedName.parse("X.y.run")
        assigned = mangle_all([run], Naming.preset("member"), reserved=["run"])
        assert assigned[run] != "run"

    def test_uniqueness_is_case_insensitive(self) -> None:
        # fpp-to-cpp derives EVENTID_<UPPER> from a member name, so two names that
        # differ only in case would emit the same C++ enumerator.
        upper = QualifiedName.parse("M.i.Foo")
        lower = QualifiedName.parse("M.i.foo")
        assigned = mangle_all([upper, lower], Naming.preset("member"))
        folded = {value.casefold() for value in assigned.values()}
        assert len(folded) == 2

    def test_result_is_independent_of_input_order(self) -> None:
        names = [CMD, NO_OP, MIXED, LOWER]
        forward = mangle_all(names, Naming.preset("instance"))
        backward = mangle_all(list(reversed(names)), Naming.preset("instance"))
        assert forward == backward


class TestAgainstRealDictionary:
    """The Ref dictionary is the interesting case: 590 names across three kinds."""

    @pytest.mark.parametrize("preset", sorted(PRESETS))
    def test_every_preset_is_unique_per_kind(
        self, ref: Dictionary, preset: str
    ) -> None:
        naming = Naming.preset(preset)
        for kind in (ref.commands, ref.events, ref.channels):
            names = [entry.name for entry in kind]
            assigned = mangle_all(names, naming)
            assert len(assigned) == len(set(names))
            folded = [value.casefold() for value in assigned.values()]
            assert len(folded) == len(set(folded)), f"{preset} collided"

    def test_names_are_legal_identifiers(self, ref: Dictionary) -> None:
        import re

        legal = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for preset in PRESETS:
            assigned = mangle_all(
                [entry.name for entry in ref.events], Naming.preset(preset)
            )
            for value in assigned.values():
                assert legal.match(value), f"{preset}: {value!r}"
