# utils/project_root.py
"""
Where the client reads its inputs and writes its outputs.

Every caller used to derive this by walking up from its own ``__file__``. That is right in a source
checkout, where the code sits inside the project, and wrong once the package is installed: the
modules then live in ``site-packages``, so the client would look for ``JSON_User_input/`` and write
``Runtime_Capture/`` inside the installed package - a directory that is often read-only and is never
where the user's bench files belong.

Resolution order:

1. ``GRL_C3_PROJECT_ROOT`` - used as given. The installed package sets this to the user's workspace
   before anything else is imported.
2. The checkout that contains this file, which is what every caller computed before.

Nothing here creates or writes anything; resolving a root must stay free of side effects.
"""
import os

#: Set by the installed package to the user's workspace; also settable by hand to point a source
#: checkout at a different bench directory.
ENV_VAR = "GRL_C3_PROJECT_ROOT"

#: utils/ -> the project that contains it. The historical behaviour, and the fallback.
_CHECKOUT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def project_root() -> str:
    """The directory holding JSON_User_input/, Run_time_files/, Runtime_Capture/ and logs/."""
    configured = os.environ.get(ENV_VAR, "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return _CHECKOUT_ROOT


def checkout_root() -> str:
    """The checkout this file lives in, ignoring any override. For diagnostics."""
    return _CHECKOUT_ROOT
