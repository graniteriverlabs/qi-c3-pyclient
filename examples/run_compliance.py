"""
Run the selected compliance test cases on one application.

    python run_compliance.py                      # the workspace's Selected_app
    python run_compliance.py GRL-C3-TPT-MPP       # a named application

Run it from a workspace created by c3-init, or set GRL_C3_PROJECT_ROOT to point at one. The cases
are the ones in Test_Case_List_From_System\\<application>\\Manual_test_cases.json - names, or
["ALL"] for every case the description file allows. An empty selection runs nothing.
"""
import sys

from grlps_c3_client import GRLApiClient, config_path


def main():
    app = sys.argv[1] if len(sys.argv) > 1 else None

    # Entering the block starts the application and connects the tester, and raises RuntimeError
    # saying why if either fails. Leaving it - however it is left - closes the application.
    try:
        with GRLApiClient(str(config_path()), app=app) as client:
            # One call does the rest: create the project, load the description file, sync the
            # power profile, select the cases, submit them, run, and collect the report.
            status = client.run_compliance()
    except RuntimeError as exc:
        print(exc)
        return 1

    print(status)
    # Only this status means every selected case ran; anything else says why not.
    return 0 if status == ["Test Execution completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
