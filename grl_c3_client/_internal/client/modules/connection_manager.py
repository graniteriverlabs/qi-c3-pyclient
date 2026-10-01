# client/modules/connection_manager.py
"""
Test equipment connection management module.
Handles connecting to test equipment, diagnostics, and version verification.
Updated to use enum-based API calls.
"""

import threading
import time
from typing import Dict, Any, Optional
import requests
from API import ApiName
from ..transport.app_state import read_app_state

# How long to wait for the app to report the tester actually connected, after the
# ConnectToTestEquipment request is accepted.
_TESTER_CONNECT_TIMEOUT_S = 8.0
_CONNECTED = "CONNECTED"


class ConnectionManager:
    """
    Manages test equipment connections including:
    1. Equipment connection and diagnostics
    2. Version verification
    3. API health checks
    Updated to use enum-based API calls.
    """

    def __init__(self, config_manager, logger, system_state):
        """
        Initialize the ConnectionManager.

        Args:
            config_manager: GRLConfigManager instance with connection config
            logger: Logger instance for debug and error logging
            system_state: SystemState instance for tracking connection state
        """
        self.config_manager = config_manager
        self.logger = logger
        self.system_state = system_state

        # Extract configuration values
        self.ip_address = config_manager.ip_address

        # State variables
        self.api_handler = None
        self.live_transport = None

    def set_api_handler(self, api_handler):
        """Set the API handler instance."""
        self.api_handler = api_handler

    def set_live_transport(self, live_transport):
        """Set the live-data transport (REST polling or WebSocket) used during connect."""
        self.live_transport = live_transport

    def connect(self, ip_address=None, popup_manager=None) -> Dict[str, Any]:
        """
        Connect to the test equipment using enum-based API calls.

        Args:
            ip_address: IP address of the test equipment. Uses config if None.
            popup_manager: PopupManager instance for handling connection dialogs

        Returns:
            Dict: Connection result or error information.
        """
        self.logger.info("Attempting connection to test equipment")
        self._check_api_handler()

        # Step 1: Get the IP address if not provided
        if not ip_address:
            ip_address = self.ip_address

        # Step 2: Validate the IP address
        if not ip_address:
            self.logger.error("No IP address provided or found.")
            return {"success": False, "error": "No IP address available."}

        # Step 3: Start receiving live data, so operator pop-ups raised during the
        # connect are answered. The transport is WebSocket or REST polling depending
        # on the app build; if none was wired up, fall back to the popup thread.
        popup_thread = None
        if self.live_transport:
            self.live_transport.start()
        elif popup_manager:
            popup_manager.start_popup_thread()
            popup_thread = threading.Thread(target=popup_manager._handle_popups, daemon=True)
            popup_thread.start()

        try:
            # Step 4: Connect using enum-based API call
            result = self.api_handler.call_api(
                ApiName.CONNECT_TO_TEST_EQUIPMENT,
                endpoint_override=ip_address
            )

            self._log_connection_result(result)

            # NOTE: result["response"]["success"] is only the HTTP status (200-299). The C3
            # server accepts ConnectToTestEquipment and returns 200 even when no tester is
            # actually reachable at that IP, so HTTP 200 alone does NOT mean "connected".
            if not result["response"].get("success"):
                error_msg = f"Failed to connect to equipment: {result['response'].get('data', 'Unknown error')}"
                self.logger.error(error_msg)
                return {"success": False, "error": error_msg}

            # Confirm the app itself reports the tester connected (EthernetConnectionState),
            # so an unreachable/wrong IP can never be reported as a successful connection.
            if not self._verify_tester_connected():
                error_msg = (f"App did not report the tester connected at {ip_address} "
                             f"(EthernetConnectionState != {_CONNECTED}). Check the tester IP/power/LAN.")
                self.logger.error(error_msg)
                return {"success": False, "error": error_msg}

            self.logger.info(f"Connected to equipment at {ip_address}")
            # Cache the ConnectionSetup response (carries the license fields IsLicenseEnabled /
            # LicenseInfo) so the exerciser license gate can reuse it — no redundant connect.
            self.last_connection_info = result["response"].get("data")
            return {"success": True, "data": self.last_connection_info}

        except Exception as e:
            self.logger.error(f"Exception during connection: {str(e)}")
            return {"success": False, "error": str(e)}

        finally:
            # Step 5: Stop live data. On a WebSocket build the socket is kept open for
            # the test run, so only the REST fallback thread is torn down here.
            if self.live_transport:
                if self.live_transport.name == "REST":
                    self.live_transport.stop()
                    self.logger.info("Live data (REST polling) stopped after connect")
            elif popup_manager and popup_thread:
                popup_manager.stop_popup_thread()
                popup_thread.join(timeout=2)
                self.logger.info("Popup thread safely stopped after connect")

    def _verify_tester_connected(self, timeout: float = _TESTER_CONNECT_TIMEOUT_S) -> bool:
        """
        Confirm the tester is really connected by reading the app's own connection state.

        ConnectToTestEquipment returning HTTP 200 only means the request was accepted; the app
        exposes the true state via GetAppState's EthernetConnectionState. Poll it briefly (the
        link takes a moment to come up) and return True only once it reports CONNECTED.
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                resp = self.api_handler.call_api(ApiName.GET_APP_STATE)
                data = resp.get("response", {}).get("data", {}) or {}
                _, connection_state = read_app_state(data)
                last = connection_state
                # Mirror onto system_state so the rest of the client sees the real state.
                if hasattr(self.system_state, "connection_state"):
                    self.system_state.connection_state = connection_state
                if str(connection_state).upper() == _CONNECTED:
                    return True
            except Exception as e:
                self.logger.debug(f"connection-state check failed: {e}")
            time.sleep(0.5)
        self.logger.warning(f"Tester did not reach {_CONNECTED} within {timeout:.0f}s "
                            f"(last EthernetConnectionState={last})")
        return False

    def run_diagnostics(self) -> None:
        """
        Run full connection diagnostics after successful connection.

        This method performs comprehensive diagnostics including:
        1. Version verification using enum-based APIs
        2. Error log examination
        """
        self.logger.info("🔍 Running connection diagnostics...")

        try:
            # Run version verification using enum-based API calls
            self.logger.info("📋 Verifying software and hardware versions...")
            versions = self.verify_versions()

            if versions:
                self.logger.info("✅ Version Information:")
                for key, value in versions.items():
                    self.logger.info(f"  📄 {key.replace('_', ' ').title()}: {value}")
            else:
                self.logger.warning("⚠️ No version information available")

            # Check error log
            self.logger.debug("🔍 Checking Error Log...")
            self._check_error_log()

            self.logger.info("✅ Connection diagnostics completed successfully")

        except Exception as e:
            self.logger.error(f"❌ Failed to run connection diagnostics: {str(e)}")

    def verify_versions(self) -> Dict[str, str]:
        """
        Verify and return all version information using enum-based API calls.

        Retrieves version information for various components:
        1. Software version
        2. Firmware version
        3. Electronic load version
        4. Short fixture version

        Returns:
            Dict[str, str]: Dictionary mapping version types to their values
        """
        self.logger.info("Verifying software and hardware versions")
        self._check_api_handler()

        try:
            versions = {}

            # Get software version using enum API
            sw_response = self.api_handler.call_api(ApiName.GET_SOFTWARE_VERSION)
            if sw_response["response"].get("success"):
                versions["software_version"] = str(sw_response["response"].get("data", "Unknown"))
            else:
                versions["software_version"] = f"Error: {sw_response['response'].get('error', 'Unknown')}"

            # Get firmware version using enum API
            fw_response = self.api_handler.call_api(ApiName.GET_LATEST_FIRMWARE_VERSION)
            if fw_response["response"].get("success"):
                fw_data = fw_response["response"].get("data", "Unknown")
                versions["firmware_version"] = str(fw_data)
            else:
                versions["firmware_version"] = f"Error: {fw_response['response'].get('error', 'Unknown')}"

            # Get eload version using enum API
            eload_response = self.api_handler.call_api(ApiName.GET_LATEST_ELOAD_VERSION)
            if eload_response["response"].get("success"):
                eload_data = eload_response["response"].get("data", "Unknown")
                versions["eload_version"] = str(eload_data)
            else:
                versions["eload_version"] = f"Error: {eload_response['response'].get('error', 'Unknown')}"

            # Get short fixture version using enum API
            sf_response = self.api_handler.call_api(ApiName.GET_LATEST_SHORT_FIXTURE_VERSION)
            if sf_response["response"].get("success"):
                sf_data = sf_response["response"].get("data", "Unknown")
                versions["short_fixture_version"] = str(sf_data)
            else:
                versions["short_fixture_version"] = f"Error: {sf_response['response'].get('error', 'Unknown')}"

            self.logger.info(f"Versions fetched: {versions}")
            return versions

        except Exception as e:
            self.logger.error(f"Failed to verify versions: {str(e)}")
            raise

    def _check_error_log(self) -> None:
        """Check and log error information from the API."""
        try:
            # Always use the direct HTTP approach since get_error_log isn't available
            base_url = self.api_handler.base_url if self.api_handler else ""
            if not base_url:
                self.logger.warning("Cannot check error log - No base URL available")
                return

            # Construct the error log URL
            if base_url.endswith('/api'):
                api_base_url = base_url
            else:
                api_base_url = f"{base_url.rstrip('/')}/api"

            url = f"{api_base_url}/App/GetErrorLog"

            self.logger.debug(f"Checking error log at: {url}")
            response = requests.get(url, timeout=5)
            self._process_error_log_response(response)

        except Exception as e:
            self.logger.exception(f"Unexpected exception occurred while retrieving error log: {str(e)}")

    def _process_error_log_response(self, response):
        """Process the raw HTTP response from the error log endpoint."""
        status_code = response.status_code
        self.logger.debug(f"Error log response status: {status_code}")

        if 200 <= status_code < 300:
            try:
                error_log = response.json()
                self._process_error_log_data(error_log)
            except ValueError:
                self.logger.debug(f"Error log response is not JSON: {response.text[:100]}")
        else:
            self.logger.warning(
                f"Expected failure: Unable to retrieve error log (Status: {status_code}) - {response.text}"
            )

    def _process_error_log_data(self, error_log):
        """Process the parsed error log data."""
        if isinstance(error_log, list) and error_log:
            self.logger.debug(f"Found {len(error_log)} error log entries:")
            for i, error in enumerate(error_log[:3], 1):
                self.logger.debug(f"  Error {i}: {error.get('message', 'Unknown error')}")
            if len(error_log) > 3:
                self.logger.debug(f"  ... and {len(error_log) - 3} more errors")
        else:
            self.logger.debug("No errors found in log.")

    def _log_connection_result(self, result: Dict[str, Any]) -> None:
        """
        Logs the connection result details to debug logs.

        Args:
            result: Dictionary containing connection result data.
        """
        if not result or not result.get("response", {}).get("data"):
            self.logger.debug("No connection result to log.")
            return

        self.logger.debug("Connection Result Details:")
        result_data = result["response"].get("data")

        if isinstance(result_data, dict):
            for key, value in result_data.items():
                self.logger.debug(f"  {key}: {value}")
        else:
            self.logger.debug(f"Connection result: {result_data}")

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