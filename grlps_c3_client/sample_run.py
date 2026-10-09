"""
Run one C3 session end to end: launch, connect, then compliance cases or an exerciser session.

From a checkout::

    python sample_run.py

Installed, this same module backs the ``c3-run`` and ``c3-exerciser`` commands, so what you read
here is exactly what those commands do.

Which of the two it runs comes from ``common.run_mode`` in the configuration - "compliance"
(the default) or "exerciser" - unless a caller passes ``mode`` explicitly.
"""
import os
import sys
from typing import Any, Dict, List, Optional

from client.grl_api_client import GRLApiClient
from client.modules import run_preflight
from client.modules.exerciser_manager import _stdin_is_interactive


def _preflight(client: GRLApiClient, run_mode: str, esdf: Optional[str],
               test_cases: Optional[List[str]], sequence_file: Optional[str],
               dry_run: Optional[bool], hold: Optional[float]) -> List[run_preflight.Item]:
    """
    What this run will use, read from the files with the script's values applied - the same
    values the run itself goes on to use. Nothing is started.
    """
    settings = client.config_manager
    app = settings.app_name
    config_file = os.path.abspath(settings.config_file_path)
    manager = client.legacy.project_manager
    inputs = manager._setup_directories()["json_dir"]
    files = settings.file_map or {}

    items = [run_preflight.tester(app, settings.ip_address, config_file)]
    if run_mode == "exerciser":
        session = dict(getattr(settings, "exerciser", {}) or {})
        dry = bool(session.get("dry_run")) if dry_run is None else bool(dry_run)
        items.append(run_preflight.sequence(app, inputs, files, config_file,
                                            client.legacy.exerciser_manager.variant,
                                            override=sequence_file))
        items.append(run_preflight.session_end(dry, _stdin_is_interactive(), hold))
    else:
        items.append(run_preflight.description(app, inputs, files,
                                               bool(settings.is_multiple_esdf_files), config_file,
                                               override=esdf))
        items.append(run_preflight.cases(app, manager.selection(test_cases)))
        refusal = manager.override_refusal(esdf, test_cases)
        if refusal:
            items.append(run_preflight.Item("Passed by the script", "cannot be used",
                                            status="override not usable", refusal=refusal))
    return items


def _report_exerciser(result: Dict[str, Any]) -> None:
    """Print what an exerciser session actually did, step by step."""
    if result.get("dry_run"):
        print("Exerciser session: DRY RUN - nothing was sent to the tester")
    print("Exerciser session:", "OK" if result.get("success") else "FAILED",
          result.get("error", ""))

    # Per-step detail: a 200 does not mean a command took effect, so show what the read-back
    # actually confirmed.
    for record in result.get("results", []):
        if record.get("op") == "hold":
            # The exerciser has no end of its own - this is how long it actually ran.
            mins, secs = divmod(int(record.get("held_seconds", 0)), 60)
            samples = record.get("samples") or []
            counts = [s.get("packets") for s in samples if s.get("packets") is not None]
            grew = f", packets {counts[0]} -> {counts[-1]}" if counts else ""
            print(f"  {'ok  ' if record.get('success') else 'FAIL'} "
                  f"{'hold':<16} ran {mins:02d}:{secs:02d}, "
                  f"{len(samples)} readings{grew} "
                  f"({record.get('reason', '')})")
            continue
        if record.get("skipped"):
            print(f"  --   {record.get('op', '?'):<16} not available on this controller")
            continue
        # Show the read-back detail whenever it says something: a mismatch, or the tier-B
        # evidence of whether the app's state actually moved.
        state = record.get("verified", "")
        detail = record.get("verify_detail", "")
        show = detail if state in ("mismatch", "changed", "unchanged") else ""
        print(f"  {'ok  ' if record.get('success') else 'FAIL'} "
              f"{record.get('op', '?'):<16} {state}"
              f"{'  ' + show if show else ''}")

    if result.get("interrupted"):
        print("  session was interrupted - the exerciser was stopped cleanly")
    for problem in result.get("problems", []):
        print(f"  config: {problem}")
    capture = result.get("capture") or {}
    if capture.get("copied_to"):
        print(f"  capture: {capture['copied_to']}")


def main(config_file_path: str = "grl_config.json",
         app: Optional[str] = None,
         ip_address: Optional[str] = None,
         mode: Optional[str] = None,
         project: Optional[str] = None,
         esdf: Optional[str] = None,
         test_cases: Optional[List[str]] = None,
         sequence_file: Optional[str] = None,
         dry_run: Optional[bool] = None,
         hold: Optional[float] = None) -> Dict[str, Any]:
    """
    Run one session and return what happened.

    Every argument is optional. Anything you pass is used for this run; anything you leave out
    falls back to the configuration file, so a script can supply as much or as little as it likes
    and the configuration-only way of working is unchanged.

    Args:
        config_file_path: the configuration file to use
        app: which application to drive.          None -> ``Selected_app``
        ip_address: the tester to talk to.        None -> that application's ``ip_address``
        mode: "compliance" or "exerciser".        None -> ``common.run_mode``
        project: the project name.                None -> ``ProjectConfigurationModel``
        esdf: description file for compliance,
            e.g. "esdf/MyDevice.json".            None -> ``files.EsdfConfigurationModel``
        test_cases: the cases to run, or ["ALL"]. None -> ``Manual_test_cases.json``. An empty
                                                  selection runs nothing.
        sequence_file: exported exerciser file.   None -> ``files.ExerciserSequenceModel``
        dry_run: compose but send nothing.        None -> ``common.exerciser.dry_run``
        hold: seconds the exerciser runs.         None -> until Enter is pressed

    Returns:
        A dict describing how far the run got. A step that never happened is absent, so the
        absence of a key is itself the signal - callers do not have to guess from a bare False.
        Keys: ``launched``, ``connected``, ``mode``, and then ``exerciser`` or ``compliance``.
        ``refused`` holds the message when the run was stopped before anything was started.
    """
    outcome: Dict[str, Any] = {"launched": False, "connected": False}
    client = GRLApiClient(config_file_path=config_file_path, app=app, ip_address=ip_address)
    run_mode = mode or client.config_manager.run_mode
    outcome["mode"] = run_mode

    # Say what this run will use, and stop here - before the application is started or the tester
    # touched - when something would stop it anyway. A refusal costs no time and changes nothing
    # on the bench, and it names the file or setting to fix.
    items = _preflight(client, run_mode, esdf=esdf, test_cases=test_cases,
                       sequence_file=sequence_file, dry_run=dry_run, hold=hold)
    title = "{0}   {1}".format("Exerciser session" if run_mode == "exerciser" else "Compliance run",
                               client.config_manager.app_name)
    print(run_preflight.summary(title, items))
    print()
    problems = run_preflight.refusals(items)
    if problems:
        outcome["refused"] = "\n\n".join(problems)
        print(outcome["refused"])
        return outcome

    try:
        # Step 1: Launch the GRL Application
        if not client.launch_app():
            print("Failed to launch GRL application.")
            return outcome
        outcome["launched"] = True
        print("Application launched successfully!")

        # Step 2: Connect to Test Equipment
        connection_result = client.connect()
        if "error" in connection_result:
            print(f"Connection failed: {connection_result['error']}")
            outcome["error"] = connection_result["error"]
            return outcome

        # connect() returns {"success": True, "data": {...}}; the tester's details are in "data".
        success_data = connection_result.get("data")
        print("Connected to Test Equipment successfully!")
        if isinstance(success_data, dict):
            print(f"Tester Status: {success_data.get('testerStatus')}")
            print(f"Firmware Version: {success_data.get('firmwareVersion')}")
        outcome["connected"] = True

        # Step 3: Run compliance test list OR a manual exerciser session, per config
        # common.run_mode ("compliance" default, or "exerciser"), unless the caller said which.
        if run_mode == "exerciser":
            result = client.run_exerciser(sequence_file=sequence_file, dry_run=dry_run, hold=hold)
            _report_exerciser(result)
            outcome["exerciser"] = result
        else:
            status = client.run_compliance(project_name=project, esdf=esdf, test_cases=test_cases)
            print(status)
            outcome["compliance"] = status
        return outcome

    except Exception as e:
        print(f"Exception occurred: {str(e)}")
        outcome["error"] = str(e)
        return outcome

    finally:
        # Step 5: Always disconnect at the end
        client.disconnect()
        print("Disconnected cleanly.")


def cli(config_file_path: str = "grl_config.json",
        app: Optional[str] = None,
        mode: Optional[str] = None,
        **overrides: Any) -> int:
    """
    Console-script entry point.

    ``main`` returns the outcome dict, which callers rely on. A console script must hand back an
    int, so this maps the run onto a process exit code: 0 when the session ran to completion, 1
    otherwise. ``main`` leaves a step's key out when the step never happened, so the absence of a
    key is what is checked here.
    """
    result = main(config_file_path=config_file_path, app=app, mode=mode, **overrides)

    if "refused" in result:
        # The reason has been printed; nothing was started, so there is nothing else to say.
        return 1
    if not result.get("launched"):
        print("[sample_run] the application did not start - nothing was run.",
              file=sys.stderr, flush=True)
        return 1
    if not result.get("connected"):
        print("[sample_run] the tester was not connected - nothing was run.",
              file=sys.stderr, flush=True)
        return 1

    if result.get("mode") == "exerciser":
        session = result.get("exerciser") or {}
        if not session.get("success"):
            print("[sample_run] the exerciser session failed - see the detail above.",
                  file=sys.stderr, flush=True)
            return 1
    elif "compliance" not in result:
        print("[sample_run] stopped before the test list ran - see the message above.",
              file=sys.stderr, flush=True)
        return 1
    elif not compliance_completed(result["compliance"]):
        # Having a status is not the same as having finished. A refused description file, a
        # failed submission and an incomplete run all come back as a status line, and used to
        # exit 0 here because only the PRESENCE of a status was checked.
        print("[sample_run] the test run did not complete - see the message above.",
              file=sys.stderr, flush=True)
        return 1

    return 0


def compliance_completed(status: Any) -> bool:
    """
    True only when a compliance run finished every case it was asked to run.

    ``set_project`` reports with one status line. Every other line it can return says the run
    did not finish: did not start, failed, INCOMPLETE, submitted but never started, completed
    with warnings/errors across several description files, or no line at all.
    """
    return isinstance(status, list) and status == ["Test Execution completed"]


if __name__ == "__main__":
    raise SystemExit(cli())
