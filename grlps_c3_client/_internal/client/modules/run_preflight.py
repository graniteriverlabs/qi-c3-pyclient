"""
What a run will use, and whether it can start - decided from the files alone.

``c3-apps`` shows it as a table. ``c3-run``, ``c3-exerciser`` and ``sample_run.main`` print it
before they start anything, and refuse when something would stop the run. One implementation, so
the table and the commands cannot disagree about what is ready.

Nothing here talks to the application or the tester.
"""
from __future__ import annotations

import glob
import os
import textwrap
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .project_management import case_selection

#: The address the package ships with. RFC 5737 TEST-NET-1, so it can never reach a real device;
#: it means "no tester has been set", and is shown and checked as exactly that.
PLACEHOLDER_IP = "192.0.2.50"

#: Width of the label column in every summary.
_LABEL = 18


@dataclass(frozen=True)
class Item:
    """One line of a summary, and what is wrong with it."""

    label: str
    shown: str
    #: Short form for the ``c3-apps`` STATUS column. Empty when nothing is wrong.
    status: str = ""
    #: What is wrong and exactly what to change. Empty when nothing is wrong.
    refusal: str = ""
    #: Further summary lines, as (label, value).
    more: Tuple[Tuple[str, str], ...] = ()


def summary(title: str, items: List[Item]) -> str:
    """
    The block a command prints before it starts.

    A long value is wrapped under its own column, so the block stays inside an 80-column terminal.
    A word longer than the column - a path - is left whole rather than broken.
    """
    lines = [title]
    width = 79 - _LABEL - 3
    for item in items:
        for label, value in ((item.label, item.shown),) + tuple(item.more):
            chunks = textwrap.wrap(value, width=width, break_long_words=False,
                                   break_on_hyphens=False) or [""]
            lines.append("  {0:<{1}} {2}".format(label, _LABEL, chunks[0]).rstrip())
            lines.extend("  {0:<{1}} {2}".format("", _LABEL, chunk) for chunk in chunks[1:])
    return "\n".join(lines)


def refusals(items: List[Item]) -> List[str]:
    return [item.refusal for item in items if item.refusal]


# --------------------------------------------------------------------------- tester
def tester(app: str, address: Optional[str], config_file: str) -> Item:
    """The tester address, or why there is none."""
    address = (address or "").strip()
    if address and address != PLACEHOLDER_IP:
        return Item("Tester", address)
    return Item("Tester", "not set", status="set the tester address", refusal=(
        "No tester address is set for {0}.\n\n"
        "Set it with:\n"
        "  c3-init --app {0} --tester <address>\n"
        "or in applications.{0}.ip_address in:\n"
        "  {1}".format(app, config_file)))


# --------------------------------------------------------------------------- description file
def description(app: str, inputs: str, files: Dict[str, Any], multiple: bool, config_file: str,
                override: Optional[str] = None) -> Item:
    """
    The description file a compliance run will load.

    Resolved the way the client resolves it: one file named by ``EsdfConfigurationModel`` (or
    passed by a script), or every ``*.json`` in the ``EsdfFolderName`` folder when
    ``is_multiple_esdf_files`` is set.
    """
    label = "Description file"
    if multiple:
        folder = (files.get("EsdfFolderName") or "esdf").strip()
        found = sorted(glob.glob(os.path.join(inputs, folder, "*.json")))
        shown = "every .json file in {0}{1} ({2} found)".format(os.path.normpath(folder), os.sep,
                                                                len(found))
        if found:
            return Item(label, shown)
        return Item(label, shown, status="no description files", refusal=(
            "No description files for {0} in:\n"
            "  {1}\n\n"
            "applications.{0}.is_multiple_esdf_files is true, so every .json file in that folder "
            "is run.".format(app, os.path.join(inputs, folder))))

    value = (override or files.get("EsdfConfigurationModel") or "").strip()
    setting = "applications.{0}.files.EsdfConfigurationModel".format(app)
    if not value:
        return Item(label, "not set", status="set the description file", refusal=(
            "No description file is set for {0}.\n\n"
            "Set {1} in:\n"
            "  {2}\n"
            "to a file in:\n"
            "  {3}".format(app, setting, config_file,
                           os.path.join(inputs, files.get("EsdfFolderName") or "esdf"))))
    path = os.path.join(inputs, value)
    shown = os.path.normpath(value)
    if os.path.isfile(path):
        return Item(label, shown)
    source = ("It was passed by the script." if override else
              "It is set by {0} in:\n  {1}".format(setting, config_file))
    return Item(label, shown + "   (not found)", status="description file not found", refusal=(
        "The description file for {0} is not there:\n"
        "  {1}\n\n"
        "{2}".format(app, os.path.normpath(path), source)))


# --------------------------------------------------------------------------- test cases
def cases(app: str, selection: case_selection.Selection) -> Item:
    """What the selection asks for."""
    if selection.runnable:
        return Item("Test cases", selection.summary())
    status = ("select test cases" if selection.kind == case_selection.NONE
              else "fix the test case selection")
    return Item("Test cases", selection.summary(), status=status,
                refusal=selection.refusal(app))


# --------------------------------------------------------------------------- exerciser session end
def session_end(dry_run: bool, interactive: bool, hold: Any = None) -> Item:
    """
    How the session will end. A started exerciser runs until it is told to stop: by a time limit
    passed from a script, or by Enter. Without either nobody can stop it, so that is refused
    before anything is started.
    """
    from .exerciser_manager import hold_problem

    if dry_run:
        return Item("Ends", "at once - a dry run sends nothing to the tester")
    if hold is not None:
        problem = hold_problem(hold)
        if problem:
            return Item("Ends", repr(hold), status="hold cannot be used",
                        refusal="The session length cannot be used: {0}.".format(problem))
        return Item("Ends", "after {0:g} s{1}".format(
            hold, " - or sooner if you press Enter" if interactive else ""))
    if interactive:
        return Item("Ends", "when you press Enter")
    return Item("Ends", "when you press Enter", status="no console", refusal=(
        "This session runs until you press Enter, but no console is attached to read it.\n\n"
        "Run it from a terminal, pass hold=SECONDS from a script, or set\n"
        "common.exerciser.dry_run to true to compose every request without sending any."))


# --------------------------------------------------------------------------- exerciser sequence
def sequence(app: str, inputs: str, files: Dict[str, Any], config_file: str,
             variant: Optional[str], override: Optional[str] = None) -> Item:
    """
    The exported sequence an exerciser session will run, read and checked without sending
    anything: its Qi specification and power profile, its packets, the steps it becomes, and any
    controller setting in it the application has no endpoint for.
    """
    from . import exerciser_sequence as seq

    label = "Sequence"
    setting = "applications.{0}.files.ExerciserSequenceModel".format(app)
    available = [os.path.normpath(p) for p in seq.available_sequences(inputs)]
    listing = "\n  ".join(available) or "(none - run c3-init here)"
    if variant is None:
        return Item(label, "-", status="no exerciser", refusal=(
            "{0} has no exerciser this client can drive.".format(app)))

    value = (override or files.get("ExerciserSequenceModel") or "").strip()
    if not value:
        return Item(label, "not set", status="set the exerciser sequence", refusal=(
            "No exerciser sequence is set for {0}.\n\n"
            "Available in {1}:\n"
            "  {2}\n\n"
            "Set {3} in:\n"
            "  {4}".format(app, inputs, listing, setting, config_file)))

    path = seq.sequence_path(inputs, value)
    shown = os.path.normpath(os.path.relpath(path, inputs))
    if not os.path.isfile(path):
        source = ("It was passed by the script." if override else
                  "Point {0} at one of them in:\n  {1}".format(setting, config_file))
        return Item(label, shown + "   (not found)", status="exerciser sequence not found",
                    refusal=(
                        "The exerciser sequence for {0} is missing:\n"
                        "  {1}\n\n"
                        "Available in {2}:\n"
                        "  {3}\n\n"
                        "{4}\n"
                        "A sequence for another power profile is exported from the "
                        "application's own UI.".format(app, os.path.normpath(path),
                                                       os.path.normpath(inputs), listing,
                                                       source)))
    try:
        doc = seq.load_sequence_file(path)
        steps, problems = seq.build_steps(doc, variant)
    except seq.SequenceError as exc:
        return Item(label, shown, status="exerciser sequence cannot be used", refusal=(
            "The exerciser sequence for {0} cannot be used:\n"
            "  {1}\n"
            "  {2}".format(app, os.path.normpath(path), exc)))

    info = seq.describe(doc)
    packets = info.get("packets") or []
    if len(packets) > 1:
        packet_text = "{0}, in {1} sequences ({2})".format(
            sum(packets), len(packets), " and ".join(str(n) for n in packets))
    else:
        packet_text = str(sum(packets))
    controller = [s for s in steps if s.get("op") not in ("reset", "phase_settings", "packets",
                                                          "start")]
    parts = []
    if any(s.get("op") == "reset" for s in steps):
        parts.append("reset")
    if controller:
        parts.append("{0} controller setting{1}".format(len(controller),
                                                         "" if len(controller) == 1 else "s"))
    if any(s.get("op") == "phase_settings" for s in steps):
        parts.append("phases")
    if any(s.get("op") == "packets" for s in steps):
        parts.append("packets")
    if any(s.get("op") == "start" for s in steps):
        parts.append("start")

    more = [("Qi specification", str(info.get("qi_spec") or "not stated in the file")),
            ("Power profile", str(info.get("power_profile") or "not stated in the file")),
            ("Packets", packet_text),
            ("Steps", "{0} - {1}".format(len(steps), ", ".join(parts)))]
    unapplied = [p.split("'")[1] for p in problems
                 if p.startswith("controller group '") and p.count("'") >= 2]
    other = [p for p in problems if not p.startswith("controller group '")]
    if len(unapplied) == 1:
        more.append(("Not applied", "{0} - this application cannot apply it".format(
            unapplied[0])))
    elif unapplied:
        # Wrapped to the value column, so a long list stays inside an 80-column terminal.
        more.append(("Not applied", "{0} controller settings this application cannot "
                                    "apply:".format(len(unapplied))))
        more.extend(("", line) for line in textwrap.wrap(", ".join(unapplied),
                                                         width=79 - _LABEL - 3))
    for problem in other:
        more.append(("Warning", problem))
    return Item(label, shown, more=tuple(more))
