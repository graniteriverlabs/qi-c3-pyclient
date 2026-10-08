"""
Print the test cases the current description file makes applicable. Runs nothing.

    python fetch_test_cases.py [APPLICATION]

Useful before a run: the applicable set depends entirely on the description file, so this is how
you find the exact case names to put in Manual_test_cases.json.
"""
import json
import sys
from pathlib import Path

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

        manager = client.legacy.project_manager
        directories = manager._setup_directories()
        manager._sync_spec_mode_to_esdf(directories)
        manager.create_project(project_name="fetch-only")

        listing = (Path(directories["root_dir"]) / "Test_Case_List_From_System"
                   / client.config.config_manager.app_name / "Received_test_cases.json")
        cases = json.loads(listing.read_text(encoding="utf-8")) if listing.is_file() else []
        print("{0} applicable case(s)".format(len(cases)))
        for name in cases:
            print(" ", name)
        return 0
    finally:
        client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
