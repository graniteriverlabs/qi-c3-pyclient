# client/transport/ui_popup.py
"""
Answer an operator pop-up by driving the app's own desktop UI.

On a WebSocket build the C3 app runs its Electron desktop UI, and the backend accepts a
pop-up answer **only from that UI's own socket** — an external client's WebSocket reply
(byte-identical though it is) and the REST message-box are both ignored, because on a WS
build the pop-up is a front-end/DSUI pop-up, not a backend message box. So the correct,
build-faithful way for automation to answer it is to press the pop-up's button in the
Electron window, exactly as an operator would.

This module finds the C3 Electron window and invokes the pop-up button (default "OK"),
falling back to sending the window its default-button keystroke (Enter). It is Windows-only
and used only on WebSocket builds; REST builds answer over REST and never call this.
"""
from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes
from typing import List, Optional

# Chromium/Electron renders its UI (and pop-up buttons) in a GPU/renderer child window and
# keeps its accessibility tree OFF until an assistive-tech client asks for it. Sending
# WM_GETOBJECT with UiaRootObjectId to the window (and its children) is that request — it
# makes Chromium expose the DOM to UI Automation so the "OK" button becomes findable.
_WM_GETOBJECT = 0x003D
_UIA_ROOT_OBJECT_ID = -25  # UiaRootObjectId, passed as lParam

# The Electron UI executables per product (TPT / MPP-TPR). Matched case-insensitively.
_APP_PROCESS_RE = re.compile(r"GRL-C3-.*\.exe$", re.IGNORECASE)
# Window titles that belong to the C3 app (and must NOT match unrelated Chromium apps).
_APP_TITLE_RE = re.compile(r"GRL[-\s]?C3|Test Solution", re.IGNORECASE)
_EXCLUDE_TITLE_RE = re.compile(r"Visual Studio Code|Chrome|Edge|Slack", re.IGNORECASE)


def available() -> bool:
    """True if the UI-automation backend can be used on this machine."""
    try:
        import pywinauto  # noqa: F401
        return True
    except Exception:
        return False


def _process_name(pid: int) -> str:
    """Best-effort image name for a pid, without a psutil dependency."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        k32.CloseHandle(h)


def _enable_chromium_accessibility(hwnd: int) -> None:
    """Ask Chromium to expose its accessibility tree for this window and its render children,
    so UI Automation can see the pop-up's HTML button."""
    u = ctypes.windll.user32
    targets = [hwnd]

    EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _collect(child, _):
        targets.append(child)
        return True

    try:
        u.EnumChildWindows(hwnd, EnumChildProc(_collect), 0)
    except Exception:
        pass
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_ulong(0)
    for h in targets:
        try:
            # Timeout-bounded so a busy Electron window can never stall our thread.
            u.SendMessageTimeoutW(h, _WM_GETOBJECT, 0, _UIA_ROOT_OBJECT_ID & 0xFFFFFFFF,
                                  SMTO_ABORTIFHUNG, 500, ctypes.byref(result))
        except Exception:
            pass


def _candidate_windows(logger=None):
    """C3 Electron top-level windows, most-recently-active first."""
    from pywinauto import Desktop

    wins = []
    for w in Desktop(backend="uia").windows():
        try:
            title = w.window_text() or ""
            if _EXCLUDE_TITLE_RE.search(title):
                continue
            pid = w.process_id()
            proc = _process_name(pid)
            if _APP_PROCESS_RE.search(proc) or _APP_TITLE_RE.search(title):
                wins.append(w)
        except Exception:
            continue
    return wins


def click_popup_button(button: str = "OK", logger=None) -> bool:
    """
    Press `button` (default "OK") in the C3 Electron pop-up. Returns True if a button was
    invoked (or the Enter fallback was sent to a matching window), False if no C3 window /
    button could be found.
    """
    if not available():
        if logger:
            logger.debug("[ui-popup] pywinauto not available; cannot drive the UI")
        return False

    from pywinauto.findwindows import ElementNotFoundError  # noqa: F401

    windows = _candidate_windows(logger)
    if not windows:
        if logger:
            logger.debug("[ui-popup] no C3 Electron window found")
        return False

    want = button.strip().upper()
    # Force Chromium to expose its accessibility tree, else the HTML "OK" button is invisible
    # to UI Automation and we'd be stuck on the (unreliable) Enter fallback.
    for w in windows:
        try:
            _enable_chromium_accessibility(w.handle)
        except Exception:
            pass
    time.sleep(0.4)

    for w in windows:
        # 1) REAL mouse click on the matching button — NOT invoke().
        #    UIA invoke() fires the accessibility "Invoke" pattern, which Chromium/Electron
        #    HTML buttons frequently do NOT map to their JavaScript onclick handler. The result
        #    is that invoke() reports success (and may even close the dialog visually) while the
        #    answer is never delivered to the backend — the run then stalls forever waiting on
        #    the pop-up. A real click delivers the actual mouse event the web handler listens for.
        try:
            for b in w.descendants(control_type="Button"):
                label = (b.window_text() or "").strip()
                if label.upper() == want or (want == "OK" and label.upper() in ("OK", "OKAY")):
                    b.click_input()          # real click -> fires the button's JS handler
                    if logger:
                        logger.info(f"[ui-popup] clicked '{label}' (real click) in {w.window_text()!r}")
                    return True
        except Exception as e:
            if logger:
                logger.debug(f"[ui-popup] button click failed on {w.window_text()!r}: {e}")

    # 2) Fallback: press the pop-up's default button with a real keystroke. Enter DOES trigger
    #    the default button's JS handler (proven to complete full runs end to end), so it's the
    #    reliable path when the specific button isn't exposed in the accessibility tree.
    for w in windows:
        try:
            w.set_focus()
            w.type_keys("{ENTER}", set_foreground=True)
            if logger:
                logger.info(f"[ui-popup] sent Enter (default button) to {w.window_text()!r}")
            return True
        except Exception as e:
            if logger:
                logger.debug(f"[ui-popup] Enter fallback failed on {w.window_text()!r}: {e}")

    return False
