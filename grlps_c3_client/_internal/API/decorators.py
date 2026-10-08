# API/decorators.py
"""
Decorators for API handlers.

This module provides decorators that can be applied to API handler methods
to add common functionality like logging, error handling, and performance
monitoring.
"""
from functools import wraps
import logging
import traceback
import time

logger = logging.getLogger(__name__)

#: A single API call slower than this is worth surfacing at WARNING.
SLOW_CALL_S = 5.0
#: Hard cap on any value rendered into a log line, so payloads never bloat the log.
_MAX_RENDER = 160


def _short(value, limit: int = _MAX_RENDER) -> str:
    """Render a value for a log line, bounded so request/response payloads never bloat the log."""
    try:
        s = repr(value)
    except Exception:
        s = f"<{type(value).__name__}>"
    return s if len(s) <= limit else f"{s[:limit]}… (+{len(s) - limit} chars)"


def _result_summary(result) -> str:
    """Summarize an API-handler result as `METHOD endpoint -> status`, never the full body."""
    if isinstance(result, dict) and "response" in result:
        req = result.get("request") or {}
        resp = result.get("response") or {}
        url = str(req.get("url", ""))
        endpoint = url.split("/api/", 1)[-1] if "/api/" in url else url
        status = resp.get("status_code", "?")
        tail = "" if resp.get("success") else " FAIL"
        return f"{req.get('method', '')} {endpoint} -> {status}{tail}".strip()
    return _short(result)


def api_call(func):
    """
    Decorator to wrap API methods for logging entry, exit, and error handling.

    This decorator:
    1. Logs method entry with arguments
    2. Executes the method and catches any exceptions
    3. Logs method exit with return value or error details
    4. Re-raises any exceptions for proper error handling

    Args:
        func: The function to be decorated

    Returns:
        Wrapped function with logging and error handling
    """

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        # Get logger from instance or use module logger
        log = getattr(self, "logger", logger)
        func_name = f"{self.__class__.__name__}.{func.__name__}"

        # Log method entry — compact and bounded (never the full args/payload).
        log.debug(f"[ENTRY] {func_name} args={_short(args, 120)}")

        try:
            # Execute the method
            result = func(self, *args, **kwargs)

            # Log successful exit — a summary (METHOD endpoint -> status), never the full body.
            log.debug(f"[EXIT] {func_name} | {_result_summary(result)}")
            return result

        except Exception as e:
            # Log error with traceback
            log.error(f"[ERROR] {func_name} raised: {e}")
            log.debug(traceback.format_exc())

            # Re-raise the exception
            raise

    return wrapper


def performance_monitor(func):
    """
    Decorator to monitor API call performance.

    Logs the execution time of API calls to help identify performance issues.
    """

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        log = getattr(self, "logger", logger)
        func_name = f"{self.__class__.__name__}.{func.__name__}"

        start_time = time.time()
        try:
            result = func(self, *args, **kwargs)
            duration = time.time() - start_time
            # Routine timing is DEBUG noise; only a genuinely slow call is worth INFO/WARNING.
            if duration >= SLOW_CALL_S:
                log.warning(f"[PERF] {func_name} slow: {duration:.3f}s")
            else:
                log.debug(f"[PERF] {func_name} {duration:.3f}s")
            return result
        except Exception:
            duration = time.time() - start_time
            log.warning(f"[PERF] {func_name} failed after {duration:.3f}s")
            raise

    return wrapper
