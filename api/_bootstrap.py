"""Load `.env` into the process before any env-reading import runs.

Must be imported before `hireme_agent`, which reads `LLM_MODEL` at
module-import time. `override=False` (the default) means real environment
variables, e.g. from Docker `--env-file` or a hosting platform, always win.
"""

import os

from dotenv import dotenv_values, load_dotenv

# A stale `export LLM_MODEL=...` in the launching shell silently beats `.env` and the code
# default, so remember where the model setting came from and surface a conflict at boot.
_FROM_PROCESS_ENV = "LLM_MODEL" in os.environ
_DOTENV_MODEL = dotenv_values().get("LLM_MODEL")

load_dotenv()


def model_source() -> str:
    """Where the effective `LLM_MODEL` came from, for the startup log."""
    if "LLM_MODEL" not in os.environ:
        return "code default"
    return "process environment" if _FROM_PROCESS_ENV else ".env"


def model_shadowed_by_process_env() -> str | None:
    """The `.env` model the process environment overrode, if they disagree."""
    if _FROM_PROCESS_ENV and _DOTENV_MODEL and _DOTENV_MODEL != os.environ["LLM_MODEL"]:
        return _DOTENV_MODEL
    return None
