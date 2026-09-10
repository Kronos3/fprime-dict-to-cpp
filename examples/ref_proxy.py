"""Generate a proxy for the Ref deployment through the Python API.

The command line covers the common cases; this is the same thing done directly, for when
a project wants to drive generation from its own build script and inspect the plan on the
way past.

Run it to print a summary, or pass a directory to write the component out::

    python examples/ref_proxy.py
    python examples/ref_proxy.py build-artifacts/RefProxy
"""

from __future__ import annotations

import sys
from pathlib import Path

from fprime_dict_to_cpp import dictionary
from fprime_dict_to_cpp.cpp import cpp_file_names, render_implementation
from fprime_dict_to_cpp.fpp import render_fpp
from fprime_dict_to_cpp.generate import generate_from_plan
from fprime_dict_to_cpp.model import ComponentKind
from fprime_dict_to_cpp.naming import Naming, Qualification, Style
from fprime_dict_to_cpp.plan import Config, resolve

DICTIONARY = (
    Path(__file__).parent.parent / "tests" / "data" / "RefTopologyDictionary.json"
)


def configuration(deployment: str) -> Config:
    """A queued proxy: packets are decoded on whatever drives its ``run`` port.

    ``naming`` is spelled out with its two knobs rather than through
    ``Naming.preset("instance")``, to show what the presets are made of.
    """
    return Config(
        component=f"{deployment}Proxy",
        namespace=f"{deployment}Proxy",
        kind=ComponentKind.QUEUED,
        naming=Naming(qualification=Qualification.INSTANCE, style=Style.UNDERSCORE),
        include_prefix=f"{deployment}Proxy",
    )


def main() -> int:
    parsed = dictionary.load(DICTIONARY)
    config = configuration(parsed.metadata.deployment_identifier)
    plan = resolve(parsed, config)

    print(
        f"{config.component} ({config.kind.value}) from {parsed.metadata.deployment_name}"
    )
    print(f"  commands  {len(plan.commands):4d}")
    print(f"  events    {len(plan.events):4d}")
    print(f"  channels  {len(plan.channels):4d}")
    print(f"  packets   {len(plan.packets):4d}")
    print(f"  types     {len(plan.spelling.all_type_defs()):4d}")
    for warning in plan.warnings:
        print(f"  warning: {warning}")

    # The remote's widths, which are what the decoder actually reads with.
    platform = parsed.platform
    print("\nremote wire widths:")
    for label, ref in (
        ("packet descriptor", platform.packet_descriptor),
        ("opcode", platform.opcode),
        ("event id", platform.event_id),
        ("channel id", platform.channel_id),
        ("packet id", platform.packetize_id),
    ):
        print(f"  {label:18s} {ref.name}")

    if len(sys.argv) > 1:
        result = generate_from_plan(parsed, sys.argv[1], config)
        print()
        for path in result.files:
            print(f"  wrote {path}")
        return 0

    # No output directory: show what the emitters produce without writing anything.
    print("\nrendered sizes:")
    documents = {
        **render_fpp(plan),
        **render_implementation(plan).files(cpp_file_names(plan)),
    }
    for name, text in documents.items():
        print(f"  {name:28s} {len(text.splitlines()):5d} lines")
    print("\nPass a directory to write these out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
