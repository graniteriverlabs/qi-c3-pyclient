# client/modules/test_manager.py
"""
Updated test execution management module.
Uses enum-based API calls for all operations.
"""

import time
import threading
from typing import List, Dict, Any, Optional

from API import ApiName
from ..transport.app_state import read_app_state, is_app_ready, is_app_busy
from .data_stall_watch import DataStallWatch


def _normalise_case(name: Any) -> str:
    """
    A case name reduced to its letters and digits, for comparing the two spellings in use.

    The selected list and the application's progress reports do not agree on spelling: the
    selection carries ``8.2.16 PTX.CPX.CFG.S05.BPX.013`` while the application reports
    ``PTX_CPX_CFG_S05_BPX_013``. Comparing them as written would mark every case missing, so
    both sides are reduced before being compared at all.
    """
    return "".join(ch for ch in str(name or "").upper() if ch.isalnum())


def _cases_not_finished(submitted: List[str], finished: Any) -> List[str]:
    """
    Which submitted cases have no finished counterpart, by the names as submitted.

    A case counts as finished when either spelling contains the other once reduced - the
    application's name has no section number, so it is a suffix of the selected one. Matching
    loosely in this direction is deliberate: a false "incomplete" would cry wolf on good runs
    and get the whole check ignored.
    """
    done = [_normalise_case(f) for f in (finished or ()) if _normalise_case(f)]
    missing = []
    for case in submitted:
        key = _normalise_case(case)
        if not key:
            continue
        if not any(key in d or d in key for d in done):
            missing.append(case)
    return missing


class TestManager:
    """
    Manages test execution using enum-based API calls.
    """

    def __init__(self, logger, system_state):
        """
        Initialize the TestManager.

        Args:
            logger: Logger instance for debug and error logging
            system_state: SystemState instance for tracking test state
        """
        self.logger = logger
        self.system_state = system_state

        # State variables
        self.api_handler = None
        self.stop_requested = False
        self.live_transport = None
        self.capture_manager = None

    def set_api_handler(self, api_handler):
        """Set the API handler instance."""
        self.api_handler = api_handler

    def set_live_transport(self, live_transport):
        """
        Set the live-data transport (REST polling or WebSocket).

        Test progress and pop-ups are read through it, so this manager behaves the same
        whichever build of the app we are attached to.
        """
        self.live_transport = live_transport

    def set_capture_manager(self, capture_manager):
        """
        Set the per-case data-capture manager. When present, each test case's measurement
        data (report ZIP + packet log + WebSocket streams) is captured at its completion and
        the whole run is packaged into one ZIP — the same on both builds. Optional: if None,
        the run behaves exactly as before (no capture).
        """
        self.capture_manager = capture_manager

    # Test-status tokens that mean a case has finished (used to time per-case capture).
    _TERMINAL_STATUS = ("complete", "finish", "pass", "fail", "abort", "skip", "done")

    def _is_terminal_status(self, status) -> bool:
        s = str(status or "").lower()
        return any(tok in s for tok in self._TERMINAL_STATUS)

    def _capture_case(self, case_name, captured: set) -> None:
        """Capture one finished case's data (never raises; capture must not abort a run)."""
        captured.add(case_name)
        telemetry = None
        if self.live_transport:
            try:
                telemetry = self.live_transport.get_telemetry()
            except Exception:
                telemetry = None
        try:
            self.capture_manager.capture_case(case_name, telemetry)
        except Exception as e:
            self.logger.warning(f"Data capture for '{case_name}' failed (run continues): {e}")

    def _capture_on_boundary(self, current_case, captured: set):
        """
        Detect a per-case boundary from the live test status and capture the case that just
        finished. Works the same on both builds — WebSocket pushes and REST polling both land
        the current case in system_state.test_case_name/test_status. Returns the case name now
        running.
        """
        name = getattr(self.system_state, "test_case_name", None)
        status = getattr(self.system_state, "test_status", None)
        if not name:
            return current_case
        if name != current_case:
            # A new case is running -> the previous one has completed; capture it.
            if current_case and current_case not in captured:
                self._capture_case(current_case, captured)
            return name
        # Same case still current: capture as soon as it reports a terminal status.
        if status and self._is_terminal_status(status) and name not in captured:
            self._capture_case(name, captured)
        return current_case

    def submit_test_list(self, test_list: List[str], popup_manager=None) -> Dict[str, Any]:
        """
        Submit a list of test cases to be executed using enum-based API.

        Args:
            test_list: List of test case names to execute
            popup_manager: PopupManager instance for handling popups during test

        Returns:
            Dict: Success/failure information and any error messages
        """
        self.logger.info("Submitting test list for execution...")
        self._check_api_handler()

        if not test_list:
            return {"success": False, "error": "Empty test list provided"}

        # Start receiving live data for the duration of the run: pop-ups get answered,
        # and on a WebSocket build the app also streams progress and measurements.
        self.stop_requested = False
        popup_thread = None
        if self.live_transport:
            self.live_transport.start()
        elif popup_manager:
            popup_manager.start_popup_thread()
            popup_thread = threading.Thread(target=popup_manager._handle_popups, daemon=True)
            popup_thread.start()

        try:
            # Submit the test list using enum API
            response = self.api_handler.call_api(ApiName.POST_TEST_LIST_TO_EXECUTE, data=test_list)

            if not response["response"].get("success"):
                error = response["response"].get("error", "Unknown error")
                self.logger.error(f"Failed to submit test list: {error}")
                return {"success": False, "error": "Failed to submit test list"}

            self.logger.info("Test list submitted successfully.")

            # Begin capturing this run's per-case data (no-op if no capture manager wired).
            if self.capture_manager:
                transport_name = self.live_transport.name if self.live_transport else "REST"
                self.capture_manager.start_run(transport_name)

            # Monitor the test status and allow the popup handler to work
            start_time = time.time()
            timeout = 30  # seconds to wait for test to start

            # Wait for test to start
            test_started = False
            while time.time() - start_time < timeout and not self.stop_requested:
                if self._is_test_running():
                    test_started = True
                    break
                time.sleep(1)

            if not test_started and not self.stop_requested:
                self.logger.warning(f"Test did not start within {timeout} seconds")
                if self.capture_manager:
                    self.capture_manager.finalize_run()
                return {"success": True, "warning": "Test submission successful but test did not start within timeout"}

            # Monitor until test completes, capturing each case's data at its completion.
            # While a case runs, snapshot its packet buffer (the app clears GetCCLinePackets
            # when the next case starts, so a boundary-only GET comes back empty).
            captured: set = set()
            current_case = None
            last_pkt_poll = 0.0
            last_heartbeat = time.time()
            PKT_POLL_S = 2.0
            HEARTBEAT_S = 15.0
            # Self-calibrating data-cessation watchdog (WP 1.2). Liveness is judged by DATA, not
            # elapsed time and NOT by any configured duration: the watchdog learns this run's own
            # data cadence and only flags a stall when measurement data stops relative to that,
            # while the app is BUSY and no operator pop-up is pending. A data-producing case is
            # never aborted, however long it runs.
            stall = DataStallWatch()
            while self._is_test_running() and not self.stop_requested:
                now = time.time()
                if self.capture_manager:
                    current_case = self._capture_on_boundary(current_case, captured)
                    if current_case and current_case not in captured and (now - last_pkt_poll) >= PKT_POLL_S:
                        self.capture_manager.poll_packets(current_case)
                        last_pkt_poll = now

                # Feed the watchdog: current data-progress + whether the app is BUSY + whether an
                # operator pop-up is pending (a legitimate no-data period that pauses the watchdog).
                popup_pending = self._popup_pending()
                app_busy = is_app_busy(getattr(self.system_state, "app_state", ""))
                data_value = self._data_progress_value()
                # A case that has reported `Ended` is not running: the quiet that follows is
                # the app writing its report, and aborting there throws that report away.
                case_ended = str(getattr(self.system_state, "test_status", "") or "").lower() == "ended"
                if stall.update(now, data_value, app_busy, popup_pending, case_ended):
                    # SUSPECTED cessation — confirm with one active probe before acting.
                    if self._confirm_data_ceased(current_case, data_value):
                        return self._abort_run(
                            captured, current_case,
                            reason=f"tester stopped producing data on {current_case or '(unknown case)'}",
                            detail=(f"no measurement data for {stall.flat_seconds(time.time()):.0f}s "
                                    f"(> {stall.CADENCE_MULTIPLE}x this run's observed "
                                    f"{stall.observed_cadence:.0f}s data cadence) while app=BUSY"),
                        )

                # Heartbeat so a legitimate wait is visibly alive instead of looking frozen; it
                # reports the data-progress signal, so a stall shows as data_frames flat-lining
                # with since_last_data climbing. Escalates to WARNING once data has been flat well
                # beyond the run's own cadence (observability only — the abort decision is above).
                if now - last_heartbeat >= HEARTBEAT_S:
                    app_state = getattr(self.system_state, "app_state", "?")
                    case = getattr(self.system_state, "test_case_name", None) or current_case or "(starting)"
                    status = getattr(self.system_state, "test_status", None) or "-"
                    flat = stall.flat_seconds(now)
                    msg = (f"still running — app_state={app_state}, case={case}, status={status}, "
                           f"data_frames={data_value}, since_last_data={flat:.0f}s")
                    if stall.armed and app_busy and not popup_pending and flat > stall.observed_cadence * 2:
                        self.logger.warning(msg + " [no new measurement data — watching for cessation]")
                    else:
                        self.logger.info(msg)
                    last_heartbeat = now
                time.sleep(0.5)  # Check periodically

            if self.stop_requested:
                self.logger.info("Stop requested by user. Cancelling test submission.")
                self._cancel_running_test()
                if self.capture_manager:
                    self.capture_manager.finalize_run()
                return {"success": False, "error": "Test cancelled by user"}

            # Capture the final case (the one running when the app returned to idle).
            if self.capture_manager:
                if current_case and current_case not in captured:
                    self._capture_case(current_case, captured)
                run_dir = self.capture_manager.finalize_run()
                if run_dir:
                    self.logger.info(f"Run evidence collected: {run_dir}")

            # A run that stopped early must not be reported as a run that finished. The
            # application's stop signal only says it stopped - seen once in eight runs on
            # MPP-TPR, where it stopped after starting the final case, left no final report and
            # 7 of 8 case folders, and the client logged "Test Execution completed
            # successfully". Anything built on that message takes an incomplete run for a good
            # one, and nobody looks at it again.
            outcome: Dict[str, Any] = {"success": True, "submitted": len(test_list)}
            if self.capture_manager:
                missing = _cases_not_finished(test_list, captured)
                outcome["finished"] = len(test_list) - len(missing)
                outcome["incomplete"] = missing
                if missing:
                    self.logger.error(
                        "The application stopped, but {0} of {1} selected case(s) never "
                        "reached a verdict:\n  {2}\n"
                        "  The run is INCOMPLETE. Re-run it, and check the application's own "
                        "report for how far it got.".format(
                            len(missing), len(test_list), "\n  ".join(missing))
                    )
                    return outcome
            else:
                # Without capture there is no per-case record to check against, so say that
                # rather than implying the run was verified.
                self.logger.info("Per-case completion was not verified: capture is not enabled.")

            self.logger.info("Test run complete.")
            return outcome

        except Exception as e:
            error_msg = f"Failed to submit test list: {str(e)}"
            self.logger.error(error_msg)
            return {"success": False, "error": error_msg}
        finally:
            # Stop receiving live data for this run.
            if self.live_transport:
                try:
                    self.live_transport.stop()
                    self.logger.info(
                        f"Live data ({self.live_transport.name}) stopped after submit_test_list"
                    )
                except Exception as e:
                    self.logger.warning(f"Error stopping live transport: {str(e)}")
            elif popup_manager and popup_thread:
                popup_manager.stop_popup_thread()
                try:
                    popup_thread.join(timeout=2)
                    self.logger.info("Popup handler thread safely stopped after submit_test_list")
                except Exception as e:
                    self.logger.warning(f"Error stopping popup thread: {str(e)}")

    def _data_progress_value(self) -> int:
        """
        Combined measurement-data-progress counter (WP 1.0): WS stream frames + new Qi
        packets. It only rises while the tester is delivering measurement data, so the
        monitor loop treats a rise as liveness and a flat-line as a potential stall. Never
        raises — a missing transport/capture manager simply contributes 0.
        """
        total = 0
        if self.live_transport:
            try:
                total += int(self.live_transport.data_frame_count())
            except Exception:
                pass
        if self.capture_manager:
            try:
                total += int(self.capture_manager.packet_progress())
            except Exception:
                pass
        return total

    def _popup_pending(self) -> bool:
        """Whether an operator pop-up is currently up (WP 1.2 watchdog pause), on either
        transport. Never raises — a missing transport simply means 'no pop-up'."""
        if not self.live_transport:
            return False
        try:
            return bool(self.live_transport.popup_pending())
        except Exception:
            return False

    def _confirm_data_ceased(self, current_case, before_value: int) -> bool:
        """
        Actively confirm the tester really produced no new measurement data before aborting
        (WP 1.2). One bounded fresh probe: poll the packet buffer once more; if the combined
        data-progress value still has not advanced past what we saw when cessation was
        suspected, the tester has genuinely stopped delivering. A probe that fails because the
        app process is gone also yields no advance → correctly treated as ceased. Never raises.
        """
        if self.capture_manager and current_case:
            try:
                self.capture_manager.poll_packets(current_case)
            except Exception:
                pass
        try:
            return self._data_progress_value() <= before_value
        except Exception:
            return True

    def _abort_run(self, captured: set, current_case, reason: str, detail: str) -> Dict[str, Any]:
        """
        Clean abort of a stalled run (WP 1.3): log loudly, best-effort force-stop, write a
        PARTIAL capture ZIP (so evidence up to the stall is preserved — the incident saved
        none), and return a clear failure. The transport is stopped by the caller's `finally`.
        """
        self.logger.error(f"STALLED — {detail}; aborting run")
        self._cancel_running_test()   # PostForceStop, best-effort + connect-bounded (WP 1.6)
        if self.capture_manager:
            try:
                self.capture_manager.finalize_run()
            except Exception as e:
                self.logger.warning(f"Partial capture during abort failed (continuing teardown): {e}")
        return {"success": False, "error": reason}

    def _is_test_running(self) -> bool:
        """
        Check whether a test is currently running.

        On a WebSocket build this reads state the app pushed to us; on a REST build it
        polls. Either way the answer means the same thing, so the caller is unchanged.

        Returns:
            bool: True if test is still running, False if it has ended.
        """
        if self.live_transport:
            return self.live_transport.is_test_running()

        # No transport wired up (e.g. this manager used standalone) — poll directly.
        try:
            test_status_response = self.api_handler.call_api(ApiName.GET_TEST_STATUS)
            app_state_response = self.api_handler.call_api(ApiName.GET_APP_STATE)

            test_status = test_status_response.get("response", {}).get("data", {}) or {}
            app_state = app_state_response.get("response", {}).get("data", {}) or {}

            self._update_system_state(app_state_dict=app_state, test_status_dict=test_status)
            current_test_status = test_status.get("Test Status", "")

            # Accept both the old (appState) and new (ApplicationState) field names.
            app_state_value, _ = read_app_state(app_state)

            self.logger.debug(f"App state: {app_state_value}")
            self.logger.debug(f"Test status: {current_test_status}")

            if is_app_ready(app_state_value):
                self.logger.info("Test has ended. App state is READY.")
                return False

            if "Started" in str(current_test_status) or is_app_busy(app_state_value):
                self.logger.info("Test is currently running.")
                return True

            self.logger.info("Test is not running based on current status.")
            return False

        except Exception as e:
            self.logger.error(f"Failed to check test status: {str(e)}")
            return False

    def _update_system_state(self, app_state_dict: Dict[str, Any], test_status_dict: Dict[str, Any]) -> None:
        """
        Update system_state based on application state and test status dictionaries.

        Args:
            app_state_dict: Dictionary containing appState and connectionState
            test_status_dict: Dictionary containing test status information
        """
        try:
            # Update app state and connection state.
            # Newer app builds renamed these fields (ApplicationState / EthernetConnectionState),
            # so read_app_state accepts either naming.
            app_state_value, connection_state_value = read_app_state(app_state_dict)
            if hasattr(self.system_state, 'app_state'):
                self.system_state.app_state = app_state_value
            if hasattr(self.system_state, 'connection_state'):
                self.system_state.connection_state = connection_state_value

            # Extract test information if available
            test_info_string = test_status_dict.get('Test Status', '')
            if test_info_string and test_info_string.startswith('Test:'):
                self.logger.info(f"Parsing test info from: {test_info_string}")

                try:
                    # Parse test info - Split the string by colon
                    parts = test_info_string.split(':')

                    # Check if we have at least 3 parts (Test, test_case_name, status)
                    if len(parts) >= 3:
                        # The second item (index 1) is the test case name (after 'Test')
                        test_case_name = parts[1].strip()

                        # The last item is the status
                        status = parts[-1].strip()

                        # Update the system state with parsed values
                        if hasattr(self.system_state, 'test_case_name'):
                            self.system_state.test_case_name = test_case_name
                        if hasattr(self.system_state, 'test_status'):
                            self.system_state.test_status = status

                        self.logger.info(
                            f"Test case updated: {getattr(self.system_state, 'test_case_name', 'N/A')} - "
                            f"Status: {getattr(self.system_state, 'test_status', 'N/A')}"
                        )
                    else:
                        self.logger.warning(f"Test info string doesn't have enough parts: {test_info_string}")
                except Exception as e:
                    self.logger.error(f"Error parsing test info: {str(e)}")
            else:
                self.logger.debug("No test status information available")

        except Exception as e:
            self.logger.error(f"Error updating system state: {str(e)}")
            # Set system state to error values in case of failure
            if hasattr(self.system_state, 'app_state'):
                self.system_state.app_state = 'ERROR'
            if hasattr(self.system_state, 'connection_state'):
                self.system_state.connection_state = 'ERROR'
            if hasattr(self.system_state, 'test_case_name'):
                self.system_state.test_case_name = None
            if hasattr(self.system_state, 'test_status'):
                self.system_state.test_status = None

    def _cancel_running_test(self) -> bool:
        """
        Cancel a running test using enum-based API.

        Returns:
            bool: True if cancel request was sent successfully, False otherwise
        """
        try:
            response = self.api_handler.call_api(ApiName.POST_FORCE_STOP)
            if response["response"].get("success"):
                self.logger.info("Test cancelled successfully")
                return True
            else:
                error = response["response"].get("error", "Unknown error")
                self.logger.error(f"Failed to cancel test: {error}")
                return False
        except Exception as e:
            self.logger.error(f"Error cancelling test: {str(e)}")
            return False

    def stop_test_execution(self) -> bool:
        """
        Request stopping the test run.

        Returns:
            bool: True if stop request was initiated, False if there's no active test
        """
        if not self._is_test_running():
            self.logger.warning("Cannot stop test - no test is currently running")
            return False

        self.stop_requested = True
        self.logger.info("Stop request received. Test run will be cancelled.")
        return self._cancel_running_test()

    def stop_test_run(self):
        """Allow the user to request stopping the test run."""
        self.stop_requested = True
        self.logger.info("Stop request received. Test run will be canceled.")

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
