"""Emitting the FPP half of a generated proxy component."""

from __future__ import annotations

from .component import (
    fragments,
    render_commands,
    render_component,
    render_events,
    render_telemetry,
)
from .files import fpp_file_names, render_fpp
from .keywords import RESERVED_WORDS, escape, is_reserved
from .typedefs import render_types
from .writer import FppWriter, annotation_lines, string_literal

__all__ = [
    "FppWriter",
    "RESERVED_WORDS",
    "annotation_lines",
    "escape",
    "fpp_file_names",
    "fragments",
    "is_reserved",
    "render_commands",
    "render_component",
    "render_events",
    "render_fpp",
    "render_telemetry",
    "render_types",
    "string_literal",
]
