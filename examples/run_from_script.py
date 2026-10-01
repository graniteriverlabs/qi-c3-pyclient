"""
Supply everything from the script: application, tester, project, description file and test cases.

    python run_from_script.py

Nothing here has to be in grl_config.json. Whatever you leave out falls back to the configuration
file, so you can take this script and delete the arguments you do not need - the run still works.

Compare with run_compliance.py, which supplies nothing and takes it all from the configuration.
"""
from grl_c3_client import GRLApiClient, config_path

APPLICATION = "GRL-C3-MP-TPR"
TESTER = None            # e.g. "192.0.2.77"; None uses the configured address
PROJECT = "MyProject"
DESCRIPTION_FILE = None  # e.g. "esdf/MyDevice.json"; None uses the configured one

# The exact names must match what the description file makes applicable. Get them with
# `c3-testcases`, or with get_testcases.main() in a script. A name that is not applicable is
# dropped and named in the log rather than sent and silently ignored.
TEST_CASES = [
    "8.1.1 PTX.CPX.PNG.S01.EPT.001",
    "8.1.2 PTX.CPX.PNG.S01.EPT.002",
]


def main():
    client = GRLApiClient(str(config_path()), app=APPLICATION, ip_address=TESTER)
    try:
        if not client.launch_app():
            print("Could not start the application.")
            return 1

        result = client.connect()
        if "error" in result:
            print("Tester not reachable:", result["error"])
            return 1

        info = result.get("success")
        if isinstance(info, dict):
            print("Tester {0}, firmware {1}".format(
                info.get("testerStatus"), info.get("firmwareVersion")))

        # One call does the rest: create the project, load the description file, sync the power
        # profile, select these cases, submit them, run, and collect the report.
        print(client.set_project(
            project_name=PROJECT,
            esdf=DESCRIPTION_FILE,
            test_cases=TEST_CASES,
        ))
        return 0
    finally:
        client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
