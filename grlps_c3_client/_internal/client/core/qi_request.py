"""
What a run asks the application for - a power profile at a Qi specification version - checked
against the tester's licence, set on the application and read back before anything is started.

WHY THIS EXISTS
---------------
The request has to come from the file the run uses: the description file for a compliance run
(and for fetching its test cases), the exported sequence for an exerciser session. The
application's own setting is not the request. The MPP-TPT 2.4 builds reset it to MPP50 at Qi
2.4.0 every time they connect to the tester, and the client connects at the start of every
command - so a version read from the application was one nobody asked for, and a 2.3.1
description file was checked, and would have run, at 2.4.

THE RULE
--------
Fail-closed. A run goes ahead only when all of these hold; anything that cannot be established
refuses it:

1. the profile and the Qi version asked for are known (from the file; the application's current
   setting is used only for a value the file does not state, and the log says so);
2. the tester's licence covers that profile at that version (`license_gate.evaluate`);
3. the application accepted the request: written, then read back. A version that does not read
   back as asked refuses the run; a profile is accepted unread only where the application cannot
   report that profile at that version, because its getter has no case for it.

A refusal says what was asked for, where it came from, and what the tester is licensed for.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from API import ApiName

from . import license_gate

#: How each application's `PutSelectedQiSpecMode_MPP` spells a version, tried in order. Both
#: backends switch on exact strings: "2.3.1" on the current builds, "2.3.0" on older ones, and a
#: spelling they do not know falls through to a default version. Every write is read back, so a
#: spelling the build does not take is simply the next one tried - never a run at the wrong
#: version.
SPELLINGS: Dict[str, Tuple[str, ...]] = {
    "1.3": ("1.3.3",),
    "2.0": ("2.0.1",),
    "2.1": ("2.1",),
    "2.2": ("2.2",),
    "2.3": ("2.3.1", "2.3.0"),
    "2.4": ("2.4.0", "2.4"),
}

#: The profiles `GetSelectedQiSpecMode_MPP` can report at each version, read from the getters in
#: both backends. Outside this set the getter has no case and returns its default, so a profile
#: that IS applied (the MPP-TPR setter stores it separately, in `_dutProfile`) cannot be shown.
#: Only for those profiles is an unconfirmed profile accepted; the version must always read back.
#: Versions not listed report every profile they take, so the profile must read back exactly.
REPORTABLE: Dict[str, frozenset] = {
    "2.0": frozenset({"MPP"}),
    "2.1": frozenset({"MPP", "APP"}),
    "2.2": frozenset({"MPP15", "MPP25"}),
    "2.3": frozenset({"MPP15", "MPP25"}),
}


def spellings(spec: Any) -> List[str]:
    """The spellings to write for ``spec``: the known ones for its major.minor, else as given."""
    norm = license_gate.normalise_spec(spec)
    known = list(SPELLINGS.get(norm, ()))
    given = str(spec).strip() if spec is not None else ""
    if given and given not in known and not known:
        known.append(given)
    return known


def read_mode(api_handler: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(data, "")`` with the application's current ``qiSpecMode``/``powerProfile``, or
    ``(None, why)`` when it cannot be read. Never raises."""
    try:
        resp = api_handler.call_api(ApiName.GET_SELECTED_QI_SPEC_MODE_MPP)
    except Exception as exc:                                    # transport failure
        return None, "the application could not be asked ({0})".format(exc)
    body = (resp or {}).get("response", {}) or {}
    data = body.get("data")
    if not body.get("success") or not isinstance(data, dict):
        return None, "the application did not report its profile and Qi version ({0})".format(
            body.get("status_code") or body.get("error") or "no data")
    return data, ""


def resolve(current: Dict[str, Any], profile: Optional[str], spec: Optional[str],
            source: str, logger: Any, profile_from_app: bool) -> Dict[str, Any]:
    """
    The request: the file's profile and version, with the application's current value used only
    where the file states none (and only for the profile when ``profile_from_app``).

    Returns ``{"profile", "spec", "reason"}``; ``reason`` is set when the request is incomplete.
    """
    want_profile = (str(profile).strip().upper() or None) if profile else None
    want_spec = str(spec).strip() if spec else None

    if not want_profile and profile_from_app and current.get("powerProfile"):
        want_profile = str(current["powerProfile"]).strip().upper()
        logger.info("Request: the {0} names no power profile, so the application's current "
                    "profile {1} is the one checked and used".format(source, want_profile))
    if not want_spec and current.get("qiSpecMode"):
        want_spec = str(current["qiSpecMode"]).strip()
        logger.info("Request: the {0} names no Qi specification version, so the application's "
                    "current version {1} is the one checked and used".format(source, want_spec))

    reason = ""
    if not want_profile:
        reason = "the {0} names no power profile, so there is nothing to check the licence " \
                 "against".format(source)
    elif not license_gate.normalise_spec(want_spec):
        reason = "the Qi specification version this run asks for is not known (the {0} " \
                 "names none and the application reports {1!r})".format(
                     source, current.get("qiSpecMode"))
    return {"profile": want_profile, "spec": want_spec, "reason": reason}


def _same(data: Optional[Dict[str, Any]], profile: str, spec: str) -> Tuple[bool, bool]:
    """``(version_matches, profile_matches_or_cannot_be_reported)`` for a read-back."""
    if not isinstance(data, dict):
        return False, False
    norm = license_gate.normalise_spec(spec)
    spec_ok = license_gate.normalise_spec(data.get("qiSpecMode")) == norm
    shown = str(data.get("powerProfile") or "").strip().upper()
    reportable = REPORTABLE.get(norm)
    profile_ok = shown == profile or (reportable is not None and profile not in reportable)
    return spec_ok, profile_ok


def apply(api_handler: Any, logger: Any, current: Dict[str, Any],
          profile: str, spec: str) -> Tuple[bool, str]:
    """
    Put ``profile`` at ``spec`` on the application and confirm it by reading it back.

    Returns ``(True, "")`` when the application reports the request (or already did), else
    ``(False, why)`` after putting back what it reported before. Never raises.
    """
    spec_ok, profile_ok = _same(current, profile, spec)
    if spec_ok and str(current.get("powerProfile") or "").strip().upper() == profile:
        logger.info("Request: the application is already on {0} at Qi {1}".format(profile, spec))
        return True, ""

    last = current
    for spelling in spellings(spec):
        payload = dict(current)
        payload["qiSpecMode"] = spelling
        payload["powerProfile"] = profile
        try:
            api_handler.call_api(ApiName.PUT_SELECTED_QI_SPEC_MODE_MPP, data=payload)
        except Exception as exc:
            logger.warning("Request: writing {0} at Qi {1} failed ({2})".format(
                profile, spelling, exc))
            continue
        last, error = read_mode(api_handler)
        spec_ok, profile_ok = _same(last, profile, spec)
        if spec_ok and profile_ok:
            shown = str((last or {}).get("powerProfile") or "").strip().upper()
            if shown == profile:
                logger.info("Request: the application now reports {0} at Qi {1}".format(
                    shown, last.get("qiSpecMode")))
            else:
                logger.info("Request: {0} was applied at Qi {1}; this build cannot report {0} "
                            "at that version, so it still shows {2}".format(
                                profile, last.get("qiSpecMode"), shown or "nothing"))
            return True, ""
        logger.info("Request: the application did not take Qi {0} for {1} (it reports {2}); "
                    "trying the next spelling".format(spelling, profile, _describe(last, error)))

    # Leave the application as it was found rather than on a fallback the setter chose.
    try:
        api_handler.call_api(ApiName.PUT_SELECTED_QI_SPEC_MODE_MPP, data=dict(current))
    except Exception:
        pass
    return False, ("the application did not take {0} at Qi {1}: after the request it reports "
                   "{2}. This build may not offer that version".format(
                       profile, spec, _describe(last, "")))


def _describe(data: Optional[Dict[str, Any]], error: str) -> str:
    if isinstance(data, dict):
        return "{0} at Qi {1}".format(data.get("powerProfile"), data.get("qiSpecMode"))
    return error or "nothing"


def check_and_apply(api_handler: Any, logger: Any, connection_info: Any, app_name: Optional[str],
                    profile: Optional[str], spec: Optional[str], source: str,
                    profile_from_app: bool = False, write: bool = True,
                    allowed_profiles: Optional[frozenset] = None) -> Dict[str, Any]:
    """
    The whole decision: read the application, resolve the request, check the licence, check the
    application can run the profile, then set and confirm it.

    Args:
        api_handler: the client's handler
        logger: the client's logger
        connection_info: the cached connect response, which lists the tester's licences
        app_name: the application being driven
        profile, spec: what the file asks for (None where it states nothing)
        source: names the file in messages, e.g. "description file MyDevice.json"
        profile_from_app: use the application's current profile when the file names none (the
            C3-TPR exerciser export carries no profile); a compliance run never does
        write: False for a dry run - the request is checked but nothing is sent
        allowed_profiles: the profiles this application can run, or None for any

    Returns:
        ``{"allowed", "licence", "reason", "profile", "spec", "verdict"}``. ``licence`` is True
        when the refusal (if any) is the licence itself, so callers can say so.
    """
    out = {"allowed": False, "licence": False, "reason": "", "profile": profile, "spec": spec,
           "verdict": None}

    current, error = read_mode(api_handler)
    if current is None:
        out["reason"] = error
        return out

    request = resolve(current, profile, spec, source, logger, profile_from_app)
    out["profile"], out["spec"] = request["profile"], request["spec"]
    if request["reason"]:
        out["reason"] = request["reason"]
        return out

    verdict = license_gate.evaluate(connection_info, profile=request["profile"],
                                    spec=request["spec"], app_name=app_name, source=source)
    out["verdict"] = verdict
    if not verdict["allowed"]:
        out["licence"] = True
        out["reason"] = verdict["reason"]
        return out
    logger.info("Licence check passed: {0}".format(verdict["reason"]))

    if allowed_profiles is not None and request["profile"] not in allowed_profiles:
        out["reason"] = ("Controller is wrong. Expecting powerProfile in {0}, but got: {1} "
                         "(from the {2})".format(sorted(allowed_profiles), request["profile"],
                                                 source))
        return out

    if not write:
        out["allowed"] = True
        return out

    ok, why = apply(api_handler, logger, current, request["profile"], request["spec"])
    if not ok:
        out["reason"] = why
        return out
    out["allowed"] = True
    return out
