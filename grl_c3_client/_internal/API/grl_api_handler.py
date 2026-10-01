# API/grl_api_handler.py
"""
Streamlined GRL API handler using enum-based configuration.

This module provides the main GRLApiHandler class which uses the ApiName enum
and grl_api_config.json to make API calls. All specific API methods have been
removed in favor of the unified call_api() method.
"""
import json
import logging
import os
from typing import Dict, Any, Optional, Union

import requests

from .api_enum import ApiName
from .decorators import api_call, performance_monitor

# Logger configuration
logger = logging.getLogger(__name__)

#: Connect-timeout only (WP 1.6). This bounds ONLY the TCP connection setup — "will the app
#: process accept a socket at all?" — which on localhost is sub-millisecond when healthy and
#: does NOT depend on what the call does. It exists so that if the app process dies, a liveness
#: call returns in a few seconds instead of hanging forever. It is deliberately NOT a read
#: timeout: the read side is left uncapped (None) so a legitimately slow-but-healthy call
#: (e.g. report generation) is never cut off. This is an internal network constant, NOT a
#: user config — the user can never know how long an operation takes, so they are never asked.
#: 3.05 s (rather than 3.0) sits just past the 3 s TCP SYN-retransmit boundary, a common
#: requests recommendation so a single dropped SYN doesn't trip the timeout on a live host.
CONNECT_TIMEOUT_S = 3.05
#: (connect, read) tuple passed to requests: bounded connect, uncapped read.
_REQUEST_TIMEOUT = (CONNECT_TIMEOUT_S, None)


class GRLApiHandler:
    """
    Unified GRL API handler using enum-based configuration.

    This handler uses the ApiName enum and grl_api_config.json to make all API calls
    through a single call_api() method, eliminating the need for individual methods
    for each API endpoint.

    Attributes:
        base_url (str): Base URL for the API endpoints
        logger (Logger): Logger instance for recording operations
        session (Session): Requests session for making HTTP requests
        api_definitions (dict): Loaded API configuration from JSON file
    """

    def __init__(self, base_url: str, custom_logger: Optional[logging.Logger] = None):
        """
        Initialize the GRL API handler.

        Args:
            base_url: Base URL for the API (e.g., "http://localhost:8080/api")
            custom_logger: Optional logger instance. If None, a default logger is used.
        """
        self.base_url = base_url.rstrip("/")
        self.logger = custom_logger or logger
        self.session = requests.Session()
        self.api_definitions = self._load_api_config()

        if not self.api_definitions:
            self.logger.warning("API definitions could not be loaded. Some functionality may be limited.")

        self.logger.info(f"Initialized GRLApiHandler with base URL: {self.base_url}")

    def _load_api_config(self) -> dict:
        """
        Load API configuration from the JSON file.

        Returns:
            Dictionary containing API definitions, or empty dict if loading fails
        """
        config_path = os.path.join(os.path.dirname(__file__), "grl_api_config.json")
        try:
            self.logger.debug(f"Loading API config from {config_path}")
            with open(config_path, "r") as f:
                config = json.load(f)
            self.logger.info(f"Loaded {len(config)} API definitions")
            return config
        except FileNotFoundError:
            self.logger.error(f"API config file not found: {config_path}")
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in API config file: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error loading API config: {e}")
        return {}

    def has_api_definitions(self) -> bool:
        """
        Check if API definitions are loaded and non-empty.

        Returns:
            True if config is loaded, else False
        """
        return bool(self.api_definitions)

    @api_call
    @performance_monitor
    def call_api(self,
                 api_name: ApiName,
                 data: Optional[Dict[str, Any]] = None,
                 params: Optional[Dict[str, Any]] = None,
                 endpoint_params: Optional[Dict[str, str]] = None,
                 endpoint_override: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        Call an API defined in the JSON config using the enum name.

        Args:
            api_name: Enum value from ApiName specifying which API to call
            data: Optional request payload. A dict is serialised as JSON; a str/bytes is
                sent verbatim (PostVerifyESDFJsonSign takes the raw ESDF file text).
            params: Optional query parameters
            endpoint_params: Optional parameters to substitute in endpoint templates
            endpoint_override: Optional override for the entire endpoint path
            headers: Optional HTTP headers, replacing the JSON defaults

        Returns:
            Dictionary containing both request and response information

        Example:
            # Using endpoint_params (recommended)
            response = handler.call_api(
                ApiName.PUT_CERTIFICATION_FILTER_TOGGLE,
                endpoint_params={"state": "true"}
            )

            # Using endpoint_override (for dynamic endpoints)
            response = handler.call_api(
                ApiName.PUT_CERTIFICATION_FILTER_TOGGLE,
                endpoint_override="PutCertificationFilterToggle/true"
            )
        """
        # Get API configuration
        config = self.api_definitions.get(api_name.value)
        if not config:
            error_msg = f"No configuration found for API: {api_name.value}"
            self.logger.error(error_msg)
            return {
                "request": {"api_name": api_name.value},
                "response": {
                    "success": False,
                    "error": error_msg
                }
            }

        # Extract configuration
        method = config["method"]
        service = config["service"]

        # Use endpoint override if provided, otherwise use configured endpoint
        if endpoint_override:
            endpoint = endpoint_override
            self.logger.debug(f"Using endpoint override: {endpoint}")
        else:
            endpoint = config["endpoint"]

            # Handle endpoint parameter substitution for non-override cases
            if endpoint_params:
                try:
                    endpoint = endpoint.format(**endpoint_params)
                except KeyError as e:
                    error_msg = f"Missing endpoint parameter: {e}"
                    self.logger.error(error_msg)
                    return {
                        "request": {"api_name": api_name.value, "endpoint_params": endpoint_params},
                        "response": {
                            "success": False,
                            "error": error_msg
                        }
                    }

        # Make the API call
        self.logger.info(f"Calling API: {api_name.value} ({method} {service}/{endpoint})")
        return self.send_request(method, service, endpoint, params=params, data=data,
                                 headers=headers)
    @api_call
    def send_request(self,
                    method: str,
                    service: str,
                    endpoint: str = "",
                    params: Optional[Dict] = None,
                    data: Optional[Dict] = None,
                    headers: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Send an HTTP request to the API.

        This is the core method that handles all API communication. It constructs
        the request, sends it, and processes the response.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            service: API service name (e.g., "App", "ConnectionSetup")
            endpoint: API endpoint path (e.g., "GetSoftwareVersion")
            params: Optional query parameters
            data: Optional request body data
            headers: Optional HTTP headers

        Returns:
            Dictionary containing both request and response information
        """
        # FIX: Clean base_url to avoid path issues
        base_url_clean = self.base_url.rstrip('/')
        # Construct URL
        url = f"{base_url_clean}/{service}"
        if endpoint:
            url += f"/{endpoint}"

        # Set default headers
        headers = headers or {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        # One concise request line. Payload size (not the payload) is enough for tracing;
        # the full body is only rendered at TRACE-style depth if explicitly needed.
        body_note = ""
        if data is not None:
            try:
                body_note = f" body={len(str(data))}B"
            except Exception:
                body_note = " body=?"
        self.logger.debug(f"{method.upper()} {service}/{endpoint or ''}{body_note}")

        # Prepare result structure
        result = {
            "request": {
                "method": method.upper(),
                "url": url,
                "params": params,
                "data": data,
                "headers": headers
            },
            "response": {}
        }

        try:
            # Make the HTTP request
            response = self._dispatch_request(method.upper(), url, params, data, headers)

            # Process response
            result["response"].update({
                "status_code": response.status_code,
                "success": 200 <= response.status_code < 300,
                "headers": dict(response.headers),
                "content_type": response.headers.get("Content-Type", "")
            })

            # Parse response content
            try:
                result["response"]["data"] = response.json()
                result["response"]["content_type"] = "json"
            except ValueError:
                result["response"]["data"] = response.text
                result["response"]["content_type"] = "text"

            self.logger.debug(f"Response Status: {response.status_code}")
            if not result["response"]["success"]:
                self.logger.warning(f"API call failed with status {response.status_code}")

            return result

        except requests.exceptions.ConnectionError as e:
            error_msg = f"Connection error: {str(e)}"
        except requests.exceptions.Timeout as e:
            error_msg = f"Request timed out: {str(e)}"
        except requests.exceptions.RequestException as e:
            error_msg = f"Request failed: {str(e)}"
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"

        # Handle errors
        self.logger.error(error_msg)
        result["response"].update({
            "success": False,
            "error": error_msg
        })
        return result

    def _dispatch_request(self, method: str, url: str,
                        params: Optional[Dict], data: Optional[Dict],
                        headers: Dict) -> requests.Response:
        """
        Dispatch the HTTP request using the appropriate method.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            url: Complete URL for the request
            params: Query parameters
            data: Request body data
            headers: HTTP headers

        Returns:
            Response object from the requests library

        Raises:
            ValueError: If an unsupported HTTP method is provided
        """
        # A str/bytes body is sent verbatim, a dict is serialised as JSON. The app's
        # PostVerifyESDFJsonSign reads Request.Body as text and parses it itself, so
        # re-encoding the ESDF file through json= would hand it a quoted string and
        # fail signature verification (the signature covers the exact file bytes).
        raw_body = isinstance(data, (str, bytes, bytearray))
        body = {"data": data} if raw_body else {"json": data}

        # Connect-timeout only (WP 1.6): bounded connect so a DEAD app process is noticed fast;
        # read left uncapped so a slow-but-healthy call (report generation, etc.) is never cut off.
        dispatch_map = {
            "GET": lambda: self.session.get(url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT),
            "POST": lambda: self.session.post(url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT, **body),
            "PUT": lambda: self.session.put(url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT, **body),
            "DELETE": lambda: self.session.delete(url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT)
        }

        if method not in dispatch_map:
            raise ValueError(f"Unsupported HTTP method: {method}")

        return dispatch_map[method]()

    def close(self) -> None:
        """
        Close the HTTP session and free resources.

        This should be called when the handler is no longer needed to ensure
        proper cleanup of resources.
        """
        if self.session:
            self.session.close()
            self.logger.info("HTTP session closed")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with automatic cleanup."""
        self.close()

    # Convenience methods for common API patterns
    def get_versions(self) -> Dict[str, str]:
        """
        Get all version information in a single call.

        Returns:
            Dictionary with version information for software, firmware, eload, and short fixture
        """
        versions = {}

        version_apis = [
            (ApiName.GET_SOFTWARE_VERSION, "software"),
            (ApiName.GET_LATEST_FIRMWARE_VERSION, "firmware"),
            (ApiName.GET_LATEST_ELOAD_VERSION, "eload"),
            (ApiName.GET_LATEST_SHORT_FIXTURE_VERSION, "short_fixture")
        ]

        for api_name, key in version_apis:
            try:
                response = self.call_api(api_name)
                if response["response"].get("success"):
                    data = response["response"].get("data", "")
                    if isinstance(data, dict):
                        versions[key] = data.get("text_response", str(data))
                    else:
                        versions[key] = str(data)
                else:
                    versions[key] = f"Error: {response['response'].get('error', 'Unknown error')}"
            except Exception as e:
                versions[key] = f"Exception: {str(e)}"

        return versions

    def check_connection_status(self, ip_address: str) -> bool:
        """
        Check if connection to test equipment is successful.

        Args:
            ip_address: IP address to connect to

        Returns:
            True if connection is successful, False otherwise
        """
        try:
            response = self.call_api(
                ApiName.CONNECT_TO_TEST_EQUIPMENT,
                endpoint_params={"ip_address": ip_address}
            )
            return response["response"].get("success", False)
        except Exception as e:
            self.logger.error(f"Connection check failed: {e}")
            return False
