# client/transport/rest_transport.py
"""
Live data over REST — the polling transport.

This is the behaviour the client has always had, now behind the LiveTransport interface:
a background thread asks the app "is there a pop-up?" every poll interval and answers any
it finds, while test progress is discovered by asking "are you still busy?" on demand.

Used when the app build does not serve a WebSocket.
"""
import threading
from typing import Optional

from API import ApiName
from .app_state import read_app_state, is_app_ready, is_app_busy
from .base import LiveTransport

DEFAULT_POLL_MS = 500


class RestLiveTransport(LiveTransport):
    """Polls the app for pop-ups and test status."""

    name = "REST"

    def __init__(self, api_handler, logger, system_state, popup_manager,
                 poll_ms: int = DEFAULT_POLL_MS):
        self.api_handler = api_handler
        self.logger = logger
        self.system_state = system_state
        self.popup_manager = popup_manager
        self.poll_interval = max(poll_ms, 50) / 1000.0

        self._popup_thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._popup_thread and self._popup_thread.is_alive():
            return
        if not self.popup_manager:
            self.logger.debug("No popup manager available; pop-ups will not be answered")
            return

        self.popup_manager.start_popup_thread()
        self._popup_thread = threading.Thread(
            target=self.popup_manager._handle_popups,
            name="RestPopupPoller",
            daemon=True,
        )
        self._popup_thread.start()
        self.logger.info(f"[{self.name}] live data started (polling every {self.poll_interval:.1f}s)")

    def stop(self) -> None:
        if not self._popup_thread:
            return
        if self.popup_manager:
            self.popup_manager.stop_popup_thread()
        self._popup_thread.join(timeout=2)
        self._popup_thread = None
        self.logger.info(f"[{self.name}] live data stopped")

    # -- test progress -----------------------------------------------------

    def is_test_running(self) -> bool:
        """Ask the app whether a test is still executing."""
        try:
            status_response = self.api_handler.call_api(ApiName.GET_TEST_STATUS)
            state_response = self.api_handler.call_api(ApiName.GET_APP_STATE)

            test_status = status_response.get("response", {}).get("data", {}) or {}
            app_state_data = state_response.get("response", {}).get("data", {}) or {}

            app_state, connection_state = read_app_state(app_state_data)
            current_status = test_status.get("Test Status", "") if isinstance(test_status, dict) else ""

            self._update_system_state(app_state, connection_state, current_status)

            self.logger.debug(f"[{self.name}] app={app_state} status={current_status}")

            if is_app_ready(app_state):
                self.logger.info("Test has ended. App state is READY.")
                return False
            if "Started" in str(current_status) or is_app_busy(app_state):
                return True
            return False

        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to check test status: {e}")
            return False

    def popup_pending(self) -> bool:
        """Whether a pop-up is currently up on this REST build (WP 1.2 watchdog pause).

        Mirrors the popup manager's live `popup_active` flag, updated every ~500 ms poll — so
        an operator wait (coil placement) pauses the data-stall watchdog on REST just as
        `_last_popup` does on WebSocket.
        """
        return bool(self.popup_manager and getattr(self.popup_manager, "popup_active", False))

    def _update_system_state(self, app_state: str, connection_state: str, test_info: str) -> None:
        """Mirror the app's state onto SystemState, parsing 'Test:<name>:...:<status>'."""
        try:
            self.system_state.app_state = app_state
            self.system_state.connection_state = connection_state

            if isinstance(test_info, str) and test_info.startswith("Test:"):
                parts = test_info.split(":")
                if len(parts) >= 3:
                    self.system_state.test_case_name = parts[1].strip()
                    self.system_state.test_status = parts[-1].strip()
        except Exception as e:
            self.logger.error(f"[{self.name}] Error updating system state: {e}")
