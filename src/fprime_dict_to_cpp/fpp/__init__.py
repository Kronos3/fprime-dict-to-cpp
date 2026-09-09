"""Emitting the FPP half of a generated proxy component."""

from __future__ import annotations

from .component import render_component
from .keywords import RESERVED_WORDS, escape, is_reserved
from .typedefs import render_types
from .writer import FppWriter, annotation_lines, string_literal

__all__ = [
    "FppWriter",
    "RESERVED_WORDS",
    "annotation_lines",
    "escape",
    "is_reserved",
    "render_component",
    "render_types",
    "string_literal",
]
