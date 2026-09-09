"""Every option there is, stated once as a dataclass and reflected into each surface.

An option is spelled three ways -- a command-line flag, a key in a YAML configuration
file, and a commented line in the skeleton ``-i`` writes -- and all three need the same
facts about it: what it is called, what it accepts, what it defaults to, and one line
saying what it does.  Those facts are :class:`Options`' fields, and the three surfaces
are derived from them by :func:`specs`, so adding an option means adding a field and
nothing else.

What a field *accepts* comes from its type rather than from a validator written out
beside it.  ``Path`` takes a path, ``bool`` takes true or false, an :class:`~enum.Enum`
takes one of its values and hands back the member, and a ``Mapping`` takes a table whose
keys are built by the key type.  :class:`Spec`, in a field's metadata, carries only what
a type cannot say: how the key is spelled, which short flags it answers to, whether the
flag reads negatively, and the line of prose.

:class:`Options` is the flag surface, one field per option, which is not quite the same
shape as the :class:`~fprime_dict_to_cpp.plan.Config` the emitters read: a flag may read
negatively where the model reads positively, and three flags decide one
:class:`~fprime_dict_to_cpp.naming.Naming`.  :meth:`Options.settle` is where the two
meet, and where every default that depends on another answer -- the component's name
follows the deployment, the namespace follows the component -- is worked out, once, so
that the answer is the same however the option arrived.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import Any

from .model import ComponentKind, QualifiedName
from .naming import PRESETS, Naming, Qualification, Style
from .plan import Config

__all__ = ["Invalid", "Options", "Rejected", "Shape", "Spec", "specs"]


class Invalid(Exception):
    """What an option will accept, raised for a value that is not one of them.

    The message is the tail of "expected ...", so whoever caught it says where the value
    came from: a key and a file, or the flag that carried it.
    """


class Rejected(Exception):
    """A complaint about one entry of a table, which already names the entry."""


class Shape(Enum):
    """What an option accepts, as far as the surfaces have to tell the difference.

    Derived from the field's type by :func:`specs`; a surface reads it to decide which of
    its own shapes to use -- an ``argparse`` action, or how a skeleton writes a default.
    """

    TEXT = "text"
    PATH = "path"
    FLAG = "flag"
    CHOICE = "choice"
    STYLE = "style"
    """A flag that may instead name something: ``--clang-format`` or ``--clang-format
    Google``."""

    TABLE = "table"
    """A mapping, which a command line has no good shape for; ``--config`` only."""

    LIST = "list"
    """A sequence, for the same reason; ``--config`` only."""


def _unresolved(value: Any) -> Any:
    raise AssertionError("this spec did not come from specs()")


@dataclass(frozen=True)
class Spec:
    """How one option is spelled, and what its type cannot say about it.

    Everything below the divider is filled in by :func:`specs` from the field the spec is
    attached to, so a declaration states only what is not already in the dataclass.
    """

    key: str
    """The configuration key, and the long flag's stem: ``--type-prefix``."""

    note: str = ""
    """One line about the option, used as its ``--help`` text and its skeleton comment."""

    negated_note: str = ""
    """The same line said the other way round, for a :attr:`negated` flag's ``--help``.

    The only thing a key and a flag cannot share: ``packetized: false`` and
    ``--no-packetized`` do one thing, and each has to be described as what it does.
    """

    short: tuple[str, ...] = ()
    """Short flags it also answers to, e.g. ``("-c",)``."""

    group: str = ""
    """Which ``argparse`` argument group it belongs in; empty for the top level."""

    metavar: str = ""
    """What ``--help`` calls its argument, where the type does not say it well enough."""

    example: tuple[str, ...] = ()
    """The lines a skeleton comments out in place of ``key: <default>``.

    For an option whose default is not the useful thing to show: an override table has no
    default at all, and a knob that overrides a preset has one that goes stale the moment
    the preset above it is changed.  Showing the shape, or the choices, does not.
    """

    choices: tuple[str, ...] = ()
    """The values it accepts, where they are not an enumeration's own.  Filled in from
    the type when the type is an :class:`~enum.Enum`."""

    relative: bool = False
    """Whether a relative value in a file resolves against the file's own directory."""

    negated: bool = False
    """Whether the flag reads negatively -- ``--no-packetized`` for ``packetized``.

    A file reads it positively regardless, because a file describes the component it
    wants rather than the switches that get flipped to obtain it.
    """

    positional: bool = False
    flag: bool = True
    """Whether there is a command-line flag for it at all."""

    configurable: bool = True
    """Whether a configuration file may set it."""

    empty: bool = False
    """Whether an empty string is a value rather than an omission."""

    show_default: bool = False
    """Whether ``--help`` should state the default as well as describe the option."""

    # -----------------------------------------------------------------------
    dest: str = ""
    """The field, and so the ``argparse`` destination, this option feeds."""

    default: Any = None
    shape: Shape = Shape.TEXT
    coerce: Callable[[Any], Any] = _unresolved
    """``value`` as this option's type, raising :class:`Invalid` or :class:`Rejected`."""


def option(spec: Spec, **field_arguments: Any) -> Any:
    """A dataclass field carrying ``spec``; ``field_arguments`` are :func:`~dataclasses.field`'s."""
    return dataclasses.field(metadata={"option": spec}, **field_arguments)


def specs(cls: type) -> dict[str, Spec]:
    """Every option ``cls`` declares, resolved against its fields and keyed by key.

    In declaration order, which is the order a skeleton writes them in and the order
    ``--help`` lists them in, so the fields run from what a component *is* to how it is
    spelled to what the run does.
    """
    hints = typing.get_type_hints(cls)
    resolved: dict[str, Spec] = {}
    for member in fields(cls):
        declared = member.metadata.get("option")
        if declared is None:
            continue
        shape, coerce = _accepts(hints[member.name], declared)
        resolved[declared.key] = dataclasses.replace(
            declared,
            dest=member.name,
            default=_default(member),
            choices=declared.choices or _choices(hints[member.name]),
            shape=shape,
            coerce=coerce,
        )
    return resolved


def _default(member: dataclasses.Field[Any]) -> Any:
    """``member``'s default, built afresh where it is built rather than shared."""
    if member.default is not dataclasses.MISSING:
        return member.default
    if member.default_factory is not dataclasses.MISSING:
        return member.default_factory()
    return None


# ---------------------------------------------------------------------------
# What a type accepts
# ---------------------------------------------------------------------------

#: How a table's keys are built from the strings a file writes them as.
_TABLE_KEYS: dict[Any, Callable[[str], Any]] = {
    str: str,
    QualifiedName: QualifiedName.parse,
}


def _members(annotation: Any) -> tuple[Any, ...]:
    """``annotation``'s alternatives, with ``None`` dropped: it means "not given"."""
    if typing.get_origin(annotation) in (types.UnionType, typing.Union):
        return tuple(a for a in typing.get_args(annotation) if a is not type(None))
    return (annotation,)


def _choices(annotation: Any) -> tuple[str, ...]:
    """The values an enumerated type accepts, in the order it declares them."""
    for member in _members(annotation):
        if isinstance(member, type) and issubclass(member, Enum):
            return tuple(str(constant.value) for constant in member)
    return ()


def _accepts(annotation: Any, spec: Spec) -> tuple[Shape, Callable[[Any], Any]]:
    """What the field typed ``annotation`` accepts, and how to build one from a value."""
    alternatives = _members(annotation)
    if set(alternatives) == {bool, str}:
        return Shape.STYLE, _style
    (declared,) = alternatives
    if declared is bool:
        return Shape.FLAG, _flag
    if declared is Path:
        return Shape.PATH, _path
    if spec.choices:
        return Shape.CHOICE, _one_of(spec.choices, str)
    if isinstance(declared, type) and issubclass(declared, Enum):
        return Shape.CHOICE, _one_of(_choices(declared), declared)
    if typing.get_origin(declared) in (
        Mapping,
        dict,
        typing.get_origin(Mapping[Any, Any]),
    ):
        key, value = typing.get_args(declared)
        return Shape.TABLE, _table(key, value)
    if typing.get_origin(declared) in (
        Sequence,
        tuple,
        typing.get_origin(Sequence[Any]),
    ):
        return Shape.LIST, _list
    if declared is str:
        return Shape.TEXT, _text(empty=spec.empty)
    raise TypeError(f"no option accepts {annotation!r}")


def _text(*, empty: bool) -> Callable[[Any], str]:
    def coerce(value: Any) -> str:
        if not isinstance(value, str) or (not empty and not value.strip()):
            raise Invalid("a string" if empty else "a name")
        return value

    return coerce


def _path(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise Invalid("a path")
    return Path(value).expanduser()


def _flag(value: Any) -> bool:
    # YAML has already turned true/false/yes/no into a bool, and a command line only ever
    # hands this a flag's presence; anything else is a mistake worth naming rather than a
    # truthiness question.
    if not isinstance(value, bool):
        raise Invalid("true or false")
    return value


def _style(value: Any) -> bool | str:
    """``true`` for a tool's own default style, or a named style."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value
    raise Invalid("true, false, or a style name")


def _one_of(
    choices: tuple[str, ...], build: Callable[[str], Any]
) -> Callable[[Any], Any]:
    def coerce(value: Any) -> Any:
        if not isinstance(value, str) or value not in choices:
            raise Invalid("one of " + ", ".join(choices))
        return build(value)

    return coerce


def _table(key: Any, value_type: Any) -> Callable[[Any], dict[Any, Any]]:
    """A table keyed by dictionary name, checked as far as one file can be.

    Whether a name is a *usable* identifier, and whether the dictionary has anything by
    that name, are questions for :func:`~fprime_dict_to_cpp.plan.resolve`, which is where
    the answers live and where the Python API needs them checked too.

    A port table's values also accept ``true`` and ``false``, which is what makes hooking a
    port a one-word answer where naming it is not the interesting part.
    """
    build = _TABLE_KEYS[key]
    switchable = set(_members(value_type)) == {bool, str}
    accepts = (
        "a mapping of dictionary names to a port name, or to true or false"
        if switchable
        else "a mapping of dictionary names to generated names"
    )
    wanted = "a name, true or false" if switchable else "a name"

    def coerce(value: Any) -> dict[Any, Any]:
        if not isinstance(value, Mapping):
            raise Invalid(accepts)
        table: dict[Any, Any] = {}
        for name, chosen in value.items():
            if not isinstance(name, str) or not name.strip():
                raise Invalid(f"dictionary names as its keys, found {name!r}")
            if switchable and isinstance(chosen, bool):
                table[build(name.strip())] = chosen
                continue
            if not isinstance(chosen, str) or not chosen.strip():
                raise Rejected(f"{name}: expected {wanted}, found {chosen!r}")
            table[build(name.strip())] = chosen.strip()
        return table

    return coerce


def _list(value: Any) -> tuple[str, ...]:
    """A list of names or patterns.  A bare string is refused rather than wrapped.

    Because ``omit: Ref.dpDemo.*`` reads as one entry and is almost certainly meant as one,
    but taking it silently would make ``omit: [a, b]`` and ``omit: a`` two spellings of the
    same shape, and one of them would eventually be written where a list belonged.
    """
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise Invalid("a list of dictionary names or patterns")
    entries: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise Rejected(
                f"entry {index + 1}: expected a dictionary name or pattern, "
                f"found {entry!r}"
            )
        entries.append(entry.strip())
    return tuple(entries)


# ---------------------------------------------------------------------------
# The options themselves
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """Everything the command line and a configuration file can set, one field each."""

    dictionary: Path | None = option(
        Spec(
            "dictionary",
            positional=True,
            relative=True,
            note="the remote deployment's JSON dictionary",
        ),
        default=None,
    )

    # Neither of the next two is a configuration, so neither has a key: a file that
    # selects another file, or that decides to start asking questions, is a surprise.
    interactive: bool = option(
        Spec(
            "interactive",
            short=("-i",),
            configurable=False,
            note="set up a new component: ask the few questions that shape one, generate "
            "it, and write a configuration file beside it holding the answers with every "
            "other option listed and commented out.  Iterate through that file "
            "afterwards; this refuses to write over one that is already there",
        ),
        default=False,
    )

    config: Path | None = option(
        Spec(
            "config",
            configurable=False,
            metavar="FILE",
            note="read these options from a YAML file, one key per option named as the "
            "option is; anything also given on the command line wins over the file",
        ),
        default=None,
    )

    output: Path | None = option(
        Spec(
            "output",
            short=("-o",),
            relative=True,
            note="the directory to write the component into, one named after the "
            "component by default",
        ),
        default=None,
    )

    component: str | None = option(
        Spec(
            "component",
            short=("-c",),
            note="the generated component's name; <Deployment>Proxy by default",
        ),
        default=None,
    )

    namespace: str | None = option(
        Spec(
            "namespace",
            short=("-n",),
            note="the FPP module holding the component and its types; the component's "
            "own name by default",
        ),
        default=None,
    )

    kind: ComponentKind = option(
        Spec(
            "kind",
            short=("-k",),
            show_default=True,
            note="active: decode on the component's own thread.  queued: on whatever "
            "drives its run port.  passive: on the caller's thread, with guarded commands",
        ),
        default=ComponentKind.ACTIVE,
    )

    naming: str = option(
        Spec(
            "naming",
            group="naming",
            show_default=True,
            choices=tuple(sorted(PRESETS)),
            note="how Ref.dpDemo.SelectColor collapses into one member name.  instance: "
            "dpDemo_SelectColor.  qualified: Ref_dpDemo_SelectColor.  source-case: "
            "RefDpDemoSelectColor, keeping the member's own convention, so a "
            "SCREAMING_SNAKE member stays screaming.  member: SelectColor",
        ),
        default="instance",
    )

    name_qualification: Qualification | None = option(
        Spec(
            "name-qualification",
            group="naming",
            note="how much of the instance path a name keeps, whatever the preset above "
            "says",
            example=(
                "name-qualification: full  # or "
                + ", or ".join(
                    q.value for q in Qualification if q is not Qualification.FULL
                ),
            ),
        ),
        default=None,
    )

    name_style: Style | None = option(
        Spec(
            "name-style",
            group="naming",
            note="how the kept segments join, whatever the preset above says",
            example=("name-style: source-case  # or underscore",),
        ),
        default=None,
    )

    name_overrides: Mapping[QualifiedName, str] = option(
        Spec(
            "names",
            flag=False,
            note="members named outright, in place of whatever the scheme above calls "
            "them; a key matching nothing in the dictionary is an error, not a no-op",
            example=("names:", "  Ref.dpDemo.SelectColor: SelectDemoColor"),
        ),
        default_factory=dict,
    )

    type_name_overrides: Mapping[str, str] = option(
        Spec(
            "type-names",
            flag=False,
            note="generated types named outright, in place of the flattened qualified "
            "name; used as written, so type-prefix is not applied to one",
            example=(
                "type-names:",
                "  Ref.DpDemo.ColorEnum: DemoColor  # not Ref_DpDemo_ColorEnum",
            ),
        ),
        default_factory=dict,
    )

    omit: Sequence[str] = option(
        Spec(
            "omit",
            flag=False,
            note="members the component does not declare, as dictionary names or globs; "
            "an omitted member keeps a port hooked onto it and loses only its command, "
            "event or telemetry declaration, and one matching nothing is an error",
            example=(
                "omit:",
                '  - "Ref.dpDemo.*"',
                '  - "*_PRM_SET"',
                "  - CdhCore.cmdDisp.CMD_NO_OP",
            ),
        ),
        default_factory=tuple,
    )

    command_ports: Mapping[QualifiedName, bool | str] = option(
        Spec(
            "command-ports",
            flag=False,
            note="commands to hook an input port onto: invoking the port forwards the "
            "command to the remote, with no ground command in the way.  true takes the "
            "derived port name, a string names the port outright",
            example=(
                "command-ports:",
                "  Ref.dpDemo.SelectColor: true  # -> dpDemo_SelectColor_cmdIn",
                "  CdhCore.cmdDisp.CMD_NO_OP: pingRemote",
            ),
        ),
        default_factory=dict,
    )

    event_ports: Mapping[QualifiedName, bool | str] = option(
        Spec(
            "event-ports",
            flag=False,
            note="events to hook an output port onto, which fires with the event's own "
            "arguments when one arrives from the remote; spelled as command-ports is",
            example=(
                "event-ports:",
                "  CdhCore.cmdDisp.OpCodeDispatched: true",
            ),
        ),
        default_factory=dict,
    )

    telemetry_ports: Mapping[QualifiedName, bool | str] = option(
        Spec(
            "telemetry-ports",
            flag=False,
            note="telemetry channels to hook an output port onto, which fires with the "
            "value and the remote's own timestamp; spelled as command-ports is",
            example=(
                "telemetry-ports:",
                "  CdhCore.cmdDisp.CommandsDispatched: true",
            ),
        ),
        default_factory=dict,
    )

    packet_ports: Mapping[str, bool | str] = option(
        Spec(
            "packet-ports",
            flag=False,
            note="telemetry packets to hook an output port onto, keyed <packet "
            "set>.<packet>; the port carries every channel of the packet in wire order "
            "and then the one timestamp they share",
            example=(
                "packet-ports:",
                "  RefPackets.CDH: true",
            ),
        ),
        default_factory=dict,
    )

    type_prefix: str = option(
        Spec(
            "type-prefix",
            group="naming",
            empty=True,
            note="prepended to every generated type name, for a leaf name that would "
            "otherwise clash with a framework type",
        ),
        default="",
    )

    include_prefix: str | None = option(
        Spec(
            "include-prefix",
            group="output",
            note="path prefix for the #include of the autocoded base class, normally the "
            "component's directory relative to the project root; an include path rather "
            "than a file path, and the output directory's name by default",
        ),
        default=None,
    )

    packetized: bool = option(
        Spec(
            "packetized",
            group="output",
            negated=True,
            note="decode FW_PACKET_PACKETIZED_TLM as well as channelized telemetry",
            negated_note="do not decode packetized telemetry "
            "(FW_PACKET_PACKETIZED_TLM)",
        ),
        default=True,
    )

    diagnostics: bool = option(
        Spec(
            "diagnostics",
            group="output",
            negated=True,
            note="declare the proxy's own health telemetry and events, without which a "
            "dropping link looks the same as a quiet one",
            negated_note="do not declare the proxy's own health telemetry and events",
        ),
        default=True,
    )

    cmake: bool = option(
        Spec(
            "cmake",
            group="output",
            note="rewrite CMakeLists.txt from the current file list.  Off, so that "
            "whatever this project has added to it survives a regeneration",
        ),
        default=False,
    )

    clang_format: bool | str = option(
        Spec(
            "clang-format",
            group="output",
            metavar="STYLE",
            note="run the generated C++ through clang-format; true for its own default "
            "style, or name one",
        ),
        default=False,
    )

    cross_check: bool = option(
        Spec(
            "cross-check",
            group="output",
            note="also parse the dictionary with fprime-gds and report any disagreement",
        ),
        default=False,
    )

    dry_run: bool = option(
        Spec(
            "dry-run",
            group="output",
            note="report what would be generated without writing anything",
        ),
        default=False,
    )

    quiet: bool = option(
        Spec(
            "quiet",
            short=("-q",),
            group="output",
            note="only report warnings and errors",
        ),
        default=False,
    )

    # -----------------------------------------------------------------------
    # Settling what one answer implies about another
    # -----------------------------------------------------------------------

    @classmethod
    def from_namespace(cls, namespace: Any) -> Options:
        """What ``argparse`` parsed, as the structure the rest of the tool reads."""
        return cls(**{f.name: getattr(namespace, f.name) for f in fields(cls)})

    def component_for(self, deployment: str) -> str:
        """The component's name: the one asked for, or one named for the deployment."""
        return self.component or f"{deployment}Proxy"

    def output_for(self, component: str) -> Path:
        """Where the component goes: the directory asked for, or one named for it."""
        return self.output or Path(component)

    def include_prefix_for(self, output: Path) -> str:
        """The prefix its ``#include`` uses: the one asked for, or the directory's name."""
        return self.include_prefix if self.include_prefix is not None else output.name

    def settle(self, deployment: str) -> tuple[Path, Config]:
        """The output directory, and the configuration the emitters read.

        Every default that depends on another answer is worked out here, so that a run
        configured by flags and a run configured by a file agree by construction.
        """
        component = self.component_for(deployment)
        output = self.output_for(component)
        return output, Config(
            component=component,
            namespace=self.namespace or component,
            kind=self.kind,
            naming=Naming.resolve(
                self.naming,
                qualification=self.name_qualification,
                style=self.name_style,
            ),
            name_overrides=self.name_overrides,
            type_prefix=self.type_prefix,
            type_name_overrides=self.type_name_overrides,
            omit=self.omit,
            command_ports=self.command_ports,
            event_ports=self.event_ports,
            telemetry_ports=self.telemetry_ports,
            packet_ports=self.packet_ports,
            packetized=self.packetized,
            diagnostics=self.diagnostics,
            include_prefix=self.include_prefix_for(output),
        )
