"""Writing a component to disk, and the CLI that drives it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from fprime_dict_to_cpp.cli import build_parser, main
from fprime_dict_to_cpp.generate import generate
from fprime_dict_to_cpp.model import ComInput, ComponentKind
from fprime_dict_to_cpp.naming import Naming
from fprime_dict_to_cpp.options import Options
from fprime_dict_to_cpp.plan import Config

EXPECTED = {
    "CMakeLists.txt",
    "RefProxy.cpp",
    "RefProxy.fpp",
    "RefProxy.hpp",
    "RefProxyCommands.cpp",
    "RefProxyCommands.fppi",
    "RefProxyEvents.cpp",
    "RefProxyEvents.fppi",
    "RefProxyPackets.cpp",
    "RefProxyTelemetry.cpp",
    "RefProxyTelemetry.fppi",
    "RefProxyTypes.fppi",
}

#: The files CMakeLists.txt names.  The ``.fppi`` fragments are not among them: the build
#: tracks them as included files of ``RefProxy.fpp``, and naming one as an autocoder input
#: would have the autocoder process component members with no component around them.
IN_CMAKE = {name for name in EXPECTED if not name.endswith(".fppi")}


@pytest.fixture
def written(tmp_path: Path, ref_path: Path, config: Config):  # type: ignore[no-untyped-def]
    """A component generated into a temporary directory."""
    return generate(ref_path, tmp_path / "RefProxy", config)


class TestGenerate:
    def test_writes_the_expected_files(self, written) -> None:  # type: ignore[no-untyped-def]
        assert {path.name for path in written.files} == EXPECTED

    def test_reports_only_the_string_limit_notes_for_ref(self, written) -> None:  # type: ignore[no-untyped-def]
        # Ref needs no re-qualification and reuses no id; the only notes are about
        # declared string sizes that F Prime's defaults clamp.
        assert all("STRING_MAX_SIZE" in w for w in written.warnings)

    def test_regeneration_leaves_every_file_alone(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        # An unchanged mtime is what stops a no-op regeneration cascading a rebuild
        # through everything downstream of the component.
        output = tmp_path / "RefProxy"
        first = generate(ref_path, output, config)
        assert first.unchanged == []
        second = generate(ref_path, output, config)
        assert second.written == []
        assert {p.name for p in second.unchanged} == EXPECTED

    def test_changing_the_configuration_rewrites(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        from dataclasses import replace

        output = tmp_path / "RefProxy"
        generate(ref_path, output, config)
        again = generate(
            ref_path, output, replace(config, naming=Naming.preset("qualified"))
        )
        assert again.written

    def test_creates_the_output_directory(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        output = tmp_path / "deep" / "nested" / "RefProxy"
        generate(ref_path, output, config)
        assert output.is_dir()

    def test_cross_check_reports_agreement_silently(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        result = generate(ref_path, tmp_path / "out", config, cross_check=True)
        disagreements = [w for w in result.warnings if "fprime-gds" in w]
        assert all("not installed" in w for w in disagreements)


class TestCMakeListsIsKept:
    """Once it exists it belongs to the project, which is the one hand-edited case."""

    def test_a_hand_edited_one_survives_a_regeneration(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        output = tmp_path / "RefProxy"
        generate(ref_path, output, config)
        cmake = output / "CMakeLists.txt"
        cmake.write_text(
            cmake.read_text() + "\n# a dependency the model cannot imply\n"
        )
        edited = cmake.read_text()

        again = generate(ref_path, output, config)
        assert again.kept == [cmake]
        assert cmake.read_text() == edited
        assert cmake in again.files

    def test_cmake_rewrites_it(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        output = tmp_path / "RefProxy"
        generate(ref_path, output, config)
        cmake = output / "CMakeLists.txt"
        cmake.write_text("# mine\n")
        again = generate(ref_path, output, config, cmake=True)
        assert again.kept == []
        assert cmake in again.written
        assert "register_fprime_module(" in cmake.read_text()

    def test_one_that_already_agrees_is_simply_unchanged(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        # Not "kept": a plain regeneration of an untouched directory reads the way it
        # always did, and nothing downstream rebuilds.
        output = tmp_path / "RefProxy"
        generate(ref_path, output, config)
        again = generate(ref_path, output, config)
        assert again.kept == []
        assert output / "CMakeLists.txt" in again.unchanged

    def test_it_is_written_when_it_is_not_there(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        result = generate(ref_path, tmp_path / "RefProxy", config)
        assert result.directory / "CMakeLists.txt" in result.written


class TestEmptiedSections:
    """A section ``omit`` empties stops being generated, and its old file goes too."""

    def test_an_emptied_section_is_removed(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        # Left behind, the old decoder logs events the model no longer declares, so the
        # component stops compiling.
        output = tmp_path / "RefProxy"
        generate(ref_path, output, config)
        events = output / "RefProxyEvents.cpp"
        assert events.is_file()

        again = generate(ref_path, output, replace(config, omit_events=["*"]))
        assert not events.exists()
        assert again.removed == [events]

    def test_a_file_this_tool_did_not_write_is_left_alone(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        output = tmp_path / "RefProxy"
        output.mkdir()
        events = output / "RefProxyEvents.cpp"
        events.write_text("// mine\n")
        result = generate(ref_path, output, replace(config, omit_events=["*"]))
        assert events.read_text() == "// mine\n"
        assert result.removed == []

    def test_a_kept_cmake_naming_the_old_sources_is_reported(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        output = tmp_path / "RefProxy"
        generate(ref_path, output, config)
        again = generate(ref_path, output, replace(config, omit_events=["*"]))
        assert again.kept == [output / "CMakeLists.txt"]
        assert any("--cmake" in warning for warning in again.warnings)

    def test_a_kept_cmake_listing_the_same_sources_is_not(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        output = tmp_path / "RefProxy"
        generate(ref_path, output, config)
        cmake = output / "CMakeLists.txt"
        cmake.write_text(cmake.read_text() + "\n# a dependency\n")
        again = generate(ref_path, output, config)
        assert not any("--cmake" in warning for warning in again.warnings)


class TestIncludePrefix:
    """F Prime includes a component by its path below the root that encloses it."""

    def test_is_the_path_below_the_enclosing_library(self, tmp_path: Path) -> None:
        (tmp_path / "library.cmake").write_text("")
        output = tmp_path / "Components" / "Proxy"
        assert Options().include_prefix_for(output) == "Components/Proxy"

    def test_is_the_path_below_the_enclosing_project(self, tmp_path: Path) -> None:
        (tmp_path / "settings.ini").write_text("")
        output = tmp_path / "Ref" / "Proxy"
        assert Options().include_prefix_for(output) == "Ref/Proxy"

    def test_the_nearest_root_wins(self, tmp_path: Path) -> None:
        # A library checked out under a project is its own include root.
        (tmp_path / "settings.ini").write_text("")
        (tmp_path / "lib" / "mine").mkdir(parents=True)
        (tmp_path / "lib" / "mine" / "library.cmake").write_text("")
        output = tmp_path / "lib" / "mine" / "Fc" / "Proxy"
        assert Options().include_prefix_for(output) == "Fc/Proxy"

    def test_is_the_directory_name_outside_of_any_root(self, tmp_path: Path) -> None:
        assert Options().include_prefix_for(tmp_path / "Proxy") == "Proxy"

    def test_one_asked_for_wins(self, tmp_path: Path) -> None:
        (tmp_path / "library.cmake").write_text("")
        options = Options(include_prefix="Somewhere/Else")
        assert options.include_prefix_for(tmp_path / "Proxy") == "Somewhere/Else"


class TestCMakeLists:
    def test_lists_exactly_the_files_that_were_written(self, written) -> None:  # type: ignore[no-untyped-def]
        text = (written.directory / "CMakeLists.txt").read_text()
        for name in IN_CMAKE - {"CMakeLists.txt"}:
            assert name in text, name
        for name in EXPECTED - IN_CMAKE:
            assert name not in text, name

    def test_uses_the_current_register_fprime_module_keywords(self, written) -> None:  # type: ignore[no-untyped-def]
        text = (written.directory / "CMakeLists.txt").read_text()
        assert "register_fprime_module(" in text
        for keyword in ("AUTOCODER_INPUTS", "SOURCES", "HEADERS"):
            assert f"  {keyword}\n" in text

    def test_lists_only_the_model_file_as_an_autocoder_input(self, written) -> None:  # type: ignore[no-untyped-def]
        text = (written.directory / "CMakeLists.txt").read_text()
        inputs = text.split("AUTOCODER_INPUTS")[1].split("SOURCES")[0]
        assert "RefProxy.fpp" in inputs
        assert ".fppi" not in inputs

    def test_does_not_restate_dependencies(self, written) -> None:  # type: ignore[no-untyped-def]
        # F Prime derives them from the FPP model; naming them again is how a generated
        # file goes stale.
        text = (written.directory / "CMakeLists.txt").read_text()
        assert "DEPENDS" not in text


class TestCli:
    def test_dry_run_writes_nothing(
        self, tmp_path: Path, ref_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        output = tmp_path / "RefProxy"
        assert main([str(ref_path), "-o", str(output), "--dry-run"]) == 0
        assert not output.exists()
        assert "would write into" in capsys.readouterr().out

    def test_generates_with_defaults(
        self, tmp_path: Path, ref_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        output = tmp_path / "RefProxy"
        assert main([str(ref_path), "-o", str(output)]) == 0
        assert {p.name for p in output.iterdir()} == EXPECTED
        assert "116 commands" in capsys.readouterr().out

    def test_component_name_defaults_to_the_deployment(
        self, tmp_path: Path, ref_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([str(ref_path), "-o", str(tmp_path / "out"), "--dry-run"])
        assert "RefProxy" in capsys.readouterr().out

    def test_kind_and_naming_reach_the_output(
        self, tmp_path: Path, ref_path: Path
    ) -> None:
        output = tmp_path / "P"
        assert (
            main(
                [
                    str(ref_path),
                    "-o",
                    str(output),
                    "-c",
                    "P",
                    "--kind",
                    "passive",
                    "--naming",
                    "qualified",
                ]
            )
            == 0
        )
        assert "passive component P {" in (output / "P.fpp").read_text()
        assert "Ref_dpDemo_SelectColor" in (output / "PCommands.fppi").read_text()

    def test_naming_knobs_override_the_preset(
        self, tmp_path: Path, ref_path: Path
    ) -> None:
        output = tmp_path / "P"
        main(
            [
                str(ref_path),
                "-o",
                str(output),
                "-c",
                "P",
                "--naming",
                "instance",
                "--name-qualification",
                "full",
            ]
        )
        assert "Ref_dpDemo_SelectColor" in (output / "PCommands.fppi").read_text()

    def test_no_diagnostics_and_no_packetized(
        self, tmp_path: Path, ref_path: Path
    ) -> None:
        output = tmp_path / "P"
        main(
            [
                str(ref_path),
                "-o",
                str(output),
                "-c",
                "P",
                "--no-diagnostics",
                "--no-packetized",
            ]
        )
        events = (output / "PEvents.fppi").read_text()
        assert "ProxyDecodeError" not in events
        assert not (output / "PPackets.cpp").exists()

    def test_no_channelized(self, tmp_path: Path, ref_path: Path) -> None:
        output = tmp_path / "P"
        main([str(ref_path), "-o", str(output), "-c", "P", "--no-channelized"])
        assert not (output / "PTelemetry.cpp").exists()
        assert (output / "PPackets.cpp").exists()

    def test_type_prefix_reaches_the_types(
        self, tmp_path: Path, ref_path: Path
    ) -> None:
        output = tmp_path / "P"
        main([str(ref_path), "-o", str(output), "-c", "P", "--type-prefix", "Rp_"])
        assert "Rp_Ref_DpDemo_ColorEnum" in (output / "PTypes.fppi").read_text()

    def test_missing_dictionary_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([str(tmp_path / "absent.json")]) == 2
        assert "error:" in capsys.readouterr().err

    def test_malformed_dictionary_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{}")
        assert main([str(path)]) == 2
        assert "metadata" in capsys.readouterr().err

    def test_quiet_suppresses_the_summary(
        self, tmp_path: Path, ref_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([str(ref_path), "-o", str(tmp_path / "out"), "-q"])
        assert capsys.readouterr().out == ""

    def test_parser_exposes_every_kind_and_preset(self) -> None:
        from fprime_dict_to_cpp.naming import PRESETS

        text = build_parser().format_help()
        for kind in ComponentKind:
            assert kind.value in text
        for preset in PRESETS:
            assert preset in text


class TestComIn:
    """``com-in``: the port type the remote's packets arrive on."""

    def test_is_fw_com_by_default(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        result = generate(ref_path, tmp_path / "RefProxy", config)
        model = (result.directory / "RefProxy.fpp").read_text()
        assert "comIn: Fw.Com" in model
        assert "comReturnOut" not in model

    def test_buffer_takes_and_hands_back_a_buffer(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        result = generate(
            ref_path, tmp_path / "RefProxy", replace(config, com_in=ComInput.BUFFER)
        )
        model = (result.directory / "RefProxy.fpp").read_text()
        assert "async input port comIn: Fw.BufferSend" in model
        assert "output port comReturnOut: Fw.BufferSend" in model
        assert "output port comOut: Fw.Com" in model

        source = (result.directory / "RefProxy.cpp").read_text()
        handler = source[source.index("comIn_handler(") :]
        # Returned before decoding starts, so the sender gets it back however long that is.
        assert handler.index("comReturnOut_out(0, buffer)") < handler.index(
            "deserializeTo(_descriptor)"
        )

    def test_a_hooked_port_may_not_take_comReturnOut(
        self, ref_path: Path, config: Config
    ) -> None:
        from fprime_dict_to_cpp import dictionary
        from fprime_dict_to_cpp.model import NamingError
        from fprime_dict_to_cpp.plan import resolve

        ref = dictionary.load(ref_path)
        with pytest.raises(NamingError, match="comReturnOut"):
            resolve(
                ref,
                replace(config, command_ports={ref.commands[0].name: "comReturnOut"}),
            )

    def test_the_option_reaches_the_config(self) -> None:
        parsed = build_parser().parse_args(["d.json", "--com-in", "buffer"])
        assert Options.from_namespace(parsed).settle("Ref")[1].com_in is ComInput.BUFFER
