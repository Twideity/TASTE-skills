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
        "active_warnings": [],
        "warning_history": [],
        "resolved_warnings": [],
        "coverage_notices": [],
        "historical_incidents": [],
    })
    return run_dir


def require_run(path: Path, *, mutable: bool = True) -> Path:
    """Resolve an existing run; downstream commands must never create one by typo."""
    run_dir = safe_write_target(path)
    state = read_json(run_dir / "run.json", None)
    if not isinstance(state, dict) or clean_run_id(state.get("run_id")) != run_dir.name:
        raise ValueError(f"run-dir is not an initialized recommend-papers run: {run_dir}")
    if mutable and state.get("status") == "complete":
        raise ValueError(f"Run is complete and immutable; create a child run instead: {run_dir}")
    return run_dir


def clean_run_id(value: Any) -> str:
    return str(value or "").strip()


def update_run(
    run_dir: Path,
    *,
    stage: str,
    status: str = "active",
    counts: dict[str, int] | None = None,
    warnings: list[str] | None = None,
    coverage_notices: list[dict[str, Any]] | None = None,
    incidents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    path = run_dir / "run.json"
    lock_path = safe_write_target(run_dir / ".run-state.lock")
    with FileLock(str(lock_path)):
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
            merged_counts = dict(state.get("counts") or {})
            merged_counts.update(counts)
            state["counts"] = merged_counts
        if warnings is not None:
            previous = [str(item) for item in state.get("warnings") or []]
            current = [str(item) for item in warnings]
            history = list(state.get("warning_history") or [])
            changed_at = now_iso()
            for message in current:
                if message not in previous:
                    history.append({"action": "raised", "message": message, "stage": stage, "at": changed_at})
            for message in previous:
                if message not in current:
                    history.append({"action": "resolved", "message": message, "stage": stage, "at": changed_at})
            state["warnings"] = current
            state["active_warnings"] = current
            state["warning_history"] = history
            state["resolved_warnings"] = list(dict.fromkeys(
                item["message"]
                for item in history
                if isinstance(item, dict) and item.get("action") == "resolved" and item.get("message")
            ))
        if coverage_notices is not None:
            state["coverage_notices"] = coverage_notices
        if incidents:
            existing = list(state.get("historical_incidents") or [])
            known = {str(item.get("incident_id") or "") for item in existing if isinstance(item, dict)}
            for incident in incidents:
                incident_id = str(incident.get("incident_id") or stable_hash(incident))
                if incident_id not in known:
                    existing.append({**incident, "incident_id": incident_id})
                    known.add(incident_id)
            state["historical_incidents"] = existing
        write_json(path, state)
    return state
