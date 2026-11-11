"""File locations used by the tool."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
K6_DIR = PACKAGE_DIR / "k6"
GUI_DIR = PACKAGE_DIR / "gui"
PROJECT_ROOT = PACKAGE_DIR.parent


def reports_dir() -> Path:
    """Folder for run reports.

    PAGELOAD_REPORTS_DIR wins; in a source checkout it is <project>/reports,
    otherwise ./reports in the current folder.
    """
    configured = os.environ.get("PAGELOAD_REPORTS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if (PROJECT_ROOT / "pyproject.toml").exists():
        return PROJECT_ROOT / "reports"
    return Path.cwd() / "reports"
