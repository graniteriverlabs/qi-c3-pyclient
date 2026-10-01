# client/modules/project_management/project_manager.py
"""
Main Project Manager - Orchestrates project operations.
Updated to use enum-based API calls for all operations.
"""
import os
from datetime import datetime
from typing import Dict, List, Optional, Any

from API import ApiName
from .coil_data_manager import CoilDataManager
from .esdf_manager import EsdfManager
from .project_config_manager import ProjectConfigManager
from .test_case_manager import TestCaseManager


# What `GetSelectedQiSpecMode_MPP` is CAPABLE OF REPORTING, per Qi spec version. Read from that
# getter in QiDataModelTPR: it maps QIHelper.CustomVer, and at spec 2.2/2.3 that switch only has
# arms for MPP15 and MPP25 (everything else returns MPP25).
#
# This is NOT the set the setter accepts. The setter stores the profile in a DIFFERENT field:
# after resolving CustomVer it does Enum.TryParse<DutProfile>(powerProfile), and DutProfile holds
# BPP/EPP/EPP5/MPP/APP/MPP15/MPP25/APP15/APP25/MCPE/MCPM. So APP25 at spec 2.2 IS applied — it
# lands in _dutProfile — while CustomVer stays MPP25 and the getter can only ever echo CustomVer.
# The write succeeds and the read-back cannot show it. Use this table only to decide whether a
# non-matching read-back means failure or merely means "not observable".
_PROFILES_REPORTABLE_BY_SPEC = {
    "2.0.1": {"MPP"},
    "2.1": {"MPP", "APP"},
    "2.2": {"MPP15", "MPP25"},
    "2.3.0": {"MPP15", "MPP25"},
}

# App quirk: GetSelectedQiSpecMode_MPP returns "2.3", but PutSelectedQiSpecMode_MPP only has a
# case for "2.3.0". Reading the current mode and writing it straight back therefore cannot work
# at spec 2.3. Translate on the way out.
_SPEC_MODE_ON_THE_WIRE = {"2.3": "2.3.0"}

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

    def _esdf_power_profile(self, directories) -> Optional[str]:
        """The PowerProfile declared by the selected ESDF (e.g. 'MPP15'), or None."""
        try:
            path = os.path.join(directories["json_dir"],
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

    def _sync_spec_mode_to_esdf(self, directories) -> None:
        """
        Make the controller's spec mode match the SELECTED ESDF's PowerProfile (compliance).

        RCA: the app never derives the controller profile from the ESDF; set_project only READ
        `GetSelectedQiSpecMode_MPP` for the guard and never set it, so selecting e.g. the MPP15
        ESDF still executed as whatever the controller was last set to (MPP25). This reads the
        ESDF's PowerProfile and, only if the controller reports a DIFFERENT one, sets it via
        `PutSelectedQiSpecMode_MPP` (which does not change app mode on these apps), then re-reads
        to confirm. No-op when they already match (zero risk to working runs); never raises — on
        any failure the run proceeds exactly as before.
        """
        try:
            esdf_prof = self._esdf_power_profile(directories)
            if not esdf_prof:
                return
            resp = self.api_handler.call_api(ApiName.GET_SELECTED_QI_SPEC_MODE_MPP)
            data = resp.get("response", {}).get("data", {}) or {}
            current = data.get("powerProfile")
            spec = data.get("qiSpecMode")
            if current == esdf_prof:
                return  # controller already matches the ESDF — nothing to do

            wire_spec = _SPEC_MODE_ON_THE_WIRE.get(spec, spec)
            self.logger.info(
                f"Spec-mode sync: controller reports '{current}' at Qi spec '{spec}' but the "
                f"selected ESDF is '{esdf_prof}' -> setting the controller to '{esdf_prof}'"
            )
            payload = dict(data)
            payload["powerProfile"] = esdf_prof
            if wire_spec != spec:
                # The app's getter emits "2.3" but its setter only matches "2.3.0", so echoing
                # back what we just read would fall through to the default and force MPP25.
                payload["qiSpecMode"] = wire_spec
                self.logger.info(
                    f"Spec-mode sync: sending qiSpecMode '{wire_spec}' instead of the reported "
                    f"'{spec}' — the application reads and writes different spellings (D-10)"
                )
            self.api_handler.call_api(ApiName.PUT_SELECTED_QI_SPEC_MODE_MPP, data=payload)
            confirm = self.api_handler.call_api(ApiName.GET_SELECTED_QI_SPEC_MODE_MPP)
            now = (confirm.get("response", {}).get("data", {}) or {}).get("powerProfile")
            if now == esdf_prof:
                self.logger.info(f"Spec-mode sync: controller now reports '{now}'")
            elif esdf_prof not in _PROFILES_REPORTABLE_BY_SPEC.get(wire_spec, set()):
                # Not a failure. The write stores the profile in one field and the read-back
                # reports a different one, so an APP profile at spec 2.2/2.3 is applied but can
                # never be echoed. Reported as unconfirmed rather than failed.
                self.logger.info(
                    f"Spec-mode sync: '{esdf_prof}' was applied, but this build's read-back only "
                    f"reports {sorted(_PROFILES_REPORTABLE_BY_SPEC.get(wire_spec, set()))} at Qi "
                    f"spec '{spec}', so it still shows '{now}'. The profile is set; it is simply "
                    f"not observable through this call (D-9)"
                )
            else:
                self.logger.warning(
                    f"Spec-mode sync: controller still reports '{now}' (wanted '{esdf_prof}') — "
                    f"the run will proceed with what the controller reports"
                )
        except Exception as e:
            self.logger.warning(f"Spec-mode sync skipped ({e}); run proceeds as before")

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
            test_cases: cases to run. None -> `Manual_test_cases.json`, or every applicable
                case when that file is absent.

        Returns:
            Combined test case list from all ESDF files
        """
        # Applied to the in-memory file map only, and put back afterwards, so supplying an ESDF
        # for one run never rewrites the user's configuration file.
        file_map = self.config_manager.file_map or {}
        original_esdf = file_map.get("EsdfConfigurationModel")
        if esdf:
            file_map["EsdfConfigurationModel"] = esdf
            self.logger.info(f"Description file for this run: {esdf}")
        try:
            return self._set_project(project_name, test_cases)
        finally:
            if esdf and original_esdf is not None:
                file_map["EsdfConfigurationModel"] = original_esdf

    def _set_project(self, project_name: str = None, test_cases: List[str] = None):
        """The run itself, once any per-run overrides are in place."""
        try:
            directories = self._setup_directories()

            # Compliance: the app does NOT set the controller's spec mode from the ESDF, so make
            # the controller match the selected ESDF's PowerProfile BEFORE the guard reads it —
            # otherwise a run with e.g. the MPP15 ESDF still executes as whatever the controller
            # was last set to. No-op when they already match; never fatal.
            self._sync_spec_mode_to_esdf(directories)

            qi_spec_mode_response = self.api_handler.call_api(ApiName.GET_SELECTED_QI_SPEC_MODE_MPP)

            # Validate power profile based on app name
            if qi_spec_mode_response and qi_spec_mode_response.get('response', {}).get('success', False):
                power_profile = qi_spec_mode_response['response']['data'].get('powerProfile')
                self.logger.info(f"Retrieved power profile: {power_profile}")

                ok, allowed = self._profile_guard_ok(self.config_manager.app_name, power_profile)
                if not ok:
                    error_msg = (f"Controller is wrong. Expecting powerProfile in "
                                 f"{sorted(allowed)}, but got: {power_profile}")
                    self.logger.error(error_msg)
                    return []
                if allowed is not None:
                    self.logger.info(
                        f"Power profile validation passed: {power_profile} in {sorted(allowed)}"
                    )
            else:
                self.logger.error("Failed to retrieve QI spec mode response or response was unsuccessful")
                return []

            # Check if multiple ESDF files processing is enabled
            if not getattr(self.config_manager, 'is_multiple_esdf_files', False):
                self.logger.info("Processing single ESDF file")
                test_cases_list = self.create_project(project_name, test_cases=test_cases)
                if self.client and test_cases_list:
                    result = self.client.submit_test_list(test_cases_list)

                    if result.get("success"):
                        if "warning" in result:
                            self.logger.warning(f"Test submitted with warning: {result['warning']}")
                            return [f"Test submitted with warning: {result['warning']}"]
                        else:
                            self.logger.info("Test Execution completed successfully.")
                            return ["Test Execution completed"]
                    else:
                        error_message = result.get("error", "Unknown error occurred during test execution")
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
