# client/transport/popup_coordinator.py
"""
Cross-channel pop-up de-duplication.

In hybrid mode the WebSocket and the REST poller run at the same time. On a WebSocket build
the same pop-up can appear on *both* channels (pushed over the socket, and also visible via
`GetMessageBox`). Without coordination both would answer it, sending two responses for one
dialog.

The coordinator makes answering a pop-up a claim: the first channel to see it wins and
answers; the other skips. Pop-ups are keyed by their message text (the one field both
channels carry), within a short time window so a genuinely repeated prompt later is still
handled.
"""
import threading
import time

DEFAULT_WINDOW_S = 5.0


class PopupCoordinator:
    """Thread-safe: ensures a given pop-up is answered by exactly one channel."""

    def __init__(self, window_s: float = DEFAULT_WINDOW_S):
        self._window_s = window_s
        self._claimed = {}  # message text -> time it was claimed
        self._lock = threading.Lock()

    def claim(self, message: str) -> bool:
        """
        Try to claim responsibility for answering this pop-up.

        Returns True if the caller should answer it (first to claim within the window),
        False if another channel already has.
        """
        if not message:
            return True  # nothing to key on; let the caller proceed

        now = time.time()
        with self._lock:
            last = self._claimed.get(message)
            if last is not None and (now - last) < self._window_s:
                return False
            self._claimed[message] = now
            self._prune(now)
            return True

    def _prune(self, now: float) -> None:
        """Drop entries older than the window so the map can't grow without bound."""
        stale = [msg for msg, t in self._claimed.items() if (now - t) >= self._window_s]
        for msg in stale:
            del self._claimed[msg]
