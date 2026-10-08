# client/core/api_interface.py
"""
Main API interface for enum-based API calls.
"""
from typing import Dict, Any, Optional, List

from API import GRLApiHandler, ApiName
from API.diagnostics_api_handler import DiagnosticsApiHandler


class ApiInterface:
    """
    Handles the main API interface with enum-based calls.
    """

    def __init__(self, logger):
        """Initialize the API interface."""
        self.logger = logger
        self.api_handler: Optional[GRLApiHandler] = None
        self.diagnostics_handler: Optional[DiagnosticsApiHandler] = None
        self.base_url: Optional[str] = None

    def initialize_api_handler(self, api_handler) -> bool:
        """
        Use the existing API handler instead of creating a duplicate.

        Args:
            api_handler: Already initialized GRLApiHandler instance from app_manager

        Returns:
            bool: True if initialization successful, False otherwise
        """
        try:
            if not api_handler:
                self.logger.error("No API handler provided")
                return False

            # Use the existing handler (no duplication)
            self.api_handler = api_handler
            self.base_url = api_handler.base_url

            self.logger.info(f"Using existing API handler with base URL: {self.base_url}")

            # Initialize diagnostics handler
            self.diagnostics_handler = DiagnosticsApiHandler(self.api_handler, self.logger)

            # Test the connection (reuse existing handler)
            self.logger.info("Testing API connection with existing handler...")
            test_response = self.call_api(ApiName.GET_SOFTWARE_VERSION)

            if test_response["response"].get("success"):
                version = test_response["response"].get("data", "Unknown")
                self.logger.info(f"✅ API interface initialized successfully. Software version: {version}")
                return True
            else:
                error = test_response["response"].get("error", "Unknown error")
                self.logger.error(f"❌ API interface test failed: {error}")
                return False

        except Exception as e:
            self.logger.error(f"Failed to initialize API interface: {e}")
            return False

    def call_api(self, api_name: ApiName, data: Optional[Dict] = None,
                 params: Optional[Dict] = None, endpoint_params: Optional[Dict] = None,
                 endpoint_override: Optional[str] = None) -> Dict[str, Any]:
        """
        Universal method to call any API using enum values.

        Args:
            api_name: ApiName enum value
            data: Optional request payload for POST/PUT requests
            params: Optional query parameters
            endpoint_params: Optional parameters for URL template substitution
            endpoint_override: Optional override for the entire endpoint path

        Returns:
            Dictionary with consistent response format
        """
        if not self.api_handler:
            self.logger.error("API handler not initialized. Call launch_app() first.")
            return {
                "request": {"api_name": api_name.value},
                "response": {
                    "success": False,
                    "error": "API handler not initialized. Call launch_app() first."
                }
            }

        self.logger.debug(f"API Call: {api_name.value}")

        try:
            return self.api_handler.call_api(api_name, data, params, endpoint_params, endpoint_override)
        except Exception as e:
            self.logger.error(f"Exception in call_api for {api_name.value}: {e}")
            return {
                "request": {
                    "api_name": api_name.value,
                    "data": data,
                    "params": params,
                    "endpoint_params": endpoint_params,
                    "endpoint_override": endpoint_override
                },
                "response": {
                    "success": False,
                    "error": f"Client exception: {str(e)}"
                }
            }

    def is_api_ready(self) -> bool:
        """Check if the API handler is ready for use."""
        return self.api_handler is not None and self.api_handler.has_api_definitions()

    def get_available_apis(self) -> List[str]:
        """Get list of all available API endpoints."""
        if not self.is_api_ready():
            return []
        return [api_name.value for api_name in ApiName]

    def check_api_health(self) -> Dict[str, Any]:
        """Quick health check of API endpoints."""
        if not self.diagnostics_handler:
            return {"error": "Diagnostics handler not available"}
        return self.diagnostics_handler.check_api_health(use_parallel=True)

    def disconnect(self) -> None:
        """Close the API handler and clean up resources."""
        if self.api_handler:
            try:
                self.api_handler.close()
                self.logger.info("API handler closed")
            except Exception as e:
                self.logger.warning(f"Error closing API handler: {e}")

        # Reset handlers
        self.api_handler = None
        self.diagnostics_handler = None
        self.base_url = None
