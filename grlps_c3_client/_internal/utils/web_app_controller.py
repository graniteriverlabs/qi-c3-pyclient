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
        """
        True when something is already serving on this port.

        Tested by CONNECTING, not by binding. Binding to ``("localhost", port)`` SUCCEEDS on
        Windows while the application holds ``0.0.0.0:port`` - Windows permits that unless the
        owner asked for SO_EXCLUSIVEADDRUSE - so a running application was reported as a free
        port. Everything downstream followed from that: the client never recognised an
        application that was already up, launched a second instance that died with "Failed to
        bind to address http://[::]:<port>: address already in use" while the first one kept
        driving the tester, and had no handle on the one actually serving, so nothing closed it.

        A connect probe is unambiguous and does not care which interface the server bound to.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            try:
                return s.connect_ex(("127.0.0.1", port)) == 0
            except socket.error:
                return False

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

    def _kill_process_tree(self) -> bool:
        """
        End the process tree we launched, by PID. True when the command ran.

        `Popen.kill()` ends one process; these applications run as several, and the children
        outlive the parent. ``taskkill /T`` takes the tree with it. Standard library only -
        nothing is added to what a customer has to install - and keyed on OUR pid, so an
        application somebody else started is never a candidate.
        """
        pid = getattr(self.process, "pid", None)
        if not pid:
            return False
        try:
            done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                  capture_output=True, text=True, timeout=20)
            if done.returncode == 0:
                self.logger.info(f"Ended the application process tree (PID {pid} and children).")
                return True
            self.logger.debug(
                f"taskkill on PID {pid} returned {done.returncode}: "
                f"{(done.stderr or done.stdout or '').strip()}")
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.debug(f"Could not end the process tree for PID {pid}: {exc}")
        return False

    def _warn_if_app_processes_remain(self) -> None:
        """
        Say so when processes of this application are still running after we stopped ours.

        Only ever reports. Killing by name would reach an application the operator opened
        themselves, which is not ours to close - but leaving them unmentioned is how they ended
        up being discovered run after run.
        """
        name = os.path.basename(self.app_path)
        try:
            done = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"],
                                  capture_output=True, text=True, timeout=20)
            count = sum(1 for line in (done.stdout or "").splitlines()
                        if name.lower() in line.lower())
            if count:
                self.logger.warning(
                    f"{count} {name} process(es) are still running. They are not from this "
                    f"client's process tree, so they were left alone - close them before the "
                    f"next run if they were not started deliberately.")
        except (OSError, subprocess.SubprocessError):
            pass

    def _pid_serving_known_port(self) -> Optional[int]:
        """
        The PID listening on this application's port, or None.

        Used to close the application this client drove when we have no handle on it, because we
        attached to one that was already running instead of starting it. The port is the
        application we were driving - unlike the image name, which would also match a copy the
        operator opened for something else. Standard library plus netstat, like the taskkill path.
        """
        if not self.known_port:
            return None
        try:
            done = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.debug(f"Could not read the port table: {exc}")
            return None

        for line in (done.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[-2].upper() != "LISTENING":
                continue
            local = parts[1]
            if local.rsplit(":", 1)[-1] != str(self.known_port):
                continue
            try:
                return int(parts[-1])
            except ValueError:
                continue
        return None

    def _stop_attached_application(self) -> bool:
        """
        Close the application we were driving but did not launch. True when it is gone.

        Without this, a session that attached to an already-running application left it running
        and still connected to the tester - every command launches or attaches, so chaining two
        of them had the second one take over a link the first had left open.

        Keyed on the PID serving our port, so this only ever ends the application this client was
        actually driving.
        """
        pid = self._pid_serving_known_port()
        if pid is None:
            return False

        self.logger.info(
            f"Closing the application this run was driving (PID {pid} on port "
            f"{self.known_port}); it was already running when this run started.")
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.warning(f"Could not close the application (PID {pid}): {exc}")
            return False

        for _ in range(10):
            if not self._check_port_in_use(self.known_port):
                self.logger.info("Application closed.")
                return True
            time.sleep(1)
        self.logger.warning(
            f"The application is still serving on port {self.known_port} after being asked to "
            f"close. Close it from its own window before the next run.")
        return False

    def stop_process(self) -> bool:
        # No handle of our own: either the Electron app quit on its own (WebSocket builds, via
        # window-all-closed -> app.quit()), a previous stop already ran, or this run attached to
        # an application that was already up. Only the last of those leaves anything behind, so
        # ask the port before deciding there is nothing to do.
        if self._process_exited():
            if not self._check_port_in_use(self.known_port):
                self.logger.debug("No active process to stop (already exited).")
                return True
            # Something is still serving our port, so the application this run drove is still
            # up. Report honestly whether it was closed: saying True here claimed "Application
            # stopped" while it kept running.
            return self._stop_attached_application()

        self.logger.info("Stopping application process...")
        try:
            self.process.terminate()

            for _ in range(5):
                if self._process_exited():
                    self.logger.info("Process terminated.")
                    return True
                time.sleep(1)

            self.logger.warning("Force killing unresponsive process...")
            # Kill the whole tree we started, not just the process we hold a handle to. The
            # WebSocket builds run as several processes, and `kill()` ends only the one we
            # spawned: five `GRL-C3-MP-TPR.exe` survived every session on the WebSocket build
            # and four on the TPT one, and had to be cleared by hand before the next run.
            # Scoped to our own process tree by PID, so an application the operator started
            # themselves is never touched.
            if not self._kill_process_tree():
                self.process.kill()
            self.process.wait(timeout=5)
            self.logger.info("Process killed successfully.")
            self._warn_if_app_processes_remain()
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
