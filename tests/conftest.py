"""Shared fixtures.

``tests`` is on ``sys.path`` so the helper modules next to these tests -- ``fpptools``
and ``build_probe`` -- import as plain modules.
"""

from __future__ import annotations

import difflib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fprime_dict_to_cpp import dictionary  # noqa: E402
from fprime_dict_to_cpp.model import ComponentKind, Dictionary  # noqa: E402
from fprime_dict_to_cpp.naming import Naming  # noqa: E402
from fprime_dict_to_cpp.plan import Config, Plan, resolve  # noqa: E402

REF_DICTIONARY = Path(__file__).parent / "data" / "RefTopologyDictionary.json"

GOLDENS = Path(__file__).parent / "goldens"

#: Set to rewrite the golden files instead of comparing against them.  Review the diff
#: that appears in ``git status`` afterwards -- that diff is the point of having them.
UPDATE_GOLDENS = os.environ.get("FDTC_UPDATE_GOLDENS") == "1"


class _PassThrough:
    """Stands in for a formatter: returns what it was given."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __call__(self, text: str, file_name: str) -> str:
        return text


@pytest.fixture(autouse=True)
def _no_external_formatters(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clang-format`` is on at the command line, so the CLI tests would otherwise need
    it installed to test anything else.  A test marked ``formatters`` gets the real one.
    """
    if request.node.get_closest_marker("formatters") is not None:
        return
    monkeypatch.setattr("fprime_dict_to_cpp.generate.ClangFormat", _PassThrough)


def assert_matches_golden(text: str, name: str) -> None:
    """Assert ``text`` is byte-identical to the golden file ``name``.

    With ``FDTC_UPDATE_GOLDENS=1`` the golden is rewritten instead, so a deliberate
    change to the generator becomes a reviewable diff rather than a puzzle.
    """
    path = GOLDENS / name
    if UPDATE_GOLDENS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    if not path.is_file():
        pytest.fail(
            f"no golden for {name}; run FDTC_UPDATE_GOLDENS=1 pytest to create it"
        )
    expected = path.read_text(encoding="utf-8")
    if text == expected:
        return
    diff = list(
        difflib.unified_diff(
            expected.splitlines(),
            text.splitlines(),
            fromfile=f"goldens/{name}",
            tofile=f"generated/{name}",
            lineterm="",
        )
    )
    shown = "\n".join(diff[:80])
    elided = f"\n... and {len(diff) - 80} more diff lines" if len(diff) > 80 else ""
    pytest.fail(
        f"{name} does not match its golden:\n\n{shown}{elided}\n\n"
        f"If the change is intended, rerun with FDTC_UPDATE_GOLDENS=1 and review the diff."
    )


@pytest.fixture(scope="session")
def ref_path() -> Path:
    """The vendored Ref deployment dictionary."""
    return REF_DICTIONARY


@pytest.fixture(scope="session")
def ref(ref_path: Path) -> Dictionary:
    """The Ref dictionary, parsed once for the whole session."""
    return dictionary.load(ref_path)


@pytest.fixture(scope="session")
def config() -> Config:
    """A representative configuration: an active proxy with the default naming."""
    return Config(
        component="RefProxy",
        namespace="RefProxy",
        kind=ComponentKind.ACTIVE,
        naming=Naming.preset("instance"),
        include_prefix="RefProxy",
    )


@pytest.fixture(scope="session")
def plan(ref: Dictionary, config: Config) -> Plan:
    """The Ref dictionary resolved against :func:`config`."""
    return resolve(ref, config)
