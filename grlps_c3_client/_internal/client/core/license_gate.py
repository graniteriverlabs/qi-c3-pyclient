"""
One licence decision for the whole client, from what the application reports enabled.

WHY THIS EXISTS
---------------
The licence was only checked when an exerciser session started, and the question it asked was the
exerciser's own: *is any module named 2.3 active*. Nothing in the compliance path checked anything.
So a controller with no licence for the profile being tested would run it: on 2026-10-08 this bench
reported eight modules, none of them BPP, and a BPP project was created and three BPP cases offered.

The application will not let that happen by hand, and driving it from Python must not be a way
around it.

WHAT THE APPLICATION REPORTS
----------------------------
The connect response carries ``licenseInfo``: one entry per module, with a name and a status. Read
live from an MPP-TPR bench::

    MPP 2.1                 Permanent License Activated
    APP 2.1                 Permanent License Activated
    2.2 - MPP 25W           Permanent License Activated
    2.2 - MPP 15W           Permanent License Activated
    2.2 - APP 15W & 25W     Permanent License Activated
    2.3 - MPP 25W           Permanent License Activated
    2.3 - MPP 15W           Permanent License Activated
    2.3 - APP 15W & 25W     Permanent License Activated

and the application's own assembly carries these as well::

    2.3 - BPP   2.3 BPP   2.3 - EPP   2.3 EPP   2.3 - MCPE&MCPM

So a module names a **Qi specification version** and a **power profile**, in either order, with or
without a dash. Nothing is hardcoded here: the version and the profiles are read out of the name,
and the requirement comes from the profile the run actually asks for.

**There is no EPP5 module anywhere** - not on the bench, not in the application - so EPP5 can only
be covered by the EPP module, and is treated that way.

A module with a version but no profile in its name (``MPP_PTx_ComplianceV2_3``) covers that
version's profiles for the family it names.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

#: Status strings are LicenseType *descriptions*, not enum names: "Permanent License Activated",
#: "Demo License Activated", "Not Activated - Contact GRL Support", "Not-Issued". Negatives are
#: tested first, because "Not Activated" contains "activated".
_STATUS_INACTIVE = ("not activated", "not-activated", "not issued", "not-issued", "inactive",
                    "expired", "unlicensed", "not licensed", "none")
_STATUS_ACTIVE = ("activated", "active", "enabled", "valid", "licensed", "demo", "true")

#: Power profile families, and the profiles each one means when no power is named.
_FAMILY_PROFILES = {
    "MPP": ("MPP15", "MPP25"),
    "APP": ("APP15", "APP25"),
    "BPP": ("BPP",),
    # No EPP5 module exists in the application, so an EPP licence is what covers EPP5.
    "EPP": ("EPP", "EPP5"),
    "MCPE": ("MCPE",),
    "MCPM": ("MCPM",),
}

#: Profiles an MPP licence also covers, **on MPP-TPR only**.
#:
#: There, MPP builds on the baseline profiles, so a controller licensed for MPP is licensed to
#: test them: an MPP licence allows BPP, EPP and EPP5 as well.
_MPP_IMPLIES = ("BPP", "EPP", "EPP5")

#: Which applications the implication applies to, by what appears in the application name.
#: **`GRL-C3-MP-TPR` only.** Every other application - both TPT applications and the WP product
#: - allows exactly the profiles its licence names and nothing more.
_MPP_PRODUCT_MARKERS = ("MP-TPR",)

_VERSION_RE = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?:\.\d+)*(?![\d])")
_V_UNDERSCORE_RE = re.compile(r"V(\d+)_(\d+)(?:_\d+)*", re.IGNORECASE)
_POWER_RE = re.compile(r"(\d+)\s*W", re.IGNORECASE)


def normalise_spec(text: Any) -> Optional[str]:
    """
    A Qi specification version as ``major.minor``, or None when there is none to find.

    2.3 and 2.3.1 are the same licence - there is no 2.3.1 module - so the patch part is dropped.
    """
    if text is None:
        return None
    s = str(text)
    m = _VERSION_RE.search(s) or _V_UNDERSCORE_RE.search(s)
    if not m:
        return None
    return "{0}.{1}".format(m.group(1), m.group(2))


def module_active(status: Any) -> bool:
    """Whether a module status denotes a licence that can be used."""
    text = str(status or "").strip().lower()
    if not text:
        return False
    if any(bad in text for bad in _STATUS_INACTIVE):
        return False
    return any(good in text for good in _STATUS_ACTIVE)


def module_profiles(name: Any) -> Set[str]:
    """
    Which power profiles a module name covers.

    ``2.3 - MPP 25W`` -> {MPP25} · ``2.3 - APP 15W & 25W`` -> {APP15, APP25} ·
    ``MPP 2.1`` -> {MPP15, MPP25} (no power named, so the whole family) ·
    ``2.3 - EPP`` -> {EPP, EPP5} · ``2.3 - MCPE&MCPM`` -> {MCPE, MCPM}
    """
    text = str(name or "").upper()
    powers = {p for p in _POWER_RE.findall(text)}
    profiles: Set[str] = set()

    for family, defaults in _FAMILY_PROFILES.items():
        if family not in text:
            continue
        if family in ("MPP", "APP") and powers:
            for power in powers:
                candidate = "{0}{1}".format(family, power)
                # Only a profile the family actually has; "MPP 5W" is not a profile.
                if candidate in defaults:
                    profiles.add(candidate)
        else:
            profiles.update(defaults)

    # "MCPE&MCPM" contains neither MPP nor APP; "MPP_PTx_Compliance" names MPP with no power.
    return profiles


def parse_modules(connection_info: Any) -> List[Dict[str, Any]]:
    """
    Every module in the connect response, with what it covers. Never raises.

    Each entry: ``{"name", "status", "active", "spec", "profiles"}``.
    """
    info = connection_info if isinstance(connection_info, dict) else {}
    raw = info.get("licenseInfo")
    if raw is None:
        raw = info.get("LicenseInfo")
    if not isinstance(raw, list):
        return []

    out: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            out.append({"name": repr(entry), "status": None, "active": False,
                        "spec": None, "profiles": set()})
            continue
        name = entry.get("moduleName") or entry.get("ModuleName") or ""
        status = entry.get("moduleStatus") or entry.get("ModuleStatus")
        out.append({
            "name": str(name),
            "status": status,
            "active": module_active(status),
            "spec": normalise_spec(name),
            "profiles": module_profiles(name),
        })
    return out


def mpp_covers_baseline(app_name: Any) -> bool:
    """Whether an MPP licence also allows BPP, EPP and EPP5 on this application (MPP-TPR only)."""
    text = str(app_name or "").upper()
    return any(marker in text for marker in _MPP_PRODUCT_MARKERS)


def evaluate(connection_info: Any,
             profile: Optional[str] = None,
             spec: Optional[str] = None,
             app_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Decide whether this controller is licensed for what is being asked of it.

    Args:
        connection_info: the connect response the client already cached
        profile: the power profile the run will use, from the device description file
        spec: the Qi specification version the controller is set to, if known
        app_name: which application is being driven. On MPP-TPR an MPP licence also allows
            BPP, EPP and EPP5, because MPP builds on them. Everywhere else - both TPT
            applications and the WP product - only the profiles the licence names are allowed.

    Returns:
        ``{"allowed", "reason", "covering", "enabled", "asked"}``. ``enabled`` lists every active
        module, so a refusal can be checked against what the application actually reports instead
        of being taken on trust.
    """
    modules = parse_modules(connection_info)
    active = [m for m in modules if m["active"]]
    enabled = [m["name"] for m in active]
    want_spec = normalise_spec(spec)
    want_profile = (profile or "").upper().strip() or None
    asked = "{0}{1}".format(want_profile or "any profile",
                            " at Qi spec {0}".format(want_spec) if want_spec else "")

    if not modules:
        return {"allowed": False, "reason":
                "the application reported no licence modules at all, so there is nothing to "
                "check this run against. Connect to the tester first; if it is connected, the "
                "controller is not licensed.",
                "covering": [], "enabled": enabled, "asked": asked}

    if not active:
        return {"allowed": False, "reason":
                "no licence module on this controller is active. The application reported: "
                "{0}".format(", ".join("{0} ({1})".format(m["name"], m["status"])
                                       for m in modules) or "nothing"),
                "covering": [], "enabled": enabled, "asked": asked}

    if not want_profile:
        # Nothing specific was asked, so any active licence is enough to proceed.
        return {"allowed": True,
                "reason": "controller is licensed: {0}".format(", ".join(enabled)),
                "covering": enabled, "enabled": enabled, "asked": asked}

    def _spec_ok(module):
        return want_spec is None or module["spec"] is None or module["spec"] == want_spec

    covering = [m["name"] for m in active if want_profile in m["profiles"] and _spec_ok(m)]
    if covering:
        return {"allowed": True,
                "reason": "licensed for {0} by {1}".format(asked, ", ".join(covering)),
                "covering": covering, "enabled": enabled, "asked": asked}

    # On the MPP products, an MPP licence carries the baseline profiles with it: MPP builds on
    # BPP and EPP, so a controller licensed for MPP is licensed to test them. Preferred at the
    # same specification version, accepted at any if there is none - the licence is for the
    # profile, and the baseline profiles are not versioned separately in any module name.
    if want_profile in _MPP_IMPLIES and mpp_covers_baseline(app_name):
        mpp_same_spec = [m["name"] for m in active
                         if any(p.startswith("MPP") for p in m["profiles"]) and _spec_ok(m)]
        mpp_any_spec = [m["name"] for m in active
                        if any(p.startswith("MPP") for p in m["profiles"])]
        by = mpp_same_spec or mpp_any_spec
        if by:
            return {"allowed": True,
                    "reason": "licensed for {0}: an MPP licence on {1} covers {2} as well "
                              "({3})".format(asked, app_name, ", ".join(_MPP_IMPLIES),
                                             ", ".join(by)),
                    "covering": by, "enabled": enabled, "asked": asked}

    # Say what is missing AND what is present: a refusal nobody can check is a refusal people
    # work around.
    same_profile = [m["name"] for m in active if want_profile in m["profiles"]]
    if same_profile and want_spec:
        detail = ("this controller is licensed for {0} at another Qi specification version ({1}) "
                  "but not at {2}".format(want_profile, ", ".join(same_profile), want_spec))
    else:
        detail = "this controller has no licence for {0}".format(want_profile)

    return {"allowed": False,
            "reason": "{0}.\n  asked for: {1}\n  licensed  : {2}\n"
                      "  The application will not run an unlicensed profile either. Select a "
                      "description file for a profile this controller is licensed for, or have "
                      "the licence added.".format(detail, asked, ", ".join(enabled) or "nothing"),
            "covering": [], "enabled": enabled, "asked": asked}
