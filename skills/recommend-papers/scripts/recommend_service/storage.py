from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout


APP_NAMESPACE = Path("taste") / "recommend-papers"


def env_path(name: str, fallback: Path, *, ignore_relative: bool = False) -> Path:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return fallback.expanduser().resolve(strict=False)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if ignore_relative:
            return fallback.expanduser().resolve(strict=False)
        raise ValueError(f"{name} must be an absolute path")
    return candidate.resolve(strict=False)


XDG_STATE_HOME = env_path("XDG_STATE_HOME", Path.home() / ".local" / "state", ignore_relative=True)
XDG_CACHE_HOME = env_path("XDG_CACHE_HOME", Path.home() / ".cache", ignore_relative=True)
_legacy_state_override = str(os.environ.get("RECOMMEND_PAPERS_DATA_DIR") or "").strip()
STATE_ROOT = env_path(
    "RECOMMEND_PAPERS_STATE_DIR",
    env_path("RECOMMEND_PAPERS_DATA_DIR", XDG_STATE_HOME / APP_NAMESPACE) if _legacy_state_override else XDG_STATE_HOME / APP_NAMESPACE,
)
CACHE_ROOT = env_path("RECOMMEND_PAPERS_CACHE_DIR", XDG_CACHE_HOME / APP_NAMESPACE)
# Compatibility alias for internal modules written before the XDG state migration.
DATA_ROOT = STATE_ROOT
RUNS_ROOT = DATA_ROOT / "runs"
METADATA_CACHE_ROOT = CACHE_ROOT / "metadata"
FULLTEXT_CACHE_ROOT = CACHE_ROOT / "fulltext"
HTTP_STATE_ROOT = DATA_ROOT / "state" / "http"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def run_lock(run_dir: Path, operation: str):
    """Prevent two writers for the same long-running run operation."""
    lock_path = safe_write_target(run_dir / f".{operation}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise RuntimeError(
            f"{operation} is already running for {run_dir}. Resume/wait for that process; do not start a replacement."
        ) from exc
    try:
        yield
    finally:
        lock.release()


def git_root_for(path: Path) -> Path | None:
    candidate = path.expanduser().resolve(strict=False)
    for current in (candidate, *candidate.parents):
        marker = current / ".git"
        if marker.exists():
            return current
    return None


def allow_git_writes() -> bool:
    return str(os.environ.get("RECOMMEND_PAPERS_ALLOW_GIT_WRITES") or "").strip().lower() in {"1", "true", "yes"}


def safe_write_target(path: Path) -> Path:
    target = path.expanduser().resolve(strict=False)
    root = git_root_for(target)
    if root is not None and not allow_git_writes():
        raise ValueError(
            f"Refusing to write recommend-papers runtime data inside Git repository {root}: {target}. "
            "Use the external run directory or explicitly set RECOMMEND_PAPERS_ALLOW_GIT_WRITES=1."
        )
    return target


def write_json(path: Path, value: Any) -> Path:
    target = safe_write_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, prefix=target.name + ".", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return target


def write_text(path: Path, value: str) -> Path:
    target = safe_write_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, prefix=target.name + ".", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return target


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def create_run() -> Path:
    root = safe_write_target(RUNS_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    while True:
        run_dir = root / utc_run_id()
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            continue
    write_json(run_dir / "run.json", {
        "schema_version": 1,
        "run_id": run_dir.name,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "stage": "initialized",
        "status": "active",
        "counts": {},
        "warnings": [],
    })
    return run_dir


def ensure_run(path: Path | None) -> Path:
    if path is None:
        return create_run()
    run_dir = safe_write_target(path)
    run_dir.mkdir(parents=True, exist_ok=True)
    if not (run_dir / "run.json").exists():
        write_json(run_dir / "run.json", {
            "schema_version": 1,
            "run_id": run_dir.name,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "stage": "initialized",
            "status": "active",
            "counts": {},
            "warnings": [],
        })
    return run_dir


def update_run(run_dir: Path, *, stage: str, status: str = "active", counts: dict[str, int] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    path = run_dir / "run.json"
    state = read_json(path, {})
    if not isinstance(state, dict):
        state = {}
    state.update({
        "schema_version": 1,
        "run_id": run_dir.name,
        "updated_at": now_iso(),
        "stage": stage,
        "status": status,
    })
    if counts is not None:
        state["counts"] = counts
    if warnings is not None:
        state["warnings"] = warnings
    write_json(path, state)
    return state
