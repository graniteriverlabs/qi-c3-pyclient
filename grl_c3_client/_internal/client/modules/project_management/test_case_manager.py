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
            # Get test cases from API using enum
            test_cases_response = self.api_handler.call_api(ApiName.GET_TEST_CASE_LIST)

            # Some applications (C3-TPR, and TPT in its BPP/EPP mode) serve the case tree ONLY
            # from the profile-scoped route and return an empty list from the plain one — which
            # reads as "no applicable cases" rather than as an error. Retry by profile before
            # believing that. Probed live: every profile returned [] on the plain route.
            if self._is_empty_list(test_cases_response) and power_profile:
                self.logger.info(f"Empty test case list; retrying scoped to power profile "
                                 f"'{power_profile}'")
                test_cases_response = self.api_handler.call_api(
                    ApiName.GET_TEST_CASE_LIST_BY_PROFILE,
                    endpoint_params={"powerProfile": power_profile})

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

            # Check if Manual_test_cases.json file is available
            if os.path.exists(input_file_path):
                try:
                    with open(input_file_path, 'r') as input_file:
                        manual_test_cases = json.load(input_file)

                    # Create a new list to store matching test cases
                    newlist = []

                    # Check each test case name in Manual_test_cases.json with enabled_keys
                    for test_case_name in manual_test_cases:
                        if test_case_name in enabled_keys:
                            newlist.append(test_case_name)
                            #self.logger.info(f"Test case '{test_case_name}' found in enabled_keys and added to newlist")
                        else:
                            self.logger.warning(f"Test case '{test_case_name}' not found in Received TestCases List, skipping")

                    # Save the filtered test cases to output file
                    with open(output_file_path, 'w') as output_file:
                        json.dump(newlist, output_file, indent=2)


                    self.logger.info(
                        f"{len(newlist)} matching test cases from manual test cases saved to {output_file_path}")
                    return newlist

                except json.JSONDecodeError as e:
                    self.logger.error(f"Error parsing JSON file {input_file_path}: {e}")
                    # Fallback to original behavior
                    with open(output_file_path, 'w') as output_file:
                        json.dump(enabled_keys, output_file, indent=2)


                    self.logger.info(f"{len(enabled_keys)} enabled keys extracted and saved to {output_file_path}")
                    return enabled_keys

                except Exception as e:
                    self.logger.error(f"Error reading file {input_file_path}: {e}")
                    # Fallback to original behavior
                    with open(output_file_path, 'w') as output_file:
                        json.dump(enabled_keys, output_file, indent=2)
                    self.logger.info(f"{len(enabled_keys)} enabled keys extracted and saved to {output_file_path}")
                    return enabled_keys
            else:
                # File doesn't exist, use original behavior
                self.logger.warning(f"Manual test cases file {input_file_path} not found, using Received TestCases List")
                with open(output_file_path, 'w') as output_file:
                    json.dump(enabled_keys, output_file, indent=2)

                self.logger.info(f"{len(enabled_keys)} enabled keys extracted and saved to {output_file_path}")
                return enabled_keys

        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in file {json_file_path}: {str(e)}")
        except Exception as e:
            self.logger.error(f"Failed to process file {json_file_path}: {str(e)}")

        return []

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