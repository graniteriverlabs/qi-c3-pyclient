# client/modules/remote_capture.py
"""
Fetch the application's report folders over HTTP, so a run saves the same files wherever the package runs.

The package used to copy the run's report folder, and an exerciser session's capture folder, straight off
the bench PC's disk. That only works on the PC the application runs on. Builds with API mode serve both
folders instead (``api/RemoteCapture``):

    GET RemoteCapture/GetCurrentReportFolder   -> {reportRoot, projectFolder, runFolder, runFinished} of the
                                                  current or last run, worked out by the application itself;
                                                  runFinished is true once its report files are all written
    GET RemoteCapture/GetReportFolderZip/<name> -> a folder directly under the report root, as one ZIP whose
                                                  entries all start with "<name>/"

``fetch_folder`` downloads one such folder and unpacks it so that ``<parent>/<name>/`` holds exactly what
the application's folder holds - the same place and the same files the disk copy produced. It is used on
the bench PC too (one method), and a build without these routes (404) is reported as such so the caller
can fall back to the disk copy. Nothing here raises: every outcome is a dict the caller records.
"""
import os
import re
import shutil
import tempfile
import time
import zipfile
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

CURRENT_FOLDER = "RemoteCapture/GetCurrentReportFolder"
FOLDER_ZIP = "RemoteCapture/GetReportFolderZip/{name}"
#: Connect fast; allow a slow read of a large report folder.
TIMEOUT = (10, 300)
CHUNK = 1 << 20
#: How long to wait, and how often to ask, for the application to finish writing a run's report.
WRITTEN_WAIT_S = 120.0
WRITTEN_POLL_S = 0.5


def _base_and_session(api_handler) -> Tuple[Optional[str], Any]:
    url = getattr(api_handler, "base_url", None)
    session = getattr(api_handler, "session", None)
    if session is None:
        import requests
        session = requests.Session()
    return (url.rstrip("/") if isinstance(url, str) else None), session


def folder_name(path: Any) -> Optional[str]:
    """The last part of a path the application gave, split on both separators (it is a Windows path
    even when the package runs on macOS, where os.path would not split on a backslash)."""
    if not isinstance(path, str) or not path.strip():
        return None
    parts = [p for p in re.split(r"[\\/]", path.strip()) if p]
    return parts[-1] if parts else None


def current_report_folder(api_handler) -> Dict[str, Any]:
    """
    {"available": bool, "status": int|None, "projectFolder": str|None, "reportRoot": str|None, "error": str}
    "available" is False only when the build has no such route (404) - then the disk copy is the way.
    """
    base, session = _base_and_session(api_handler)
    if not base:
        return {"available": False, "status": None, "error": "no application address"}
    try:
        r = session.get(f"{base}/{CURRENT_FOLDER}", timeout=60)
    except Exception as e:                                        # the application did not answer
        return {"available": True, "status": None, "error": f"no answer: {e}"}
    if r.status_code == 404:
        return {"available": False, "status": 404, "error": "this build has no RemoteCapture routes"}
    if r.status_code != 200:
        return {"available": True, "status": r.status_code, "error": f"HTTP {r.status_code}"}
    try:
        body = r.json() or {}
    except ValueError:
        return {"available": True, "status": 200, "error": "the answer was not JSON"}
    return {"available": True, "status": 200, "projectFolder": body.get("projectFolder"),
            "reportRoot": body.get("reportRoot"), "runFolder": body.get("runFolder"),
            "runFinished": body.get("runFinished"), "error": ""}


def report_folder_when_written(api_handler, timeout: Optional[float] = None,
                               poll: Optional[float] = None, logger=None) -> Dict[str, Any]:
    """
    current_report_folder(), once the application says the run's report files are all written.

    After a run is stopped - control taken back at the bench - the application reports READY at once,
    while its test task is still writing the last case's files and the run report; that task marks the
    run finished (runFinished) as its very last step. So the folder is collected only after that. A build
    that does not send runFinished is answered at once, as before. Waits at most ``timeout`` seconds, then
    returns what the application last said, with "waited_out" set.
    """
    timeout = WRITTEN_WAIT_S if timeout is None else timeout
    poll = WRITTEN_POLL_S if poll is None else poll
    deadline = time.time() + timeout
    where = current_report_folder(api_handler)
    waited = False
    while where.get("available") and where.get("runFinished") is False:
        if time.time() >= deadline:
            where["waited_out"] = True
            if logger:
                logger.warning(f"[capture] the application still says the run's report is being "
                               f"written after {timeout:.0f} s; collecting what there is")
            return where
        if not waited and logger:
            logger.info("[capture] waiting for the application to finish writing the run's report")
        waited = True
        time.sleep(poll)
        where = current_report_folder(api_handler)
    if waited:
        where["waited"] = True
    return where


def fetch_folder(api_handler, name: str, parent: str, logger=None) -> Dict[str, Any]:
    """
    Download the application's report-root folder ``name`` and unpack it as ``parent/name``.

    Returns {"fetched": bool, "status", "target", "file_count", "bytes", "skipped", "error"}.
    "status" 404 with error "no route" means the build cannot serve folders (fall back to the disk copy).
    """
    result: Dict[str, Any] = {"fetched": False, "status": None, "target": None, "file_count": 0,
                              "bytes": 0, "skipped": 0, "error": ""}
    base, session = _base_and_session(api_handler)
    if not base or not name:
        result["error"] = "no application address" if not base else "no folder name"
        return result
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="grl_capture_", suffix=".zip", dir=parent)
    os.close(fd)
    try:
        url = f"{base}/{FOLDER_ZIP.format(name=quote(name, safe=''))}"
        with session.get(url, stream=True, timeout=TIMEOUT) as r:
            result["status"] = r.status_code
            if r.status_code != 200:
                detail = ""
                try:
                    body = r.json()
                    detail = body.get("message") or body.get("error") or ""
                except Exception:
                    pass
                if r.status_code == 404 and not detail:
                    detail = "no route"
                result["error"] = f"HTTP {r.status_code}" + (f": {detail}" if detail else "")
                return result
            try:
                result["skipped"] = int(r.headers.get("X-Skipped-Files", "0") or 0)
            except ValueError:
                result["skipped"] = 0
            with open(tmp, "wb") as out:
                for chunk in r.iter_content(CHUNK):
                    if chunk:
                        out.write(chunk)
        result.update(_unpack(tmp, name, parent))
        result["fetched"] = not result["error"]
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _unpack(zip_path: str, name: str, parent: str) -> Dict[str, Any]:
    """Unpack a folder ZIP into parent/name, refusing any entry that would land outside it."""
    target = os.path.realpath(os.path.join(parent, name))
    out: Dict[str, Any] = {"target": target, "file_count": 0, "bytes": 0, "error": ""}
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            prefix = name + "/"
            for member in members:
                if not member.filename.replace("\\", "/").startswith(prefix):
                    out["error"] = f"the ZIP holds {member.filename!r}, outside '{name}/'"
                    return out
                dest = os.path.realpath(os.path.join(parent, *member.filename.replace("\\", "/").split("/")))
                if dest != target and not dest.startswith(target + os.sep):
                    out["error"] = f"the ZIP entry {member.filename!r} would land outside the capture"
                    return out
            os.makedirs(target, exist_ok=True)
            for member in members:
                dest = os.path.realpath(os.path.join(parent, *member.filename.replace("\\", "/").split("/")))
                if member.is_dir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with archive.open(member) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                try:   # keep the application's write time, as the disk copy did
                    stamp = time.mktime(member.date_time + (0, 0, -1))
                    os.utime(dest, (stamp, stamp))
                except (OverflowError, ValueError, OSError):
                    pass
                out["file_count"] += 1
                out["bytes"] += member.file_size
    except zipfile.BadZipFile as e:
        out["error"] = f"the download is not a ZIP: {e}"
    return out
