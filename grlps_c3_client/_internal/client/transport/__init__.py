# client/transport/__init__.py
"""
Live-data transport layer.

The C3 app ships in two mutually exclusive modes:

    REST build       live data is polled (~500 ms)
    WebSocket build  live data is pushed over ws://<host>/ws

Everything else (launch, connect, project setup, submitting tests) is identical REST on both.
Only live data — pop-ups, test progress, and measurement streams — differs, so only that is
abstracted here. `create_live_transport()` asks the app which build it is and returns the
right one, falling back to REST if the WebSocket cannot be established.
"""
from .app_state import read_app_state, is_app_ready, is_app_busy
from .base import LiveTransport
from .hybrid_transport import HybridLiveTransport
from .popup_coordinator import PopupCoordinator
from .rest_transport import RestLiveTransport
from .selector import create_live_transport, supports_websocket, derive_ws_url
from .ws_transport import WsLiveTransport, WebSocketUnavailable

__all__ = [
    "LiveTransport",
    "RestLiveTransport",
    "WsLiveTransport",
    "HybridLiveTransport",
    "PopupCoordinator",
    "WebSocketUnavailable",
    "create_live_transport",
    "supports_websocket",
    "derive_ws_url",
    "read_app_state",
    "is_app_ready",
    "is_app_busy",
]
