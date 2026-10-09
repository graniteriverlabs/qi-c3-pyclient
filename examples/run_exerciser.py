"""
Run an exerciser session from the sequence exported by the application's own UI.

    python run_exerciser.py [APPLICATION]

The sequence is the one named by files.ExerciserSequenceModel in grl_config.json. With HOLD set the
session stops by itself after that many seconds, so it can run unattended; with HOLD = None it runs
until you press Enter, which needs a terminal - without one it is refused before anything is sent.
"""
import sys

from grlps_c3_client import GRLApiClient, config_path

HOLD = None              # e.g. 60 to stop after a minute; None waits for Enter


def main():
    app = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        with GRLApiClient(str(config_path()), app=app) as client:
            outcome = client.run_exerciser(hold=HOLD)
    except RuntimeError as exc:          # the application did not start, or no tester
        print(exc)
        return 1

    for record in outcome.get("results", []):
        print("  {0} {1:<18} {2}".format(
            "ok  " if record.get("success") else "FAIL",
            record.get("op", "?"), record.get("reason") or record.get("verified", "")))

    capture = outcome.get("capture") or {}
    if capture.get("copied_to"):
        print("capture:", capture["copied_to"])
    if outcome.get("error"):
        print(outcome["error"])
    return 0 if outcome.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
