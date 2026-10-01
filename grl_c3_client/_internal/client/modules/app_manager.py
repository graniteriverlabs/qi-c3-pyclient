# client/modules/app_manager.py
"""
Updated Application lifecycle management module.
Uses enum-based API calls for all operations.
"""

import contextlib
from typing import Optional, Dict, Any

from API import GRLApiHandler, ApiName
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

    def launch_app(self) -> bool:
        """
        Launch the GRL application using WebAppController.
        This is the ONLY place where GRLApiHandler should be created.
        """
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
                return False

        except Exception as e:
            self.logger.error(f"Exception during application launch: {str(e)}")
            return False

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
        if self.controller:
            try:
                # Note: WebAppController might not have a stop_process method
                # Just clear the reference for now
                self.controller = None
                self.logger.info("Application controller reference cleared")
            except Exception as e:
                self.logger.warning(f"Error during controller cleanup: {str(e)}")
        
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