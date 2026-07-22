#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path


MINIMUM_PYTHON = (3, 10)


def requirements_path() -> Path:
    configured = str(os.environ.get("RECOMMEND_PAPERS_REQUIREMENTS") or "").strip()
    if configured:
        return Path(configured).expanduser()
    source_candidate = Path(__file__).resolve().parents[3] / "requirements.txt"
    if source_candidate.is_file():
        return source_candidate
    if platform.system() == "Windows":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "TASTE" / "recommend-papers"
    elif platform.system() == "Darwin":
        root = Path.home() / "Library" / "Application Support" / "TASTE" / "recommend-papers"
    else:
        xdg_data = str(os.environ.get("XDG_DATA_HOME") or "").strip()
        root = (Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share") / "taste" / "recommend-papers"
    return root / "requirements.txt"


def bootstrap_error(message: str, **details: object) -> int:
    payload = {
        "status": "error",
        "error_type": "runtime_setup",
        "message": message,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "requirements": str(requirements_path()),
        **details,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2


def run() -> int:
    if sys.version_info < MINIMUM_PYTHON:
        return bootstrap_error("recommend-papers requires Python 3.10 or newer.")
    try:
        from recommend_service.cli import main
    except ModuleNotFoundError as exc:
        return bootstrap_error(
            "A required Python package is missing. Install the bundled requirements with this same interpreter.",
            missing_module=exc.name or "unknown",
            install_command=f'"{sys.executable}" -m pip install -r "{requirements_path()}"',
        )
    return main()


if __name__ == "__main__":
    raise SystemExit(run())
