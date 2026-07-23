from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .credentials import openreview_settings
from .claude_reads import claude_status, run_claude_reads
from .metadata import catalog, clean, metadata_cache_inventory, migrate_metadata_caches, probe_venue
from .pipeline import build_random_venue_shortlist, build_shortlist, complete, finalize, finish_stage, replace_failed_random_venue_papers, run_fulltext, run_metadata
from .reading_artifacts import prepare_fast_batches, prepare_reads, validate_reads
from .storage import CACHE_ROOT, RUNS_ROOT, STATE_ROOT, create_run, git_root_for, read_json, require_run, write_json


PROBE_DIAGNOSTIC_SAMPLE_LIMIT = 3


def _command_exit_code(command: str, result: dict[str, Any]) -> int:
    """Partial/action-required states must not look like successful endpoints."""
    status = clean(result.get("status")).lower()
    accepted = {
        "doctor": {"ok"},
        "cache-status": {"ok"},
        "migrate-metadata-cache": {"complete"},
        "init-run": {"initialized"},
        "probe-venue": {"probe_available"},
        "metadata": {"complete"},
        "random-venue-shortlist": {"complete"},
        "replace-failed-random-venue": {"complete"},
        "fulltext": {"complete", "complete_with_gaps"},
        "prepare-reads": {"pending", "ready_for_validation"},
        "claude-reads": {"complete"},
        "prepare-fast-read-batches": {"ready"},
        "validate-reads": {"complete"},
        "complete": {"complete"},
        "finish-stage": {"complete"},
    }
    if command in accepted:
        return 0 if status in accepted[command] else 2
    return 2 if status in {"error", "blocked", "probe_error", "temporarily_unresolved", "unavailable"} else 0


def initialize_run(*, parent_run_dir: Path | None = None, mode: str = "", question: str = "") -> dict[str, Any]:
    parent_payload: dict[str, Any] = {}
    if parent_run_dir is not None:
        parent = require_run(parent_run_dir, mutable=False)
        parent_state = read_json(parent / "run.json", None)
        if parent_state.get("status") != "complete":
            raise ValueError("parent-run-dir must be complete and immutable; continue an active run directly")
        artifact_names = ("plan.json", "metadata.json", "shortlist.json", "full_text_results.json", "read_artifacts.json", "evidence_cards.validated.json", "final_ranking.json", "recommendations.md")
        parent_plan = read_json(parent / "plan.json", {})
        parent_workflow = (parent_plan.get("workflow") if isinstance(parent_plan, dict) else {}) or {}
        inherited_settings = {}
        if (
            isinstance(parent_workflow, dict)
            and parent_workflow.get("user_disabled_claude") is True
            and parent_workflow.get("conversation_reading_preference_locked") is True
            and clean(parent_workflow.get("reading_preference_scope")).lower() == "conversation"
        ):
            inherited_settings = {
                "reading_preference": "codex_fast",
                "user_disabled_claude": True,
                "reading_preference_scope": "conversation",
                "conversation_reading_preference_locked": True,
            }
        parent_payload = {
            "parent_run_id": parent_state["run_id"],
            "parent_run_dir": str(parent),
            "available_parent_artifacts": {name: str(parent / name) for name in artifact_names if (parent / name).is_file()},
            **({"inherited_workflow_settings": inherited_settings} if inherited_settings else {}),
        }
    run_dir = create_run()
    continuation = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "research_mode": clean(mode) or "auto",
        "research_question": clean(question),
        **parent_payload,
    }
    write_json(run_dir / "continuation.json", continuation)
    state = read_json(run_dir / "run.json", {})
    state.update({"research_mode": continuation["research_mode"], "research_question": continuation["research_question"], **parent_payload})
    write_json(run_dir / "run.json", state)
    return {"status": "initialized", "run_id": run_dir.name, "run_dir": str(run_dir), "plan_path": str(run_dir / "plan.json"), "continuation": continuation}


def doctor() -> dict[str, Any]:
    dependencies = {
        name: importlib.util.find_spec(name) is not None
        for name in ("bs4", "dotenv", "filelock", "fitz", "openreview", "requests")
    }
    python_supported = sys.version_info >= (3, 10)
    conda_environment = str(os.environ.get("CONDA_DEFAULT_ENV") or "")
    virtual_environment = str(os.environ.get("VIRTUAL_ENV") or "")
    is_virtual_environment = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    environment_type = "venv" if is_virtual_environment else "conda" if conda_environment else "system-or-managed"
    environment_name = Path(sys.prefix).name if is_virtual_environment else conda_environment
    openreview_config = openreview_settings()
    checks = {}
    for label, root in (("state_root", STATE_ROOT), ("cache_root", CACHE_ROOT)):
        try:
            if git_root_for(root) is not None:
                raise ValueError(f"path is inside Git repository {git_root_for(root)}")
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=root, prefix=".doctor-", delete=False) as handle:
                path = Path(handle.name)
                handle.write("ok\n")
            path.unlink()
            checks[label] = {"ok": True, "path": str(root)}
        except Exception as exc:
            checks[label] = {"ok": False, "path": str(root), "error_type": type(exc).__name__, "message": str(exc)}
    ok = python_supported and all(dependencies.values()) and all(item["ok"] for item in checks.values())
    claude = claude_status()
    return {
        "status": "ok" if ok else "error",
        "version": __version__,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "python_version_supported": python_supported,
        "environment": {
            "type": environment_type,
            "name": environment_name or (Path(virtual_environment).name if virtual_environment else ""),
        },
        "standalone": True,
        "dependencies": dependencies,
        "openreview": {
            "env_file": str(openreview_config["env_file"]),
            "env_file_exists": openreview_config["env_file_exists"],
            "authenticated_credentials_configured": openreview_config["authenticated"],
            "access_mode": "authenticated" if openreview_config["authenticated"] else "anonymous",
            "anonymous_fallback_enabled": True,
        },
        "claude": {**claude, "fallback": "three_direct_codex_batch_subagents"},
        "paths": {"state_root": str(STATE_ROOT), "cache_root": str(CACHE_ROOT), "metadata_cache_root": metadata_cache_inventory()["authoritative_root"], "runs_root": str(RUNS_ROOT), "checks": checks},
        "metadata_cache": metadata_cache_inventory(),
    }


def inspect_run(run_dir: Path) -> dict[str, Any]:
    root = require_run(run_dir, mutable=False)
    files = [{"path": str(path), "bytes": path.stat().st_size} for path in sorted(root.rglob("*")) if path.is_file()]
    return {"run_dir": str(root), "state": read_json(root / "run.json", {}), "file_count": len(files), "files": files}


def _compact_metadata_result(result: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(clean(result.get("run_dir")))
    receipts = result.get("source_receipts") if isinstance(result.get("source_receipts"), list) else []
    return {
        "schema_version": result.get("schema_version"),
        "run_id": result.get("run_id"),
        "run_dir": str(run_dir),
        "status": result.get("status"),
        "raw_count": result.get("raw_count"),
        "deduplicated_count": result.get("deduplicated_count"),
        "metadata_profile": result.get("metadata_profile"),
        "coverage_notice_count": len(result.get("coverage_notices") or []),
        "metadata_path": str(run_dir / "metadata.json"),
        "source_receipts": [
            {
                "index": row.get("index"),
                "venue": (row.get("source") or {}).get("venue_id") or (row.get("source") or {}).get("venue"),
                "year": ((row.get("source") or {}).get("years") or [None])[0],
                "status": row.get("status"),
                "paper_count": row.get("paper_count"),
                "cache_status": (row.get("cache") or {}).get("status"),
            }
            for row in receipts if isinstance(row, dict)
        ],
        "warnings": result.get("warnings") or [],
    }


def _compact_shortlist_result(result: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": result.get("schema_version"),
        "run_id": result.get("run_id"),
        "status": result.get("status") or "complete",
        "selection_method": result.get("selection_method"),
        "seed": result.get("seed"),
        "per_venue": result.get("per_venue"),
        "target_count": result.get("target_count"),
        "actual_count": result.get("actual_count"),
        "replacement_round": result.get("replacement_round"),
        "replacement_count": result.get("replacement_count"),
        "replacement_count_last_round": result.get("replacement_count_last_round"),
        "replacement_count_total": result.get("replacement_count_total"),
        "attempted_identity_count": result.get("attempted_identity_count"),
        "strata": [
            {key: row.get(key) for key in ("venue", "year", "population_count", "sample_count", "kept_ready_count", "replacement_count") if key in row}
            for row in result.get("strata") or [] if isinstance(row, dict)
        ],
        "shortlist_path": str(run_dir / "shortlist.json"),
    }


def _compact_fulltext_result(result: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": result.get("schema_version"),
        "run_id": result.get("run_id"),
        "status": result.get("status"),
        "requested_count": result.get("requested_count"),
        "full_text_ready_count": result.get("full_text_ready_count"),
        "worker_count": result.get("worker_count"),
        "cooldown_requeue": result.get("cooldown_requeue") or {},
        "history_entry_path": result.get("history_entry_path"),
        "history_invocation_index": result.get("history_invocation_index"),
        "full_text_results_path": str(run_dir / "full_text_results.json"),
    }


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "doctor":
        return doctor()
    if args.command == "cache-status":
        return {"status": "ok", **metadata_cache_inventory()}
    if args.command == "migrate-metadata-cache":
        return migrate_metadata_caches()
    if args.command == "init-run":
        return initialize_run(parent_run_dir=args.parent_run_dir, mode=args.mode, question=args.question)
    if args.command == "catalog":
        return catalog(args.query)
    if args.command == "probe-venue":
        return probe_venue(
            {"type": "venue", "venue_id": args.venue_id, "venue": args.venue, "adapter": args.adapter, "openreview_venue_id": args.openreview_venue_id},
            args.start_year,
            args.lookback,
            PROBE_DIAGNOSTIC_SAMPLE_LIMIT,
            args.run_dir,
        )
    if args.command == "metadata":
        return _compact_metadata_result(run_metadata(args.plan.resolve(), args.run_dir))
    if args.command == "shortlist":
        return build_shortlist(args.run_dir.resolve(), args.scores.resolve(), args.target)
    if args.command == "random-venue-shortlist":
        directory = args.run_dir.resolve()
        return _compact_shortlist_result(build_random_venue_shortlist(directory, args.per_venue, args.seed), directory)
    if args.command == "replace-failed-random-venue":
        directory = args.run_dir.resolve()
        return _compact_shortlist_result(replace_failed_random_venue_papers(directory), directory)
    if args.command == "fulltext":
        directory = args.run_dir.resolve()
        return _compact_fulltext_result(run_fulltext(directory, args.workers), directory)
    if args.command == "prepare-reads":
        return prepare_reads(args.run_dir.resolve())
    if args.command == "claude-reads":
        return run_claude_reads(args.run_dir.resolve(), timeout_sec=args.timeout, only_failed=args.only_failed, workers=args.workers)
    if args.command == "prepare-fast-read-batches":
        return prepare_fast_batches(args.run_dir.resolve())
    if args.command == "validate-reads":
        return validate_reads(args.run_dir.resolve())
    if args.command == "finalize":
        return finalize(args.run_dir.resolve(), args.evidence_cards.resolve(), args.target)
    if args.command == "complete":
        return complete(args.run_dir.resolve(), args.recommendations.resolve())
    if args.command == "finish-stage":
        return finish_stage(args.run_dir.resolve(), args.stage)
    return inspect_run(args.run_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone private service for the recommend-papers Codex skill")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("cache-status")
    sub.add_parser("migrate-metadata-cache")
    initialize = sub.add_parser("init-run")
    initialize.add_argument("--parent-run-dir", type=Path)
    initialize.add_argument("--mode", choices=["auto", "comprehensive", "focused", "incremental", "metadata_only"], default="auto")
    initialize.add_argument("--question", default="")
    cat = sub.add_parser("catalog")
    cat.add_argument("--query", default="")
    probe = sub.add_parser("probe-venue")
    probe.add_argument("--venue-id", default="")
    probe.add_argument("--venue", default="")
    probe.add_argument("--adapter", choices=["dblp", "openreview", "neurips_official", "icml_official", "acm_enriched", "aaai_ojs", "cvf_openaccess", "acl_anthology", "ijcai_proceedings", "eccv_virtual"], default="")
    probe.add_argument("--openreview-venue-id", default="")
    probe.add_argument("--start-year", type=int, default=datetime.now().year)
    probe.add_argument("--lookback", type=int, default=5)
    probe.add_argument("--run-dir", type=Path, required=True)
    meta = sub.add_parser("metadata")
    meta.add_argument("--plan", type=Path, required=True)
    meta.add_argument("--run-dir", type=Path, required=True)
    shortlist = sub.add_parser("shortlist")
    shortlist.add_argument("--run-dir", type=Path, required=True)
    shortlist.add_argument("--scores", type=Path, required=True)
    shortlist.add_argument("--target", type=int)
    random_shortlist = sub.add_parser("random-venue-shortlist")
    random_shortlist.add_argument("--run-dir", type=Path, required=True)
    random_shortlist.add_argument("--per-venue", type=int, required=True)
    random_shortlist.add_argument("--seed", type=int)
    replace_random = sub.add_parser("replace-failed-random-venue")
    replace_random.add_argument("--run-dir", type=Path, required=True)
    full = sub.add_parser("fulltext")
    full.add_argument("--run-dir", type=Path, required=True)
    full.add_argument("--workers", type=int, default=8)
    prepare = sub.add_parser("prepare-reads")
    prepare.add_argument("--run-dir", type=Path, required=True)
    claude_reads = sub.add_parser("claude-reads")
    claude_reads.add_argument("--run-dir", type=Path, required=True)
    claude_reads.add_argument("--timeout", type=int, default=1800)
    claude_reads.add_argument("--workers", type=int, default=16, help="Claude concurrency, capped at 16")
    claude_reads.add_argument("--only-failed", action="store_true")
    fast_batches = sub.add_parser("prepare-fast-read-batches")
    fast_batches.add_argument("--run-dir", type=Path, required=True)
    validate = sub.add_parser("validate-reads")
    validate.add_argument("--run-dir", type=Path, required=True)
    final = sub.add_parser("finalize")
    final.add_argument("--run-dir", type=Path, required=True)
    final.add_argument("--evidence-cards", type=Path, required=True)
    final.add_argument("--target", type=int)
    done = sub.add_parser("complete")
    done.add_argument("--run-dir", type=Path, required=True)
    done.add_argument("--recommendations", type=Path, required=True)
    finish = sub.add_parser("finish-stage")
    finish.add_argument("--run-dir", type=Path, required=True)
    finish.add_argument("--stage", choices=["metadata", "shortlist", "fulltext", "reading"], required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = _execute(args)
    except Exception as exc:
        result = {
            "status": "error",
            "command": args.command,
            "error_type": type(exc).__name__,
            "message": str(exc)[:2000],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _command_exit_code(args.command, result)
