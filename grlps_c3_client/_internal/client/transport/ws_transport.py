# client/transport/ws_transport.py
"""
Live data over WebSocket — the push transport.

One background thread owns the socket. The app pushes pop-ups, application state, test
status and measurement streams as they happen, so nothing is polled.

Test progress is driven primarily by the `TCExecutionServerState` frame (Start / InProgress
/ Stop). Because that frame is only sent on a change, a REST read of the app state is used
as a fallback until the first one arrives — that keeps a run correct even if the app pushes
nothing early on.

**API mode.** The application listens to one connection at a time, its "primary". Normally that
is its own window (UI mode), and anything another client sends is ignored - so a pop-up could
only be answered by clicking the window. This transport therefore connects as a Python client
(``/ws?clientType=api&clientId=<id>``), asks the application for API mode (``PutApiMode``),
reads the mode back, and only then counts as connected. In API mode:

* pop-ups are answered over this socket the way the REST loop answers them: a background pass
  every 0.5 s answers every pop-up still waiting, "OK" (a timed-out one too), NONE for one the
  application closed itself, without waiting for an acknowledgement;
* a pop-up waits from when it arrives until the application reports it closed (RESPONDED /
  NONE with its id) or the test moves on;
* control is handed back (``PutUiMode``) whenever the transport stops - a normal end, an error
  or Ctrl+C;
* if the person at the bench takes control back in the window, the application says so
  (``OperationMode`` ... ``TakenByUI``) and closes this socket with "Control taken by UI": the
  transport stops answering, never takes control again, and reports it through
  ``control_taken``.

An application build without API mode is refused with the reason (``ApiModeRefused``). There is
no window-click fallback.

``take_control=False`` gives the old receive-only client: it records what the application pushes
and answers nothing. The hybrid transport uses it for old builds, where REST polling answers.
"""
import threading
import time
import uuid
from typing import Any, Dict, Optional

from API import ApiName
from . import ws_protocol as proto
from .app_state import read_app_state, is_app_ready, is_app_busy, UNKNOWN
from .base import LiveTransport

CONNECT_TIMEOUT_S = 10
RECV_TIMEOUT_S = 1.0
#: PKG-04: how often waiting pop-ups are answered again, as the REST loop does.
POPUP_LOOP_S = 0.5
#: How long the application may take to register a new socket before PutApiMode can name it.
REGISTER_WAIT_S = 3.0
#: The application closes the socket of the client it took control from with this reason.
CLOSE_TAKEN_BY_UI = "Control taken by UI"

OPERATION_MODE_MESSAGE = "OperationMode"
MODE_API = "API"
MODE_UI = "UI"
REASON_TAKEN_BY_UI = "TakenByUI"


class WebSocketUnavailable(RuntimeError):
    """Raised when the WebSocket cannot be established."""


class ApiModeRefused(RuntimeError):
    """The application cannot be put in API mode. The message says why and what to do."""


def _response(result: Dict[str, Any]):
    """(HTTP status, parsed body) from a GRLApiHandler result."""
    inner = (result or {}).get("response") or {}
    return inner.get("status_code"), inner.get("data"), inner.get("error")


class WsLiveTransport(LiveTransport):
    """Receives live data pushed by the app over a WebSocket and, in API mode, answers pop-ups."""

    name = "WebSocket"

    def __init__(self, ws_url, api_handler, logger, system_state, popup_manager=None,
                 popup_coordinator=None, take_control: bool = True):
        self.ws_url = ws_url
        self.api_handler = api_handler
        self.logger = logger
        self.system_state = system_state
        self.popup_manager = popup_manager
        # In hybrid mode REST polling answers pop-ups, so this socket only listens.
        self.popup_coordinator = popup_coordinator
        self.take_control = bool(take_control) and popup_coordinator is None
        self.client_id = "grlps-c3-" + uuid.uuid4().hex[:12]

        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._popup_thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()

        # State learned from pushed frames.
        self._tc_state: Optional[str] = None      # Start / InProgress / Stop
        self._app_state: str = UNKNOWN
        self._subscription = proto.READY

        self._telemetry: Dict[str, list] = {name: [] for name in proto.STREAM_TYPES}
        # Which stream types have actually arrived on this socket, to log the first frame of each.
        self._stream_seen: set = set()
        # The most recent pop-up still up (the watchdog pauses while one is).
        self._last_popup: Optional[Dict[str, Any]] = None
        # API mode: every pop-up waiting for an answer, by id ->
        # {"popup", "reply" ("OK" | "NONE"), "case", "answers"}.
        self._pending: Dict[Any, Dict[str, Any]] = {}
        self._popup_counted: set = set()
        self.frames_received = 0
        self.popups_answered = 0
        # Monotonic count of measurement STREAM frames (Packets/Signals/PowerChart/
        # PowerTransferInfo) received - the WS half of the data-progress signal (WP 1.0).
        self._stream_frame_count = 0

        #: True once this client holds API mode.
        self.in_api_mode = False
        #: True once the person at the bench took control back in the window (PKG-09).
        self.control_taken = False
        #: Why the application closed this socket, when it did.
        self.closed_by_app: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._running.is_set():
            return
        if self.control_taken:
            # PKG-09: control was taken back at the bench - never take it again in this session.
            raise ApiModeRefused(
                "Control of the application was taken back at the bench (UI mode was selected in "
                "the application window), so this session does not take it again.")
        self._connect()
        self._running.set()
        self._thread = threading.Thread(target=self._listen, name="WsLiveTransport", daemon=True)
        self._thread.start()
        if self.take_control:
            try:
                self._take_api_mode()
            except Exception:
                self._shutdown_socket()
                raise
            self._popup_thread = threading.Thread(target=self._popup_loop, name="WsPopupAnswer",
                                                  daemon=True)
            self._popup_thread.start()
        self.logger.info(f"[{self.name}] live data started ({self.ws_url}"
                         f"{', API mode, client ' + self.client_id if self.in_api_mode else ''})")

    def _connect_url(self) -> str:
        if not self.take_control:
            return self.ws_url
        joiner = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{joiner}clientType=api&clientId={self.client_id}"

    def _connect(self) -> None:
        try:
            from websocket import create_connection
        except ImportError as e:
            raise WebSocketUnavailable(
                "The 'websocket-client' package is required for WebSocket mode "
                "(pip install websocket-client)"
            ) from e

        self.closed_by_app = None
        try:
            self._ws = create_connection(self._connect_url(), timeout=CONNECT_TIMEOUT_S)
            self._ws.settimeout(RECV_TIMEOUT_S)
        except Exception as e:
            raise WebSocketUnavailable(f"Could not connect to {self.ws_url}: {e}") from e

        self._subscribe(proto.READY)

    # -- API mode ----------------------------------------------------------

    def _take_api_mode(self) -> None:
        """
        PKG-02: switch the application to API mode for this client and read it back. Raises
        ApiModeRefused, with the reason and what to do, when that is not possible (PKG-03).
        """
        body = {"clientId": self.client_id}
        deadline = time.time() + REGISTER_WAIT_S
        while True:
            status, data, error = _response(self.api_handler.call_api(ApiName.PUT_API_MODE, data=body))
            code = data.get("error") if isinstance(data, dict) else None
            # The socket is registered by the application a moment after it opens.
            if status == 404 and code == "NoSuchConnection" and time.time() < deadline:
                time.sleep(0.25)
                continue
            break

        if status == 200 and isinstance(data, dict) and data.get("mode") == MODE_API:
            status, state, error = _response(self.api_handler.call_api(ApiName.GET_OPERATION_MODE))
            if (status == 200 and isinstance(state, dict) and state.get("mode") == MODE_API
                    and state.get("apiClientId") == self.client_id):
                self.in_api_mode = True
                self.logger.info(f"[{self.name}] API mode: this client ({self.client_id}) controls "
                                 f"the application; pop-ups are answered over this socket")
                # PKG-07: the application streams live data by its primary's state - ours now.
                self._subscribe(self._subscription)
                return
            raise ApiModeRefused(
                "The application accepted API mode but does not report this client in control "
                "(GetOperationMode -> HTTP {0}: {1}). Nothing was started. Run again; if it "
                "repeats, close the application and open it again.".format(status, state or error))

        raise ApiModeRefused(self._refusal(status, code, data, error))

    def _refusal(self, status, code, data, error) -> str:
        message = data.get("message") if isinstance(data, dict) else None
        if status == 404 and code is None:
            return ("This application build has no API mode (PUT OperationMode/PutApiMode returned "
                    "HTTP 404), so the package cannot answer its pop-ups. Nothing was started.\n\n"
                    "MPP-TPR and MPP-TPT need an application build with API mode - the one that "
                    "shows a \"UI mode / API mode\" choice at the top right of its window. Install "
                    "that build and run again.")
        if code == "AnotherClientInControl":
            return ("Another Python client is already controlling the application{0}. Nothing was "
                    "started. Wait for its run to end, or take control back in the application "
                    "window (select UI mode), then run again.".format(
                        f" ({message})" if message else ""))
        if code == "AppBusy":
            return ("The application is busy - a test, an exerciser session or another operation "
                    "is running{0}. API mode can only be taken while it is idle. Nothing was "
                    "started. Wait for it to finish, or stop it in the window, then run "
                    "again.".format(f" ({message})" if message else ""))
        if code in ("NoSuchConnection", "NotPythonClient", "NoSession"):
            return ("The application did not register this package's connection as a Python "
                    "client ({0}{1}). Nothing was started. Run again; if it repeats, close the "
                    "application and open it again.".format(code, f": {message}" if message else ""))
        if status is None:
            return ("The application did not answer the request for API mode ({0}). Nothing was "
                    "started. Check that it is still running.".format(error or "no answer"))
        return ("The application refused API mode (HTTP {0}{1}). Nothing was started.".format(
            status, f": {code} - {message}" if code else f": {data or error}"))

    def _give_back_control(self) -> None:
        """PKG-08: hand control back to the window. Never raises."""
        if not self.in_api_mode:
            return
        self.in_api_mode = False
        if self.control_taken:
            return   # the window already has it
        try:
            status, data, error = _response(
                self.api_handler.call_api(ApiName.PUT_UI_MODE, data={"clientId": self.client_id}))
            if status == 200:
                self.logger.info(f"[{self.name}] control handed back to the application window "
                                 f"(UI mode)")
            else:
                self.logger.warning(
                    f"[{self.name}] could not hand control back (HTTP {status}: {data or error}). "
                    f"The application returns to UI mode by itself when this connection closes.")
        except Exception as e:
            self.logger.warning(f"[{self.name}] could not hand control back ({e}). The "
                                f"application returns to UI mode by itself when this connection "
                                f"closes.")

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._give_back_control()
        self._running.clear()
        for thread in (self._popup_thread, self._thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=5)
        self._popup_thread = None
        self._thread = None
        self._shutdown_socket()
        stream_counts = {name: len(items) for name, items in self._telemetry.items() if items}
        self.logger.info(
            f"[{self.name}] live data stopped "
            f"(frames={self.frames_received}, pop-ups answered={self.popups_answered}, "
            f"stream frames={stream_counts or 'none'})"
        )
        if stream_counts:
            self.logger.info(
                f"[{self.name}] measurement streams DID reach this client: "
                f"{sorted(stream_counts)} — native WS capture usable"
            )
        else:
            self.logger.info(
                f"[{self.name}] NO measurement streams reached this client — live-data capture "
                f"uses the REST endpoints (fallback)"
            )

    def _shutdown_socket(self) -> None:
        self._running.clear()
        try:
            if self._ws:
                self._ws.close()
        except Exception as e:
            self.logger.debug(f"[{self.name}] error closing socket: {e}")
        self._ws = None

    # -- sending -----------------------------------------------------------

    def _send(self, frame: str) -> None:
        with self._send_lock:
            if self._ws:
                self._ws.send(frame)

    def _subscribe(self, state: str) -> None:
        """Tell the app which state we're in; it only streams data while BUSY_ACTIVE."""
        for stream in proto.SUBSCRIBE_TYPES:
            try:
                self._send(proto.build_subscribe(stream, state))
            except Exception as e:
                self.logger.warning(f"[{self.name}] failed to subscribe {stream}: {e}")
        self._subscription = state
        self.logger.debug(f"[{self.name}] subscribed streams as {state}")

    # -- receive loop ------------------------------------------------------

    def _listen(self) -> None:
        from websocket import ABNF, WebSocketConnectionClosedException

        while self._running.is_set():
            ws = self._ws
            if ws is None:
                return
            try:
                opcode, frame = ws.recv_data_frame(True)
            except Exception as e:
                if not self._running.is_set():
                    return
                if _is_timeout(e):
                    continue
                if isinstance(e, (WebSocketConnectionClosedException, OSError)):
                    self._on_closed(None, e)
                    return
                self.logger.warning(f"[{self.name}] receive error: {e}")
                continue

            if opcode == ABNF.OPCODE_CLOSE:
                data = frame.data or b""
                reason = data[2:].decode("utf-8", errors="replace") if len(data) > 2 else ""
                self._on_closed(reason, None)
                return
            if opcode not in (ABNF.OPCODE_TEXT, ABNF.OPCODE_BINARY):
                continue
            raw = frame.data.decode("utf-8", errors="replace") if isinstance(frame.data, bytes) \
                else frame.data
            if not raw:
                continue
            self.frames_received += 1
            parsed = proto.parse_frame(raw)
            if parsed:
                try:
                    self._handle(parsed)
                except Exception as e:
                    self.logger.error(f"[{self.name}] error handling frame: {e}")

    def _on_closed(self, reason: Optional[str], error: Optional[Exception]) -> None:
        """The application closed this socket (a take-back, a restart) or the link dropped."""
        self.closed_by_app = reason if reason is not None else f"connection lost: {error}"
        if reason == CLOSE_TAKEN_BY_UI:
            self._mark_taken_by_ui()
        elif self._running.is_set():
            self.logger.warning(f"[{self.name}] the application closed this connection "
                                f"({self.closed_by_app}); test progress is read over REST from "
                                f"here on")
        self.in_api_mode = False
        with self._pending_lock:
            self._pending.clear()
        self._last_popup = None

    def _mark_taken_by_ui(self) -> None:
        if self.control_taken:
            return
        self.control_taken = True
        self.in_api_mode = False
        self.logger.warning(
            f"[{self.name}] control was TAKEN BACK at the bench: UI mode was selected in the "
            f"application window. The application stops the running test; this run ends and does "
            f"not take control again.")

    def _handle(self, frame: Dict[str, Any]) -> None:
        message_type = frame["message_type"]
        data = frame["data"]

        if message_type == proto.MSG_POPUP:
            # Ignore the echo of our own reply.
            if frame.get("message_flow") == "response":
                return
            self._handle_popup(data)
            return

        if message_type == OPERATION_MODE_MESSAGE:
            self._handle_operation_mode(data)
            return

        # Only REAL test progress means the app accepted the pop-up answer and moved on:
        # a new execution state or a test-status change. Measurement STREAMS do NOT count —
        # the app streams signal/power data WHILE a coil pop-up is still up.
        if message_type == proto.MSG_TC_EXEC_STATE:
            self._last_popup = None
            with self._pending_lock:
                self._pending.clear()
        elif message_type == proto.MSG_TEST_STATUS:
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
                if message_type not in self._stream_seen:
                    self._stream_seen.add(message_type)
                    self.logger.info(
                        f"[{self.name}] live stream received: {message_type} "
                        f"(first frame; native WS capture is available for this type)"
                    )

    def _handle_operation_mode(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        self.logger.info(f"[{self.name}] operation mode: {data.get('mode')} "
                         f"({data.get('reason')})")
        if (data.get("mode") == MODE_UI and data.get("reason") == REASON_TAKEN_BY_UI
                and data.get("apiClientId") in (None, self.client_id)):
            self._mark_taken_by_ui()

    def _handle_test_status(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        # The WebSocket frame carries "Status"; the REST endpoint calls it "Test Status".
        info = data.get("Test Status") or data.get("Status") or ""
        if isinstance(info, str) and info.startswith("Test:"):
            parts = info.split(":")
            if len(parts) >= 3:
                case = parts[1].strip()
                self.system_state.test_case_name = case
                self.system_state.test_status = parts[-1].strip()
                # PKG-05: a pop-up of an earlier case is no longer waiting once the test moved on.
                with self._pending_lock:
                    for pid in [p for p, rec in self._pending.items()
                                if rec.get("case") and rec["case"] != case]:
                        self._pending.pop(pid, None)

    # -- pop-ups -----------------------------------------------------------

    def _handle_popup(self, data: Any) -> None:
        raw = data.get("MessagePopupData", data) if isinstance(data, dict) else None
        if not isinstance(raw, dict) or raw.get("id") is None:
            return
        pid = raw.get("id")
        status = str(raw.get("status", "")).upper()

        # The application's closing notice (API mode): the pop-up is gone.
        if status in ("RESPONDED", "NONE", "CLOSED", "CANCELLED", "DISMISSED"):
            with self._pending_lock:
                done = self._pending.pop(pid, None)
                if not self._pending:
                    self._last_popup = None
            if done is not None:
                self.logger.debug(f"[{self.name}] pop-up {pid} closed by the application")
            return

        popup = raw
        message = proto.popup_text(popup)
        with self._pending_lock:
            known = pid in self._pending
        if not known:
            if message:
                self.logger.info(f"[{self.name}] pop-up: {message[:80]}")
            self._record_popup(message)
        self._last_popup = popup

        if not self.take_control:
            return
        # PKG-18: NONE for a pop-up the application closed itself, OK for everything else - a
        # timed-out one included. Without a reply the test waits for ever.
        reply = "NONE" if status == "TERMINATED" else "OK"
        with self._pending_lock:
            record = self._pending.setdefault(pid, {"answers": 0})
            record.update(popup=popup, reply=reply,
                          case=getattr(self.system_state, "test_case_name", None) or record.get("case"))
        self._answer(pid)          # at once, then again on every pass while it waits

    def _record_popup(self, message: str) -> None:
        """Log the pop-up to the same JSON files the REST poll loop writes (PKG-10)."""
        if not self.popup_manager or not message:
            return
        payload = {"message": message}
        try:
            self.popup_manager.save_only_message(payload)
            self.popup_manager.save_message_by_test_case(payload)
        except Exception as e:
            self.logger.debug(f"[{self.name}] could not record pop-up: {e}")

    def _answer(self, pid: Any) -> None:
        """Send this pop-up's reply over the socket. No acknowledgement is awaited (PKG-04)."""
        if not self.in_api_mode or self.control_taken:
            return
        with self._pending_lock:
            record = self._pending.get(pid)
            if record is None:
                return
            popup, reply = record["popup"], record["reply"]
        if reply == "NONE":
            frame = proto.build_frame(proto.MSG_POPUP,
                                      {"id": pid, "responseFields": {}, "status": "NONE"},
                                      message_flow="response")
        else:
            frame = proto.build_popup_reply(popup, "OK")   # PKG-19: "OK", case-sensitive
        try:
            self._send(frame)
        except Exception as e:
            self.logger.debug(f"[{self.name}] could not answer pop-up {pid}: {e}")
            return
        with self._pending_lock:
            if pid in self._pending:
                self._pending[pid]["answers"] += 1
        if pid not in self._popup_counted:
            self._popup_counted.add(pid)
            self.popups_answered += 1
            self.logger.info(f"[{self.name}] pop-up {pid} answered {reply} over the socket")

    def _popup_loop(self) -> None:
        """PKG-04: every 0.5 s, answer again every pop-up that is still waiting."""
        while self._running.is_set():
            time.sleep(POPUP_LOOP_S)
            with self._pending_lock:
                waiting = list(self._pending)
            for pid in waiting:
                self._answer(pid)

    # -- test progress -----------------------------------------------------

    def is_test_running(self) -> bool:
        """Whether a test is still executing — a definitive app-state is authoritative over the
        pushed execution-state latch (WP 1.1).

          1. An explicit ``"Stop"`` execution-state → ended (trust it immediately).
          2. A DEFINITIVE app-state wins over a possibly-stale ``"Start"`` latch:
             READY → ended, BUSY → running.
          3. Only when app-state is UNKNOWN do we fall back to the pushed ``"Start"`` latch,
             then a one-shot REST read.

        Once the application has closed this socket (control taken back at the bench, or a
        restart) nothing is pushed any more, so the state is read over REST on every call.
        """
        if self.closed_by_app is not None or self.control_taken:
            return self._rest_fallback_running()

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
        """Ask the application over REST whether it is busy."""
        try:
            response = self.api_handler.call_api(ApiName.GET_APP_STATE)
            data = response.get("response", {}).get("data", {}) or {}
            app_state, connection_state = read_app_state(data)
            if app_state != UNKNOWN:
                self._app_state = app_state
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
        """Monotonic count of measurement STREAM frames received so far (WP 1.0)."""
        return self._stream_frame_count

    def popup_pending(self) -> bool:
        """Whether an operator pop-up is currently up (WP 1.2 watchdog pause)."""
        with self._pending_lock:
            if self._pending:
                return True
        return self._last_popup is not None


def _is_timeout(error: Exception) -> bool:
    """A recv timeout is normal — it's just how we stay responsive to stop()."""
    return "timed out" in str(error).lower() or type(error).__name__ == "WebSocketTimeoutException"
