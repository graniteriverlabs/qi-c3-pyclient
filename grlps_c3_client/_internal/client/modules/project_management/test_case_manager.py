# client/modules/project_management/test_case_manager.py
"""
Handles test case operations including saving and processing test case lists.
Updated to use enum-based API calls.
"""
import json
import os
from typing import List, Dict, Any

from API import ApiName


class TestCaseManager:
    """
    Manages test case operations including:
    - Retrieving test cases from API
    - Saving test case lists to JSON files
    - Processing and filtering enabled test cases
    Updated to use enum-based API calls.
    """

    def __init__(self, config_manager, logger):
        self.config_manager = config_manager
        self.logger = logger
        self.api_handler = None
        self.is_test_list_with_project_name = False

    def set_api_handler(self, api_handler):
        """Set the API handler instance."""
        self.api_handler = api_handler

    @staticmethod
    def _is_empty_list(response: Dict) -> bool:
        """True when the call succeeded but carried no test cases at all."""
        try:
            if not response["response"].get("success"):
                return False
            data = response["response"].get("data")
            return isinstance(data, list) and not data
        except Exception:
            return False

    @staticmethod
    def _case_count(response: Dict) -> int:
        """
        How many leaf test cases a case-list response carries. 0 when it carries none.

        Used only to compare two answers for the same project, so it counts leaves rather than
        trying to interpret the tree: the applications nest cases differently (a list holding
        one tree, or the tree itself) and a count that depends on the nesting would compare
        two shapes rather than two case sets.
        """
        try:
            if not response["response"].get("success"):
                return 0
            data = response["response"].get("data")
        except Exception:
            return 0

        def leaves(node) -> int:
            if isinstance(node, dict):
                return sum(leaves(v) for v in node.values())
            if isinstance(node, list):
                return sum(leaves(v) for v in node)
            return 1

        if isinstance(data, list) and not data:
            return 0
        return leaves(data)

    def save_and_process_test_cases(self, project_name: str, root_dir: str,
                                    power_profile: str = None,
                                    test_cases: List[str] = None) -> List[str]:
        """
        Save test case list to JSON file and process enabled test cases using enum-based API.

        Args:
            project_name: Name of the project for the output filename
            root_dir: Root directory of the project
            power_profile: the ESDF's power profile, used only if the plain route comes back
                empty (see below)
            test_cases: cases to run, supplied by the caller. When given these are used instead of
                `Manual_test_cases.json`; when None the file is read as before.

        Returns:
            List of enabled test case keys, or empty list on failure
        """
        try:
            # The case list must follow the DESCRIPTION FILE, not whatever the controller was
            # last left on. Without the profile there is nothing to check the list against and
            # the profile-scoped route cannot be used, so the run stops here instead of
            # quietly fetching some other profile's cases. RCA: `_esdf_power_profile` returns
            # None on any failure, which used to mean "no sync, no scoped retry, carry on" -
            # and a 2026-09-16 run did exactly that, executing MPP25 while the description
            # file said APP15.
            if not power_profile:
                self.logger.error(
                    "Cannot fetch test cases: the power profile of the selected description "
                    "file could not be determined.\n"
                    "  The applicable cases depend entirely on that file, so fetching them "
                    "without it would return whatever profile the controller happens to be "
                    "set to.\n"
                    "  Check that the description file named in the configuration exists, "
                    "loads, and declares a PowerProfile."
                )
                return []

            # Get test cases from API using enum
            test_cases_response = self.api_handler.call_api(ApiName.GET_TEST_CASE_LIST)
            source = "the project"

            # Some applications (C3-TPR, and TPT in its BPP/EPP mode) serve the case tree ONLY
            # from the profile-scoped route and return an empty list from the plain one — which
            # reads as "no applicable cases" rather than as an error. Retry by profile before
            # believing that. Probed live: every profile returned [] on the plain route.
            if self._is_empty_list(test_cases_response):
                self.logger.info(f"Empty test case list; retrying scoped to power profile "
                                 f"'{power_profile}'")
                test_cases_response = self.api_handler.call_api(
                    ApiName.GET_TEST_CASE_LIST_BY_PROFILE,
                    endpoint_params={"powerProfile": power_profile})
                source = f"power profile '{power_profile}'"
            else:
                # The plain route answers for the project, which carries the description file,
                # so it is normally right. "Normally" is not good enough on its own: ask the
                # profile-scoped route as well and compare. If the two disagree, the scoped
                # answer is the one that is provably for this description file's profile -
                # the profile is in the URL - so it wins, and the disagreement is reported
                # rather than left to be discovered in a report.
                scoped = self.api_handler.call_api(
                    ApiName.GET_TEST_CASE_LIST_BY_PROFILE,
                    endpoint_params={"powerProfile": power_profile})
                plain_n = self._case_count(test_cases_response)
                scoped_n = self._case_count(scoped)
                if scoped_n and scoped_n != plain_n:
                    self.logger.warning(
                        "The application offers {0} case(s) for power profile '{1}' but {2} "
                        "for the current project. Using the {0} scoped to '{1}', because that "
                        "is the profile the selected description file declares.".format(
                            scoped_n, power_profile, plain_n)
                    )
                    test_cases_response = scoped
                    source = f"power profile '{power_profile}'"

            self.logger.info(
                f"Test case list fetched for power profile '{power_profile}' (from {source})"
            )

            if not test_cases_response["response"].get("success"):
                error_msg = test_cases_response['response'].get('data', 'Unknown error')
                self.logger.error(f"Failed to retrieve test cases: {error_msg}")
                return []

            data = test_cases_response["response"].get("data")

            # Handle list response format
            if isinstance(data, list):
                if not data:
                    self.logger.warning("Test case list is empty. Nothing written to file.")
                    return []
                data_to_write = data[0]
            else:
                data_to_write = data
                try:
                    json.dumps(data_to_write)  # Validate JSON serializability
                except TypeError as e:
                    self.logger.error(f"Data is not JSON serializable: {e}")
                    return []

            # Setup directories and save test cases
            json_file_path = self._save_test_cases_to_file(data_to_write, project_name, root_dir)
            if not json_file_path:
                return []

            # Process and return enabled test cases
            return self._create_selected_test_cases_json(json_file_path, test_cases)

        except Exception as e:
            self.logger.error(f"Failed to save and process test cases: {e}")
            return []

    def _save_test_cases_to_file(self, data: Dict, project_name: str, root_dir: str) -> str:
        """
        Save test case data to JSON file.

        Returns:
            Path to the saved file, or empty string on failure
        """
        try:
            # Create output directories
            base_output_dir = os.path.join(root_dir, "Test_Case_List_From_System")
            os.makedirs(base_output_dir, exist_ok=True)

            app_name = self.config_manager.app_name or "Unknown_App"
            app_output_dir = os.path.join(base_output_dir, app_name)
            os.makedirs(app_output_dir, exist_ok=True)

            # Determine filename
            filename = (f"Test_cases_list_{project_name}.json" if self.is_test_list_with_project_name 
                       else "Generated_Test_cases_list.json")
            json_file_path = os.path.join(app_output_dir, filename)

            # Write JSON data
            with open(json_file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)

            self.logger.info(f"Test case data saved to {json_file_path}")
            return json_file_path

        except Exception as e:
            self.logger.error(f"Failed to save test cases to file: {e}")
            return ""

    def _create_selected_test_cases_json(self, json_file_path: str,
                                         test_cases: List[str] = None) -> List[str]:
        """
        Process a JSON file to extract enabled keys and save the results.

        Args:
            json_file_path: Full path to the JSON file to process
            test_cases: cases supplied by the caller, used instead of `Manual_test_cases.json`

        Returns:
            List of enabled test case keys (empty if operation failed)
        """
        try:
            if not os.path.exists(json_file_path):
                self.logger.error(f"File not found: {json_file_path}")
                return []

            with open(json_file_path, 'r') as file:
                json_data = json.load(file)

            enabled_keys = self._extract_enabled_keys_from_json(json_data)

            # A caller-supplied list wins over the file, and is filtered the same way: a name the
            # description file does not make applicable cannot be run, so it is dropped and named
            # rather than sent and silently ignored.
            if test_cases is not None:
                folder_path = os.path.dirname(json_file_path)
                with open(os.path.join(folder_path, "Received_test_cases.json"), 'w') as fh:
                    json.dump(enabled_keys, fh, indent=2)
                selected = [name for name in test_cases if name in enabled_keys]
                for name in test_cases:
                    if name not in enabled_keys:
                        self.logger.warning(
                            f"Test case '{name}' was supplied by the caller but is not applicable "
                            f"for this description file, skipping")
                with open(os.path.join(folder_path, "selected_test_cases.json"), 'w') as fh:
                    json.dump(selected, fh, indent=2)
                self.logger.info(f"{len(selected)} of {len(test_cases)} caller-supplied test "
                                 f"case(s) are applicable and will be run")
                return selected
            # Save all enabled keys to received test cases file
            folder_path = os.path.dirname(json_file_path)
            output_file_received_path = os.path.join(folder_path, "Received_test_cases.json")
            with open(output_file_received_path, 'w') as output_file:
                json.dump(enabled_keys, output_file, indent=2)

            # Save selected test cases

            output_file_path = os.path.join(folder_path, "selected_test_cases.json")

            input_file_path = os.path.join(folder_path, "Manual_test_cases.json")

            # An empty selection means the same thing as no selection file at all: nothing has
            # been chosen, so every applicable case runs. `c3-init` creates this file empty so the
            # user has something to edit, and deciding on the file's PRESENCE would turn that into
            # "run nothing" for everyone who has not edited it yet.
            manual_test_cases, reason = self._read_manual_selection(input_file_path)

            if manual_test_cases is None:
                self.logger.info(f"No manual selection ({reason}), running every applicable case")
                with open(output_file_path, 'w') as output_file:
                    json.dump(enabled_keys, output_file, indent=2)
                self.logger.info(f"{len(enabled_keys)} enabled keys extracted and saved to "
                                 f"{output_file_path}")
                return enabled_keys

            # A name the description file does not make applicable cannot be run, so it is dropped
            # and named rather than sent and silently ignored.
            newlist = []
            for test_case_name in manual_test_cases:
                if test_case_name in enabled_keys:
                    newlist.append(test_case_name)
                else:
                    self.logger.warning(f"Test case '{test_case_name}' not found in Received "
                                        f"TestCases List, skipping")

            with open(output_file_path, 'w') as output_file:
                json.dump(newlist, output_file, indent=2)

            self.logger.info(f"{len(newlist)} matching test cases from manual test cases saved to "
                             f"{output_file_path}")
            return newlist

        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in file {json_file_path}: {str(e)}")
        except Exception as e:
            self.logger.error(f"Failed to process file {json_file_path}: {str(e)}")

        return []

    def _read_manual_selection(self, path: str) -> tuple:
        """
        The cases named in `Manual_test_cases.json`, or None when no choice has been made.

        Returns (names, reason). `names` is None whenever the file does not express a choice -
        missing, blank, or an empty list - and the caller then runs every applicable case, which
        is what this client has always done when the file was absent.

        `c3-init` creates the file empty, so "exists" stopped being the same question as "has a
        selection in it". Reading an empty file as a selection of nothing would run no cases for
        every user who has not edited it yet.

        A file that is present but unreadable is a different matter: that is the user asking for
        specific cases in a way we cannot understand, so it is reported as an error rather than
        quietly turned into a full run.
        """
        if not os.path.exists(path):
            return None, "no {0}".format(os.path.basename(path))

        try:
            with open(path, 'r', encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            self.logger.error(f"Could not read {path}: {exc}. Running every applicable case")
            return None, "unreadable"

        if not text.strip():
            return None, "{0} is empty".format(os.path.basename(path))

        try:
            parsed = json.loads(text)
        except ValueError as exc:
            self.logger.error(f"{path} is not valid JSON ({exc}), so the cases listed in it "
                              f"cannot be read. Running every applicable case instead - correct "
                              f"the file if you meant to run a subset")
            return None, "invalid JSON"

        if not isinstance(parsed, list):
            self.logger.error(f"{path} should hold a list of test case names, found "
                              f"{type(parsed).__name__}. Running every applicable case")
            return None, "not a list of names"

        names = [str(item) for item in parsed if str(item).strip()]
        if not names:
            return None, "{0} lists no cases".format(os.path.basename(path))
        return names, ""

    def _extract_enabled_keys_from_json(self, json_data: Any) -> List[str]:
        """
        Recursively extract all keys from JSON data where 'enable' is True
        and the node is a leaf node (has empty children array).
        """
        enabled_keys = []

        def is_test_case_node(node):
            return 'children' in node and node['children'] == []

        def traverse(node):
            if not isinstance(node, dict):
                return

            is_enabled = node.get('enable') is True
            has_key = 'key' in node

            if is_enabled and has_key and is_test_case_node(node):
                enabled_keys.append(node['key'])
                self.logger.debug(f"Enabled leaf node found with key: {node['key']}")

            if 'children' in node and isinstance(node['children'], list):
                for child in node['children']:
                    traverse(child)

        self.logger.debug("Starting JSON tree traversal to extract enabled keys.")
        if isinstance(json_data, list):
            for item in json_data:
                traverse(item)
        elif isinstance(json_data, dict):
            traverse(json_data)
        else:
            self.logger.warning("Invalid JSON format: expected list or dict at root.")

        self.logger.debug(f"Finished traversal. Total enabled keys extracted: {len(enabled_keys)}")
        return enabled_keys