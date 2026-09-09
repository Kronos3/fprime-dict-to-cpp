"""FPP's reserved words, and the ``$`` escape that gets around them.

FPP lets a reserved word be used as an identifier by prefixing it with ``$`` -- the
prefix is purely lexical and does not become part of the name, which is why real
F Prime models contain ``$block``, ``$size`` and ``$time``.  A generator has no
control over what a dictionary calls a formal parameter or a struct member, so it has
to apply the escape mechanically.

The list below is transcribed from the FPP compiler's own lexer,
``compiler/lib/src/main/scala/syntax/Lexer.scala`` (``val keywords`` / ``val
reservedWordSet``), at fpp v3.3.0.  Note that the published language specification's
list is shorter than the compiler's, so the compiler is the authority.
"""

from __future__ import annotations

__all__ = ["RESERVED_WORDS", "escape", "is_reserved"]

#: Every FPP reserved word, verbatim from the compiler's lexer.
#:
#: The built-in type names are keywords too, which is easy to miss: they are the only
#: entries containing a digit.
RESERVED_WORDS = (
    frozenset("""
    F32 F64 I16 I32 I64 I8 U16 U32 U64 U8
    action active activity always array assert async at base block bool change
    choice command component connections constant container cpu default deployment
    diagnostic dictionary do drop else enter entry enum event every exit expand
    external false fatal format get group guard guarded health high hook id if
    implements import include initial input instance interface internal locate low
    machine match module omit on opcode orange output packet packets param passive
    phase port priority product queue queued record recv red ref reg request resp
    save send serial set severity signal size sizeof stack state string struct sync
    system telemetry template text throttle time topology true type unmatched update
    warning with yellow
    """.split())
    | frozenset(
        # Keywords in other FPP releases but not this one.  Escaping one costs nothing --
        # ``$foo`` and ``foo`` are the same name, and ``$`` is accepted on any identifier --
        # and it keeps generated output portable across compiler versions.
        """
    vector
    """.split()
    )
)


def is_reserved(identifier: str) -> bool:
    """Whether ``identifier`` is an FPP reserved word."""
    return identifier in RESERVED_WORDS


def escape(identifier: str) -> str:
    """``identifier``, prefixed with ``$`` if FPP would otherwise read it as a keyword.

    The escape is lexical only: ``$size`` denotes the identifier ``size``, so the
    generated C++ and the ground dictionary both see the unescaped spelling.
    """
    return f"${identifier}" if identifier in RESERVED_WORDS else identifier
