# client/modules/popup_manager.py
"""
Updated popup and dialog management module.
Uses enum-based API calls for all operations.
"""

import json
import os
import time
from typing import Dict, Any, Optional

from API import ApiName


class PopupManager:
    """
    Manages popup handling using enum-based API calls.
    """

    def __init__(self, logger, config_manager, system_state):
        """
        Initialize the PopupManager.

        Args:
            logger: Logger instance for debug and error logging
            config_manager: Configuration manager instance
            system_state: SystemState instance for tracking current test case
        """
        self.logger = logger
        self.system_state = system_state
        self.config_manager = config_manager

        # State variables
        self.api_handler = None
        self.popup_thread_active = False
        # In hybrid mode, shared with the WebSocket transport so a pop-up is answered once.
        self.popup_coordinator = None
        # Whether the most recent GetMessageBox poll found a pop-up up (WP 1.2). Reflects the
        # REST build's live pop-up state so the data-stall watchdog can pause during operator
        # waits (e.g. coil placement, which the app re-shows until the operator acts). Set on
        # every poll in answer_via_rest — near-real-time given the 500 ms poll cadence.
        self.popup_active = False

        # JSON file names for popup logging
        self.all_popup_json_name = None
        self.test_cases_popup_json_name = None
        self.create_empty_json_file()

    def set_api_handler(self, api_handler):
        """Set the API handler instance."""
        self.api_handler = api_handler

    def set_popup_coordinator(self, coordinator):
        """Share a pop-up coordinator (hybrid mode) so each pop-up is answered by one channel."""
        self.popup_coordinator = coordinator

    def create_empty_json_file(self) -> None:
        """Create empty JSON files for popup logging."""
        try:
            from utils.project_root import project_root
            root_dir = project_root()

            # Create runtime directory structure
            base_output_dir = os.path.join(root_dir, "Run_time_files")
            os.makedirs(base_output_dir, exist_ok=True)

            # Get app_name and create subdirectory
            app_name = self.config_manager.app_name or "Unknown_App"
            popup_json_dir = os.path.join(base_output_dir, app_name)
            os.makedirs(popup_json_dir, exist_ok=True)

            # Log the paths for debugging
            self.logger.debug(f"Root directory: {root_dir}")
            self.logger.debug(f"Popup JSON directory: {popup_json_dir}")

            # Define file paths
            self.all_popup_json_name = os.path.join(popup_json_dir, self.config_manager.all_popup)
            self.test_cases_popup_json_name = os.path.join(popup_json_dir, self.config_manager.test_popup)

            # Create empty JSON files
            for file_path, label in [
                (self.all_popup_json_name, "All popup JSON"),
                (self.test_cases_popup_json_name, "Test Cases Popup")
            ]:
                with open(file_path, 'w') as file:
                    json.dump([], file, indent=4)
                self.logger.info(f"Created empty - {label} file: {file_path}")

        except Exception as e:
            self.logger.error(f"Failed to create empty JSON files: {e}")

    def start_popup_thread(self) -> None:
        """Start the popup handling thread."""
        self.popup_thread_active = True
        self.logger.debug("Popup handler thread started")

    def stop_popup_thread(self) -> None:
        """Stop the popup handling thread."""
        self.popup_thread_active = False
        self.logger.debug("Popup handler thread exiting")

    def _handle_popups(self) -> None:
        """
        Internal thread function to handle popups during operations.
        """
        while self.popup_thread_active:
            try:
                self._handle_connection_popup()
                time.sleep(0.5)  # Check for popups every 500ms
            except Exception as e:
                self.logger.error(f"Popup handler thread error: {str(e)}")

    def _handle_connection_popup(self) -> None:
        """
        Handle a popup during connection automatically using enum-based API.
        """
        self.answer_via_rest()

    def answer_via_rest(self) -> bool:
        """
        Answer the currently-displayed message box over REST (PutMessageBoxResponse).

        This is the mechanism the app actually uses to dismiss a pop-up, and it works on
        every build — including WebSocket builds, which still serve the REST endpoints.
        It is naturally idempotent: once a pop-up is answered the app clears the box, so a
        later call simply finds nothing and returns False.

        Returns:
            bool: True if a pop-up was found and a response was sent, False otherwise.
        """
        if not self.api_handler:
            return False

        # Fetch the popup data using enum API
        try:
            response = self.api_handler.call_api(ApiName.GET_MESSAGE_BOX)
        except Exception as e:
            self.logger.debug(f"answer_via_rest: GetMessageBox failed: {e}")
            return False

        if not response.get("response", {}).get("success"):
            return False

        popup_data = response.get("response", {}).get("data")
        if not popup_data:
            self.popup_active = False
            return False

        # An empty message box is the normal idle case (polled continuously) — return quietly
        # without logging, so the poll loop doesn't spam the log.
        if not popup_data.get("message"):
            self.popup_active = False
            return False

        # A pop-up is up right now — reflect it for the data-stall watchdog (WP 1.2), whether
        # or not this channel is the one that answers it.
        self.popup_active = True

        # In hybrid mode, skip if the WebSocket channel already handled this pop-up.
        if self.popup_coordinator and not self.popup_coordinator.claim(popup_data.get("message")):
            return False

        self.save_only_message(popup_data)
        self.save_message_by_test_case(popup_data)

        # Define the popup response data
        popupdata = {
            "userTextBoxInput": "",
            "responseButton": "Ok",
            "shouldTextBoxBeAdded": False,
            "isValid": True,
            "popID": popup_data.get("popID", 23),
            "displayPopUp": False,
            "isDisplayPopUpOpen": False,
            "title": popup_data.get("title", "GRL Test Solution"),
            "message": "",
            "button": "OK",
            "image": "",
            "icon": "Asterisk",
            "isFrontEndPopUp": False,
            "callBackMethod": "",
            "comboBoxEntries": "",
            "selectedComboBoxValue": "",
            "comboBoxEntriesFE": [],
            "selectedComboBoxValueFE": "",
            "onlyDropdownAdded": False,
            "enableTimerOKButton": False,
            "enableCustomUserInputs": False,
            "customInputValues": {}
        }

        try:
            # Send the popup response using enum API
            put_response = self.api_handler.call_api(ApiName.PUT_MESSAGE_BOX_RESPONSE, data=popupdata)

            if put_response["response"].get("success"):
                self.logger.info(f"Popup auto-answered (Ok): {popup_data.get('message', '')[:80]}")
                return True
            else:
                error = put_response["response"].get("error", "Unknown error")
                self.logger.error(f"Failed to send popup response: {error}")
                return False

        except Exception as e:
            self.logger.error(f"Failed to send popup response: {str(e)}")
            return False

    def save_only_message(self, json_data: Dict[str, Any]) -> None:
        """Save a unique popup message to a JSON file."""
        try:
            file_path = self.all_popup_json_name
            message = json_data.get("message")
            if not message:
                self.logger.debug("No message found in the provided data.")
                return

            messages = []

            if os.path.exists(file_path):
                if os.path.getsize(file_path) > 0:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        messages = json.load(f)
                    self.logger.debug(f"Loaded {len(messages)} existing messages from {file_path}.")
                else:
                    self.logger.debug(f"{file_path} is empty. Initializing with an empty list.")
            else:
                self.logger.debug(f"{file_path} does not exist. A new file will be created.")

            messages.append(message)
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(messages, f, indent=4, ensure_ascii=False)
            self.logger.debug("New message saved successfully.")

        except Exception as e:
            self.logger.error(f"Error saving message: {str(e)}")

    def save_message_by_test_case(self, json_data: Dict[str, Any]) -> None:
        """Save popup messages organized by test case name."""
        try:
            file_path = self.test_cases_popup_json_name
            message = json_data.get("message")
            if not message:
                self.logger.debug("No message found in the provided data.")
                return

            # Get the current test case name from system_state
            test_case_name = getattr(self.system_state, 'test_case_name', None)
            if not test_case_name:
                test_case_name = "controller_message"  # Default key
                self.logger.warning("No test case name available, using 'controller_message' as key.")

            # Initialize an empty dictionary to store messages by test case
            messages_by_test = {}

            # Load existing data if the file exists
            if os.path.exists(file_path):
                if os.path.getsize(file_path) > 0:
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            messages_by_test = json.load(f)
                        # Check if loaded data is a dictionary
                        if not isinstance(messages_by_test, dict):
                            self.logger.warning(
                                f"Existing file {file_path} is not in dictionary format. Creating new dictionary."
                            )
                            messages_by_test = {}
                        self.logger.debug(f"Loaded messages for {len(messages_by_test)} test cases from {file_path}.")
                    except json.JSONDecodeError:
                        self.logger.warning(f"Error decoding JSON from {file_path}. Creating new dictionary.")
                        messages_by_test = {}
                else:
                    self.logger.debug(f"{file_path} is empty. Initializing with an empty dictionary.")
            else:
                self.logger.debug(f"{file_path} does not exist. A new file will be created.")

            # Initialize list for this test case if it doesn't exist
            if test_case_name not in messages_by_test:
                messages_by_test[test_case_name] = []
                self.logger.debug(f"Created new entry for test case: {test_case_name}")

            # Add message to the list
            messages_by_test[test_case_name].append(message)
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(messages_by_test, f, indent=4, ensure_ascii=False)
            self.logger.debug(f"Message saved for test case '{test_case_name}': {message[:50]}...")

        except Exception as e:
            self.logger.error(f"Error saving message: {str(e)}")
