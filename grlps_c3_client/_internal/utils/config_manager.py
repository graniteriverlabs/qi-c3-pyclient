# utils/config_manager.py
"""
Configuration manager for GRL API.
Handles loading application configuration settings from JSON file.
"""

import json
import logging
from typing import Dict, Any, Optional


class GRLConfigManager:
    """
    Simple manager for GRL application configuration.
    Handles loading configuration settings from JSON file.
    """

    def __init__(self, config_file_path: str = "grl_config.json"):
        """
        Initialize the configuration manager.

        Args:
            config_file_path (str): Path to the configuration JSON file
        """
        self.config_file_path = config_file_path
        self.config = {}  # Store config data

        # Initialize with a null logger
        self.logger = logging.getLogger("NullLogger")
        self.logger.addHandler(logging.NullHandler())

        # Initialize attributes
        self.ip_address = None
        self.load_from_json = None
        self.project_name_with_time_stamp = None
        self.initial_wait = None
        self.log_filename = None
        self.default_log_mode = None
        self.log_level = "INFO"
        self.max_connection_attempts = None
        self.connection_timeout = None
        self.api_timeout = None
        self.app_name = None
        self.app_path = None
        self.known_port = None
        self.file_map=None
        self.all_popup=None
        self.test_popup=None
        self.report_file_name=None
        self.is_multiple_esdf_files = None
        self.esdf_folder_name = None
        self.websocket_config = {}

        # Load configuration when instantiated
        self.load_config()

    def set_logger(self, logger: logging.Logger) -> None:
        """
        Set the logger to be used by this class.

        Args:
            logger (logging.Logger): Logger instance to use
        """
        self.logger = logger
        self.logger.debug("Logger set for GRLConfigManager")

    def load_config(self) -> bool:
        """
        Load configuration from the JSON file.

        Returns:
            bool: True if config loaded successfully, False otherwise
        """
        try:
            with open(self.config_file_path, 'r') as config_file:
                self.config = json.load(config_file)

            # Load from JSON flag
            load_from_json = self.config.get("load_from_json")
            if isinstance(load_from_json, str):
                self.load_from_json = load_from_json.lower() == "true"
            else:
                self.load_from_json = load_from_json

            # Project name with timestamp
            project_name_ts = self.config.get("project_name_with_time_stamp")
            if isinstance(project_name_ts, str):
                self.project_name_with_time_stamp = project_name_ts.lower() == "true"
            else:
                self.project_name_with_time_stamp = project_name_ts

            # Load common settings
            common = self.config.get("common", {})
            self.initial_wait = common.get("initial_wait")
            self.log_filename = common.get("log_filename")
            self.default_log_mode = common.get("default_log_mode")
            # Logging verbosity: INFO by default (production); set "DEBUG" for full tracing.
            self.log_level = common.get("log_level", "INFO")
            self.max_connection_attempts = common.get("max_connection_attempts")
            self.connection_timeout = common.get("connection_timeout")
            self.api_timeout = common.get("api_timeout")
            self.all_popup=common.get("All_popup_msg_JSON_name")
            self.test_popup=common.get("Test_cases_wise_popup_msg_JSON_name")
            self.report_file_name=common.get("Report_JSON_name")
            # Opt-in raw HTTP trace (Phase 4 / debugging). Off by default; when true, every
            # HTTP call (incl. the capture manager's polls/report POSTs) is logged to a JSONL.
            self.trace_http = bool(common.get("trace_http", False))
            # Run mode: "compliance" (default — set_project runs the certification test list)
            # or "exerciser" (license-gated session driven by the app's exported sequence file).
            self.run_mode = str(common.get("run_mode", "compliance")).strip().lower()
            # Exerciser run controls. The session's CONTENT (packets, controller settings, phase
            # timings) comes from the app's exported sequence file named per-app in `files`;
            # this block only says how the session runs. Defaults are the safe ones: dry run on,
            # strict verification, halt on the first failure.
            self.exerciser = dict(common.get("exerciser", {}) or {})

            # Live-data transport settings (global defaults). Absent block == auto-detect.
            # A per-app "websocket" block (below) overrides these for the selected app.
            global_websocket_config = self.config.get("websocket", {}) or {}

            # Load application-specific settings
            selected_app = self.config.get('Selected_app')
            return self.select_app(selected_app, global_websocket_config)

        except Exception as e:
            self.logger.error(f"Exception during config loading: {str(e)}")
            return False

    def select_app(self, selected_app: Optional[str],
                   global_websocket_config: Optional[Dict[str, Any]] = None) -> bool:
        """
        Apply one application's settings as the active ones.

        This client drives four applications from a single configuration file, and which of them a
        run uses is a per-run choice: the command line can name one without the config file being
        edited. Loading the config applies ``Selected_app``; calling this again re-points every
        derived field at a different application.

        Args:
            selected_app: key under "applications"
            global_websocket_config: the global transport block; re-read from the config when None

        Returns:
            True when the application was applied, False when it is missing or not configured.
        """
        try:
            if global_websocket_config is None:
                global_websocket_config = self.config.get("websocket", {}) or {}

            # Validate that selected app exists in applications
            if not selected_app:
                self.logger.error(
                    "No application is selected in configuration (Selected_app field is missing or empty)")
                return False

            available_apps = self.config.get("applications", {})
            if selected_app not in available_apps:
                self.logger.error(f"Selected application '{selected_app}' is not available in applications configuration")
                self.logger.error(f"Available applications: {list(available_apps.keys())}")
                return False

            # Load the selected application configuration
            app_config = available_apps[selected_app]
            self.app_name = app_config.get("app_name")
            self.app_path = app_config.get("app_path")
            self.known_port = app_config.get("known_port")
            self.file_map = app_config.get("files", {})
            self.is_multiple_esdf_files = app_config.get("is_multiple_esdf_files", False)
            self.esdf_folder_name = self.file_map.get("EsdfFolderName", "esdf")

            # Per-app transport override merges over the global block. This lets a specific
            # app (e.g. a WebSocket build that under-reports GetWebSocketSupport as 404) be
            # pinned to "ws" without changing the global default for the others.
            app_websocket_config = app_config.get("websocket", {}) or {}
            self.websocket_config = {**global_websocket_config, **app_websocket_config}
            # Get IP address ONLY from application config
            self.ip_address = app_config.get("ip_address")
            if not self.ip_address:
                self.logger.error(f"No IP address found for application '{selected_app}'")
            self.logger.info(f"Successfully loaded configuration for selected app: '{selected_app}'")
            self.logger.debug(f"Using IP address: {self.ip_address} for application: {self.app_name}")
            self.logger.debug(f"Multiple ESDF files: {self.is_multiple_esdf_files}")
            self.logger.debug(f"ESDF folder name: {self.esdf_folder_name}")

            # Perform validation of the selected app
            if not self.validate_selected_app():
                self.logger.warning(f"Selected application '{selected_app}' has configuration issues")
            return True

        except Exception as e:
            self.logger.error(f"Exception during config loading: {str(e)}")
            return False

    def validate_selected_app(self) -> bool:
        """
        Whether the active application has everything a run needs.

        Warns rather than fails: a field can legitimately be absent while the caller supplies it
        another way, for example an IP address passed to ``connect()``. The call to this method
        existed before the method did, so every ``load_config()`` raised AttributeError, swallowed
        it and returned False. Nothing broke only because no caller checked the result.
        """
        missing = [name for name in ("app_name", "app_path", "known_port", "ip_address")
                   if not getattr(self, name, None)]
        if missing:
            self.logger.warning(
                f"Application '{self.app_name or '?'}' is missing: {', '.join(missing)}")
            return False
        return True

    def get_app_config(self, app_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Get configuration for a specific application.

        Args:
            app_name: Name of the application to get config for.
                     If None, uses the default app.

        Returns:
            Dict containing the application configuration, or empty dict if not found
        """
        if app_name is None:
            app_name = self.config.get("default_app")
            self.logger.debug(f"Using selected app: {app_name}")

        if not app_name:
            self.logger.warning("No application name provided and no selected app configured")
            return {}

        app_config = self.config.get("applications", {}).get(app_name, {})
        if app_config:
            self.logger.debug(f"Found configuration for app '{app_name}'")
        else:
            self.logger.warning(f"No configuration found for app '{app_name}'")

        return app_config

    def get_available_apps(self) -> list:
        """
        Get a list of all available applications in the configuration.

        Returns:
            list: List of application names
        """
        return list(self.config.get("applications", {}).keys())

    def get_ip_address(self, app_name: Optional[str] = None) -> Optional[str]:
        """
        Get the IP address for a specific application.

        Args:
            app_name: Name of the application to get IP for.
                     If None, uses the current app.

        Returns:
            IP address string or None if not found
        """
        if app_name is None:
            # Return the already loaded IP address for current app
            return self.ip_address

        # Look up IP address for specified app
        app_config = self.get_app_config(app_name)
        if app_config and "ip_address" in app_config:
            return app_config["ip_address"]

        self.logger.error(f"No IP address found for application '{app_name}'")
        return None

    def get_is_multiple_esdf_files(self, app_name: Optional[str] = None) -> bool:
        """
        Get the is_multiple_esdf_files flag for a specific application.

        Args:
            app_name: Name of the application to get flag for.
                     If None, uses the current app.

        Returns:
            Boolean flag indicating if multiple ESDF files are supported
        """
        if app_name is None:
            # Return the already loaded flag for current app
            return self.is_multiple_esdf_files

        # Look up flag for specified app
        app_config = self.get_app_config(app_name)
        return app_config.get("is_multiple_esdf_files", False)

    def get_esdf_folder_name(self, app_name: Optional[str] = None) -> str:
        """
        Get the ESDF folder name for a specific application.

        Args:
            app_name: Name of the application to get folder name for.
                     If None, uses the current app.

        Returns:
            ESDF folder name string, defaults to "esdf" if not found
        """
        if app_name is None:
            # Return the already loaded folder name for current app
            return self.esdf_folder_name or "esdf"

        # Look up folder name for specified app
        app_config = self.get_app_config(app_name)
        files_config = app_config.get("files", {})
        return files_config.get("EsdfFolderName", "esdf")

    def get_file_mapping(self, app_name: Optional[str] = None) -> Dict[str, str]:
        """
        Get the file mapping configuration for a specific application.

        Args:
            app_name: Name of the application to get file mapping for.
                     If None, uses the current app.

        Returns:
            Dictionary containing file mappings
        """
        if app_name is None:
            # Return the already loaded file mapping for current app
            return self.file_map or {}

        # Look up file mapping for specified app
        app_config = self.get_app_config(app_name)
        return app_config.get("files", {})

    def get_selected_app(self) -> Optional[str]:
        """
        Get the currently selected application name from configuration.

        Returns:
            Selected application name or None if not configured
        """
        return self.config.get("Selected_app","Unknown_App")

    def is_app_available(self, app_name: str) -> bool:
        """
        Check if a specific application is available in the configuration.

        Args:
            app_name: Name of the application to check

        Returns:
            True if application exists in configuration, False otherwise
        """
        available_apps = self.get_available_apps()
        return app_name in available_apps