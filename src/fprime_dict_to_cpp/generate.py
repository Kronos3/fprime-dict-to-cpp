"""Turning a dictionary into a buildable F Prime component on disk."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from pathlib import Path

from fprime_cpp_codegen import ClangFormat, Formatter

from . import dictionary
from .cmake import render_cmake
from .cpp import cpp_file_names, render_implementation
from .cpp.component import SECTIONS
from .fpp import render_fpp
from .fpp.files import FRAGMENTS
from .model import Dictionary
from .plan import Config, Plan, resolve

__all__ = [
    "CustomFileError",
    "Generated",
    "check_custom_files",
    "generate",
    "generate_from_plan",
]


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

    removed: list[Path] = field(default_factory=list)
    """Sources an earlier run generated that this one no longer populates.

    A section emptied by ``omit`` -- every event omitted, say -- stops being generated,
    and the file left behind still decodes members the model no longer declares, so it
    no longer compiles.  Only a file this tool's own banner marks as generated is removed.
    """

    stale_cmake: bool = False
    """Whether a kept ``CMakeLists.txt`` lists a different set of sources than this run made."""

    @property
    def files(self) -> list[Path]:
        """Every file the component owns, written or not."""
        return sorted([*self.written, *self.unchanged, *self.kept, *self.removed])

    @property
    def warnings(self) -> tuple[str, ...]:
        """Anything about the dictionary, or the files, worth the caller's attention."""
        if not self.stale_cmake:
            return self.plan.warnings
        return (
            *self.plan.warnings,
            f"{self.directory / 'CMakeLists.txt'} was kept, but does not list the "
            "sources this run generated; regenerate it with --cmake (or `cmake: true`) "
            "or edit its SOURCES to match",
        )


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
    to choose one; it is off here and on at the command line, where a missing
    ``clang-format`` is an error rather than a reason to skip it.  ``cross_check`` additionally parses the dictionary with
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


class CustomFileError(Exception):
    """A file of the project's own that the component cannot be built with."""


def check_custom_files(config: Config, output: str | Path) -> None:
    """Refuse a custom file that is missing, or that this tool would itself write.

    Missing, because the include that names it would otherwise fail in ``fpp-check`` or
    the compiler, a build later and a long way from the line that asked for it.  One of
    the generator's own names, because that file is overwritten -- or, once its section
    empties, removed -- by the very run that is meant to build it in.  Every name the
    component could own is refused, not just the ones this run populates, so a file
    that is safe today does not become a casualty of a later ``omit``.
    """
    directory = Path(output)
    owned = _owned_names(config.component)
    for path, key in config.custom_keys:
        if not path:
            continue
        if Path(path).as_posix() in owned:
            raise CustomFileError(
                f"{key}: {path} is a file this tool generates into {directory}, so it "
                f"would be overwritten; name your own file something else, such as "
                f"{config.component}Custom{Path(path).suffix}"
            )
        if not (directory / path).is_file():
            raise CustomFileError(
                f"{key}: {directory / path} does not exist; this tool includes your "
                "file but does not write it"
            )


def _owned_names(component: str) -> set[str]:
    """Every file name this tool may generate into a component's directory."""
    return {
        "CMakeLists.txt",
        f"{component}.fpp",
        f"{component}.hpp",
        f"{component}.cpp",
        *(f"{component}{section}.fppi" for section in FRAGMENTS),
        *(f"{component}{section}.cpp" for section in SECTIONS),
    }


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
    check_custom_files(config, output)
    plan = resolve(parsed, config)
    config = plan.config
    if extra_warnings:
        plan = replace(plan, warnings=(*plan.warnings, *extra_warnings))

    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    unchanged: list[Path] = []
    kept: list[Path] = []
    removed: list[Path] = []

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
        directory, formatter=_formatter(clang_format, directory)
    )
    written.extend(result.written)
    unchanged.extend(result.unchanged)

    populated = set(cpp_file_names(plan))
    for section in SECTIONS:
        stale = directory / f"{config.component}{section}.cpp"
        if f"{config.component}{section}" not in populated and _is_generated(stale):
            stale.unlink()
            removed.append(stale)

    return Generated(
        directory=directory,
        plan=plan,
        written=written,
        unchanged=unchanged,
        kept=kept,
        removed=removed,
        stale_cmake=bool(kept) and _sources(cmake_file) != _sources_text(cmake_text),
    )


#: The banner every generated C++ file opens with, which marks one safe to remove.
_BANNER = "Generated by fprime-dict-to-cpp"


def _is_generated(path: Path) -> bool:
    """Whether ``path`` exists and is a file this tool wrote."""
    if not path.is_file():
        return False
    with path.open(encoding="utf-8") as stream:
        head = [stream.readline() for _ in range(5)]
    return any(_BANNER in line for line in head)


def _sources(path: Path) -> set[str]:
    """The component sources a ``CMakeLists.txt`` names."""
    return _sources_text(path.read_text(encoding="utf-8"))


def _sources_text(text: str) -> set[str]:
    """The ``.cpp`` file names quoted in some CMake text."""
    return set(re.findall(r"([\w.-]+\.cpp)\b", text))


def _formatter(clang_format: bool | str, directory: Path) -> Formatter | None:
    """A C++ formatter, if one was asked for."""
    if clang_format is False:
        return None
    clang = ClangFormat() if clang_format is True else ClangFormat(style=clang_format)

    def formatter(text: str, file_name: str) -> str:
        # clang-format looks for a .clang-format above the file it is told it is
        # formatting, so it is told the real path: a bare name would have it search
        # from wherever the tool happened to be run.
        return clang(text, str((directory / file_name).resolve()))

    return formatter


def _write(path: Path, text: str, written: list[Path], unchanged: list[Path]) -> None:
    """Write ``text`` unless the file already says exactly that."""
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        unchanged.append(path)
        return
    path.write_text(text, encoding="utf-8")
    written.append(path)
