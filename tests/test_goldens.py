"""The Ref dictionary's generated component, checked in and compared byte for byte.

Every other test asserts a property of the output -- that a declaration is present, that
a local is not shadowed.  This one pins the *whole* thing, so a change anywhere in the
generator shows up as a reviewable diff of real F Prime code rather than as a passing
test suite and a shrug.

The golden tree under ``goldens/RefProxy/`` is what
``fprime-dict-to-cpp tests/data/RefTopologyDictionary.json -o RefProxy`` produces with
every default: an active component, ``instance`` naming, diagnostics and packetized
telemetry on.  ``tests/test_toolchain.py`` is what establishes that this exact output is
accepted by ``fpp-check``, autocoded by ``fpp-to-cpp`` and compiled warning-free.

To adopt a deliberate change::

    FDTC_UPDATE_GOLDENS=1 venv/bin/python -m pytest tests/test_goldens.py
    git diff tests/goldens
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import GOLDENS, assert_matches_golden

from fprime_dict_to_cpp.cmake import render_cmake
from fprime_dict_to_cpp.cpp import cpp_file_names, render_implementation
from fprime_dict_to_cpp.fpp import render_component, render_types
from fprime_dict_to_cpp.model import ComponentKind, Dictionary
from fprime_dict_to_cpp.naming import Naming
from fprime_dict_to_cpp.plan import Config, Plan, resolve

COMPONENT = "RefProxy"

#: The configuration the goldens were generated with: every CLI default.
GOLDEN_CONFIG = Config(
    component=COMPONENT,
    namespace=COMPONENT,
    kind=ComponentKind.ACTIVE,
    naming=Naming.preset("instance"),
    include_prefix=COMPONENT,
)

#: Every file the component owns, in the order the CLI reports them.
GOLDEN_FILES = (
    f"{COMPONENT}Types.fpp",
    f"{COMPONENT}.fpp",
    "CMakeLists.txt",
    f"{COMPONENT}.hpp",
    f"{COMPONENT}.cpp",
    f"{COMPONENT}Commands.cpp",
    f"{COMPONENT}Events.cpp",
    f"{COMPONENT}Telemetry.cpp",
    f"{COMPONENT}Packets.cpp",
)


@pytest.fixture(scope="module")
def golden_plan(ref: Dictionary) -> Plan:
    """The Ref dictionary resolved against :data:`GOLDEN_CONFIG`."""
    return resolve(ref, GOLDEN_CONFIG)


@pytest.fixture(scope="module")
def rendered(golden_plan: Plan) -> dict[str, str]:
    """Every generated file, rendered once for the module."""
    return {
        f"{COMPONENT}Types.fpp": render_types(golden_plan),
        f"{COMPONENT}.fpp": render_component(golden_plan),
        "CMakeLists.txt": render_cmake(golden_plan),
        **render_implementation(golden_plan).files(cpp_file_names(golden_plan)),
    }


class TestGoldens:
    @pytest.mark.parametrize("name", GOLDEN_FILES)
    def test_matches(self, rendered: dict[str, str], name: str) -> None:
        assert_matches_golden(rendered[name], f"{COMPONENT}/{name}")

    def test_the_file_set_is_exactly_the_golden_set(
        self, rendered: dict[str, str]
    ) -> None:
        # A new output file that nobody goldened would otherwise go unnoticed.
        assert set(rendered) == set(GOLDEN_FILES)

    def test_no_golden_is_orphaned(self) -> None:
        # And a file the generator stopped producing would otherwise linger.
        directory = GOLDENS / COMPONENT
        if not directory.is_dir():
            pytest.skip("goldens have not been generated yet")
        on_disk = {path.name for path in directory.iterdir() if path.is_file()}
        assert on_disk == set(GOLDEN_FILES)


def _declarations(model: str, keyword: str) -> int:
    """Count ``keyword`` member declarations, which are not the same-named ports.

    ``event port logOut`` and ``telemetry port tlmOut`` open with the same keyword as the
    members do, so a plain substring count is one too many.
    """
    return len(re.findall(rf"^    {keyword} (?!port )\S", model, re.MULTILINE))


@pytest.fixture(scope="module")
def golden_model() -> str:
    """The checked-in component model, read from disk."""
    path = GOLDENS / COMPONENT / f"{COMPONENT}.fpp"
    if not path.is_file():
        pytest.skip("goldens have not been generated yet")
    return path.read_text()


class TestGoldensAreRealFPrimeCode:
    """Spot checks that read the goldens rather than the renderer.

    These would pass trivially against the renderer's own output; read from disk they
    also confirm the checked-in files are the ones the rest of the suite describes.
    """

    def test_declares_an_active_component(self, golden_model: str) -> None:
        assert "active component RefProxy {" in golden_model

    def test_mirrors_every_member(self, golden_model: str, golden_plan: Plan) -> None:
        assert _declarations(golden_model, "async command") == len(golden_plan.commands)
        # Diagnostics add two events and three channels of the proxy's own.
        assert _declarations(golden_model, "event") == len(golden_plan.events) + 2
        assert _declarations(golden_model, "telemetry") == len(golden_plan.channels) + 3

    def test_declares_the_ports_too(self, golden_model: str) -> None:
        for declaration in (
            "async input port comIn: Fw.Com",
            "output port comOut: Fw.Com",
            "event port logOut",
            "text event port logTextOut",
            "telemetry port tlmOut",
        ):
            assert declaration in golden_model

    def test_carries_provenance_for_every_member(
        self, golden_model: str, golden_plan: Plan
    ) -> None:
        # Each mirrored member annotates the remote name and id it came from, which is
        # what makes the generated model readable next to the remote's own dictionary.
        mirrored = (
            len(golden_plan.commands)
            + len(golden_plan.events)
            + len(golden_plan.channels)
        )
        assert golden_model.count("] remote ") == mirrored

    def test_events_record_the_remote_id_they_match_on(self, golden_plan: Plan) -> None:
        path = GOLDENS / COMPONENT / f"{COMPONENT}Events.cpp"
        if not path.is_file():
            pytest.skip("goldens have not been generated yet")
        events = path.read_text()
        # One switch arm per event, keyed on the remote's absolute id.
        for event in golden_plan.events[:20]:
            assert f"case 0x{event.remote_id:X}: {{" in events


class TestGoldenStability:
    def test_rendering_twice_gives_the_same_bytes(self, ref: Dictionary) -> None:
        # Nothing in the pipeline may depend on set iteration order or a timestamp, or
        # the goldens would churn on every run.
        first = resolve(ref, GOLDEN_CONFIG)
        second = resolve(ref, GOLDEN_CONFIG)
        assert render_component(first) == render_component(second)
        assert render_types(first) == render_types(second)
        assert render_implementation(first).files(
            cpp_file_names(first)
        ) == render_implementation(second).files(cpp_file_names(second))

    def test_written_files_match_the_rendered_text(
        self, tmp_path: Path, ref_path: Path, rendered: dict[str, str]
    ) -> None:
        # generate() must not transform anything on the way to disk.
        from fprime_dict_to_cpp.generate import generate

        result = generate(ref_path, tmp_path / COMPONENT, GOLDEN_CONFIG)
        for path in result.files:
            assert path.read_text() == rendered[path.name], path.name
