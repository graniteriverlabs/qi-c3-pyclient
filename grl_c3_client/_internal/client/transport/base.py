# client/transport/base.py
"""
The live-data transport interface.

The C3 app ships in two modes and only one is active at a time:

    REST build       live data is *pulled* by polling every ~500 ms
    WebSocket build  live data is *pushed* by the app over ws://<host>/ws

Everything else the client does (launch, connect, project setup, submitting tests) is
identical REST on both builds. Only the live-data path differs, so only that path is
abstracted here.

Callers (ConnectionManager, TestManager) talk to this interface and never know which
build they are attached to.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict


class LiveTransport(ABC):
    """Common interface for receiving live data (pop-ups, test status, telemetry)."""

    #: Short name used in logs, e.g. "REST" or "WebSocket".
    name: str = "base"

    @abstractmethod
    def start(self) -> None:
        """Begin receiving live data. Must be safe to call more than once."""

    @abstractmethod
    def stop(self) -> None:
        """Stop receiving live data and release resources. Must be safe to call twice."""

    @abstractmethod
    def is_test_running(self) -> bool:
        """
        Whether a test is currently executing.

        Returns False once the application is idle again, which is how the caller
        knows a run has finished.
        """

    def get_telemetry(self) -> Dict[str, Any]:
        """
        Live measurement data collected so far (power chart, signals, packets).

        Only the WebSocket transport can supply this; the REST build does not expose it,
        so the default is empty.
        """
        return {}

    def data_frame_count(self) -> int:
        """
        Monotonic count of measurement STREAM frames received so far (WP 1.0 data-progress).

        Only a push transport (WebSocket) sees these frames; a REST build gets its
        measurement data through the packet log instead, so the default is 0 — the REST
        half of the data-progress signal comes from the capture manager's packet count.
        """
        return 0

    def popup_pending(self) -> bool:
        """
        Whether an operator pop-up is currently up and awaiting action (WP 1.2).

        The data-stall watchdog pauses while this is True, because an operator wait (e.g.
        coil placement) legitimately produces no measurement data and must not be mistaken
        for a stall. Default False; each transport reports its own pop-up state.
        """
        return False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
