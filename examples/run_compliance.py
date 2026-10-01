"""
Run the selected compliance test cases on one application.

    python run_compliance.py                      # the workspace's Selected_app
    python run_compliance.py GRL-C3-TPT-MPP       # a named application

Run it from a workspace created by c3-init, or set GRL_C3_PROJECT_ROOT to point at one.
"""
import sys

from grl_c3_client import GRLApiClient, config_path


def main():
    app = sys.argv[1] if len(sys.argv) > 1 else None
    client = GRLApiClient(str(config_path()), app=app)
    try:
        if not client.launch_app():
            print("Could not start the application.")
            return 1

        # connect() returns a dict; "error" means the tester was not reached. Check it rather
        # than assuming, or the run continues against an application that is not connected.
        result = client.connect()
        if "error" in result:
            print("Tester not reachable:", result["error"])
            return 1

        info = result.get("success")
        if isinstance(info, dict):
            print("Tester {0}, firmware {1}".format(
                info.get("testerStatus"), info.get("firmwareVersion")))

        # One call does the rest: create the project, load the description file, sync the power
        # profile, fetch and filter the cases, submit them, run, and collect the report.
        print(client.set_project())
        return 0
    finally:
        client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
