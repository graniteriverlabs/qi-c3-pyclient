"""
Run one C3 session end to end: launch, connect, then compliance cases or an exerciser session.

From a checkout::

    python sample_run.py

Installed, this same module backs the ``c3-run`` and ``c3-exerciser`` commands, so what you read
here is exactly what those commands do.

Which of the two it runs comes from ``common.run_mode`` in the configuration - "compliance"
(the default) or "exerciser" - unless a caller passes ``mode`` explicitly.
"""
import sys
from typing import Any, Dict, List, Optional

from client.grl_api_client import GRLApiClient


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
         dry_run: Optional[bool] = None) -> Dict[str, Any]:
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
        test_cases: the cases to run.             None -> ``Manual_test_cases.json``, or every
                                                  applicable case when that file is absent
        sequence_file: exported exerciser file.   None -> ``files.ExerciserSequenceModel``
        dry_run: compose but send nothing.        None -> ``common.exerciser.dry_run``

    Returns:
        A dict describing how far the run got. A step that never happened is absent, so the
        absence of a key is itself the signal - callers do not have to guess from a bare False.
        Keys: ``launched``, ``connected``, ``mode``, and then ``exerciser`` or ``compliance``.
    """
    outcome: Dict[str, Any] = {"launched": False, "connected": False}
    client = GRLApiClient(config_file_path=config_file_path, app=app, ip_address=ip_address)

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

        success_data = connection_result.get("success")
        print("Connected to Test Equipment successfully!")
        if isinstance(success_data, dict):
            print(f"Tester Status: {success_data.get('testerStatus')}")
            print(f"Firmware Version: {success_data.get('firmwareVersion')}")
        outcome["connected"] = True

        # Step 3: Run compliance test list OR a manual exerciser session, per config
        # common.run_mode ("compliance" default, or "exerciser"), unless the caller said which.
        run_mode = mode or client.config_manager.run_mode
        outcome["mode"] = run_mode

        if run_mode == "exerciser":
            result = client.run_exerciser(sequence_file=sequence_file, dry_run=dry_run)
            _report_exerciser(result)
            outcome["exerciser"] = result
        else:
            status = client.set_project(project_name=project, esdf=esdf, test_cases=test_cases)
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
    int, so this maps the run onto a process exit code: 0 when the session ran, 1 when it stopped
    early. ``main`` leaves a step's key out when the step never happened, so the absence of a key
    is what is checked here.
    """
    result = main(config_file_path=config_file_path, app=app, mode=mode, **overrides)

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

    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
