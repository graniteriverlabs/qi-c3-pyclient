"""
Workspace resolution for the pip-installed GRL C3 client.

The client reads its bench inputs and writes its logs, reports and captures under a project root.
Installed with pip the code lives in ``site-packages``, which is often read-only and is never where
a customer's ESDF files belong, so the runtime state has to live somewhere else.

This module resolves a writable workspace and seeds it from the read-only defaults in the wheel.

**The workspace is the directory you run in**, so your config, description files and captures sit
where you are working. One workspace drives all four applications at once - each has its own
sub-directory - so a second workspace is only needed when you want a second set of settings.

Resolution order:

1. ``GRL_C3_PROJECT_ROOT`` - used as given. Point it at a workspace you manage yourself.
2. ``GRL_C3_HOME`` - an explicit workspace location instead of the current directory.
3. The current working directory.

Only ``c3-init`` writes anything. Importing the package resolves the workspace but never creates or
seeds files, so ``import grl_c3_client`` in an arbitrary directory leaves it untouched.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

_PKG_DIR = Path(__file__).resolve().parent

#: The applications this client drives. Each gets its own input and output directories, so one
#: workspace covers a whole bench rather than one tester.
APPLICATIONS = (
    "GRL-C3-MP-TPR",
    "GRL-C3-TPT-MPP",
    "GRL-WP-TPR-C3",
    "GRL-C3-TPT-BPP-EPP",
)

#: Read-only defaults copied out of the wheel by ``c3-init``. Existing files are never overwritten,
#: so local edits survive an upgrade.
_SEED_FILES = ("grl_config.json",)
_SEED_DIRS = ("JSON_User_input",)

#: Created empty. Runtime output only, never seeded - a shipped copy would describe some other
#: bench. ``c3-testcases`` and ``c3-run`` fill them.
_RUNTIME_DIRS = (
    "logs",
    "Run_time_files",
    "Runtime_Capture",
    "Test_Case_List_From_System",
)

#: RFC 5737 TEST-NET-1: non-routable on purpose, so an unconfigured install fails fast instead of
#: reaching whatever happens to answer on a real address.
PLACEHOLDER_IP = "192.0.2.50"


def default_workspace() -> Path:
    """Where a workspace is created: ``GRL_C3_HOME``, else the current directory."""
    override = os.environ.get("GRL_C3_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd().resolve()


def resolve_workspace() -> Path:
    """
    The project root to read from. Creates nothing and seeds nothing.

    Used at import time, where writing files would be an unwanted side effect of
    ``import grl_c3_client``.
    """
    existing = os.environ.get("GRL_C3_PROJECT_ROOT", "").strip()
    if existing:
        return Path(existing).expanduser().resolve()
    return default_workspace()


def is_initialised(workspace: Path) -> bool:
    """True once ``c3-init`` has seeded this directory."""
    return (workspace / "grl_config.json").is_file()


def config_path(workspace: Path | None = None) -> Path:
    """The configuration file the client should be pointed at."""
    return (workspace or resolve_workspace()) / "grl_config.json"


def _seed_tree(rel: str, workspace: Path, force: bool = False) -> List[str]:
    """
    Copy bundled defaults for one relative directory, recursively. Returns copied names.

    Without ``force`` an existing file is left alone, so your edits survive. With it, the files
    this package ships are replaced - but a file you added yourself is never one of them, because
    only the bundled paths are walked.
    """
    src = _PKG_DIR.joinpath(*rel.split("/"))
    if not src.is_dir():
        return []
    copied = []
    for item in sorted(src.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(src)
        target = workspace / rel / relative
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.append("{0}/{1}".format(rel, relative.as_posix()))
    return copied


def _merge_missing(shipped: Any, existing: Any, path: str = "") -> List[str]:
    """
    Add keys the shipped configuration has and the workspace's does not. Returns what was added.

    An upgrade that introduces a setting - a new application, a new run control - has to reach
    workspaces that already exist, or an existing user never sees it and the release notes are the
    only way they would know. Seeding alone cannot do that, because the file is already there.

    A value that is present locally is never touched, whatever it is: that is the user's bench
    address, their file choices, their run settings. Lists are values, not structure, so a list
    the user has edited is left whole rather than merged element by element.
    """
    added: List[str] = []
    if not isinstance(shipped, dict) or not isinstance(existing, dict):
        return added
    for key, value in shipped.items():
        where = "{0}.{1}".format(path, key) if path else key
        if key not in existing:
            existing[key] = copy.deepcopy(value)
            added.append(where)
        elif isinstance(value, dict) and isinstance(existing[key], dict):
            added.extend(_merge_missing(value, existing[key], where))
    return added


def _update_config(workspace: Path, force: bool) -> Tuple[List[str], Optional[Path]]:
    """
    Bring the workspace's configuration up to date with the one this package ships.

    Returns (settings added, path of the backup taken when forcing).
    """
    shipped_path = _PKG_DIR / "grl_config.json"
    target = workspace / "grl_config.json"
    if not shipped_path.is_file():
        return [], None

    if not target.exists():
        shutil.copy2(shipped_path, target)
        return [], None

    if force:
        # Replacing the file loses the bench address and the file choices, so keep a copy. The
        # stamp means repeated resets never overwrite one another.
        backup = target.with_suffix(".json.bak-{0}".format(time.strftime("%Y%m%d-%H%M%S")))
        shutil.copy2(target, backup)
        shutil.copy2(shipped_path, target)
        return [], backup

    try:
        shipped = json.loads(shipped_path.read_text(encoding="utf-8"))
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("WARNING: could not merge new settings into {0}: {1}".format(target, exc))
        return [], None

    added = _merge_missing(shipped, existing)
    if added:
        target.write_text(json.dumps(existing, indent=4, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    return added, None


def _ensure(force: bool = False) -> Tuple[Path, List[str], List[str], Optional[Path]]:
    """
    Create, seed and update the workspace. This is what ``c3-init`` runs.

    Safe to run again, and worth running after an upgrade: settings the new version adds are
    merged into your configuration, and files that have gone missing are put back. Nothing you
    have set is changed.

    With ``force`` the shipped configuration and the shipped example inputs replace what is there,
    after backing the configuration up. Files you added yourself are still left alone.

    Returns (workspace, settings added, files restored, configuration backup path).
    """
    workspace = resolve_workspace()
    workspace.mkdir(parents=True, exist_ok=True)

    added, backup = _update_config(workspace, force)

    restored: List[str] = []
    for rel in _SEED_DIRS:
        restored.extend(_seed_tree(rel, workspace, force))

    for rel in _RUNTIME_DIRS:
        for app in APPLICATIONS:
            # logs/ is shared; everything else is per application, matching what the client writes.
            path = workspace / rel if rel == "logs" else workspace / rel / app
            path.mkdir(parents=True, exist_ok=True)
    return workspace, added, restored, backup


def ensure_workspace(force: bool = False) -> Path:
    """
    Create, seed and update the workspace, then return it.

    Safe to run again, and worth running after an upgrade: settings the new version adds are merged
    into your configuration and missing files are put back, without changing anything you have set.
    With ``force`` this version's defaults replace the configuration and the shipped example
    inputs, after backing the configuration up.
    """
    return _ensure(force)[0]


def unconfigured_warnings(workspace: Path) -> List[str]:
    """Flag settings the site must supply before the client can connect."""
    config = config_path(workspace)
    if not config.is_file():
        return ["WARNING: {0} is missing. Run c3-init here.".format(config)]

    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ["WARNING: could not read {0}: {1}".format(config, exc)]

    warnings = []
    for app_name, app in (data.get("applications") or {}).items():
        if not isinstance(app, dict):
            continue
        if app.get("ip_address") == PLACEHOLDER_IP:
            warnings.append(
                "ACTION REQUIRED: applications.{0}.ip_address is still the placeholder {1}.\n"
                "  Set your tester's address in: {2}".format(app_name, PLACEHOLDER_IP, config))
        app_path = app.get("app_path")
        if app_path and not Path(app_path).is_file():
            warnings.append(
                "CHECK: applications.{0}.app_path does not exist on this machine:\n"
                "  {1}".format(app_name, app_path))
    return warnings


def require_workspace() -> Path:
    """
    Stop with a clear instruction when the current directory is not set up.

    Without this the first missing file surfaces from deep inside the config manager, which tells a
    new user nothing useful.
    """
    workspace = resolve_workspace()
    if not is_initialised(workspace):
        raise SystemExit(
            "No GRL C3 workspace in:\n"
            "  {0}\n\n"
            "Run 'c3-init' in this directory first, or point GRL_C3_PROJECT_ROOT at a directory\n"
            "you have already initialised.".format(workspace))
    return workspace


def docs_dir() -> Path:
    """Location of the bundled guides (read-only, inside the package)."""
    return _PKG_DIR / "docs"


def describe_workspace() -> str:
    """Human-readable summary of the resolved workspace and its contents."""
    workspace = resolve_workspace()
    lines = ["GRL C3 client workspace: {0}".format(workspace), ""]

    config = config_path(workspace)
    lines.append("  {0:<34} {1}".format("grl_config.json",
                                        "ok" if config.is_file() else "MISSING"))
    for rel in _SEED_DIRS + _RUNTIME_DIRS:
        path = workspace / rel
        count = sum(1 for _ in path.rglob("*") if _.is_file()) if path.is_dir() else 0
        lines.append("  {0:<34} {1:>4} file(s)  [{2}]".format(
            rel, count, "ok" if path.is_dir() else "MISSING"))

    for warning in unconfigured_warnings(workspace):
        lines.append("")
        lines.append(warning)

    docs = docs_dir()
    if docs.is_dir():
        lines.append("")
        lines.append("Bundled documentation: {0}".format(docs))
        for item in sorted(docs.glob("*.md")):
            lines.append("  {0}".format(item.name))

    lines.append("")
    lines.append("Next: c3-apps to list the applications, then c3-testcases and c3-run.")
    lines.append("To use this workspace from elsewhere:")
    lines.append("  set GRL_C3_PROJECT_ROOT={0}".format(workspace))
    return "\n".join(lines)


def main() -> int:
    """Console entry point: create or update the workspace here, and report what it contains."""
    parser = argparse.ArgumentParser(
        prog="c3-init",
        description="Create a GRL C3 workspace in this directory, or update an existing one. "
                    "Run it again after upgrading: settings the new version adds are merged into "
                    "your configuration and missing files are restored, without changing anything "
                    "you have set.")
    parser.add_argument(
        "--force", action="store_true",
        help="replace the configuration and the shipped example inputs with this version's "
             "defaults. Your configuration is backed up first, and files you added yourself are "
             "left alone. Use this to start over, not to upgrade.")
    args = parser.parse_args()

    _workspace, added, restored, backup = _ensure(force=args.force)

    if backup:
        print("Configuration replaced with this version's defaults.")
        print("  your previous one: {0}".format(backup))
    if added:
        print("Added {0} setting(s) new in this version:".format(len(added)))
        for key in added:
            print("  {0}".format(key))
    if restored:
        print("Restored {0} file(s):".format(len(restored)))
        for name in restored[:10]:
            print("  {0}".format(name))
        if len(restored) > 10:
            print("  ... and {0} more".format(len(restored) - 10))
    if added or restored or backup:
        print()

    print(describe_workspace())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
