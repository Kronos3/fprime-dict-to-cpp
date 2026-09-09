"""Locating and driving a real FPP toolchain, for tests that want the last word.

Generated FPP is only correct if ``fpp-check`` says so, and generated C++ only lines up
with the autocoder if ``fpp-to-cpp`` produces a base class it actually overrides.  Both
tools ship with an F Prime checkout, so the tests use one when it is present and skip
when it is not.

Set ``FPRIME_ROOT`` to point at a checkout other than ``~/git/fprime``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Toolchain", "find_toolchain"]


@dataclass(frozen=True)
class Toolchain:
    """A located FPP toolchain, plus the framework model files it needs."""

    fpp_check: Path
    fpp_to_cpp: Path
    dependencies: tuple[Path, ...]
    """The framework's own FPP: platform types, config, ``Fw``, and ``Svc.Sched``.

    A generated component references ``Fw.Com``, ``Fw.Log`` and friends, and a queued one
    references ``Svc.Sched``, so none of it resolves standalone.
    """

    def check(self, *sources: Path) -> subprocess.CompletedProcess[str]:
        """Run ``fpp-check`` over the framework model plus ``sources``."""
        return self._run(
            [str(self.fpp_check), *map(str, self.dependencies), *map(str, sources)]
        )

    def to_cpp(
        self, output: Path, *sources: Path, path_prefix: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run ``fpp-to-cpp`` over ``sources``, with the framework model as imports.

        Imports rather than sources: the framework's C++ already exists, and asking for
        it again would collide with itself.
        """
        output.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.fpp_to_cpp),
            "-d",
            str(output),
            "-i",
            ",".join(str(p) for p in self.dependencies),
        ]
        if path_prefix is not None:
            command += ["-p", str(path_prefix)]
        return self._run([*command, *map(str, sources)])

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False)


def find_toolchain() -> Toolchain | None:
    """Locate an FPP toolchain, or ``None`` if there is not a usable one."""
    root = Path(os.environ.get("FPRIME_ROOT", Path.home() / "git" / "fprime"))
    if not root.is_dir():
        return None

    check = _executable("fpp-check", root)
    to_cpp = _executable("fpp-to-cpp", root)
    if check is None or to_cpp is None:
        return None

    dependencies = _framework_model(root)
    return Toolchain(check, to_cpp, dependencies) if dependencies else None


def _executable(name: str, root: Path) -> Path | None:
    """``name`` from the checkout's virtualenv, else from ``PATH``."""
    candidate = root / "fprime-venv" / "bin" / name
    if candidate.is_file():
        return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def _framework_model(root: Path) -> tuple[Path, ...]:
    """The framework FPP a generated component needs in order to resolve."""
    platform = root / "cmake" / "platform" / "unix" / "Platform" / "PlatformTypes.fpp"
    config = sorted((root / "default" / "config").glob("*.fpp"))
    framework = sorted((root / "Fw").rglob("*.fpp"))
    sched = root / "Svc" / "Sched" / "Sched.fpp"
    if not platform.is_file() or not config or not framework:
        return ()
    found = [platform, *config, *framework]
    if sched.is_file():
        found.append(sched)
    return tuple(found)
