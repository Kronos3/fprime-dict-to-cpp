"""The ``fprime-dict-to-cpp`` command line.

The arguments are not written out here: they are
:class:`~fprime_dict_to_cpp.options.Options`' fields, and :func:`build_parser` turns each
one into whichever of ``argparse``'s shapes its type calls for.  So this module holds the
prose that is only a command line's -- the description, the worked examples, the argument
groups -- and the run itself.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

from fprime_cpp_codegen import CppCodegenError

from . import config_file, dictionary, interactive
from .cpp import cpp_file_names
from .fpp import fpp_file_names
from .generate import Generated, generate
from .model import Dictionary, DictionaryError, NamingError
from .options import Invalid, Options, Shape, Spec, specs
from .plan import Config, Plan, resolve

__all__ = ["build_parser", "main"]

_DESCRIPTION = (
    "Generate an F Prime component that proxies a remote deployment's dictionary: it "
    "decodes the remote's event and telemetry packets into its own events and channels, "
    "and wraps each of the remote's commands as a command of its own."
)

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

  # Set up a new component, and a configuration file to iterate on it through
  fprime-dict-to-cpp -i dict.json

  # Iterate: change a key in that file and run it again
  fprime-dict-to-cpp --config Ref/RefProxy/RefProxy.yaml
"""

#: The argument groups, by the name :class:`~fprime_dict_to_cpp.options.Spec` gives them.
_GROUPS: dict[str, tuple[str, str]] = {
    "naming": (
        "naming",
        "How a dotted dictionary name such as Ref.dpDemo.SelectColor becomes one "
        "component member name.  Colliding names are re-qualified automatically, "
        "whichever scheme is chosen.",
    ),
    "output": ("output", ""),
}


def build_parser() -> argparse.ArgumentParser:
    """The argument parser, exposed so the tests can exercise it directly."""
    parser = argparse.ArgumentParser(
        prog="fprime-dict-to-cpp",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    groups: dict[str, Any] = {"": parser}
    for name, (title, about) in _GROUPS.items():
        groups[name] = parser.add_argument_group(title, about or None)
    for spec in specs(Options).values():
        _declare(parser, groups[spec.group], spec)
    return parser


def _declare(parser: argparse.ArgumentParser, group: Any, spec: Spec) -> None:
    """Declare one option, in whichever of ``argparse``'s shapes its type calls for."""
    if not spec.flag:
        # A per-name override table is a table, which a command line has no good shape
        # for, so --config is the only way in.  Declaring the default here means
        # everything downstream can read the destination unconditionally.
        parser.set_defaults(**{spec.dest: spec.default})
        return

    help_text = spec.negated_note if spec.negated else spec.note
    if spec.show_default:
        help_text += f" (default: {_shown(spec.default)})"
    if spec.positional:
        group.add_argument(
            spec.dest,
            type=_argument(spec),
            nargs="?",
            default=spec.default,
            help=help_text,
        )
        return

    stem = f"--no-{spec.key}" if spec.negated else f"--{spec.key}"
    flags = [*spec.short, stem]
    if spec.shape is Shape.FLAG:
        group.add_argument(
            *flags,
            dest=spec.dest,
            action="store_false" if spec.negated else "store_true",
            default=spec.default,
            help=help_text,
        )
    elif spec.shape is Shape.STYLE:
        group.add_argument(
            *flags,
            dest=spec.dest,
            nargs="?",
            const=True,
            default=spec.default,
            metavar=spec.metavar,
            help=help_text,
        )
        if spec.default is not False:
            # On by default, so it needs a way off that does not mean spelling "false"
            # as though it were a style.
            group.add_argument(
                f"--no-{spec.key}",
                dest=spec.dest,
                action="store_false",
                help=f"do not {spec.note.split(':')[0]}",
            )
    else:
        group.add_argument(
            *flags,
            dest=spec.dest,
            type=_argument(spec),
            default=spec.default,
            metavar=_metavar(spec),
            help=help_text,
        )


def _shown(default: Any) -> str:
    """A default as the option's own values spell it, not as Python repeats it."""
    return str(default.value if isinstance(default, Enum) else default)


def _argument(spec: Spec) -> Callable[[str], Any]:
    """``spec``'s coercion, complaining the way ``argparse`` complains."""

    def convert(text: str) -> Any:
        try:
            return spec.coerce(text)
        except Invalid as accepted:
            raise argparse.ArgumentTypeError(f"expected {accepted}") from None

    return convert


def _metavar(spec: Spec) -> str | None:
    """What ``--help`` calls the argument: the choices, where there are any."""
    if spec.metavar:
        return spec.metavar
    if spec.shape is Shape.CHOICE:
        return "{" + ",".join(spec.choices) + "}"
    return None


def main(argv: list[str] | None = None) -> int:
    """Run the command line.  Returns the process exit status."""
    parser = build_parser()
    options = Options.from_namespace(parser.parse_args(argv))

    if options.config is not None:
        # The file's values become the parser's defaults, and the arguments are read
        # again on top of them, so an explicit flag still wins.
        try:
            parser.set_defaults(**config_file.load(options.config))
        except (config_file.ConfigError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        options = Options.from_namespace(parser.parse_args(argv))

    scaffold: interactive.Scaffold | None = None
    source: Path | None
    if options.interactive:
        prompter = interactive.Prompter(sys.stdin, sys.stderr)
        try:
            scaffold = interactive.scaffold(options, prompter)
        except (interactive.Aborted, KeyboardInterrupt) as reason:
            print(str(reason) or "aborted", file=sys.stderr)
            return 1
        except interactive.AlreadyConfigured as occupied:
            print(f"error: {occupied}", file=sys.stderr)
            return 2
        parsed, source, options = scaffold.dictionary, scaffold.source, scaffold.options
    else:
        source = options.dictionary
        if source is None:
            parser.error("a dictionary is required unless -i/--interactive is given")
        try:
            parsed = dictionary.load(source)
        except (DictionaryError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    output, config = options.settle(parsed.metadata.deployment_identifier)

    try:
        if options.dry_run:
            return _report_dry_run(parsed, config, output, quiet=options.quiet)
        result = generate(
            source,
            output,
            config,
            clang_format=options.clang_format,
            cross_check=options.cross_check,
            cmake=options.cmake,
        )
    except (DictionaryError, NamingError) as error:
        print(f"error: {_attributed(error, options)}", file=sys.stderr)
        return 2
    except CppCodegenError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if not options.quiet:
        _report(result, scaffold)
    return 0


def _attributed(error: Exception, options: Options) -> str:
    """``error``, said to be the configuration file's if that is where it came from.

    A :class:`NamingError` is about an override, and the only thing that can ask for one
    is ``--config``, so naming the file turns "that name is taken" into something the
    reader can go and edit.
    """
    if isinstance(error, NamingError) and options.config is not None:
        return f"{options.config}: {error}"
    return str(error)


def _override_note(config: Config) -> str | None:
    """A line about the names that were chosen rather than mangled, if any were."""
    names, types = len(config.name_overrides), len(config.type_name_overrides)
    if not names and not types:
        return None
    parts = []
    if names:
        parts.append(f"{names} member name(s)")
    if types:
        parts.append(f"{types} type name(s)")
    return f"  named explicitly: {', '.join(parts)}"


def _hook_notes(plan: Plan) -> list[str]:
    """What the port tables and ``omit`` did, said only when they did something.

    Worth its own lines rather than folded into the counts above: the counts are what the
    ground sees, and a hooked port is deliberately not part of that.
    """
    lines: list[str] = []
    if plan.ports:
        kinds = ", ".join(
            f"{count} {noun}"
            for noun, count in (
                ("command", len(plan.input_ports)),
                ("report", len(plan.output_ports)),
            )
            if count
        )
        lines.append(f"  hooked ports: {kinds}")
    dictionary = plan.dictionary
    total = len(dictionary.commands) + len(dictionary.events) + len(dictionary.channels)
    planned = len(plan.commands) + len(plan.events) + len(plan.channels)
    declared = (
        len(plan.declared_commands)
        + len(plan.declared_events)
        + len(plan.declared_channels)
    )
    if total != declared:
        lines.append(
            f"  omitted from the ground interface: {total - declared} member(s), of "
            f"which {planned - declared} are still decoded for a port"
        )
    return lines


def _report(result: Generated, scaffold: interactive.Scaffold | None = None) -> None:
    """Summarise a completed run, and say how to iterate on what it set up."""
    plan = result.plan
    print(
        f"{plan.config.component}: {len(plan.declared_commands)} commands, "
        f"{len(plan.declared_events)} events, "
        f"{len(plan.declared_channels)} telemetry channels"
        + (f", {len(plan.packets)} telemetry packets" if plan.has_packets else "")
    )
    print(
        f"  {len(plan.spelling.all_type_defs())} type definitions "
        f"({len(plan.spelling.wire_type_defs())} wire-format widths, and "
        f"{len(plan.spelling.type_defs())} of the "
        f"{len(plan.dictionary.type_defs)} in the dictionary; the rest are unreachable "
        f"from any mirrored member)"
    )
    for line in _hook_notes(plan):
        print(line)
    note = _override_note(plan.config)
    if note:
        print(note)
    if scaffold is not None and scaffold.written:
        print(f"  {'wrote':9s} {scaffold.config}")
    for path in result.files:
        print(f"  {_state(path, result):9s} {path}")
    print(
        f"\nAdd it to the build with:\n"
        f'  add_fprime_subdirectory("${{CMAKE_CURRENT_LIST_DIR}}/{result.directory.name}")'
    )
    if scaffold is not None:
        print(
            f"\nEverything else is configured in {scaffold.config}.  "
            f"Change a key there and re-run:\n"
            f"  fprime-dict-to-cpp --config {scaffold.config}"
        )


def _state(path: Path, result: Generated) -> str:
    """What happened to one file: written, already right, or left as it was found."""
    if path in result.kept:
        return "kept"
    if path in result.removed:
        return "removed"
    return "unchanged" if path in result.unchanged else "wrote"


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
        f"  {len(plan.declared_commands)} commands, {len(plan.declared_events)} events, "
        f"{len(plan.declared_channels)} telemetry channels, "
        f"{len(plan.packets)} telemetry packets"
    )
    print(f"  {len(plan.spelling.all_type_defs())} type definitions")
    for line in _hook_notes(plan):
        print(line)
    note = _override_note(config)
    if note:
        print(note)
    names = [
        *fpp_file_names(plan),
        "CMakeLists.txt",
        f"{component}.hpp",
        f"{component}.cpp",
        *(f"{name}.cpp" for name in cpp_file_names(plan)),
    ]
    print(f"  would write into {output}/:")
    for name in names:
        print(f"    {name}")
    return 0
