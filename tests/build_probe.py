"""Generating a component and compiling it, end to end.

The only convincing evidence that a generated component is correct is that the real
autocoder accepts its model and a real compiler accepts its implementation against the
resulting base class.  This drives all three steps, and doubles as the harness the tests
use.

Run it directly to see the result and keep the output tree:

    python tests/build_probe.py [output-directory]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fpptools import Toolchain, find_toolchain  # noqa: E402

from fprime_dict_to_cpp import dictionary  # noqa: E402
from fprime_dict_to_cpp.cpp import cpp_file_names, render_implementation  # noqa: E402
from fprime_dict_to_cpp.fpp import render_fpp  # noqa: E402
from fprime_dict_to_cpp.model import ComponentKind, QualifiedName  # noqa: E402
from fprime_dict_to_cpp.naming import Naming  # noqa: E402
from fprime_dict_to_cpp.plan import Config, resolve  # noqa: E402

DICTIONARY = Path(__file__).parent / "data" / "RefTopologyDictionary.json"


@dataclass
class Result:
    """What a probe run produced."""

    directory: Path
    autocode: subprocess.CompletedProcess[str] | None = None
    compiles: dict[str, subprocess.CompletedProcess[str]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        if self.autocode is None or self.autocode.returncode != 0:
            return False
        return all(r.returncode == 0 for r in self.compiles.values())

    def failures(self) -> list[str]:
        """A readable account of whatever went wrong."""
        problems = []
        if self.autocode is not None and self.autocode.returncode != 0:
            problems.append(
                "fpp-to-cpp failed:\n" + self.autocode.stdout + self.autocode.stderr
            )
        for name, run in sorted(self.compiles.items()):
            if run.returncode != 0:
                problems.append(f"{name} did not compile:\n" + run.stderr[:4000])
        return problems


def generate(
    output: Path,
    *,
    kind: ComponentKind = ComponentKind.ACTIVE,
    naming: str = "instance",
    component: str = "RefProxy",
    namespace: str = "RefProxy",
    diagnostics: bool = True,
    packetized: bool = True,
    name_overrides: dict[QualifiedName, str] | None = None,
    type_name_overrides: dict[str, str] | None = None,
    omit: Sequence[str] = (),
    command_ports: dict[QualifiedName, bool | str] | None = None,
    event_ports: dict[QualifiedName, bool | str] | None = None,
    telemetry_ports: dict[QualifiedName, bool | str] | None = None,
    packet_ports: dict[str, bool | str] | None = None,
    custom: dict[str, tuple[str, str]] | None = None,
    path: Path = DICTIONARY,
) -> tuple[Config, Path, tuple[str, ...]]:
    """Write a component's model and implementation into ``output``.

    ``include_prefix`` is left empty so everything lands in one flat directory, which is
    what makes the files compilable without a CMake build tree.

    ``custom`` maps a :class:`Config` field -- ``custom_fpp``, ``custom_public_hpp``,
    ``custom_private_hpp`` or ``custom_cpp`` -- to the name and text of a project's own
    file, which is written beside the generated ones and built in through that field.
    """
    parsed = dictionary.load(path)
    output.mkdir(parents=True, exist_ok=True)
    chosen: dict[str, str] = {}
    for key, (name, text) in (custom or {}).items():
        (output / name).write_text(text)
        chosen[key] = name
    config = Config(
        component=component,
        namespace=namespace,
        kind=kind,
        naming=Naming.preset(naming),
        name_overrides=name_overrides or {},
        type_name_overrides=type_name_overrides or {},
        omit=omit,
        command_ports=command_ports or {},
        event_ports=event_ports or {},
        telemetry_ports=telemetry_ports or {},
        packet_ports=packet_ports or {},
        diagnostics=diagnostics,
        packetized=packetized,
        include_prefix="",
        custom_fpp=chosen.get("custom_fpp", ""),
        custom_public_hpp=chosen.get("custom_public_hpp", ""),
        custom_private_hpp=chosen.get("custom_private_hpp", ""),
        custom_cpp=chosen.get("custom_cpp", ""),
    )
    plan = resolve(parsed, config)
    for name, text in render_fpp(plan).items():
        (output / name).write_text(text)
    render_implementation(plan).write(output)
    return config, output / f"{component}.fpp", plan.warnings


def autocode(
    toolchain: Toolchain, output: Path, *sources: Path
) -> subprocess.CompletedProcess[str]:
    """Run ``fpp-to-cpp``, stripping both the output tree and the framework root.

    Stripping the framework root is what turns its includes into the project-relative
    form (``Fw/Cmd/CmdPortAc.hpp``) that a real build resolves, instead of absolute paths
    to ``.fpp`` files that have no ``.hpp`` beside them.
    """
    framework = _framework_root(toolchain)
    command = [
        str(toolchain.fpp_to_cpp),
        "-d",
        str(output),
        "-i",
        ",".join(str(p) for p in toolchain.dependencies),
        "-p",
        f"{output},{framework}",
        *map(str, sources),
    ]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def include_paths(toolchain: Toolchain, output: Path) -> list[str]:
    """The ``-I`` arguments needed to compile against an autocoded framework.

    A generated component's base class includes framework headers that only exist once
    the framework itself has been autocoded, so this needs a checkout that has been
    built at least once.
    """
    framework = _framework_root(toolchain)
    build = framework / "build-fprime-automatic-native"
    return [
        f"-I{output}",
        f"-I{framework}",
        f"-I{build / 'F-Prime'}",
        f"-I{build}",
        f"-I{framework / 'default'}",
        f"-I{build / 'F-Prime' / 'default'}",
        f"-I{framework / 'cmake' / 'platform' / 'unix'}",
    ]


def framework_is_built(toolchain: Toolchain) -> bool:
    """Whether the framework checkout has been autocoded, as compiling requires."""
    build = _framework_root(toolchain) / "build-fprime-automatic-native" / "F-Prime"
    return (build / "Fw" / "Cmd" / "CmdPortAc.hpp").is_file()


def _framework_root(toolchain: Toolchain) -> Path:
    """The F Prime checkout the toolchain's model files came from."""
    # dependencies[0] is <root>/cmake/platform/unix/Platform/PlatformTypes.fpp
    return toolchain.dependencies[0].parents[4]


def compiler() -> str | None:
    """A C++ compiler, if there is one."""
    for candidate in ("g++", "clang++", "c++"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def run(output: Path, *, keep: bool = True, **options: object) -> Result:
    """Generate, autocode and compile, reporting what happened at each step."""
    if output.exists() and not keep:
        shutil.rmtree(output)
    config, model_file, warnings = generate(output, **options)  # type: ignore[arg-type]
    result = Result(directory=output, warnings=warnings)

    toolchain = find_toolchain()
    if toolchain is None:
        return result
    result.autocode = autocode(toolchain, output, model_file)
    if result.autocode.returncode != 0:
        return result

    executable = compiler()
    if executable is None or not framework_is_built(toolchain):
        return result
    sources = [
        output / f"{config.component}.cpp",
        *(output / f"{name}.cpp" for name in _supplemental(output, config.component)),
    ]
    includes = include_paths(toolchain, output)
    for source in sources:
        result.compiles[source.name] = subprocess.run(
            [executable, "-std=c++14", "-fsyntax-only", *includes, str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
    return result


def _supplemental(output: Path, component: str) -> list[str]:
    """The supplemental source files that were actually written."""
    return sorted(
        path.stem for path in output.glob(f"{component}*.cpp") if path.stem != component
    )


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/fdtc-probe")
    result = run(output, keep=False)
    for warning in result.warnings:
        print(f"warning: {warning}")
    if result.autocode is None:
        print("no FPP toolchain found; generated files only")
        return 0
    for problem in result.failures():
        print(problem)
    print(f"{'OK' if result.ok else 'FAILED'} -- files in {output}")
    if not result.compiles:
        print("(compilation skipped: no compiler, or framework not autocoded)")
    else:
        print(f"compiled {len(result.compiles)} translation unit(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
