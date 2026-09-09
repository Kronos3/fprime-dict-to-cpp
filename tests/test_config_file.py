"""Reading the configuration out of a YAML file, and how it ranks against the flags."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fprime_dict_to_cpp import config_file
from fprime_dict_to_cpp.cli import build_parser, main
from fprime_dict_to_cpp.model import QualifiedName


@pytest.fixture
def write(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Write a configuration file and hand back its path."""

    def write(body: str, name: str = "proxy.yaml") -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    return write


class TestLoad:
    def test_maps_each_key_to_its_argparse_destination(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        path = write(
            "component: P\n"
            "namespace: Proxies\n"
            "kind: passive\n"
            "naming: qualified\n"
            "name-qualification: full\n"
            "name-style: underscore\n"
            "type-prefix: Rp_\n"
            "include-prefix: Ref/P\n"
            "cross-check: true\n"
            "dry-run: true\n"
            "quiet: true\n"
        )
        assert config_file.load(path) == {
            "component": "P",
            "namespace": "Proxies",
            "kind": "passive",
            "naming": "qualified",
            "name_qualification": "full",
            "name_style": "underscore",
            "type_prefix": "Rp_",
            "include_prefix": "Ref/P",
            "cross_check": True,
            "dry_run": True,
            "quiet": True,
        }

    def test_every_key_reaches_a_destination_the_command_line_reads(self) -> None:
        # A key whose destination nothing downstream reads would be accepted and then
        # ignored, which is the one failure this file cannot report itself.  ``names``
        # and ``type-names`` have no flag of their own, so this asks the namespace
        # rather than the arguments.
        namespace = vars(build_parser().parse_args([]))
        for key, option in config_file.KEYS.items():
            assert option.dest in namespace, key

    def test_the_positive_keys_stand_in_for_the_negative_flags(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        path = write("packetized: false\ndiagnostics: false\n")
        assert config_file.load(path) == {
            "no_packetized": True,
            "no_diagnostics": True,
        }
        path = write("packetized: true\ndiagnostics: true\n")
        assert config_file.load(path) == {
            "no_packetized": False,
            "no_diagnostics": False,
        }

    def test_underscores_are_accepted_wherever_hyphens_are(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        assert config_file.load(write("type_prefix: Rp_\n")) == {"type_prefix": "Rp_"}

    def test_clang_format_is_a_style_or_a_bare_yes(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        assert config_file.load(write("clang-format: Google\n")) == {
            "clang_format": "Google"
        }
        assert config_file.load(write("clang-format: true\n")) == {"clang_format": True}
        assert config_file.load(write("clang-format: false\n")) == {
            "clang_format": False
        }

    def test_an_empty_file_configures_nothing(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        assert config_file.load(write("")) == {}
        assert config_file.load(write("# nothing but a comment\n")) == {}

    def test_an_empty_type_prefix_is_a_value_and_not_an_omission(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        assert config_file.load(write('type-prefix: ""\n')) == {"type_prefix": ""}


class TestOverrideTables:
    def test_names_are_keyed_the_way_a_member_is_keyed(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        path = write(
            "names:\n"
            "  Ref.dpDemo.SelectColor: SelectDemoColor\n"
            "  CdhCore.cmdDisp.CMD_NO_OP: NoOp\n"
        )
        assert config_file.load(path) == {
            "name_overrides": {
                QualifiedName.parse("Ref.dpDemo.SelectColor"): "SelectDemoColor",
                QualifiedName.parse("CdhCore.cmdDisp.CMD_NO_OP"): "NoOp",
            }
        }

    def test_type_names_stay_qualified_strings(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        path = write("type-names:\n  Ref.DpDemo.ColorEnum: DemoColor\n")
        assert config_file.load(path) == {
            "type_name_overrides": {"Ref.DpDemo.ColorEnum": "DemoColor"}
        }

    def test_surrounding_space_is_not_part_of_either_name(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        path = write('type-names:\n  " A.B ": " C "\n')
        assert config_file.load(path) == {"type_name_overrides": {"A.B": "C"}}

    def test_an_empty_table_is_accepted(self, write) -> None:  # type: ignore[no-untyped-def]
        assert config_file.load(write("names: {}\n")) == {"name_overrides": {}}

    def test_a_table_that_is_not_a_mapping(self, write) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(config_file.ConfigError) as raised:
            config_file.load(write("names:\n  - Ref.dpDemo.SelectColor\n"))
        assert "a mapping of dictionary names to generated names" in str(raised.value)

    def test_an_entry_with_no_name_says_which_entry(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        with pytest.raises(config_file.ConfigError) as raised:
            config_file.load(write("names:\n  Ref.dpDemo.SelectColor:\n"))
        message = str(raised.value)
        assert "names: Ref.dpDemo.SelectColor: expected a name" in message

    def test_an_entry_whose_name_is_not_a_string(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        with pytest.raises(config_file.ConfigError) as raised:
            config_file.load(write("names:\n  Ref.dpDemo.SelectColor: 42\n"))
        assert "expected a name, found 42" in str(raised.value)

    def test_a_repeated_entry_is_an_error_rather_than_the_last_one_winning(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        # YAML says the last one wins, which here would silently drop an override.
        with pytest.raises(config_file.ConfigError) as raised:
            config_file.load(
                write(
                    "names:\n"
                    "  Ref.dpDemo.SelectColor: One\n"
                    "  Ref.dpDemo.SelectColor: Two\n"
                )
            )
        message = str(raised.value)
        assert "duplicate key 'Ref.dpDemo.SelectColor'" in message
        assert "line 3" in message

    def test_a_repeated_option_is_an_error_too(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        with pytest.raises(config_file.ConfigError) as raised:
            config_file.load(write("kind: passive\nkind: queued\n"))
        assert "duplicate key 'kind'" in str(raised.value)

    def test_whether_a_name_is_usable_is_left_to_the_planner(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        # The file layer only checks shape; resolve() owns what a legal generated name
        # is, so that the Python API is held to the same rule.
        path = write("names:\n  Ref.dpDemo.SelectColor: not an identifier\n")
        assert config_file.load(path)["name_overrides"] == {
            QualifiedName.parse("Ref.dpDemo.SelectColor"): "not an identifier"
        }


class TestPaths:
    def test_a_relative_path_resolves_against_the_file(
        self, write, tmp_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        path = write("dictionary: dicts/ref.json\noutput: out/P\n", "sub/proxy.yaml")
        loaded = config_file.load(path)
        assert loaded["dictionary"] == tmp_path / "sub" / "dicts" / "ref.json"
        assert loaded["output"] == tmp_path / "sub" / "out" / "P"

    def test_a_relative_path_that_climbs_out_is_collapsed(
        self, write, tmp_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        path = write("output: ../build/P\n", "sub/proxy.yaml")
        assert config_file.load(path)["output"] == tmp_path / "build" / "P"

    def test_an_absolute_path_is_left_alone(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        path = write("dictionary: /dicts/ref.json\n")
        assert config_file.load(path)["dictionary"] == Path("/dicts/ref.json")

    def test_a_tilde_is_expanded(self, write) -> None:  # type: ignore[no-untyped-def]
        path = write("output: ~/proxies/P\n")
        assert config_file.load(path)["output"] == Path.home() / "proxies" / "P"

    def test_an_include_prefix_is_not_a_filesystem_path(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        # It goes into an #include, so it stays exactly as written even though it looks
        # like a relative path.
        path = write("include-prefix: Ref/RefProxy\n")
        assert config_file.load(path)["include_prefix"] == "Ref/RefProxy"


class TestRejects:
    def _error(self, path: Path) -> str:
        with pytest.raises(config_file.ConfigError) as raised:
            config_file.load(path)
        return str(raised.value)

    def test_an_unknown_key_by_listing_the_known_ones(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        message = self._error(write("componnet: P\n"))
        assert "unknown option 'componnet'" in message
        assert "component" in message

    def test_a_value_outside_a_choice_by_naming_the_choices(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        message = self._error(write("kind: synchronous\n"))
        assert "expected one of passive, queued, active" in message
        for bad in ("naming: instanced\n", "name-style: camel\n"):
            assert "expected one of" in self._error(write(bad))

    def test_a_flag_that_is_not_true_or_false(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        assert "expected true or false" in self._error(write("quiet: sometimes\n"))
        assert "expected true or false" in self._error(write("packetized: 1\n"))

    def test_a_key_with_no_value(self, write) -> None:  # type: ignore[no-untyped-def]
        assert "gives no value" in self._error(write("output:\n"))

    def test_an_empty_name(self, write) -> None:  # type: ignore[no-untyped-def]
        assert "expected a name" in self._error(write('component: "  "\n'))

    def test_a_document_that_is_not_a_mapping(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        assert "found a list" in self._error(write("- component: P\n"))
        assert "found a string" in self._error(write("component\n"))

    def test_malformed_yaml_keeping_the_parser_s_own_line_numbers(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        message = self._error(write("kind: [passive\n"))
        assert "line 1" in message
        assert "proxy.yaml" in message

    def test_a_file_that_is_not_there_as_an_os_error(self, tmp_path: Path) -> None:
        # Which is what the command line already reports for a missing dictionary.
        with pytest.raises(OSError):
            config_file.load(tmp_path / "absent.yaml")

    def test_pyyaml_not_being_installed_with_a_way_out(
        self, write, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
    ) -> None:
        monkeypatch.setitem(sys.modules, "yaml", None)
        assert "needs PyYAML" in self._error(write("kind: passive\n"))


class TestSkeleton:
    """The file ``-i`` leaves behind: the answers, and every other key commented out."""

    def test_sets_what_it_was_given(self) -> None:
        text = config_file.skeleton({"component": "P", "kind": "passive"})
        assert "\ncomponent: P\n" in text
        assert "\nkind: passive\n" in text

    def test_comments_out_everything_else_at_its_default(self) -> None:
        text = config_file.skeleton({"component": "P"})
        assert "\n# kind: active\n" in text
        assert "\n# packetized: true\n" in text
        assert "\n# quiet: false\n" in text
        assert '\n# type-prefix: ""\n' in text

    def test_mentions_every_key_with_a_line_about_it(self) -> None:
        text = config_file.skeleton({"component": "P"})
        for key, option in config_file.KEYS.items():
            assert f"{key}:" in text, key
            # Wrapped, so only the first few words are one contiguous string.
            assert " ".join(option.note.split()[:4]) in text, key

    def test_shows_an_override_table_s_shape_rather_than_a_default(self) -> None:
        text = config_file.skeleton({"component": "P"})
        assert "\n# names:\n#   Ref.dpDemo.SelectColor: SelectDemoColor\n" in text
        assert "\n# type-names:\n" in text

    def test_shows_the_choices_for_a_knob_that_overrides_a_preset(self) -> None:
        # Its default follows whichever preset is in force, so a value written here
        # would go stale the moment the preset above it changed.
        text = config_file.skeleton({"component": "P"})
        assert "# name-qualification: full  # or member, or instance" in text
        assert "# name-style: source-case  # or underscore" in text

    def test_follows_the_component_where_a_default_does(self) -> None:
        assert "\n# namespace: P\n" in config_file.skeleton({"component": "P"})

    def test_a_caller_s_default_is_what_is_shown(self) -> None:
        text = config_file.skeleton(
            {"component": "P"}, defaults={"include-prefix": "Ref/P"}
        )
        assert "\n# include-prefix: Ref/P\n" in text

    def test_writes_a_table_it_was_given(self) -> None:
        text = config_file.skeleton(
            {"type-names": {"Ref.DpDemo.ColorEnum": "DemoColor"}}
        )
        assert "\ntype-names:\n  Ref.DpDemo.ColorEnum: DemoColor\n" in text

    def test_the_header_is_comments(self) -> None:
        text = config_file.skeleton({"component": "P"}, header=["hello", "", "world"])
        assert text.startswith("# hello\n#\n# world\n")

    def test_quotes_only_what_yaml_would_read_as_something_else(self) -> None:
        text = config_file.skeleton(
            {"component": "P", "type-prefix": "", "include-prefix": "no"}
        )
        assert "component: P\n" in text
        assert 'type-prefix: ""\n' in text
        assert 'include-prefix: "no"\n' in text  # else YAML reads it as false

    def test_what_it_writes_is_what_load_reads_back(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        chosen = {
            "dictionary": Path("../ref.json"),
            "output": Path("."),
            "component": "P",
            "kind": "passive",
            "type-prefix": "Rp_",
            "names": {"Ref.dpDemo.SelectColor": "SelectDemoColor"},
        }
        path = write(config_file.skeleton(chosen))
        loaded = config_file.load(path)
        assert loaded["component"] == "P"
        assert loaded["kind"] == "passive"
        assert loaded["type_prefix"] == "Rp_"
        assert loaded["output"] == path.parent
        assert loaded["dictionary"] == path.parent.parent / "ref.json"
        assert loaded["name_overrides"] == {
            QualifiedName.parse("Ref.dpDemo.SelectColor"): "SelectDemoColor"
        }

    def test_the_commented_keys_read_back_as_the_defaults_they_claim(
        self, write  # type: ignore[no-untyped-def]
    ) -> None:
        # Uncommenting a key must change nothing until its value is changed, or the
        # comment was a lie.
        text = config_file.skeleton({"component": "P"})
        live = "\n".join(
            line[2:] if _is_commented_key(line) else line for line in text.splitlines()
        )
        loaded = config_file.load(write(live))
        parser = build_parser().parse_args([])
        for dest, value in loaded.items():
            if dest in (
                "component",
                "namespace",
                "include_prefix",
                "name_style",
                "name_qualification",
                "name_overrides",
                "type_name_overrides",
            ):
                continue  # no built-in default, or shown as choices rather than a value
            assert getattr(parser, dest) == value, dest


def _is_commented_key(line: str) -> bool:
    """Whether ``line`` is a commented-out ``key: value`` rather than prose.

    A bare ``# dictionary:`` is a placeholder for something with no default, so it is
    not one of the claims this is checking.
    """
    if not line.startswith("# ") or ":" not in line:
        return False
    key, _, value = line[2:].partition(":")
    return key in config_file.KEYS and bool(value.strip())


class TestTheShippedExample:
    """``examples/proxy.yaml`` documents the format, so it has to stay loadable."""

    EXAMPLE = Path(__file__).parent.parent / "examples" / "proxy.yaml"

    def test_loads(self) -> None:
        loaded = config_file.load(self.EXAMPLE)
        assert loaded["component"] == "RefProxy"
        assert loaded["dictionary"].is_file()

    def test_generates(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Not just loadable: its override tables name real members of the Ref
        # dictionary, and resolve() would refuse the whole run if one had gone stale.
        assert main(["--config", str(self.EXAMPLE), "--dry-run"]) == 0
        assert "named explicitly" in capsys.readouterr().out

    def test_mentions_every_key(self) -> None:
        # Including the ones it leaves commented out: an option the example never names
        # is an option nobody discovers.
        text = self.EXAMPLE.read_text(encoding="utf-8")
        for key in config_file.KEYS:
            assert key in text, key


class TestCli:
    def test_takes_its_whole_configuration_from_the_file(
        self, write, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "P"
        path = write(
            f"dictionary: {ref_path}\n"
            f"output: {output}\n"
            "component: P\n"
            "kind: passive\n"
            "naming: qualified\n"
            "quiet: true\n"
        )
        assert main(["--config", str(path)]) == 0
        model = (output / "P.fpp").read_text()
        assert "passive component P {" in model
        assert "Ref_dpDemo_SelectColor" in model

    def test_a_flag_beats_the_file(
        self, write, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        path = write(
            f"dictionary: {ref_path}\ncomponent: FromFile\nkind: passive\nquiet: true\n"
        )
        output = tmp_path / "out"
        assert (
            main(
                [
                    "--config",
                    str(path),
                    "-o",
                    str(output),
                    "-c",
                    "P",
                    "--kind",
                    "queued",
                ]
            )
            == 0
        )
        assert "queued component P {" in (output / "P.fpp").read_text()

    def test_the_file_beats_the_built_in_default(
        self, write, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        path = write("kind: passive\n")
        output = tmp_path / "out"
        assert (
            main([str(ref_path), "--config", str(path), "-o", str(output), "-q"]) == 0
        )
        assert "passive component" in (output / "RefProxy.fpp").read_text()

    def test_a_dictionary_on_the_command_line_beats_the_file(
        self, write, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        path = write("dictionary: /absent/ref.json\nquiet: true\n")
        output = tmp_path / "out"
        assert (
            main([str(ref_path), "--config", str(path), "-o", str(output), "--dry-run"])
            == 0
        )

    def test_produces_what_the_equivalent_flags_produce(
        self, write, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        configured = tmp_path / "configured" / "P"
        path = write(
            f"dictionary: {ref_path}\n"
            f"output: {configured}\n"
            "component: P\n"
            "kind: queued\n"
            "naming: source-case\n"
            "type-prefix: Rp_\n"
            "diagnostics: false\n"
            "packetized: false\n"
            "quiet: true\n"
        )
        assert main(["--config", str(path)]) == 0
        told = tmp_path / "told" / "P"
        assert (
            main(
                [
                    str(ref_path),
                    "-o",
                    str(told),
                    "-c",
                    "P",
                    "--kind",
                    "queued",
                    "--naming",
                    "source-case",
                    "--type-prefix",
                    "Rp_",
                    "--no-diagnostics",
                    "--no-packetized",
                    "-q",
                ]
            )
            == 0
        )
        assert {p.name for p in configured.iterdir()} == {
            p.name for p in told.iterdir()
        }
        for path in sorted(told.iterdir()):
            assert (configured / path.name).read_text() == path.read_text()

    def test_overrides_reach_the_generated_component(
        self, write, tmp_path: Path, ref_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        output = tmp_path / "out"
        path = write(
            f"dictionary: {ref_path}\n"
            f"output: {output}\n"
            "quiet: true\n"
            "names:\n"
            "  Ref.dpDemo.SelectColor: SelectDemoColor\n"
            "type-names:\n"
            "  Ref.DpDemo.ColorEnum: DemoColor\n"
        )
        assert main(["--config", str(path)]) == 0
        model = (output / "RefProxy.fpp").read_text()
        assert "async command SelectDemoColor(" in model
        assert "dpDemo_SelectColor" not in model
        assert "enum DemoColor" in (output / "RefProxyTypes.fpp").read_text()
        assert "Ref_DpDemo_ColorEnum" not in model
        # The C++ has to agree with the model, or the base class will not be overridden.
        assert (
            "SelectDemoColor_cmdHandler"
            in (output / "RefProxyCommands.cpp").read_text()
        )

    def test_an_override_that_cannot_be_honoured_names_the_file(
        self, write, tmp_path: Path, ref_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        output = tmp_path / "out"
        path = write("names:\n  Ref.dpDemo.SelectColour: X\n")
        assert main([str(ref_path), "--config", str(path), "-o", str(output)]) == 2
        assert not output.exists()
        error = capsys.readouterr().err
        assert "proxy.yaml: name override for Ref.dpDemo.SelectColour" in error
        assert "did you mean Ref.dpDemo.SelectColor?" in error

    def test_an_override_is_checked_before_a_dry_run_reports(
        self, write, tmp_path: Path, ref_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write("names:\n  Ref.dpDemo.SelectColor: comIn\n")
        assert main([str(ref_path), "--config", str(path), "--dry-run"]) == 2
        captured = capsys.readouterr()
        assert "would write into" not in captured.out
        assert "the proxy declares itself" in captured.err

    def test_the_summary_counts_the_names_that_were_chosen(
        self, write, tmp_path: Path, ref_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write(
            f"dictionary: {ref_path}\n"
            f"output: {tmp_path / 'out'}\n"
            "names:\n"
            "  Ref.dpDemo.SelectColor: SelectDemoColor\n"
            "type-names:\n"
            "  Ref.DpDemo.ColorEnum: DemoColor\n"
        )
        assert main(["--config", str(path)]) == 0
        assert (
            "named explicitly: 1 member name(s), 1 type name(s)"
            in capsys.readouterr().out
        )

    def test_a_bad_file_is_an_error_before_anything_is_written(
        self, write, tmp_path: Path, ref_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        output = tmp_path / "out"
        path = write("kind: synchronous\n")
        assert main([str(ref_path), "--config", str(path), "-o", str(output)]) == 2
        assert not output.exists()
        assert "error:" in capsys.readouterr().err

    def test_a_missing_file_is_an_error(
        self, tmp_path: Path, ref_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        absent = tmp_path / "absent.yaml"
        assert main([str(ref_path), "--config", str(absent)]) == 2
        assert "absent.yaml" in capsys.readouterr().err

    def test_the_help_mentions_the_flag(self) -> None:
        assert "--config" in build_parser().format_help()
