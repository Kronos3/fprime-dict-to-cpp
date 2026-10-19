"""The set of FPP files a plan produces, and their contents.

One model file plus the ``.fppi`` fragments it includes.  Only ``<Component>.fpp`` is an
autocoder input: ``fpp-depend`` reports the fragments as included files, and F Prime's
build makes them regenerators of the same targets, so listing them again would autocode
each one standalone -- which cannot work, since a fragment is component members with no
component around them.
"""

from __future__ import annotations

from ..plan import Plan
from .component import (
    fragments,
    render_commands,
    render_component,
    render_events,
    render_telemetry,
)
from .typedefs import render_types

__all__ = ["FRAGMENTS", "fpp_file_names", "render_fpp"]

#: How each fragment is rendered, keyed by the section name :func:`fragments` uses.
_RENDERERS = {
    "Types": render_types,
    "Commands": render_commands,
    "Events": render_events,
    "Telemetry": render_telemetry,
}

#: Every fragment a plan may populate, whether or not a given one does.
FRAGMENTS = tuple(_RENDERERS)


def render_fpp(plan: Plan) -> dict[str, str]:
    """Every FPP file for ``plan``, keyed by file name, model file first."""
    component = plan.config.component
    files = {f"{component}.fpp": render_component(plan)}
    for section in fragments(plan):
        files[f"{component}{section}.fppi"] = _RENDERERS[section](plan)
    return files


def fpp_file_names(plan: Plan) -> list[str]:
    """The FPP file names for ``plan``, in the same order :func:`render_fpp` gives them."""
    component = plan.config.component
    return [
        f"{component}.fpp",
        *(f"{component}{section}.fppi" for section in fragments(plan)),
    ]
