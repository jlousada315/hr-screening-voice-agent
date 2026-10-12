"""Pin the suite to a fixed owner before any package import loads it."""

import os
from pathlib import Path

os.environ["OWNER_FILE"] = str(Path(__file__).parent / "config/example.md")
