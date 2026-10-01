# client/transport/ws_transport.py
"""
Live data over WebSocket — the push transport.

One background thread owns the socket. The app pushes pop-ups, application state, test
status and measurement streams as they happen, so nothing is polled.

Test progress is driven primarily by the `TCExecutionServerState` frame (Start / InProgress
/ Stop). Because that frame is only sent on a change, a REST read of the app state is used
as a fallback until the first one arrives — that keeps a run correct even if the app pushes
nothing early on.
"""
import threading
import time
from typing import Any, Dict, Optional

from API import ApiName
from . import ui_popup
from . import ws_protocol as proto
from .app_state import read_app_state, is_app_ready, is_app_busy, UNKNOWN
from .base import LiveTransport

CONNECT_TIMEOUT_S = 10
RECV_TIMEOUT_S = 1.0
#: Don't re-answer the same pop-up id more often than this (the app re-notifies until
#: it registers the dismissal).
POPUP_REANSWER_S = 2.0
#: Keep retrying a given pop-up for at most this long from when it was first seen, then stop
#: clicking (avoids hammering the UI forever if a pop-up can't be dismissed). Generous, since
#: a coil pop-up may legitimately wait on the operator finishing the physical adjustment.
POPUP_ANSWER_WINDOW_S = 180.0


class WebSocketUnavailable(RuntimeError):
    """Raised when the WebSocket cannot be established, so the caller can fall back to REST."""


class WsLiveTransport(LiveTransport):
    """Receives live data pushed by the app over a WebSocket."""

    name = "WebSocket"

    def __init__(self, ws_url, api_handler, logger, system_state, popup_manager=None,
                 popup_coordinator=None):
        self.ws_url = ws_url
        self.api_handler = api_handler
        self.logger = logger
        self.system_state = system_state
        self.popup_manager = popup_manager
        # In hybrid mode, shared with the REST poller so a pop-up is answered once.
        self.popup_coordinator = popup_coordinator

        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._send_lock = threading.Lock()

        # State learned from pushed frames.
        self._tc_state: Optional[str] = None      # Start / InProgress / Stop
        self._app_state: str = UNKNOWN
        self._subscription = proto.READY

        self._telemetry: Dict[str, list] = {name: [] for name in proto.STREAM_TYPES}
        # Which stream types have actually arrived on THIS (external) socket — used to log the
        # first frame of each type and to report, at stop, whether the app pushed live streams
        # to us (native capture possible) or kept them on its own UI socket (REST fallback).
        self._stream_seen: set = set()
        self._last_popup: Optional[Dict[str, Any]] = None
        # Per-pop-up-id time of last answer attempt, to throttle re-clicks (the app re-notifies
        # until the answer registers), and when the pop-up was first seen, to stop retrying an
        # unanswerable pop-up after a generous window instead of clicking forever.
        self._answered: Dict[Any, float] = {}
        self._popup_first_seen: Dict[Any, float] = {}
        self._popup_counted: set = set()
        self.frames_received = 0
        self.popups_answered = 0
        # Monotonic count of measurement STREAM frames (Packets/Signals/PowerChart/
        # PowerTransferInfo) received. This is the WS half of the data-progress signal
        # (WP 1.0): while it climbs, the tester is delivering measurement data. Written only
        # by the single listener thread; an int read is atomic under the GIL, so the monitor
        # loop reads it via data_frame_count() without a lock.
        self._stream_frame_count = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._running.is_set():
            return
        self._connect()
        self._running.set()
        self._thread = threading.Thread(target=self._listen, name="WsLiveTransport", daemon=True)
        self._thread.start()
        self.logger.info(f"[{self.name}] live data started ({self.ws_url})")

    def _connect(self) -> None:
        try:
            from websocket import create_connection
        except ImportError as e:
            raise WebSocketUnavailable(
                "The 'websocket-client' package is required for WebSocket mode "
                "(pip install websocket-client)"
            ) from e

        try:
            self._ws = create_connection(self.ws_url, timeout=CONNECT_TIMEOUT_S)
            self._ws.settimeout(RECV_TIMEOUT_S)
        except Exception as e:
            raise WebSocketUnavailable(f"Could not connect to {self.ws_url}: {e}") from e

        self._subscribe(proto.READY)

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            if self._ws:
                self._ws.close()
        except Exception as e:
            self.logger.debug(f"[{self.name}] error closing socket: {e}")
        self._ws = None
        stream_counts = {name: len(items) for name, items in self._telemetry.items() if items}
        self.logger.info(
            f"[{self.name}] live data stopped "
            f"(frames={self.frames_received}, pop-ups answered={self.popups_answered}, "
            f"stream frames={stream_counts or 'none'})"
        )
        # Explicit verdict so the capture layer's native-vs-REST-fallback choice is observable.
        if stream_counts:
            self.logger.info(
                f"[{self.name}] measurement streams DID reach this client: "
                f"{sorted(stream_counts)} — native WS capture usable"
            )
        else:
            self.logger.info(
                f"[{self.name}] NO measurement streams reached this client — they are UI-socket "
                f"(DSUI) owned; live-data capture must use the REST endpoints (fallback)"
            )

    # -- sending -----------------------------------------------------------

    def _send(self, frame: str) -> None:
        with self._send_lock:
            if self._ws:
                self._ws.send(frame)

    def _subscribe(self, state: str) -> None:
        """Tell the app which state we're in; it only streams data while BUSY_ACTIVE."""
        for stream in proto.STREAM_TYPES:
            try:
                self._send(proto.build_subscribe(stream, state))
            except Exception as e:
                self.logger.warning(f"[{self.name}] failed to subscribe {stream}: {e}")
        self._subscription = state
        self.logger.debug(f"[{self.name}] subscribed streams as {state}")

    # -- receive loop ------------------------------------------------------

    def _listen(self) -> None:
        while self._running.is_set():
            # Keep answering a still-pending operator pop-up on EVERY iteration (throttled in
            # _answer_popup), not only on a recv timeout. A UI-automation click can miss, and
            # the app keeps the pop-up up until the answer registers — so we must retry. Doing
            # it here means retries still happen while measurement streams keep the socket busy
            # (recv never timing out). It stops the instant real progress clears _last_popup.
            if self._last_popup is not None and is_app_busy(self._app_state):
                self._answer_popup(self._last_popup)
            try:
                raw = self._ws.recv()
            except Exception as e:
                if not self._running.is_set():
                    return
                if _is_timeout(e):
                    continue
                self.logger.warning(f"[{self.name}] receive error: {e}")
                continue

            if not raw:
                continue
            self.frames_received += 1
            frame = proto.parse_frame(raw)
            if frame:
                try:
                    self._handle(frame)
                except Exception as e:
                    self.logger.error(f"[{self.name}] error handling frame: {e}")

    def _handle(self, frame: Dict[str, Any]) -> None:
        message_type = frame["message_type"]
        data = frame["data"]

        if message_type == proto.MSG_POPUP:
            # Ignore the echo of our own reply.
            if frame.get("message_flow") == "response":
                return
            self._handle_popup(data)
            return

        # Only REAL test progress means the app accepted the pop-up answer and moved on:
        # a new execution state or a test-status change. Measurement STREAMS do NOT count —
        # the app streams signal/power data WHILE a coil pop-up is still up, so clearing on a
        # stream would abandon a click that never registered (the intermittent-stall bug).
        # ApplicationStatus pings don't clear it either.
        if message_type in (proto.MSG_TC_EXEC_STATE, proto.MSG_TEST_STATUS):
            self._last_popup = None

        if message_type == proto.MSG_APP_STATUS:
            app_state, connection_state = read_app_state(data)
            self._app_state = app_state
            self.system_state.app_state = app_state
            self.system_state.connection_state = connection_state
            self.logger.debug(f"[{self.name}] app={app_state} connection={connection_state}")

        elif message_type == proto.MSG_TC_EXEC_STATE:
            state = str(data) if data is not None else ""
            self._tc_state = state
            self.logger.info(f"[{self.name}] test execution state: {state}")
            # Only ask for measurement streams while a test is actually running.
            if state in proto.TC_RUNNING and self._subscription != proto.BUSY_ACTIVE:
                self._subscribe(proto.BUSY_ACTIVE)
            elif state in proto.TC_STOPPED and self._subscription != proto.READY:
                self._subscribe(proto.READY)

        elif message_type == proto.MSG_TEST_STATUS:
            self._handle_test_status(data)

        elif message_type in proto.STREAM_TYPES:
            if data is not None:
                self._telemetry[message_type].append(data)
                self._stream_frame_count += 1   # data-progress signal (WP 1.0)
                # Log the first frame of each stream type — this is how we confirm the app
                # actually pushes live measurement streams to our external client.
                if message_type not in self._stream_seen:
                    self._stream_seen.add(message_type)
                    self.logger.info(
                        f"[{self.name}] live stream received: {message_type} "
                        f"(first frame; native WS capture is available for this type)"
                    )

    def _handle_test_status(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        # The WebSocket frame carries "Status"; the REST endpoint calls it "Test Status".
        info = data.get("Test Status") or data.get("Status") or ""
        if isinstance(info, str) and info.startswith("Test:"):
            parts = info.split(":")
            if len(parts) >= 3:
                self.system_state.test_case_name = parts[1].strip()
                self.system_state.test_status = parts[-1].strip()

    def _handle_popup(self, data: Any) -> None:
        # The app pushes the pop-up over the socket. Record it (so the pop-up JSON logs match
        # the REST build) and answer it. A fresh push means a fresh appearance, so clear the
        # per-id throttle for this pop-up.
        popup = proto.extract_popup(data)
        if not popup:
            return
        message = proto.popup_text(popup)
        if message:
            self.logger.info(f"[{self.name}] pop-up: {message[:80]}")
        self._record_popup(message)
        self._last_popup = popup
        # Fresh appearance of this pop-up id — reset its throttle and retry window so it is
        # answered again from scratch.
        pid = popup.get("id")
        self._answered.pop(pid, None)
        self._popup_first_seen.pop(pid, None)
        self._answer_popup(popup)

    def _record_popup(self, message: str) -> None:
        """Log the pop-up to the same JSON files the REST poll loop writes, so the run's
        output is identical on a WebSocket build (where the REST message box stays empty)."""
        if not self.popup_manager or not message:
            return
        payload = {"message": message}
        try:
            self.popup_manager.save_only_message(payload)
            self.popup_manager.save_message_by_test_case(payload)
        except Exception as e:
            self.logger.debug(f"[{self.name}] could not record pop-up: {e}")

    def _answer_popup(self, popup: Dict[str, Any]) -> None:
        """
        Dismiss an operator pop-up, adapting to how this build exposes it:

        1. REST message box — some builds also register the pop-up under GetMessageBox;
           answering over PutMessageBoxResponse dismisses it. Tried first (cheap, no UI).
        2. Desktop UI — on a WebSocket build the pop-up is a front-end/DSUI pop-up owned by
           the app's own Electron window; the backend ignores any external socket's reply and
           keeps GetMessageBox empty. The build-faithful answer is to press the pop-up's
           button in that window, exactly as an operator would (see ui_popup).
        """
        pid = popup.get("id")
        now = time.time()

        # Stop retrying a pop-up we've been unable to clear for a long time (avoid hammering).
        first = self._popup_first_seen.setdefault(pid, now)
        if now - first > POPUP_ANSWER_WINDOW_S:
            return

        last = self._answered.get(pid)
        if last is not None and (now - last) < POPUP_REANSWER_S:
            return  # answered recently; give the app a moment to register it before retrying

        # 1) REST message box (no-op on builds where the box is empty).
        answered = self._answer_pending_popup()

        # 2) Desktop UI — the real answer path for WebSocket-build operator pop-ups. A real
        #    click (see ui_popup) fires the Electron button's JS handler; we keep retrying via
        #    the listen loop until the app makes real progress (which clears _last_popup).
        if not answered:
            button = proto.choose_button(popup)
            try:
                answered = ui_popup.click_popup_button(button, logger=self.logger)
            except Exception as e:
                self.logger.debug(f"[{self.name}] UI pop-up answer failed: {e}")

        if answered:
            self._answered[pid] = now          # throttle the next retry
            if pid not in self._popup_counted:  # count each distinct pop-up once, not per retry
                self._popup_counted.add(pid)
                self.popups_answered += 1

    def _answer_pending_popup(self) -> bool:
        """Answer any operator pop-up over REST. Returns True if one was answered."""
        if not self.popup_manager:
            return False
        try:
            return bool(self.popup_manager.answer_via_rest())
        except Exception as e:
            self.logger.debug(f"[{self.name}] REST pop-up answer failed: {e}")
            return False

    # -- test progress -----------------------------------------------------

    def is_test_running(self) -> bool:
        """Whether a test is still executing — a definitive app-state is authoritative over the
        pushed execution-state latch (WP 1.1).

        The hang bug (B-hang-1): the app pushes ``TCExecutionServerState="Start"`` exactly once
        and often never pushes ``"Stop"``, so trusting the latch first meant a run could report
        "running" forever even after the app went idle. Fix = pure state precedence, no timer,
        no config:

          1. An explicit ``"Stop"`` execution-state → ended (trust it immediately).
          2. A DEFINITIVE app-state wins over a possibly-stale ``"Start"`` latch:
             READY → ended, BUSY → running. This is what a stale ``"Start"`` could never clear.
          3. Only when app-state is UNKNOWN do we fall back to the pushed ``"Start"`` latch,
             then a one-shot REST read.

        (This aligns the WS transport with the REST transport, which already treats app-state as
        authoritative. The "app BUSY but internally wedged" incident is out of scope here — that
        is caught by the DATA-cessation watchdog, WP 1.2, not by state.)
        """
        if self._tc_state in proto.TC_STOPPED:
            return False

        if is_app_ready(self._app_state):
            return False
        if is_app_busy(self._app_state):
            return True

        # App-state not yet known — fall back to the pushed running latch, then a REST read.
        if self._tc_state in proto.TC_RUNNING:
            return True
        return self._rest_fallback_running()

    def _rest_fallback_running(self) -> bool:
        """The app hasn't pushed state yet — ask once over REST so we never stall."""
        try:
            response = self.api_handler.call_api(ApiName.GET_APP_STATE)
            data = response.get("response", {}).get("data", {}) or {}
            app_state, connection_state = read_app_state(data)
            if app_state != UNKNOWN:
                self.system_state.app_state = app_state
                self.system_state.connection_state = connection_state
            return is_app_busy(app_state)
        except Exception as e:
            self.logger.debug(f"[{self.name}] REST state fallback failed: {e}")
            return False

    def get_telemetry(self) -> Dict[str, Any]:
        """Live measurement data collected during the run."""
        return {
            "counts": {name: len(items) for name, items in self._telemetry.items()},
            "data": self._telemetry,
        }

    def data_frame_count(self) -> int:
        """Monotonic count of measurement STREAM frames received so far (WP 1.0).

        Thread-safe to read: only the listener thread writes it and an int read is atomic
        under the GIL. While this climbs the tester is delivering measurement data, so the
        monitor loop treats an increase as liveness.
        """
        return self._stream_frame_count

    def popup_pending(self) -> bool:
        """Whether an operator pop-up is currently up (WP 1.2 watchdog pause).

        `_last_popup` is set when the app pushes a pop-up and cleared only on REAL test
        progress (a new execution-state or test-status frame), so it stays True across a
        coil-placement wait — exactly the no-data period the watchdog must not abort.
        """
        return self._last_popup is not None


def _is_timeout(error: Exception) -> bool:
    """A recv timeout is normal — it's just how we stay responsive to stop()."""
    return "timed out" in str(error).lower() or type(error).__name__ == "WebSocketTimeoutException"
