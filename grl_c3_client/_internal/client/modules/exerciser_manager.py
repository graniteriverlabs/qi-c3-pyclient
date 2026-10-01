# client/modules/exerciser_manager.py
"""
Exerciser mode (Phase 5) — license-gated manual controller operations, for BOTH apps.

The C3 apps expose an exerciser controller per build variant, routed as
``api/CustomAPIConfiguration_<variant>/<Action>`` where ``<variant>`` is MPPTPR (MPP-TPR,
receiver-emulation) or MPPTPT (TPT, transmitter-emulation). The two play opposite roles, so
some ops share an endpoint and some differ; see [[c3-exerciser-api-from-dll]].

Design:
  - **License gate first.** The connect response (`ConnectionSetupInfo`) carries
    `IsLicenseEnabled` + `LicenseInfo[]`; the exerciser is enabled only when an active
    ``*V2_3*`` / ``*V23*`` license module is present. There is no ``V2_3_1`` module, so a 2.3
    license already covers 2.3.1 (the user's "2.3 ≡ 2.3.1" rule). `evaluate_exerciser_license`
    is a pure function so it is unit-testable over a captured payload.
  - **Every op is gated.** An op called while unlicensed returns a clear blocked result and
    makes no HTTP call.
  - Ops are issued with the handler's `send_request(method, service, endpoint)` directly, so
    the route pattern is honoured without needing an enum/config entry per op.

  - **HTTP 200 proves nothing.** Every exerciser endpoint in the app is `public void` inside a
    try/catch, so it answers 200 with an empty body whether the operation ran, threw, or bound an
    empty model — and `PutStartExerciser` silently no-ops when the tester is disconnected. Success
    is therefore established by READING STATE BACK, never from the HTTP status. See
    `Docs/archive/EXERCISER_FINDINGS.md`.
  - **Stop is structural.** It runs from a `finally` on every exit path, because leaving the
    exerciser running drives real power into a DUT — and because the app only writes the session's
    capture folder when stop is called.
  - **The exerciser has no end of its own.** Unlike a compliance run, which works through a list of
    test cases and reports itself finished, `PutStartExerciser` begins emulation and the app never
    stops it — there is no "exerciser finished" state anywhere in its API. So the session runner
    holds the run open (`_hold_session`) until the operator ends it, watching live readings while it
    does. Without that phase the step loop would fall straight into the `finally` and stop the
    exerciser about a second after starting it, leaving an essentially empty capture folder.

Where the session's content comes from:
  - The **packet sequence and controller settings** come from the app's own exported file, parsed
    by `exerciser_sequence.py`. Nothing is hand-typed and nothing is fabricated here.
  - The **run controls** (which steps to run, measurement channels, dry-run, verify policy) come
    from `common.exerciser` in `grl_config.json`, beside `run_mode`.
"""
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from client.core.app_response import ci_get
from client.modules.data_capture_manager import _packet_count


def _strip_json_comments(text: str) -> str:
    """Remove // line and /* */ block comments from JSON text, without touching comment-like
    sequences that appear INSIDE string values (e.g. a URL's `//`). Lets the exerciser config
    carry real inline comments to guide the user, then be parsed by the standard json module.
    """
    out = []
    i, n = 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:          # keep escaped char verbatim
                out.append(text[i + 1]); i += 2; continue
            if c == quote:
                in_str = False
            i += 1; continue
        if c == '"' or c == "'":
            in_str = True; quote = c; out.append(c); i += 1; continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":       # line comment
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":       # block comment
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c); i += 1
    return "".join(out)


def load_commented_json(path: str) -> Any:
    """Load a JSON file that may contain // and /* */ comments (the exerciser config)."""
    with open(path, "r", encoding="utf-8") as f:
        return json.loads(_strip_json_comments(f.read()))


#: App name -> exerciser controller variant.
_VARIANT = {
    "GRL-C3-MP-TPR": "MPPTPR",
    "GRL-C3-TPT-MPP": "MPPTPT",
    # C3-TPR is a separate application (port 3003) with its own controller. Routes below were
    # read from the build's own Swagger list, not guessed: it publishes 437 routes, of which
    # `CustomAPIConfiguration_C3TPR/*` are the exerciser ones.
    "GRL-WP-TPR-C3": "C3TPR",
    # Same install and port as GRL-C3-TPT-MPP; which mode the tester is in is a firmware
    # choice the operator makes. In BPP/EPP mode the app drives the C3TPT controller, which
    # is a much smaller set than MPPTPT - 18 routes, no reset, no cloak, no calibration ops.
    "GRL-C3-TPT-BPP-EPP": "C3TPT",
}

#: Op-name -> routing + optional read-back, per variant. Routes are from the DLL decompile.
#: ``path`` are fixed leading segments; the session may append more. ``read`` is the GET used to
#: confirm the write actually landed, with a verification tier:
#:   A  the GET returns the SAME model the PUT accepts -> compare the fields we wrote
#:   B  only indirect evidence exists (state/settings) -> snapshot, no field-level claim
#:   C  no read-back exists at all -> always reported "unavailable", never pass or fail
#: ``use_path`` repeats the op's own path segments on the GET (e.g. GetModulatorValues/{Freq}).
_OP_REGISTRY = {
    "MPPTPR": {
        "start":           {"verb": "PUT", "route": "PutStartExerciser",
                            "read": {"verb": "GET", "route": "GetRetainedExerciserProps", "tier": "B"}},
        "stop":            {"verb": "GET", "route": "GetStopExerciser"},
        "reset":           {"verb": "GET", "route": "GetLoadRamp", "path": [True],
                            "read": {"verb": "GET", "route": "GetSetLoadValues", "tier": "B"}},
        "packets":         {"verb": "PUT", "route": "PutPacketInformationMppTprModel",
                            "read": {"verb": "GET", "route": "GetDefaultPacketsMppTprModel", "tier": "B"}},
        "packets_config":  {"verb": "PUT", "route": "RunTimePacketConfiguration",
                            "read": {"verb": "GET", "route": "GetDefaultPacketsMppTprModel", "tier": "B"}},
        "phase_settings":  {"verb": "PUT", "route": "PutPhaseSetting"},
        # ⚠ TIER B, NOT A, on all three of these. `GetSetLoadValues`, `GetRxCoilValues` and
        # `GetModulatorValues` do NOT return what was written — they return the app's recommended
        # values for the SELECTED COIL (QiDataModelTPR.cs:36805/36896/37074 are chains of
        # `if (SelectedcoilTypeinFW == CoilType.X) { ... }` assigning constants, and
        # `GetModulatorValues` in MPP mode always answers "33nF"/"ASK_DC_Mod"). Comparing them
        # field-by-field would fail whenever the export's values differ from the coil's defaults —
        # e.g. this export sets load 50 while TPR_MP4's default is 100 — halting a session that is
        # working perfectly. They are snapshots only.
        "set_load":        {"verb": "PUT", "route": "PutSetLoadValues",
                            "read": {"verb": "GET", "route": "GetSetLoadValues", "tier": "B"}},
        "load_ramp":       {"verb": "PUT", "route": "PutLoadRamp",
                            "read": {"verb": "GET", "route": "GetSetLoadValues", "tier": "B"}},
        "vrect":           {"verb": "PUT", "route": "PutSetVoltageValues",          # takes RxCoilModel
                            "read": {"verb": "GET", "route": "GetRxCoilValues", "path": [True], "tier": "B"}},
        "vrect_loop":      {"verb": "PUT", "route": "PutVoltageLoop",               # /{isEnabled} via wrapper
                            "read": {"verb": "GET", "route": "GetRetainedExerciserProps", "tier": "B"}},
        "ask_modulation":  {"verb": "PUT", "route": "PutModulatorValues",           # /{Freq}
                            "read": {"verb": "GET", "route": "GetModulatorValues", "use_path": True, "tier": "B"}},
        "coil_type":       {"verb": "PUT", "route": "PutRxCoil",
                            "read": {"verb": "GET", "route": "GetRxCoilValues", "path": [True], "tier": "B"}},
        # Path-only endpoints: the app declares NO body parameter, so a body would be ignored.
        #   PutPPValues(string Selected_PP_Val)          - and on TPR the method is EMPTY, a no-op
        #   Put_CloakEntryReasonConfig(int cloakRsn)     - index into the app's own option list
        #   Put_CloakExitTypeConfig(string exitType)     - index; app does Convert.ToInt32 then +1
        #   Put_CloakIllegalPacketConfig(string msg)     - bare hex byte, app does Convert.ToByte(msg,16)
        # None has a GET, so all are tier C and can never be verified.
        "pp_values":       {"verb": "PUT", "route": "PutPPValues"},
        "cloak_entry_reason":   {"verb": "PUT", "route": "Put_CloakEntryReasonConfig"},
        "cloak_exit_type":      {"verb": "PUT", "route": "Put_CloakExitTypeConfig"},
        "cloak_illegal_packet": {"verb": "PUT", "route": "Put_CloakIllegalPacketConfig"},
        # `Putuploadedtestsequence` exists ONLY on the TPT controller, so MPP-TPR has no
        # save-sequence op at all. Listing it here would make it look available and then 404.
        "recall_sequence": {"verb": "PUT", "route": "PutLoadConfigurationFileData"},  # /{fName}
    },
    "C3TPR": {
        "start":           {"verb": "PUT", "route": "PutStartExerciser",
                            "read": {"verb": "GET", "route": "GetControllerSettings", "tier": "B"}},
        "stop":            {"verb": "GET", "route": "GetStopExerciser"},
        "reset":           {"verb": "GET", "route": "GetLoadRamp", "path": [True],
                            "read": {"verb": "GET", "route": "GetSetLoadValues", "tier": "B"}},
        "packets":         {"verb": "PUT", "route": "PutQiPacketInformation",
                            "read": {"verb": "GET", "route": "GetDefaultQiPackets", "tier": "B"}},
        "packets_config":  {"verb": "PUT", "route": "RunTimePacketConfiguration",
                            "read": {"verb": "GET", "route": "GetDefaultQiPackets", "tier": "B"}},
        "phase_settings":  {"verb": "PUT", "route": "PutPhaseSetting",
                            "read": {"verb": "GET", "route": "GetDefaultPhaseSettings", "tier": "B"}},
        # Same tier-B reasoning as MPP-TPR: these GETs return the coil's recommended values,
        # not what was written, so they are snapshots and never field-compared.
        "set_load":        {"verb": "PUT", "route": "PutSetLoadValues",
                            "read": {"verb": "GET", "route": "GetSetLoadValues", "tier": "B"}},
        "load_ramp":       {"verb": "PUT", "route": "PutLoadRamp",
                            "read": {"verb": "GET", "route": "GetSetLoadValues", "tier": "B"}},
        "vrect":           {"verb": "PUT", "route": "PutSetVoltageValues",
                            "read": {"verb": "GET", "route": "GetRxCoilValues", "path": [True], "tier": "B"}},
        "ask_modulation":  {"verb": "PUT", "route": "PutModulatorValues",
                            "read": {"verb": "GET", "route": "GetModulatorValues", "use_path": True, "tier": "B"}},
        "coil_type":       {"verb": "PUT", "route": "PutRxCoil",
                            "read": {"verb": "GET", "route": "GetRxCoilValues", "path": [True], "tier": "B"}},
        "pp_values":       {"verb": "PUT", "route": "PutPPValues"},
        "recall_sequence": {"verb": "PUT", "route": "PutLoadConfigurationFileData"},
    },
    "C3TPT": {
        "start":           {"verb": "PUT", "route": "PutStartExerciser",
                            "read": {"verb": "GET", "route": "GetSelectedQiSpecMode", "tier": "B"}},
        "stop":            {"verb": "GET", "route": "GetStopExerciser"},
        "packets":         {"verb": "PUT", "route": "PutTPTPacketInformation",
                            "read": {"verb": "GET", "route": "GetDefaultQiPacketsTPT", "tier": "B"}},
        "phase_settings":  {"verb": "PUT", "route": "PutTPTPhaseSettings",
                            "read": {"verb": "GET", "route": "GetDefaultTPTPhaseSettings", "tier": "B"}},
        "coil_type":       {"verb": "PUT", "route": "PutTPTCoilConfiguration", "path": [True]},
        "inverter_config": {"verb": "PUT", "route": "PutInverterConfiguration", "path": [True]},
        "pfo_offset":      {"verb": "PUT", "route": "PutPfoOffsetConfiguration", "path": [True]},
        # No `reset` op: this controller has no PutResetFWExerciser. Omitted rather than
        # listed, so a configured 'reset' step reports unavailable instead of 404ing.
    },
    "MPPTPT": {
        "start":           {"verb": "PUT", "route": "PutStartExerciser",
                            "read": {"verb": "GET", "route": "GetControllerSettings", "tier": "B"}},
        "stop":            {"verb": "GET", "route": "GetStopExerciser"},
        "reset":           {"verb": "PUT", "route": "PutResetFWExerciser",
                            "read": {"verb": "GET", "route": "GetSetLoadValues", "tier": "B"}},
        "packets":         {"verb": "PUT", "route": "PutPacketInformationMppTptModel",
                            "read": {"verb": "GET", "route": "GetDefaultPacketsMppTptModel", "tier": "B"}},
        "packets_config":  {"verb": "PUT", "route": "PutPacketInformationMppTptModel",
                            "read": {"verb": "GET", "route": "GetDefaultPacketsMppTptModel", "tier": "B"}},
        "phase_settings":  {"verb": "PUT", "route": "PutTPTPhaseSettings",
                            "read": {"verb": "GET", "route": "GetDefaultTPTPhaseSettings", "tier": "B"}},
        "coil_type":       {"verb": "PUT", "route": "PutTPTCoilConfiguration", "path": [True],
                            "read": {"verb": "GET", "route": "GetRxCoilValues", "path": [True], "tier": "B"}},
        "inverter_config": {"verb": "PUT", "route": "PutInverterConfiguration", "path": [True],
                            "read": {"verb": "GET", "route": "GetDefaultTPTPhaseSettings", "tier": "B"}},
        "pfo_offset":      {"verb": "PUT", "route": "PutPfoOffsetConfiguration", "path": [True]},
        "power_mode":      {"verb": "PUT", "route": "PutPowerModeConfig",
                            "read": {"verb": "GET", "route": "GetPPValues", "tier": "B"}},
        "mated_q":         {"verb": "PUT", "route": "PutMatedQParams", "path": [True],
                            "read": {"verb": "GET", "route": "GetDefaultMatedQConfig", "tier": "A"}},
        "modexcap":        {"verb": "PUT", "route": "PutModeXCAP"},                 # tier C
        "dploss_offset":   {"verb": "PUT", "route": "PutDPLossOffsetConfiguration"},
        "cloak":           {"verb": "PUT", "route": "PutCloakingFeature"},
        "tuning_clamp":    {"verb": "PUT", "route": "PutTuningandClamp"},
        "calib_points":    {"verb": "PUT", "route": "PutCalibrationConfig"},
        "instant_packets": {"verb": "PUT", "route": "PutCurrentRowQiPacketInformation"},
        "save_sequence":   {"verb": "PUT", "route": "Putuploadedtestsequence"},     # tier C
        "recall_sequence": {"verb": "PUT", "route": "PutLoadConfigurationFileData"},
    },
}

#: Route -> how many path segments the app's route declares, e.g. `PutModulatorValues/{Freq}` = 1.
#: Taken from the `[HttpPut(...)]` / `[HttpGet(...)]` attributes in the app binary; routes absent
#: here take none. Both controllers agree on every route we use, so one table covers both.
#:
#: Why this exists: a call missing its segment does not fail loudly, it simply addresses a URL that
#: does not exist and comes back **404**. That is what broke the first MPP-TPR run (2026-08-17) —
#: `PutModulatorValues` sent with no frequency. Checked pre-flight now, so the session is rejected
#: before anything is sent instead of dying at step 2. Unit U55 asserts this table still matches
#: the binary; re-derive it when a new app version lands.
_ROUTE_SEGMENTS = {
    "GetLoadRamp": 1,                       # /{isFromResetExerciser}
    "GetModulatorValues": 1,                # /{Freq}
    "GetRxCoilValues": 1,                   # /{isPageRefreshed}
    "PutInverterConfiguration": 1,          # /{isConfigreq}
    "PutLoadConfigurationFileData": 1,      # /{fName}
    "PutMatedQParams": 1,                   # /{isConfigreq}
    "PutModulatorValues": 1,                # /{Freq}
    "PutPPValues": 1,                       # /{Selected_PP_Val}
    "PutPfoOffsetConfiguration": 1,         # /{isConfigreq}
    "PutTPTCoilConfiguration": 1,           # /{isConfigreq}
    "PutVoltageLoop": 1,                    # /{isEnabled}
    "Put_CloakEntryReasonConfig": 1,        # /{cloakRsn}
    "Put_CloakExitTypeConfig": 1,           # /{exitType}
    "Put_CloakIllegalPacketConfig": 1,      # /{msg}
}

#: Same route name, different arity per controller. The C3-TPR build declares
#: `PutModulatorValues` / `GetModulatorValues` with NO path parameter, where MPP-TPR declares both
#: as `/{Freq}`. Appending the segment there answers 404 and failed the step outright
#: ("'ask_modulation' -> FAILED") on the first live C3-TPR exerciser session.
_ROUTE_SEGMENTS_BY_VARIANT = {
    "C3TPR": {"PutModulatorValues": 0, "GetModulatorValues": 0},
}


#: Every op name any controller supports. Used to tell "this controller cannot do it"
#: (skip) from "that is not an op at all" (reject), which validate_session still catches.
_ALL_OPS = frozenset(op for _v in _OP_REGISTRY.values() for op in _v)


def _segments_for(variant: str, route: str) -> int:
    """How many path segments `route` takes on `variant`'s controller."""
    override = _ROUTE_SEGMENTS_BY_VARIANT.get(variant, {})
    if route in override:
        return override[route]
    return _ROUTE_SEGMENTS.get(route, 0)


#: Ops whose request MUST carry a body. Checked pre-flight so a missing body is caught before
#: anything is sent, rather than becoming a silent 200 against an empty model.
_BODY_REQUIRED = {
    "MPPTPR": {"packets", "packets_config", "phase_settings", "set_load", "load_ramp", "vrect",
               "ask_modulation", "coil_type"},
    "C3TPR": {"packets", "packets_config", "phase_settings", "set_load", "load_ramp",
              "vrect", "ask_modulation", "coil_type"},
    "C3TPT": {"packets", "phase_settings", "coil_type", "inverter_config", "pfo_offset"},
    "MPPTPT": {"packets", "packets_config", "phase_settings", "coil_type", "inverter_config",
               "pfo_offset", "power_mode", "mated_q", "modexcap", "dploss_offset", "cloak",
               "tuning_clamp", "calib_points", "instant_packets"},
}

#: Measurement channels the tester records during a session (the app's MeasurementChannels enum).
#: `PutStartExerciser` takes this list; send nothing and the app records ONLY Rectified_Voltage +
#: Rectified_Current, and what was not requested cannot be recovered afterwards.
_MEASUREMENT_CHANNELS = frozenset((
    "Coil_Voltage", "Coil_Current", "Rectified_Voltage", "Rectified_Current",
    "Temperature_Channel1", "Temperature_Channel2", "Coil_Voltage_VCTX", "I_Channel",
    "Q_Channel", "Voltage_Magnitude", "I_Slope_Phase", "TekScope",
    "Ext_Coil_Voltage_Plus", "Ext_Coil_Voltage_Minus", "Ext_Coil_Current",
    "Ext_Rectified_Voltage", "Ext_Rectified_Current", "RMS_AVG_2ms_data",
    "AC_Current_DC_Mode_TPT_AskMag",
))

#: The app returns this from GetStopExerciser instead of a folder path when the tester link is
#: down — the session produced no data, so it must not be reported as a success.
_STOP_NO_COMMS = "tester communication was not established"

#: ⚠ `PutSelectedQiSpecMode/{SpecMode}` does NOT enter exerciser mode on the two apps we drive.
#: Only the legacy `CustomAPIConfiguration_C3TPR`/`_C3TPT` controllers set
#: `TestcaseOrExerciser = ExerciserMode` in that action (exe.cs:4838, 5008). On `_MPPTPR`
#: (exe.cs:6142) and `_MPPTPT` (exe.cs:6534) it only selects a legacy spec/technology.
#:
#: **`PutStartExerciser` is what enters exerciser mode.** `PutStartExerciserMPP` calls
#: `Start_Service("QiSignalCapture", …)` (QiDataModelTPT.cs:19954/20006), which reaches
#: `CommonHelper.AppRunningModeSeletionBasedonEnum(testName)` (QiDataResultsModel.cs:9399); and
#: `AppRunningMode.ExerciserMode` carries `[Description("QiSignalCapture")]` (AppFrmWrk.cs:36608),
#: so the name matches and the app sets ExerciserMode itself. No separate entry call exists.
#:
#: The values each app's `PutSelectedQiSpecMode` actually accepts (its C# `switch` has no default,
#: so anything else falls through and does nothing — while the void endpoint still answers 200):
_SPEC_MODES = {
    # QiDataModelTPT.cs:21141 — technology family, NOT a power profile. "MPP25" is not accepted.
    "MPPTPT": ("MPP", "BPP", "EPP", "V_2.0.1", "V2.1"),
    "C3TPT": ("1.3.3", "2.0.1", "2.1.0", "2.2.1", "2.3.1"),
    # QiDataModelTPR.cs:37935 — spec version numbers.
    "MPPTPR": ("1.3", "1.2.4", "1.3.3", "2.0.0", "2.0.1", "2.1"),
    # This build reports its own list via TestConfiguration/GetAppModeList.
    "C3TPR": ("1.3.3", "2.0.1", "2.1.0", "2.0.0"),
}

#: What `GetAppState` reports once a run is under way. `PutStartExerciser` sets HostState BUSY, but
#: when the tester is unreachable it shows a message box, sets READY and returns — still HTTP 200.
#: So BUSY is the one signal that separates a real start from that silent no-op.
_BUSY = "busy"

#: Tries (about 0.5 s apart) allowed for HostState to reach BUSY after start. An internal settle
#: window for one state transition, not a duration the user has to guess.
_START_STATE_TRIES = 6

#: Re-print the "press Enter to stop" prompt every N sweeps (~1 minute). The readings scroll it
#: out of sight, and a session with no visible way out looks stuck.
_PROMPT_EVERY = 12

#: How often the hold phase samples live readings, in seconds. Deliberately a CONSTANT, not a
#: config key: it is a sampling rate, not a guess at how long anything takes, and the project rule
#: is that no run needs a time setting from the user.
_MONITOR_INTERVAL_S = 5.0

#: Consecutive fully-failed monitor sweeps that mean the tester link is gone. One failed sweep is
#: not enough — a single dropped read happens; three in a row does not.
_MONITOR_FAIL_LIMIT = 3

#: Upper bound on samples kept in memory for one hold. At one sweep per 5 s this is roughly 7
#: hours; past it the run continues and only the stored detail stops growing.
_MAX_SAMPLES = 5000

#: Live readings taken during the hold, per variant: (label, GET route on that app's exerciser
#: controller). The two apps emulate OPPOSITE roles, so the readings that mean anything differ:
#:
#:   * **MPP-TPR emulates a receiver**, so the rectified/load/received-power readings are its own
#:     and carry real values.
#:   * **TPT emulates a transmitter.** It has no Rx coil — `GetRxCoilRectifiedReadings` belongs to
#:     the DUT and reads all zeros, which is exactly what the 2026-08-17 bench run logged while the
#:     packet capture showed the tester driving 9.98 V at 360 kHz. It was removed for that reason.
#:     The TPT controller exposes no live transmitter voltage/frequency GET at all (its live values
#:     reach us only in the packet stream), so packet growth below is what proves TPT is working.
_MONITOR_READS = {
    "MPPTPR": (
        ("rectified", "GetRxCoilRectifiedReadings"),
        ("load", "GetCurrentLoadValue"),
        ("received_power", "GetReceivedPowerValue"),
        ("temperature", "GetLoadRampTemperatureReadings"),
    ),
    "C3TPR": (
        ("rectified", "GetRxCoilRectifiedReadings"),
        ("load", "GetCurrentLoadValue"),
        ("received_power", "GetReceivedPowerValue"),
        ("temperature", "GetLoadRampTemperatureReadings"),
    ),
    "C3TPT": (),          # no live-reading GET on this controller; packet growth is the signal
    "MPPTPT": (
        ("power_status", "Get_MPP_Supp_PowerStatus"),
    ),
}

#: Live packet count, on the `Plot` service rather than the exerciser controller. This is the one
#: signal that works on BOTH apps and both transports, and it answers the question the readings
#: cannot: **is the session actually producing data?** It is the same source the compliance
#: capture and the stall watch already use.
_MONITOR_PACKETS = ("Plot", "GetCCLinePackets")


def _stable(value: Any) -> str:
    """A comparable rendering of an app payload, order-insensitive for object keys."""
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:                                             # pragma: no cover - defensive
        return str(value)


def _changed_keys(before: Any, after: Any) -> List[str]:
    """
    Top-level fields that differ between two snapshots of the same read-back.

    Used for tier-B evidence: the app moving between the before and after reads is real proof the
    write landed, even where no field-level claim can be made. Falls back to a whole-value compare
    when the payloads are not objects.
    """
    if isinstance(before, dict) and isinstance(after, dict):
        keys = set(before) | set(after)
        return sorted(k for k in keys
                      if _stable(ci_get(before, k, _MISSING)) != _stable(ci_get(after, k, _MISSING)))
    return [] if _stable(before) == _stable(after) else ["<value>"]


def _compare_fields(sent_body: Any, after: Any) -> Tuple[List[str], int]:
    """
    Compare the fields we wrote against a read-back.

    Returns ``(mismatches, compared)``. Fields the model does not echo back are skipped, not
    counted as mismatches — only fields present on BOTH sides are compared, and `compared` says how
    many those were, so "no mismatches" out of zero comparisons is never mistaken for proof.
    Case-insensitive, because the app answers camelCase while accepting PascalCase.
    """
    if not isinstance(sent_body, dict) or not isinstance(after, dict):
        return [], 0
    mismatched: List[str] = []
    compared = 0
    for key, want in sent_body.items():
        got = ci_get(after, key, _MISSING)
        if got is _MISSING:
            continue                          # the model does not echo this field; not a mismatch
        compared += 1
        if str(got) != str(want):
            mismatched.append(f"{key}: sent {want!r}, app has {got!r}")
    return mismatched, compared


class _InterruptGuard:
    """
    Turn Ctrl+C into a clean stop REQUEST instead of an exception thrown at an arbitrary line.

    Why this exists (bench run 2026-08-17 12:22): `KeyboardInterrupt` is a `BaseException`, not an
    `Exception`, so it sailed straight through `_finalize_session`'s `except Exception` and landed
    **inside the stop call** — which was aborted after 29.5 s with no response. The exerciser was
    left running on the tester and that session's capture folder was never written, which is the
    exact failure the `finally` was built to prevent.

    With this installed, Ctrl+C sets a flag and asks the session to wind down; the loop leaves
    normally and teardown runs to completion. Further Ctrl+C presses during teardown are absorbed
    too, with a message, because stopping the hardware matters more than a fast exit.

    Only the main thread may install a signal handler; anywhere else this is a no-op, so unit
    tests and embedded callers are unaffected.
    """

    def __init__(self, logger, on_interrupt):
        self.logger = logger
        self._on_interrupt = on_interrupt
        self.hits = 0
        self._previous = None
        self._installed = False

    @property
    def interrupted(self) -> bool:
        return self.hits > 0

    def _handle(self, signum, frame) -> None:                     # pragma: no cover - signal path
        self.hits += 1
        if self.hits == 1:
            self.logger.warning("[exerciser] Ctrl+C - stopping the exerciser and saving the "
                                "capture, please wait")
        else:
            self.logger.warning(f"[exerciser] already stopping ({self.hits} presses) - the tester "
                                f"must be told to stop or it keeps driving the DUT")
        try:
            self._on_interrupt()
        except Exception:                                         # pragma: no cover - defensive
            pass

    def __enter__(self) -> "_InterruptGuard":
        try:
            self._previous = signal.signal(signal.SIGINT, self._handle)
            self._installed = True
        except (ValueError, OSError, AttributeError):
            self._installed = False           # not the main thread, or no signal support
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._installed:
            try:
                signal.signal(signal.SIGINT, self._previous)
            except (ValueError, OSError):                         # pragma: no cover - defensive
                pass
        return False


def _stdin_is_interactive() -> bool:
    """
    Whether an operator can actually press Enter.

    The hold phase ends on a keypress, so a session with no console attached (a scheduled task, a
    piped run) could never end it. Checked pre-flight so such a session is refused BEFORE the
    exerciser is started, rather than started and then waited on forever.
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:                                             # pragma: no cover - defensive
        return False


def _fmt_elapsed(seconds: float) -> str:
    """mm:ss, for the hold's progress lines."""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _fmt_reading(value: Any) -> str:
    """
    Render one live reading for a console line.

    Scalars print as-is; a model prints its first few numeric fields; anything else prints as a
    presence marker. Display only — the full payload is kept in the session record.
    """
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.3g}"
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, dict):
        nums = [f"{k}={v:.3g}" if isinstance(v, float) else f"{k}={v}"
                for k, v in value.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return "(" + ",".join(nums[:3]) + ")" if nums else "ok"
    if isinstance(value, list):
        return f"[{len(value)}]"
    return "ok"                                                   # pragma: no cover - defensive


#: Sentinel for "the read-back does not echo this field at all" — distinct from a real None,
#: which would be a genuine value mismatch.
_MISSING = object()


def _seg(value: Any) -> str:
    """
    Render one URL path segment.

    `str(True)` is "True", but the app's routes expect the JSON spelling "true" — the exported
    configs and templates write `true`, so without this the URL carries a capital T.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)

#: ⚠ The app sends the enum **Description**, not the enum identifier. `ConnectionSetupInfo` is
#: filled with `new LicenseInfo(EnumHelper.GetEnumDescription(item), GetEnumDescription(status), …)`
#: (exe.cs:9617), so a 2.3 module arrives as **"2.3 - MPP 25W"**, never as
#: `MPP_PTx_ComplianceV2_3`. Matching the identifier spelling blocks EVERY controller, licensed or
#: not — that is exactly what happened on the 2026-08-17 bench run.
#:
#: Real `C3_hardwareLicense` descriptions for 2.3 (AppFrmWrk.cs:36670): "2.3 BPP", "2.3 EPP",
#: "2.3 - MPP 25W", "2.3 - MPP 15W", "2.3 - APP 15W & 25W", "2.3 - BPP", "2.3 - EPP",
#: "2.3 - MCPE&MCPM". The version is the leading number, so that is what we match.
#:
#: The identifier tokens are kept as well: descriptions are resolved with
#: `GetEnumDescription(value, fetchFromDB: true)`, so an install could serve different text, and a
#: build that sends identifiers must still work.
_V23_TOKENS = ("V2_3", "V23")

#: "2.3" as a version, not as a fragment of another number: matches "2.3 BPP", "2.3 - MPP 25W" and
#: "2.3.1 …", but not "2.2.1 EPP" (no 2.3) and not "1.2.3 …" (2.3 preceded by a dot).
_V23_NAME_RE = re.compile(r"(?<![\d.])2\.3(?![\d])")

#: `LicenseType` descriptions that mean NOT usable (AppFrmWrk.cs:36643). Checked FIRST, because
#: "Not Activated - Contact GRL Support" also contains "activated".
_STATUS_NOT_ACTIVE = ("not activated", "not-issued", "not issued", "inactive", "disabled",
                      "expired", "unlicensed", "not licensed", "none")

#: `LicenseType` descriptions that mean usable. The real ones all end "… License Activated":
#: Demo / Evaluation / Permanent / Developer Demo / Developer Permanent. "activated" is the token
#: that actually appears — "active" does NOT occur in "Activated", which is why the original list
#: rejected a permanently licensed controller.
_STATUS_ACTIVE = ("activated", "active", "enabled", "valid", "licensed", "demo", "true")


def _module_matches_2_3(name: Any) -> bool:
    """Whether a LicenseInfo.ModuleName denotes spec 2.3 (which covers 2.3.1)."""
    text = str(name or "")
    return any(tok in text for tok in _V23_TOKENS) or bool(_V23_NAME_RE.search(text))


def _module_active(status: Any) -> bool:
    """
    Whether a `LicenseInfo.ModuleStatus` denotes an active/usable license.

    The strings are `LicenseType` **descriptions** from the app, not enum names — "Permanent
    License Activated", "Demo License Activated", "Evaluation License Activated", "Developer
    Permanent License Activated", "Not Activated - Contact GRL Support", "Not-Issued". Negatives
    are tested first because "Not Activated" contains "activated". Never raises.
    """
    s = str(status or "").strip().lower()
    if not s:
        return False
    if any(bad in s for bad in _STATUS_NOT_ACTIVE):
        return False
    return any(good in s for good in _STATUS_ACTIVE)


def evaluate_exerciser_license(connection_setup_info: Any) -> Dict[str, Any]:
    """
    Decide whether the exerciser is licensed, from a `ConnectionSetupInfo` payload.

    Returns ``{"licensed", "reason", "modules", "seen", "flag", "shape"}``. Enabled iff an active
    ``*V2_3*`` / ``*V23*`` module is present (2.3 covers 2.3.1); falls back to
    ``IsLicenseEnabled``. Pure and defensive — never raises.

    The last three keys are DIAGNOSTIC and carry no decision weight: `seen` is every module the
    payload contained with its status, `flag` is the IsLicenseEnabled value as found, `shape`
    describes a payload that is not the expected form. A block is otherwise unfalsifiable — you
    cannot tell an unlicensed controller from a module name or status spelling this function does
    not recognise, and the two need opposite fixes.
    """
    info = connection_setup_info if isinstance(connection_setup_info, dict) else {}
    shape = ""
    if not isinstance(connection_setup_info, dict):
        shape = f"connect response is {type(connection_setup_info).__name__}, not an object"

    lic_list = info.get("LicenseInfo")
    if lic_list is None:
        lic_list = info.get("licenseInfo")
    if lic_list is None:
        lic_list = []
        if info and not shape:
            shape = (f"no LicenseInfo key in the connect response; it has: "
                     f"{sorted(info.keys())[:12]}")
    elif not isinstance(lic_list, list):
        shape = f"LicenseInfo is {type(lic_list).__name__}, not a list"
        lic_list = []

    seen: List[Dict[str, Any]] = []
    active_v23: List[str] = []
    for m in lic_list:
        if not isinstance(m, dict):
            seen.append({"name": repr(m), "status": None, "entry": "not an object"})
            continue
        name = str(m.get("ModuleName") or m.get("moduleName") or "")
        status = m.get("ModuleStatus") or m.get("moduleStatus")
        matches = _module_matches_2_3(name)
        active = _module_active(status)
        seen.append({"name": name, "status": status, "name_matches_2_3": matches,
                     "status_reads_active": active})
        if matches and active:
            active_v23.append(name)

    flag = info.get("IsLicenseEnabled")
    if flag is None:
        flag = info.get("isLicenseEnabled")

    if active_v23:
        return {"licensed": True, "reason": f"active 2.3 license module(s): {active_v23}",
                "modules": active_v23, "seen": seen, "flag": flag, "shape": shape}
    if flag:
        return {"licensed": True,
                "reason": "IsLicenseEnabled is true (no explicit V2_3 module matched)",
                "modules": [], "seen": seen, "flag": flag, "shape": shape}
    return {"licensed": False,
            "reason": "no active 2.3 (V2_3/V23) license module found; exerciser unavailable",
            "modules": [], "seen": seen, "flag": flag, "shape": shape}


class ExerciserManager:
    """License-gated exerciser operations for the current app (MPP-TPR or TPT)."""

    def __init__(self, config_manager, logger):
        self.config_manager = config_manager
        self.logger = logger
        self.api_handler = None
        self._licensed: Optional[bool] = None
        self._license_reason: str = "license not checked yet"
        #: When true, requests are composed and logged but never sent.
        self.dry_run: bool = False
        #: Where a session's evidence is written (set by the client before run_session).
        self.capture_dir: Optional[str] = None
        #: Packet count from the previous monitor sweep, for the growth figure.
        self._last_packets: Optional[int] = None
        #: Paging cursor + running total for the packet endpoint (it returns batches, not totals).
        self._pkt_index: int = 0
        self._pkt_total: int = 0
        #: Set when the session should wind down (Enter, Ctrl+C, or a lost tester link).
        self._stop_requested = threading.Event()
        #: Who asked for the stop. First request wins, except Ctrl+C, which always wins.
        self._stop_reason: Optional[str] = None

    def _request_stop(self, reason: str) -> None:
        """
        Ask the session to wind down, recording why. Safe from any thread or a signal handler.

        First request wins, so a lost link is not relabelled by the Enter thread waking up
        afterwards - except an interrupt, which always takes precedence because Ctrl+C also causes
        the console read to return and would otherwise be logged as a tidy operator stop.
        """
        if self._stop_reason is None or reason == "interrupted":
            self._stop_reason = reason
        self._stop_requested.set()

    def set_api_handler(self, api_handler) -> None:
        self.api_handler = api_handler

    @property
    def variant(self) -> Optional[str]:
        """The exerciser controller variant for the selected app, or None if unsupported."""
        return _VARIANT.get(getattr(self.config_manager, "app_name", None))

    @property
    def licensed(self) -> bool:
        return bool(self._licensed)

    @property
    def license_reason(self) -> str:
        return self._license_reason

    # -- license gate ------------------------------------------------------
    def apply_license(self, connection_info: Any) -> Dict[str, Any]:
        """
        Evaluate the exerciser license gate from an ALREADY-FETCHED ConnectionSetup response
        (the client caches it at connect time — no redundant call). Caches the verdict on the
        manager. This is the preferred path; `check_license` is a fallback that re-fetches.
        """
        gate = evaluate_exerciser_license(connection_info)
        self._licensed = gate["licensed"]
        self._license_reason = gate["reason"]
        lvl = self.logger.info if gate["licensed"] else self.logger.warning
        lvl(f"[exerciser] license {'ENABLED' if gate['licensed'] else 'BLOCKED'}: {gate['reason']}")
        if not gate["licensed"]:
            self._report_license_evidence(gate, connection_info)
        return gate

    def _report_license_evidence(self, gate: Dict[str, Any], connection_info: Any) -> None:
        """
        Log exactly what the gate saw when it blocked, and save the raw connect response.

        A bare "not licensed" cannot be acted on: an unlicensed controller and a module name or
        status spelling this client does not recognise look identical from outside, and they need
        opposite fixes. Printing every module with its status tells the two apart in one run.
        Diagnostic only — never raises, never changes the verdict.
        """
        try:
            if gate.get("shape"):
                self.logger.warning(f"[exerciser] license payload: {gate['shape']}")
            seen = gate.get("seen") or []
            self.logger.warning(f"[exerciser] license modules reported by the app: {len(seen)}; "
                                f"IsLicenseEnabled={gate.get('flag')!r}")
            for m in seen:
                self.logger.warning(f"[exerciser]   module {m.get('name')!r} "
                                    f"status={m.get('status')!r} "
                                    f"name_matches_2_3={m.get('name_matches_2_3')} "
                                    f"status_reads_active={m.get('status_reads_active')}")
            if not seen:
                self.logger.warning("[exerciser]   (the app reported no license modules at all)")

            from utils.project_root import project_root
            path = os.path.join(project_root(), "logs", "exerciser_license_debug.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"verdict": {k: v for k, v in gate.items() if k != "seen"},
                           "seen": seen, "connect_response": connection_info},
                          f, indent=2, default=str)
            self.logger.warning(f"[exerciser] raw connect response saved to {path}")
        except Exception as e:                                    # pragma: no cover - defensive
            self.logger.warning(f"[exerciser] could not report license evidence: {e}")

    def check_license(self, ip_address: Optional[str] = None) -> Dict[str, Any]:
        """
        Fallback: re-fetch the connect response and evaluate the gate. Prefer `apply_license`
        with the cached connect response. Returns the gate dict.
        """
        if not self.api_handler:
            self._licensed, self._license_reason = False, "no API handler"
            return {"licensed": False, "reason": self._license_reason, "modules": []}
        ip = ip_address or getattr(self.config_manager, "ip_address", None)
        try:
            from API import ApiName
            # ConnectToTestEquipment's endpoint template is "<ip_address>" (no {} placeholder),
            # so it must be built with endpoint_override, exactly as ConnectionManager.connect does.
            resp = self.api_handler.call_api(ApiName.CONNECT_TO_TEST_EQUIPMENT,
                                             endpoint_override=str(ip))
            data = (resp or {}).get("response", {}).get("data", {})
        except Exception as e:
            self._licensed, self._license_reason = False, f"connect read failed: {e}"
            self.logger.warning(f"[exerciser] license check failed: {e}")
            return {"licensed": False, "reason": self._license_reason, "modules": []}
        return self.apply_license(data)

    # -- mode entry --------------------------------------------------------
    def enter(self, spec_mode: Optional[str]) -> Dict[str, Any]:
        """
        Prepare the session. **This does NOT enter exerciser mode — `start` does that.**

        On `_MPPTPR` and `_MPPTPT`, `PutSelectedQiSpecMode` never touches `TestcaseOrExerciser`
        (only the legacy C3TPR/C3TPT controllers do); the app enters ExerciserMode from inside
        `PutStartExerciser`, via `Start_Service("QiSignalCapture")` — see `_SPEC_MODES` above for
        the full evidence trail. So there is nothing to call here to "enter", and the earlier
        version's PUT was both the wrong endpoint and, when handed a power profile like "MPP25",
        an unaccepted value that the app's `switch` silently dropped.

        `spec_mode` is therefore OPTIONAL and selects a legacy spec/technology only. Left unset
        (the default) nothing is sent. When set it is validated pre-flight against the values that
        app actually accepts, then written and read back.

        Returns `{"entered", "spec_mode", "reported", "reason"}`. The compliance flow
        (`set_project` → PutProjectFolder) restores TestCaseMode, so there is no explicit exit.
        Never raises.
        """
        if not self._licensed:
            return {"entered": False, "spec_mode": spec_mode, "reported": None,
                    "reason": f"exerciser not licensed ({self._license_reason})"}

        if not spec_mode:
            self.logger.info("[exerciser] no spec_mode configured - exerciser mode is entered by "
                             "'start' itself; nothing to send here")
            return {"entered": True, "spec_mode": None, "reported": None,
                    "reason": "mode is entered by start"}

        put = self.op("PutSelectedQiSpecMode", method="PUT", path=[spec_mode])
        if not (put and put.get("response", {}).get("success")):
            self.logger.warning(f"[exerciser] PutSelectedQiSpecMode/{spec_mode} did not succeed")
            return {"entered": False, "spec_mode": spec_mode, "reported": None,
                    "reason": "PutSelectedQiSpecMode did not succeed"}
        if self.dry_run:
            return {"entered": True, "spec_mode": spec_mode, "reported": None, "reason": ""}

        # The PUT is `void` with no `default` in its switch, so a 200 says nothing about whether
        # the selection landed. Confirm it.
        check = self.op("GetSelectedQiSpecMode", method="GET")
        reported = (check or {}).get("response", {}).get("data")
        if reported is not None and str(spec_mode) not in str(reported):
            self.logger.error(f"[exerciser] spec-mode selection not confirmed: asked for "
                              f"{spec_mode!r}, app reports {reported!r}")
            return {"entered": False, "spec_mode": spec_mode, "reported": reported,
                    "reason": f"app reports {reported!r}, not {spec_mode!r}"}
        self.logger.info(f"[exerciser] spec mode set to {spec_mode} (app reports {reported})")
        return {"entered": True, "spec_mode": spec_mode, "reported": reported, "reason": ""}

    def _confirm_started(self) -> Tuple[bool, str]:
        """
        Confirm `start` really started something, by requiring HostState BUSY.

        `PutStartExerciser` is `void`: when the tester is unreachable the app shows a message box,
        sets HostState READY and returns — still HTTP 200. BUSY is the only signal that tells a
        real start from that silent no-op. Polls a few times because the transition is not
        instantaneous. Never raises.
        """
        if self.dry_run:
            return True, "dry run"
        last = None
        for attempt in range(_START_STATE_TRIES):
            last = self._read_app_state()
            if last is not None and _BUSY in str(last).lower():
                return True, str(last)
            if attempt < _START_STATE_TRIES - 1:
                time.sleep(0.5)
        return False, str(last)

    # -- op dispatch -------------------------------------------------------
    def _blocked(self, why: str) -> Dict[str, Any]:
        return {"success": False, "error": why, "response": {"success": False, "error": why}}

    def op(self, action: str, method: str = "PUT",
           path: Optional[List[Any]] = None, data: Any = None) -> Dict[str, Any]:
        """
        Issue one exerciser operation on the current app's variant controller, gated by
        license. ``action`` is the controller action (e.g. ``PutStartExerciser``); ``path`` are
        trailing path segments (e.g. a frequency or an ``isEnabled`` flag); ``data`` is the
        request body for ops that carry one. Returns the handler's result dict, or a blocked
        result if unlicensed / unsupported. Never raises.
        """
        if not self._licensed:
            return self._blocked(f"exerciser not licensed ({self._license_reason})")
        variant = self.variant
        if not variant:
            return self._blocked(f"exerciser not supported for app "
                                 f"'{getattr(self.config_manager, 'app_name', None)}'")
        if not self.api_handler:
            return self._blocked("no API handler")
        service = f"CustomAPIConfiguration_{variant}"
        endpoint = action
        if path:
            endpoint = action + "/" + "/".join(_seg(p) for p in path)
        if self.dry_run:
            # Compose and record, send nothing. Stamped so a dry run can never be read as real.
            body_note = "" if data is None else f" body={len(json.dumps(data))}B"
            self.logger.info(f"[exerciser] DRY RUN {method.upper()} {service}/{endpoint}{body_note}")
            return {"dry_run": True,
                    "request": {"method": method.upper(), "url": f"{service}/{endpoint}",
                                "data": data},
                    "response": {"success": True, "dry_run": True, "data": None}}
        try:
            return self.api_handler.send_request(method.upper(), service, endpoint, data=data)
        except Exception as e:
            self.logger.error(f"[exerciser] op {action} failed: {e}")
            return self._blocked(f"op {action} failed: {e}")

    # -- registry-driven ops -----------------------------------------------
    def _op_spec(self, name: str) -> Optional[Dict[str, Any]]:
        """The routing spec for an op-name on the current app's variant, or None."""
        return _OP_REGISTRY.get(self.variant or "", {}).get(name)

    def _run_op(self, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch a named op via the registry (app-specific route) + the session params
        (dynamic `path` appended, `body` sent). License/variant gating is in `op()`."""
        if not self.variant:
            return self._blocked(f"exerciser not supported for app "
                                 f"'{getattr(self.config_manager, 'app_name', None)}'")
        spec = self._op_spec(name)
        if not spec:
            return self._blocked(f"exerciser op '{name}' not available for {self.variant}")
        params = params or {}
        path = list(spec.get("path", [])) + [str(x) for x in (params.get("path") or [])]
        # Trim segments the controller does not declare. The same op can differ per variant:
        # C3-TPR's `PutModulatorValues` takes none where MPP-TPR's takes /{Freq}, and sending
        # the extra segment there is a 404, not a no-op.
        allowed = _segments_for(self.variant, spec["route"])
        if len(path) > allowed:
            self.logger.debug(f"[exerciser] {spec['route']} takes {allowed} path segment(s) on "
                              f"{self.variant}; dropping {len(path) - allowed}")
            path = path[:allowed]
        return self.op(spec["route"], method=spec.get("verb", "PUT"),
                       path=path or None, data=params.get("body"))

    # -- verification ------------------------------------------------------
    def _read_back(self, spec: Dict[str, Any], op_path: Optional[List[Any]]) -> Any:
        """Issue an op's read-back GET and return the decoded payload (None if it has none)."""
        read = spec.get("read")
        if not read:
            return None
        path = list(read.get("path", []))
        if read.get("use_path"):
            path += list(op_path or [])
        # Don't issue a read-back we know is malformed: without its path segment the GET is a 404,
        # which would be reported as "the app has no data" rather than "we asked wrongly".
        need = _segments_for(self.variant, read["route"])
        if len(path) > need:
            path = path[:need]
        if len(path) < need:
            self.logger.debug(f"[exerciser] skipping read-back {read['route']}: needs {need} path "
                              f"segment(s), {len(path)} available")
            return None
        res = self.op(read["route"], method=read.get("verb", "GET"), path=path or None)
        return (res or {}).get("response", {}).get("data")

    def _verify_op(self, name: str, spec: Dict[str, Any], sent_body: Any,
                   op_path: Optional[List[Any]], before: Any) -> Dict[str, Any]:
        """
        Confirm an op actually took effect by reading state back.

        The PUT's 200 proves nothing — the endpoint is `void` inside a try/catch, so it answers 200
        whether the operation ran, threw, or bound an empty model. Only a read-back distinguishes
        those. Never raises: a failed read is reported "unavailable", not as an op failure.

        Args:
            name: Op name (for messages)
            spec: Its registry entry
            sent_body: The body we PUT, or None
            op_path: The op's own path segments (repeated on the GET when `use_path`)
            before: The read-back taken before the write, for tier B evidence

        Returns:
            {"state": verified|mismatch|unavailable, "tier": A|B|C, "detail": str,
             "before": ..., "after": ...}
        """
        read = spec.get("read")
        tier = (read or {}).get("tier", "C")
        if not read:
            return {"state": "unavailable", "tier": "C",
                    "detail": "the app offers no read-back for this operation",
                    "before": before, "after": None}
        try:
            after = self._read_back(spec, op_path)
        except Exception as e:                                    # pragma: no cover - defensive
            self.logger.warning(f"[exerciser] could not read '{name}' back to verify: {e}")
            return {"state": "unavailable", "tier": tier, "detail": f"read-back failed: {e}",
                    "before": before, "after": None}

        if after is None:
            return {"state": "unavailable", "tier": tier,
                    "detail": "read-back returned no data", "before": before, "after": None}
        mismatched, compared = _compare_fields(sent_body, after)

        if tier != "A" or not isinstance(sent_body, dict):
            # Tier B makes no field-level CLAIM — but the before/after snapshots we already take
            # are real evidence, and used to be discarded. Report whether the app's state actually
            # moved, and whether the read-back happens to echo what we sent.
            #
            # Still never fails a step: writing identical values legitimately changes nothing, and
            # some GetDefault* endpoints look like they serve factory content rather than current
            # state. Enforcing here would halt a session that is working (proved at the bench
            # 2026-08-17, where the packet capture showed the sequence running correctly).
            state, detail = "unavailable", "snapshot only (no field-level comparison available)"
            if before is not None:
                changed = _changed_keys(before, after)
                if changed:
                    state = "changed"
                    detail = f"app state changed after this write ({', '.join(changed[:5])})"
                else:
                    state = "unchanged"
                    detail = "app reports identical state before and after this write"
            if compared:
                detail += ("; read-back echoes every field sent" if not mismatched
                           else f"; read-back differs on {len(mismatched)} of {compared} field(s): "
                                f"{'; '.join(mismatched[:3])}")
            return {"state": state, "tier": tier, "detail": detail,
                    "before": before, "after": after,
                    "echoes_sent": (not mismatched) if compared else None}

        if mismatched:
            return {"state": "mismatch", "tier": tier,
                    "detail": "; ".join(mismatched[:5]), "before": before, "after": after,
                    "echoes_sent": False}
        return {"state": "verified", "tier": tier, "detail": "read-back matches",
                "before": before, "after": after, "echoes_sent": True}

    # -- pre-flight --------------------------------------------------------
    def validate_session(self, session_cfg: Dict[str, Any],
                         steps: List[Dict[str, Any]]) -> List[str]:
        """
        Check a session before anything is sent, so a config error never reaches the hardware.

        Args:
            session_cfg: The run-controls config
            steps: The resolved step list ({op, body, path} each)

        Returns:
            A list of problems; empty means the session is runnable.
        """
        problems: List[str] = []
        variant = self.variant or ""
        known = _OP_REGISTRY.get(variant, {})
        body_required = _BODY_REQUIRED.get(variant, set())

        for index, step in enumerate(steps):
            name = step.get("op")
            if name not in known:
                problems.append(f"step {index} '{name}': not a known {variant} operation")
                continue
            if name in body_required and not isinstance(step.get("body"), dict):
                problems.append(f"step {index} '{name}': requires a request body but none was "
                                f"supplied - the app would answer 200 having done nothing")

            # A route missing its path segment is not a soft failure - it addresses a URL that does
            # not exist and comes back 404. Caught here so the session never starts, rather than
            # dying mid-run as the first MPP-TPR attempt did on `PutModulatorValues`.
            route = known[name].get("route", "")
            need = _segments_for(self.variant, route)
            have = len(known[name].get("path", []) or []) + len(step.get("path") or [])
            if have < need:
                problems.append(f"step {index} '{name}': route '{route}' needs {need} path "
                                f"segment(s) but only {have} supplied - the app would answer 404")
            for seg in (step.get("path") or []):
                if isinstance(seg, (dict, list)):
                    problems.append(f"step {index} '{name}': path segment {seg!r} is not a scalar")

        for channel in (session_cfg.get("channels") or []):
            if channel not in _MEASUREMENT_CHANNELS:
                problems.append(f"measurement channel '{channel}' is not one the app records")

        # spec_mode is optional (start enters exerciser mode). If one IS given it must be a value
        # this app's switch handles - anything else falls through silently behind a 200, which is
        # exactly how a power profile ("MPP25") got sent here and quietly did nothing.
        spec_mode = session_cfg.get("spec_mode")
        accepted = _SPEC_MODES.get(variant, ())
        if spec_mode and str(spec_mode) not in accepted:
            problems.append(f"spec_mode '{spec_mode}' is not one {variant} accepts - it would be "
                            f"silently ignored behind an HTTP 200. Accepted: {list(accepted)}. "
                            f"Leave it unset unless you need a legacy spec/technology; exerciser "
                            f"mode itself is entered by 'start'")

        # A started exerciser runs until the operator ends it, and the hold phase reads that from
        # the console. With no console there is no way to end it, so refuse here - before anything
        # is sent - instead of starting the hardware and then waiting on a keypress that can never
        # come. A dry run starts nothing, so it is exempt.
        if (not self.dry_run and any(s.get("op") == "start" for s in steps)
                and not _stdin_is_interactive()):
            problems.append("this session starts the exerciser, which then runs until you press "
                            "Enter - but no console is attached to read that keypress. Run it from "
                            "a terminal, or set common.exerciser.dry_run to true")
        return problems

    # Convenience wrappers (also used directly): route through the registry so they are
    # variant-correct (e.g. reset differs MPP-TPR vs TPT).
    def start(self) -> Dict[str, Any]:
        """Start the exerciser (EX-*02)."""
        return self._run_op("start")

    def stop(self) -> Dict[str, Any]:
        """Stop the exerciser (GET GetStopExerciser)."""
        return self._run_op("stop")

    def reset(self) -> Dict[str, Any]:
        """Reset the exerciser — variant-specific (MPP-TPR GetLoadRamp/{true}, TPT PutResetFWExerciser)."""
        return self._run_op("reset")

    def ask_modulation(self, freq: Any) -> Dict[str, Any]:
        """Change ASK modulation frequency (EX-R09, PutModulatorValues/{Freq})."""
        return self._run_op("ask_modulation", {"path": [freq]})

    def vrect_loop(self, enabled: bool) -> Dict[str, Any]:
        """Enable/disable the Vrect loop (EX-R06, PutVoltageLoop/{isEnabled}). MPP-TPR only."""
        return self._run_op("vrect_loop", {"path": [str(bool(enabled)).lower()]})

    # -- session runner ----------------------------------------------------
    def _run_step(self, step: Dict[str, Any], verify: str) -> Dict[str, Any]:
        """Run one step and verify it. Returns the record appended to the session results."""
        name = step["op"]
        spec = self._op_spec(name) or {}
        body = step.get("body")
        path = list(step.get("path") or [])

        before = None
        if verify != "off" and not self.dry_run and spec.get("read"):
            try:
                before = self._read_back(spec, path)
            except Exception:                                     # pragma: no cover - defensive
                before = None

        res = self._run_op(name, {"path": path, "body": body})
        ok = bool(res and res.get("response", {}).get("success"))

        record: Dict[str, Any] = {"op": name, "group": step.get("group"), "success": ok,
                                  "response": res}
        if self.dry_run:
            record["dry_run"] = True
            record["verified"] = "skipped (dry run)"
            return record

        if ok and verify != "off":
            check = self._verify_op(name, spec, body, path, before)
            record["verified"] = check["state"]
            record["verify_tier"] = check["tier"]
            record["verify_detail"] = check["detail"]
            record["echoes_sent"] = check.get("echoes_sent")
            record["read_before"], record["read_after"] = check["before"], check["after"]
            if check["state"] == "mismatch":
                msg = (f"[exerciser] '{name}' returned 200 but the read-back shows it did NOT take "
                       f"effect - {check['detail']}")
                if verify == "strict":
                    self.logger.error(msg)
                    record["success"] = False          # strict: a silent no-op is a failure
                else:
                    self.logger.warning(msg)
        else:
            record["verified"] = "not attempted"
        return record

    # -- hold phase --------------------------------------------------------
    def _read_app_state(self) -> Optional[str]:
        """
        The app's own state, read off the `App` service rather than the exerciser controller.

        Used as the hold's liveness signal: it is the one read that works on every build, so a
        sweep whose exerciser reads are all unavailable can still tell a live app from a dead
        link. Handles the field rename (`appState` -> `ApplicationState`) in newer builds.
        Never raises.
        """
        if not self.api_handler or self.dry_run:
            return None
        try:
            res = self.api_handler.send_request("GET", "App", "GetAppState")
        except Exception:
            return None
        if not (res and res.get("response", {}).get("success")):
            return None
        data = res.get("response", {}).get("data")
        if isinstance(data, dict):
            value = ci_get(data, "ApplicationState", ci_get(data, "appState", None))
            return None if value is None else str(value)
        return None if data is None else str(data)

    def _read_packet_count(self) -> Optional[int]:
        """
        Cumulative packets the app has logged this session, via its **paged** packet endpoint.

        `GetCCLinePackets(stopTime, lastPacketIndex)` returns the batch AFTER `lastPacketIndex`,
        capped at 100. Calling it with no parameters — which this did at first — therefore returns
        the same first batch every time: the count pins at 100 and reports `+0` for the rest of the
        session whether the tester is thriving or dead. Seen live 2026-08-17
        (`00:05 packets=100 (+100)` then `+0` forever while the session was in fact producing
        1,216 packets).

        So resume from the highest `Index` seen and accumulate. Growth is then a true "data is
        still arriving" signal, on both apps and both transports. Never raises.
        """
        if not self.api_handler or self.dry_run:
            return None
        try:
            service, route = _MONITOR_PACKETS
            res = self.api_handler.send_request(
                "GET", service, route,
                params={"stopTime": 0, "lastPacketIndex": self._pkt_index})
        except Exception:
            return None
        if not (res and res.get("response", {}).get("success")):
            return None

        batch = res.get("response", {}).get("data")
        if not isinstance(batch, list):
            # A build that answers with a wrapper object rather than a bare list: fall back to the
            # shared shape-tolerant count and make no paging claim.
            counted = _packet_count(batch)
            return self._pkt_total if counted is None else counted
        if not batch:
            return self._pkt_total

        # Count by the HIGHEST `CClinePacket.Index` seen, not by adding up batch sizes. Adding
        # sizes double-counts whenever the cursor fails to advance - which both inflates the
        # figure and hides the very stall this is meant to reveal, since re-reading the same
        # packets would still look like growth. The index is monotonic, so re-reading a batch
        # leaves the count exactly where it was, which is the honest answer.
        highest = 0
        for packet in batch:
            index = ci_get(packet, "Index", None) if isinstance(packet, dict) else None
            if isinstance(index, int) and index > highest:
                highest = index
        if highest:
            self._pkt_index = max(self._pkt_index, highest)
            self._pkt_total = self._pkt_index
            return self._pkt_total

        # No index on this build: the best available is to add batch sizes and step the cursor,
        # accepting that a non-advancing cursor cannot be detected here.
        self._pkt_total += len(batch)
        self._pkt_index += len(batch)
        return self._pkt_total

    def _monitor_sample(self, elapsed: float) -> Dict[str, Any]:
        """
        Take one sweep of live readings while the exerciser runs.

        Returns ``{"t", "state", "reads", "packets", "packets_delta", "ok", "summary"}``. ``ok`` is
        False only when the WHOLE sweep came back empty — that, repeated, is what identifies a lost
        tester link. A single route being unavailable on a given build is normal and recorded as
        None.
        """
        reads: Dict[str, Any] = {}
        any_ok = False
        for label, route in _MONITOR_READS.get(self.variant or "", ()):
            res = self.op(route, method="GET")
            ok = bool(res and res.get("response", {}).get("success"))
            reads[label] = (res or {}).get("response", {}).get("data") if ok else None
            any_ok = any_ok or ok

        state = self._read_app_state()
        if state is not None:
            any_ok = True

        packets = self._read_packet_count()
        delta = None
        if packets is not None:
            any_ok = True
            if self._last_packets is not None:
                delta = packets - self._last_packets
            self._last_packets = packets

        parts = [f"state={state if state is not None else '?'}"]
        if packets is not None:
            parts.append(f"packets={packets}" + (f" (+{delta})" if delta is not None else ""))
        parts += [f"{label}={_fmt_reading(value)}" for label, value in reads.items()]
        return {"t": elapsed, "state": state, "reads": reads, "packets": packets,
                "packets_delta": delta, "ok": any_ok, "summary": "  ".join(parts)}

    def _should_hold(self, results: List[Dict[str, Any]]) -> Tuple[bool, str]:
        """
        Whether to keep the session open once the steps have run — and, if not, why not.

        Only when the exerciser is genuinely running: a dry run started nothing, and a session
        whose `start` step failed (or that has no `start` step at all) has nothing to hold open.
        Waiting in either case would just hang for no reason.
        """
        if self.dry_run:
            return False, "dry run - nothing was started"
        for record in results:
            if record.get("op") == "start":
                if record.get("success"):
                    return True, ""
                return False, "the start step failed, so nothing is running"
        return False, "the session has no start step"

    def _hold_session(self) -> Dict[str, Any]:
        """
        Keep the session open while the exerciser runs, sampling live readings.

        The exerciser is not a test list — the app never ends it — so this is what gives the
        session its duration. Ends on any of three things:

          * the operator presses Enter (the normal case),
          * Ctrl+C,
          * `_MONITOR_FAIL_LIMIT` sweeps in a row come back empty, meaning the tester link is gone.

        Returns the record appended to the session results. Never raises: whatever happens here,
        `run_session`'s `finally` must still get to issue stop.
        """
        started_at = time.monotonic()
        samples: List[Dict[str, Any]] = []
        truncated = False
        fails = 0
        done = self._stop_requested

        # Enter is read on a daemon thread so the monitor keeps sampling while we wait. Daemon, so
        # a thread still parked on stdin can never hold up interpreter shutdown.
        #
        # A real Enter returns "\n"; a console read broken by Ctrl+C (or a closed stdin) returns
        # "". Telling those apart matters: without it a Ctrl+C was reported as "operator", so the
        # log claimed the operator ended the session when they had actually interrupted it.
        def _wait_for_enter() -> None:
            line = ""
            try:
                line = sys.stdin.readline()
            except Exception:                                     # pragma: no cover - defensive
                line = ""
            self._request_stop("operator" if line not in ("", None) else "input closed")

        threading.Thread(target=_wait_for_enter, name="exerciser-hold", daemon=True).start()
        self.logger.warning("[exerciser] RUNNING - press Enter in THIS console window to stop "
                            "(Ctrl+C also stops it cleanly)")

        try:
            # Sample first, THEN check for the stop request, so even a session ended immediately
            # still records one reading of what the exerciser was doing.
            sweeps = 0
            while True:
                sample = self._monitor_sample(round(time.monotonic() - started_at, 1))
                if len(samples) < _MAX_SAMPLES:
                    samples.append(sample)
                elif not truncated:
                    truncated = True
                    self.logger.warning(f"[exerciser] over {_MAX_SAMPLES} samples - keeping the "
                                        f"first {_MAX_SAMPLES}; the session continues")
                self.logger.info(f"[exerciser] {_fmt_elapsed(sample['t'])}  {sample['summary']}")

                # The prompt scrolls out of sight behind the readings, so say it again now and
                # then - otherwise a session looks like it has no way out.
                sweeps += 1
                if sweeps % _PROMPT_EVERY == 0 and not done.is_set():
                    self.logger.warning("[exerciser] still RUNNING - press Enter in this console "
                                        "window (or Ctrl+C) to stop")

                if sample["ok"]:
                    fails = 0
                else:
                    fails += 1
                    if fails >= _MONITOR_FAIL_LIMIT:
                        self._request_stop("link lost")
                        self.logger.error(f"[exerciser] {fails} monitor sweeps in a row came back "
                                          f"empty - the tester link looks down; ending the session")
                        break
                if done.is_set():
                    break
                done.wait(_MONITOR_INTERVAL_S)
        except KeyboardInterrupt:            # only if the guard could not be installed
            self._request_stop("interrupted")
            self.logger.warning("[exerciser] interrupted - stopping the exerciser")
        except Exception as e:                                    # pragma: no cover - defensive
            self._request_stop(f"monitor error: {e}")
            self.logger.error(f"[exerciser] hold ended on an unexpected error: {e}")

        reason = self._stop_reason or "operator"
        held = round(time.monotonic() - started_at, 1)
        self.logger.info(f"[exerciser] hold ended after {_fmt_elapsed(held)} ({reason})")
        return {"op": "hold", "success": reason in ("operator", "interrupted"),
                "reason": reason, "held_seconds": held, "samples": samples,
                "samples_truncated": truncated, "verified": "n/a (hold)"}

    def run_session(self, session_cfg: Optional[Dict[str, Any]],
                    steps: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Run a full exerciser session.

        license-gate → pre-flight validation → `enter(spec_mode)` → each step in order, each
        verified by read-back → **hold the session open while it runs** → **stop in a `finally`**.

        Stop is NOT a step in the list. It runs on every exit path — normal end, failed step,
        verify mismatch or exception — because leaving the exerciser running drives real power into
        a DUT, and because the app only writes the session's capture folder when stop is called.

        The hold (`_hold_session`) is what gives the session its duration: the app never ends an
        exerciser run by itself, so without it stop would follow start almost immediately. It is
        skipped when nothing is actually running — a dry run, a failed `start`, or no `start` step.

        Args:
            session_cfg: Run controls (spec_mode, channels, dry_run, verify, stop_on_error)
            steps: Resolved steps ({op, body, path, group}); usually built from the exported
                   sequence file by `exerciser_sequence.py`

        Returns:
            {"success", "results": [...], "capture": {...}, "dry_run": bool}
        """
        if not self._licensed:
            return self._blocked(f"exerciser not licensed ({self._license_reason})")
        if not self.variant:
            return self._blocked(f"exerciser not supported for app "
                                 f"'{getattr(self.config_manager, 'app_name', None)}'")

        session_cfg = session_cfg or {}
        steps = list(steps or [])
        self.dry_run = bool(session_cfg.get("dry_run", False))
        self._stop_requested = threading.Event()      # fresh per session
        self._stop_reason = None
        self._last_packets = None
        self._pkt_index = self._pkt_total = 0
        verify = str(session_cfg.get("verify", "strict")).strip().lower()
        stop_on_error = session_cfg.get("stop_on_error", True)
        spec_mode = session_cfg.get("spec_mode")
        results: List[Dict[str, Any]] = []

        # Measurement channels ride on `start`. Omit them and the app records only rectified
        # voltage + current, and what was not requested cannot be recovered afterwards.
        channels = session_cfg.get("channels") or None
        if channels:
            for step in steps:
                if step.get("op") == "start" and step.get("body") is None:
                    step["body"] = list(channels)

        # One step list is written once and used against every application, but each controller
        # exposes a different set of ops: C3TPT has no `reset` (PutResetFWExerciser lives on the
        # MPPTPT controller), MPP-TPR has no `save_sequence`, and so on. Drop what THIS controller
        # cannot do and say so, instead of failing a whole session over a step that was never
        # applicable to it. The alternative - hand-editing the shared `steps` config per app -
        # silently breaks the next run on a different app.
        known_ops = _OP_REGISTRY.get(self.variant, {})
        runnable, unsupported = [], []
        for step in steps:
            op = step.get("op")
            if op in known_ops:
                runnable.append(step)
            elif op in _ALL_OPS:
                unsupported.append(step)          # real op, absent on THIS controller
            else:
                runnable.append(step)             # not an op anywhere - let validation reject it
        for step in unsupported:
            op = step.get("op")
            self.logger.info(f"[exerciser] '{op}' has no endpoint on {self.variant} - skipped")
            results.append({"op": op, "success": True, "skipped": True,
                            "detail": f"not available on {self.variant}"})
        steps = runnable

        problems = self.validate_session(session_cfg, steps)
        if problems:
            for p in problems:
                self.logger.error(f"[exerciser] config: {p}")
            return {"success": False, "error": f"session config rejected: {problems[0]}",
                    "problems": problems, "results": results, "dry_run": self.dry_run}

        if self.dry_run:
            self.logger.warning("[exerciser] DRY RUN - composing requests, sending nothing")

        ent = self.enter(spec_mode)
        results.append({"op": "enter", "success": ent["entered"], "detail": ent})
        if not ent["entered"]:
            return {"success": False,
                    "error": f"could not enter exerciser mode: {ent.get('reason')}",
                    "results": results, "dry_run": self.dry_run}

        started = False
        capture: Dict[str, Any] = {}
        # Ctrl+C becomes a stop REQUEST for the whole session, teardown included. Without this it
        # is a BaseException that skips `except Exception` and can land inside the stop call - as
        # it did at the bench on 2026-08-17, aborting stop after 29.5 s and leaving the exerciser
        # running with its capture folder never written.
        with _InterruptGuard(self.logger, lambda: self._request_stop("interrupted")) as guard:
            try:
                started = True
                for step in steps:
                    record = self._run_step(step, verify)
                    # `start` is void and no-ops silently when the tester is unreachable, so its 200
                    # is worth nothing. HostState BUSY is the proof that something is running.
                    if record["op"] == "start" and record["success"]:
                        ok_started, state_seen = self._confirm_started()
                        record["app_state"] = state_seen
                        if not ok_started:
                            record["success"] = False
                            record["verified"] = "mismatch"
                            record["verify_detail"] = (f"start returned 200 but the app reports "
                                                       f"{state_seen!r}, not BUSY - nothing started")
                            self.logger.error(f"[exerciser] start did not take effect: app reports "
                                              f"{state_seen!r}, not BUSY (tester unreachable?)")
                    results.append(record)
                    state = record.get("verified")
                    self.logger.info(f"[exerciser] '{record['op']}' -> "
                                     f"{'ok' if record['success'] else 'FAILED'} ({state})")
                    if not record["success"] and stop_on_error:
                        self.logger.warning(f"[exerciser] stopping session after "
                                            f"'{record['op']}'")
                        break
                else:
                    # Every step ran. The exerciser has no end of its own, so the session is held open
                    # here; without this we would fall straight into `finally` and stop it a second
                    # after starting it. The hold is INSIDE the try, so stop is still guaranteed.
                    hold, why = self._should_hold(results)
                    if hold:
                        results.append(self._hold_session())
                    else:
                        self.logger.info(f"[exerciser] not holding the session open: {why}")
            finally:
                if started:
                    capture = self._finalize_session(results)

        ok = all(r.get("success", True) for r in results)
        interrupted = guard.interrupted or self._stop_reason == "interrupted"
        return {"success": ok, "results": results, "capture": capture,
                "dry_run": self.dry_run, "interrupted": interrupted}

    def _finalize_session(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Always-run teardown: stop the exerciser and collect its capture folder.

        Runs from `run_session`'s `finally`, so it happens after a failure or an exception too.
        Stop is the only exerciser endpoint that returns anything — the path of the folder the app
        just wrote with the packet log, packet CSV, debug log and waveform files. Never raises.
        """
        info: Dict[str, Any] = {}
        try:
            res = self._run_op("stop")
            ok = bool(res and res.get("response", {}).get("success"))
            payload = (res or {}).get("response", {}).get("data")
            results.append({"op": "stop", "success": ok, "response": res,
                            "verified": "n/a (teardown)"})
            self.logger.info(f"[exerciser] stop -> {'ok' if ok else 'FAILED'}")

            folder = payload if isinstance(payload, str) else None
            info["app_folder"] = folder
            if folder and _STOP_NO_COMMS in folder.lower():
                self.logger.error("[exerciser] stop reports the tester link was down - this "
                                  "session captured no data")
                info["captured"] = False
                results[-1]["success"] = False
            elif folder:
                info["captured"] = True
                info["copied_to"] = self._collect_capture(folder)
        except BaseException as e:                                # noqa: BLE001 - deliberate
            # BaseException, not Exception. KeyboardInterrupt is not an Exception, so the narrower
            # clause let a Ctrl+C tear straight through this and abandon the stop mid-flight
            # (bench, 2026-08-17: stop aborted at 29.5 s, exerciser left running, capture lost).
            # Teardown must survive anything: the tester keeps driving the DUT until it is stopped.
            self.logger.error(f"[exerciser] teardown hit {type(e).__name__}: {e} - the exerciser "
                              f"may still be running; stop it from the app UI")
            info["error"] = f"{type(e).__name__}: {e}"
        return info

    def _collect_capture(self, app_folder: str) -> Optional[str]:
        """
        Copy the app's exerciser capture folder into Runtime_Capture, like a compliance run.

        The app reuses its own Report directory, so a later session would overwrite this evidence.
        Reporting only — never raises, never fails a session.
        """
        if self.dry_run or not self.capture_dir:
            return None
        try:
            if not os.path.isdir(app_folder):
                self.logger.warning(f"[exerciser] capture folder not found on disk: {app_folder}")
                return None
            target = os.path.join(self.capture_dir, os.path.basename(app_folder.rstrip("\\/")))
            os.makedirs(self.capture_dir, exist_ok=True)
            shutil.copytree(app_folder, target, dirs_exist_ok=True)
            self.logger.info(f"[exerciser] capture copied to {target}")
            return target
        except Exception as e:
            self.logger.warning(f"[exerciser] could not copy the capture folder: {e}")
            return None
