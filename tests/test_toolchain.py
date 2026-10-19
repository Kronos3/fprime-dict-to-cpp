"""Integration: does a real FPP toolchain and a real compiler accept the output?

Everything else in this suite checks that the generator produces the text it meant to.
These tests check the only thing that finally matters -- that ``fpp-check`` accepts the
model, ``fpp-to-cpp`` autocodes it, and a C++ compiler builds the implementation against
the base class that comes out.  They found the two bugs that mattered most: a component
with events needs a text event port, and inside ``class RefProxy`` the injected
class-name hides ``namespace RefProxy``.

Each test skips itself when the thing it needs is missing, so the suite still runs on a
machine with no F Prime checkout.  Point ``FPRIME_ROOT`` at a checkout to opt in.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import build_probe
from fpptools import Toolchain, find_toolchain

from fprime_dict_to_cpp import dictionary
from fprime_dict_to_cpp.fpp import render_fpp
from fprime_dict_to_cpp.model import ComInput, ComponentKind, Dictionary, QualifiedName
from fprime_dict_to_cpp.naming import PRESETS, Naming
from fprime_dict_to_cpp.plan import Config, Plan, resolve

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def toolchain() -> Toolchain:
    found = find_toolchain()
    if found is None:
        pytest.skip("no FPP toolchain found; set FPRIME_ROOT to an F Prime checkout")
    return found


def _write_model(directory: Path, plan: Plan) -> Path:
    """Write every FPP file for ``plan`` into ``directory`` and return the model file.

    Only the model file goes to the toolchain: the ``.fppi`` fragments reach it through
    the ``include`` directives, resolved relative to the file that includes them, which is
    why they have to be written beside it.
    """
    files = render_fpp(plan)
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory / next(iter(files))


class TestFppCheck:
    """``fpp-check`` is the authority on whether the generated model is legal."""

    @pytest.mark.parametrize("kind", list(ComponentKind))
    @pytest.mark.parametrize("naming", sorted(PRESETS))
    def test_accepts_every_kind_and_naming_combination(
        self,
        toolchain: Toolchain,
        ref: Dictionary,
        tmp_path: Path,
        kind: ComponentKind,
        naming: str,
    ) -> None:
        plan = resolve(
            ref,
            Config(
                component="RefProxy",
                namespace="RefProxy",
                kind=kind,
                naming=Naming.preset(naming),
            ),
        )
        model_file = _write_model(tmp_path, plan)

        result = toolchain.check(model_file)
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("kind", list(ComponentKind))
    def test_accepts_a_buffer_com_in(
        self, toolchain: Toolchain, ref: Dictionary, tmp_path: Path, kind: ComponentKind
    ) -> None:
        plan = resolve(
            ref,
            Config(
                component="RefProxy",
                namespace="RefProxy",
                kind=kind,
                naming=Naming.preset("instance"),
                com_in=ComInput.BUFFER,
            ),
        )
        result = toolchain.check(_write_model(tmp_path, plan))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_accepts_names_that_were_chosen_rather_than_mangled(
        self, toolchain: Toolchain, ref: Dictionary, tmp_path: Path
    ) -> None:
        # An override reaches the model verbatim, so fpp-check is the authority on
        # whether what somebody typed is a name FPP will take.
        plan = resolve(
            ref,
            Config(
                component="RefProxy",
                namespace="RefProxy",
                kind=ComponentKind.ACTIVE,
                naming=Naming.preset("instance"),
                name_overrides={
                    QualifiedName.parse("Ref.dpDemo.SelectColor"): "SelectDemoColor",
                    QualifiedName.parse("CdhCore.cmdDisp.CMD_NO_OP"): "NoOp",
                    # A member named after an FPP reserved word, which the writer has to
                    # escape for the model to parse at all.
                    QualifiedName.parse("Ref.dpDemo.DpStarted"): "time",
                },
                type_name_overrides={"Ref.DpDemo.ColorEnum": "DemoColor"},
            ),
        )
        model_file = _write_model(tmp_path, plan)
        result = toolchain.check(model_file)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_accepts_a_nested_namespace(
        self, toolchain: Toolchain, ref: Dictionary, tmp_path: Path
    ) -> None:
        plan = resolve(
            ref,
            Config(
                component="Proxy",
                namespace="Ref.Bridge",
                kind=ComponentKind.ACTIVE,
                naming=Naming.preset("instance"),
            ),
        )
        model_file = _write_model(tmp_path, plan)
        result = toolchain.check(model_file)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_accepts_a_component_named_unlike_its_module(
        self, toolchain: Toolchain, ref: Dictionary, tmp_path: Path
    ) -> None:
        plan = resolve(
            ref,
            Config(
                component="Bridge",
                namespace="Ref",
                kind=ComponentKind.PASSIVE,
                naming=Naming.preset("source-case"),
            ),
        )
        model_file = _write_model(tmp_path, plan)
        result = toolchain.check(model_file)
        assert result.returncode == 0, result.stdout + result.stderr


class TestAutocodeAndCompile:
    """The full path: generate, autocode, compile against the generated base class."""

    @pytest.mark.parametrize("kind", list(ComponentKind))
    def test_compiles(
        self, toolchain: Toolchain, tmp_path: Path, kind: ComponentKind
    ) -> None:
        if build_probe.compiler() is None:
            pytest.skip("no C++ compiler on PATH")
        if not build_probe.framework_is_built(toolchain):
            pytest.skip(
                "the F Prime checkout has not been autocoded; the generated base class "
                "includes framework headers that only exist after a build"
            )
        result = build_probe.run(tmp_path / "out", keep=False, kind=kind)
        assert result.ok, "\n\n".join(result.failures())
        assert result.compiles, "nothing was compiled"

    def test_compiles_with_names_that_were_chosen(
        self, toolchain: Toolchain, tmp_path: Path
    ) -> None:
        # fpp-to-cpp derives a generated file name and a C++ identifier from a type's
        # name, so a renamed type is only really renamed if this passes.
        if build_probe.compiler() is None:
            pytest.skip("no C++ compiler on PATH")
        if not build_probe.framework_is_built(toolchain):
            pytest.skip("the F Prime checkout has not been autocoded")
        result = build_probe.run(
            tmp_path / "out",
            keep=False,
            name_overrides={
                QualifiedName.parse("Ref.dpDemo.SelectColor"): "SelectDemoColor"
            },
            type_name_overrides={"Ref.DpDemo.ColorEnum": "DemoColor"},
        )
        assert result.ok, "\n\n".join(result.failures())
        assert result.compiles, "nothing was compiled"
        out = tmp_path / "out"
        assert "SelectDemoColor" in (out / "RefProxyCommands.fppi").read_text()
        assert "DemoColor" in (out / "RefProxyTypes.fppi").read_text()

    def test_compiles_with_the_optional_parts_turned_off(
        self, toolchain: Toolchain, tmp_path: Path
    ) -> None:
        # A different code path: without diagnostics the helpers keep their signatures but
        # have nothing to do with their arguments, which -Wunused-parameter notices.
        if build_probe.compiler() is None:
            pytest.skip("no C++ compiler on PATH")
        if not build_probe.framework_is_built(toolchain):
            pytest.skip("the F Prime checkout has not been autocoded")
        result = build_probe.run(
            tmp_path / "out", keep=False, diagnostics=False, packetized=False
        )
        assert result.ok, "\n\n".join(result.failures())

    def test_compiles_without_warnings(
        self, toolchain: Toolchain, tmp_path: Path
    ) -> None:
        # F Prime builds with warnings enabled, and a shadowed variable shows up here
        # rather than in any assertion about the generated text.
        executable = build_probe.compiler()
        if executable is None:
            pytest.skip("no C++ compiler on PATH")
        if not build_probe.framework_is_built(toolchain):
            pytest.skip("the F Prime checkout has not been autocoded")

        output = tmp_path / "out"
        result = build_probe.run(output, keep=False)
        assert result.ok, "\n\n".join(result.failures())

        includes = build_probe.include_paths(toolchain, output)
        for source in sorted(output.glob("RefProxy*.cpp")):
            if source.name.endswith("ComponentAc.cpp"):
                continue  # the autocoder's own output is not ours to answer for
            run = subprocess.run(
                [
                    executable,
                    "-std=c++14",
                    "-fsyntax-only",
                    "-Wall",
                    "-Wextra",
                    *includes,
                    str(source),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            warnings = [
                line for line in run.stderr.splitlines() if ": warning:" in line
            ]
            assert not warnings, f"{source.name}:\n" + "\n".join(warnings[:10])


#: A configuration exercising every hook: an input port for a command with no arguments,
#: one with an enum, one with two strings (which the handler clamps), output ports for an
#: event, a channel and a whole packet, and an ``omit`` glob that leaves two of those
#: members with a port and nothing else.
HOOKS: dict[str, object] = {
    "omit": ("Ref.dpDemo.*",),
    "command_ports": {
        QualifiedName.parse("Ref.dpDemo.SelectColor"): True,
        QualifiedName.parse("CdhCore.cmdDisp.CMD_NO_OP"): "pingRemote",
        QualifiedName.parse("FileHandling.fileDownlink.SendFile"): True,
    },
    "event_ports": {
        QualifiedName.parse("CdhCore.cmdDisp.OpCodeDispatched"): True,
        QualifiedName.parse("Ref.dpDemo.DpStarted"): True,
    },
    "telemetry_ports": {
        QualifiedName.parse("CdhCore.cmdDisp.CommandsDispatched"): True
    },
    "packet_ports": {"RefPackets.CDH": True},
}

#: The same, with everything omitted, which is the "ports only" component.
PORTS_ONLY: dict[str, object] = {**HOOKS, "omit": ("*",)}

#: Hooks asked for by pattern rather than by name, which is where the derived names and
#: the generated port types arrive in bulk: one glob over an instance's commands, a second
#: keeping its parameter setters out, one name settling a port of its own, and a glob over
#: every channel of an instance.  ``fpp-check`` is the authority on whether twenty-odd
#: derived port names and port types are all still legal and all still distinct.
GLOBBED: dict[str, object] = {
    "omit": ("*",),
    "command_ports": {
        QualifiedName.parse("Ref.typeDemo.*"): True,
        QualifiedName.parse("*_PRM_SET"): False,
        QualifiedName.parse("CdhCore.cmdDisp.CMD_NO_OP"): "pingRemote",
    },
    "event_ports": {QualifiedName.parse("Ref.dpDemo.*"): True},
    "telemetry_ports": {QualifiedName.parse("CdhCore.cmdDisp.*"): True},
    "packet_ports": {"RefPackets.SigGen1*": True},
}


class TestHookedPorts:
    """The port hooks and ``omit``, which change both halves of the output at once."""

    @pytest.mark.parametrize("kind", list(ComponentKind))
    def test_fpp_check_accepts_hooked_ports(
        self, toolchain: Toolchain, ref: Dictionary, tmp_path: Path, kind: ComponentKind
    ) -> None:
        # A generated port type reaches the model verbatim, formals and all, so fpp-check
        # is the authority on whether a dictionary's argument names survive becoming one.
        plan = resolve(
            ref,
            Config(
                component="RefProxy",
                namespace="RefProxy",
                kind=kind,
                naming=Naming.preset("instance"),
                **HOOKS,  # type: ignore[arg-type]
            ),
        )
        model_file = _write_model(tmp_path, plan)
        result = toolchain.check(model_file)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_fpp_check_accepts_ports_hooked_by_pattern(
        self, toolchain: Toolchain, ref: Dictionary, tmp_path: Path
    ) -> None:
        plan = resolve(
            ref,
            Config(
                component="RefProxy",
                namespace="RefProxy",
                naming=Naming.preset("instance"),
                **GLOBBED,  # type: ignore[arg-type]
            ),
        )
        # Enough ports that a collision among the derived names would be likely rather
        # than possible, and none of them needed adjusting to stay unique.
        assert len(plan.ports) > 20
        assert not any("needed a suffix" in w for w in plan.warnings)
        result = toolchain.check(_write_model(tmp_path, plan))
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        ("label", "options"),
        [("hooked", HOOKS), ("ports-only", PORTS_ONLY), ("globbed", GLOBBED)],
    )
    def test_compiles(
        self,
        toolchain: Toolchain,
        tmp_path: Path,
        label: str,
        options: dict[str, object],
    ) -> None:
        if build_probe.compiler() is None:
            pytest.skip("no C++ compiler on PATH")
        if not build_probe.framework_is_built(toolchain):
            pytest.skip("the F Prime checkout has not been autocoded")
        result = build_probe.run(tmp_path / "out", keep=False, **options)
        assert result.ok, "\n\n".join(result.failures())
        assert result.compiles, "nothing was compiled"

    def test_compiles_without_warnings(
        self, toolchain: Toolchain, tmp_path: Path
    ) -> None:
        # The packet-port decoder declares every slot up front rather than one per block,
        # which is exactly the shape -Wunused-variable has opinions about.
        executable = build_probe.compiler()
        if executable is None:
            pytest.skip("no C++ compiler on PATH")
        if not build_probe.framework_is_built(toolchain):
            pytest.skip("the F Prime checkout has not been autocoded")

        output = tmp_path / "out"
        result = build_probe.run(output, keep=False, **HOOKS)
        assert result.ok, "\n\n".join(result.failures())

        includes = build_probe.include_paths(toolchain, output)
        for source in sorted(output.glob("RefProxy*.cpp")):
            if source.name.endswith("ComponentAc.cpp"):
                continue
            run = subprocess.run(
                [
                    executable,
                    "-std=c++14",
                    "-fsyntax-only",
                    "-Wall",
                    "-Wextra",
                    *includes,
                    str(source),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            warnings = [
                line for line in run.stderr.splitlines() if ": warning:" in line
            ]
            assert not warnings, f"{source.name}:\n" + "\n".join(warnings[:10])

    def test_ports_only_compiles_without_diagnostics(
        self, toolchain: Toolchain, tmp_path: Path
    ) -> None:
        # Without diagnostics the forward-error helper keeps its signature and has nothing
        # to do with its arguments, which is a different code path again.
        if build_probe.compiler() is None:
            pytest.skip("no C++ compiler on PATH")
        if not build_probe.framework_is_built(toolchain):
            pytest.skip("the F Prime checkout has not been autocoded")
        result = build_probe.run(
            tmp_path / "out", keep=False, diagnostics=False, **PORTS_ONLY
        )
        assert result.ok, "\n\n".join(result.failures())


#: A project's own members, of every shape the custom files exist for: a command and an
#: input port, whose handlers the private header has to declare, an event and a channel
#: the source reports through -- one of them carrying a mirrored type, named as the
#: generated fragments name it -- and a public accessor for the rest of the deployment.
#: Keyed by the configuration field each file is built in through.
CUSTOM: dict[str, tuple[str, str]] = {
    "custom_fpp": (
        "RefProxyCustom.fppi",
        """\
@ Ping the proxy itself, rather than the remote
async command PROXY_PING(token: U32)

@ Answers PROXY_PING
event ProxyPong(token: U32, color: Ref_DpDemo_ColorEnum) \\
  severity activity high \\
  format "pong {} {}"

@ PROXY_PINGs answered
telemetry ProxyPings: U32

@ Poked by a rate group
async input port poke: Svc.Sched
""",
    ),
    "custom_public_hpp": (
        "RefProxyPublic.hpp",
        """\
//! PROXY_PINGs answered so far
U32 pings() const;
""",
    ),
    "custom_private_hpp": (
        "RefProxyPrivate.hpp",
        """\
//! Handler for PROXY_PING
void PROXY_PING_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, U32 token) override;

//! Handler for poke
void poke_handler(FwIndexType portNum, U32 context) override;

//! PROXY_PINGs answered
U32 m_pings = 0;
""",
    ),
    "custom_cpp": (
        "RefProxyCustom.cpp",
        """\
#include "RefProxy.hpp"

namespace RefProxy {

U32 RefProxy::pings() const {
  return this->m_pings;
}

void RefProxy::PROXY_PING_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, U32 token) {
  this->m_pings++;
  this->tlmWrite_ProxyPings(this->m_pings);
  this->log_ACTIVITY_HI_ProxyPong(token, RefProxy_Ref_DpDemo_ColorEnum::RED);
  this->cmdResponse_out(opCode, cmdSeq, Fw::CmdResponse::OK);
}

void RefProxy::poke_handler(FwIndexType portNum, U32 context) {
  (void)portNum;
  this->log_ACTIVITY_HI_ProxyPong(context, RefProxy_Ref_DpDemo_ColorEnum::BLUE);
}

}  // namespace RefProxy
""",
    ),
}

#: Code outside the class, as a topology is, reaching what each header declares.
_OUTSIDE = """\
#include "RefProxy.hpp"

U32 countPings(const RefProxy::RefProxy& proxy);
U32 countPings(const RefProxy::RefProxy& proxy) {
  return proxy.pings();
}
"""


class TestCustomMembers:
    """A project's own ``.fppi``, headers and source, built into the generated component."""

    def test_autocodes_and_compiles_without_warnings(
        self, toolchain: Toolchain, tmp_path: Path
    ) -> None:
        output = tmp_path / "out"
        result = build_probe.run(output, keep=False, custom=CUSTOM, **HOOKS)
        assert result.ok, "\n\n".join(result.failures())
        if not result.compiles:
            pytest.skip("no C++ compiler, or the F Prime checkout is not autocoded")
        assert "RefProxyCustom.cpp" in result.compiles

        for name in ("RefProxy.cpp", "RefProxyCustom.cpp"):
            run = _compile(toolchain, output, output / name)
            warnings = [
                line for line in run.stderr.splitlines() if ": warning:" in line
            ]
            assert not warnings, f"{name}:\n" + "\n".join(warnings[:10])

    def test_each_header_lands_in_its_own_access_section(
        self, toolchain: Toolchain, tmp_path: Path
    ) -> None:
        output = tmp_path / "out"
        build_probe.run(output, keep=False, custom=CUSTOM)
        outside = output / "Outside.cpp"
        outside.write_text(_OUTSIDE)
        if build_probe.compiler() is None or not build_probe.framework_is_built(
            toolchain
        ):
            pytest.skip("no C++ compiler, or the F Prime checkout is not autocoded")
        public = _compile(toolchain, output, outside)
        assert public.returncode == 0, public.stderr[:4000]

        # And the private one really is private: the same call, to a handler, is refused.
        outside.write_text(_OUTSIDE.replace("proxy.pings()", "proxy.m_pings"))
        private = _compile(toolchain, output, outside)
        assert private.returncode != 0
        assert "private" in private.stderr

    def test_unpinned_members_follow_the_generated_ones(
        self, toolchain: Toolchain, tmp_path: Path
    ) -> None:
        output = tmp_path / "out"
        config, model_file, _ = build_probe.generate(output, custom=CUSTOM)
        assert build_probe.autocode(toolchain, output, model_file).returncode == 0
        base = (output / "RefProxyComponentAc.hpp").read_text()
        plan = resolve(dictionary.load(build_probe.DICTIONARY), config)
        assert f"OPCODE_PROXY_PING = 0x{len(plan.declared_commands):x}," in base


def _compile(
    toolchain: Toolchain, output: Path, source: Path
) -> subprocess.CompletedProcess[str]:
    """Syntax-check one translation unit against the autocoded component, warnings on."""
    executable = build_probe.compiler()
    assert executable is not None
    return subprocess.run(
        [
            executable,
            "-std=c++14",
            "-fsyntax-only",
            "-Wall",
            "-Wextra",
            *build_probe.include_paths(toolchain, output),
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


class TestCheckedInGoldens:
    """The goldens are what a reader reviews, so they are what has to build.

    Every other test in this file renders fresh text and validates that.  This one copies
    the checked-in tree out and validates *it*, so the files in the repository cannot
    drift into being unbuildable while the suite still passes.
    """

    @pytest.mark.parametrize("tree", ["RefProxy", "RefPorts"])
    def test_the_golden_tree_autocodes_and_compiles(
        self, toolchain: Toolchain, tmp_path: Path, tree: str
    ) -> None:
        from test_goldens import COMPONENT, GOLDEN_FILES, PORTED, PORTED_FILES

        COMPONENT, GOLDEN_FILES = (
            (COMPONENT, GOLDEN_FILES) if tree == "RefProxy" else (PORTED, PORTED_FILES)
        )
        source = Path(__file__).parent / "goldens" / COMPONENT
        if not source.is_dir():
            pytest.skip("goldens have not been generated yet")
        if build_probe.compiler() is None:
            pytest.skip("no C++ compiler on PATH")
        if not build_probe.framework_is_built(toolchain):
            pytest.skip("the F Prime checkout has not been autocoded")

        # Each tree was generated with include_prefix equal to its own name, so its
        # includes read "RefProxy/RefProxy.hpp".  Laying it out under that directory and
        # compiling from its parent is what makes that resolve, exactly as a real build
        # would.
        root = tmp_path / COMPONENT
        root.mkdir()
        for name in GOLDEN_FILES:
            shutil.copy(source / name, root / name)

        # Only the model file: the .fppi fragments beside it reach the autocoder through
        # its include directives.
        autocode = build_probe.autocode(toolchain, root, root / f"{COMPONENT}.fpp")
        assert autocode.returncode == 0, autocode.stdout + autocode.stderr

        executable = build_probe.compiler()
        assert executable is not None
        includes = [f"-I{tmp_path}", *build_probe.include_paths(toolchain, root)]
        sources = sorted(
            path
            for path in root.glob(f"{COMPONENT}*.cpp")
            if "ComponentAc" not in path.name
        )
        assert sources, "no implementation sources were copied"
        for path in sources:
            run = subprocess.run(
                [
                    executable,
                    "-std=c++14",
                    "-fsyntax-only",
                    "-Wall",
                    "-Wextra",
                    *includes,
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert run.returncode == 0, f"{path.name}:\n{run.stderr[:4000]}"
            assert ": warning:" not in run.stderr, f"{path.name}:\n{run.stderr[:2000]}"


class TestFppFormat:
    """The generated model should already be what ``fpp-format`` would make of it,
    modulo layout: if the tool rejects it, the model is not parseable."""

    def test_the_model_round_trips_through_fpp_format(
        self, toolchain: Toolchain, ref: Dictionary, tmp_path: Path
    ) -> None:
        formatter = toolchain.fpp_check.parent / "fpp-format"
        if not formatter.is_file() and shutil.which("fpp-format") is None:
            pytest.skip("fpp-format is not available")
        plan = resolve(
            ref,
            Config(
                component="RefProxy",
                namespace="RefProxy",
                kind=ComponentKind.ACTIVE,
                naming=Naming.preset("instance"),
            ),
        )
        model_file = _write_model(tmp_path, plan)
        # -i resolves the include specifiers, so the fragments are formatted too and their
        # declarations reach stdout -- which is what the assertion below reads.
        run = subprocess.run(
            [str(formatter), "-i", str(model_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        # Formatting is layout only, so every declaration has to survive it.
        assert "async command dpDemo_SelectColor" in run.stdout
