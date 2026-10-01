# client/transport/ws_protocol.py
"""
The C3 WebSocket wire protocol.

Frames are JSON text. A client frame carries a SHA-256 `checkSum`; the app silently
ignores frames without a valid one (verified against frames captured from the real app).

    checkSum = sha256( json.dumps({"header": ..., "payload": ...}, separators=(",", ":")) )

The hash covers the compact JSON of the message *excluding* the checkSum field itself.

Subscribing: the client sends one frame per stream carrying its own state. The app only
streams measurement data while the client says BUSY_ACTIVE, so the subscription state is
updated as the test starts and stops.
"""
import hashlib
import json
from typing import Any, Dict, Optional

# --- Subscription states the client advertises (DSUISocketStatus) ---
READY = "READY"                # idle: app streams nothing
BUSY_ACTIVE = "BUSY_ACTIVE"    # test running: app streams measurement data
BUSY_PAUSED = "BUSY_PAUSED"    # test paused

# --- Streams the client subscribes to (client -> server) ---
STREAM_TYPES = ("Packets", "Signals", "PowerChart", "PowerTransferInfo")

# --- Frames the app pushes (server -> client) ---
MSG_POPUP = "MessagePopupData"
MSG_APP_STATUS = "ApplicationStatus"
MSG_TEST_STATUS = "TestStatus"
MSG_TC_EXEC_STATE = "TCExecutionServerState"

#: TCExecutionServerState payload values that mean a test is under way.
TC_RUNNING = ("Start", "InProgress")
TC_STOPPED = ("Stop",)


def checksum(message: Dict[str, Any]) -> str:
    """SHA-256 of the compact JSON of {header, payload} — excluding any checkSum field."""
    body = {"header": message["header"], "payload": message["payload"]}
    compact = json.dumps(body, separators=(",", ":"))
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def build_frame(message_type: str, data: Any, message_flow: str = "request") -> str:
    """Build a checksummed client frame, serialized ready for ws.send()."""
    message: Dict[str, Any] = {
        "header": {"messageType": message_type, "messageFlow": message_flow},
        "payload": {"data": data},
    }
    message["checkSum"] = checksum(message)
    return json.dumps(message, separators=(",", ":"))


def build_subscribe(message_type: str, state: str) -> str:
    """Subscribe (or re-subscribe) a stream with the client's current state."""
    return build_frame(message_type, state)


def build_popup_reply(popup: Dict[str, Any], button: Optional[str] = None) -> str:
    """
    Build the reply that dismisses a pop-up.

    Note the flow is "response", not "request" — this is an answer to the app's notification.
    """
    return build_frame(
        MSG_POPUP,
        {
            "id": popup.get("id"),
            "responseFields": {"responseButton": button or choose_button(popup)},
            "status": "RESPONDED",
        },
        message_flow="response",
    )


def choose_button(popup: Dict[str, Any]) -> str:
    """Pick which button to press: the app's default, else OK, else the first offered."""
    default = popup.get("defaultButton")
    if default:
        return str(default)

    buttons = ((popup.get("inputFields") or {}).get("inputButtons") or "")
    options = [b.strip() for b in str(buttons).split(",") if b.strip()]
    for option in options:
        if option.upper() == "OK":
            return option
    return options[0] if options else "OK"


def parse_frame(raw: str) -> Optional[Dict[str, Any]]:
    """Parse an incoming frame into {message_type, message_flow, data}; None if unusable."""
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(message, dict):
        return None

    header = message.get("header") or {}
    payload = message.get("payload") or {}
    return {
        "message_type": header.get("messageType", ""),
        "message_flow": header.get("messageFlow", ""),
        "seq": header.get("seq"),
        "data": payload.get("data"),
    }


def extract_popup(data: Any) -> Optional[Dict[str, Any]]:
    """
    Pull the pop-up out of a MessagePopupData frame.

    The payload nests it: {"MessagePopupData": {id, title, description, ...}}.
    Returns None for pop-ups that have already been dealt with.
    """
    if not isinstance(data, dict):
        return None

    popup = data.get("MessagePopupData", data)
    if not isinstance(popup, dict) or popup.get("id") is None:
        return None

    status = str(popup.get("status", "")).upper()
    if status in ("RESPONDED", "CLOSED", "CANCELLED", "DISMISSED"):
        return None

    return popup


def popup_text(popup: Dict[str, Any]) -> str:
    """The human-readable pop-up message (REST calls this field `message`)."""
    return str(popup.get("description") or popup.get("message") or "")
