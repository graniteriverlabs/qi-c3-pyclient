"""
Supply everything from the script: application, tester, project, description file and test cases.

    python run_from_script.py

Nothing here has to be in grl_config.json. Whatever you leave out falls back to the configuration
file, so you can take this script and delete the arguments you do not need - the run still works.

Compare with run_compliance.py, which supplies nothing and takes it all from the configuration.
"""
from grlps_c3_client import GRLApiClient, config_path

APPLICATION = "GRL-C3-MP-TPR"
TESTER = None            # e.g. "192.0.2.77"; None uses the configured address
PROJECT = "MyProject"
DESCRIPTION_FILE = None  # e.g. "esdf/MyDevice.json"; None uses the configured one

# The exact names must match what the description file makes applicable. Get them with
# `c3-testcases`, or with get_testcases.main() in a script. A name that is not applicable is
# dropped and named in the log rather than sent and silently ignored. ["ALL"] runs every case the
# description file allows; an empty list runs nothing.
TEST_CASES = [
    "8.1.1 PTX.CPX.PNG.S01.EPT.001",
    "8.1.2 PTX.CPX.PNG.S01.EPT.002",
]


def main():
    try:
        with GRLApiClient(str(config_path()), app=APPLICATION, ip_address=TESTER) as client:
            # One call does the rest: create the project, load the description file, sync the
            # power profile, select these cases, submit them, run, and collect the report.
            status = client.run_compliance(
                project_name=PROJECT,
                esdf=DESCRIPTION_FILE,
                test_cases=TEST_CASES,
            )
    except RuntimeError as exc:          # the application did not start, or no tester
        print(exc)
        return 1

    print(status)
    return 0 if status == ["Test Execution completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
