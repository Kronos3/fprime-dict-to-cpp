"""Reading the same configuration out of a YAML file, and writing one back.

One key per option, named as the flag is named, so ``--type-prefix Rp_`` is
``type-prefix: Rp_``.  A flag that reads negatively reads *positively* here --
``packetized: false`` rather than ``no-packetized: true`` -- because a file describes the
component it wants rather than the switches that get flipped to obtain it.  Underscores
are accepted wherever a hyphen is, since ``type_prefix`` is what a Python programmer will
type first.

Which keys there are, what each accepts, and what each defaults to are not decided here:
they are :class:`~fprime_dict_to_cpp.options.Options`' fields, reflected into
:data:`KEYS`.  What is decided here is only what is particular to a file -- how a value
is spelled as YAML, that a repeated key is an error, and that a relative path resolves
against the file's own directory.

What :func:`load` comes back with is a mapping of ``argparse`` destinations to values,
which the command line installs as its *defaults*.  That fixes the precedence in one
place and without a second pass over the arguments: a flag beats the file, and the file
beats the built-in default.  It also means ``-i`` offers the file's values as the answers
to accept.
"""

from __future__ import annotations

import os
import re
import textwrap
from collections.abc import Hashable, Iterable, Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any

from .options import Invalid, Options, Rejected, Spec, specs

__all__ = ["ConfigError", "KEYS", "load", "skeleton"]


class ConfigError(Exception):
    """A configuration file that cannot be understood."""


#: Every key a configuration file may set, in the order :func:`skeleton` writes them.
KEYS: dict[str, Spec] = {
    key: spec for key, spec in specs(Options).items() if spec.configurable
}

_WIDTH = 86


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def load(path: Path) -> dict[str, Any]:
    """The ``argparse`` defaults ``path`` asks for.

    Raises :class:`ConfigError` for anything wrong with the file's *contents*, and lets
    :class:`OSError` through for anything wrong with reading it, which the command line
    already reports the same way for the dictionary.
    """
    defaults: dict[str, Any] = {}
    for key, value in _document(path).items():
        name, spec = _spec(key, path)
        if value is None:
            raise ConfigError(f"{path}: {name}: gives no value")
        try:
            coerced = spec.coerce(value)
        except Invalid as accepted:
            raise ConfigError(
                f"{path}: {name}: expected {accepted}, found {value!r}"
            ) from None
        except Rejected as entry:
            raise ConfigError(f"{path}: {name}: {entry}") from None
        defaults[spec.dest] = _resolved(coerced, path) if spec.relative else coerced
    return defaults


def _resolved(value: Path, path: Path) -> Path:
    """A relative path, against the configuration file's own directory.

    So that a configuration committed next to a deployment keeps working whatever
    directory it is run from.  Collapsed textually, so that a file one directory down
    asking for ``../build/P`` reports ``build/P`` rather than ``examples/../build/P``.
    """
    if value.is_absolute():
        return value
    return Path(os.path.normpath(path.parent / value))


def _spec(key: Any, path: Path) -> tuple[str, Spec]:
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


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


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
    for key, spec in KEYS.items():
        lines.append("")
        lines.extend(_note(spec))
        shown = given.get(key, spec.default)
        if key in chosen:
            lines.extend(_entry(key, chosen[key], commented=False))
        elif spec.example:
            lines.extend(f"# {line}" for line in spec.example)
        elif shown is None:
            # Nothing true to suggest -- there is no default dictionary.  Left as a bare
            # key, which reads as the placeholder it is and, if uncommented as it stands,
            # is reported as giving no value rather than quietly meaning something.
            lines.append(f"# {key}:")
        else:
            lines.extend(_entry(key, shown, commented=True))
    return "\n".join(lines).strip() + "\n"


def _note(spec: Spec) -> list[str]:
    """``spec``'s line of prose, wrapped, and what a file adds to it."""
    note = spec.note
    if not note:
        return []
    if spec.relative:
        note += "; a relative path resolves against this file's own directory"
    return textwrap.wrap(
        note, width=_WIDTH, initial_indent="# ", subsequent_indent="#   "
    )


def _entry(key: str, value: Any, *, commented: bool) -> list[str]:
    """One ``key: value`` entry, or its commented-out form."""
    prefix = "# " if commented else ""
    if isinstance(value, Mapping):
        if not value:
            return [f"{prefix}{key}: {{}}"]
        entries = [
            f"{_scalar(name)}: {_scalar(chosen)}" for name, chosen in value.items()
        ]
        return [f"{prefix}{key}:", *(f"{prefix}{line}" for line in _indented(entries))]
    if isinstance(value, (list, tuple)):
        if not value:
            return [f"{prefix}{key}: []"]
        entries = [f"- {_scalar(entry)}" for entry in value]
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
    if isinstance(value, Enum):
        value = value.value
    text = str(value)
    if _AMBIGUOUS.search(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text
