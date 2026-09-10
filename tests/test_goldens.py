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
from fprime_dict_to_cpp.fpp import render_fpp
from fprime_dict_to_cpp.model import ComponentKind, Dictionary, QualifiedName
from fprime_dict_to_cpp.naming import Naming
from fprime_dict_to_cpp.plan import Config, Plan, resolve

COMPONENT = "RefProxy"

#: The second golden tree: the same dictionary with nothing declared and four ports hooked.
#:
#: Small on purpose.  ``omit`` takes every command, event and channel out of the ground
#: interface, so what is left is exactly the port machinery -- the generated port types, the
#: hooked declarations, the input-port handler that forwards a command, an event decoder
#: that reports through a port and nothing else, and a packet decoder that fires one.
#: Reading it beside ``RefProxy/`` is the fastest way to see what the port hooks do.
PORTED = "RefPorts"

PORTED_CONFIG = Config(
    component=PORTED,
    namespace=PORTED,
    kind=ComponentKind.ACTIVE,
    naming=Naming.preset("instance"),
    include_prefix=PORTED,
    omit=("*",),
    command_ports={
        QualifiedName.parse("Ref.dpDemo.SelectColor"): True,
        QualifiedName.parse("CdhCore.cmdDisp.CMD_NO_OP"): "pingRemote",
    },
    event_ports={QualifiedName.parse("CdhCore.cmdDisp.OpCodeDispatched"): True},
    packet_ports={"RefPackets.SigGen1Info": True},
)

#: Every file the ported component owns.  No ``Telemetry.cpp``: nothing declares or hooks a
#: channel, so there is no channelized telemetry to decode.
PORTED_FILES = (
    f"{PORTED}.fpp",
    f"{PORTED}Types.fppi",
    f"{PORTED}Events.fppi",
    f"{PORTED}Telemetry.fppi",
    "CMakeLists.txt",
    f"{PORTED}.hpp",
    f"{PORTED}.cpp",
    f"{PORTED}Commands.cpp",
    f"{PORTED}Events.cpp",
    f"{PORTED}Packets.cpp",
)

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
    f"{COMPONENT}.fpp",
    f"{COMPONENT}Types.fppi",
    f"{COMPONENT}Commands.fppi",
    f"{COMPONENT}Events.fppi",
    f"{COMPONENT}Telemetry.fppi",
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
        **render_fpp(golden_plan),
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


@pytest.fixture(scope="module")
def ported_plan(ref: Dictionary) -> Plan:
    """The Ref dictionary resolved against :data:`PORTED_CONFIG`."""
    return resolve(ref, PORTED_CONFIG)


@pytest.fixture(scope="module")
def ported(ported_plan: Plan) -> dict[str, str]:
    return {
        **render_fpp(ported_plan),
        "CMakeLists.txt": render_cmake(ported_plan),
        **render_implementation(ported_plan).files(cpp_file_names(ported_plan)),
    }


class TestPortedGoldens:
    """The hooked-port tree, pinned the same way and for the same reason."""

    @pytest.mark.parametrize("name", PORTED_FILES)
    def test_matches(self, ported: dict[str, str], name: str) -> None:
        assert_matches_golden(ported[name], f"{PORTED}/{name}")

    def test_the_file_set_is_exactly_the_golden_set(
        self, ported: dict[str, str]
    ) -> None:
        assert set(ported) == set(PORTED_FILES)

    def test_no_golden_is_orphaned(self) -> None:
        directory = GOLDENS / PORTED
        if not directory.is_dir():
            pytest.skip("goldens have not been generated yet")
        on_disk = {path.name for path in directory.iterdir() if path.is_file()}
        assert on_disk == set(PORTED_FILES)

    def test_declares_no_ground_interface_of_its_own(
        self, ported_plan: Plan, ported: dict[str, str]
    ) -> None:
        # The point of the configuration: every command, event and channel is omitted, so
        # the target deployment's ground dictionary gains nothing from this component
        # beyond its own diagnostics.
        assert not ported_plan.declared_commands
        assert not ported_plan.declared_events
        assert not ported_plan.declared_channels
        assert f"{PORTED}Commands.fppi" not in ported
        assert "async command " not in "".join(ported.values())
        assert len(ported_plan.ports) == 4


def _declarations(fragment: str, keyword: str) -> int:
    """Count ``keyword`` member declarations, which are not the same-named ports.

    ``event port logOut`` and ``telemetry port tlmOut`` open with the same keyword as the
    members do, so a plain substring count is one too many.  The fragments hold members
    only, but the pattern stays as it was: it is what makes the count trustworthy.
    """
    return len(re.findall(rf"^{keyword} (?!port )\S", fragment, re.MULTILINE))


def _golden(name: str) -> str:
    """One checked-in golden, read from disk."""
    path = GOLDENS / COMPONENT / name
    if not path.is_file():
        pytest.skip("goldens have not been generated yet")
    return path.read_text()


@pytest.fixture(scope="module")
def golden_model() -> str:
    """The checked-in component model, read from disk."""
    return _golden(f"{COMPONENT}.fpp")


@pytest.fixture(scope="module")
def golden_members() -> dict[str, str]:
    """The checked-in member fragments, by section, read from disk."""
    return {
        section: _golden(f"{COMPONENT}{section}.fppi")
        for section in ("Commands", "Events", "Telemetry")
    }


class TestGoldensAreRealFPrimeCode:
    """Spot checks that read the goldens rather than the renderer.

    These would pass trivially against the renderer's own output; read from disk they
    also confirm the checked-in files are the ones the rest of the suite describes.
    """

    def test_declares_an_active_component(self, golden_model: str) -> None:
        assert "active component RefProxy {" in golden_model

    def test_includes_every_member_fragment(self, golden_model: str) -> None:
        # The members are split out, so the component is only right if it pulls all four
        # fragments back in.
        for section in ("Types", "Commands", "Events", "Telemetry"):
            assert f'include "{COMPONENT}{section}.fppi"' in golden_model

    def test_mirrors_every_member(
        self, golden_members: dict[str, str], golden_plan: Plan
    ) -> None:
        commands, events, telemetry = (
            golden_members["Commands"],
            golden_members["Events"],
            golden_members["Telemetry"],
        )
        assert _declarations(commands, "async command") == len(golden_plan.commands)
        # Diagnostics add two events and three channels of the proxy's own.
        assert _declarations(events, "event") == len(golden_plan.events) + 2
        assert _declarations(telemetry, "telemetry") == len(golden_plan.channels) + 3

    def test_declares_the_remote_wire_widths(self) -> None:
        # The decoders read every packet header at these widths, so the model declares all
        # five whether or not a mirrored member happens to use one.
        types = _golden(f"{COMPONENT}Types.fppi")
        for alias, width in (
            ("FwPacketDescriptorType", "U16"),
            ("FwOpcodeType", "U32"),
            ("FwEventIdType", "U32"),
            ("FwChanIdType", "U32"),
            ("FwTlmPacketizeIdType", "U16"),
        ):
            assert f"type {alias} = {width}" in types

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
        self, golden_members: dict[str, str], golden_plan: Plan
    ) -> None:
        # Each mirrored member annotates the remote name and id it came from, which is
        # what makes the generated model readable next to the remote's own dictionary.
        mirrored = (
            len(golden_plan.commands)
            + len(golden_plan.events)
            + len(golden_plan.channels)
        )
        found = sum(text.count("] remote ") for text in golden_members.values())
        assert found == mirrored

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
        assert render_fpp(first) == render_fpp(second)
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
