# API/diagnostics_api_handler.py
"""
Handler for system-level diagnostics and health checks.

This module provides specialized functionality for performing diagnostic
operations on the GRL application API using the unified enum-based approach.
"""
import logging
import time
import concurrent.futures
from typing import Dict, Any, List, Tuple

from .api_enum import ApiName
from .decorators import api_call

logger = logging.getLogger(__name__)


class DiagnosticsApiHandler:
    """
    Handler for system-level diagnostics and health checks.

    This handler provides methods for verifying API connectivity and gathering
    diagnostic information about the system state using the enum-based API approach.

    Attributes:
        api_handler: Reference to the main GRLApiHandler instance
        logger (Logger): Logger instance for recording operations
    """

    def __init__(self, api_handler, custom_logger=None):
        """
        Initialize the diagnostics handler.

        Args:
            api_handler: Instance of GRLApiHandler to use for API calls
            custom_logger: Optional logger instance
        """
        self.api_handler = api_handler
        self.logger = custom_logger or logger

    @api_call
    def check_api_health(self, use_parallel: bool = False) -> Dict[str, Any]:
        """
        Check the health of various API endpoints.

        This method tests connectivity to key API endpoints and reports
        their status and response times.

        Args:
            use_parallel: Whether to check endpoints in parallel for faster results

        Returns:
            Dictionary containing health check results with the following structure:
            {
                "timestamp": "YYYY-MM-DD HH:MM:SS",
                "endpoints": {
                    "endpoint1": {"status": "ok", "response_time": 0.123},
                    ...
                },
                "overall_status": "healthy|degraded|critical"
            }
        """
        self.logger.info("Running API health check")

        health_results = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "endpoints": {},
            "overall_status": "unknown"
        }

        # Define key endpoints to check
        endpoints_to_check = [
            ApiName.GET_SOFTWARE_VERSION,
            ApiName.GET_APP_STATE,
            ApiName.GET_TEST_CASE_LIST,
            ApiName.GET_LATEST_FIRMWARE_VERSION
        ]

        def check_endpoint(api_name: ApiName) -> bool:
            """
            Check a single endpoint and record its status.

            Args:
                api_name: API endpoint to check

            Returns:
                Boolean indicating if the endpoint is healthy
            """
            endpoint_key = api_name.value
            start_time = time.time()

            try:
                response = self.api_handler.call_api(api_name)
                duration = round(time.time() - start_time, 3)

                success = response["response"].get("success", False)
                health_results["endpoints"][endpoint_key] = {
                    "status": "ok" if success else "error",
                    "response_time": duration,
                    "status_code": response["response"].get("status_code", 0)
                }

                if not success:
                    health_results["endpoints"][endpoint_key]["error"] = response["response"].get("error",
                                                                                                  "Unknown error")

                return success

            except Exception as e:
                duration = round(time.time() - start_time, 3)
                health_results["endpoints"][endpoint_key] = {
                    "status": "error",
                    "response_time": duration,
                    "error": str(e)
                }
                return False

        # Execute checks - either in parallel or sequentially
        if use_parallel:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(check_endpoint, endpoints_to_check))
                success_count = sum(results)
        else:
            success_count = sum(check_endpoint(endpoint) for endpoint in endpoints_to_check)

        # Determine overall health status
        total_endpoints = len(endpoints_to_check)
        if success_count == total_endpoints:
            health_results["overall_status"] = "healthy"
        elif success_count > total_endpoints // 2:
            health_results["overall_status"] = "degraded"
        else:
            health_results["overall_status"] = "critical"

        self.logger.info(
            f"API Health Status: {health_results['overall_status']} ({success_count}/{total_endpoints} endpoints healthy)")
        return health_results

    @api_call
    def log_api_diagnostics(self) -> Dict[str, Any]:
        """
        Run comprehensive API diagnostics and log the results.

        This method gathers information about:
        - Software, firmware, and eload versions
        - Application state
        - Available test cases count
        - System connectivity

        Returns:
            Dictionary containing all diagnostic information
        """
        self.logger.info("=== GRL API Diagnostics ===")

        diagnostics = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "versions": {},
            "system_info": {},
            "errors": []
        }

        # Get version information using the convenience method
        try:
            diagnostics["versions"] = self.api_handler.get_versions()
            self.logger.info(f"Versions: {diagnostics['versions']}")
        except Exception as e:
            error_msg = f"Failed to get version info: {str(e)}"
            diagnostics["errors"].append(error_msg)
            self.logger.error(error_msg)

        # Get application state
        try:
            app_state_response = self.api_handler.call_api(ApiName.GET_APP_STATE)
            if app_state_response["response"].get("success"):
                app_state = app_state_response["response"].get("data", "Unknown")
                diagnostics["system_info"]["app_state"] = app_state
                self.logger.info(f"Application State: {app_state}")
            else:
                error_msg = f"Failed to get app state: {app_state_response['response'].get('error')}"
                diagnostics["errors"].append(error_msg)
                self.logger.warning(error_msg)
        except Exception as e:
            error_msg = f"Exception getting app state: {str(e)}"
            diagnostics["errors"].append(error_msg)
            self.logger.error(error_msg)

        # Get test case count
        try:
            tc_response = self.api_handler.call_api(ApiName.GET_TEST_CASE_LIST)
            if tc_response["response"].get("success"):
                test_cases = tc_response["response"].get("data", [])
                test_case_count = len(test_cases) if isinstance(test_cases, list) else "Unknown format"
                diagnostics["system_info"]["test_case_count"] = test_case_count
                self.logger.info(f"Test Case Count: {test_case_count}")
            else:
                error_msg = f"Failed to get test cases: {tc_response['response'].get('error')}"
                diagnostics["errors"].append(error_msg)
                self.logger.warning(error_msg)
        except Exception as e:
            error_msg = f"Exception getting test cases: {str(e)}"
            diagnostics["errors"].append(error_msg)
            self.logger.error(error_msg)

        # Log summary
        self.logger.info(f"Diagnostics completed. {len(diagnostics['errors'])} errors encountered.")
        if diagnostics["errors"]:
            self.logger.warning(f"Diagnostic errors: {'; '.join(diagnostics['errors'])}")

        return diagnostics

    def run_connection_diagnostics(self, ip_addresses: List[str]) -> Dict[str, Any]:
        """
        Test connectivity to multiple IP addresses.

        Args:
            ip_addresses: List of IP addresses to test

        Returns:
            Dictionary with connection test results
        """
        self.logger.info(f"Testing connectivity to {len(ip_addresses)} IP addresses")

        results = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "connections": {},
            "summary": {"successful": 0, "failed": 0}
        }

        for ip in ip_addresses:
            try:
                start_time = time.time()
                success = self.api_handler.check_connection_status(ip)
                duration = round(time.time() - start_time, 3)

                results["connections"][ip] = {
                    "status": "connected" if success else "failed",
                    "response_time": duration
                }

                if success:
                    results["summary"]["successful"] += 1
                    self.logger.info(f"Connection to {ip}: SUCCESS ({duration}s)")
                else:
                    results["summary"]["failed"] += 1
                    self.logger.warning(f"Connection to {ip}: FAILED ({duration}s)")

            except Exception as e:
                results["connections"][ip] = {
                    "status": "error",
                    "error": str(e)
                }
                results["summary"]["failed"] += 1
                self.logger.error(f"Connection to {ip}: ERROR - {str(e)}")

        return results