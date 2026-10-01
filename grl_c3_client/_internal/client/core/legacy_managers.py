# client/core/legacy_managers.py
"""
Legacy manager integration for backward compatibility.
"""
from typing import Dict, Any, List

from ..modules.app_manager import AppManager
from ..modules.connection_manager import ConnectionManager
from ..modules.popup_manager import PopupManager
from ..modules import ProjectManager
from ..modules.test_manager import TestManager
from ..modules.data_capture_manager import DataCaptureManager
from ..modules.exerciser_manager import ExerciserManager


class LegacyManagers:
    """
    Manages legacy manager instances for backward compatibility.
    """

    def __init__(self, config_manager, logger, client_instance):
        """Initialize legacy managers."""
        self.logger = logger

        # Initialize managers
        self.app_manager = AppManager(config_manager, logger)
        self.connection_manager = ConnectionManager(config_manager, logger, client_instance.system_state)
        self.popup_manager = PopupManager(logger, config_manager, client_instance.system_state)
        self.project_manager = ProjectManager(config_manager, logger, client=client_instance)
        self.test_manager = TestManager(logger, client_instance.system_state)

        # Per-case measurement-data capture (report ZIP + packet log + WS streams -> one ZIP
        # per run). Driven by TestManager during a run, so a normal sample_run.py run captures
        # automatically on both builds.
        self.capture_manager = DataCaptureManager(logger, config_manager)
        self.test_manager.set_capture_manager(self.capture_manager)

        # License-gated exerciser mode (manual controller ops). Created here like the others;
        # its API handler is set in launch_app and its license is applied from the cached
        # connect response. No effect unless run_mode == "exerciser".
        self.exerciser_manager = ExerciserManager(config_manager, logger)

    def update_all_managers_logger(self, logger) -> None:
        """Update the logger instance for all managers."""
        self.app_manager.logger = logger
        self.connection_manager.logger = logger
        self.popup_manager.logger = logger
        self.project_manager.logger = logger
        self.test_manager.logger = logger
        self.capture_manager.logger = logger
        self.exerciser_manager.logger = logger

    def setup_legacy_api_handlers(self) -> None:
        """Set up legacy managers with API handlers if needed for backward compatibility."""
        try:
            # Get legacy API handler from AppManager if available
            legacy_api_handler = getattr(self.app_manager, 'api_handler', None)
            if legacy_api_handler:
                self.connection_manager.set_api_handler(legacy_api_handler)
                self.popup_manager.set_api_handler(legacy_api_handler)
                self.project_manager.set_api_handler(legacy_api_handler)
                self.test_manager.set_api_handler(legacy_api_handler)
                self.capture_manager.set_api_handler(legacy_api_handler)
                self.exerciser_manager.set_api_handler(legacy_api_handler)
                self.logger.debug("Legacy API handlers set for managers")
        except Exception as e:
            self.logger.warning(f"Failed to set legacy API handlers: {e}")

    def launch_app(self) -> tuple[bool, Any | None] | tuple[bool, None]:
        """
        Launch the application using AppManager.

        Returns:
            Tuple of (success, base_url)
        """
        result = self.app_manager.launch_app()
        if result:
            base_url = getattr(self.app_manager, 'base_url', None)
            return True, base_url
        return False, None

    def disconnect_app(self) -> None:
        """Disconnect using AppManager."""
        self.app_manager.disconnect()

    # Legacy method implementations
    def set_project(self, project_name: str = None, esdf: str = None,
                    test_cases: List[str] = None) -> str:
        """
        Create the project, load the description file and run the selected cases.

        Each argument overrides the configuration file for this run only; leave one out and the
        configured value is used.
        """
        self.logger.info(f"Legacy set_project called for: {project_name}")
        return self.project_manager.set_project(project_name, esdf=esdf, test_cases=test_cases)

    def submit_test_list(self, test_list: List[str]) -> Dict[str, Any]:
        """Legacy method for test submission using TestManager."""
        self.logger.info(f"Legacy submit_test_list called with {len(test_list)} tests")
        return self.test_manager.submit_test_list(test_list, self.popup_manager)

    def connect(self, ip_address=None) -> Dict[str, Any]:
        """Legacy method for connection using ConnectionManager."""
        self.logger.info(f"Legacy connect called for: {ip_address}")
        result = self.connection_manager.connect(ip_address, self.popup_manager)
        if result.get("success"):
            self.connection_manager.run_diagnostics()
        return result
