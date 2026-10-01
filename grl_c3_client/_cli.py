"""
Console entry points for the pip-installed client.

These are thin wrappers. The work is done by the shipped ``sample_run`` and ``get_testcases``
modules, which are the same scripts the client is developed and tested against - so what
``c3-run`` does is exactly what ``python sample_run.py`` does, with no second implementation to
drift out of step.

What each wrapper adds is the part that only matters once installed: a check that the current
directory is a workspace, the ``--app`` and ``--config`` options, and for the exerciser a
pre-flight check of the exported sequence file, so a missing file is reported before the
application is started rather than after.

===============  ==========================================================
c3-init          create or inspect the workspace, and report what is unset
c3-apps          list the configured applications and their settings
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
from typing import Any, Dict, Optional, Tuple

from ._bootstrap import APPLICATIONS, config_path, require_workspace


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--app", metavar="NAME", default=None,
        help="application to drive for this run; defaults to Selected_app in the config. "
             "One of: {0}".format(", ".join(APPLICATIONS)))
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help="configuration file to use; defaults to grl_config.json in the workspace")


def _resolve(args: argparse.Namespace) -> Tuple[Path, Optional[str]]:
    """The config file to read and the application to drive."""
    workspace = require_workspace()
    config = Path(args.config).expanduser().resolve() if args.config else config_path(workspace)
    if not config.is_file():
        raise SystemExit("No configuration file at:\n  {0}".format(config))
    return config, args.app


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
def apps() -> int:
    """``c3-apps`` - what is configured, without starting anything."""
    parser = argparse.ArgumentParser(
        prog="c3-apps",
        description="List the C3 applications this workspace is configured to drive.")
    _add_common(parser)
    args = parser.parse_args()
    config, _ = _resolve(args)

    data = _read_config(config)
    selected = data.get("Selected_app")
    configured = data.get("applications") or {}
    if not configured:
        print("No applications are configured in {0}".format(config))
        return 1

    workspace = config.parent
    print("Configuration: {0}".format(config))
    print("Run mode     : {0}".format((data.get("common") or {}).get("run_mode", "compliance")))
    print()
    header = "{0:<22} {1:>5}  {2:<16} {3:<10} {4:<10} {5}".format(
        "APPLICATION", "PORT", "TESTER", "INPUTS", "SEQUENCE", "INSTALLED")
    print(header)
    print("-" * len(header))

    for name, app in sorted(configured.items()):
        if not isinstance(app, dict):
            continue
        inputs = workspace / "JSON_User_input" / name
        n_inputs = sum(1 for _ in inputs.rglob("*") if _.is_file()) if inputs.is_dir() else 0
        files = app.get("files") or {}
        sequence = files.get("ExerciserSequenceModel")
        has_sequence = bool(sequence) and (inputs / sequence).is_file()
        app_path = app.get("app_path") or ""
        print("{0:<22} {1:>5}  {2:<16} {3:<10} {4:<10} {5}".format(
            ("* " if name == selected else "  ") + name,
            app.get("known_port") or "?",
            app.get("ip_address") or "not set",
            "{0} file(s)".format(n_inputs) if n_inputs else "none",
            "yes" if has_sequence else "no",
            "yes" if app_path and Path(app_path).is_file() else "NOT FOUND"))

    print()
    print("* is the application used when --app is not given.")
    print("SEQUENCE is the exported exerciser file, needed only by c3-exerciser.")
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
    config, app = _resolve(args)

    from .get_testcases import cli

    return cli(config_file_path=str(config), app=app, out=args.out)


# --------------------------------------------------------------------------- c3-run
def run() -> int:
    """``c3-run`` - start, connect, load the description file, run the selected cases."""
    parser = argparse.ArgumentParser(
        prog="c3-run",
        description="Run the selected test cases: start the application, connect to the tester, "
                    "load the description file, execute and collect the report.")
    _add_common(parser)
    args = parser.parse_args()
    config, app = _resolve(args)

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
        description="Run an exerciser session from the sequence file exported by the "
                    "application's own UI. The session runs until you press Enter.")
    _add_common(parser)
    parser.add_argument("--dry-run", action="store_true",
                        help="compose every request and send none")
    args = parser.parse_args()
    config, app = _resolve(args)

    # An exerciser session is driven entirely by a file exported from the application's UI. It is
    # the one prerequisite that is not a setting, and without it the session gets as far as
    # starting the application before failing, so it is checked here first.
    name, settings = _selected(config, app)
    sequence = ((settings.get("files") or {}).get("ExerciserSequenceModel") or "").strip()
    inputs = config.parent / "JSON_User_input" / name
    if not sequence:
        raise SystemExit(
            "No exerciser sequence file is configured for {0}.\n\n"
            "Set it up in the application's own UI, export it, put the exported file in:\n"
            "  {1}\n"
            "and name it in {2} under:\n"
            '  applications.{0}.files.ExerciserSequenceModel'.format(name, inputs, config))
    if not (inputs / sequence).is_file():
        raise SystemExit(
            "The exerciser sequence file for {0} is missing:\n"
            "  {1}\n\n"
            "Export it from the application's own UI and put it there, or point\n"
            "applications.{0}.files.ExerciserSequenceModel at a file that exists.".format(
                name, inputs / sequence))

    from .sample_run import cli

    if not args.dry_run:
        return cli(config_file_path=str(config), app=app, mode="exerciser")

    # A dry run is a one-off, so it goes through a copy of the configuration rather than editing
    # the workspace's own file and having to put it back. Input and output paths come from the
    # workspace, not from where the configuration file sits, so the copy can live anywhere.
    import tempfile

    data = _read_config(config)
    data.setdefault("common", {}).setdefault("exerciser", {})["dry_run"] = True
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(data, handle, indent=4)
        handle.close()
        return cli(config_file_path=handle.name, app=app, mode="exerciser")
    finally:
        try:
            Path(handle.name).unlink()
        except OSError:
            pass
