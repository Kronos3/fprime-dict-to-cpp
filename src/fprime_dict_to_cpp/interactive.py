"""``-i``: the few decisions that shape a component, and a configuration to iterate on.

Interactive mode is a way *in*, not a second command line.  It asks only the questions
whose answers a component is built around -- which dictionary, what the component is
called, where it goes, and what kind it is -- generates the component, and writes a
configuration file beside it: those answers set, and every finer-grained option listed,
commented out, one line each saying what it does.

From then on that file is the interface.  Uncomment a key, re-run ``--config``, and look
at the diff.  Nothing set up by hand is written over on the way: the configuration
belongs to whoever edits it, and so does ``CMakeLists.txt`` once it exists.

The questions are deliberately few.  Everything else is a decision better made against a
diff than against a prompt, which is exactly what the file it leaves behind is for.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import config_file, dictionary
from .model import ComponentKind, Dictionary, DictionaryError

__all__ = [
    "Aborted",
    "AlreadyConfigured",
    "Prompter",
    "Scaffold",
    "config_path",
    "scaffold",
]


class Aborted(Exception):
    """The session ended without generating -- end of input, or an interrupt."""


class AlreadyConfigured(Exception):
    """There is a configuration file here already, so ``-i`` has nothing to add.

    Overwriting it is the most destructive thing this tool could do: it is the one file
    in the directory that is written once and edited afterwards.
    """


class Prompter:
    """Line-oriented questions on a pair of streams.

    Prompts go to ``output``, which the command line makes standard error, so that a
    run's report on standard output stays pipeable even when it was set up by hand.
    """

    def __init__(self, reader: TextIO, output: TextIO) -> None:
        self._reader = reader
        self._output = output

    def say(self, message: str = "") -> None:
        """Write one line of context, not a question."""
        print(message, file=self._output)

    def _read(self, prompt: str) -> str:
        self._output.write(prompt)
        self._output.flush()
        line = self._reader.readline()
        if line == "":  # end of input, not an empty answer
            self._output.write("\n")
            raise Aborted("aborted")
        return line.strip()

    def _complain(self, message: str) -> None:
        print(f"  {message}", file=self._output)

    def text(self, label: str, default: str = "") -> str:
        """A free-text answer.  Empty keeps ``default``."""
        shown = f" [{default}]" if default else ""
        while True:
            answer = self._read(f"{label}{shown}: ")
            if answer:
                return answer
            if default:
                return default
            self._complain("an answer is needed here")

    def path(self, label: str, default: Path | None = None) -> Path:
        """A filesystem path, expanded so a typed ``~`` behaves as in a shell."""
        answer = self.text(label, str(default) if default is not None else "")
        return Path(answer).expanduser()

    def choice(self, label: str, choices: list[str], default: str) -> str:
        """One of ``choices``, matched case-insensitively.  Empty keeps ``default``."""
        options = "/".join(choices)
        while True:
            answer = self.text(f"{label} ({options})", default)
            for choice in choices:
                if answer.casefold() == choice.casefold():
                    return choice
            self._complain(f"choose one of: {options}")


@dataclass(frozen=True)
class Scaffold:
    """What interactive mode set up."""

    dictionary: Dictionary
    """The parsed dictionary, so the caller does not read it twice."""

    config: Path
    """The configuration file to iterate through from here."""

    written: bool
    """Whether it was written.  ``False`` under ``--dry-run``, which writes nothing."""


def config_path(output: Path, component: str) -> Path:
    """Where a component's configuration file lives: beside the component it builds."""
    return output / f"{component}.yaml"


def scaffold(arguments: argparse.Namespace, prompter: Prompter) -> Scaffold:
    """Ask the shaping questions, fill ``arguments`` in, and write the configuration.

    The dictionary is parsed here rather than by the caller because the questions that
    follow depend on it, and because a path that will not load is worth re-asking about
    rather than exiting over.
    """
    parsed = _ask_dictionary(arguments, prompter)
    deployment = parsed.metadata.deployment_identifier

    prompter.say(f"\na proxy for {parsed.metadata.deployment_name}")
    component = prompter.text(
        "component name", arguments.component or f"{deployment}Proxy"
    )
    arguments.component = component
    output = prompter.path("output directory", arguments.output or Path(component))
    arguments.output = output
    arguments.kind = prompter.choice(
        "component kind", [k.value for k in ComponentKind], arguments.kind
    )

    path = config_path(output, component)
    if path.exists():
        raise AlreadyConfigured(
            f"{path} already exists; iterate on it with\n"
            f"  fprime-dict-to-cpp --config {path}"
        )
    if arguments.dry_run:
        prompter.say(f"\nwould write the configuration {path}")
        return Scaffold(dictionary=parsed, config=path, written=False)

    output.mkdir(parents=True, exist_ok=True)
    path.write_text(_skeleton(arguments, output, component), encoding="utf-8")
    return Scaffold(dictionary=parsed, config=path, written=True)


def _skeleton(arguments: argparse.Namespace, output: Path, component: str) -> str:
    """The configuration file's text: the answers, and everything else commented out.

    Anything else that is already in force is carried over as well, so that the file
    describes the run it came out of rather than a run nobody asked for.  That matters
    for ``-i --config``, where an override table came from somewhere else: leaving it out
    would make the new file quietly disagree with the component beside it.
    """
    chosen: dict[str, Any] = {
        "dictionary": _relative(arguments.dictionary, output),
        "output": Path("."),
        "component": component,
        "kind": arguments.kind,
    }
    if arguments.name_overrides:
        chosen["names"] = {
            str(name): identifier
            for name, identifier in arguments.name_overrides.items()
        }
    if arguments.type_name_overrides:
        chosen["type-names"] = dict(arguments.type_name_overrides)
    return config_file.skeleton(
        chosen,
        defaults={
            "include-prefix": arguments.include_prefix or output.name,
            "namespace": arguments.namespace or component,
        },
        header=[
            f"{component}: how this proxy is generated.",
            "",
            "  fprime-dict-to-cpp --config " f"{config_path(output, component)}",
            "",
            "Everything below is commented out at its default.  Uncomment a key, re-run",
            "that command, and read the diff.  This file is not written again, and",
            "neither is CMakeLists.txt once it exists, so anything added to either of",
            "them by hand stays.",
        ],
    )


def _relative(path: Path, output: Path) -> Path:
    """``path`` as the configuration file will name it: from its own directory.

    An absolute answer stays absolute -- somebody who typed one meant it, and a
    dictionary that lives outside the project is better named outright than reached by a
    chain of ``..``.
    """
    if path.is_absolute():
        return path
    try:
        return Path(os.path.relpath(path, output))
    except ValueError:  # different drives on Windows; nothing relative to say
        return path.resolve()


def _ask_dictionary(arguments: argparse.Namespace, prompter: Prompter) -> Dictionary:
    """Load the dictionary, re-asking for the path until one parses."""
    path = arguments.dictionary
    while True:
        if path is None:
            path = prompter.path("dictionary")
        try:
            parsed = dictionary.load(path)
        except (DictionaryError, OSError) as error:
            prompter.say(f"  cannot read {path}: {error}")
            path = None
            continue
        arguments.dictionary = path
        return parsed
