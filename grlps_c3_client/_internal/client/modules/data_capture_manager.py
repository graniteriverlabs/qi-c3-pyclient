# client/modules/data_capture_manager.py
"""
Per-test-case measurement-data capture, packaged as one ZIP per run.

A manager in the same shape as ConnectionManager / TestManager / PopupManager: it is created
once, given the API handler, and driven by TestManager during a run. Because it lives in the
client, a normal `python sample_run.py` run captures automatically on both builds — nothing
extra to launch.

For each test case, at its completion, it saves (the set the earlier pipeline produced, plus
the WebSocket streams):

    Runtime_Capture/<app>/run_<ts>/
        <case>/  <case>.zip        report ZIP   (POST ReportsGeneration/PostCurrentTestReport)
                 packet_logs.json  packet log   (GET  Plot/GetCCLinePackets)
                 streams.json      WS streams   (WebSocket builds only; the frames for THIS case)
        run_summary.json
    Runtime_Capture/<app>/run_<ts>.zip

Source per build (by design): the report ZIP + packet log are REST on BOTH builds (served on
the same base URL); the WS streams come from the live transport's telemetry and exist only on
WebSocket builds. On a REST build there is no push, so streams.json is simply omitted and the
packet log carries the measurement data.

Robustness (plan D06): every step is isolated — a failure is logged at WARNING and recorded in
the run summary, but NEVER raises, so a capture problem can never abort the test run.
"""
import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

#: The live packet buffer, polled DURING a run so the stall watchdog can tell a slow case from
#: a hung one. Relative to the app's /api base URL (REST on both builds). Nothing is written from
#: it: the packet record that ships is the application's own trace file, inside its report folder.
_EP_PACKETS = "Plot/GetCCLinePackets"

#: The application's own report folders. It answers with its report root and one entry per run,
#: so the reports it writes can be collected without guessing where they are:
#:
#:   {"item2": {"reportRootFolderPath": "C:\\GRL\\GRL-C3-MP-TPR\\Report",
#:              "parentNode": [{"reportRootFolderName": "<run>", "reportRootFolderPath": "..."}]}}
_EP_REPORT_FOLDERS = "TestConfiguration/GetGProjReportFolders"

#: A run folder written up to this many seconds before capture started still belongs to the run.
#: The application creates its folder when the first case starts, which is after we begin, so this
#: is only slack for clock granularity - not a window wide enough to pick up the previous run.
_FOLDER_SLACK_SECONDS = 120

def _packet_count(data: Any) -> Optional[int]:
    """Best-effort packet count from a GetCCLinePackets payload (shape varies by build)."""
    try:
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for key in ("packets", "Packets", "data", "Data", "packetList", "PacketList"):
                v = data.get(key)
                if isinstance(v, list):
                    return len(v)
    except Exception:
        pass
    return None


class DataCaptureManager:
    """Captures each test case's data at completion and packages the run into one ZIP."""

    def __init__(self, logger, config_manager=None):
        self.logger = logger
        self.config_manager = config_manager
        self.api_handler = None

        self.run_dir: Optional[str] = None
        self.summary: Dict[str, Any] = {}
        self._active = False

        # Data-progress signal (WP 1.0), REST/packet half: a monotonic count of NEW Qi
        # packets observed across the run. It rises only when GetCCLinePackets returns more
        # packets than we last saw for the live case — i.e. genuine new measurement data.
        # Paired with the WS transport's stream-frame count, this is how the monitor loop
        # tells "tester still producing data" from "data has ceased".
        self._packet_progress = 0
        self._last_pkt_count: Dict[str, int] = {}   # per case: highest packet count seen

    def set_api_handler(self, api_handler):
        """Set the API handler (provides the base URL and shared HTTP session)."""
        self.api_handler = api_handler

    # -- HTTP plumbing (raw session; these endpoints are outside the enum config) ----------
    @property
    def base_url(self) -> Optional[str]:
        url = getattr(self.api_handler, "base_url", None)
        return url.rstrip("/") if isinstance(url, str) else None

    @property
    def _session(self):
        # Reuse the handler's session so we share cookies/keep-alive; fall back to a new one.
        s = getattr(self.api_handler, "session", None)
        if s is not None:
            return s
        import requests
        return requests.Session()

    # -- run lifecycle ---------------------------------------------------------------------
    def start_run(self, transport_name: str = "REST", run_root: Optional[str] = None) -> Optional[str]:
        """Begin a capture run: create the run directory and summary. Returns the run dir."""
        try:
            app = getattr(self.config_manager, "app_name", None) or "Unknown_App"
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            if run_root is None:
                from utils.project_root import project_root
                run_root = os.path.join(project_root(), "Runtime_Capture", app)
            self.run_dir = os.path.join(run_root, f"run_{ts}")
            os.makedirs(self.run_dir, exist_ok=True)
            self.summary = {
                "app": app, "transport": transport_name, "base_url": self.base_url,
                "started": ts, "cases": [],
            }
            self._packet_progress = 0
            self._last_pkt_count = {}
            self._active = True
            self._write_summary()
            self.logger.info(f"[capture] run started ({transport_name}) -> {self.run_dir}")
            return self.run_dir
        except Exception as e:
            self.logger.warning(f"[capture] could not start capture run: {e}")
            self._active = False
            return None

    def capture_case(self, case_name: str, telemetry: Optional[Dict] = None,
                     status: str = "FINISHED") -> Optional[Dict[str, Any]]:
        """
        Record that a case finished. **Writes no files of its own.**

        It used to build a folder per case holding the case's report archive, a packet log and a
        live-stream file. All of that was a second copy of what the application already writes:
        the archive downloaded here was byte-for-byte the application's own `Run1.zip` (checked,
        2026-10-08 - same SHA-256), and the packet and stream data are the application's
        `.grltrace` and per-case pages. The export is the application's report folder, collected
        once when the run finishes, so there is nothing for a per-case folder to add.

        What is kept is the RECORD - which cases ran, with what status and when. The run's own
        completion check reads it to tell a finished run from one that stopped early, and it is
        the client's account of what it did, which the application's report does not contain.

        Never raises: problems are recorded in the returned record and logged at WARNING.
        """
        if not self._active or not self.run_dir:
            return None
        rec: Dict[str, Any] = {
            "case": case_name, "status": status,
            "ts": datetime.now().strftime("%H:%M:%S"), "files": [], "errors": [],
        }
        try:
            # Live packet counts still matter while a run is in progress - they are the signal
            # the stall watchdog uses to tell a slow case from a hung one - so the count is
            # noted in the record. Nothing is written per case.
            packets = self._last_pkt_count.get(case_name)
            if packets:
                rec["packets_seen"] = packets
            if telemetry:
                rec["telemetry"] = {k: len(v) if isinstance(v, (list, dict)) else v
                                    for k, v in telemetry.items()}
        except Exception as e:                       # defensive — capture must never abort a run
            rec["errors"].append(f"capture_case: {e}")
            self.logger.warning(f"[capture] {case_name}: unexpected error: {e}")

        self.summary["cases"].append(rec)
        self._write_summary()
        self.logger.info(f"[capture] {case_name}: {status}"
                         + (f", {rec['packets_seen']} packet(s) seen" if "packets_seen" in rec
                            else ""))
        return rec

    def finalize_run(self) -> Optional[str]:
        """
        Collect the application's reports and write the summary. Returns the run directory.

        This used to also write the whole directory out again as `<run>.zip` beside it, and keep
        both. That made sense when the capture was this client's own files - a report archive per
        case, a packet log, a stream file - and the archive was the thing you sent on. Since the
        export became the application's own report folder, copied as the application wrote it, the
        archive held the same files with the same checksums: 13 files kept twice, 1.8 times the
        bytes, and nothing in the client, the documentation or either test suite read it. The
        folder is what the documentation points at and what a reader opens.

        Zipping a folder to send it is one action in the file manager, and does not need every run
        stored twice to make it possible.
        """
        if not self._active or not self.run_dir:
            return None
        self._active = False
        try:
            # The reports a reader actually opens are the ones the application writes in its own
            # location - the final report as PDF, HTML and JSON, the per-case pages and the
            # device description report. Collect those, or the export carries only the packet and
            # stream data this client assembles, which is not the result.
            self._capture_app_reports()
            self.summary["finished"] = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.summary["case_count"] = len(self.summary.get("cases", []))
            self._write_summary()
            self.logger.info(f"[capture] run collected -> {self.run_dir}")
            return self.run_dir
        except Exception as e:
            self.logger.warning(f"[capture] collecting the run failed: {e}")
            return None

    def _app_report_folders(self) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """
        The application's report root and its per-run folders, as it reports them.

        Asked of the application rather than derived from a path, so a site that has moved its
        report location is still collected correctly. Returns (root, folders).
        """
        if not self.base_url:
            return None, []
        try:
            r = self._session.get(f"{self.base_url}/{_EP_REPORT_FOLDERS}", timeout=60)
            if r.status_code != 200:
                self.logger.warning(
                    f"[capture] could not ask the application where its reports are "
                    f"(HTTP {r.status_code})")
                return None, []
            body = r.json()
        except Exception as e:
            self.logger.warning(f"[capture] could not read the application's report folders: {e}")
            return None, []

        inner = body.get("item2") if isinstance(body, dict) else None
        if not isinstance(inner, dict):
            return None, []
        nodes = inner.get("parentNode")
        return inner.get("reportRootFolderPath"), nodes if isinstance(nodes, list) else []

    def _capture_app_reports(self) -> None:
        """
        Copy the report folder this run produced into the capture, reports and all.

        Picks the run's folder by write time rather than by name: the application truncates the
        project name into its folder name (`live-verify_202_V231_081026_132349`), so the name
        cannot be reconstructed reliably, while the newest folder written after capture started
        is unambiguous - a run creates exactly one.

        Reporting only. A failure here never fails a run, but it is always said out loud: a
        silently missing report is the kind of thing nobody notices until it is needed.
        """
        if not self.run_dir:
            return
        started = self.summary.get("started")
        try:
            begin = datetime.strptime(started, "%Y%m%d-%H%M%S").timestamp()
        except (TypeError, ValueError):
            begin = 0.0

        root, nodes = self._app_report_folders()
        candidates = []
        for node in nodes:
            path = node.get("reportRootFolderPath") if isinstance(node, dict) else None
            if not path or not os.path.isdir(path):
                continue
            try:
                when = os.path.getmtime(path)
            except OSError:
                continue
            if when >= begin - _FOLDER_SLACK_SECONDS:
                candidates.append((when, path))

        if not candidates:
            self.summary["app_reports"] = {"captured": False,
                                           "reason": "the application listed no report folder "
                                                     "written during this run",
                                           "report_root": root}
            self.logger.warning(
                "[capture] the application wrote no report folder for this run, so no report "
                "was collected. Its report location is: {0}".format(root or "unknown"))
            return

        when, source = max(candidates)
        target = os.path.join(self.run_dir, "application_report",
                              os.path.basename(source.rstrip("\\/")))
        try:
            shutil.copytree(source, target, dirs_exist_ok=True)
            files = [os.path.relpath(os.path.join(r, f), self.run_dir)
                     for r, _d, fs in os.walk(target) for f in fs]
            size = sum(os.path.getsize(os.path.join(self.run_dir, f)) for f in files)
            self.summary["app_reports"] = {
                "captured": True, "source": source,
                "copied_to": os.path.relpath(target, self.run_dir),
                "file_count": len(files), "bytes": size,
                "reports": sorted(f for f in files if f.lower().endswith(
                    (".pdf", ".html", ".json"))),
            }
            self.logger.info(
                "[capture] collected the application's own report folder: {0} file(s), "
                "{1:,} bytes from {2}".format(len(files), size, source))
        except Exception as e:
            self.summary["app_reports"] = {"captured": False, "source": source,
                                           "reason": str(e)}
            self.logger.warning(
                f"[capture] could not copy the application's report folder {source}: {e}")

    # -- per-datum capture -----------------------------------------------------------------
    def _note_packet_count(self, case_name: str, count: int) -> None:
        """Advance the monotonic new-packet progress counter (WP 1.0 data signal).

        Rises only when this case's live buffer holds more packets than we last saw for it,
        so replays of the same buffer don't inflate it and a buffer that clears at a case
        boundary (tracked per case) doesn't decrement it. That makes it a clean 'new
        measurement data arrived' signal for the stall watchdog.
        """
        prev = self._last_pkt_count.get(case_name, 0)
        if count and count > prev:
            self._packet_progress += count - prev
            self._last_pkt_count[case_name] = count

    def packet_progress(self) -> int:
        """Monotonic count of NEW Qi packets observed this run (WP 1.0 data-progress, REST half)."""
        return self._packet_progress

    def poll_packets(self, case_name: str) -> None:
        """
        Snapshot the app's live packet buffer for the currently-running case, keeping the
        latest non-empty result. The app CLEARS GetCCLinePackets when the next case starts,
        so a fresh GET at the case boundary is usually already empty — we must grab it while
        the case is still active. Called periodically by the run loop; never raises.

        Also advances the data-progress signal (WP 1.0) when the buffer grows, so the monitor
        loop can tell an active tester from one that has stopped delivering measurement data.
        """
        if not self._active or not case_name or not self.base_url:
            return
        try:
            r = self._session.get(f"{self.base_url}/{_EP_PACKETS}", timeout=30)
            if r.status_code != 200:
                return
            try:
                data = r.json()
            except Exception:
                return
            count = _packet_count(data) or 0
            if count:
                self._note_packet_count(case_name, count)
        except Exception as e:
            self.logger.debug(f"[capture] packet poll failed: {e}")

    def _write_summary(self) -> None:
        try:
            if self.run_dir:
                with open(os.path.join(self.run_dir, "run_summary.json"), "w", encoding="utf-8") as f:
                    json.dump(self.summary, f, indent=2, default=str)
        except Exception as e:
            self.logger.warning(f"[capture] could not write run summary: {e}")
