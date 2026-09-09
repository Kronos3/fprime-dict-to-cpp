"""Turning dotted dictionary names into F Prime component member names.

A dictionary names a command ``Ref.dpDemo.SelectColor`` -- a topology instance path
plus the member as its component declared it.  A proxy has to re-declare that member
on *one* component, so every name in the dictionary has to collapse into a single
identifier, and the collapse has to be unique, legal, and stable across regenerations.

Two orthogonal knobs decide the shape:

:class:`Qualification`
    how much of the instance path to keep.
:class:`Style`
    how to join what is kept -- verbatim segments under underscores, or re-cased to
    match the member's own convention.

:data:`PRESETS` names the useful combinations.  Whatever is chosen, :func:`mangle_all`
guarantees uniqueness: a group of names that would collide is re-qualified, and only
if full qualification still collides does a numeric suffix appear.

A scheme decides every name at once, which is the point of it, but no scheme suits every
member of a real dictionary.  ``overrides`` names individual members outright:
:func:`mangle_all` assigns those verbatim and mangles everything else *around* them, so
an automatic name that would have collided with a chosen one is the one that moves.

Uniqueness is enforced **case-insensitively and across all member kinds at once**.
That is stricter than FPP needs, and deliberately so: ``fpp-to-cpp`` derives
``EVENTID_<UPPER>``, ``OPCODE_<UPPER>`` and ``CHANNELID_<UPPER>`` enumerators by
upper-casing the member name, so two members differing only in case would generate
two identical C++ enumerators.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from .model import NamingError, QualifiedName

__all__ = [
    "MEMBER_IDENTIFIER",
    "Naming",
    "PRESETS",
    "Qualification",
    "Style",
    "check_member_identifier",
    "join_camel",
    "join_screaming",
    "mangle_all",
    "split_words",
]


class Qualification(Enum):
    """How much of a name's instance path survives into the identifier."""

    MEMBER = "member"
    """Just the member: ``Ref.dpDemo.SelectColor`` -> ``SelectColor``."""

    INSTANCE = "instance"
    """The innermost instance too: -> ``dpDemo_SelectColor``."""

    FULL = "full"
    """The whole path: -> ``Ref_dpDemo_SelectColor``."""

    def segments(self, name: QualifiedName) -> tuple[str, ...]:
        """The name's segments this qualification keeps, in order."""
        if self is Qualification.MEMBER:
            return (name.member,)
        if self is Qualification.INSTANCE:
            return name.parts[-2:]
        return name.parts

    def escalated(self) -> Qualification | None:
        """The next-broader qualification, or ``None`` at :attr:`FULL`."""
        order = [Qualification.MEMBER, Qualification.INSTANCE, Qualification.FULL]
        index = order.index(self)
        return order[index + 1] if index + 1 < len(order) else None


class Style(Enum):
    """How the kept segments are joined."""

    UNDERSCORE = "underscore"
    """Join the segments verbatim with ``_``: ``dpDemo_SelectColor``."""

    SOURCE_CASE = "source-case"
    """Re-case every segment to match the member's own convention, so a
    ``SCREAMING_SNAKE`` member stays screaming (``CDH_CORE_CMD_DISP_CMD_NO_OP``) and a
    ``CamelCase`` member stays camel (``RefDpDemoSelectColor``).

    A member with no coherent convention -- ``BA_DrainBuffers``, ``CS_LoadCommands``
    -- has nothing to preserve, so it falls back to :attr:`UNDERSCORE`.
    """


#: The named combinations exposed on the command line.
PRESETS: dict[str, tuple[Qualification, Style]] = {
    "instance": (Qualification.INSTANCE, Style.UNDERSCORE),
    "qualified": (Qualification.FULL, Style.UNDERSCORE),
    "source-case": (Qualification.FULL, Style.SOURCE_CASE),
    "member": (Qualification.MEMBER, Style.UNDERSCORE),
}


@dataclass(frozen=True)
class Naming:
    """A name-mangling configuration."""

    qualification: Qualification = Qualification.INSTANCE
    style: Style = Style.UNDERSCORE

    @classmethod
    def preset(cls, name: str) -> Naming:
        """Build a configuration from a :data:`PRESETS` key."""
        try:
            qualification, style = PRESETS[name]
        except KeyError:
            known = ", ".join(sorted(PRESETS))
            raise ValueError(
                f"unknown naming preset {name!r}; known: {known}"
            ) from None
        return cls(qualification, style)

    @classmethod
    def resolve(
        cls,
        preset: str,
        *,
        qualification: Qualification | None = None,
        style: Style | None = None,
    ) -> Naming:
        """A configuration from a :data:`PRESETS` key, with either knob overridden.

        The preset is the coarse answer and the two knobs are the fine ones, which is how
        both the command line and a configuration file state them: ``--naming member``
        picks a shape, ``--name-style source-case`` adjusts it.
        """
        base = cls.preset(preset)
        return cls(
            qualification=qualification or base.qualification,
            style=style or base.style,
        )

    def apply(
        self, name: QualifiedName, *, qualification: Qualification | None = None
    ) -> str:
        """Mangle one name, optionally overriding the qualification.

        The override is how :func:`mangle_all` escalates a colliding group without
        disturbing names that do not collide.
        """
        segments = (qualification or self.qualification).segments(name)
        if self.style is Style.UNDERSCORE:
            return "_".join(segments)
        return _source_case(segments, name.member)


# ---------------------------------------------------------------------------
# Word splitting and re-casing
# ---------------------------------------------------------------------------

_SCREAMING = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")
_UPPER_CAMEL = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_LOWER_CAMEL = re.compile(r"^[a-z][A-Za-z0-9]*$")

#: Split an identifier into words: on underscores, on a lower-to-upper transition, and
#: between a run of capitals and a following capitalised word (``CDHCore`` -> ``CDH``,
#: ``Core``).  Trailing digit runs stay attached to their word, so ``CPU_00`` keeps
#: ``00`` as its own segment via the underscore but ``SG1`` stays one word.
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def split_words(segment: str) -> list[str]:
    """Split one identifier segment into its constituent words.

    ``"dpDemo"`` -> ``["dp", "Demo"]``, ``"CDHCore"`` -> ``["CDH", "Core"]``,
    ``"CMD_NO_OP"`` -> ``["CMD", "NO", "OP"]``.
    """
    return [word for part in segment.split("_") for word in _WORD.findall(part)]


def join_screaming(segments: Sequence[str]) -> str:
    """Join segments as ``SCREAMING_SNAKE_CASE``."""
    words = [word.upper() for segment in segments for word in split_words(segment)]
    return "_".join(words)


def join_camel(segments: Sequence[str], *, lower_first: bool = False) -> str:
    """Join segments as ``CamelCase``, optionally lower-casing the first word."""
    words = [word for segment in segments for word in split_words(segment)]
    if not words:
        return ""
    # A word that is already all-caps is an acronym; leave it alone rather than
    # flattening SG1 into Sg1.
    cased = [w if w.isupper() else w[:1].upper() + w[1:] for w in words]
    if lower_first:
        head = cased[0]
        cased[0] = head.lower() if head.isupper() else head[:1].lower() + head[1:]
    return "".join(cased)


def _source_case(segments: Sequence[str], member: str) -> str:
    """Re-case ``segments`` to match ``member``'s own naming convention."""
    if _SCREAMING.match(member):
        return join_screaming(segments)
    if _UPPER_CAMEL.match(member):
        return join_camel(segments)
    if _LOWER_CAMEL.match(member):
        return join_camel(segments, lower_first=True)
    # Mixed conventions (BA_DrainBuffers, CS_LoadCommands) have nothing to preserve.
    return "_".join(segments)


# ---------------------------------------------------------------------------
# Explicitly chosen names
# ---------------------------------------------------------------------------

#: What an explicitly chosen member name has to look like.
#:
#: Stricter than FPP, which would take a leading underscore and, through its ``$``
#: escape, even a reserved word.  ``fpp-to-cpp`` upper-cases a member name into
#: ``EVENTID_<UPPER>`` and friends, though, so a leading underscore becomes a
#: double-underscore-or-underscore-capital identifier, which C++ reserves to the
#: implementation.  A name nobody chose is mangled from dictionary segments and cannot
#: come out this way; a name somebody typed can, so it is checked.
MEMBER_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def check_member_identifier(identifier: str, context: str) -> None:
    """Raise :class:`~.model.NamingError` unless ``identifier`` can name a member.

    ``context`` says whose name it is, e.g. ``"name override for Ref.dpDemo.SendFile"``.
    """
    if not MEMBER_IDENTIFIER.match(identifier):
        raise NamingError(
            f"{context}: {identifier!r} is not a usable member name; it has to start "
            "with a letter and hold only letters, digits and underscores"
        )


# ---------------------------------------------------------------------------
# Collision-free assignment
# ---------------------------------------------------------------------------


def mangle_all(
    names: Iterable[QualifiedName],
    naming: Naming,
    *,
    reserved: Iterable[str] = (),
    overrides: Mapping[QualifiedName, str] | None = None,
) -> dict[QualifiedName, str]:
    """Assign every name a unique identifier under ``naming``.

    ``reserved`` are identifiers already taken -- the generator's own port names, for
    instance -- which no member may be assigned.

    ``overrides`` are names chosen outright, which are assigned verbatim and are taken
    before anything is mangled.  A name in ``overrides`` that is not in ``names`` is
    ignored here; :func:`~fprime_dict_to_cpp.plan.resolve` is what reports it, since only
    the whole dictionary can say whether it matches nothing at all or just nothing of
    this one kind.

    Escalation is per-collision-group, so a dictionary whose names are already
    distinct at the chosen qualification gets exactly the short names asked for, and
    only the ambiguous ones grow.  Ordering is by the name itself, so the result does
    not depend on the dictionary's section order.
    """
    chosen = overrides or {}
    ordered = sorted(set(names), key=lambda n: n.parts)
    taken = {r.casefold() for r in reserved}
    assigned: dict[QualifiedName, str] = {}

    # The chosen names first, and reserved as they are assigned, so that a mangled name
    # colliding with one of them is the name that gives way.
    for name in ordered:
        if name in chosen:
            assigned[name] = chosen[name]
            taken.add(chosen[name].casefold())

    # Escalate whole groups together until each group is a single name, or until
    # there is nothing broader to try.
    pending: list[tuple[Qualification, list[QualifiedName]]] = [
        (naming.qualification, [n for n in ordered if n not in chosen])
    ]
    unresolved: list[tuple[str, list[QualifiedName]]] = []
    while pending:
        qualification, group = pending.pop()
        buckets: dict[str, list[QualifiedName]] = {}
        for name in group:
            candidate = naming.apply(name, qualification=qualification)
            buckets.setdefault(candidate.casefold(), []).append(name)
        for folded, members in buckets.items():
            candidate = naming.apply(members[0], qualification=qualification)
            if len(members) == 1 and folded not in taken:
                taken.add(folded)
                assigned[members[0]] = candidate
                continue
            broader = qualification.escalated()
            if broader is not None:
                pending.append((broader, members))
            else:
                unresolved.append((candidate, members))

    # Fully qualified and still ambiguous, or colliding with a reserved name: fall
    # back to a deterministic numeric suffix.
    for candidate, members in sorted(unresolved, key=lambda item: item[0]):
        for name in sorted(members, key=lambda n: n.parts):
            assigned[name] = _unique_suffix(candidate, taken)
    return assigned


def _unique_suffix(base: str, taken: set[str]) -> str:
    """``base`` if free, else ``base_2``, ``base_3``, ... registering the result."""
    if base.casefold() not in taken:
        taken.add(base.casefold())
        return base
    index = 2
    while f"{base}_{index}".casefold() in taken:
        index += 1
    chosen = f"{base}_{index}"
    taken.add(chosen.casefold())
    return chosen
