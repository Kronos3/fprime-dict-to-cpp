"""How a dictionary type is spelled in the generated FPP and the generated C++.

The proxy re-declares every type the remote's dictionary references, under its own
namespace, for two reasons.  The obvious one is that the remote's FPP is not
necessarily in the target build.  The load-bearing one is that a dictionary records
the remote's *platform* widths -- its ``FwOpcodeType``, its ``FwSizeType`` -- and a
proxy has to deserialize with those, not with whatever the local ``FpConfig.h`` says.
Re-rooting each alias onto its resolved built-in is what pins the wire format.

Names are **flattened**, not nested: ``Ref.DpDemo.ColorEnum`` becomes
``Ref_DpDemo_ColorEnum``.  That is forced by ``fpp-to-cpp``, which derives a generated
file name from a type's *leaf* name plus its enclosing component (if any) and ignores
enclosing modules entirely -- so two same-leaf types in different modules abort the
autocoder with ``error: duplicate C++ file``.  The Ref dictionary has two such pairs
(``DpReqType``, ``Enabled``), so this is not hypothetical.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from .model import (
    AliasDef,
    ArrayDef,
    Dictionary,
    DictionaryError,
    EnumDef,
    NamingError,
    StructDef,
    TypeDef,
    TypeKind,
    TypeRef,
)

__all__ = [
    "CPP_KEYWORDS",
    "Context",
    "Spelling",
    "TYPE_IDENTIFIER",
    "TypeNamer",
    "check_type_identifier",
    "safe_identifier",
]


class Context(Enum):
    """Where a value is being spelled, which decides how a string is typed.

    ``fpp-to-cpp`` hands a command's string argument to the handler as
    ``Fw::CmdStringArg``, but declares an event argument and a telemetry value as
    ``const Fw::StringBase&``.  A local the generator declares in order to
    *deserialize* into needs a concrete sized type, which is what :attr:`LOCAL` is for.
    """

    COMMAND = "command"
    EVENT = "event"
    TELEMETRY = "telemetry"
    LOCAL = "local"


#: C++ keywords, per the C++20 keyword table plus the alternative operator spellings.
#: An enum constant, struct member or type name reaches the generated C++ verbatim, so
#: a dictionary that spells one of these would produce code that does not compile even
#: though the FPP is legal.
CPP_KEYWORDS = frozenset("""
    alignas alignof and and_eq asm atomic_cancel atomic_commit atomic_noexcept auto
    bitand bitor bool break case catch char char8_t char16_t char32_t class compl
    concept const consteval constexpr constinit const_cast continue co_await
    co_return co_yield decltype default delete do double dynamic_cast else enum
    explicit export extern false float for friend goto if inline int long mutable
    namespace new noexcept not not_eq nullptr operator or or_eq private protected
    public register reinterpret_cast requires return short signed sizeof static
    static_assert static_cast struct switch template this thread_local throw true try
    typedef typeid typename union unsigned using virtual void volatile wchar_t while
    xor xor_eq
    """.split())

#: Names FPP uses *implicitly*, which a generated definition must therefore not shadow.
#:
#: Declaring a string type is an implied use of ``FwSizeStoreType`` (the length prefix)
#: and, for an unsized string, of ``FW_FIXED_LENGTH_STRING_SIZE``.  A definition of
#: either inside the proxy's module makes every string in that module fail to compile
#: with ``invalid use of symbol ...: it shadows ... here``.  The set is transcribed from
#: the compiler's ``ConstructImpliedUseMap.scala``; the other implied uses it lists are
#: module-qualified (``Fw.Log``, ``Fw.Tlm``) or belong to constructs this generator does
#: not emit (state machines, deployment topologies), so flattening already avoids them.
IMPLIED_USE_NAMES = frozenset({"FwSizeStoreType", "FW_FIXED_LENGTH_STRING_SIZE"})

#: Applied to a generated name that would otherwise shadow an implied use.
SHADOW_ESCAPE_PREFIX = "Remote_"

_LEADING_UNDERSCORE = re.compile(r"^_+")


def safe_identifier(name: str) -> str:
    """``name``, adjusted if it would be illegal or reserved in the generated C++.

    Two hazards, both of which reach C++ verbatim through an enum constant, a struct
    member or a type name: a C++ keyword, and a leading underscore (reserved to the
    implementation).  Neither appears in the Ref dictionary, but neither is prevented
    by FPP, which accepts ``enum E { class, new }`` and generates ``class = 0,``.
    """
    stripped = _LEADING_UNDERSCORE.sub("", name)
    if stripped != name:
        name = f"u{stripped}" if stripped else "u"
    return f"{name}_" if name in CPP_KEYWORDS else name


#: What an explicitly chosen type name has to look like.  See
#: :func:`check_type_identifier` for why it is stricter than FPP.
TYPE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def check_type_identifier(identifier: str, context: str) -> None:
    """Raise :class:`~.model.NamingError` unless ``identifier`` can name a type.

    A derived name is put right by :func:`safe_identifier` and, for an implied use, by
    :data:`SHADOW_ESCAPE_PREFIX`.  A chosen one is not quietly rewritten -- being told
    that ``FwSizeStoreType`` would take every string in the module down with it is more
    use than getting ``Remote_FwSizeStoreType`` and wondering where it came from.

    ``context`` says whose name it is, e.g. ``"type name override for Ref.DpDemo.Color"``.
    """
    if not TYPE_IDENTIFIER.match(identifier):
        raise NamingError(
            f"{context}: {identifier!r} is not a usable type name; it has to start with "
            "a letter and hold only letters, digits and underscores"
        )
    if identifier in CPP_KEYWORDS:
        raise NamingError(
            f"{context}: {identifier!r} is a C++ keyword, and a type name reaches the "
            "generated C++ verbatim"
        )
    if identifier in IMPLIED_USE_NAMES:
        raise NamingError(
            f"{context}: {identifier!r} is a name FPP uses implicitly; defining it in "
            "the proxy's module would stop every string in that module from compiling"
        )


@dataclass(frozen=True)
class TypeNamer:
    """Maps a dictionary type's qualified name onto its generated spellings."""

    namespace: str
    """The FPP module the generated types and component live in."""

    prefix: str = ""
    """Prepended to every generated type name.

    Empty by default.  A generated ``FwSizeType`` produces
    ``FwSizeTypeAliasAc.hpp``, whose *name* matches the framework's own -- harmless,
    since both are included by distinct project-relative paths, but a project that
    manages to put the two directories on one include path can set a prefix to be
    certain.
    """

    overrides: Mapping[str, str] = field(default_factory=dict)
    """Types named outright, keyed by their dictionary qualified name.

    Flattening is unambiguous but verbose, and the result is what every use site in the
    generated FPP and C++ reads, so a type worth naming can be named.  An override is the
    name that comes out: :attr:`prefix` is not applied to it, and neither is the
    shadow escape, because both exist to keep a *derived* name out of trouble and an
    override that would land in trouble is refused outright by
    :func:`check_type_identifier`.
    """

    def flat(self, qualified: str) -> str:
        """The generated type's own name: ``Ref.DpDemo.ColorEnum`` -> ``Ref_DpDemo_ColorEnum``."""
        chosen = self.overrides.get(qualified)
        if chosen is not None:
            return chosen
        joined = "_".join(part for part in qualified.split(".") if part)
        name = f"{self.prefix}{joined}"
        if name in IMPLIED_USE_NAMES:
            name = f"{SHADOW_ESCAPE_PREFIX}{name}"
        return safe_identifier(name)

    def fpp(self, qualified: str) -> str:
        """How the generated FPP refers to the type from inside :attr:`namespace`."""
        return self.flat(qualified)

    def cpp(self, qualified: str) -> str:
        """How the generated C++ refers to the type: ``::RefProxy::Ref_DpDemo_ColorEnum``.

        Rooted at global scope, deliberately.  ``fpp-to-cpp`` writes
        ``RefProxy::Ref_DpDemo_ColorEnum`` in the base class and gets away with it
        because that class is called ``RefProxyComponentBase``.  The implementation class
        is called ``RefProxy``, so inside it the injected-class-name hides the namespace
        of the same name and ``RefProxy::`` starts looking in the class -- which is the
        common case, since a component and its module are usually named alike.  The
        leading ``::`` sidesteps that without depending on what the dictionary happens to
        name things.  It denotes the same type either way, so an override still overrides.
        """
        scope = "::".join(part for part in self.namespace.split(".") if part)
        return f"::{scope}::{self.flat(qualified)}" if scope else self.flat(qualified)


class Spelling:
    """Spells dictionary types for the generated FPP and C++."""

    def __init__(self, dictionary: Dictionary, namer: TypeNamer) -> None:
        self._dictionary = dictionary
        self.namer = namer
        self._reachable_cache: set[str] | None = None

    # -- FPP ----------------------------------------------------------

    def fpp_type(self, ref: TypeRef) -> str:
        """The FPP spelling of ``ref`` at a use site.

        ``string`` carries its capacity, since the dictionary always records a
        concrete size and FPP's bare ``string`` would silently mean the default.
        """
        if ref.kind is TypeKind.STRING:
            return f"string size {self._string_size(ref)}"
        if ref.kind is TypeKind.QUALIFIED:
            return self.namer.fpp(ref.name)
        return ref.name

    # -- C++ ----------------------------------------------------------

    def cpp_type(self, ref: TypeRef, context: Context) -> str:
        """The C++ *value* type of ``ref``: what a local declaration would say."""
        if ref.kind is TypeKind.STRING:
            return self._cpp_string(ref, context)
        if ref.kind is TypeKind.QUALIFIED:
            return self.namer.cpp(ref.name)
        return ref.name

    def cpp_param(self, ref: TypeRef, context: Context) -> str:
        """The C++ spelling of ``ref`` as a formal parameter of a generated function.

        Mirrors ``fpp-to-cpp``: built-ins and aliases-to-built-ins pass by value,
        everything else by ``const`` reference.  An override whose parameter list
        disagrees would not override anything.
        """
        if ref.kind is TypeKind.QUALIFIED and self.resolves_to_builtin(ref.name):
            return self.namer.cpp(ref.name)
        if ref.kind is TypeKind.QUALIFIED or ref.kind is TypeKind.STRING:
            return f"const {self.cpp_type(ref, context)}&"
        return ref.name

    def serialized_size(self, ref: TypeRef) -> str:
        """A C++ constant expression for ``ref``'s **maximum** serialized size.

        Spelled the way ``fpp-to-cpp`` spells it in a generated telemetry packet list,
        so the two agree by construction.  Needed for packetized telemetry, where every
        channel occupies a fixed-width slot of exactly this many bytes regardless of
        how many bytes its value actually took.

        Every non-built-in size is asked of the *generated C++ type* rather than computed
        from the dictionary.  That matters for an alias to a string: ``type X = string
        size 80`` generates ``using X = Fw::String``, whose capacity is
        ``FW_FIXED_LENGTH_STRING_SIZE`` and not 80, so a size computed from the
        dictionary would disagree with the slot the sender actually left.
        """
        if ref.kind is TypeKind.STRING:
            return f"Fw::StringBase::STATIC_SERIALIZED_SIZE({self._string_size(ref)})"
        if ref.kind is TypeKind.QUALIFIED:
            # A scalar alias is a using-declaration, so it has no SERIALIZED_SIZE; every
            # other generated type is a class that does.
            if self.resolves_to_builtin(ref.name):
                return f"sizeof({self.namer.cpp(ref.name)})"
            return f"{self.namer.cpp(ref.name)}::SERIALIZED_SIZE"
        return f"sizeof({ref.name})"

    # -- internals ----------------------------------------------------

    def _cpp_string(self, ref: TypeRef, context: Context) -> str:
        """The concrete or abstract string type appropriate to ``context``."""
        if context is Context.COMMAND:
            return "Fw::CmdStringArg"
        if context is Context.LOCAL:
            # An exactly-sized stack string, so capacity matches the remote's declared
            # size rather than whatever FW_LOG_STRING_MAX_SIZE happens to be locally.
            return f"Fw::StringTemplate<{self._string_size(ref)}>"
        return "Fw::StringBase"

    def _string_size(self, ref: TypeRef) -> int:
        if ref.size is None:
            raise DictionaryError(f"string type {ref.name!r} has no size")
        return ref.size

    def resolves_to_builtin(self, qualified: str) -> bool:
        """Whether ``qualified`` is an alias whose underlying type is a built-in.

        ``fpp-to-cpp`` passes such a type by value, since it is a ``using`` alias for
        a scalar.  An alias to a *string* is a class, so it goes by reference.
        """
        definition = self._dictionary.type_def(qualified)
        if not isinstance(definition, AliasDef):
            return False
        return definition.underlying.kind in (
            TypeKind.INTEGER,
            TypeKind.FLOAT,
            TypeKind.BOOL,
        )

    # -- selection and ordering ---------------------------------------

    def roots(self) -> set[str]:
        """The qualified type names the dictionary's members refer to directly."""
        names: set[str] = set()
        for command in self._dictionary.commands:
            names.update(_qualified(p.type for p in command.params))
        for event in self._dictionary.events:
            names.update(_qualified(p.type for p in event.params))
        for channel in self._dictionary.channels:
            names.update(_qualified([channel.type]))
        return names

    def missing_type_defs(self) -> set[str]:
        """Reachable type names with no ``typeDefinitions`` entry.

        A dictionary that references a type it does not define cannot produce FPP that
        resolves, so this is worth reporting before the autocoder trips over it.
        """
        return {
            name
            for name in self._reachable()
            if self._dictionary.type_def(name) is None
        }

    def type_defs(self) -> list[TypeDef]:
        """The type definitions the proxy needs, in dependency order.

        Only the *reachable* ones: a dictionary describes a whole deployment, including
        data-product record and container types the proxy does not mirror, and emitting
        an unused definition is not free.  It costs a generated C++ file pair, and it can
        actively break the build -- a stray ``FwSizeStoreType`` shadows FPP's implied use
        of that name and takes every string in the module down with it.

        Ordering is by dependency.  FPP resolves names within a module regardless of
        order, so this is for readability rather than correctness.
        """
        available = {d.qualified_name: d for d in self._dictionary.type_defs}
        reachable = self._reachable()
        emitted: list[TypeDef] = []
        placed: set[str] = set()

        def visit(name: str, stack: frozenset[str]) -> None:
            if name in placed or name not in available or name not in reachable:
                return
            if name in stack:
                # A cyclic definition cannot come out of fpp-to-dict, but refusing to
                # loop forever is cheaper than trusting that.
                return
            definition = available[name]
            for dependency in _dependencies(definition):
                visit(dependency, stack | {name})
            if name not in placed:
                placed.add(name)
                emitted.append(definition)

        for name in sorted(reachable):
            visit(name, frozenset())
        return emitted

    def _reachable(self) -> set[str]:
        """The transitive closure of :meth:`roots` over the type definitions."""
        if self._reachable_cache is not None:
            return self._reachable_cache
        seen: set[str] = set()
        stack = list(self.roots())
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            definition = self._dictionary.type_def(name)
            if definition is not None:
                stack.extend(_dependencies(definition))
        self._reachable_cache = seen
        return seen


def _qualified(refs: Iterable[TypeRef]) -> set[str]:
    """The names of those ``refs`` that point at a ``typeDefinitions`` entry."""
    return {r.name for r in refs if r.kind is TypeKind.QUALIFIED}


def _dependencies(definition: TypeDef) -> list[str]:
    """The qualified type names ``definition`` refers to.

    An alias contributes only its *underlying* type: the generated alias resolves
    straight to the built-in, so its intermediate target is never emitted and must not
    drag other definitions in with it.
    """
    refs: list[TypeRef] = []
    if isinstance(definition, EnumDef):
        refs = [definition.representation]
    elif isinstance(definition, ArrayDef):
        refs = [definition.element]
    elif isinstance(definition, StructDef):
        refs = [m.type for m in definition.members]
    elif isinstance(definition, AliasDef):
        refs = [definition.underlying]
    return sorted(_qualified(refs))
