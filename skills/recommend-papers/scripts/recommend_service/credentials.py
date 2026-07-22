from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


SKILL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPENREVIEW_ENV = SKILL_ROOT / "read.env"


def openreview_settings() -> dict[str, Any]:
    configured = str(os.environ.get("RECOMMEND_PAPERS_OPENREVIEW_ENV_FILE") or "").strip()
    path = Path(configured).expanduser().resolve(strict=False) if configured else DEFAULT_OPENREVIEW_ENV
    values = dotenv_values(path) if path.is_file() else {}
    username = str(os.environ.get("OPENREVIEW_USERNAME") or values.get("OPENREVIEW_USERNAME") or "").strip()
    password = str(os.environ.get("OPENREVIEW_PASSWORD") or values.get("OPENREVIEW_PASSWORD") or "").strip()
    return {
        "env_file": path,
        "env_file_exists": path.is_file(),
        "username": username,
        "password": password,
        "authenticated": bool(username and password),
    }
