"""Building a project's own ``.fppi``, headers and source into the generated component."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from fprime_dict_to_cpp import config_file
from fprime_dict_to_cpp.cli import main
from fprime_dict_to_cpp.cmake import render_cmake
from fprime_dict_to_cpp.cpp import render_implementation
from fprime_dict_to_cpp.fpp import render_fpp
from fprime_dict_to_cpp.generate import CustomFileError, generate
from fprime_dict_to_cpp.model import Dictionary
from fprime_dict_to_cpp.options import Options
from fprime_dict_to_cpp.plan import Config, resolve

CUSTOM = {
    "custom_fpp": "RefProxyCustom.fppi",
    "custom_public_hpp": "RefProxyPublic.hpp",
    "custom_private_hpp": "RefProxyPrivate.hpp",
    "custom_cpp": "RefProxyCustom.cpp",
}


@pytest.fixture
def custom(config: Config) -> Config:
    return replace(
        config,
        custom_fpp=CUSTOM["custom_fpp"],
        custom_public_hpp=CUSTOM["custom_public_hpp"],
        custom_private_hpp=CUSTOM["custom_private_hpp"],
        custom_cpp=CUSTOM["custom_cpp"],
    )


def _header(ref: Dictionary, config: Config) -> str:
    rendered = render_implementation(resolve(ref, config)).files()
    return next(text for name, text in rendered.items() if name.endswith(".hpp"))


def _write_custom(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in CUSTOM.values():
        (directory / name).write_text(f"// {name}\n")


class TestModel:
    def test_is_included_after_every_generated_member(
        self, ref: Dictionary, custom: Config
    ) -> None:
        model = render_fpp(resolve(ref, custom))["RefProxy.fpp"]
        includes = [line.strip() for line in model.splitlines() if "include" in line]
        assert includes[-1] == 'include "RefProxyCustom.fppi"'
        assert len(includes) == 5

    def test_is_spelled_relative_to_the_component(
        self, ref: Dictionary, config: Config
    ) -> None:
        plan = resolve(ref, replace(config, custom_fpp="../Shared/Extra.fppi"))
        assert 'include "../Shared/Extra.fppi"' in render_fpp(plan)["RefProxy.fpp"]

    def test_is_absent_unless_asked_for(self, ref: Dictionary, config: Config) -> None:
        model = render_fpp(resolve(ref, config))["RefProxy.fpp"]
        assert "Custom members" not in model

    def test_is_not_itself_rendered(self, ref: Dictionary, custom: Config) -> None:
        assert "RefProxyCustom.fppi" not in render_fpp(resolve(ref, custom))


class TestImplementation:
    @pytest.mark.parametrize(
        ("name", "access", "other"),
        [
            ("RefProxyPublic.hpp", "public:", "private:"),
            ("RefProxyPrivate.hpp", "private:", "public:"),
        ],
    )
    def test_each_header_is_included_inside_the_class_with_its_access(
        self, ref: Dictionary, custom: Config, name: str, access: str, other: str
    ) -> None:
        header = _header(ref, custom)
        include = header.index(f'#include "{name}"')
        assert header.rindex(access, 0, include) > header.rindex(other, 0, include)
        # Inside the class: its closing brace comes after the include.
        assert header.rindex("class RefProxy final", 0, include) >= 0
        assert header.index("};", include) < header.index("#endif")

    def test_public_comes_before_private(self, ref: Dictionary, custom: Config) -> None:
        header = _header(ref, custom)
        assert header.index("RefProxyPublic.hpp") < header.index("RefProxyPrivate.hpp")

    def test_either_may_be_given_alone(self, ref: Dictionary, config: Config) -> None:
        header = _header(ref, replace(config, custom_public_hpp="Mine.hpp"))
        assert "Custom public members" in header
        assert "Custom private members" not in header

    def test_is_absent_unless_asked_for(self, ref: Dictionary, config: Config) -> None:
        header = _header(ref, config)
        assert "Custom public members" not in header
        assert "Custom private members" not in header


class TestCMakeLists:
    def test_lists_the_source_and_headers_but_not_the_fragment(
        self, ref: Dictionary, custom: Config
    ) -> None:
        cmake = render_cmake(resolve(ref, custom))
        sources = cmake.split("SOURCES")[1].split("HEADERS")[0]
        headers = cmake.split("HEADERS")[1]
        assert '"${CMAKE_CURRENT_LIST_DIR}/RefProxyCustom.cpp"' in sources
        assert '"${CMAKE_CURRENT_LIST_DIR}/RefProxyPublic.hpp"' in headers
        assert '"${CMAKE_CURRENT_LIST_DIR}/RefProxyPrivate.hpp"' in headers
        inputs = cmake.split("AUTOCODER_INPUTS")[1].split("SOURCES")[0]
        assert "Custom" not in inputs

    def test_any_one_may_be_given_alone(self, ref: Dictionary, config: Config) -> None:
        cmake = render_cmake(resolve(ref, replace(config, custom_fpp="Only.fppi")))
        assert "Only.fppi" in cmake.split("####")[1]  # named in the banner only
        assert "Only" not in cmake.split("register_fprime_module")[1]


class TestGenerate:
    def test_builds_the_files_in_and_leaves_them_alone(
        self, tmp_path: Path, ref_path: Path, custom: Config
    ) -> None:
        output = tmp_path / "RefProxy"
        _write_custom(output)
        result = generate(ref_path, output, custom)
        for name in CUSTOM.values():
            assert (output / name).read_text() == f"// {name}\n"
            assert output / name not in result.files

    def test_a_missing_file_is_an_error_before_anything_is_written(
        self, tmp_path: Path, ref_path: Path, custom: Config
    ) -> None:
        output = tmp_path / "RefProxy"
        with pytest.raises(CustomFileError, match="custom-fpp: .*does not exist"):
            generate(ref_path, output, custom)
        assert not output.exists()

    @pytest.mark.parametrize(
        "name", ["RefProxyEvents.fppi", "RefProxy.hpp", "RefProxyPackets.cpp"]
    )
    def test_one_of_the_generated_names_is_refused(
        self, tmp_path: Path, ref_path: Path, config: Config, name: str
    ) -> None:
        output = tmp_path / "RefProxy"
        output.mkdir()
        (output / name).write_text("mine\n")
        key = {
            ".fppi": "custom_fpp",
            ".hpp": "custom_private_hpp",
            ".cpp": "custom_cpp",
        }
        chosen = replace(config, **{key[Path(name).suffix]: name})  # type: ignore[arg-type]
        with pytest.raises(CustomFileError, match="would be overwritten"):
            generate(ref_path, output, chosen)
        assert (output / name).read_text() == "mine\n"

    def test_a_name_whose_section_is_empty_today_is_refused_too(
        self, tmp_path: Path, ref_path: Path, config: Config
    ) -> None:
        # Nothing is generated into RefProxyCommands.cpp once every command is omitted,
        # but un-omitting one would overwrite it, so it is not a name to build on.
        output = tmp_path / "RefProxy"
        output.mkdir()
        (output / "RefProxyCommands.cpp").write_text("mine\n")
        chosen = replace(
            config, omit_commands=("*",), custom_cpp="RefProxyCommands.cpp"
        )
        with pytest.raises(CustomFileError, match="would be overwritten"):
            generate(ref_path, output, chosen)


class TestOptions:
    def test_are_spelled_relative_to_the_output_directory(self, tmp_path: Path) -> None:
        options = Options(
            output=tmp_path / "Ref" / "RefProxy",
            custom_fpp=tmp_path / "Ref" / "RefProxy" / "Mine.fppi",
            custom_public_hpp=tmp_path / "Ref" / "Shared" / "Mine.hpp",
        )
        _, config = options.settle("Ref")
        assert config.custom_fpp == "Mine.fppi"
        assert config.custom_public_hpp == "../Shared/Mine.hpp"
        assert config.custom_private_hpp == ""
        assert config.custom_cpp == ""

    def test_resolve_against_the_configuration_file(self, tmp_path: Path) -> None:
        path = tmp_path / "sub" / "proxy.yaml"
        path.parent.mkdir()
        path.write_text("custom-fpp: Mine.fppi\ncustom_cpp: ../src/Mine.cpp\n")
        loaded = config_file.load(path)
        assert loaded["custom_fpp"] == tmp_path / "sub" / "Mine.fppi"
        assert loaded["custom_cpp"] == tmp_path / "src" / "Mine.cpp"

    def test_the_skeleton_lists_them_commented_out(self) -> None:
        skeleton = config_file.skeleton({"component": "P"})
        for key in (
            "custom-fpp",
            "custom-public-hpp",
            "custom-private-hpp",
            "custom-cpp",
        ):
            assert f"# {key}: " in skeleton


class TestCli:
    def test_builds_them_in_from_a_configuration_file(
        self,
        tmp_path: Path,
        ref_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        output = tmp_path / "RefProxy"
        _write_custom(output)
        path = tmp_path / "proxy.yaml"
        path.write_text(
            f"dictionary: {ref_path}\n"
            "output: RefProxy\n"
            "custom-fpp: RefProxy/RefProxyCustom.fppi\n"
            "custom-public-hpp: RefProxy/RefProxyPublic.hpp\n"
            "custom-private-hpp: RefProxy/RefProxyPrivate.hpp\n"
            "custom-cpp: RefProxy/RefProxyCustom.cpp\n"
            "clang-format: false\n"
        )
        assert main(["--config", str(path)]) == 0
        assert "built with your own: RefProxyCustom.fppi" in capsys.readouterr().out
        assert 'include "RefProxyCustom.fppi"' in (output / "RefProxy.fpp").read_text()

    def test_a_missing_file_is_reported(
        self,
        tmp_path: Path,
        ref_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        output = tmp_path / "RefProxy"
        status = main(
            [
                str(ref_path),
                "-o",
                str(output),
                "--custom-private-hpp",
                str(tmp_path / "No.hpp"),
            ]
        )
        assert status == 2
        assert "custom-private-hpp:" in capsys.readouterr().err
        assert not output.exists()

    def test_a_dry_run_checks_them_too(
        self,
        tmp_path: Path,
        ref_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        status = main(
            [
                str(ref_path),
                "-o",
                str(tmp_path / "RefProxy"),
                "--dry-run",
                "--custom-fpp",
                str(tmp_path / "No.fppi"),
            ]
        )
        assert status == 2
        assert "custom-fpp:" in capsys.readouterr().err
