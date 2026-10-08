# client/core/http_trace.py
"""
Opt-in raw HTTP trace (Phase 4 / B5) — a debugging aid, off by default.

When enabled (common.trace_http = true in grl_config.json) this wraps the shared
requests.Session so EVERY HTTP call the client makes is recorded to a JSONL file with timing.
Crucially it hooks at the SESSION level, so it captures not just the enum API calls but also
the capture manager's `GetCCLinePackets` polls and `PostCurrentTestReport` POSTs — which go
through the same session directly. Those are exactly the calls we need to correlate against a
run stall, so this is the instrument for the hang RCA (which script action preceded the moment
measurement data ceased).

Design choices:
  - Bodies are NOT written, only their sizes. That keeps the trace small, sidesteps binary
    payloads (report ZIPs) and any secret-redaction worry, and timing + endpoint + status is
    all the RCA needs.
  - One JSONL line per call: {ts, method, url, dur_ms, status, req_bytes, resp_bytes[, error]}.
  - Thread-safe (a lock guards the append) — the WS listener and monitor threads can both
    trigger calls.
  - Idempotent and cheap-when-off: wrapping happens only if the flag is set, so a normal run
    does zero extra work.
"""
import json
import os
import threading
import time
from datetime import datetime


def _body_size(js, data) -> int:
    """Best-effort byte size of a request body, without keeping the body itself."""
    try:
        if js is not None:
            return len(json.dumps(js))
        if data is not None:
            return len(data if isinstance(data, (bytes, str)) else str(data))
    except Exception:
        pass
    return 0


def install_http_trace(session, path: str, logger) -> None:
    """
    Wrap ``session.request`` so each HTTP call appends one JSONL record to ``path``.

    Idempotent: a session is wrapped at most once. Never raises out to the caller — a trace
    problem must never disturb a run.
    """
    if session is None or getattr(session, "_grl_trace_installed", False):
        return

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    lock = threading.Lock()
    original_request = session.request

    def traced_request(method, url, **kwargs):
        started = time.time()
        response = None
        error = None
        try:
            response = original_request(method, url, **kwargs)
            return response
        except Exception as e:                     # record the failure, then re-raise
            error = repr(e)
            raise
        finally:
            record = {
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "method": str(method).upper(),
                "url": url,
                "dur_ms": round((time.time() - started) * 1000, 1),
                "status": getattr(response, "status_code", None),
                "req_bytes": _body_size(kwargs.get("json"), kwargs.get("data")),
                "resp_bytes": len(response.content) if response is not None else 0,
            }
            if error:
                record["error"] = error
            try:
                line = json.dumps(record)
                with lock:
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
            except Exception as e:
                logger.debug(f"[trace] could not write trace record: {e}")

    session.request = traced_request
    session._grl_trace_installed = True
    logger.info(f"[trace] HTTP trace enabled -> {path}")
