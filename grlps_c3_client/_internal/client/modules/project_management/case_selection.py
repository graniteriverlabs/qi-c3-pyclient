"""
Which test cases a run has been asked to execute.

One reader for the selection, used by everything that needs it - the run itself, the check
``c3-run`` makes before starting the application, and the summary ``c3-apps`` prints - so none of
them can disagree about what a file means.

The rule is that a run executes only what was asked for:

======================================  =============================================
The selection holds                     Meaning
======================================  =============================================
case names                              those cases
``["ALL"]``                             every case the description file allows
``[]``, nothing, or no file             nothing is selected; the run does not start
invalid JSON, or not a list of names    it cannot be read; the run does not start
``"ALL"`` together with case names      it is ambiguous; the run does not start
======================================  =============================================

The same rules apply to ``test_cases=[...]`` passed from a script.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

#: The one entry that selects every case the description file allows. Matched in any case.
ALL = "ALL"

#: The selection file, one per application under ``Test_Case_List_From_System``.
FILE_NAME = "Manual_test_cases.json"

NAMES = "names"
EVERY = "all"
NONE = "none"
INVALID = "invalid"

#: Where a selection came from, when it was not a file.
SCRIPT = "the script"

_EXAMPLE = '["8.1.1 PTX.CPX.PNG.S01.EPT.001", "8.1.2 PTX.CPX.PNG.S01.EPT.002"]'


@dataclass(frozen=True)
class Selection:
    """What was asked for, and where it came from."""

    kind: str
    names: Tuple[str, ...] = ()
    problem: str = ""
    source: str = ""

    @property
    def runnable(self) -> bool:
        """True when there is something to run."""
        return self.kind in (NAMES, EVERY)

    @property
    def from_script(self) -> bool:
        return self.source == SCRIPT

    def summary(self) -> str:
        """One line for a summary: what will run."""
        where = "" if self.from_script else " in {0}".format(FILE_NAME)
        if self.kind == EVERY:
            return "ALL - every case the description file allows"
        if self.kind == NAMES:
            return "{0} named{1}".format(len(self.names), where)
        if self.kind == NONE:
            return "none selected"
        return "cannot be used: {0}".format(self.problem)

    def refusal(self, app: str) -> str:
        """Why nothing will run, and exactly what to change. Empty when something will."""
        if self.runnable:
            return ""
        if self.kind == NONE:
            if self.from_script:
                return ("No test cases selected for {0}: test_cases is empty.\n\n"
                        "Pass case names, or [\"ALL\"] for every case the description file "
                        "allows.".format(app))
            return ("No test cases selected for {0}.\n\n"
                    "Add case names, or \"ALL\", to:\n"
                    "  {1}\n\n"
                    "c3-testcases lists the names your description file allows.".format(
                        app, self.source))
        if self.from_script:
            return ("The test cases passed for {0} cannot be used: {1}.\n\n"
                    "Pass a list of case names, for example:\n"
                    "  {2}\n"
                    "or [\"ALL\"] for every case the description file allows.".format(
                        app, self.problem, _EXAMPLE))
        return ("The test case selection for {0} cannot be used: {1}.\n"
                "  {2}\n\n"
                "It must be a list of case names, for example:\n"
                "  {3}\n"
                "or [\"ALL\"] for every case the description file allows.".format(
                    app, self.problem, self.source, _EXAMPLE))


def selection_path(root: str, app: str) -> str:
    """The selection file for one application in a workspace."""
    return os.path.join(root, "Test_Case_List_From_System", app, FILE_NAME)


def from_list(items: Any, source: str = SCRIPT) -> Selection:
    """
    Interpret a list of entries, from a file or from a script.

    A bare string is taken as one name. Passed from a script it would otherwise be iterated one
    character at a time and every character looked up as a case name.
    """
    if isinstance(items, str):
        items = [items]
    if isinstance(items, (dict, bytes)) or not hasattr(items, "__iter__"):
        return Selection(INVALID, problem="it holds {0}, not a list".format(
            _kind_of(items)), source=source)
    items = list(items)

    names = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, str):
            return Selection(INVALID, problem="entry {0} is {1!r}, not a case name".format(
                index, item), source=source)
        if item.strip():
            names.append(item.strip())

    if not names:
        return Selection(NONE, source=source)

    wants_all = [name for name in names if name.upper() == ALL]
    if wants_all and len(names) > 1:
        return Selection(INVALID, problem="\"ALL\" must be the only entry", source=source)
    if wants_all:
        return Selection(EVERY, source=source)
    return Selection(NAMES, names=tuple(names), source=source)


def read_file(path: str) -> Selection:
    """
    The selection held in a file.

    Read as ``utf-8-sig`` so a file saved by an editor that writes a byte-order mark is read the
    same as one that does not.
    """
    if not os.path.isfile(path):
        return Selection(NONE, source=path)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError as exc:
        return Selection(INVALID, problem="it could not be opened: {0}".format(exc), source=path)
    except UnicodeDecodeError:
        return Selection(INVALID, problem="it is not a text file", source=path)

    if not text.strip():
        return Selection(NONE, source=path)
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        line = getattr(exc, "lineno", None)
        column = getattr(exc, "colno", None)
        reason = getattr(exc, "msg", str(exc))
        where = " at line {0}, column {1}".format(line, column) if line else ""
        return Selection(INVALID, problem="not valid JSON{0} ({1})".format(where, reason),
                         source=path)
    if isinstance(parsed, str):
        return Selection(INVALID, problem="it holds a single string, not a list", source=path)
    return from_list(parsed, source=path)


def resolve(test_cases: Optional[Iterable[str]], path: str) -> Selection:
    """A script's list when one was passed, else the file - the precedence every input follows."""
    if test_cases is not None:
        return from_list(test_cases)
    return read_file(path)


def _kind_of(value: Any) -> str:
    if isinstance(value, dict):
        return "an object"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, (int, float)):
        return "a number"
    return "a {0}".format(type(value).__name__)
