"""Running the generated C++ through ``clang-format``.

On at the command line.  The tests that need the real executable are marked
``formatters`` and skip themselves when it is not on ``PATH``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fprime_dict_to_cpp.cli import build_parser
from fprime_dict_to_cpp.generate import generate
from fprime_dict_to_cpp.plan import Config

needs_clang_format = pytest.mark.skipif(
    shutil.which("clang-format") is None, reason="clang-format is not on PATH"
)


class TestTheFlag:
    def test_is_on_by_default(self) -> None:
        assert build_parser().parse_args(["d.json"]).clang_format is True

    def test_has_a_way_off(self) -> None:
        parsed = build_parser().parse_args(["d.json", "--no-clang-format"])
        assert parsed.clang_format is False

    def test_still_takes_a_style(self) -> None:
        parsed = build_parser().parse_args(["d.json", "--clang-format", "Google"])
        assert parsed.clang_format == "Google"


@needs_clang_format
@pytest.mark.formatters
class TestClangFormat:
    def test_the_projects_own_style_applies_wherever_the_tool_runs(
        self,
        tmp_path: Path,
        ref_path: Path,
        config: Config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        style = "{BasedOnStyle: LLVM, IndentWidth: 7, ColumnLimit: 100}"
        project = tmp_path / "project"
        project.mkdir()
        (project / ".clang-format").write_text(
            "BasedOnStyle: LLVM\nIndentWidth: 7\nColumnLimit: 100\n"
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        found = generate(ref_path, project / "RefProxy", config, clang_format=True)
        named = generate(ref_path, tmp_path / "Named", config, clang_format=style)
        for path in found.files:
            if path.suffix in (".cpp", ".hpp"):
                assert path.read_text() == (named.directory / path.name).read_text()
