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
import re
import zipfile
from datetime import datetime
from typing import Any, Dict, Optional

# report ZIP + packet log endpoints, relative to the app's /api base URL (REST on both builds)
_EP_REPORT = "ReportsGeneration/PostCurrentTestReport"
_EP_PACKETS = "Plot/GetCCLinePackets"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str) -> str:
    """Filesystem-safe folder/file name from a test-case name."""
    return _SAFE.sub("_", str(name)).strip("_") or "case"


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
        self._offsets: Dict[str, int] = {}   # per stream type: frames already written
        self._pkt_snapshots: Dict[str, Any] = {}   # per case: latest non-empty live packet buffer
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
            self._offsets = {}
            self._pkt_snapshots = {}
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
        Capture one case's data at completion. Never raises — problems are recorded in the
        returned record and the run summary and logged at WARNING.
        """
        if not self._active or not self.run_dir:
            return None
        rec: Dict[str, Any] = {
            "case": case_name, "status": status,
            "ts": datetime.now().strftime("%H:%M:%S"), "files": [], "errors": [],
        }
        try:
            cdir = os.path.join(self.run_dir, _safe(case_name))
            os.makedirs(cdir, exist_ok=True)
            self._capture_report(cdir, case_name, rec)
            self._capture_packets(cdir, rec, case_name)
            self._capture_streams(cdir, telemetry, rec)
        except Exception as e:                       # defensive — capture must never abort a run
            rec["errors"].append(f"capture_case: {e}")
            self.logger.warning(f"[capture] {case_name}: unexpected error: {e}")

        self.summary["cases"].append(rec)
        self._write_summary()
        note = f"{len(rec['files'])} file(s)"
        if rec["errors"]:
            note += f", {len(rec['errors'])} error(s)"
        self.logger.info(f"[capture] {case_name}: {note}")
        return rec

    def finalize_run(self) -> Optional[str]:
        """Write the final summary and zip the run directory into one ZIP. Returns the ZIP path."""
        if not self._active or not self.run_dir:
            return None
        self._active = False
        try:
            self.summary["finished"] = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.summary["case_count"] = len(self.summary.get("cases", []))
            self._write_summary()
            zip_path = self.run_dir + ".zip"
            base_parent = os.path.dirname(self.run_dir)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for root, _dirs, files in os.walk(self.run_dir):
                    for fn in files:
                        full = os.path.join(root, fn)
                        z.write(full, os.path.relpath(full, base_parent))
            self.logger.info(f"[capture] run packaged -> {zip_path}")
            return zip_path
        except Exception as e:
            self.logger.warning(f"[capture] packaging failed: {e}")
            return None

    # -- per-datum capture -----------------------------------------------------------------
    def _capture_report(self, cdir: str, case_name: str, rec: Dict[str, Any]) -> None:
        if not self.base_url:
            rec["errors"].append("report: no base URL")
            return
        try:
            r = self._session.post(f"{self.base_url}/{_EP_REPORT}", timeout=120)
            if r.status_code == 200 and r.content:
                path = os.path.join(cdir, f"{_safe(case_name)}.zip")
                with open(path, "wb") as f:
                    f.write(r.content)
                rec["files"].append({"report_zip": os.path.relpath(path, self.run_dir),
                                     "bytes": len(r.content)})
                self.logger.info(f"[capture] {case_name}: report ZIP {len(r.content)} bytes")
            else:
                msg = f"report HTTP {r.status_code} ({len(r.content)} bytes)"
                rec["errors"].append(msg)
                self.logger.warning(f"[capture] {case_name}: {msg}")
        except Exception as e:
            rec["errors"].append(f"report: {e}")
            self.logger.warning(f"[capture] {case_name}: report failed: {e}")

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
            if count:                        # keep only non-empty snapshots
                self._pkt_snapshots[case_name] = data
                self._note_packet_count(case_name, count)
        except Exception as e:
            self.logger.debug(f"[capture] packet poll failed: {e}")

    def _capture_packets(self, cdir: str, rec: Dict[str, Any], case_name: str) -> None:
        # Prefer a snapshot taken while the case was live; the boundary GET is usually empty
        # because the next case already cleared the buffer. The final case (nothing after it)
        # still has a live buffer, so a fresh GET covers it.
        data = self._pkt_snapshots.pop(case_name, None)
        source = "live-snapshot"
        if data is None:
            if not self.base_url:
                rec["errors"].append("packets: no base URL")
                return
            try:
                r = self._session.get(f"{self.base_url}/{_EP_PACKETS}", timeout=60)
                if r.status_code != 200:
                    rec["errors"].append(f"packets HTTP {r.status_code}")
                    self.logger.warning(f"[capture] packets HTTP {r.status_code}")
                    return
                try:
                    data = r.json()
                except Exception:
                    data = None
                source = "fresh-get"
                if data is None:                 # not JSON — keep the raw body
                    path = os.path.join(cdir, "packet_logs.raw")
                    with open(path, "wb") as f:
                        f.write(r.content)
                    rec["files"].append({"packet_log": os.path.relpath(path, self.run_dir),
                                         "bytes": len(r.content), "source": source})
                    self.logger.info("[capture] packet log saved (raw)")
                    return
            except Exception as e:
                rec["errors"].append(f"packets: {e}")
                self.logger.warning(f"[capture] packets failed: {e}")
                return
        count = _packet_count(data)
        path = os.path.join(cdir, "packet_logs.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        rec["files"].append({"packet_log": os.path.relpath(path, self.run_dir),
                             "packets": count, "source": source})
        if not count:
            rec["errors"].append(f"packet log empty ({source})")
        self.logger.info(f"[capture] packet log saved ({source}, packets={count})")

    @staticmethod
    def _frame_testcase(frame: Any) -> Optional[str]:
        """The case a stream frame belongs to (frames carry their own ``testcaseName``)."""
        return frame.get("testcaseName") if isinstance(frame, dict) else None

    def _frames_for_current_case(self, new: list) -> list:
        """
        Return the leading run of frames belonging to the case whose boundary just closed,
        stopping before the first frame explicitly tagged with a DIFFERENT (next) case — so
        the next case's frames are held back and captured with THAT case (WP 1.7 boundary
        fix). Frames without a ``testcaseName`` are treated as part of the current case, since
        they can't be attributed otherwise.
        """
        current = None
        kept = []
        for fr in new:
            tc = self._frame_testcase(fr)
            if tc is not None:
                if current is None:
                    current = tc
                elif tc != current:
                    break   # the next case begins here — hold this frame and the rest back
            kept.append(fr)
        return kept

    def _capture_streams(self, cdir: str, telemetry: Optional[Dict], rec: Dict[str, Any]) -> None:
        # WebSocket builds only — save the stream frames that arrived for THIS case (the delta
        # since the previous case), so each case folder holds its own measurement stream.
        if not telemetry:
            return
        try:
            data = telemetry.get("data") if isinstance(telemetry, dict) else None
            if not isinstance(data, dict):
                return
            case_streams: Dict[str, list] = {}
            for name, frames in list(data.items()):
                if not isinstance(frames, list):
                    continue
                # Snapshot once — the transport's listener thread appends concurrently, so
                # reading frames[off:] and len(frames) separately could skip a frame.
                snapshot = list(frames)
                off = self._offsets.get(name, 0)
                new = snapshot[off:]
                if not new:
                    continue
                # Keep only this case's frames; hold back any already tagged with the next case
                # and advance the offset only past what we kept, so those frames land in the
                # next case's capture instead of being mislabelled here (WP 1.7).
                kept = self._frames_for_current_case(new)
                self._offsets[name] = off + len(kept)
                if kept:
                    case_streams[name] = kept
            if not case_streams:
                return
            path = os.path.join(cdir, "streams.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(case_streams, f, indent=2, default=str)
            counts = {k: len(v) for k, v in case_streams.items()}
            rec["files"].append({"streams": os.path.relpath(path, self.run_dir), "counts": counts})
            self.logger.info(f"[capture] WS streams saved (counts={counts})")
        except Exception as e:
            rec["errors"].append(f"streams: {e}")
            self.logger.warning(f"[capture] streams failed: {e}")

    # -- summary ---------------------------------------------------------------------------
    def _write_summary(self) -> None:
        try:
            if self.run_dir:
                with open(os.path.join(self.run_dir, "run_summary.json"), "w", encoding="utf-8") as f:
                    json.dump(self.summary, f, indent=2, default=str)
        except Exception as e:
            self.logger.warning(f"[capture] could not write run summary: {e}")
