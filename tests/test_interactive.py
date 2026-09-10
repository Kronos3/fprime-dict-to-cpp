"""``-i``: the shaping questions, and the configuration file it leaves behind.

Answers are matched against the question that was just asked rather than counted off in
order, so these tests say what they mean and do not have to be renumbered every time a
question moves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fprime_dict_to_cpp import config_file, interactive
from fprime_dict_to_cpp.cli import build_parser, main


class Transcript:
    """A stand-in for standard error that remembers what was written to it."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.writes)

    @property
    def prompt(self) -> str:
        """The most recent thing written -- the question awaiting an answer."""
        return self.writes[-1] if self.writes else ""


class Answers:
    """A stand-in for standard input that answers by looking at the question.

    ``script`` maps a substring of a question to the answer it should get; a question
    matching nothing is answered with an empty line, which accepts its default.  An
    answer may be a list, whose entries are used one after another, which is how a
    rejected answer followed by an accepted one is expressed.
    """

    def __init__(
        self,
        transcript: Transcript,
        script: dict[str, str | list[str]] | None = None,
        *,
        eof: bool = False,
    ) -> None:
        self._transcript = transcript
        self.script = dict(script or {})
        self.eof = eof
        self.answered = 0

    def readline(self) -> str:
        if self.eof:
            return ""
        asked = self._transcript.prompt
        self.answered += 1
        assert self.answered < 60, (
            "a question is being asked over and over; the transcript so far is:\n"
            + self._transcript.text
        )
        for key, answer in self.script.items():
            if key not in asked:
                continue
            if isinstance(answer, list):
                return f"{answer.pop(0)}\n" if answer else "\n"
            return f"{answer}\n"
        return "\n"


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Run ``main`` with the questions and answers on stand-in streams.

    The streams are substituted inside ``run`` rather than here: pytest re-installs its
    own capture streams at the start of the call phase, which would undo a substitution
    made during setup.
    """
    transcript = Transcript()

    def run(
        argv: list[str],
        script: dict[str, str | list[str]] | None = None,
        *,
        eof: bool = False,
    ) -> int:
        monkeypatch.setattr("sys.stderr", transcript)
        monkeypatch.setattr("sys.stdin", Answers(transcript, script, eof=eof))
        return main(argv)

    run.transcript = transcript  # type: ignore[attr-defined]
    return run


class TestParser:
    def test_the_dictionary_is_optional_only_to_be_asked_for(self) -> None:
        assert build_parser().parse_args(["-i"]).dictionary is None

    def test_a_missing_dictionary_is_still_an_error_without_i(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exit:
            main([])
        assert exit.value.code == 2
        assert "-i/--interactive" in capsys.readouterr().err


class TestTheQuestions:
    """Few, and no more: the rest of the configuration is a file, not a prompt."""

    def test_are_only_the_ones_that_shape_the_component(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        assert session([str(ref_path), "-o", str(tmp_path / "out"), "-i"]) == 0
        asked = session.transcript.text
        for question in (
            "component name",
            "namespace",
            "output directory",
            "component kind",
        ):
            assert question in asked
        for finer in (
            "naming preset",
            "qualification",
            "type name prefix",
            "include prefix",
            "packetized",
            "clang-format",
            "cross-check",
            "proceed",
        ):
            assert finer not in asked, finer

    def test_reach_the_generated_component(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "elsewhere"
        code = session(
            [str(ref_path), "-i"],
            {
                "component name": "P",
                "output directory": str(output),
                "component kind": "passive",
            },
        )
        assert code == 0
        assert "passive component P {" in (output / "P.fpp").read_text()

    def test_offer_the_component_name_as_the_namespace_without_assuming_it(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        # The default is the component's name, but it is answered rather than assumed, so
        # a component in a module of its own is one line of typing away.
        output = tmp_path / "out"
        assert (
            session(
                [str(ref_path), "-o", str(output), "-i"],
                {"component name": "P", "namespace": "Ref.Proxies"},
            )
            == 0
        )
        assert "namespace [P]" in session.transcript.text
        fpp = (output / "P.fpp").read_text()
        assert "module Ref {" in fpp and "module Proxies {" in fpp
        assert "\nnamespace: Ref.Proxies\n" in (output / "P.yaml").read_text()

    def test_a_flag_given_on_the_command_line_becomes_the_offered_default(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "out"
        assert (
            session([str(ref_path), "-o", str(output), "-i", "--kind", "queued"]) == 0
        )
        assert "[queued]" in session.transcript.text
        assert "queued component" in (output / "RefProxy.fpp").read_text()

    def test_an_unrecognised_answer_is_asked_again(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "out"
        assert (
            session(
                [str(ref_path), "-o", str(output), "-i"],
                {"component kind": ["sychronous", "passive"]},
            )
            == 0
        )
        assert "choose one of" in session.transcript.text
        assert "passive component" in (output / "RefProxy.fpp").read_text()

    def test_the_questions_stay_off_standard_output(
        self, session, tmp_path: Path, ref_path: Path, capsys  # type: ignore[no-untyped-def]
    ) -> None:
        session([str(ref_path), "-o", str(tmp_path / "out"), "-i"])
        assert "component kind" in session.transcript.text
        assert "component kind" not in capsys.readouterr().out


class TestTheConfigurationItWrites:
    def test_sits_beside_the_component_it_builds(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "Ref" / "RefProxy"
        assert session([str(ref_path), "-o", str(output), "-i"]) == 0
        assert (output / "RefProxy.yaml").is_file()
        assert (output / "RefProxy.fpp").is_file()

    def test_holds_the_answers_and_comments_out_everything_else(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "out"
        session([str(ref_path), "-o", str(output), "-i"], {"component kind": "passive"})
        text = (output / "RefProxy.yaml").read_text()
        for answered in (
            "component: RefProxy",
            "namespace: RefProxy",
            "kind: passive",
            "output: .",
        ):
            assert f"\n{answered}\n" in text, answered
        for commented in ("# naming: instance", "# packetized: true", "# names:"):
            assert f"\n{commented}\n" in text, commented

    def test_says_what_every_key_is_for(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        # The point of the file is that the finer-grained options are discoverable
        # without going and reading anything else.
        output = tmp_path / "out"
        session([str(ref_path), "-o", str(output), "-i"])
        text = (output / "RefProxy.yaml").read_text()
        for key in config_file.KEYS:
            assert f"{key}:" in text, key

    def test_names_the_dictionary_relative_to_itself(
        self, session, tmp_path: Path, ref_path: Path, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        # So that the pair moves together, and the configuration works from any
        # directory it is later run in.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dicts").mkdir()
        local = tmp_path / "dicts" / "ref.json"
        local.write_bytes(ref_path.read_bytes())
        assert session(["dicts/ref.json", "-o", "Ref/RefProxy", "-i"]) == 0
        text = (tmp_path / "Ref" / "RefProxy" / "RefProxy.yaml").read_text()
        assert "dictionary: ../../dicts/ref.json" in text

    def test_keeps_an_absolute_dictionary_absolute(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "out"
        assert session([str(ref_path.resolve()), "-o", str(output), "-i"]) == 0
        assert (
            f"dictionary: {ref_path.resolve()}"
            in (output / "RefProxy.yaml").read_text()
        )

    def test_regenerates_the_same_component_it_just_generated(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        # The whole point: what -i writes is a faithful description of what -i did, so
        # iterating through it starts from exactly where the scaffold left off.
        output = tmp_path / "out"
        assert (
            session(
                [str(ref_path), "-o", str(output), "-i"], {"component kind": "queued"}
            )
            == 0
        )
        before = {
            path.name: path.read_text()
            for path in sorted(output.iterdir())
            if path.suffix != ".yaml"
        }
        assert main(["--config", str(output / "RefProxy.yaml"), "-q"]) == 0
        after = {
            path.name: path.read_text()
            for path in sorted(output.iterdir())
            if path.suffix != ".yaml"
        }
        assert after == before

    def test_is_reported_and_pointed_at(
        self, session, tmp_path: Path, ref_path: Path, capsys  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "out"
        session([str(ref_path), "-o", str(output), "-i"])
        printed = capsys.readouterr().out
        assert f"wrote     {output / 'RefProxy.yaml'}" in printed
        assert f"--config {output / 'RefProxy.yaml'}" in printed


class TestWritingOverAConfiguration:
    def test_is_refused(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "out"
        assert session([str(ref_path), "-o", str(output), "-i"]) == 0
        edited = output / "RefProxy.yaml"
        edited.write_text("# hand-written\nkind: passive\n", encoding="utf-8")

        assert session([str(ref_path), "-o", str(output), "-i"]) == 2
        assert edited.read_text() == "# hand-written\nkind: passive\n"
        assert "already exists" in session.transcript.text

    def test_says_how_to_iterate_instead(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "out"
        output.mkdir()
        (output / "RefProxy.yaml").write_text("kind: passive\n", encoding="utf-8")
        assert session([str(ref_path), "-o", str(output), "-i"]) == 2
        assert f"--config {output / 'RefProxy.yaml'}" in session.transcript.text


class TestWithAConfigFileToStartFrom:
    """``-i --config``: the file seeds the answers, and nothing it carries is lost."""

    def test_its_values_are_the_offered_defaults(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        seed = tmp_path / "seed.yaml"
        seed.write_text(
            f"dictionary: {ref_path}\ncomponent: FromFile\nkind: passive\n",
            encoding="utf-8",
        )
        output = tmp_path / "out"
        assert session(["--config", str(seed), "-i", "-o", str(output)]) == 0
        assert "component name [FromFile]" in session.transcript.text
        assert "[passive]" in session.transcript.text
        assert "passive component FromFile {" in (output / "FromFile.fpp").read_text()

    def test_an_override_table_is_carried_into_the_new_file(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        # It is in force for this run, so leaving it out would make the file that comes
        # out disagree with the component beside it.
        seed = tmp_path / "seed.yaml"
        seed.write_text(
            "names:\n  Ref.dpDemo.SelectColor: SelectDemoColor\n"
            "type-names:\n  Ref.DpDemo.ColorEnum: DemoColor\n",
            encoding="utf-8",
        )
        output = tmp_path / "out"
        assert (
            session([str(ref_path), "--config", str(seed), "-i", "-o", str(output)])
            == 0
        )
        written = (output / "RefProxy.yaml").read_text()
        assert "\nnames:\n  Ref.dpDemo.SelectColor: SelectDemoColor\n" in written
        assert "\ntype-names:\n  Ref.DpDemo.ColorEnum: DemoColor\n" in written
        assert "SelectDemoColor" in (output / "RefProxy.fpp").read_text()

        # And the file it wrote regenerates what it just generated.
        before = (output / "RefProxy.fpp").read_text()
        assert main(["--config", str(output / "RefProxy.yaml"), "-q"]) == 0
        assert (output / "RefProxy.fpp").read_text() == before


class TestTheDictionaryItself:
    def test_is_asked_for_when_it_was_omitted(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "out"
        assert session(["-i", "-o", str(output)], {"dictionary": str(ref_path)}) == 0
        assert "a proxy for Ref.Ref" in session.transcript.text

    def test_one_that_will_not_load_is_asked_for_again(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        assert (
            session(
                [str(tmp_path / "absent.json"), "-i", "-o", str(tmp_path / "out")],
                {"dictionary": str(ref_path)},
            )
            == 0
        )
        assert "cannot read" in session.transcript.text


class TestAborting:
    def test_end_of_input_writes_nothing(
        self, session, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        # A closed stdin: not even the first question can be answered.
        output = tmp_path / "out"
        assert session([str(ref_path), "-o", str(output), "-i"], eof=True) == 1
        assert not output.exists()
        assert "aborted" in session.transcript.text

    def test_a_dry_run_writes_neither_the_configuration_nor_the_component(
        self, session, tmp_path: Path, ref_path: Path, capsys  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "out"
        assert session([str(ref_path), "-o", str(output), "-i", "--dry-run"]) == 0
        assert not output.exists()
        assert "would write the configuration" in session.transcript.text
        assert "would write into" in capsys.readouterr().out


class TestPrompter:
    """The question primitives, whose edge cases are easier to state directly."""

    def _prompter(self, answers: str) -> tuple[interactive.Prompter, Transcript]:
        import io

        transcript = Transcript()
        return interactive.Prompter(io.StringIO(answers), transcript), transcript  # type: ignore[arg-type]

    def test_an_empty_answer_keeps_the_default(self) -> None:
        prompter, _ = self._prompter("\n")
        assert prompter.text("name", "RefProxy") == "RefProxy"

    def test_a_required_answer_is_asked_for_until_it_is_given(self) -> None:
        prompter, transcript = self._prompter("\nRefProxy\n")
        assert prompter.text("name") == "RefProxy"
        assert "an answer is needed here" in transcript.text

    def test_a_choice_is_matched_regardless_of_case(self) -> None:
        prompter, _ = self._prompter("PASSIVE\n")
        assert prompter.choice("kind", ["active", "passive"], "active") == "passive"

    def test_an_unanswerable_question_aborts(self) -> None:
        prompter, _ = self._prompter("")
        with pytest.raises(interactive.Aborted):
            prompter.text("name", "RefProxy")

    def test_a_path_expands_a_tilde(self) -> None:
        prompter, _ = self._prompter("~/out\n")
        assert prompter.path("output") == Path.home() / "out"
