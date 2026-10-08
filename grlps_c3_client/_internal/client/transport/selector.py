# client/transport/selector.py
"""
Picks the live-data transport to use for the app we're attached to.

The C3 app ships in one of two modes and only one is active at a time. Rather than making
the operator configure which, we ask the app itself:

    GET /api/App/GetWebSocketSupport   ->  true   : WebSocket build
                                           404/false : REST-polling build

Configuration (`websocket` block in grl_config.json) can override the decision:

    "websocket": {
        "mode": "auto",     # auto (default) | ws | rest
        "ws_url": null,     # default: ws://<host>:<port>/ws, derived from the app's base URL
        "poll_ms": 500      # REST polling interval
    }

If WebSocket is chosen but the socket cannot be established, we fall back to REST polling
rather than failing the run.
"""
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from API import ApiName
from .base import LiveTransport
from .hybrid_transport import HybridLiveTransport
from .rest_transport import RestLiveTransport, DEFAULT_POLL_MS
from .ws_transport import WsLiveTransport, WebSocketUnavailable

MODE_AUTO = "auto"
MODE_WS = "ws"
MODE_REST = "rest"
MODE_HYBRID = "hybrid"


def supports_websocket(api_handler, logger) -> bool:
    """Ask the app whether this build serves live data over a WebSocket."""
    try:
        response = api_handler.call_api(ApiName.GET_WEBSOCKET_SUPPORT)
        inner = response.get("response", {})

        if not inner.get("success"):
            # Expected on a REST build: the endpoint is only compiled into the WebSocket
            # build, so a 404 here is the normal answer and not a defect. It is not proof
            # on its own — some old WebSocket builds also lacked it — so a build known to
            # be WebSocket should be forced with mode "ws" (or "hybrid").
            logger.info(
                f"GetWebSocketSupport returned HTTP {inner.get('status_code')} — reading this "
                f"as a REST build (the endpoint ships only in the WebSocket build)"
            )
            return False

        data = inner.get("data")
        supported = data is True or str(data).strip().lower() == "true"
        logger.info(f"App WebSocket support: {supported}")
        return supported

    except Exception as e:
        logger.warning(f"Could not probe WebSocket support ({e}) — assuming REST polling")
        return False


def derive_ws_url(base_url: Optional[str]) -> Optional[str]:
    """Turn http://localhost:2004/api into ws://localhost:2004/ws."""
    if not base_url:
        return None
    parsed = urlparse(base_url)
    if not parsed.netloc:
        return None
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws"


def create_live_transport(api_handler, logger, system_state, popup_manager,
                          base_url: Optional[str] = None,
                          websocket_config: Optional[Dict[str, Any]] = None) -> LiveTransport:
    """
    Build the right transport for this app, with automatic fallback.

    Never raises on WebSocket failure — it degrades to REST polling so a run always proceeds.
    """
    config = websocket_config or {}
    mode = str(config.get("mode", MODE_AUTO)).strip().lower()
    poll_ms = int(config.get("poll_ms", DEFAULT_POLL_MS))
    ws_url = config.get("ws_url") or derive_ws_url(base_url)

    def rest() -> LiveTransport:
        return RestLiveTransport(api_handler, logger, system_state, popup_manager, poll_ms=poll_ms)

    def hybrid() -> LiveTransport:
        if not ws_url:
            logger.warning("Hybrid selected but no ws_url — using REST polling")
            return rest()
        logger.info(f"Live data: Hybrid — REST polling + opportunistic WebSocket ({ws_url})")
        return HybridLiveTransport(ws_url, api_handler, logger, system_state, popup_manager, poll_ms=poll_ms)

    def pure_ws() -> LiveTransport:
        if not ws_url:
            logger.warning("WebSocket selected but no ws_url — using REST polling")
            return rest()
        transport = WsLiveTransport(ws_url, api_handler, logger, system_state, popup_manager)
        try:
            transport.start()
            logger.info(f"Live data: WebSocket ({ws_url})")
            return transport
        except (WebSocketUnavailable, Exception) as e:
            logger.warning(f"WebSocket failed to start ({e}) — falling back to REST polling")
            return rest()

    # Explicit overrides.
    if mode == MODE_REST:
        logger.info("Live data: REST polling (forced by config)")
        return rest()
    if mode == MODE_HYBRID:
        return hybrid()
    if mode == MODE_WS:
        logger.info("Live data: WebSocket (forced by config)")
        return pure_ws()
    if mode != MODE_AUTO:
        logger.warning(f"Unknown websocket.mode '{mode}' — using auto-detect")

    # Auto-detect via `GetWebSocketSupport`:
    #   true -> WebSocket build   -> pure WebSocket
    #   404  -> REST build        -> REST polling
    # Current app builds report this correctly (a WebSocket build returns true, a REST build
    # 404), so this is reliable. NOTE: some *older* WebSocket builds (pre-2.231.1.x) served a
    # WebSocket but returned 404 and could not be distinguished from a REST build. If you must
    # support such a build, set mode to "hybrid" (runs REST + an opportunistic WebSocket).
    if supports_websocket(api_handler, logger):
        return pure_ws()
    return rest()
