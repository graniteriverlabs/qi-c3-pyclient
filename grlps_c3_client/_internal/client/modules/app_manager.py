# client/modules/app_manager.py
"""
Updated Application lifecycle management module.
Uses enum-based API calls for all operations.
"""

import contextlib
import os
from typing import Optional, Dict, Any

from API import GRLApiHandler, ApiName
from utils import app_control
from utils.web_app_controller import WebAppController


class AppManager:
    """
    Manages the GRL application lifecycle using enum-based API calls.
    """
    
    def __init__(self, config_manager, logger):
        """
        Initialize the AppManager with configuration and logging.
        
        Args:
            config_manager: GRLConfigManager instance with application config
            logger: Logger instance for debug and error logging
        """
        self.config_manager = config_manager
        self.logger = logger
        
        # Extract configuration values
        self.app_path = config_manager.app_path
        self.known_port = config_manager.known_port
        self.initial_wait = config_manager.initial_wait
        
        # State variables
        self.base_url = None
        self.controller = None
        self.api_handler = None
        self.stop_requested = False
        #: How this run reaches the application (utils.app_control), set by launch_app().
        self.control = None
        #: Why launch_app() last returned False, in full. None after a success.
        self.last_error = None
        #: Set when the application must be left open whatever open_and_close_app says - the
        #: person at the bench took control back in its window.
        self.leave_open_reason = None

    def _fail(self, message: str) -> bool:
        """Keep the full reason for the caller, log it, and return False."""
        self.last_error = message
        self.logger.error(message)
        return False

    def launch_app(self) -> bool:
        """
        Reach the GRL application: open it here, or use the one already open.

        ``open_and_close_app`` true (the default) - the application is opened on this PC through
        WebAppController if it is not already running, and closed by ``disconnect()``.
        false - the application already open on ``app_host`` is used and left open.
        This is the ONLY place where GRLApiHandler should be created.
        """
        self.last_error = None
        self.control = app_control.resolve_from(self.config_manager)
        if self.control.problems:
            return self._fail("\n\n".join(self.control.problems))
        if not self.control.open_and_close:
            return self._use_open_application(self.control)

        self.logger.info(f"Launching GRL application: {self.app_path} on port {self.known_port}")

        try:
            # Step 1: Initialize the WebAppController
            self.controller = WebAppController(self.app_path, known_port=self.known_port)
            self.controller.set_logger(self.logger)

            # Step 2: Start the application and get the base URL
            self.base_url = self.controller.start_and_get_url(initial_wait=self.initial_wait)

            if self.base_url:
                self.logger.info(f"✅ Application base URL: {self.base_url}")

                # Step 3: Create the SINGLE API handler instance (the only one)
                clean_base_url = self.base_url.rstrip('/')
                api_base_url = f"{clean_base_url}/api"

                self.logger.info(f"🔧 API base URL: {api_base_url}")
                self.api_handler = GRLApiHandler(api_base_url, self.logger)

                if not self.api_handler.has_api_definitions():
                    self.logger.error("Missing or empty API configuration(grl_api_config.json).")
                    return False

                # Step 4: Test the connection
                response = self.api_handler.call_api(ApiName.GET_SOFTWARE_VERSION)
                if response["response"].get("success"):
                    version_str = response["response"].get("data", "Unknown")
                    self.logger.info(f"✅ Connected to GRL App successfully. Software version: {version_str}")
                    return True
                else:
                    error = response["response"].get("error", "Unknown error")
                    self.logger.error(f"❌ Failed to get software version: {error}")
                    return False
            else:
                self.logger.error("Failed to connect to application - No base URL returned")
                return self._fail(self._launch_failure_detail())

        except Exception as e:
            self.logger.error(f"Exception during application launch: {str(e)}")
            return False

    def _launch_failure_detail(self) -> str:
        """Why opening the application on this PC did not give a reachable application."""
        control = self.control
        source = "{0} is {1}.".format(app_control.FLAG, control.flag_source)
        if not self.app_path or not os.path.isfile(self.app_path):
            return (
                "{0} is not running on this PC, and it is not installed where the configuration "
                "says:\n  {1}\n\n"
                "{2} is true, so this run opens the application itself. Either:\n"
                "  - set applications.{3}.app_path in {4} to the application's .exe, or\n"
                "  - open {0} yourself and set \"{2}\": false.\n\n{5}".format(
                    control.app, self.app_path or "(app_path is not set)", app_control.FLAG,
                    self.config_manager.selected_app_key or control.app, control.config_file,
                    source))
        return (
            "{0} was opened from\n  {1}\nbut did not answer at {2} in time.\n\n"
            "Check that port {3} (applications.{4}.known_port in {5}) is the port of {0}, and "
            "that no other program is using it. The log has each step.".format(
                control.app, self.app_path,
                getattr(self.controller, "web_url", None) or control.base_url, self.known_port,
                self.config_manager.selected_app_key or control.app, control.config_file))

    def _use_open_application(self, control) -> bool:
        """
        open_and_close_app is false: use the application that is already open, start nothing, and
        leave it open. No WebAppController is made, so disconnect() has nothing it could close.
        """
        self.logger.info(f"open_and_close_app is false: using the application already open on "
                         f"{control.where} at {control.base_url}; it is left open at the end.")
        reason = app_control.unreachable_reason(control.host, control.port)
        if reason:
            return self._fail(app_control.unreachable_message(control, reason))

        self.base_url = control.base_url
        self.api_handler = GRLApiHandler(f"{self.base_url}/api", self.logger)
        if not self.api_handler.has_api_definitions():
            return self._fail("Missing or empty API configuration (grl_api_config.json).")
        try:
            response = self.api_handler.call_api(ApiName.GET_SOFTWARE_VERSION)
            answer = response.get("response") or {}
        except Exception as exc:  # the handler normally reports errors in the response
            answer = {"success": False, "error": repr(exc)}
        if answer.get("success"):
            self.logger.info(f"✅ Connected to the open GRL App at {self.base_url}. "
                             f"Software version: {answer.get('data', 'Unknown')}")
            return True
        status = answer.get("status_code")
        error = answer.get("error") or ("HTTP {0}".format(status) if status else "no answer")
        self.api_handler.close()
        self.api_handler = None
        self.base_url = None
        return self._fail(app_control.not_grl_message(control, error))

    def disconnect(self) -> None:
        """
        Safely disconnect from the GRL application and close sessions.
        """
        self.logger.info("Disconnecting from GRL application")
        
        # Step 1: Close the API handler
        if self.api_handler:
            try:
                self.api_handler.close()
                self.logger.info("API handler closed successfully")
            except Exception as e:
                self.logger.warning(f"Error closing API handler: {str(e)}")
            self.api_handler = None
        
        # Step 2: Stop the application process
        if self.controller and getattr(self, "leave_open_reason", None):
            self.controller.detach()     # garbage collection must not close it either
            self.logger.info(f"Left the application open at {self.base_url}: "
                             f"{self.leave_open_reason}.")
            self.controller = None
        elif self.controller:
            try:
                # Dropping the reference used to be all this did, which left closing the
                # application to WebAppController.__del__ - i.e. to garbage collection. That is
                # not a shutdown: a console script like c3-run exits straight after this, and
                # __del__ may run late or not at all, so whether the application closed came down
                # to timing. Stop it deliberately, then drop the reference.
                stopped = self.controller.stop_process()
                if stopped:
                    self.logger.info("Application stopped")
                else:
                    self.logger.warning(
                        "The application was not stopped - it may still be running and still "
                        "driving the tester. Close it from its own window before the next run.")
            except Exception as e:
                self.logger.warning(f"Error during controller cleanup: {str(e)}")
            finally:
                self.controller = None
        elif (getattr(self, "control", None) is not None and not self.control.open_and_close
              and self.base_url):
            # open_and_close_app is false: the application was already open and is not ours to close.
            self.logger.info(f"Left the application open at {self.base_url} "
                             f"(open_and_close_app is false).")

        # Step 3: Clear the base URL
        self.base_url = None

    def get_api_handler(self):
        """
        Get the API handler instance. Other components should use this method.

        Returns:
            GRLApiHandler: The initialized API handler or None if not available
        """
        return getattr(self, 'api_handler', None)
    
    def get_base_url(self) -> Optional[str]:
        """Get the base URL of the running application."""
        return self.base_url

    def _check_api_handler(self) -> None:
        """
        Ensure API handler is initialized before performing API operations.
        
        Raises:
            Exception: If the API handler is not initialized.
        """
        if not self.api_handler:
            error_msg = "API handler not initialized. Please call launch_app() first."
            self.logger.error(error_msg)
            raise Exception(error_msg)