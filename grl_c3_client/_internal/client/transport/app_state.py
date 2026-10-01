# client/transport/app_state.py
"""
Application-state field compatibility.

The C3 app changed the field names it returns for application state:

    Older builds : {"appState": "READY",         "connectionState": "CONNECTED"}
    Newer builds : {"ApplicationState": "READY", "EthernetConnectionState": "CONNECTED"}

The newer (PascalCase) names are used by both the REST `GetAppState` endpoint and the
WebSocket `ApplicationStatus` frame. Reading only the old names silently yields "" on a
new build, which makes the test-completion check fail. Every state read goes through here.
"""
from typing import Any, Dict, Tuple

APP_STATE_KEYS = ("appState", "ApplicationState")
CONNECTION_STATE_KEYS = ("connectionState", "EthernetConnectionState")

UNKNOWN = "UNKNOWN"


def _first_present(data: Dict[str, Any], keys) -> str:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return UNKNOWN


def read_app_state(data: Any) -> Tuple[str, str]:
    """
    Extract (application_state, connection_state) from any GetAppState / ApplicationStatus payload.

    Accepts both the old camelCase and the new PascalCase field names.

    Returns:
        (app_state, connection_state) — each UNKNOWN if not present.
    """
    if not isinstance(data, dict):
        return UNKNOWN, UNKNOWN
    return (
        _first_present(data, APP_STATE_KEYS),
        _first_present(data, CONNECTION_STATE_KEYS),
    )


def is_app_ready(app_state: str) -> bool:
    """True when the application reports it is idle (i.e. no test is executing)."""
    return str(app_state).upper() == "READY"


def is_app_busy(app_state: str) -> bool:
    """True when the application reports it is executing something."""
    return str(app_state).upper() == "BUSY"
