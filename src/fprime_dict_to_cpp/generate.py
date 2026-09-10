"""Turning a dictionary into a buildable F Prime component on disk."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from fprime_cpp_codegen import ClangFormat, Formatter

from . import dictionary
from .cmake import render_cmake
from .cpp import render_implementation
from .fpp import render_fpp
from .model import Dictionary
from .plan import Config, Plan, resolve

__all__ = ["Generated", "generate", "generate_from_plan"]


@dataclass(frozen=True)
class Generated:
    """What a generation run produced."""

    directory: Path
    plan: Plan
    written: list[Path] = field(default_factory=list)
    """Files created or updated."""

    unchanged: list[Path] = field(default_factory=list)
    """Files that already held the right contents and were left alone.

    Preserved so a no-op regeneration does not cascade a rebuild through everything
    downstream of the component.
    """

    kept: list[Path] = field(default_factory=list)
    """Files that exist, are not what would be generated, and were left alone anyway.

    ``CMakeLists.txt`` only: it is the one generated file a project is expected to edit
    -- a dependency the FPP model does not imply, a target property -- and a run that is
    iterating on the *configuration* has no business undoing that.  ``cmake=True``
    rewrites it, which is what to do when the file list itself has changed.
    """

    @property
    def files(self) -> list[Path]:
        """Every file the component owns, written or not."""
        return sorted([*self.written, *self.unchanged, *self.kept])

    @property
    def warnings(self) -> tuple[str, ...]:
        """Anything about the dictionary worth the caller's attention."""
        return self.plan.warnings


def generate(
    dictionary_path: str | Path,
    output: str | Path,
    config: Config,
    *,
    clang_format: bool | str = False,
    cross_check: bool = False,
    cmake: bool = False,
) -> Generated:
    """Read the dictionary at ``dictionary_path`` and write a component into ``output``.

    ``clang_format`` runs the generated C++ through ``clang-format``; pass a style name
    to choose one.  ``cross_check`` additionally parses the dictionary with
    ``fprime-gds``'s own loaders and reports any disagreement as a warning, which is
    worth doing when adopting a dictionary from an unfamiliar toolchain.  ``cmake``
    rewrites an existing ``CMakeLists.txt``, which is otherwise left as the project left
    it; see :attr:`Generated.kept`.
    """
    parsed = dictionary.load(dictionary_path)
    extra: list[str] = []
    if cross_check:
        extra = [
            f"fprime-gds disagrees with this parse: {problem}"
            for problem in dictionary.cross_check(dictionary_path, parsed)
        ]
    return generate_from_plan(
        parsed,
        output,
        config,
        clang_format=clang_format,
        cmake=cmake,
        extra_warnings=extra,
    )


def generate_from_plan(
    parsed: Dictionary,
    output: str | Path,
    config: Config,
    *,
    clang_format: bool | str = False,
    cmake: bool = False,
    extra_warnings: list[str] | None = None,
) -> Generated:
    """Write a component for an already-parsed dictionary."""
    plan = resolve(parsed, config)
    if extra_warnings:
        plan = replace(plan, warnings=(*plan.warnings, *extra_warnings))

    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    unchanged: list[Path] = []
    kept: list[Path] = []

    for name, text in render_fpp(plan).items():
        _write(directory / name, text, written, unchanged)

    # An existing CMakeLists.txt that says something else was edited on purpose, so it
    # is reported rather than replaced.  One that already agrees is simply unchanged,
    # which keeps a plain regeneration reading the way it always did.
    cmake_file = directory / "CMakeLists.txt"
    cmake_text = render_cmake(plan)
    if cmake or not cmake_file.is_file():
        _write(cmake_file, cmake_text, written, unchanged)
    elif cmake_file.read_text(encoding="utf-8") == cmake_text:
        unchanged.append(cmake_file)
    else:
        kept.append(cmake_file)

    result = render_implementation(plan).write(
        directory, formatter=_formatter(clang_format)
    )
    written.extend(result.written)
    unchanged.extend(result.unchanged)

    return Generated(
        directory=directory,
        plan=plan,
        written=written,
        unchanged=unchanged,
        kept=kept,
    )


def _formatter(clang_format: bool | str) -> Formatter | None:
    """A C++ formatter, if one was asked for."""
    if clang_format is False:
        return None
    if clang_format is True:
        return ClangFormat()
    return ClangFormat(style=clang_format)


def _write(path: Path, text: str, written: list[Path], unchanged: list[Path]) -> None:
    """Write ``text`` unless the file already says exactly that."""
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        unchanged.append(path)
        return
    path.write_text(text, encoding="utf-8")
    written.append(path)
