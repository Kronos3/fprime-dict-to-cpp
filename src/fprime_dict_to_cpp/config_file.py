"""Reading the same configuration out of a YAML file.

One key per option, named as the flag is named, so ``--type-prefix Rp_`` is
``type-prefix: Rp_``.  Two keys read *positively* where the flag reads negatively --
``packetized: false`` rather than ``no-packetized: true`` -- because a file describes the
component it wants rather than the switches that get flipped to obtain it.  Underscores
are accepted wherever a hyphen is, since ``type_prefix`` is what a Python programmer
will type first.

What comes back is a mapping of ``argparse`` destinations to values, which the command
line installs as its *defaults*.  That fixes the precedence in one place and without a
second pass over the arguments: a flag beats the file, and the file beats the built-in
default.  It also means ``-i`` offers the file's values as the answers to accept.

Relative ``dictionary`` and ``output`` paths resolve against the file's own directory,
not the working directory, so a configuration committed next to a deployment keeps
working whatever directory it is run from.
"""

from __future__ import annotations

import os
import re
import textwrap
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import ComponentKind, QualifiedName
from .naming import PRESETS, Qualification, Style

__all__ = ["ConfigError", "KEYS", "load", "skeleton"]


class ConfigError(Exception):
    """A configuration file that cannot be understood."""


class _Invalid(Exception):
    """What a key will accept.  :func:`load` adds the key and the file to it."""


class _Rejected(Exception):
    """A complaint about one entry of a mapping, used as :func:`load` found it."""


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _Invalid("a name")
    return value


def _any_text(value: Any) -> str:
    """Text that is allowed to be empty, an empty prefix meaning no prefix."""
    if not isinstance(value, str):
        raise _Invalid("a string")
    return value


def _path(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise _Invalid("a path")
    return Path(value).expanduser()


def _flag(value: Any) -> bool:
    # YAML has already turned true/false/yes/no into a bool; anything else here is a
    # mistake worth naming rather than a truthiness question.
    if not isinstance(value, bool):
        raise _Invalid("true or false")
    return value


def _negated_flag(value: Any) -> bool:
    """A positive key standing in for a ``--no-...`` flag."""
    return not _flag(value)


def _choice(choices: tuple[str, ...]) -> Callable[[Any], str]:
    def coerce(value: Any) -> str:
        if not isinstance(value, str) or value not in choices:
            raise _Invalid("one of " + ", ".join(choices))
        return value

    return coerce


def _overrides(value: Any) -> dict[str, str]:
    """A ``dictionary name: generated name`` table, checked as far as one file can be.

    Whether a name is a *usable* identifier, and whether the dictionary has anything by
    that name, are questions for :func:`~fprime_dict_to_cpp.plan.resolve`, which is where
    the answers live and where the Python API needs them checked too.
    """
    if not isinstance(value, Mapping):
        raise _Invalid("a mapping of dictionary names to generated names")
    table: dict[str, str] = {}
    for key, chosen in value.items():
        if not isinstance(key, str) or not key.strip():
            raise _Invalid(f"dictionary names as its keys, found {key!r}")
        if not isinstance(chosen, str) or not chosen.strip():
            raise _Rejected(f"{key}: expected a name, found {chosen!r}")
        table[key.strip()] = chosen.strip()
    return table


def _member_overrides(value: Any) -> dict[QualifiedName, str]:
    """The same, keyed the way a member is keyed everywhere else."""
    return {QualifiedName.parse(key): name for key, name in _overrides(value).items()}


def _clang_format(value: Any) -> bool | str:
    """``true`` for clang-format's own default style, or a named style."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value
    raise _Invalid("true, false, or a style name")


@dataclass(frozen=True)
class _Option:
    """One key: the ``argparse`` destination it feeds, and what it accepts."""

    dest: str
    coerce: Callable[[Any], Any]
    note: str = ""
    """One line about the key, for the skeleton :func:`skeleton` writes."""

    example: tuple[str, ...] = ()
    """The lines :func:`skeleton` comments out in place of ``key: <default>``.

    For a key whose default is not the useful thing to show: an override table has no
    default at all, and a knob that overrides a preset has one that goes stale the moment
    the preset above it is changed.  Showing the shape, or the choices, does not.
    """

    relative: bool = False
    """Whether a relative value resolves against the configuration file's directory."""


#: Every key a configuration file may set.  ``--config`` and ``--interactive`` are
#: deliberately absent: a file that selects another file, or that decides to start
#: asking questions, is a surprise rather than a configuration.
#:
#: Order is the order :func:`skeleton` writes them in, so it runs from what a component
#: *is* to how it is spelled to what the run does.
KEYS: dict[str, _Option] = {
    "dictionary": _Option(
        "dictionary",
        _path,
        relative=True,
        note="the remote deployment's JSON dictionary, relative to this file",
    ),
    "output": _Option(
        "output",
        _path,
        relative=True,
        note="where the component goes; '.' is the directory this file is in",
    ),
    "component": _Option("component", _text, note="the generated component's name"),
    "namespace": _Option(
        "namespace",
        _text,
        note="the FPP module holding the component and its types",
    ),
    "kind": _Option(
        "kind",
        _choice(tuple(k.value for k in ComponentKind)),
        note="active: decode on the component's own thread.  queued: on whatever drives "
        "its run port.  passive: on the caller's thread, with guarded commands",
    ),
    "naming": _Option(
        "naming",
        _choice(tuple(sorted(PRESETS))),
        note="how Ref.dpDemo.SelectColor collapses into one member name.  instance: "
        "dpDemo_SelectColor.  qualified: Ref_dpDemo_SelectColor.  source-case: "
        "RefDpDemoSelectColor.  member: SelectColor",
    ),
    "name-qualification": _Option(
        "name_qualification",
        _choice(tuple(q.value for q in Qualification)),
        note="how much of the instance path a name keeps, whatever the preset above says",
        example=(
            "name-qualification: full  # or "
            + ", or ".join(
                q.value for q in Qualification if q is not Qualification.FULL
            ),
        ),
    ),
    "name-style": _Option(
        "name_style",
        _choice(tuple(s.value for s in Style)),
        note="how the kept segments join, whatever the preset above says",
        example=("name-style: source-case  # or underscore",),
    ),
    "names": _Option(
        "name_overrides",
        _member_overrides,
        note="members named outright, in place of whatever the scheme above calls them; "
        "a key matching nothing in the dictionary is an error, not a no-op",
        example=("names:", "  Ref.dpDemo.SelectColor: SelectDemoColor"),
    ),
    "type-names": _Option(
        "type_name_overrides",
        _overrides,
        note="generated types named outright, in place of the flattened qualified name; "
        "used as written, so type-prefix is not applied to one",
        example=(
            "type-names:",
            "  Ref.DpDemo.ColorEnum: DemoColor  # not Ref_DpDemo_ColorEnum",
        ),
    ),
    "type-prefix": _Option(
        "type_prefix",
        _any_text,
        note="prepended to every generated type name, for a leaf name that would "
        "otherwise clash with a framework type",
    ),
    "include-prefix": _Option(
        "include_prefix",
        _text,
        note="path prefix for the #include of the autocoded base class, normally this "
        "directory relative to the project root; an include path, not a file path",
    ),
    "packetized": _Option(
        "no_packetized",
        _negated_flag,
        note="decode FW_PACKET_PACKETIZED_TLM as well as channelized telemetry",
    ),
    "diagnostics": _Option(
        "no_diagnostics",
        _negated_flag,
        note="declare the proxy's own health telemetry and events, without which a "
        "dropping link looks the same as a quiet one",
    ),
    "cmake": _Option(
        "cmake",
        _flag,
        note="rewrite CMakeLists.txt from the current file list.  Off, so that whatever "
        "this project has added to it survives a regeneration",
    ),
    "clang-format": _Option(
        "clang_format",
        _clang_format,
        note="run the generated C++ through clang-format; true for its own default "
        "style, or name one",
    ),
    "cross-check": _Option(
        "cross_check",
        _flag,
        note="also parse the dictionary with fprime-gds and report any disagreement",
    ),
    "dry-run": _Option(
        "dry_run", _flag, note="report what would be generated without writing anything"
    ),
    "quiet": _Option("quiet", _flag, note="only report warnings and errors"),
}


#: What the skeleton shows a commented-out key as.  Only the keys whose default is a
#: fixed value: ``namespace`` follows the component, ``include-prefix`` follows the
#: output directory, and the two naming knobs follow whichever preset is in force, so
#: those are worked out per file instead.
_SHOWN: dict[str, Any] = {
    "kind": ComponentKind.ACTIVE.value,
    "naming": "instance",
    "type-prefix": "",
    "packetized": True,
    "diagnostics": True,
    "cmake": False,
    "clang-format": False,
    "cross-check": False,
    "dry-run": False,
    "quiet": False,
}

_WIDTH = 86


def skeleton(
    chosen: Mapping[str, Any],
    *,
    defaults: Mapping[str, Any] | None = None,
    header: Sequence[str] = (),
) -> str:
    """A configuration file holding ``chosen``, with every other key commented out.

    The point of the commented keys is that the finer-grained decisions are *discoverable
    without documentation*: the file that comes out of ``-i`` lists everything that can be
    changed, each with a line saying what it does, so iterating means uncommenting rather
    than looking something up.

    ``defaults`` gives the commented value for a key whose default depends on an answer
    -- ``namespace``, ``include-prefix`` -- and ``header`` is prepended as comments.
    """
    given = dict(defaults or {})
    if "component" in chosen:
        given.setdefault("namespace", chosen["component"])

    lines = [f"# {line}".rstrip() for line in header]
    for key, option in KEYS.items():
        lines.append("")
        if option.note:
            lines.extend(
                textwrap.wrap(
                    option.note,
                    width=_WIDTH,
                    initial_indent="# ",
                    subsequent_indent="#   ",
                )
            )
        shown = given.get(key, _SHOWN.get(key))
        if key in chosen:
            lines.extend(_entry(key, chosen[key], commented=False))
        elif option.example:
            lines.extend(f"# {line}" for line in option.example)
        elif shown is None:
            # Nothing true to suggest -- there is no default dictionary.  Left as a bare
            # key, which reads as the placeholder it is and, if uncommented as it stands,
            # is reported as giving no value rather than quietly meaning something.
            lines.append(f"# {key}:")
        else:
            lines.extend(_entry(key, shown, commented=True))
    return "\n".join(lines).strip() + "\n"


def _entry(key: str, value: Any, *, commented: bool) -> list[str]:
    """One ``key: value`` entry, or its commented-out form."""
    prefix = "# " if commented else ""
    if isinstance(value, Mapping):
        if not value:
            return [f"{prefix}{key}: {{}}"]
        entries = [f"{name}: {_scalar(chosen)}" for name, chosen in value.items()]
        return [f"{prefix}{key}:", *(f"{prefix}{line}" for line in _indented(entries))]
    return [f"{prefix}{key}: {_scalar(value)}"]


def _indented(lines: Iterable[str]) -> list[str]:
    return [f"  {line}" for line in lines]


#: Text that YAML would read as something other than a string.
_AMBIGUOUS = re.compile(
    r"^$|^\s|\s$|^[-?:,\[\]{}#&*!|>'\"%@`]|:\s|\s#|"
    r"^(?i:true|false|yes|no|on|off|null|~|[-+]?\d+(\.\d*)?)$"
)


def _scalar(value: Any) -> str:
    """``value`` as YAML, quoted only where it would otherwise change meaning."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if _AMBIGUOUS.search(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def load(path: Path) -> dict[str, Any]:
    """The ``argparse`` defaults ``path`` asks for.

    Raises :class:`ConfigError` for anything wrong with the file's *contents*, and lets
    :class:`OSError` through for anything wrong with reading it, which the command line
    already reports the same way for the dictionary.
    """
    defaults: dict[str, Any] = {}
    for key, value in _document(path).items():
        name, option = _option(key, path)
        if value is None:
            raise ConfigError(f"{path}: {name}: gives no value")
        try:
            coerced = option.coerce(value)
        except _Invalid as accepted:
            raise ConfigError(
                f"{path}: {name}: expected {accepted}, found {value!r}"
            ) from None
        except _Rejected as entry:
            raise ConfigError(f"{path}: {name}: {entry}") from None
        if option.relative and not coerced.is_absolute():
            # Collapsed textually, so that a file one directory down asking for
            # ../build/P reports build/P rather than examples/../build/P.
            coerced = Path(os.path.normpath(path.parent / coerced))
        defaults[option.dest] = coerced
    return defaults


def _option(key: Any, path: Path) -> tuple[str, _Option]:
    """The option ``key`` names, hyphens and underscores being interchangeable."""
    if not isinstance(key, str):
        raise ConfigError(f"{path}: {key!r} is not an option name")
    name = key.strip().replace("_", "-")
    try:
        return name, KEYS[name]
    except KeyError:
        raise ConfigError(
            f"{path}: unknown option {key!r}; known options are "
            + ", ".join(sorted(KEYS))
        ) from None


def _document(path: Path) -> Mapping[Any, Any]:
    """Parse ``path``.  An empty file is an empty configuration, not an error."""
    yaml = _yaml()
    try:
        # Parsed from the open file rather than from its text so that PyYAML's own
        # "line 3, column 7" markers name the file instead of "<unicode string>".
        with path.open(encoding="utf-8") as stream:
            document = yaml.load(stream, _strict_loader(yaml))
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: {error}") from None
    if document is None:
        return {}
    if not isinstance(document, Mapping):
        raise ConfigError(
            f"{path}: expected a mapping of options at the top level, found "
            f"{_describe(document)}"
        )
    return document


def _strict_loader(yaml: Any) -> Any:
    """``SafeLoader``, but a repeated key is an error rather than the last one winning.

    YAML says the last one wins, which for an override table is the wrong answer: two
    lines naming the same member look like two overrides and are one.  The error carries
    the offending line, since PyYAML's mark comes with it for free.
    """

    class Strict(yaml.SafeLoader):  # type: ignore[misc]
        def construct_mapping(self, node: Any, deep: bool = False) -> Any:
            seen: set[Any] = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, Hashable):
                    continue  # PyYAML reports this one itself
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"duplicate key {key!r}", key_node.start_mark
                    )
                seen.add(key)
            return super().construct_mapping(node, deep=deep)

    return Strict


def _describe(value: Any) -> str:
    """What a YAML value is, in the words the error messages use."""
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, (list, tuple)):
        return "a list"
    return type(value).__name__


def _yaml() -> Any:
    """PyYAML, imported late so the rest of the tool works without it installed."""
    try:
        import yaml
    except ImportError:
        raise ConfigError(
            "reading a YAML configuration needs PyYAML: "
            "pip install 'fprime-dict-to-cpp[yaml]'"
        ) from None
    return yaml
