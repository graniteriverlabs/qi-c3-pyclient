"""
Console entry points for the pip-installed client.

These are thin wrappers. The work is done by the shipped ``sample_run`` and ``get_testcases``
modules, which are the same scripts the client is developed and tested against - so what
``c3-run`` does is exactly what ``python sample_run.py`` does, with no second implementation to
drift out of step.

What each wrapper adds is the part that only matters once installed: a check that the current
directory is a workspace, and the ``--app`` and ``--config`` options. Everything else - what a
run will use, and refusing before anything starts when something would stop it - is in
``sample_run``, so a script gets exactly the same.

===============  ==========================================================
c3-init          create or inspect the workspace, and report what is unset
c3-apps          the configured applications, whether each can run, and what one will use
c3-testcases     fetch the applicable test cases; runs no tests
c3-run           run the selected test cases
c3-exerciser     run an exerciser session
===============  ==========================================================

All return 0 on success and 1 on failure.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._bootstrap import APPLICATIONS, config_path, require_workspace


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--app", metavar="NAME", default=None,
        help="application to drive for this run; defaults to Selected_app in the config. "
             "One of: {0}".format(", ".join(APPLICATIONS)))
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help="configuration file to use; defaults to grl_config.json in the workspace")


def _resolve(args: argparse.Namespace) -> Tuple[Path, Path, Optional[str]]:
    """
    The workspace, the config file to read, and the application to drive.

    The two paths are kept apart on purpose. Inputs and outputs always live in the workspace, which
    is where the client itself reads them; ``--config`` only says which settings file to use. Taking
    the inputs from beside the configuration file instead would look in the wrong folder whenever
    ``--config`` names a file kept somewhere else.
    """
    workspace = require_workspace()
    config = Path(args.config).expanduser().resolve() if args.config else config_path(workspace)
    if not config.is_file():
        raise SystemExit("No configuration file at:\n  {0}".format(config))
    return workspace, config, args.app


def _read_config(config: Path) -> Dict[str, Any]:
    with config.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _selected(config: Path, app: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """The application this run will drive, and its settings."""
    data = _read_config(config)
    name = app or data.get("Selected_app")
    applications = data.get("applications") or {}
    if name not in applications:
        raise SystemExit(
            "Unknown application {0!r}.\nConfigured in {1}:\n  {2}".format(
                name, config, "\n  ".join(sorted(applications)) or "(none)"))
    return name, applications[name]


# --------------------------------------------------------------------------- c3-apps
def _checks(workspace: Path, config: Path, name: str, settings: Dict[str, Any]) -> List[Any]:
    """
    The checks ``c3-run`` makes before it starts, read from this configuration: the tester, the
    description file and the test case selection. The same functions, so the STATUS column and
    the command cannot disagree about what is ready.
    """
    from client.modules import run_preflight
    from client.modules.project_management import case_selection

    inputs = str(workspace / "JSON_User_input" / name)
    selection = case_selection.read_file(case_selection.selection_path(str(workspace), name))
    return [run_preflight.tester(name, settings.get("ip_address"), str(config)),
            run_preflight.description(name, inputs, settings.get("files") or {},
                                      bool(settings.get("is_multiple_esdf_files")), str(config)),
            run_preflight.cases(name, selection)]


def _status(workspace: Path, config: Path, name: str, settings: Dict[str, Any]) -> str:
    """
    Whether ``c3-run`` can start for this application, as the first thing that would stop it.

    Only what can be checked without the application or the tester. "Installed" is reported here
    but not refused by ``c3-run``, which can still attach to an application already running.
    """
    app_path = (settings.get("app_path") or "").strip()
    if not app_path or not Path(app_path).is_file():
        return "application not installed"
    for item in _checks(workspace, config, name, settings):
        if item.status:
            return item.status
    return "ready"


def apps() -> int:
    """``c3-apps`` - what is configured, and whether it can run, without starting anything."""
    parser = argparse.ArgumentParser(
        prog="c3-apps",
        description="Show each configured application and whether it is ready to run, then what "
                    "the selected one will use. Starts nothing.")
    _add_common(parser)
    args = parser.parse_args()
    workspace, config, app = _resolve(args)

    data = _read_config(config)
    configured = {name: settings for name, settings in (data.get("applications") or {}).items()
                  if isinstance(settings, dict)}
    if not configured:
        print("No applications are configured in {0}".format(config))
        return 1
    default = data.get("Selected_app")

    # A name that is not configured is refused before anything is printed, not after the table.
    detail = _selected(config, app) if app else (
        (default, configured[default]) if default in configured else None)

    from client.modules import run_preflight
    from client.modules.exerciser_manager import variant_for
    from client.modules.project_management import case_selection

    rows = [("*" if name == default else "", name, str(settings.get("known_port") or "?"),
             run_preflight.tester(name, settings.get("ip_address"), str(config)).shown,
             _status(workspace, config, name, settings))
            for name, settings in sorted(configured.items())]
    headings = ("", "APPLICATION", "PORT", "TESTER", "STATUS")
    widths = [1] + [max(len(row[i]) for row in rows + [headings]) for i in range(1, 4)]
    line = " {0:<%d} {1:<%d}   {2:>%d}   {3:<%d}   {4}" % tuple(widths)

    print("Configuration  {0}".format(config))
    print()
    print(line.format(*headings).rstrip())
    for row in rows:
        print(line.format(*row).rstrip())
    print()
    if detail is None:
        print("Selected_app {0!r} is not one of the applications above. Set it in:\n  {1}".format(
            default, config))
        return 0
    print(" * is used when --app is not given.")

    name, settings = detail
    _tester, description, cases = _checks(workspace, config, name, settings)
    sequence = run_preflight.sequence(name, str(workspace / "JSON_User_input" / name),
                                      settings.get("files") or {}, str(config), variant_for(name))
    selection_file = Path("Test_Case_List_From_System") / name / case_selection.FILE_NAME

    # What to change goes on its own line, so the summary stays short enough not to wrap.
    if cases.status == "select test cases":
        case_lines = [cases.shown + ' - add case names, or "ALL", to', str(selection_file)]
    elif cases.status:
        case_lines = [cases.shown, "in {0}".format(selection_file)]
    else:
        case_lines = [cases.shown]

    details = [("Description file", description.shown), ("Test cases", case_lines[0])]
    details.extend(("", more) for more in case_lines[1:])
    details.append(("Exerciser sequence", sequence.shown))

    print()
    print("{0} will use".format(name))
    for label, value in details:
        print("  {0:<20} {1}".format(label, value).rstrip())
    return 0


# --------------------------------------------------------------------------- c3-testcases
def testcases() -> int:
    """``c3-testcases`` - which cases the current description file makes applicable."""
    parser = argparse.ArgumentParser(
        prog="c3-testcases",
        description="Fetch the test cases the selected description file makes applicable. "
                    "Runs no tests.")
    _add_common(parser)
    parser.add_argument("--out", metavar="PATH", default=None,
                        help="also write the list to this file as JSON")
    args = parser.parse_args()
    _workspace, config, app = _resolve(args)
    _selected(config, app)

    from .get_testcases import cli

    return cli(config_file_path=str(config), app=app, out=args.out)


# --------------------------------------------------------------------------- c3-run
def run() -> int:
    """``c3-run`` - start, connect, load the description file, run the selected cases."""
    parser = argparse.ArgumentParser(
        prog="c3-run",
        description="Run the test cases selected in Manual_test_cases.json: start the "
                    "application, connect to the tester, load the description file, execute and "
                    "collect the report. What it will use is printed first, and nothing starts if "
                    "something would stop the run.")
    _add_common(parser)
    args = parser.parse_args()
    _workspace, config, app = _resolve(args)
    _selected(config, app)

    from .sample_run import cli

    # The command says which mode it is, so run_mode in the config is not consulted. Otherwise
    # `c3-run` would quietly start an exerciser session for anyone whose config was last left in
    # exerciser mode.
    return cli(config_file_path=str(config), app=app, mode="compliance")


# --------------------------------------------------------------------------- c3-exerciser
def exerciser() -> int:
    """``c3-exerciser`` - drive the emulator from the sequence exported by the application."""
    parser = argparse.ArgumentParser(
        prog="c3-exerciser",
        description="Run an exerciser session from the sequence named in the configuration "
                    "(applications.<app>.files.ExerciserSequenceModel), a file exported from the "
                    "application's own UI. What the sequence does is printed first. The session "
                    "runs until you press Enter.")
    _add_common(parser)
    args = parser.parse_args()
    _workspace, config, app = _resolve(args)
    _selected(config, app)

    from .sample_run import cli

    # The command says which mode it is, so run_mode in the config is not consulted.
    return cli(config_file_path=str(config), app=app, mode="exerciser")
