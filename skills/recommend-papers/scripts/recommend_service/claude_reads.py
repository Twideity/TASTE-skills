from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .metadata import clean
from .storage import now_iso, read_json, safe_write_target, update_run, write_json, write_text

MAX_CLAUDE_CONCURRENCY = 16


def find_claude() -> str:
    explicit = clean(os.environ.get("CLAUDE_PATH"))
    if explicit and Path(explicit).expanduser().is_file():
        return str(Path(explicit).expanduser().resolve())
    return shutil.which("claude") or ""


def claude_status() -> dict[str, Any]:
    claude = find_claude()
    if not claude:
        return {"available": False, "installed": False, "authenticated": False, "path": "", "reason": "claude_cli_not_found"}
    if clean(os.environ.get("ANTHROPIC_API_KEY")):
        return {"available": True, "installed": True, "authenticated": True, "path": claude, "auth_method": "environment_api_key"}
    try:
        process = subprocess.run([claude, "auth", "status", "--json"], text=True, capture_output=True, timeout=15, check=False)
        payload = json.loads(process.stdout) if process.stdout.strip() else {}
        authenticated = process.returncode == 0 and payload.get("loggedIn") is True
        return {
            "available": authenticated,
            "installed": True,
            "authenticated": authenticated,
            "path": claude,
            "auth_method": clean(payload.get("authMethod") or "none"),
            "reason": "" if authenticated else "claude_not_authenticated",
        }
    except Exception as exc:
        return {"available": False, "installed": True, "authenticated": False, "path": claude, "reason": "claude_auth_status_failed", "error_type": type(exc).__name__}


def _queue_items(run_dir: Path, *, only_failed: bool) -> list[dict[str, Any]]:
    queue = read_json(run_dir / "reading_queue.json", {})
    items = [item for item in queue.get("pending") or [] if isinstance(item, dict)]
    if not only_failed:
        return items
    failures = read_json(run_dir / "read_artifacts.json", {}).get("failures") or []
    failed = {clean(item.get("identity")) for item in failures if isinstance(item, dict)}
    return [item for item in items if clean(item.get("identity")) in failed]


def _repair_suffix(run_dir: Path, identity: str) -> str:
    failures = read_json(run_dir / "read_artifacts.json", {}).get("failures") or []
    failure = next((row for row in failures if isinstance(row, dict) and clean(row.get("identity")) == identity), {})
    errors = [clean(error) for error in failure.get("errors") or [] if clean(error)]
    if not errors:
        return ""
    listed = "\n".join(f"- {error}" for error in errors)
    return f"""

这是一次全新的 Claude 最小质量修复调用。本段覆盖前文的首次精读要求：先完整读取已有 read.md，只修复下列验证错误；保留所有已经正确的科学内容，不得借机重写整篇。修改后更新同一个 receipt 的 read_sha256 和其他受影响字段：
{listed}
"""


def _run_one(claude: str, item: dict[str, Any], timeout_sec: int, repair_suffix: str) -> dict[str, Any]:
    paper_dir = Path(clean(item.get("read_path"))).expanduser().parent
    paper_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = Path(clean(item.get("prompt_path"))).expanduser()
    prompt = prompt_path.read_text(encoding="utf-8") + repair_suffix
    full_text_path = Path(clean(item.get("full_text_path"))).expanduser()
    command = [
        claude,
        "-p",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        "--disallowedTools",
        "Agent,Task,EnterWorktree,ExitWorktree",
        "--add-dir",
        str(paper_dir),
    ]
    if full_text_path.parent != paper_dir:
        command.extend(["--add-dir", str(full_text_path.parent)])
    environment = os.environ.copy()
    temp_dir = paper_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    environment.update({"TMPDIR": str(temp_dir), "TMP": str(temp_dir), "TEMP": str(temp_dir)})
    started = time.time()
    try:
        process = subprocess.run(
            command,
            cwd=paper_dir,
            env=environment,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=max(60, int(timeout_sec)),
            check=False,
        )
        write_text(paper_dir / "claude_stdout.json", process.stdout)
        write_text(paper_dir / "claude_stderr.log", process.stderr)
        receipt_path = Path(clean(item.get("receipt_path"))).expanduser()
        read_path = Path(clean(item.get("read_path"))).expanduser()
        failure_text = f"{process.stdout}\n{process.stderr}".lower()
        unavailable = process.returncode != 0 and any(token in failure_text for token in ("not logged in", "authentication", "unauthorized", "api key", "credit balance"))
        success = process.returncode == 0 and receipt_path.is_file() and read_path.is_file()
        return {
            "identity": item.get("identity"),
            "status": "complete" if success else "claude_unavailable" if unavailable else "failed",
            "return_code": process.returncode,
            "duration_seconds": round(time.time() - started, 3),
            "read_path": str(read_path),
            "receipt_path": str(receipt_path),
            "stdout_path": str(paper_dir / "claude_stdout.json"),
            "stderr_path": str(paper_dir / "claude_stderr.log"),
        }
    except subprocess.TimeoutExpired as exc:
        write_text(paper_dir / "claude_stderr.log", clean(exc.stderr))
        return {
            "identity": item.get("identity"),
            "status": "timeout",
            "duration_seconds": round(time.time() - started, 3),
            "timeout_seconds": timeout_sec,
        }
    except Exception as exc:
        return {
            "identity": item.get("identity"),
            "status": "failed",
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
            "duration_seconds": round(time.time() - started, 3),
        }


def run_claude_reads(run_dir: Path, *, timeout_sec: int = 1800, only_failed: bool = False, workers: int = MAX_CLAUDE_CONCURRENCY) -> dict[str, Any]:
    directory = safe_write_target(run_dir)
    availability = claude_status()
    claude = clean(availability.get("path"))
    items = _queue_items(directory, only_failed=only_failed)
    if availability.get("available") is not True:
        payload = {
            "schema_version": 1,
            "status": "claude_unavailable",
            "fallback_required": True,
            "requested_count": len(items),
            "claude": availability,
            "message": "Claude CLI is unavailable; use exactly three direct Codex batch-reading subagents.",
            "generated_at": now_iso(),
        }
        write_json(directory / "claude_read_results.json", payload)
        return payload
    if not items:
        return {"schema_version": 1, "status": "complete", "requested_count": 0, "completed_count": 0, "worker_count": 0, "max_concurrency": MAX_CLAUDE_CONCURRENCY, "pipeline_refill": True, "items": [], "generated_at": now_iso()}
    # Keep a bounded pool: as soon as one Claude finishes, the executor starts
    # the next paper with a fresh Claude process until the queue is exhausted.
    worker_count = max(1, min(MAX_CLAUDE_CONCURRENCY, int(workers or MAX_CLAUDE_CONCURRENCY), len(items)))
    results = []
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(_run_one, claude, item, timeout_sec, _repair_suffix(directory, clean(item.get("identity"))) if only_failed else ""): item
            for item in items
        }
        for future in as_completed(futures):
            results.append(future.result())
    order = {clean(item.get("identity")): index for index, item in enumerate(items)}
    results.sort(key=lambda item: order.get(clean(item.get("identity")), len(order)))
    completed = sum(item.get("status") == "complete" for item in results)
    unavailable = bool(results) and all(item.get("status") == "claude_unavailable" for item in results)
    payload = {
        "schema_version": 1,
        "status": "claude_unavailable" if unavailable else "complete" if completed == len(items) else "complete_with_failures",
        "fallback_required": unavailable,
        "reader": "external_claude_cli",
        "requested_count": len(items),
        "completed_count": completed,
        "failed_count": len(items) - completed,
        "worker_count": worker_count,
        "max_concurrency": MAX_CLAUDE_CONCURRENCY,
        "pipeline_refill": True,
        "pipeline_batch_count": (len(items) + worker_count - 1) // worker_count,
        "only_failed_repair": only_failed,
        "items": results,
        "generated_at": now_iso(),
    }
    write_json(directory / "claude_read_results.json", payload)
    update_run(directory, stage="claude_single_paper_reading", counts={"claude_reads_requested": len(items), "claude_reads_completed": completed, "claude_concurrency": worker_count})
    return payload
