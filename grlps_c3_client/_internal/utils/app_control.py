# utils/app_control.py
"""
Where the GRL application is, and whether this run opens and closes it.

Two settings, read from the configuration file (``common``, or per application under
``applications.<app>``, which wins), or passed by a script to ``GRLApiClient``:

=====================  =======================================================================
open_and_close_app     true (the default): this run opens the application on this PC when it
                       is not already running, and closes it at the end.
                       false: this run uses the application that is already open - on this PC
                       or on the PC named by app_host - and leaves it open at the end.
app_host               the PC the application runs on: "localhost" (the default) or another
                       PC's address, such as 192.0.2.20. The port is the application's own
                       ``known_port``.
=====================  =======================================================================

Opening and closing the application is only possible on the Windows PC it is installed on, so
``true`` is refused on any other system (macOS) and with an ``app_host`` that is another PC. Every
refusal says which setting, what it holds, where it was set and exactly what to change. Nothing
here starts or stops anything.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import sys
from dataclasses import dataclass
from typing import Any, Optional, Tuple

FLAG = "open_and_close_app"
HOST = "app_host"
DEFAULT_HOST = "localhost"

#: How long to wait for the application's port to answer before saying it is not reachable.
REACH_TIMEOUT_S = 3.0

_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}
_HOST_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_SYSTEMS = {"darwin": "macOS", "linux": "Linux"}


@dataclass(frozen=True)
class AppControl:
    """How this run reaches the application. ``problems`` is empty when the settings can be used."""

    app: str
    open_and_close: bool
    host: str
    port: Optional[int]
    on_this_pc: bool
    flag_source: str
    host_source: str
    config_file: str = ""
    problems: Tuple[str, ...] = ()

    @property
    def base_url(self) -> str:
        host = "[{0}]".format(self.host) if ":" in self.host else self.host
        return "http://{0}:{1}".format(host, self.port)

    @property
    def where(self) -> str:
        """"this PC" or the other PC's address, for messages."""
        return "this PC" if self.on_this_pc else self.host

    def describe(self) -> str:
        """The one line a command prints before it starts."""
        if self.open_and_close:
            return "this PC, port {0} - opened if needed, closed at the end".format(self.port)
        return "already open on {0}, port {1} - left open at the end".format(self.where, self.port)


def system_name(platform: Optional[str] = None) -> str:
    platform = platform or sys.platform
    return "Windows" if platform.startswith("win") else _SYSTEMS.get(platform, platform)


def parse_flag(value: Any) -> Optional[bool]:
    """true/false as JSON booleans or as the strings "true"/"false" (any case). None otherwise."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def host_problem(value: Any) -> str:
    """Why ``value`` cannot be used as app_host, or "" when it can."""
    if not isinstance(value, str):
        return "it is not text - put the address in quotes"
    if not value.strip():
        return "it is empty"
    text = value.strip()
    if "://" in text:
        return "it is a URL - give only the PC's address, without http:// and without a port"
    if "/" in text or "\\" in text:
        return "it contains a path - give only the PC's address"
    try:
        ipaddress.ip_address(text)
        return ""
    except ValueError:
        pass
    if ":" in text:
        return "it contains a port - give only the PC's address; the port is the " \
               "application's own known_port"
    if not _HOST_NAME.match(text):
        return "it is not an IP address or a computer name"
    return ""


def is_this_pc(host: str) -> bool:
    """True when ``host`` names the PC this code runs on."""
    text = host.strip().lower()
    if text in _LOCAL_NAMES or text.startswith("127."):
        return True
    try:
        name = socket.gethostname()
    except OSError:
        return False
    if text in (name.lower(), name.lower().split(".")[0]):
        return True
    try:
        mine = {info[4][0] for info in socket.getaddrinfo(name, None)}
    except OSError:
        mine = set()
    return text in {address.lower() for address in mine}


def _value(script: Any, app_settings: dict, common: dict, key: str, app: str,
           config_file: str) -> Tuple[Any, str, bool]:
    """(value, where it came from, was it given at all) - the script first, then the app, then common."""
    if script is not None:
        return script, "passed by the script", True
    if key in app_settings:
        return app_settings[key], "set by applications.{0}.{1} in {2}".format(app, key, config_file), True
    if key in common:
        return common[key], "set by common.{0} in {1}".format(key, config_file), True
    default = "true" if key == FLAG else '"{0}"'.format(DEFAULT_HOST)
    return None, "not set in {0}, so it is {1} - the default".format(config_file, default), False


def resolve(app: str, app_settings: Optional[dict], common: Optional[dict], config_file: str,
            port: Optional[int], script_flag: Any = None, script_host: Any = None,
            platform: Optional[str] = None) -> AppControl:
    """Read both settings, check them together, and say what is wrong in full."""
    app_settings = app_settings or {}
    common = common or {}
    platform = platform or sys.platform
    problems = []

    raw_flag, flag_source, _ = _value(script_flag, app_settings, common, FLAG, app, config_file)
    flag = True if raw_flag is None else parse_flag(raw_flag)
    if flag is None:
        problems.append(
            "{0} is {1!r} - it must be true or false.\n"
            "  true   this run opens {2} on this PC if it is not running, and closes it at the end\n"
            "  false  this run uses {2} that is already open (on this PC or on app_host) and "
            "leaves it open\n"
            "It is {3}.".format(FLAG, raw_flag, app, flag_source))

    raw_host, host_source, _ = _value(script_host, app_settings, common, HOST, app, config_file)
    host = DEFAULT_HOST if raw_host is None else (raw_host.strip() if isinstance(raw_host, str) else raw_host)
    why = host_problem(host)
    if why:
        problems.append(
            "{0} is {1!r} and cannot be used: {2}.\n"
            "Give the address of the PC the application runs on, for example \"localhost\", "
            "\"192.0.2.20\" or \"BENCH-PC-01\".\n"
            "It is {3}.".format(HOST, raw_host, why, host_source))
        host = DEFAULT_HOST
    on_this_pc = is_this_pc(host)

    if not port:
        problems.append("applications.{0}.known_port is not set in {1} - it is the port {0} "
                        "listens on.".format(app, config_file))

    if flag and not problems:
        if not platform.startswith("win"):
            source = "{0} is {1}.".format(FLAG, flag_source)
            problems.append(
                "{0} is true, but this computer runs {1}. "
                "The package can open and close the application only on the Windows PC it is installed on.\n\n"
                "To run from this computer:\n"
                "  1. Open {2} on the Windows PC.\n"
                "  2. Set these in {3}:\n"
                "       \"{0}\": false,\n"
                "       \"{4}\": \"<the Windows PC's IP address>\"\n\n"
                "{5}".format(FLAG, system_name(platform), app, config_file, HOST, source))
        elif not on_this_pc:
            problems.append(
                "{0} is true, but {1} is {2!r} - another PC. This run can open and close the "
                "application only on the PC it runs on.\n\n"
                "Either:\n"
                "  - set \"{1}\": \"localhost\" to open and close {3} on this PC, or\n"
                "  - open {3} on {2} yourself and set \"{0}\": false.\n\n"
                "{0} is {4}.\n{1} is {5}.".format(FLAG, HOST, host, app, flag_source, host_source))

    return AppControl(app=app, open_and_close=bool(flag), host=host, port=port,
                      on_this_pc=on_this_pc, flag_source=flag_source, host_source=host_source,
                      config_file=config_file, problems=tuple(problems))


def resolve_from(config_manager: Any, platform: Optional[str] = None) -> AppControl:
    """``resolve`` for the application a ``GRLConfigManager`` has selected."""
    config = getattr(config_manager, "config", {}) or {}
    name = getattr(config_manager, "app_name", None)
    key = getattr(config_manager, "selected_app_key", None) or name
    return resolve(
        app=name or key or "?",
        app_settings=(config.get("applications") or {}).get(key) or {},
        common=config.get("common") or {},
        config_file=os.path.abspath(getattr(config_manager, "config_file_path", "grl_config.json")),
        port=getattr(config_manager, "known_port", None),
        script_flag=getattr(config_manager, "open_and_close_override", None),
        script_host=getattr(config_manager, "app_host_override", None),
        platform=platform)


# --------------------------------------------------------------------------- reachability
def unreachable_reason(host: str, port: int, timeout: float = REACH_TIMEOUT_S) -> str:
    """Why nothing answers on host:port, in plain words, or "" when something does."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ""
    except socket.gaierror:
        return "the name {0!r} could not be found on the network".format(host)
    except ConnectionRefusedError:
        return "the connection was refused - nothing is listening on port {0} there".format(port)
    except socket.timeout:
        return "no answer within {0:g} s".format(timeout)
    except OSError as exc:
        return exc.strerror or str(exc)


def unreachable_message(control: AppControl, reason: str) -> str:
    """The full explanation when open_and_close_app is false and the application cannot be reached."""
    config_file = control.config_file
    if control.on_this_pc:
        return (
            "{0} is not running on this PC - nothing answers at {1} ({2}).\n\n"
            "{3} is false, so this run uses an application that is already open and does not "
            "start one. Either:\n"
            "  - open {0} first, or\n"
            "  - set \"{3}\": true in {4} to let this run open it and close it at the end.\n\n"
            "{3} is {5}.".format(control.app, control.base_url, reason, FLAG, config_file,
                                 control.flag_source))
    return (
        "{0} cannot be reached at {1} - {2}.\n\n"
        "{3} is false, so this run uses the application that is already open on {4}.\n"
        "Check:\n"
        "  1. {0} is open on {4}.\n"
        "  2. Its port is {5} (applications.{0}.known_port in {6}).\n"
        "  3. This computer can reach {4}: same network, and \"ping {4}\" answers.\n"
        "  4. The firewall on {4} lets TCP port {5} in.\n\n"
        "{7} is {8}.".format(control.app, control.base_url, reason, FLAG, control.host,
                             control.port, config_file, HOST, control.host_source))


def not_grl_message(control: AppControl, error: Any) -> str:
    """Something answers on the port, but it is not the GRL application."""
    config_file = control.config_file
    return (
        "Something answers at {0}, but it is not {1}: asking it for its software version "
        "failed ({2}).\n\n"
        "Check that port {3} (applications.{1}.known_port in {4}) is the port of {1} on {5}, "
        "and that {1} has finished starting.".format(control.base_url, control.app, error,
                                                     control.port, config_file, control.where))
