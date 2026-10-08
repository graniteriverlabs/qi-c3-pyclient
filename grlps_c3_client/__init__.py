"""
GRL C3 API Client - installable distribution.

The public API::

    from grlps_c3_client import GRLApiClient

    client = GRLApiClient()          # reads grl_config.json from the workspace
    client.launch_app()
    client.connect()

One install drives all four C3 applications - MPP-TPR, TPT in its MPP firmware, C3-TPR, and TPT in
its BPP/EPP firmware. Which one a run uses comes from ``Selected_app`` in the workspace config, or
from the ``--app`` option on the command line.

Runtime requirement: Windows, and CPython 3.11 or newer. Both are checked at import time so the
failure names the cause rather than surfacing later as a missing attribute.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

__version__ = "1.0.0"

_PKG_DIR = Path(__file__).resolve().parent
_INTERNAL_DIR = _PKG_DIR / "_internal"

#: Oldest interpreter this package supports. Kept in step with ``requires-python`` in the packaging
#: metadata so pip and this check agree on what is allowed.
_MIN_PYTHON = (3, 11)


def _validate_runtime() -> None:
    """Refuse an unsupported interpreter or OS with a message that says why."""
    is_windows = os.name == "nt" or sys.platform.startswith("win")
    is_supported = sys.version_info[:2] >= _MIN_PYTHON
    if is_windows and is_supported:
        return
    current = "Python {0}.{1}.{2} on {3}".format(
        sys.version_info.major, sys.version_info.minor, sys.version_info.micro, sys.platform)
    raise RuntimeError(
        "The GRL C3 client requires Windows with Python {0}.{1} or newer, because it starts and "
        "drives the C3 desktop applications. Current runtime: {2}.".format(
            _MIN_PYTHON[0], _MIN_PYTHON[1], current))


_validate_runtime()

# The shipped modules import one another by their original top-level names (``from API import
# ApiName``, ``from utils.project_root import project_root``). Those names are far too generic to
# install at the top level of site-packages, where they would collide with anything else called
# "client" or "utils". The tree is bundled under ``_internal`` and that directory goes on sys.path
# here, which keeps every original import working without editing the shipped sources.
if _INTERNAL_DIR.is_dir():
    _internal_path = str(_INTERNAL_DIR)
    if _internal_path not in sys.path:
        sys.path.insert(0, _internal_path)

from ._bootstrap import (  # noqa: E402
    APPLICATIONS,
    config_path,
    default_workspace,
    describe_workspace,
    docs_dir,
    ensure_workspace,
    is_initialised,
    resolve_workspace,
)

# Must happen before the client is imported. Without it the client falls back to the directory that
# contains its own modules, which under pip is site-packages - not writable, and not where the
# customer's bench files belong.
#
# This resolves only; it never creates or seeds anything. Importing the package must not write into
# whatever directory the caller happens to be in - seeding is the job of ``c3-init``.
os.environ.setdefault("GRL_C3_PROJECT_ROOT", str(resolve_workspace()))

from client.grl_api_client import GRLApiClient  # noqa: E402

__all__ = [
    "GRLApiClient",
    "APPLICATIONS",
    "config_path",
    "default_workspace",
    "describe_workspace",
    "docs_dir",
    "ensure_workspace",
    "is_initialised",
    "resolve_workspace",
    "__version__",
]
