"""User-local filesystem roots shared across SeparationWorker modules."""

from __future__ import annotations

import os
from pathlib import Path


def local_data_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Stemslayer"
    return Path.home() / "AppData" / "Local" / "Stemslayer"
