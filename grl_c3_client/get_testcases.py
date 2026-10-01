"""
Fetch the test cases the current device description file makes applicable. Runs no tests.

From a checkout::

    python get_testcases.py

Installed, this same module backs the ``c3-testcases`` command.

The applicable set depends entirely on the description file, so this loads the file and creates a
project first - which is why it needs the application running and the tester connected. It stops
before submitting anything, so no test is executed.

Use it to find the exact case names to put in ``Manual_test_cases.json``.
"""
import json
import os
import sys
from typing import Any, Dict, List, Optional

from client.grl_api_client import GRLApiClient


def _applicable_cases(client) -> List[str]:
    """Load the description file, create the project, and read back the enabled cases."""
    manager = client.legacy.project_manager
    directories = manager._setup_directories()

    # A real run syncs the controller to the description file's power profile before the case list
    # is read. Do the same here, or the list can reflect the profile from a previous run.
    manager._sync_spec_mode_to_esdf(directories)
    manager.create_project(project_name="get-testcases")

    # create_project writes every enabled leaf case here as a flat list.
    listing = os.path.join(directories["root_dir"], "Test_Case_List_From_System",
                           client.config_manager.app_name, "Received_test_cases.json")
    if not os.path.exists(listing):
        return []
    with open(listing, encoding="utf-8") as handle:
        cases = json.load(handle)
    return cases if isinstance(cases, list) else []


def main(config_file_path: str = "grl_config.json",
         app: Optional[str] = None,
         out: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch the applicable cases and return them.

    Args:
        config_file_path: the configuration file to use
        app: which application to drive, overriding ``Selected_app`` for this run
        out: also write the list to this path as JSON

    Returns:
        ``{"launched", "connected", "application", "cases", "listing"}``. A step that never
        happened leaves its key out.
    """
    outcome: Dict[str, Any] = {"launched": False, "connected": False}
    client = GRLApiClient(config_file_path=config_file_path, app=app)

    try:
        if not client.launch_app():
            print("Failed to launch GRL application.")
            return outcome
        outcome["launched"] = True

        connection_result = client.connect()
        if "error" in connection_result:
            print(f"Connection failed: {connection_result['error']}")
            outcome["error"] = connection_result["error"]
            return outcome
        outcome["connected"] = True

        app_name = client.config_manager.app_name
        cases = _applicable_cases(client)
        outcome["application"] = app_name
        outcome["cases"] = cases

        print(f"{app_name}: {len(cases)} applicable test case(s)")
        for name in cases:
            print(f"  {name}")

        listing = os.path.join("Test_Case_List_From_System", app_name,
                               "Received_test_cases.json")
        outcome["listing"] = listing
        print(f"Full list: {listing}")
        print("Put the ones you want to run in "
              f"Test_Case_List_From_System/{app_name}/Manual_test_cases.json")

        if out:
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            with open(out, "w", encoding="utf-8") as handle:
                json.dump(cases, handle, indent=2)
            print(f"Written to {os.path.abspath(out)}")
        return outcome

    except Exception as e:
        print(f"Exception occurred: {str(e)}")
        outcome["error"] = str(e)
        return outcome

    finally:
        client.disconnect()


def cli(config_file_path: str = "grl_config.json",
        app: Optional[str] = None,
        out: Optional[str] = None) -> int:
    """Console-script entry point: 0 when a list was fetched, 1 when it was not."""
    result = main(config_file_path=config_file_path, app=app, out=out)

    if not result.get("launched"):
        print("[get_testcases] the application did not start.", file=sys.stderr, flush=True)
        return 1
    if not result.get("connected"):
        print("[get_testcases] the tester was not connected.", file=sys.stderr, flush=True)
        return 1
    if not result.get("cases"):
        print("[get_testcases] no applicable cases - check the description file named by "
              "files.EsdfConfigurationModel.", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
