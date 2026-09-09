"""The ``fprime-dict-to-cpp`` command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import dictionary
from .cpp import cpp_file_names
from .generate import Generated, generate
from .model import ComponentKind, Dictionary, DictionaryError
from .naming import PRESETS, Naming, Qualification, Style
from .plan import Config, resolve

__all__ = ["build_parser", "main"]

_EPILOG = """\
examples:
  # An active proxy for the Ref deployment, into Ref/RefProxy/
  fprime-dict-to-cpp RefTopologyDictionary.json -o Ref/RefProxy

  # A passive proxy, decoding on the caller's thread, with fully qualified names
  fprime-dict-to-cpp dict.json -o out --kind passive --naming qualified

  # Keep the remote's own casing conventions
  fprime-dict-to-cpp dict.json -o out --naming source-case

  # Just report what would be generated
  fprime-dict-to-cpp dict.json --dry-run
"""


def build_parser() -> argparse.ArgumentParser:
    """The argument parser, exposed so the tests can exercise it directly."""
    parser = argparse.ArgumentParser(
        prog="fprime-dict-to-cpp",
        description=(
            "Generate an F Prime component that proxies a remote deployment's "
            "dictionary: it decodes the remote's event and telemetry packets into its "
            "own events and channels, and wraps each of the remote's commands as a "
            "command of its own."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "dictionary", type=Path, help="the remote deployment's JSON dictionary"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="directory to write the component into (default: ./<component>)",
    )
    parser.add_argument(
        "-c",
        "--component",
        help="the generated component's name (default: <Deployment>Proxy)",
    )
    parser.add_argument(
        "-n",
        "--namespace",
        help="the FPP module to put the component and its types in "
        "(default: the component name)",
    )
    parser.add_argument(
        "-k",
        "--kind",
        choices=[k.value for k in ComponentKind],
        default=ComponentKind.ACTIVE.value,
        help="component kind, which decides where a packet is decoded: 'active' on the "
        "component's own thread, 'queued' on whatever drives its run port, 'passive' on "
        "the caller's thread (default: %(default)s)",
    )

    naming = parser.add_argument_group(
        "naming",
        "How a dotted dictionary name such as Ref.dpDemo.SelectColor becomes one "
        "component member name.  Colliding names are re-qualified automatically, "
        "whichever scheme is chosen.",
    )
    naming.add_argument(
        "--naming",
        choices=sorted(PRESETS),
        default="instance",
        help="instance: dpDemo_SelectColor.  qualified: Ref_dpDemo_SelectColor.  "
        "source-case: RefDpDemoSelectColor, keeping the member's own convention, so a "
        "SCREAMING_SNAKE member stays screaming.  member: SelectColor "
        "(default: %(default)s)",
    )
    naming.add_argument(
        "--name-qualification",
        choices=[q.value for q in Qualification],
        help="override the preset's qualification depth",
    )
    naming.add_argument(
        "--name-style",
        choices=[s.value for s in Style],
        help="override the preset's joining style",
    )
    naming.add_argument(
        "--type-prefix",
        default="",
        help="prepend this to every generated type name, if one would otherwise clash "
        "with a framework type of the same leaf name",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--include-prefix",
        default=None,
        help="path prefix for the generated #include of the autocoded base class; "
        "normally the component's directory relative to the project root "
        "(default: the output directory's name)",
    )
    output.add_argument(
        "--no-packetized",
        action="store_true",
        help="do not decode packetized telemetry (FW_PACKET_PACKETIZED_TLM)",
    )
    output.add_argument(
        "--no-diagnostics",
        action="store_true",
        help="do not declare the proxy's own health telemetry and events",
    )
    output.add_argument(
        "--clang-format",
        nargs="?",
        const=True,
        default=False,
        metavar="STYLE",
        help="run the generated C++ through clang-format, optionally with a named style",
    )
    output.add_argument(
        "--cross-check",
        action="store_true",
        help="also parse the dictionary with fprime-gds and report any disagreement",
    )
    output.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be generated without writing anything",
    )
    output.add_argument(
        "-q", "--quiet", action="store_true", help="only report warnings and errors"
    )
    return parser


def _naming(arguments: argparse.Namespace) -> Naming:
    """The naming configuration, preset first and explicit overrides on top."""
    naming = Naming.preset(arguments.naming)
    if arguments.name_qualification is not None:
        naming = Naming(
            qualification=Qualification(arguments.name_qualification),
            style=naming.style,
        )
    if arguments.name_style is not None:
        naming = Naming(
            qualification=naming.qualification, style=Style(arguments.name_style)
        )
    return naming


def _config(arguments: argparse.Namespace, component: str, output: Path) -> Config:
    include_prefix = (
        arguments.include_prefix
        if arguments.include_prefix is not None
        else output.name
    )
    return Config(
        component=component,
        namespace=arguments.namespace or component,
        kind=ComponentKind(arguments.kind),
        naming=_naming(arguments),
        type_prefix=arguments.type_prefix,
        packetized=not arguments.no_packetized,
        diagnostics=not arguments.no_diagnostics,
        include_prefix=include_prefix,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the command line.  Returns the process exit status."""
    arguments = build_parser().parse_args(argv)

    try:
        parsed = dictionary.load(arguments.dictionary)
    except (DictionaryError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    deployment = parsed.metadata.deployment_identifier
    component = arguments.component or f"{deployment}Proxy"
    output = arguments.output or Path(component)
    config = _config(arguments, component, output)

    if arguments.dry_run:
        return _report_dry_run(parsed, config, output, quiet=arguments.quiet)

    try:
        result = generate(
            arguments.dictionary,
            output,
            config,
            clang_format=arguments.clang_format,
            cross_check=arguments.cross_check,
        )
    except DictionaryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if not arguments.quiet:
        _report(result)
    return 0


def _report(result: Generated) -> None:
    """Summarise a completed run."""
    plan = result.plan
    print(
        f"{plan.config.component}: {len(plan.commands)} commands, "
        f"{len(plan.events)} events, {len(plan.channels)} telemetry channels"
        + (f", {len(plan.packets)} telemetry packets" if plan.has_packets else "")
    )
    print(
        f"  {len(plan.spelling.type_defs())} type definitions "
        f"(of {len(plan.dictionary.type_defs)} in the dictionary; the rest are "
        f"unreachable from any mirrored member)"
    )
    for path in result.files:
        state = "unchanged" if path in result.unchanged else "wrote"
        print(f"  {state:9s} {path}")
    print(
        f"\nAdd it to the build with:\n"
        f'  add_fprime_subdirectory("${{CMAKE_CURRENT_LIST_DIR}}/{result.directory.name}")'
    )


def _report_dry_run(
    parsed: Dictionary, config: Config, output: Path, *, quiet: bool
) -> int:
    """Describe what a run would produce, without writing anything."""
    plan = resolve(parsed, config)
    for warning in plan.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if quiet:
        return 0
    component = config.component
    print(
        f"{component} ({config.kind.value}) in module {config.namespace}, "
        f"from {parsed.metadata.deployment_name}"
    )
    print(
        f"  {len(plan.commands)} commands, {len(plan.events)} events, "
        f"{len(plan.channels)} telemetry channels, {len(plan.packets)} telemetry packets"
    )
    print(f"  {len(plan.spelling.type_defs())} type definitions")
    names = [
        f"{component}Types.fpp",
        f"{component}.fpp",
        "CMakeLists.txt",
        f"{component}.hpp",
        f"{component}.cpp",
        *(f"{name}.cpp" for name in cpp_file_names(plan)),
    ]
    print(f"  would write into {output}/:")
    for name in names:
        print(f"    {name}")
    return 0
