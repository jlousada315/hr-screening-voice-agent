"""Check an owner profile and print what the rep will read from it.

    uv run python scripts/check_owner.py [config/owner.md]
"""

import runpy
from pathlib import Path

# Run the loader as a script, so a broken file is reported without importing the agent.
runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "agent/src/hireme_agent/owner.py"),
    run_name="__main__",
)
