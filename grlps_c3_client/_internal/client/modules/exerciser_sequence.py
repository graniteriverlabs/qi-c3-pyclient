# client/modules/exerciser_sequence.py
"""
Parse the app's exported exerciser sequence file into the models its API expects.

The C3 apps can export a complete exerciser setup. One per power profile is seeded by
c3-init as ExerciserSequence[-PROFILE].json; an export made in the UI is named
ExerciserSequenceConfigurationData-*.json and loads the same way.
That single file carries everything a session needs — the Qi packet sequence, the controller
hardware settings and the phase timings — so the user sets the exerciser up in the app UI, exports
it, and drops it in, exactly as they do with an ESDF. Nothing here is hand-typed.

The export and the API disagree on ONE thing, and this module exists to bridge it: the file holds a
flat packet list per sequence with a ``packetPhase`` on each packet, whereas
``PutPacketInformationMppT{p}tModel`` wants those packets already grouped into per-phase lists:

    ConfigureSequence[i]  ->  freqModels[i] = {freqName, PhaseModels}
        packets bucketed by packetPhase into
        TPT  QiTptControllerInfoModel {PingPhase, ConfigPhase, PowerTxPhase,
                                       NegotiationPhase, CalibrationPhase, RunTime}
        TPR  QiControllerInfoModel    {          ConfigPhase, PowerTxPhase,
                                       NegotiationPhase, CalibrationPhase, RunTime}

Two independent checks say that mapping is right: the TPR model has no ``PingPhase`` and the TPR
export contains no "Ping Phase" packets (a PRx emits no pings), and the packet field names match the
C# models one-for-one, so packet objects pass through verbatim.

``ConfigureSequence`` itself comes in two shapes. The MPP applications write one sub-array per
frequency model (a list of lists); the C3 applications write a single sequence flat (the packet
list itself). ``_sequences`` normalises both to a list of packet lists, so everything below sees
one shape.

Packets are otherwise NOT rewritten. Whatever the app exported is what the app gets back — this
module never invents or defaults a packet field.
"""
import io
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

#: The sections every exerciser export carries. Used to reject a file that is not an exerciser
#: export (e.g. an ESDF pointed at the wrong config key) before anything is sent.
_REQUIRED_SECTIONS = (
    "FileInfo", "QiSpecification", "PopupMessage",
    "ConfigureSequence", "SendInstant", "ControllerConfigurations", "PhasesSettings",
)

#: `PowerProfile` is NOT required: the C3-TPR export (FileInfo version 2.0.0.7) omits it, since
#: that application's profile is not part of the exerciser configuration. It is read only for the
#: descriptive log line, never to build a step, so a file without it is still perfectly usable.
#: Demanding it rejected a valid C3-TPR export outright.
_OPTIONAL_SECTIONS = ("PowerProfile",)

#: Exported ``packetPhase`` -> the model's phase-list name. Keyed lower-case so a casing or
#: spacing change in a future export does not silently drop packets.
_PHASE_BUCKET = {
    "ping phase": "PingPhase",
    "id/config phase": "ConfigPhase",
    "config phase": "ConfigPhase",
    "negotiation phase": "NegotiationPhase",
    "power tx phase": "PowerTxPhase",
    "calibration phase": "CalibrationPhase",
    "runtime": "RunTime",
    "run time": "RunTime",
}

#: Phase lists each variant's controller-info model actually has. TPR has no PingPhase.
_BUCKETS = {
    "MPPTPT": ("PingPhase", "ConfigPhase", "PowerTxPhase",
               "NegotiationPhase", "CalibrationPhase", "RunTime"),
    "MPPTPR": ("ConfigPhase", "PowerTxPhase",
               "NegotiationPhase", "CalibrationPhase", "RunTime"),
}

#: Frequency-model names used when the app's own default model has not been read. The app's
#: `GetDefaultPacketsMppTptModel` returns freqName "128" and "360"; prefer those at runtime and
#: treat these as the fallback only.
_DEFAULT_FREQ_NAMES = ("128", "360")

#: ControllerConfigurations group -> the op that applies it. A group with no entry here has no
#: known endpoint and is reported as unmapped rather than dropped silently.
_CONTROLLER_GROUP_OPS = {
    "MPPTPT": {
        "tptCoilConfiguration":        "coil_type",
        "inverterConfiguration":       "inverter_config",
        "pfoOffsetConfiguration":      "pfo_offset",
        "supportedPowerModesConfig":   "power_mode",
        "modeXCAPConfig":              "modexcap",
        "dPLossOffsetConfig":          "dploss_offset",
        "cloakConfig":                 "cloak",
        "configTuningClamp_DataModel": "tuning_clamp",
        "calibPointsConfig":           "calib_points",
        # gainConfig: no PUT endpoint found on the TPT controller -> reported unmapped.
    },
    "MPPTPR": {
        "ModulatorConfiguration": "ask_modulation",
        "LoadRampConfiguration":  "load_ramp",
        "SetLoad":                "set_load",
        "TPRConfiguration":       "vrect",
        "DUTType":                "pp_values",
        "cloakPhase":             "cloak_phase",
    },
    # C3-TPR exports use the SAME group names as MPP-TPR and its controller exposes the same
    # ops, so the mapping is identical minus `cloakPhase`: that controller has no cloak routes,
    # so leaving it out makes an unexpected cloak group report as unmapped instead of 404ing.
    # C3TPT (TPT in BPP/EPP firmware) exposes only three of the TPT groups. The rest -
    # cloak, tuning clamp, calib points, power modes, modeXCAP, dPLoss, gain - have no route
    # on that controller, so they report unmapped instead of 404ing.
    "C3TPT": {
        "tptCoilConfiguration":  "coil_type",
        "inverterConfiguration": "inverter_config",
        "pfoOffsetConfiguration": "pfo_offset",
    },
    "C3TPR": {
        "ModulatorConfiguration": "ask_modulation",
        "LoadRampConfiguration":  "load_ramp",
        "SetLoad":                "set_load",
        "TPRConfiguration":       "vrect",
        "DUTType":                "pp_values",
    },
}


#: ⚠ The app's own option lists for the cloak-phase fields, copied from the UI bundle
#: (`AppFiles/wwwroot/static/js/main.*.chunk.js`, the `JSON.parse('[{"configureCloakPhase"...')`
#: literal). **The POSITION in each list is the value the API takes** — the app's UI computes
#: `fields.indexOf(selectedLabel)` and sends that integer.
#:
#: These must be copied because the export stores the human label ("Generic") while the endpoints
#: take a number, and **the app exposes no GET that would tell us the lists at runtime** (there is
#: no Get_Cloak* route on either controller). That makes this a cached copy of app data, and if a
#: future release reorders a list our copy would silently send the wrong setting — the endpoints
#: are `void` with no read-back, so nothing would notice. **Unit U56 therefore re-reads these lists
#: out of the installed app and fails if they no longer match.**
#: **These are the RECEIVER-emulation controller's lists (MPPTPR), and only its.** It is the one
#: controller whose cloak endpoints take a position: `Put_CloakEntryReasonConfig`,
#: `Put_CloakExitTypeConfig` and `Put_CloakIllegalPacketConfig`. The transmitter controller has a
#: single `PutCloakingFeature` and no position anywhere, so it never uses these - which is why one
#: list is enough, and why it must be the receiver's.
#:
#: The application's files hold TWO cloak option sets: the receiver's (8 entry reasons, with the
#: 41-entry illegal-packet list) and the transmitter's (5 entry reasons, no illegal packets - it is
#: missing "Forced" and "PTx Initiated", so every later position differs). Take the receiver's.
#:
#: ⚠ Position 7 is "Foreign Object Detection", read from the MP-TPR application's own files. It
#: was briefly changed to "Reserved" on 2026-10-08 and changed back the same day: at that moment
#: the MP-TPR install had no interface files, so the comparison fell through to the MP-TPT
#: install, **a different product**, which has "Reserved" there. The lesson is in the check, not
#: here - these lists must be compared against the application they belong to.
#:
#: The wrong value also survived a live test, which is the warning worth remembering: sending it
#: returned HTTP 200, because 7 is a valid position either way. The application confirms the
#: NUMBER, never the name, so a 200 cannot tell a right label from a wrong one.
_CLOAK_ENTRY_REASONS = (
    "Generic", "Forced", "Thermally Constrained", "Insufficient Power",
    "Coex Mitigation", "End Of Charge", "PTx Initiated", "Foreign Object Detection",
)
_CLOAK_EXIT_TYPES = (
    "Natural Exit", "Illegal Packet Exit", "Ping TimeOut Exit", "Detect Timeout Exit", "Terminate",
)

#: The exit type that makes the illegal-packet field apply. The app's UI disables that dropdown for
#: every other exit type and does not send it, so neither do we.
_CLOAK_EXIT_ILLEGAL_PACKET = "Illegal Packet Exit"


class SequenceError(ValueError):
    """The sequence file is not a usable exerciser export."""


#: The folder an application's sequences live in, under its input folder.
SEQUENCE_DIR = "exerciser"


def sequence_path(inputs: str, value: str) -> str:
    """
    The file a sequence setting names, relative to the application's input folder.

    A bare file name that is not there is looked for in ``exerciser\\``, so a configuration
    written before the sequences had their own folder still finds them after they move.
    """
    path = os.path.join(inputs, value)
    if os.path.isfile(path) or os.path.dirname(value):
        return path
    moved = os.path.join(inputs, SEQUENCE_DIR, value)
    return moved if os.path.isfile(moved) else path


def available_sequences(inputs: str) -> List[str]:
    """
    The sequence files one application has, relative to its input folder: every ``.json`` in
    ``exerciser\\``, then any still loose beside the configuration files.
    """
    folder = os.path.join(inputs, SEQUENCE_DIR)
    found = []
    if os.path.isdir(folder):
        found = [os.path.join(SEQUENCE_DIR, name) for name in sorted(os.listdir(folder))
                 if name.lower().endswith(".json") and os.path.isfile(os.path.join(folder, name))]
    if os.path.isdir(inputs):
        found += [name for name in sorted(os.listdir(inputs))
                  if name.startswith("ExerciserSequence") and name.lower().endswith(".json")
                  and os.path.isfile(os.path.join(inputs, name))]
    return found


def load_sequence_file(path: str) -> Dict[str, Any]:
    """
    Read an exported sequence file and return its single configuration object.

    The export is a one-element JSON array. It contains non-cp1252 characters, so it must be read
    as UTF-8 explicitly.

    Args:
        path: Path to an exerciser sequence exported by the application

    Returns:
        The configuration object (the array's single element)

    Raises:
        SequenceError: file unreadable, wrong shape, or missing required sections
    """
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise SequenceError(f"exerciser sequence file not found: {path}")
    except Exception as e:
        raise SequenceError(f"could not read exerciser sequence file {path}: {e}")

    doc = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(doc, dict):
        raise SequenceError(f"{path}: expected a JSON object (or a one-element array), got "
                            f"{type(raw).__name__}")
    missing = [s for s in _REQUIRED_SECTIONS if s not in doc]
    if missing:
        raise SequenceError(f"{path}: not an exerciser sequence export — missing section(s): "
                            f"{', '.join(missing)}")
    return doc


def _first_value(doc: Dict[str, Any], section: str, key: str, default: Any = None) -> Any:
    """Read `doc[section][0][key]`; every scalar section is a one-element list of one object."""
    block = doc.get(section)
    if isinstance(block, list) and block and isinstance(block[0], dict):
        return block[0].get(key, default)
    return default


def _sequences(doc: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    """
    Normalise ``ConfigureSequence`` to a list of packet lists.

    Two export shapes exist and both are valid. The MPP applications export one sub-array per
    frequency model, so the section is a list of lists. The C3 applications (C3-TPR and C3-TPT)
    export a SINGLE sequence and flatten it — the section IS the packet list.

    Filtering the section on ``isinstance(s, list)`` silently discarded the flat shape, so every
    C3 exerciser session sent an empty packet model behind an HTTP 200 and the packets never
    reached the application (found 2026-09-28 from an 18-byte PutTPTPacketInformation body).
    """
    section = doc.get("ConfigureSequence") or []
    if not isinstance(section, list):
        return []
    nested = [s for s in section if isinstance(s, list)]
    if nested:
        return nested
    flat = [p for p in section if isinstance(p, dict)]
    return [flat] if flat else []


def describe(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Summarise the export for logging and for cross-checking against the licence.

    Returns:
        {"file_version", "qi_spec", "power_profile", "popup_message", "sequences", "packets"}
    """
    sequences = _sequences(doc)
    return {
        "file_version": _first_value(doc, "FileInfo", "Version"),
        "qi_spec": _first_value(doc, "QiSpecification", "Version"),
        "power_profile": _first_value(doc, "PowerProfile", "Version"),
        "popup_message": _first_value(doc, "PopupMessage", "Message"),
        "sequences": len(sequences),
        "packets": [len(s) for s in sequences],
    }


def bucket_packets(packets: List[Dict[str, Any]], variant: str) -> Tuple[Dict[str, List], List[str]]:
    """
    Group one sequence's flat packet list into the model's per-phase lists.

    Args:
        packets: The packet objects from one ConfigureSequence sub-array
        variant: MPPTPT or MPPTPR (decides whether PingPhase exists)

    Returns:
        (phase_lists, problems) — every phase list is present (possibly empty) so the model shape
        is complete; `problems` names packets whose phase could not be placed.
    """
    buckets = _BUCKETS.get(variant, _BUCKETS["MPPTPT"])
    grouped: Dict[str, List[Dict[str, Any]]] = {b: [] for b in buckets}
    problems: List[str] = []

    for index, packet in enumerate(packets):
        if not isinstance(packet, dict):
            problems.append(f"packet #{index} is not an object")
            continue
        phase = str(packet.get("packetPhase", "")).strip()
        target = _PHASE_BUCKET.get(phase.lower())
        if target is None:
            problems.append(f"packet id={packet.get('id', index)} has unknown packetPhase "
                            f"{phase!r}")
            continue
        if target not in grouped:
            # e.g. a Ping Phase packet in a TPR export: the receiver model has no such list.
            problems.append(f"packet id={packet.get('id', index)} is {phase!r}, which {variant} "
                            f"has no list for")
            continue
        grouped[target].append(packet)
    return grouped, problems


def build_packet_model(doc: Dict[str, Any], variant: str,
                       freq_names: Optional[List[str]] = None) -> Tuple[Dict[str, Any], List[str]]:
    """
    Build the MppTptModel / MppTprModel body for PutPacketInformationMppT{p}tModel.

    Args:
        doc: The parsed export
        variant: MPPTPT or MPPTPR
        freq_names: Frequency-model names, ideally read from the app's own default model
                    (`GetDefaultPacketsMppT{p}tModel`). Falls back to ("128", "360").

    Returns:
        ({"freqModels": [...]}, problems)
    """
    sequences = _sequences(doc)
    names = list(freq_names or _DEFAULT_FREQ_NAMES)
    problems: List[str] = []

    if not sequences:
        # Never send an empty packet model silently: the PUT returns 200 either way, and the
        # tier-B read-back then reports "unchanged", which reads like success.
        problems.append("ConfigureSequence holds no packet objects — the packet step would send "
                        "an empty model, so no packets would reach the application")

    if len(sequences) > len(names):
        problems.append(f"export has {len(sequences)} sequences but only {len(names)} frequency "
                        f"name(s) are known ({', '.join(names)}); extra sequences are unnamed")
        names += [f"seq{i}" for i in range(len(names), len(sequences))]

    freq_models = []
    for index, packets in enumerate(sequences):
        grouped, issues = bucket_packets(packets, variant)
        problems.extend(f"sequence {index}: {p}" for p in issues)
        freq_models.append({"freqName": names[index], "PhaseModels": grouped})
    return {"freqModels": freq_models}, problems


def _field(body: Any, name: str) -> Any:
    """Read a field from an exported group without regard to case."""
    if not isinstance(body, dict):
        return None
    if name in body:
        return body[name]
    lowered = name.lower()
    for key, value in body.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


def _build_ask_modulation(group: str, body: Any, freq_names: Tuple[str, ...]
                          ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    `PutModulatorValues/{Freq}` — one call; the body carries BOTH frequencies.

    The route needs a frequency segment (without it the app answers 404, which is what broke the
    first MPP-TPR run). The value itself is not used: `PutModulatorValues` reads `CoilModulation`
    for 128 and `CoilModulation_360` for 360 out of the body and never looks at `Freq`
    (QiDataModelTPR.cs:35332). The segment is taken from the export's own frequency models rather
    than written here, so it always matches the file.
    """
    freq = freq_names[0] if freq_names else _DEFAULT_FREQ_NAMES[0]
    return [{"group": group, "op": "ask_modulation", "body": body, "path": [freq]}], []


def _build_pp_values(group: str, body: Any, _freqs: Tuple[str, ...]
                     ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    `PutPPValues/{Selected_PP_Val}` — path only, NO body.

    The app declares `PutPPValues(string Selected_PP_Val)` with no body parameter, and the value is
    the potential-power figure the export already holds (the UI assigns
    `selected_PP_Value = potentialPower[i]`, i.e. the value, not an index).
    Note: on MPP-TPR the backend method is empty (QiDataModelTPR.cs:37517), so this is a genuine
    no-op there — sent for fidelity with the UI, and never verifiable.
    """
    value = _field(body, "selected_PP_Value")
    if value in (None, ""):
        return [], [f"controller group '{group}': no 'selected_PP_Value' to put in the URL - "
                    f"PutPPValues needs it as a path segment, so this group is skipped"]
    return [{"group": group, "op": "pp_values", "body": None, "path": [str(value)]}], []


def _build_cloak_phase(group: str, body: Any, _freqs: Tuple[str, ...]
                       ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    `cloakPhase` is not one command — it is up to three, each path-only.

    Mirrors what the app's own UI does (its cloak component, `onChangecloakDropDown` /
    `onClickDropDownSendBtn`):
      * each label is converted to its **index** in the app's option list and sent as the segment;
      * the illegal-packet field is only sent when the exit type is "Illegal Packet Exit" — for any
        other exit type the UI disables that dropdown and sends nothing;
      * when it IS sent, it goes **before** the exit type (the UI chains it via a callback), and the
        value is the bare hex byte from the label: "Signal Strength (0x01)" -> "01".

    An unrecognised label is reported and skipped rather than guessed at: sending the wrong index
    would silently configure the wrong behaviour, and these endpoints have no read-back.
    """
    commands: List[Dict[str, Any]] = []
    problems: List[str] = []

    reason = _field(body, "cloakEntryReason")
    if reason is not None:
        if reason in _CLOAK_ENTRY_REASONS:
            commands.append({"group": group, "op": "cloak_entry_reason", "body": None,
                             "path": [_CLOAK_ENTRY_REASONS.index(reason)]})
        else:
            problems.append(f"controller group '{group}': cloak entry reason {reason!r} is not one "
                            f"the app offers {list(_CLOAK_ENTRY_REASONS)} - skipped rather than "
                            f"guessed")

    exit_type = _field(body, "cloakExitType")
    exit_index = None
    if exit_type is not None:
        if exit_type in _CLOAK_EXIT_TYPES:
            exit_index = _CLOAK_EXIT_TYPES.index(exit_type)
        else:
            problems.append(f"controller group '{group}': cloak exit type {exit_type!r} is not one "
                            f"the app offers {list(_CLOAK_EXIT_TYPES)} - skipped rather than "
                            f"guessed")

    # Illegal packet applies to one exit type only, and is sent first when it does.
    if exit_type == _CLOAK_EXIT_ILLEGAL_PACKET:
        label = _field(body, "illegalPackets")
        header = _illegal_packet_header(label)
        if header:
            commands.append({"group": group, "op": "cloak_illegal_packet", "body": None,
                             "path": [header]})
        else:
            problems.append(f"controller group '{group}': exit type is "
                            f"'{_CLOAK_EXIT_ILLEGAL_PACKET}' but no packet header could be read "
                            f"from {label!r} - the app needs a hex byte such as '01'")

    if exit_index is not None:
        commands.append({"group": group, "op": "cloak_exit_type", "body": None,
                         "path": [exit_index]})
    return commands, problems


def _illegal_packet_header(label: Any) -> Optional[str]:
    """
    The hex byte out of an illegal-packet label: "Signal Strength (0x01)" -> "01".

    Same extraction the app's UI performs (`split("(")[1].replace(")","").replace("0x","")`), and
    the form its backend needs — `Convert.ToByte(msg, 16)`.
    """
    text = str(label or "")
    match = re.search(r"\(\s*(?:0x)?([0-9A-Fa-f]{1,2})\s*\)", text)
    return match.group(1) if match else None


#: Groups whose export data does not map to a single body-carrying PUT. Anything NOT listed here
#: keeps the original behaviour exactly — one op, whole group as the body — so TPT is untouched.
_GROUP_BUILDERS = {
    "MPPTPR": {
        "ModulatorConfiguration": _build_ask_modulation,
        "DUTType":                _build_pp_values,
        "cloakPhase":             _build_cloak_phase,
    },
    "C3TPR": {
        "ModulatorConfiguration": _build_ask_modulation,
        "DUTType":                _build_pp_values,
    },
}


def controller_commands(doc: Dict[str, Any], variant: str,
                        freq_names: Optional[List[str]] = None
                        ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Turn the ControllerConfigurations groups into ordered commands.

    TPT nests its groups under a single `TPT_Configuration` wrapper; TPR has them at the top level.

    Args:
        doc: The parsed export
        variant: MPPTPT or MPPTPR

    Returns:
        ([{"group", "op", "body"}, ...], problems) — problems name groups with no known endpoint,
        so an unsupported setting is visible rather than silently skipped.
    """
    block = doc.get("ControllerConfigurations") or []
    groups = block[0] if isinstance(block, list) and block and isinstance(block[0], dict) else {}
    mapping = _CONTROLLER_GROUP_OPS.get(variant, {})

    # TPT wraps everything in one container (`TPT_Configuration`); TPR has its groups at the top
    # level. Unwrap a lone dict-valued key ONLY when that key is not itself a known group — a
    # bare `len(groups) == 1` test would tear apart a legitimate single-group export, treating its
    # FIELDS as groups. TPR happens to ship six groups today, so that has never bitten, but it is
    # one edited export away from doing so.
    if len(groups) == 1:
        only_key, only_value = next(iter(groups.items()))
        if isinstance(only_value, dict) and only_key not in mapping:
            groups = only_value
    builders = _GROUP_BUILDERS.get(variant, {})
    freqs = tuple(freq_names or _DEFAULT_FREQ_NAMES)
    commands: List[Dict[str, Any]] = []
    problems: List[str] = []
    for name, body in groups.items():
        op = mapping.get(name)
        if op is None:
            problems.append(f"controller group '{name}' has no known endpoint - not applied")
            continue
        builder = builders.get(name)
        if builder is None:
            # The ordinary case: one op, the whole group as the body, no path segments.
            commands.append({"group": name, "op": op, "body": body})
            continue
        built, issues = builder(name, body, freqs)
        commands.extend(built)
        problems.extend(issues)
    return commands, problems


def phase_settings(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The PhasesSettings body for PutTPTPhaseSettings / PutPhaseSetting, or None if absent."""
    block = doc.get("PhasesSettings") or []
    if isinstance(block, list) and block and isinstance(block[0], dict):
        return block[0]
    return None


#: Steps that may be switched off in the run-controls file. `start` is always present; `stop` is
#: never a step — the manager issues it from a `finally` so it cannot be skipped.
DEFAULT_STEP_ORDER = ("reset", "controller", "phases", "packets", "start")


def build_steps(doc: Dict[str, Any], variant: str,
                include: Optional[List[str]] = None,
                freq_names: Optional[List[str]] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Turn an exported sequence into the ordered steps of a session.

    Order matters: the controller hardware and phase timings are configured, then the packet
    sequence is loaded, and only then is the exerciser started — starting first would run against
    whatever the controller held from a previous session.

    Args:
        doc: The parsed export
        variant: MPPTPT or MPPTPR
        include: Step groups to run, from `DEFAULT_STEP_ORDER`; None means all
        freq_names: Frequency-model names, ideally read from the app's own default model

    Returns:
        ([{op, body, path, group}, ...], problems)
    """
    wanted = list(include or DEFAULT_STEP_ORDER)
    steps: List[Dict[str, Any]] = []
    problems: List[str] = []

    if "reset" in wanted:
        steps.append({"op": "reset", "group": "reset"})

    if "controller" in wanted:
        commands, issues = controller_commands(doc, variant, freq_names)
        problems.extend(issues)
        for command in commands:
            step = {"op": command["op"], "body": command["body"], "group": command["group"]}
            if command.get("path"):
                step["path"] = command["path"]
            steps.append(step)

    if "phases" in wanted:
        settings = phase_settings(doc)
        if settings:
            steps.append({"op": "phase_settings", "body": settings, "group": "PhasesSettings"})

    if "packets" in wanted:
        model, issues = build_packet_model(doc, variant, freq_names)
        problems.extend(issues)
        steps.append({"op": "packets", "body": model, "group": "ConfigureSequence"})

    if "start" in wanted:
        steps.append({"op": "start", "group": "start"})

    return steps, problems
