# utils/web_app_controller.py
"""
Web application controller for GRL API.
Handles launching, monitoring, and interacting with the GRL application.
"""

import os
import time
import logging
import subprocess
import requests
import socket
from typing import Optional


#: Windows creation flags, resolved defensively so this module still imports on other platforms.
#:
#: **Why the app must not share our console.** `Popen([app_path])` gives the child our standard
#: handles AND puts it in our console process group. Two consequences, both seen at the bench on
#: 2026-08-17:
#:   * Keystrokes go into a console input buffer BOTH processes are attached to, so the app can
#:     swallow the Enter meant for us. The exerciser's hold phase waits on Enter, and it never
#:     arrived — the session could only be ended with Ctrl+C.
#:   * Ctrl+C is not a keystroke but a console CONTROL EVENT, broadcast to every process in the
#:     group. So it reached the GRL app too. We then asked an app that had just been told to quit
#:     to finish `GetStopExerciser`; it never answered (29.5 s, no status), the app never wrote the
#:     session's capture folder, and the exerciser was left running on the tester.
#:
#: `DETACHED_PROCESS` gives the child no console at all — the complete fix, since a console-attached
#: process can read `CONIN$` directly whatever its stdin handle is. `CREATE_NEW_PROCESS_GROUP`
#: keeps our Ctrl+C out of it. Neither affects `TerminateProcess`, so shutdown is unchanged, and
#: neither affects a GUI app's window.
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


class WebAppController:
    """
    Controller for launching and interacting with web applications.
    Manages the application process and connectivity.
    """

    def __init__(self, app_path: str, known_port: Optional[int] = None,
                 max_connection_attempts: int = 3, connection_timeout: int = 30):
        self.app_path = app_path
        self.known_port = known_port
        self.max_connection_attempts = max_connection_attempts
        self.connection_timeout = connection_timeout

        self.process = None
        self.web_url = f"http://localhost:{self.known_port}" if self.known_port else None

        self.logger = logging.getLogger("WebAppController")
        self.logger.addHandler(logging.NullHandler())

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.logger.debug("Logger set for WebAppController")

    def _check_port_in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("localhost", port))
                return False
            except socket.error:
                return True

    def _check_application_running(self) -> bool:
        if not self.known_port:
            self.logger.warning("Cannot check application status without a known port.")
            return False

        if not self._check_port_in_use(self.known_port):
            self.logger.debug(f"Port {self.known_port} is free.")
            return False

        endpoints = ["/api/healthcheck", "/", "/api/status"]
        for endpoint in endpoints:
            try:
                response = requests.get(f"{self.web_url}{endpoint}", timeout=2)
                self.logger.info(f"Application detected on port {self.known_port} (status {response.status_code} at {endpoint})")
                return True
            except requests.exceptions.RequestException:
                continue

        self.logger.debug(f"Port {self.known_port} occupied but no valid application detected.")
        return False

    def _spawn(self) -> subprocess.Popen:
        """
        Start the app **detached from our console**, so it cannot take our keyboard input and our
        Ctrl+C cannot reach it. See `_DETACHED_PROCESS` above for what that cost us at the bench.

        `stdin=DEVNULL` on every platform; on Windows also no console and its own process group.
        If those flags are rejected for any reason the app is launched the old way rather than not
        at all — a degraded launch beats a blocked bench session — but it says so loudly, because
        the exerciser's Enter-to-stop is unreliable in that state.
        """
        kwargs = {"stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen([self.app_path], **kwargs)
            self.logger.debug("Application launched detached from this console "
                              "(its own process group, no shared stdin)")
            return process
        except (ValueError, OSError) as e:
            self.logger.warning(f"Could not launch the application detached from this console "
                                f"({e!r}); falling back to a shared console. The app may consume "
                                f"keystrokes meant for this window, and Ctrl+C will also reach it "
                                f"- so prefer Enter to stop an exerciser session, and expect the "
                                f"capture folder to be at risk if you do use Ctrl+C.")
            return subprocess.Popen([self.app_path])

    def _launch_process(self) -> bool:
        if self._check_application_running():
            self.logger.info("Application is already running.")
            return True

        if self.process and self.process.poll() is None:
            self.logger.info("Process already active.")
            return True

        if not os.path.exists(self.app_path):
            self.logger.error(f"Application path not found: {self.app_path}")
            return False

        try:
            self.logger.debug(f"Launching application: {self.app_path}")
            self.process = self._spawn()

            if self.process.poll() is None:
                self.logger.info(f"Successfully launched {os.path.basename(self.app_path)} (PID {self.process.pid})")
                return True
            else:
                self.logger.error(f"Process terminated immediately (exit code {self.process.returncode})")
                return False

        except Exception as e:
            self.logger.error(f"Failed to launch application: {repr(e)}")
            return False

    def start_and_get_url(self, initial_wait: int = 10) -> Optional[str]:
        if self._check_application_running():
            self.logger.info(f"Using existing application at {self.web_url}")
            return self.web_url

        if not self._launch_process():
            return None

        self.logger.info(f"Waiting {initial_wait} seconds for initialization...")
        time.sleep(initial_wait)

        if not self.web_url:
            self.logger.error("Known port not specified. Cannot determine web URL.")
            return None

        self.logger.info(f"Attempting to connect to web server at {self.web_url} (timeout {self.connection_timeout}s)")
        start_time = time.time()

        endpoints = ["/api/healthcheck", "/", "/api/status"]

        for attempt in range(1, self.max_connection_attempts + 1):
            if time.time() - start_time > self.connection_timeout:
                self.logger.error(f"Connection timeout after {self.connection_timeout} seconds.")
                return None

            self.logger.debug(f"Attempt {attempt}/{self.max_connection_attempts} to {self.web_url}")

            for endpoint in endpoints:
                try:
                    response = requests.get(f"{self.web_url}{endpoint}", timeout=5)
                    self.logger.info(f"Connected to {self.web_url} (status {response.status_code} at {endpoint})")
                    return self.web_url
                except requests.exceptions.RequestException:
                    continue

            time.sleep(2)

        self.logger.error("Failed to connect to the web server after multiple attempts.")
        return None

    def _process_exited(self) -> bool:
        """
        True if the launched process is known to have exited.

        On Windows, poll() itself can raise once the process handle is invalid (which also
        means the process is gone), so treat that as exited too.
        """
        if not self.process:
            return True
        try:
            return self.process.poll() is not None
        except OSError:
            return True

    def stop_process(self) -> bool:
        # Already gone (e.g. the WebSocket build's Electron app quits on its own via
        # window-all-closed -> app.quit(), or a previous stop already ran). Nothing to do.
        if self._process_exited():
            self.logger.debug("No active process to stop (already exited).")
            return True

        self.logger.info("Stopping application process...")
        try:
            self.process.terminate()

            for _ in range(5):
                if self._process_exited():
                    self.logger.info("Process terminated.")
                    return True
                time.sleep(1)

            self.logger.warning("Force killing unresponsive process...")
            self.process.kill()
            self.process.wait(timeout=5)
            self.logger.info("Process killed successfully.")
            return True

        except OSError as e:
            # "The handle is invalid" (errno 9 / WinError 6) means the process handle is no longer
            # usable — the app released/closed it or exited on its own, and poll() can still claim
            # it is running, so we key off the error rather than poll().
            #
            # Do NOT simply assume it stopped: ask the port whether the app is still serving. A
            # stale handle and a still-running app look identical from here, and the difference
            # matters — a surviving app keeps the tester driving a DUT.
            handle_gone = e.errno == 9 or getattr(e, "winerror", None) == 6
            if handle_gone or self._process_exited():
                if self._check_application_running():
                    self.logger.warning(
                        "Application process handle is no longer manageable "
                        f"({e.__class__.__name__}: {e.strerror or e}) AND the app is still serving "
                        f"on port {self.known_port}. It was NOT stopped - close it from its own "
                        f"window, and check the tester is not left running.")
                    return False
                self.logger.info("Application process handle no longer manageable at shutdown "
                                 f"({e.__class__.__name__}: {e.strerror or e}); confirmed stopped "
                                 f"- the app is no longer serving.")
                return True
            self.logger.error(f"Error stopping process: {repr(e)}")
            return False
        except Exception as e:
            self.logger.error(f"Error stopping process: {repr(e)}")
            return False

    def is_running(self) -> bool:
        return (self.process and self.process.poll() is None) or self._check_application_running()

    def __del__(self):
        try:
            self.stop_process()
        except Exception:
            pass
