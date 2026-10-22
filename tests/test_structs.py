"""``kind: types``: top-level FPP structs for each command and telemetry packet."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import assert_matches_golden
from fpptools import Toolchain, find_toolchain

import build_probe

from fprime_dict_to_cpp.cli import main
from fprime_dict_to_cpp.fpp.structs import render_structs
from fprime_dict_to_cpp.generate import generate_types
from fprime_dict_to_cpp.model import (
    ComponentKind,
    Dictionary,
    NamingError,
    QualifiedName,
)
from fprime_dict_to_cpp.naming import Naming
from fprime_dict_to_cpp.plan import Config, resolve
from fprime_dict_to_cpp.structs import (
    StructsPlan,
    TypesConfigError,
    resolve_structs,
)

#: The golden tree, under ``goldens/RefTypes/``.
NAME = "RefTypes"

#: The configuration without the id members, which the goldens do not pin.
PLAIN_CONFIG = Config(
    component=NAME,
    namespace=NAME,
    kind=ComponentKind.TYPES,
    naming=Naming.preset("instance"),
)

#: The configuration the goldens were generated with: both id members on, so that the
#: checked-in tree shows everything a types run can say.
TYPES_CONFIG = replace(PLAIN_CONFIG, opcode_field=True, packet_id_field=True)

FILES = (f"{NAME}Types.fpp", f"{NAME}Commands.fpp", f"{NAME}Packets.fpp")


@pytest.fixture(scope="module")
def types_plan(ref: Dictionary) -> StructsPlan:
    return resolve_structs(ref, TYPES_CONFIG)


@pytest.fixture(scope="module")
def rendered(types_plan: StructsPlan) -> dict[str, str]:
    return render_structs(types_plan)


@pytest.fixture(scope="module")
def plain(ref: Dictionary) -> dict[str, str]:
    """Rendered without the id members."""
    return render_structs(resolve_structs(ref, PLAIN_CONFIG))


class TestGoldens:
    @pytest.mark.parametrize("name", FILES)
    def test_matches(self, rendered: dict[str, str], name: str) -> None:
        assert_matches_golden(rendered[name], f"{NAME}/{name}")

    def test_the_file_set_is_exactly_the_golden_set(
        self, rendered: dict[str, str]
    ) -> None:
        assert set(rendered) == set(FILES)


class TestPlan:
    def test_one_struct_per_command_and_packet(
        self, ref: Dictionary, types_plan: StructsPlan
    ) -> None:
        assert len(types_plan.commands) == len(ref.commands)
        packets = sum(len(s.packets) for s in ref.packet_sets)
        assert len(types_plan.packets) == packets

    def test_every_type_a_struct_reaches_is_defined_here(
        self, types_plan: StructsPlan, rendered: dict[str, str]
    ) -> None:
        # The dictionary is the only interface: nothing may need the remote's own FPP.
        commands = rendered[f"{NAME}Commands.fpp"]
        assert "      color: Ref_DpDemo_ColorEnum\n" in commands
        assert "Ref.DpDemo" not in commands
        types = rendered[f"{NAME}Types.fpp"]
        assert "  enum Ref_DpDemo_DpReqType: I32 {" in types
        assert "  enum Ref_SignalGen_DpReqType: I32 {" in types
        defined = {d.qualified_name for d in types_plan.type_defs}
        assert "Ref.ChoicePair" in defined and "Ref.Choice" in defined  # transitively

    def test_types_come_before_what_uses_them(self, types_plan: StructsPlan) -> None:
        order = [d.qualified_name for d in types_plan.type_defs]
        assert order.index("Ref.Choice") < order.index("Ref.ChoicePair")
        assert order.index("Ref.ManyChoices") < order.index("Ref.TooManyChoices")

    def test_unreachable_types_are_left_out(
        self, ref: Dictionary, types_plan: StructsPlan
    ) -> None:
        assert len(types_plan.type_defs) < len(ref.type_defs)
        assert "FwDpIdType" not in {d.qualified_name for d in types_plan.type_defs}

    def test_top_level_types_are_redeclared_at_the_remote_s_width(
        self, types_plan: StructsPlan, rendered: dict[str, str]
    ) -> None:
        # The project including these has its own FwSizeType, which need not agree.
        types = rendered[f"{NAME}Types.fpp"]
        assert "type Remote_FwSizeType = U64" in types
        packets = rendered[f"{NAME}Packets.fpp"]
        assert "events_EventsDropped: Remote_FwSizeType" in packets
        assert ": FwSizeType" not in packets

    def test_a_command_without_arguments_is_an_empty_struct(
        self, plain: dict[str, str]
    ) -> None:
        assert "struct cmdDisp_CMD_NO_OP {}" in plain[f"{NAME}Commands.fpp"]

    def test_packet_fields_keep_wire_order(
        self, ref: Dictionary, types_plan: StructsPlan
    ) -> None:
        (cdh,) = [p for p in types_plan.packets if p.source.name == "CDH"]
        channels = [f for f in cdh.fields if f.default is None]
        assert [f.annotation.split("]")[0][1:] for f in channels] == [
            str(member) for member in cdh.source.members
        ]

    def test_everything_is_in_the_namespace(self, rendered: dict[str, str]) -> None:
        for text in rendered.values():
            declarations = [l for l in text.splitlines() if l and l[0] not in "#@"]
            assert declarations[0] == f"module {NAME} {{"
            assert declarations[-1] == "}"

    def test_structs_and_constants_are_in_modules_of_their_own(
        self, rendered: dict[str, str]
    ) -> None:
        commands = rendered[f"{NAME}Commands.fpp"]
        assert "  module Opcode {\n    constant dpDemo_SelectColor = 0xA10" in commands
        assert "  module Command {\n" in commands
        assert "    struct dpDemo_SelectColor {\n" in commands
        packets = rendered[f"{NAME}Packets.fpp"]
        assert "  module PacketId {\n    constant CDH = 0x1" in packets
        assert "  module Packet {\n" in packets
        assert "    struct CDH {\n" in packets

    def test_a_nested_namespace_nests_the_modules(self, ref: Dictionary) -> None:
        plan = resolve_structs(ref, replace(TYPES_CONFIG, namespace="Proxy.Types"))
        commands = render_structs(plan)[f"{NAME}Commands.fpp"]
        assert "module Proxy {\n  module Types {\n    @ " in commands

    def test_a_struct_named_like_a_type_is_refused(self, ref: Dictionary) -> None:
        # One leaf name, one generated file: fpp-to-cpp would write both to it.
        config = replace(
            TYPES_CONFIG,
            name_overrides={
                QualifiedName.parse("Ref.dpDemo.SelectColor"): "Ref_DpDemo_ColorEnum"
            },
        )
        with pytest.raises(NamingError, match="generated type"):
            resolve_structs(ref, config)

    def test_name_overrides_name_a_struct_and_a_field(self, ref: Dictionary) -> None:
        plan = resolve_structs(
            ref,
            Config(
                component=NAME,
                namespace=NAME,
                kind=ComponentKind.TYPES,
                name_overrides={
                    QualifiedName.parse("Ref.dpDemo.SelectColor"): "SelectColor",
                    QualifiedName.parse(
                        "CdhCore.cmdDisp.CommandsDispatched"
                    ): "dispatched",
                },
            ),
        )
        assert "SelectColor" in [s.name for s in plan.commands]
        (cdh,) = [p for p in plan.packets if p.source.name == "CDH"]
        assert cdh.fields[0].name == "dispatched"  # PLAIN_CONFIG: no id member

    def test_omit_commands_drops_their_structs(self, ref: Dictionary) -> None:
        plan = resolve_structs(
            ref,
            Config(
                component=NAME,
                namespace=NAME,
                kind=ComponentKind.TYPES,
                omit_commands=("Ref.dpDemo.*",),
            ),
        )
        assert not any(
            str(s.source.name).startswith("Ref.dpDemo.") for s in plan.commands
        )

    def test_an_override_matching_nothing_is_an_error(self, ref: Dictionary) -> None:
        with pytest.raises(NamingError, match="matches no"):
            resolve_structs(
                ref,
                Config(
                    component=NAME,
                    namespace=NAME,
                    kind=ComponentKind.TYPES,
                    name_overrides={QualifiedName.parse("No.Such.Thing"): "X"},
                ),
            )

    @pytest.mark.parametrize(
        "option",
        [
            {"command_ports": {QualifiedName.parse("Ref.dpDemo.SelectColor"): True}},
            {"custom_fpp": "Custom.fppi"},
            {"type_prefix": "P_"},
        ],
    )
    def test_component_only_options_are_refused(
        self, ref: Dictionary, option: dict[str, object]
    ) -> None:
        config = Config(component=NAME, namespace=NAME, kind=ComponentKind.TYPES, **option)  # type: ignore[arg-type]
        with pytest.raises(TypesConfigError, match="generates no component"):
            resolve_structs(ref, config)


@pytest.fixture(scope="module")
def with_opcode(ref: Dictionary) -> StructsPlan:
    return resolve_structs(ref, replace(PLAIN_CONFIG, opcode_field=True))


class TestOpcodeField:
    """``opcode-field``: each command struct names its own opcode, by default."""

    def test_off_by_default(self, plain: dict[str, str]) -> None:
        assert "$opcode" not in plain[f"{NAME}Commands.fpp"]
        assert "default {" not in plain[f"{NAME}Commands.fpp"]
        assert "Remote_FwOpcodeType" not in plain[f"{NAME}Types.fpp"]

    def test_every_opcode_has_a_constant_either_way(
        self, plain: dict[str, str], types_plan: StructsPlan
    ) -> None:
        commands = plain[f"{NAME}Commands.fpp"]
        assert (
            "constant dpDemo_SelectColor = 0xA10 @< [Ref.dpDemo.SelectColor]\n"
            in commands
        )
        assert commands.count("constant ") == len(types_plan.commands)

    def test_the_opcode_comes_first_and_defaults_to_its_constant(
        self, with_opcode: StructsPlan
    ) -> None:
        commands = render_structs(with_opcode)[f"{NAME}Commands.fpp"]
        assert (
            "    struct dpDemo_SelectColor {\n"
            "      $opcode: Remote_FwOpcodeType @< The remote's opcode for this command\n"
            "      color: Ref_DpDemo_ColorEnum\n"
            "    } default { $opcode = Opcode.dpDemo_SelectColor }\n"
        ) in commands
        for struct in with_opcode.commands:
            assert struct.fields[0].default == f"Opcode.{struct.name}"

    def test_a_command_without_arguments_still_has_its_opcode(
        self, with_opcode: StructsPlan
    ) -> None:
        commands = render_structs(with_opcode)[f"{NAME}Commands.fpp"]
        assert "} default { $opcode = Opcode.cmdDisp_CMD_NO_OP }" in commands
        assert "struct cmdDisp_CMD_NO_OP {}" not in commands

    def test_the_remote_s_opcode_width_is_redeclared(
        self, with_opcode: StructsPlan
    ) -> None:
        types = render_structs(with_opcode)[f"{NAME}Types.fpp"]
        assert "type Remote_FwOpcodeType = U32" in types

    def test_steps_aside_for_an_argument_called_opcode(self, ref: Dictionary) -> None:
        (command,) = [c for c in ref.commands if c.params][:1]
        clashing = replace(
            command,
            params=(replace(command.params[0], name="opcode"), *command.params[1:]),
        )
        dictionary = replace(ref, commands=(clashing,))
        plan = resolve_structs(dictionary, replace(PLAIN_CONFIG, opcode_field=True))
        names = [field.name for field in plan.commands[0].fields]
        assert names[:2] == ["opcode_", "opcode"]

    @pytest.mark.parametrize("key", ["opcode_field", "packet_id_field"])
    def test_a_component_kind_refuses_it(self, ref: Dictionary, key: str) -> None:
        with pytest.raises(NamingError, match=key.replace("_", "-")):
            resolve(ref, Config(component="P", namespace="P", **{key: True}))  # type: ignore[arg-type]


class TestPacketIdField:
    """``packet-id-field``: each packet struct names its own id, by default."""

    def test_every_packet_id_has_a_constant_either_way(
        self, plain: dict[str, str], types_plan: StructsPlan
    ) -> None:
        packets = plain[f"{NAME}Packets.fpp"]
        assert "constant CDH = 0x1 @< [RefPackets.CDH]\n" in packets
        assert packets.count("constant ") == len(types_plan.packets)
        assert "default {" not in packets

    def test_the_id_comes_first_and_defaults_to_its_constant(
        self, ref: Dictionary
    ) -> None:
        plan = resolve_structs(ref, replace(PLAIN_CONFIG, packet_id_field=True))
        packets = render_structs(plan)[f"{NAME}Packets.fpp"]
        assert (
            "    struct CDH {\n"
            "      $id: Remote_FwTlmPacketizeIdType @< The remote's id for this packet\n"
        ) in packets
        assert "} default { $id = PacketId.CDH }\n" in packets
        types = render_structs(plan)[f"{NAME}Types.fpp"]
        assert "type Remote_FwTlmPacketizeIdType = U16" in types


class TestCli:
    def test_writes_only_the_struct_files(self, tmp_path: Path, ref_path: Path) -> None:
        output = tmp_path / "out"
        assert main([str(ref_path), "-o", str(output), "--kind", "types", "-q"]) == 0
        assert sorted(p.name for p in output.iterdir()) == sorted(
            ["RefTypesTypes.fpp", "RefTypesCommands.fpp", "RefTypesPackets.fpp"]
        )

    def test_dry_run_writes_nothing(self, tmp_path: Path, ref_path: Path) -> None:
        output = tmp_path / "out"
        assert (
            main([str(ref_path), "-o", str(output), "--kind", "types", "--dry-run"])
            == 0
        )
        assert not output.exists()

    def test_a_component_only_option_is_an_error(
        self, tmp_path: Path, ref_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = [
            str(ref_path),
            "-o",
            str(tmp_path),
            "--kind",
            "types",
            "--type-prefix",
            "P",
        ]
        assert main(argv) == 2
        assert "type-prefix" in capsys.readouterr().err

    def test_a_file_no_longer_populated_is_removed(
        self, tmp_path: Path, ref: Dictionary
    ) -> None:
        generate_types(ref, tmp_path, TYPES_CONFIG)
        result = generate_types(
            ref,
            tmp_path,
            Config(
                component=NAME,
                namespace=NAME,
                kind=ComponentKind.TYPES,
                omit_commands=("*",),
            ),
        )
        assert [p.name for p in result.removed] == [f"{NAME}Commands.fpp"]

    def test_regeneration_leaves_every_file_alone(
        self, tmp_path: Path, ref: Dictionary
    ) -> None:
        generate_types(ref, tmp_path, TYPES_CONFIG)
        again = generate_types(ref, tmp_path, TYPES_CONFIG)
        assert again.written == []


@pytest.fixture(scope="module")
def toolchain() -> Toolchain:
    found = find_toolchain()
    if found is None:
        pytest.skip("no FPP toolchain found; set FPRIME_ROOT to an F Prime checkout")
    return found


@pytest.mark.integration
class TestToolchain:
    """The files stand alone: beside the framework's own model, nothing else."""

    @pytest.mark.parametrize("id_fields", [False, True])
    def test_checks_autocodes_and_compiles(
        self,
        toolchain: Toolchain,
        ref: Dictionary,
        tmp_path: Path,
        id_fields: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # fpp-to-cpp places FppConstantsAc.hpp by the working directory, and includes it
        # by that path less the prefixes it is told to strip.
        monkeypatch.chdir(tmp_path)
        config = PLAIN_CONFIG if not id_fields else TYPES_CONFIG
        rendered = render_structs(resolve_structs(ref, config))
        sources = []
        for name, text in rendered.items():
            (tmp_path / name).write_text(text)
            sources.append(tmp_path / name)

        result = toolchain.check(*sources)
        assert result.returncode == 0, result.stdout + result.stderr
        result = build_probe.autocode(toolchain, tmp_path, *sources)
        assert result.returncode == 0, result.stdout + result.stderr

        header = (tmp_path / "CDHSerializableAc.hpp").read_text()
        assert f"namespace {NAME} {{" in header and "namespace Packet {" in header
        constants = (tmp_path / "FppConstantsAc.hpp").read_text()
        assert "namespace Opcode {" in constants
        assert "dpDemo_SelectColor = 2576" in constants
        if id_fields:
            # The default clause, through its constant, reaches the constructor.
            source = tmp_path / "dpDemo_SelectColorSerializableAc.cpp"
            assert "m_opcode(2576)" in source.read_text()
            source = tmp_path / "CDHSerializableAc.cpp"
            assert "m_id(1)" in source.read_text()

        if build_probe.compiler() is None:
            pytest.skip("no C++ compiler on PATH")
        if not build_probe.framework_is_built(toolchain):
            pytest.skip("the F Prime checkout has not been autocoded")
        for unit in sorted(tmp_path.glob("*Ac.cpp")):
            result = subprocess.run(
                [
                    str(build_probe.compiler()),
                    "-std=c++14",
                    "-fsyntax-only",
                    *build_probe.include_paths(toolchain, tmp_path),
                    str(unit),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, f"{unit.name}:\n{result.stderr}"
