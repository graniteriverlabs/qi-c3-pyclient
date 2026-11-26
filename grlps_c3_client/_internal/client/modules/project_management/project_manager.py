# client/modules/project_management/project_manager.py
"""
Main Project Manager - Orchestrates project operations.
Updated to use enum-based API calls for all operations.
"""
import os
from datetime import datetime
from typing import Dict, List, Optional, Any

from API import ApiName
from . import case_selection
from .coil_data_manager import CoilDataManager
from .esdf_manager import EsdfManager
from .project_config_manager import ProjectConfigManager
from .test_case_manager import TestCaseManager


# The profile and Qi version a run asks for, the spellings each application accepts and what its
# read-back can show are in client.core.qi_request.

#: Per-app controller power-profile guard (B1). set_project aborts if the connected
#: controller's reported ``powerProfile`` is not in the app's allow-set — a safety check
#: that the right controller is attached. An app absent from this map has NO guard (e.g.
#: MPP-TPR, which accepts any profile). Notes:
#:   - GRL-C3-TPT-MPP accepts BOTH MPP15 and MPP25 (was hard-coded MPP25-only, which
#:     blocked the MPP15 cases TC-04…06). The exact MPP15 string is confirmed at first live
#:     run (0.2); if the controller reports a different token, add it here — do not guess.
#:   - GRL-C3-TPT-BPP-EPP reports "BPP", which covers EPP testing too (expected, not a bug).
_PROFILE_GUARD = {
    # One install, one tester; which profile it runs is a firmware choice the operator makes,
    # so this entry accepts every profile that mode can report. Narrowing it to {"BPP"} blocked
    # a legitimate EPP/MPP run with "Controller is wrong".
    "GRL-C3-TPT-BPP-EPP": {"BPP", "EPP", "MPP15", "MPP25"},
    "GRL-C3-TPT-MPP": {"MPP15", "MPP25"},
}


class ProjectManager:
    """
    Main project manager that orchestrates project operations.
    Delegates specific tasks to specialized managers.
    Updated to use enum-based API calls.
    """

    def __init__(self, config_manager, logger, client=None):
        """
        Initialize the ProjectManager with specialized sub-managers.

        Args:
            config_manager: GRLConfigManager instance with project config
            logger: Logger instance for debug and error logging
            client: Optional client instance
        """
        self.config_manager = config_manager
        self.logger = logger
        self.client = client
        self.api_handler = None

        # Initialize specialized managers
        self.config_loader = ProjectConfigManager(config_manager, logger)
        self.esdf_manager = EsdfManager(config_manager, logger)
        self.test_case_manager = TestCaseManager(config_manager, logger)
        self.coil_manager = CoilDataManager(config_manager, logger)

        # Configuration values
        self.project_name_with_time_stamp = config_manager.project_name_with_time_stamp
        self.app_name = config_manager.app_name

    def set_api_handler(self, api_handler):
        """Set the API handler for all sub-managers."""
        self.api_handler = api_handler
        self.config_loader.set_api_handler(api_handler)
        self.esdf_manager.set_api_handler(api_handler)
        self.test_case_manager.set_api_handler(api_handler)
        self.coil_manager.set_api_handler(api_handler)

    def create_project(self, project_name: str = None, esdf_file_path: str = None,
                       esdf_file_name: str = None, test_cases: List[str] = None) -> List[str]:
        """
        Set up a new project on the test system using enum-based API calls.

        Args:
            project_name: Name of the project
            esdf_file_path: Custom path to load the EsdfConfigurationModel
            esdf_file_name: Name of the ESDF file
            test_cases: cases to run, used instead of `Manual_test_cases.json` when given

        Returns:
            List of test cases if project created successfully, otherwise empty list
        """
        try:
            # Setup directories
            directories = self._setup_directories()

            # Load project configuration
            put_project_config = self.config_loader.load_project_configuration(directories['json_dir'])
            if not put_project_config:
                return []

            # Load ESDF configuration
            esdf_model = self.esdf_manager.load_esdf_model(
                directories['json_dir'], esdf_file_path
            )
            if not esdf_model:
                return []

            # The request - this description file's profile at its Qi version - is checked against
            # the licence and set on the application HERE, before a project exists, for the exact
            # file this project uses. Every way in passes through: a run, a run per file in the
            # esdf folder, and fetching the test cases. Checking later would let an unlicensed
            # request get as far as selecting cases (seen 2026-10-08: a BPP project created with
            # three cases on a controller with no BPP licence).
            refusal = self._check_request(directories, esdf_file_path)
            if refusal:
                self.test_case_manager.last_problem = (
                    "Test Execution did not start: " + refusal.splitlines()[0])
                return []

            put_project_config["EsdfConfigurationModel"] = esdf_model

            # Setup project name
            project_name = self._setup_project_name(
                put_project_config["ProjectConfigurationModel"],
                project_name,
                esdf_file_name
            )

            # Save configuration and create report
            self._save_project_data(put_project_config, directories['run_time_app_json_dir'])
            report_data = self._create_report_data(project_name)

            # Clear capture and create project using enum API
            self._clear_capture()
            response = self.api_handler.call_api(ApiName.PUT_PROJECT_FOLDER, data=put_project_config)

            test_case_list = []
            if response['response']['success']:
                self.logger.info(f"Project '{project_name}' created successfully.")
                report_data["is_created"] = True
                test_case_list = self.test_case_manager.save_and_process_test_cases(
                    project_name, directories['root_dir'],
                    power_profile=self._esdf_power_profile(directories),
                    test_cases=test_cases
                )
            else:
                error_msg = response.get('response', {}).get('data', 'Unknown error')
                self.logger.error(f"Project creation failed: {error_msg}")

            # Save report
            self._save_report(report_data, directories['run_time_app_json_dir'])

            # Save project configuration response
            self._save_project_config_response(directories['run_time_app_json_dir'])

            # Handle coil data for specific applications

            self.coil_manager.process_coil_data(
                directories['json_dir'],
                directories['run_time_app_json_dir']
            )

            return test_case_list or []

        except Exception as e:
            self.logger.error(f"Exception during project setup: {str(e)}")
            return []

    def verify_esdf(self, esdf_file_path: str = None, is_from_edit: bool = False) -> Optional[Dict[str, Any]]:
        """
        Send the selected ESDF to PutVerifyEsdfData (same contract as the C3 UI upload/verify).
        A WPC signed file is verified and flattened by the app first; a legacy flat file is
        parsed locally. Either way the request body is the same Esdf_Elements list.

        Args:
            esdf_file_path: Path to an ESDF JSON file. If None, uses EsdfConfigurationModel from JSON_User_input.
            is_from_edit: Maps to request field isFromEdit (default False per HAR).

        Returns:
            Full API handler result dict, or None on configuration/load error.
        """
        if not self.api_handler:
            self.logger.error("API handler not set")
            return None

        directories = self._setup_directories()
        path = esdf_file_path
        if not path:
            if getattr(self.config_manager, "is_multiple_esdf_files", False):
                self.logger.error("verify_esdf requires esdf_file_path when is_multiple_esdf_files is true")
                return None
            model_key = "EsdfConfigurationModel"
            if model_key not in self.config_manager.file_map:
                self.logger.error(f"Missing '{model_key}' in file map for app '{self.config_manager.app_name}'")
                return None
            path = os.path.join(directories["json_dir"], self.config_manager.file_map[model_key])

        return self.esdf_manager.put_verify_esdf_data(path, is_from_edit=is_from_edit)

    @staticmethod
    def _profile_guard_ok(app_name, power_profile):
        """
        Whether the connected controller's reported ``power_profile`` is acceptable for this
        app (B1 guard). Returns ``(ok, allowed_set)`` where ``allowed_set`` is None for apps
        with no guard (then ``ok`` is always True). Pure/testable — no I/O.
        """
        allowed = _PROFILE_GUARD.get(app_name)
        if allowed is None:
            return True, None
        return (power_profile in allowed), allowed

    def _check_request(self, directories, esdf_file_path: str = None) -> str:
        """
        Check and apply what this description file asks for; "" when the project may be created,
        otherwise why not (already logged).

        The request is the file's ``PowerProfile`` at its ``SpecificationSupported`` Qi version.
        The application's own setting is NOT the request: MPP-TPT resets it to its default on
        every tester connect. The decision is `client.core.qi_request` - fail-closed, the same
        for every caller: no licence list, no profile, no version, or an application that does
        not take the request, and nothing is created.
        """
        from client.core import qi_request

        connection_info = None
        client = getattr(self, "client", None)
        legacy = getattr(client, "legacy", None)
        conn_mgr = getattr(legacy, "connection_manager", None)
        if conn_mgr is not None:
            connection_info = getattr(conn_mgr, "last_connection_info", None)

        path = esdf_file_path or os.path.join(
            directories.get("json_dir", ""),
            (self.config_manager.file_map or {}).get("EsdfConfigurationModel", ""))
        source = "description file {0}".format(os.path.basename(path) or "?")
        app_name = getattr(self.config_manager, "app_name", None)

        result = qi_request.check_and_apply(
            self.api_handler, self.logger, connection_info, app_name,
            profile=self._esdf_power_profile(directories, esdf_file_path),
            spec=self._esdf_spec_version(directories, esdf_file_path),
            source=source, profile_from_app=False, write=True,
            allowed_profiles=_PROFILE_GUARD.get(app_name))
        if result["allowed"]:
            return ""
        if result["licence"]:
            self.logger.error("Licence check FAILED - the project was not created.\n  {0}".format(
                result["reason"]))
            return "licence check failed - " + result["reason"]
        self.logger.error("The project was not created: {0}".format(result["reason"]))
        return result["reason"]

    def _esdf_spec_version(self, directories, esdf_file_path: str = None) -> Optional[str]:
        """
        The Qi specification version the selected description file declares, or None.

        A WPC signed file carries it as ``DutInfo.SpecificationSupported`` (verified by the
        application before this is reached); an older flat file (SchemaVersion 2.0) carries the
        same key at its top level. A file that declares none falls back, visibly, to the
        application's setting.
        """
        import json

        try:
            path = esdf_file_path or os.path.join(
                directories["json_dir"],
                self.config_manager.file_map["EsdfConfigurationModel"])
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                return None
            holder = raw["DutInfo"] if isinstance(raw.get("DutInfo"), dict) else raw
            value = holder.get("SpecificationSupported")
            if value not in (None, ""):
                return str(value).strip()
            for e in raw.get("Esdf_Elements") or []:
                if isinstance(e, dict) and e.get("Field") == "SpecificationSupported":
                    value = e.get("Value")
                    return str(value).strip() if value not in (None, "") else None
        except Exception as e:
            self.logger.debug(f"Could not read the ESDF's SpecificationSupported: {e}")
        return None

    def _esdf_power_profile(self, directories, esdf_file_path: str = None) -> Optional[str]:
        """
        The PowerProfile declared by the selected ESDF (e.g. 'MPP15'), or None.

        Honours a per-run file path, so the licence check and the case fetch judge the file this
        run will actually use rather than the one named in the configuration.
        """
        try:
            path = esdf_file_path or os.path.join(
                directories["json_dir"],
                self.config_manager.file_map["EsdfConfigurationModel"])
            model = self.esdf_manager._load_esdf_config_model(path)
            if not model:
                return None
            for e in model.get("Esdf_Elements", []):
                if e.get("Field") == "PowerProfile":
                    return e.get("Value")
        except Exception as e:
            self.logger.debug(f"Could not read ESDF PowerProfile: {e}")
        return None

    def set_project(self, project_name: str = None, esdf: str = None,
                    test_cases: List[str] = None):
        """
        Runs set_project for each ESDF JSON file in a folder.

        Every argument is optional and falls back to the configuration file, so a caller can
        supply as much or as little as they like:

        Args:
            project_name: Base name of the project. None -> `ProjectConfigurationModel`.
            esdf: description file to use, relative to this application's input folder
                (e.g. "esdf/MyDevice.json"). None -> `files.EsdfConfigurationModel`.
            test_cases: cases to run, or ["ALL"] for every case the description file allows.
                None -> `Manual_test_cases.json`. An empty selection runs nothing.

        Returns:
            One status line. "Test Execution completed" is the only one that means the run
            finished; anything else says why it did not.
        """
        # With several description files every file in the folder is run, each with the selection
        # file, so one file or one list passed for this run has nowhere to apply. Say so rather
        # than run something other than what was asked for.
        refusal = self.override_refusal(esdf, test_cases)
        if refusal:
            self.logger.error(refusal)
            return [refusal]

        # Applied to the in-memory file map only, and put back exactly afterwards - removed again
        # if it was not there - so supplying an ESDF for one run never rewrites the user's
        # configuration and never outlives the run.
        if self.config_manager.file_map is None:
            self.config_manager.file_map = {}
        file_map = self.config_manager.file_map
        key = "EsdfConfigurationModel"
        had, original_esdf = key in file_map, file_map.get(key)
        if esdf:
            file_map[key] = esdf
            self.logger.info(f"Description file for this run: {esdf}")
        try:
            return self._set_project(project_name, test_cases)
        finally:
            if esdf:
                if had:
                    file_map[key] = original_esdf
                else:
                    file_map.pop(key, None)

    def override_refusal(self, esdf: Optional[str], test_cases: Optional[List[str]]) -> str:
        """
        Why ``esdf`` or ``test_cases`` passed for this run cannot be used; empty when they can.
        """
        if not getattr(self.config_manager, "is_multiple_esdf_files", False):
            return ""
        passed = [name for name, value in (("esdf", esdf), ("test_cases", test_cases))
                  if value is not None]
        if not passed:
            return ""
        app = self.config_manager.app_name
        return ("{0} cannot be used for {1}: applications.{1}.is_multiple_esdf_files is true, so "
                "every description file in its folder is run, each with Manual_test_cases.json.\n\n"
                "Set is_multiple_esdf_files to false to run one description file, or leave {0} "
                "out.".format(" and ".join(passed), app))

    def selection(self, test_cases: Optional[List[str]] = None) -> "case_selection.Selection":
        """
        The cases this run has been asked for: ``test_cases`` when given, else this
        application's ``Manual_test_cases.json``. Reads files only; sends nothing.
        """
        path = case_selection.selection_path(self._setup_directories()["root_dir"],
                                             self.config_manager.app_name or "Unknown_App")
        return case_selection.resolve(test_cases, path)

    def _set_project(self, project_name: str = None, test_cases: List[str] = None):
        """The run itself, once any per-run overrides are in place."""
        try:
            directories = self._setup_directories()

            # Nothing is sent to the application, and the controller is not touched, until it is
            # known that there is something to run. An empty or unusable selection used to run
            # every applicable case - 636 on MPP-TPR - which is never what it asked for.
            selection = self.selection(test_cases)
            if not selection.runnable:
                message = selection.refusal(self.config_manager.app_name)
                self.logger.error(message)
                return [message]
            self.test_case_manager.last_problem = ""

            # The profile and Qi version are set on the application, checked against the licence
            # and against what this application can run, inside create_project - per description
            # file, so a folder of files is checked file by file. See _check_request.

            # Check if multiple ESDF files processing is enabled
            if not getattr(self.config_manager, 'is_multiple_esdf_files', False):
                self.logger.info("Processing single ESDF file")
                test_cases_list = self.create_project(project_name, test_cases=test_cases)

                # Every refusal reaches here with an empty list: a description file the
                # application could not verify, a profile that cannot be read, a licence that
                # does not cover what was asked. This branch used to have no return for that
                # case, so control fell through to the multiple-ESDF path below - which processed
                # EVERY description file in the folder and then reported "Test Execution
                # completed with warnings/errors". A refusal was both ignored and reported as a
                # completion. Seen on the bench on 2026-10-08 with a tampered file: rejected ten
                # times over, and the caller was told the execution had completed.
                if not test_cases_list:
                    # The selection was usable, so an empty list has one of two causes, and only
                    # the case manager knows which: none of the names is one the description file
                    # allows, or the project was never created at all.
                    message = self.test_case_manager.last_problem or (
                        "Test Execution did not start: no project was created. "
                        "The reason is logged above - the description file was refused, "
                        "its power profile could not be read, or the licence does not "
                        "cover the profile asked for.")
                    self.logger.error(message)
                    return [message]

                if not self.client:
                    message = ("Test Execution did not start: {0} case(s) were selected but no "
                               "client is attached to submit them".format(len(test_cases_list)))
                    self.logger.error(message)
                    return [message]

                result = self.client.submit_test_list(test_cases_list)

                if result.get("success"):
                    if "warning" in result:
                        self.logger.warning(f"Test submitted with warning: {result['warning']}")
                        return [f"Test submitted with warning: {result['warning']}"]
                    # "The application stopped" is not "the run finished". A session that
                    # ends before every selected case has a verdict is reported as what it
                    # is, so no caller can read an incomplete run as a successful one.
                    incomplete = result.get("incomplete") or []
                    if result.get("control_taken") and incomplete:
                        message = ("Test Execution STOPPED: control was taken back at the bench "
                                   "(UI mode was selected in the application window) - {0} of {1} "
                                   "selected case(s) never reached a verdict".format(
                                       len(incomplete), result.get("submitted", "?")))
                        self.logger.error(message)
                        return [message]
                    if incomplete:
                        message = ("Test Execution INCOMPLETE: {0} of {1} selected case(s) "
                                   "never reached a verdict".format(
                                       len(incomplete), result.get("submitted", "?")))
                        self.logger.error(message)
                        return [message]
                    self.logger.info("Test Execution completed successfully.")
                    return ["Test Execution completed"]
                else:
                    error_message = result.get("error", "Unknown error occurred during test execution")
                    if result.get("control_taken"):
                        message = "Test Execution STOPPED: " + error_message
                        self.logger.error(message)
                        return [message]
                    self.logger.error(f"Test Execution failed: {error_message}")
                    return [f"Test Execution failed: {error_message}"]

            # Process multiple ESDF files
            return self.esdf_manager.process_multiple_esdf_files(project_name, directories['json_dir'],
                                                                 self.create_project, self.client)

        except Exception as e:
            self.logger.error(f"Unexpected error in set_project: {str(e)}")
            return []


    def _setup_directories(self) -> Dict[str, str]:
        """Setup and return directory paths."""
        from utils.project_root import project_root
        root_dir = project_root()
        user_json_dir = os.path.join(root_dir, 'JSON_User_input')
        run_time_json_dir = os.path.join(root_dir, "Run_time_files")

        app_name = self.config_manager.app_name or "Unknown_App"
        json_dir = os.path.join(user_json_dir, app_name)
        run_time_app_json_dir = os.path.join(run_time_json_dir, app_name)

        os.makedirs(run_time_json_dir, exist_ok=True)
        os.makedirs(run_time_app_json_dir, exist_ok=True)
        os.makedirs(json_dir, exist_ok=True)

        return {
            'root_dir': root_dir,
            'json_dir': json_dir,
            'run_time_app_json_dir': run_time_app_json_dir
        }

    def _setup_project_name(self, project_model: Dict, project_name: str = None,
                            esdf_file_name: str = None) -> str:
        """Setup and return the final project name with timestamp if needed."""
        if not project_name:
            project_name = project_model.get("projectName") or "Project"
            self.logger.info(f"No project name provided. Using: {project_name}")

        if esdf_file_name:
            project_name = f"{project_name}_{esdf_file_name}"

        if str(self.project_name_with_time_stamp).lower() == "true":
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
            project_name = f"{project_name}_{timestamp}"
            self.logger.info(f"project_name_with_time_stamp is selected. New project name: {project_name}")

        project_model["projectName"] = project_name
        return project_name

    def _save_project_data(self, config_data: Dict, output_dir: str):
        """Save project configuration data to JSON file."""
        import json
        output_path = os.path.join(output_dir, "Set_project_Configuration_Model_data.json")
        with open(output_path, "w") as f:
            json.dump(config_data, f, indent=4)

    def _create_report_data(self, project_name: str) -> Dict:
        """Create initial report data structure."""
        return {
            "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "project_name": project_name,
            "is_created": False
        }

    def _clear_capture(self):
        """Clear capture using enum-based API."""
        from time import sleep
        response_put_clear = self.api_handler.call_api(ApiName.PUT_CLEAR_CAPTURE)
        if response_put_clear['response']['success']:
            self.logger.info("Cleared capture successfully.")
        sleep(0.2)

    def _save_report(self, report_data: Dict, output_dir: str):
        """Save report data to JSON file."""
        import json
        report_file_name = os.path.join(output_dir, self.config_manager.report_file_name)
        with open(report_file_name, "w") as json_file:
            json.dump(report_data, json_file, indent=4)

    def _save_project_config_response(self, output_dir: str):
        """Save project configuration response from API using enum-based call."""
        import json
        response_get_project_configuration = self.api_handler.call_api(ApiName.GET_PROJECT_CONFIGURATION)
        get_project_config = response_get_project_configuration.get('response', {}).get('data', {})
        output_path = os.path.join(output_dir, "Get_project_Configuration_Model_Response.json")
        with open(output_path, "w") as f:
            json.dump(get_project_config, f, indent=4)
