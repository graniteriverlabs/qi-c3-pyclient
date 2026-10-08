"""
Run an exerciser session from the sequence exported by the application's own UI.

    python run_exerciser.py [APPLICATION]

The session runs until you press Enter, so run this from a terminal. It is refused before
anything is sent if no console is attached, because nothing could then end it.
"""
import sys

from grlps_c3_client import GRLApiClient, config_path


def main():
    app = sys.argv[1] if len(sys.argv) > 1 else None
    client = GRLApiClient(str(config_path()), app=app)
    try:
        if not client.launch_app():
            print("Could not start the application.")
            return 1
        result = client.connect()
        if "error" in result:
            print("Tester not reachable:", result["error"])
            return 1

        outcome = client.run_exerciser()
        for record in outcome.get("results", []):
            print("  {0} {1:<18} {2}".format(
                "ok  " if record.get("success") else "FAIL",
                record.get("op", "?"), record.get("verified", "")))

        capture = outcome.get("capture") or {}
        if capture.get("copied_to"):
            print("capture:", capture["copied_to"])
        return 0 if outcome.get("success") else 1
    finally:
        client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
