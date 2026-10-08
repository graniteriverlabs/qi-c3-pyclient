# client/transport/hybrid_transport.py
"""
Hybrid live-data transport — runs WebSocket and REST polling at the same time.

Why this exists: some app builds serve a WebSocket but do NOT advertise it
(`GetWebSocketSupport` returns 404 on the MPP-TPR WebSocket build). When idle, such a build
is indistinguishable from a genuine REST build — both accept the `/ws` handshake, both answer
pings, both return 404 for the support probe. So there is no reliable way to *detect* which
one it is up front.

Hybrid sidesteps detection by using both:
- **REST polling** owns test progress and answers pop-ups. This works on *every* build,
  because every build serves the REST endpoints (`GetMessageBox` / `GetTestStatus` /
  `GetAppState`). So correctness never depends on the WebSocket.
- **WebSocket** is opened opportunistically. On a real WebSocket build it delivers the live
  measurement streams (Packets / Signals / PowerChart / PowerTransferInfo) and pushed
  pop-ups; on a REST build it simply stays quiet.

A shared `PopupCoordinator` makes sure a pop-up seen on both channels is answered once.
"""
from typing import Any, Dict

from .base import LiveTransport
from .popup_coordinator import PopupCoordinator
from .rest_transport import RestLiveTransport
from .ws_transport import WsLiveTransport, WebSocketUnavailable


class HybridLiveTransport(LiveTransport):
    """Runs REST polling (authoritative) plus an opportunistic WebSocket for live streams."""

    name = "Hybrid"

    def __init__(self, ws_url, api_handler, logger, system_state, popup_manager,
                 poll_ms: int = 500):
        self.logger = logger
        self.coordinator = PopupCoordinator()

        # REST poller answers pop-ups + tracks status. Share the coordinator so it and the
        # WebSocket don't both answer the same pop-up.
        if popup_manager and hasattr(popup_manager, "set_popup_coordinator"):
            popup_manager.set_popup_coordinator(self.coordinator)
        self.rest = RestLiveTransport(api_handler, logger, system_state, popup_manager, poll_ms=poll_ms)

        # WebSocket adds live streams (and pushed pop-ups on a WS build), deduped via the coordinator.
        self.ws = WsLiveTransport(
            ws_url, api_handler, logger, system_state, popup_manager,
            popup_coordinator=self.coordinator,
        )
        self._ws_active = False

    def start(self) -> None:
        # REST first: it's the part that must always work.
        self.rest.start()
        # WebSocket is best-effort — if it can't connect, we simply run as REST-only.
        try:
            self.ws.start()
            self._ws_active = True
            self.logger.info(f"[{self.name}] REST polling + WebSocket both active")
        except WebSocketUnavailable as e:
            self._ws_active = False
            self.logger.info(f"[{self.name}] WebSocket unavailable ({e}); running REST-only")
        except Exception as e:
            self._ws_active = False
            self.logger.warning(f"[{self.name}] WebSocket failed to start ({e}); running REST-only")

    def stop(self) -> None:
        if self._ws_active:
            try:
                self.ws.stop()
            except Exception as e:
                self.logger.warning(f"[{self.name}] error stopping WebSocket: {e}")
            self._ws_active = False
        self.rest.stop()

    def is_test_running(self) -> bool:
        # REST is authoritative — GetAppState works on every build.
        return self.rest.is_test_running()

    def get_telemetry(self) -> Dict[str, Any]:
        # Live measurements only exist if the WebSocket delivered them.
        return self.ws.get_telemetry() if self._ws_active else {}

    def data_frame_count(self) -> int:
        # Stream frames only arrive on the opportunistic WebSocket half (WP 1.0).
        return self.ws.data_frame_count() if self._ws_active else 0

    def popup_pending(self) -> bool:
        # A pop-up is pending if either channel sees it (WP 1.2). REST is authoritative and
        # always present; the WS half adds pushed pop-ups when active.
        if self.rest.popup_pending():
            return True
        return bool(self._ws_active and self.ws.popup_pending())
