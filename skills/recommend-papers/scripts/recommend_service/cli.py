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
from .pipeline import build_shortlist, complete, finalize, finish_stage, run_fulltext, run_metadata
from .reading_artifacts import prepare_fast_batches, prepare_reads, validate_reads
from .storage import CACHE_ROOT, RUNS_ROOT, STATE_ROOT, create_run, git_root_for, read_json, write_json


def initialize_run(*, parent_run_dir: Path | None = None, mode: str = "", question: str = "") -> dict[str, Any]:
    parent_payload: dict[str, Any] = {}
    if parent_run_dir is not None:
        parent = parent_run_dir.expanduser().resolve()
        parent_state = read_json(parent / "run.json", None)
        if not isinstance(parent_state, dict) or not parent_state.get("run_id"):
            raise ValueError("parent-run-dir must point to an existing recommend-papers run")
        artifact_names = ("plan.json", "metadata.json", "shortlist.json", "full_text_results.json", "read_artifacts.json", "evidence_cards.validated.json", "final_ranking.json", "recommendations.md")
        parent_payload = {
            "parent_run_id": parent_state["run_id"],
            "parent_run_dir": str(parent),
            "available_parent_artifacts": {name: str(parent / name) for name in artifact_names if (parent / name).is_file()},
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
    root = run_dir.expanduser().resolve()
    files = [{"path": str(path), "bytes": path.stat().st_size} for path in sorted(root.rglob("*")) if path.is_file()]
    return {"run_dir": str(root), "state": read_json(root / "run.json", {}), "file_count": len(files), "files": files}


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
    probe.add_argument("--sample-limit", type=int, default=3)
    meta = sub.add_parser("metadata")
    meta.add_argument("--plan", type=Path, required=True)
    meta.add_argument("--run-dir", type=Path)
    shortlist = sub.add_parser("shortlist")
    shortlist.add_argument("--run-dir", type=Path, required=True)
    shortlist.add_argument("--scores", type=Path, required=True)
    shortlist.add_argument("--target", type=int, default=100)
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
    final.add_argument("--target", type=int, default=20)
    done = sub.add_parser("complete")
    done.add_argument("--run-dir", type=Path, required=True)
    done.add_argument("--recommendations", type=Path, required=True)
    finish = sub.add_parser("finish-stage")
    finish.add_argument("--run-dir", type=Path, required=True)
    finish.add_argument("--stage", choices=["metadata", "shortlist", "fulltext", "reading"], required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        result = doctor()
    elif args.command == "cache-status":
        result = {"status": "ok", **metadata_cache_inventory()}
    elif args.command == "migrate-metadata-cache":
        result = migrate_metadata_caches()
    elif args.command == "init-run":
        result = initialize_run(parent_run_dir=args.parent_run_dir, mode=args.mode, question=args.question)
    elif args.command == "catalog":
        result = catalog(args.query)
    elif args.command == "probe-venue":
        result = probe_venue({"type": "venue", "venue_id": args.venue_id, "venue": args.venue, "adapter": args.adapter, "openreview_venue_id": args.openreview_venue_id}, args.start_year, args.lookback, args.sample_limit)
    elif args.command == "metadata":
        result = run_metadata(args.plan.resolve(), args.run_dir)
    elif args.command == "shortlist":
        result = build_shortlist(args.run_dir.resolve(), args.scores.resolve(), args.target)
    elif args.command == "fulltext":
        result = run_fulltext(args.run_dir.resolve(), args.workers)
    elif args.command == "prepare-reads":
        result = prepare_reads(args.run_dir.resolve())
    elif args.command == "claude-reads":
        result = run_claude_reads(args.run_dir.resolve(), timeout_sec=args.timeout, only_failed=args.only_failed, workers=args.workers)
    elif args.command == "prepare-fast-read-batches":
        result = prepare_fast_batches(args.run_dir.resolve())
    elif args.command == "validate-reads":
        result = validate_reads(args.run_dir.resolve())
    elif args.command == "finalize":
        result = finalize(args.run_dir.resolve(), args.evidence_cards.resolve(), args.target)
    elif args.command == "complete":
        result = complete(args.run_dir.resolve(), args.recommendations.resolve())
    elif args.command == "finish-stage":
        result = finish_stage(args.run_dir.resolve(), args.stage)
    else:
        result = inspect_run(args.run_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") not in {"error", "blocked"} else 2
