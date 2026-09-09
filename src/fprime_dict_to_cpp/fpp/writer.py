"""A small text writer for FPP source.

FPP has no equivalent of ``fprime-cpp-codegen``'s document builder, and it does not
need one: the language is shallow, and the constructs a dictionary maps onto are all
flat lists of declarations.  What is worth centralising is the punctuation --
annotation comments, the backslash continuations that a multi-clause declaration
needs, and module nesting -- because getting any of it wrong fails the whole file.

Output is deliberately plainer than ``fpp-format``'s deep alignment.  Pass the result
through :class:`~fprime_dict_to_cpp.formatting.FppFormat` to get the canonical layout.
"""

from __future__ import annotations

from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager

__all__ = ["FppWriter", "annotation_lines", "string_literal"]

INDENT = "  "


def annotation_lines(text: str | None, *, pre: str = "@") -> list[str]:
    """Render an annotation as ``@ ...`` lines, one per line of ``text``.

    Returns an empty list for ``None`` or an empty string, so a caller can splice the
    result in unconditionally.
    """
    if not text:
        return []
    return [f"{pre} {line}".rstrip() for line in text.splitlines()]


def string_literal(text: str) -> str:
    """Render ``text`` as an FPP double-quoted string literal.

    FPP string literals accept the usual backslash escapes, so a backslash or a quote
    in an event's format string has to be doubled up.  Newlines are escaped rather
    than emitted literally, which keeps a declaration on one line.
    """
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


class FppWriter:
    """Accumulates FPP source lines at a tracked indentation."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._depth = 0

    # -- output -------------------------------------------------------

    def render(self) -> str:
        """The accumulated source, newline-terminated."""
        body = "\n".join(self._lines).rstrip("\n")
        return f"{body}\n" if body else ""

    def __str__(self) -> str:
        return self.render()

    # -- primitives ---------------------------------------------------

    def line(self, text: str = "") -> None:
        """Append one line at the current indentation.  Blank text stays blank."""
        self._lines.append(f"{INDENT * self._depth}{text}" if text else "")

    def lines(self, texts: Iterable[str]) -> None:
        """Append several lines."""
        for text in texts:
            self.line(text)

    def blank(self) -> None:
        """Append a blank line, unless the output is empty or already blank."""
        if self._lines and self._lines[-1] != "":
            self._lines.append("")

    def annotate(self, text: str | None) -> None:
        """Append an ``@`` annotation above whatever comes next."""
        self.lines(annotation_lines(text))

    def comment(self, text: str) -> None:
        """Append a ``#`` comment, which does not become an annotation."""
        for line in text.splitlines():
            self.line(f"# {line}".rstrip())

    def banner(self, text: str) -> None:
        """Append the ruled ``#`` banner F Prime models use to head a section."""
        self.blank()
        rule = "# " + "-" * 70
        self.line(rule)
        self.comment(text)
        self.line(rule)
        self.blank()

    # -- structure ----------------------------------------------------

    @contextmanager
    def block(self, opening: str, closing: str = "}") -> Generator[FppWriter]:
        """Open ``opening``, indent the body, then close it."""
        self.line(opening)
        self._depth += 1
        try:
            yield self
        finally:
            self._depth -= 1
        self.line(closing)

    @contextmanager
    def module(self, path: str) -> Generator[FppWriter]:
        """Open a module, or a chain of nested ones for a dotted ``path``.

        ``module("RefProxy.Ref.DpDemo")`` opens all three, so members added inside land
        in the innermost.
        """
        names = [p for p in path.split(".") if p]
        if not names:
            yield self
            return
        for name in names:
            self.line(f"module {name} {{")
            self._depth += 1
        try:
            yield self
        finally:
            for _ in names:
                self._depth -= 1
                self.line("}")

    # -- declarations -------------------------------------------------

    def declaration(
        self,
        head: str,
        params: Sequence[str] = (),
        clauses: Sequence[str] = (),
        *,
        annotation: str | None = None,
    ) -> None:
        """Write one declaration, wrapping its parameter list and trailing clauses.

        ``head`` is everything up to the parameter list (``"async command Foo"``),
        ``params`` are the formal parameters, and ``clauses`` are the trailing
        specifiers (``"opcode 0"``, ``"severity activity low"``).  Continuations get
        the backslashes FPP requires when a declaration spans lines.
        """
        self.annotate(annotation)
        if not params and not clauses:
            self.line(head)
            return
        if not params:
            self._clauses(head, clauses)
            return

        self.line(f"{head}(")
        self._depth += 1
        for index, param in enumerate(params):
            suffix = "" if index == len(params) - 1 else ","
            # An annotated parameter carries its comment after the comma, since the
            # post-comment binds to the whole entry.
            if " @<" in param:
                text, _, note = param.partition(" @<")
                self.line(f"{text}{suffix} @<{note}")
            else:
                self.line(f"{param}{suffix}")
        self._depth -= 1
        self._clauses(")", clauses)

    def _clauses(self, head: str, clauses: Sequence[str]) -> None:
        """Write ``head`` followed by indented, backslash-continued ``clauses``."""
        if not clauses:
            self.line(head)
            return
        self.line(f"{head} \\")
        self._depth += 1
        for index, clause in enumerate(clauses):
            self.line(clause if index == len(clauses) - 1 else f"{clause} \\")
        self._depth -= 1
