from __future__ import annotations

import json
import os
import random
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .channels.registry import canonical as canonical_channel
from .fulltext import acquire_many
from .metadata import clean, deduplicate, fetch_source, migrate_metadata_caches, paper_identity, validate_plan
from .reading_artifacts import READ_CONTRACT_VERSION
from .storage import now_iso, read_json, require_run, run_lock, stable_hash, update_run, write_json, write_text


METADATA_COMPONENTS = ("topic_fit", "transferability_potential", "abstract_specificity")
METADATA_MAXIMA = {"topic_fit": 50, "transferability_potential": 30, "abstract_specificity": 20}
FULLTEXT_COMPONENTS = ("match_score", "transferability_score")
FULLTEXT_MAXIMA = {"match_score": 10, "transferability_score": 10}
STAGE_ORDER = {"metadata": 0, "shortlist": 1, "fulltext": 2, "reading": 3, "recommendation": 4}


def _number(value: Any, *, label: str, minimum: float = 0, maximum: float = 100) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return result


def _require_stage_allowed(directory: Path, stage: str) -> dict[str, Any]:
    plan = read_json(directory / "plan.json", {})
    workflow = plan.get("workflow") if isinstance(plan, dict) and isinstance(plan.get("workflow"), dict) else {}
    stop_after = clean(workflow.get("stop_after")).lower()
    if stop_after not in STAGE_ORDER:
        raise ValueError("plan.json lacks a valid workflow.stop_after")
    if STAGE_ORDER[stage] > STAGE_ORDER[stop_after]:
        raise ValueError(f"Plan stops after {stop_after}; {stage} would exceed the authorized workflow")
    return plan


def _require_plan_unchanged(directory: Path, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    current = plan if isinstance(plan, dict) else read_json(directory / "plan.json", {})
    metadata = read_json(directory / "metadata.json", {})
    if not isinstance(current, dict) or metadata.get("plan_fingerprint") != stable_hash(current):
        raise ValueError("plan.json changed after metadata acquisition; create a child run or rerun metadata")
    return current


def run_metadata(plan_path: Path, run_dir: Path | None = None) -> dict[str, Any]:
    if run_dir is None:
        raise ValueError("metadata requires an explicit initialized run-dir")
    directory = require_run(run_dir)
    if plan_path.expanduser().resolve() != (directory / "plan.json").resolve():
        raise ValueError("metadata plan must be the plan.json inside the initialized run directory")
    with run_lock(directory, "metadata"):
        return _run_metadata_locked(plan_path, directory)


def _source_field_coverage(papers: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(papers)
    counts = {
        "title": sum(bool(clean(item.get("title"))) for item in papers),
        "authors": sum(bool(item.get("authors")) for item in papers),
        "abstract": sum(bool(clean(item.get("abstract"))) for item in papers),
        "url": sum(bool(clean(item.get("url"))) for item in papers),
        "pdf_url": sum(bool(clean(item.get("pdf_url"))) for item in papers),
    }
    return {
        "record_count": total,
        **{f"{field}_count": count for field, count in counts.items()},
        **{f"{field}_coverage": round(count / total, 6) if total else 0.0 for field, count in counts.items()},
    }


def _metadata_coverage_notices(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    for receipt in receipts:
        source = receipt.get("source") if isinstance(receipt.get("source"), dict) else {}
        coverage = receipt.get("field_coverage") if isinstance(receipt.get("field_coverage"), dict) else {}
        total = int(coverage.get("record_count") or 0)
        if total <= 0:
            continue
        for field in ("authors", "abstract", "pdf_url"):
            present = int(coverage.get(f"{field}_count") or 0)
            if present >= total:
                continue
            notices.append({
                "severity": "notice",
                "kind": f"{field}_incomplete",
                "source_index": receipt.get("index"),
                "venue": clean(source.get("venue_id") or source.get("venue")),
                "year": int((source.get("years") or [0])[0] or 0),
                "field": field,
                "record_count": total,
                "present_count": present,
                "missing_count": total - present,
                "coverage": coverage.get(f"{field}_coverage"),
            })
    return notices


def _run_metadata_locked(plan_path: Path, directory: Path) -> dict[str, Any]:
    migrate_metadata_caches()
    plan = validate_plan(read_json(plan_path, {}))
    continuation = read_json(directory / "continuation.json", {})
    inherited = continuation.get("inherited_workflow_settings") if isinstance(continuation, dict) else {}
    if isinstance(inherited, dict) and inherited:
        workflow = plan.get("workflow") or {}
        mismatched = [key for key, value in inherited.items() if workflow.get(key) != value]
        if mismatched:
            raise ValueError(
                "Child plan violates the inherited conversation-level reading preference: " + ", ".join(mismatched)
            )
    write_json(directory / "plan.json", plan)
    policy = clean(plan.get("cache_policy") or "reuse").lower()
    if policy not in {"reuse", "refresh", "only"}:
        raise ValueError("cache_policy must be reuse, refresh, or only")
    max_age_days = _number(plan.get("metadata_cache_max_age_days", 7), label="metadata_cache_max_age_days", maximum=3650)
    all_rows: list[dict[str, Any]] = []
    receipts_by_index: dict[int, dict[str, Any]] = {}
    papers_by_index: dict[int, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    sources = plan["sources"]
    update_run(directory, stage="metadata_crawl", counts={"sources": len(sources), "completed_sources": 0, "raw_papers": 0})
    receipt_dir = directory / "source_receipts"

    def fetch_one(index: int, spec: dict[str, Any]) -> tuple[int, list[dict[str, Any]], dict[str, Any], str]:
        try:
            papers, source_receipt = fetch_source(spec, policy=policy, max_age_days=max_age_days)
            details = source_receipt.get("details") if isinstance(source_receipt, dict) else None
            coverage_status = details.get("status") if isinstance(details, dict) else "complete"
            row = {
                "index": index,
                "source": spec,
                "status": coverage_status,
                "paper_count": len(papers),
                "field_coverage": _source_field_coverage(papers),
                "cache": source_receipt,
            }
            warning = f"Source {index} coverage is {coverage_status}; inspect receipt and repair before scoring" if coverage_status != "complete" else ""
        except Exception as exc:
            papers = []
            root = exc
            while root.__cause__ is not None:
                root = root.__cause__
            row = {
                "index": index,
                "source": spec,
                "status": "error",
                "paper_count": 0,
                "error_type": type(exc).__name__,
                "message": str(exc)[:1000],
                "root_error_type": type(root).__name__,
                "root_message": str(root)[:1000],
            }
            warning = f"Source {index} failed: {type(root).__name__}: {str(root)[:500]}"
        return index, papers, row, warning

    try:
        source_workers = max(1, min(8, int(os.environ.get("RECOMMEND_PAPERS_METADATA_SOURCE_WORKERS", "6") or 6), len(sources)))
    except (TypeError, ValueError):
        source_workers = min(6, len(sources))
    completed_sources = 0
    with ThreadPoolExecutor(max_workers=source_workers) as pool:
        futures = {pool.submit(fetch_one, index, spec): index for index, spec in enumerate(sources, 1)}
        for future in as_completed(futures):
            index, papers, row, warning = future.result()
            papers_by_index[index] = papers
            receipts_by_index[index] = row
            if warning:
                warnings.append(warning)
                warnings.sort()
            write_json(receipt_dir / f"{index:03d}.json", row)
            completed_sources += 1
            raw_count = sum(len(items) for items in papers_by_index.values())
            update_run(directory, stage="metadata_crawl", counts={"sources": len(sources), "completed_sources": completed_sources, "raw_papers": raw_count, "metadata_source_workers": source_workers}, warnings=warnings)
    receipts = [receipts_by_index[index] for index in range(1, len(sources) + 1)]
    all_rows = [paper for index in range(1, len(sources) + 1) for paper in papers_by_index.get(index, [])]
    papers = deduplicate(all_rows)
    coverage_notices = _metadata_coverage_notices(receipts)
    status = "complete" if papers and not warnings else "complete_with_gaps" if papers else "blocked"
    payload = {
        "schema_version": 1,
        "run_id": directory.name,
        "run_dir": str(directory),
        "status": status,
        "generated_at": now_iso(),
        "raw_count": len(all_rows),
        "deduplicated_count": len(papers),
        "plan_fingerprint": stable_hash(plan),
        "metadata_fingerprint": stable_hash({"plan": plan, "papers": papers}),
        "metadata_profile": "full_metadata",
        "coverage_notices": coverage_notices,
        "papers": papers,
        "source_receipts": receipts,
        "warnings": warnings,
    }
    write_json(directory / "metadata.json", payload)
    stage = "metadata_ready" if status == "complete" else "metadata_incomplete" if papers else "metadata_blocked"
    update_run(
        directory,
        stage=stage,
        status="active" if papers else "blocked",
        counts={"sources": len(sources), "completed_sources": len(sources), "raw_papers": len(all_rows), "metadata_papers": len(papers)},
        warnings=warnings,
        coverage_notices=coverage_notices,
    )
    return payload


def _score_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("scores", "papers", "items"):
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
    raise ValueError("Score artifact must be a list or contain scores/papers/items")


def build_shortlist(run_dir: Path, scores_path: Path, target: int | None) -> dict[str, Any]:
    directory = require_run(run_dir)
    plan = _require_stage_allowed(directory, "shortlist")
    _require_plan_unchanged(directory, plan)
    metadata = read_json(directory / "metadata.json", {})
    papers = metadata.get("papers") if isinstance(metadata, dict) else None
    if not isinstance(papers, list) or not papers:
        raise ValueError("metadata.json is missing or empty")
    if metadata.get("status") is not None and metadata.get("status") != "complete":
        raise ValueError("metadata coverage is incomplete; repair all source receipts before scoring")
    by_identity = {paper_identity(item): item for item in papers if isinstance(item, dict)}
    rows = _score_rows(read_json(scores_path, None))
    scored: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        identity = clean(row.get("identity"))
        if identity not in by_identity:
            raise ValueError(f"scores[{index}] identity is not in metadata: {identity}")
        if identity in scored:
            raise ValueError(f"Duplicate metadata score identity: {identity}")
        components = row.get("components") if isinstance(row.get("components"), dict) else {}
        values = {name: _number(components.get(name), label=f"scores[{index}].components.{name}", maximum=METADATA_MAXIMA[name]) for name in METADATA_COMPONENTS}
        total = round(sum(values.values()), 4)
        declared = _number(row.get("metadata_score"), label=f"scores[{index}].metadata_score")
        if abs(total - declared) > 0.01:
            raise ValueError(f"scores[{index}] metadata_score does not equal component sum")
        reason = clean(row.get("reason"))
        uncertainty = clean(row.get("uncertainty"))
        if not reason or not uncertainty:
            raise ValueError(f"scores[{index}] requires reason and uncertainty")
        scored[identity] = {"identity": identity, "metadata_score": total, "components": values, "reason": reason, "uncertainty": uncertainty}
    missing = sorted(set(by_identity) - set(scored))
    if missing:
        raise ValueError(f"Codex must score every metadata paper before shortlisting; missing={len(missing)}")
    ordered = sorted(scored.values(), key=lambda item: (-item["metadata_score"], item["identity"]))
    planned_target = (plan.get("workflow") or {}).get("shortlist_target")
    if target is not None and planned_target is not None and int(target) != int(planned_target):
        raise ValueError("--target conflicts with workflow.shortlist_target")
    target_count = int(target if target is not None else planned_target or 100)
    if target_count < 1:
        raise ValueError("shortlist target must be a positive integer")
    selected = ordered[:target_count]
    shortlist_papers = [{**by_identity[item["identity"]], "metadata_evaluation": item} for item in selected]
    scores_payload = {"schema_version": 1, "run_id": directory.name, "scored_count": len(ordered), "scores": ordered, "generated_at": now_iso()}
    write_json(directory / "metadata_scores.json", scores_payload)
    payload = {
        "schema_version": 1,
        "run_id": directory.name,
        "target_count": target_count,
        "actual_count": len(shortlist_papers),
        "shortfall": max(0, target_count - len(shortlist_papers)),
        "papers": shortlist_papers,
        "metadata_fingerprint": metadata.get("metadata_fingerprint"),
        "generated_at": now_iso(),
    }
    write_json(directory / "shortlist.json", payload)
    update_run(directory, stage="shortlist_ready", counts={"metadata_papers": len(papers), "metadata_scored": len(ordered), "shortlist_target": target_count, "shortlist_actual": len(shortlist_papers)})
    return payload


def build_random_venue_shortlist(run_dir: Path, per_venue: int, seed: int | None = None) -> dict[str, Any]:
    """Select an auditable uniform sample independently within every planned venue."""
    directory = require_run(run_dir)
    plan = _require_stage_allowed(directory, "shortlist")
    _require_plan_unchanged(directory, plan)
    metadata = read_json(directory / "metadata.json", {})
    papers = metadata.get("papers") if isinstance(metadata, dict) else None
    if not isinstance(papers, list) or not papers:
        raise ValueError("metadata.json is missing or empty")
    if metadata.get("status") != "complete":
        raise ValueError("metadata coverage is incomplete; repair all source receipts before random sampling")
    if per_venue < 1:
        raise ValueError("--per-venue must be a positive integer")

    venue_sources: list[tuple[str, int]] = []
    for index, source in enumerate(plan.get("sources") or []):
        if clean(source.get("type")).lower() != "venue":
            raise ValueError("random-venue-shortlist supports venue-only plans")
        venue = canonical_channel(source.get("venue_id") or source.get("venue"))
        years = source.get("years") or []
        if not venue or len(years) != 1:
            raise ValueError(f"sources[{index}] must identify exactly one venue and year")
        key = (venue, int(years[0]))
        if key in venue_sources:
            raise ValueError(f"Duplicate planned venue/year: {venue} {years[0]}")
        venue_sources.append(key)

    candidates: dict[tuple[str, int], list[dict[str, Any]]] = {key: [] for key in venue_sources}
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        try:
            key = (canonical_channel(paper.get("venue")), int(paper.get("year") or 0))
        except (TypeError, ValueError):
            continue
        if key in candidates:
            candidates[key].append(paper)
    short = {f"{venue}:{year}": len(rows) for (venue, year), rows in candidates.items() if len(rows) < per_venue}
    if short:
        raise ValueError(f"Not enough metadata papers for per-venue sampling: {short}")

    actual_seed = int(seed if seed is not None else secrets.randbits(64))
    generator = random.Random(actual_seed)
    selected: list[dict[str, Any]] = []
    strata: list[dict[str, Any]] = []
    for venue, year in venue_sources:
        population = sorted(candidates[(venue, year)], key=paper_identity)
        sample = generator.sample(population, per_venue)
        selected.extend(sample)
        strata.append({
            "venue": venue,
            "year": year,
            "population_count": len(population),
            "sample_count": len(sample),
            "identities": [paper_identity(item) for item in sample],
        })

    target_count = per_venue * len(venue_sources)
    planned_target = (plan.get("workflow") or {}).get("shortlist_target")
    if planned_target is not None and int(planned_target) != target_count:
        raise ValueError(
            f"Random venue sample size {target_count} conflicts with workflow.shortlist_target={planned_target}"
        )
    payload = {
        "schema_version": 1,
        "run_id": directory.name,
        "status": "complete",
        "selection_method": "uniform_random_without_replacement_stratified_by_venue",
        "seed": actual_seed,
        "per_venue": per_venue,
        "replacement_round": 0,
        "replacement_count": 0,
        "replacement_count_last_round": 0,
        "replacement_count_total": 0,
        "attempted_identities": sorted(paper_identity(item) for item in selected),
        "attempted_identity_count": len(selected),
        "target_count": target_count,
        "actual_count": len(selected),
        "shortfall": 0,
        "strata": strata,
        "papers": selected,
        "metadata_fingerprint": metadata.get("metadata_fingerprint"),
        "generated_at": now_iso(),
    }
    write_json(directory / "shortlist.json", payload)
    update_run(directory, stage="shortlist_ready", counts={
        "metadata_papers": len(papers),
        "shortlist_target": target_count,
        "shortlist_actual": len(selected),
        "shortlist_venues": len(venue_sources),
        "shortlist_per_venue": per_venue,
        "random_replacement_round": 0,
        "random_replacements": 0,
        "random_replacements_last_round": 0,
        "random_replacements_total": 0,
        "random_attempted_identities_total": len(selected),
    })
    return payload


def replace_failed_random_venue_papers(run_dir: Path) -> dict[str, Any]:
    """Keep acquired papers and draw same-venue replacements for failed random picks."""
    directory = require_run(run_dir)
    plan = _require_stage_allowed(directory, "fulltext")
    _require_plan_unchanged(directory, plan)
    shortlist = read_json(directory / "shortlist.json", {})
    fulltext = read_json(directory / "full_text_results.json", {})
    metadata = read_json(directory / "metadata.json", {})
    if shortlist.get("selection_method") != "uniform_random_without_replacement_stratified_by_venue":
        raise ValueError("replace-failed-random-venue requires a random venue shortlist")
    if fulltext.get("shortlist_fingerprint") != stable_hash(shortlist):
        raise ValueError("full_text_results.json is stale for the current shortlist")
    items = fulltext.get("items") if isinstance(fulltext.get("items"), list) else []
    if not items:
        raise ValueError("full_text_results.json has no acquisition results")
    per_venue = int(shortlist.get("per_venue") or 0)
    seed = int(shortlist.get("seed"))
    replacement_round = int(shortlist.get("replacement_round") or 0) + 1
    ready = {
        paper_identity(item.get("paper") or {}): item.get("paper")
        for item in items
        if isinstance(item, dict) and item.get("full_text_available") is True and isinstance(item.get("paper"), dict)
    }
    attempted = set(clean(value) for value in shortlist.get("attempted_identities") or [])
    attempted.update(paper_identity(item) for item in shortlist.get("papers") or [] if isinstance(item, dict))
    metadata_papers = metadata.get("papers") if isinstance(metadata.get("papers"), list) else []
    population: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for paper in metadata_papers:
        if not isinstance(paper, dict):
            continue
        try:
            key = (canonical_channel(paper.get("venue")), int(paper.get("year") or 0))
        except (TypeError, ValueError):
            continue
        population.setdefault(key, []).append(paper)

    selected: list[dict[str, Any]] = []
    strata: list[dict[str, Any]] = []
    replacement_count = 0
    for prior in shortlist.get("strata") or []:
        venue, year = canonical_channel(prior.get("venue")), int(prior.get("year") or 0)
        prior_ids = [clean(value) for value in prior.get("identities") or []]
        kept = [ready[identity] for identity in prior_ids if identity in ready]
        needed = per_venue - len(kept)
        available = sorted(
            (paper for paper in population.get((venue, year), []) if paper_identity(paper) not in attempted),
            key=paper_identity,
        )
        if len(available) < needed:
            raise ValueError(
                f"No untried same-venue replacements remain for {venue} {year}: needed={needed}, available={len(available)}"
            )
        generator = random.Random(f"{seed}:{replacement_round}:{venue}:{year}")
        replacements = generator.sample(available, needed)
        replacement_count += len(replacements)
        current = [*kept, *replacements]
        selected.extend(current)
        strata.append({
            "venue": venue,
            "year": year,
            "population_count": len(population.get((venue, year), [])),
            "sample_count": len(current),
            "kept_ready_count": len(kept),
            "replacement_count": len(replacements),
            "identities": [paper_identity(item) for item in current],
        })
    if replacement_count == 0:
        raise ValueError("Every random venue selection already has acquired full text; no replacements are needed")
    attempted.update(paper_identity(item) for item in selected)
    target_count = int(shortlist.get("target_count") or len(selected))
    replacement_count_total = max(0, len(attempted) - target_count)
    payload = {
        **shortlist,
        "status": "complete",
        "replacement_round": replacement_round,
        "replacement_count": replacement_count,
        "replacement_count_last_round": replacement_count,
        "replacement_count_total": replacement_count_total,
        "attempted_identities": sorted(attempted),
        "attempted_identity_count": len(attempted),
        "strata": strata,
        "papers": selected,
        "actual_count": len(selected),
        "generated_at": now_iso(),
    }
    write_json(directory / "shortlist.json", payload)
    update_run(directory, stage="shortlist_ready", status="active", counts={
        "shortlist_target": len(selected),
        "shortlist_actual": len(selected),
        "random_replacement_round": replacement_round,
        "random_replacements": replacement_count_total,
        "random_replacements_last_round": replacement_count,
        "random_replacements_total": replacement_count_total,
        "random_attempted_identities_total": len(attempted),
    })
    return payload


def _fulltext_round_incident(
    result: dict[str, Any],
    shortlist: dict[str, Any],
    archive_path: Path,
    invocation_index: int,
) -> dict[str, Any]:
    attempted_identity_count = int(shortlist.get("attempted_identity_count") or len(shortlist.get("attempted_identities") or []))
    target_count = int(shortlist.get("target_count") or len(shortlist.get("papers") or []))
    replacement_count_total = int(shortlist.get("replacement_count_total") or max(0, attempted_identity_count - target_count))
    failure_steps: dict[str, int] = {}
    unavailable_by_venue: dict[str, int] = {}
    for item in result.get("items") or []:
        paper = item.get("paper") if isinstance(item, dict) and isinstance(item.get("paper"), dict) else {}
        venue = clean(paper.get("venue")) or "unknown"
        if item.get("full_text_available") is not True:
            unavailable_by_venue[venue] = unavailable_by_venue.get(venue, 0) + 1
            item_reason = clean(item.get("error_type") or item.get("status") or item.get("message"))
            if item_reason:
                key = f"item:{item_reason}"
                failure_steps[key] = failure_steps.get(key, 0) + 1
        attempt_payload = item.get("attempts")
        attempt_groups = (
            attempt_payload.values()
            if isinstance(attempt_payload, dict)
            else [attempt_payload]
            if isinstance(attempt_payload, list)
            else []
        )
        for attempts in attempt_groups:
            for attempt in attempts or []:
                if not isinstance(attempt, dict):
                    continue
                try:
                    status_code = int(attempt.get("status_code") or 0)
                except (TypeError, ValueError):
                    status_code = 0
                failed = (
                    attempt.get("accepted") is False
                    or status_code >= 400
                    or clean(attempt.get("status")).lower() in {"error", "skipped_cooldown", "skipped_persisted_cooldown"}
                    or bool(attempt.get("error_type"))
                )
                if not failed:
                    continue
                kind = clean(attempt.get("kind")) or "unknown"
                reason = clean(
                    attempt.get("reason")
                    or attempt.get("status")
                    or attempt.get("error_type")
                    or attempt.get("status_code")
                    or "rejected"
                )
                key = f"{kind}:{reason}"
                failure_steps[key] = failure_steps.get(key, 0) + 1
    shortlist_fingerprint = clean(result.get("shortlist_fingerprint"))
    return {
        "incident_id": f"fulltext:{invocation_index:04d}:{shortlist_fingerprint}",
        "kind": "fulltext_round",
        "at": result.get("completed_at") or now_iso(),
        "invocation_index": invocation_index,
        "random_replacement_round": int(shortlist.get("replacement_round") or 0),
        "random_replacement_count_last_round": int(shortlist.get("replacement_count_last_round") or shortlist.get("replacement_count") or 0),
        "random_replacement_count_total": replacement_count_total,
        "attempted_identity_count": attempted_identity_count,
        "status": result.get("status"),
        "requested_count": int(result.get("requested_count") or 0),
        "ready_count": int(result.get("full_text_ready_count") or 0),
        "unavailable_count": int(result.get("requested_count") or 0) - int(result.get("full_text_ready_count") or 0),
        "unavailable_by_venue": dict(sorted(unavailable_by_venue.items())),
        "failure_step_counts": dict(sorted(failure_steps.items())),
        "cooldown_requeue": result.get("cooldown_requeue") or {},
        "shortlist_fingerprint": shortlist_fingerprint,
        "archive_path": str(archive_path),
    }


def _archive_fulltext_round(
    directory: Path,
    result: dict[str, Any],
    shortlist: dict[str, Any],
) -> tuple[Path, int, dict[str, Any]]:
    history_dir = directory / "fulltext_rounds"
    with run_lock(directory, "fulltext-history"):
        indices = []
        for path in history_dir.glob("*.json"):
            try:
                indices.append(int(path.stem))
            except ValueError:
                continue
        invocation_index = max(indices, default=-1) + 1
        archive_path = history_dir / f"{invocation_index:04d}.json"
        attempted_identity_count = int(shortlist.get("attempted_identity_count") or len(shortlist.get("attempted_identities") or []))
        target_count = int(shortlist.get("target_count") or len(shortlist.get("papers") or []))
        replacement_count_total = int(shortlist.get("replacement_count_total") or max(0, attempted_identity_count - target_count))
        archived = {
            **result,
            "history_schema_version": 1,
            "history_invocation_index": invocation_index,
            "random_replacement_round": int(shortlist.get("replacement_round") or 0),
            "random_replacement_count_last_round": int(shortlist.get("replacement_count_last_round") or shortlist.get("replacement_count") or 0),
            "random_replacement_count_total": replacement_count_total,
            "attempted_identity_count": attempted_identity_count,
        }
        write_json(archive_path, archived)
    return archive_path, invocation_index, _fulltext_round_incident(result, shortlist, archive_path, invocation_index)


def run_fulltext(run_dir: Path, workers: int) -> dict[str, Any]:
    directory = require_run(run_dir)
    plan = _require_stage_allowed(directory, "fulltext")
    _require_plan_unchanged(directory, plan)
    shortlist = read_json(directory / "shortlist.json", {})
    papers = shortlist.get("papers") if isinstance(shortlist, dict) else None
    if not isinstance(papers, list) or not papers:
        raise ValueError("shortlist.json is missing or empty")
    metadata = read_json(directory / "metadata.json", {})
    if metadata.get("status") != "complete":
        raise ValueError("metadata coverage is incomplete; full-text acquisition requires complete formal metadata")
    if shortlist.get("metadata_fingerprint") != metadata.get("metadata_fingerprint"):
        raise ValueError("shortlist.json is stale because metadata changed; rerun scoring and shortlist")
    result = acquire_many(papers, directory, workers)
    result["metadata_fingerprint"] = metadata.get("metadata_fingerprint")
    result["shortlist_fingerprint"] = stable_hash(shortlist)
    archive_path, invocation_index, incident = _archive_fulltext_round(directory, result, shortlist)
    result["history_entry_path"] = str(archive_path)
    result["history_invocation_index"] = invocation_index
    write_json(directory / "full_text_results.json", result)
    attempted_total = int(shortlist.get("attempted_identity_count") or len(shortlist.get("attempted_identities") or papers))
    target_total = int(shortlist.get("target_count") or len(papers))
    replacement_total = int(shortlist.get("replacement_count_total") or max(0, attempted_total - target_total))
    requested = int(result.get("requested_count") or 0)
    ready = int(result.get("full_text_ready_count") or 0)
    update_run(
        directory,
        stage="fulltext_complete",
        status="active",
        counts={
            "requested": requested,
            "completed": requested,
            "ready": ready,
            "random_replacement_round": int(shortlist.get("replacement_round") or 0),
            "random_replacements": replacement_total,
            "random_replacements_last_round": int(shortlist.get("replacement_count_last_round") or shortlist.get("replacement_count") or 0),
            "random_replacements_total": replacement_total,
            "random_attempted_identities_total": attempted_total,
            "fulltext_history_entries": invocation_index + 1,
        },
        warnings=[] if ready == requested else [f"Full text unavailable for {requested - ready} papers"],
        incidents=[incident],
    )
    return result


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {line_number}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"Evidence card line {line_number} must be an object")
        rows.append(item)
    return rows


def _fast_card_valid(card: dict[str, Any], expected: dict[str, Any], batch_id: int) -> bool:
    if clean(card.get("identity")) != clean(expected.get("identity")) or int(card.get("batch_id") or 0) != batch_id:
        return False
    cited = Path(clean(card.get("full_text_path"))).expanduser()
    wanted = Path(clean(expected.get("full_text_path"))).expanduser()
    if not cited.is_file() or cited.resolve() != wanted.resolve():
        return False
    try:
        match = _number(card.get("match_score"), label="match_score", maximum=10)
        transfer = _number(card.get("transferability_score"), label="transferability_score", maximum=10)
        total = _number(card.get("final_score"), label="final_score", maximum=20)
    except ValueError:
        return False
    if abs(round(match + transfer, 4) - total) > 0.01:
        return False
    return all(card.get(key) for key in ("title", "summary", "decisive_evidence", "limitations", "borrowable_elements", "confidence"))


def _fast_reading_authorized(directory: Path, plan: dict[str, Any]) -> bool:
    workflow = plan.get("workflow") if isinstance(plan.get("workflow"), dict) else {}
    planned = (
        clean(workflow.get("reading_preference")).lower() == "codex_fast"
        and (workflow.get("user_disabled_claude") is True or workflow.get("claude_unavailable") is True)
    )
    observed = read_json(directory / "claude_read_results.json", {}).get("status") == "claude_unavailable"
    return planned or observed


def finalize(run_dir: Path, evidence_path: Path, target: int | None) -> dict[str, Any]:
    directory = require_run(run_dir)
    plan = read_json(directory / "plan.json", {})
    _require_plan_unchanged(directory, plan)
    if clean((plan.get("workflow") or {}).get("stop_after")).lower() != "recommendation":
        raise ValueError("This run was not planned for recommendation; do not execute final ranking on an intermediate workflow")
    metadata = read_json(directory / "metadata.json", {})
    shortlist = read_json(directory / "shortlist.json", {})
    read_artifacts = read_json(directory / "read_artifacts.json", {})
    fast_batches = read_json(directory / "codex_fast_batches.json", {})
    reading_mode = read_json(directory / "reading_mode.json", {}).get("mode")
    fast_mode = reading_mode == "codex_fast_three_batches" and isinstance(fast_batches, dict) and fast_batches.get("status") == "ready" and fast_batches.get("reading_mode") == "codex_fast_three_batches"
    if fast_mode and not _fast_reading_authorized(directory, plan):
        raise ValueError("Codex fast-reading artifacts lack an explicit or observed Claude-unavailable authorization")
    if not fast_mode and (not isinstance(read_artifacts, dict) or read_artifacts.get("status") != "complete" or read_artifacts.get("read_contract_version") != READ_CONTRACT_VERSION):
        raise ValueError("Default Claude mode requires one validated single-paper read.md per acquired paper")
    reads = {clean(item.get("identity")): item for item in read_artifacts.get("items") or [] if isinstance(item, dict)}
    fast_assignment = {
        clean(item.get("identity")): int(batch.get("batch_id") or 0)
        for batch in fast_batches.get("batches") or [] if isinstance(batch, dict)
        for item in batch.get("items") or [] if isinstance(item, dict)
    } if fast_mode else {}
    fulltext = read_json(directory / "full_text_results.json", {})
    fulltext_fingerprint = stable_hash(fulltext)
    if fast_mode:
        if fast_batches.get("fulltext_results_fingerprint") != fulltext_fingerprint:
            raise ValueError("Codex fast-reading batches are stale because full-text results changed")
    elif read_artifacts.get("fulltext_results_fingerprint") != fulltext_fingerprint:
        raise ValueError("Validated single-paper reads are stale because full-text results changed")
    metadata_fingerprint = metadata.get("metadata_fingerprint")
    if metadata_fingerprint and (shortlist.get("metadata_fingerprint") != metadata_fingerprint or fulltext.get("metadata_fingerprint") != metadata_fingerprint or fulltext.get("shortlist_fingerprint") != stable_hash(shortlist)):
        raise ValueError("Downstream artifacts are stale because metadata or shortlist changed; rerun from shortlist/fulltext")
    items = fulltext.get("items") if isinstance(fulltext, dict) else None
    if not isinstance(items, list):
        raise ValueError("full_text_results.json is missing")
    ready = {clean(item.get("identity")): item for item in items if isinstance(item, dict) and item.get("full_text_available") is True}
    if not ready:
        raise ValueError("No successfully acquired full texts are available for reading or recommendation")
    cards = _jsonl(evidence_path)
    validated: dict[str, dict[str, Any]] = {}
    for index, card in enumerate(cards):
        identity = clean(card.get("identity"))
        if identity not in ready:
            raise ValueError(f"evidence card {index} is not a successfully acquired paper: {identity}")
        if not fast_mode and identity not in reads:
            raise ValueError(f"evidence card {index} has no validated single-paper read.md: {identity}")
        if identity in validated:
            raise ValueError(f"Duplicate evidence card: {identity}")
        cited_path = Path(clean(card.get("full_text_path"))).expanduser()
        expected_path = Path(clean(ready[identity].get("text_path"))).expanduser()
        if not cited_path.is_file() or cited_path.resolve() != expected_path.resolve():
            raise ValueError(f"evidence card {index} must cite the exact acquired full-text path")
        if fast_mode:
            if int(card.get("batch_id") or 0) != fast_assignment.get(identity):
                raise ValueError(f"evidence card {index} has the wrong three-way Codex batch assignment")
        else:
            read_path = Path(clean(card.get("read_path"))).expanduser()
            expected_read_path = Path(clean(reads[identity].get("read_path"))).expanduser()
            if not read_path.is_file() or read_path.resolve() != expected_read_path.resolve():
                raise ValueError(f"evidence card {index} must cite the exact validated single-paper read.md")
        values = {name: _number(card.get(name), label=f"evidence[{index}].{name}", maximum=FULLTEXT_MAXIMA[name]) for name in FULLTEXT_COMPONENTS}
        total = round(sum(values.values()), 4)
        declared = _number(card.get("final_score"), label=f"evidence[{index}].final_score", maximum=20)
        if abs(total - declared) > 0.01:
            raise ValueError(f"evidence card {index} final_score must equal match_score plus transferability_score")
        required_text = ("summary", "decisive_evidence", "limitations", "borrowable_elements", "confidence")
        if any(not card.get(key) for key in required_text):
            raise ValueError(f"evidence card {index} lacks required scientific analysis fields")
        validated[identity] = {**card, **values, "final_score": total}
    missing = sorted(set(ready) - set(validated))
    if missing:
        raise ValueError(f"Codex must deep-read every acquired full text; missing evidence cards={len(missing)}")
    ordered = sorted(validated.values(), key=lambda item: (-item["final_score"], item["identity"]))
    if not ordered:
        raise ValueError("No validated evidence cards are available for final ranking")
    planned_target = (plan.get("workflow") or {}).get("final_target")
    if target is not None and planned_target is not None and int(target) != int(planned_target):
        raise ValueError("--target conflicts with workflow.final_target")
    target_count = int(target if target is not None else planned_target or 20)
    if target_count < 1:
        raise ValueError("final target must be a positive integer")
    payload = {
        "schema_version": 1,
        "run_id": directory.name,
        "target_count": target_count,
        "actual_count": min(target_count, len(ordered)),
        "shortfall": max(0, target_count - len(ordered)),
        "read_and_scored_count": len(ordered),
        "read_contract_version": READ_CONTRACT_VERSION,
        "reading_mode": "codex_fast_three_batches" if fast_mode else "external_claude_per_paper",
        "ranking": ordered,
        "recommendations": ordered[:target_count],
        "generated_at": now_iso(),
        "metadata_fingerprint": metadata.get("metadata_fingerprint"),
        "shortlist_fingerprint": stable_hash(shortlist),
        "fulltext_results_fingerprint": fulltext_fingerprint,
    }
    write_json(directory / "evidence_cards.validated.json", {"schema_version": 1, "cards": ordered})
    write_json(directory / "final_ranking.json", payload)
    update_run(directory, stage="final_ranking_ready", counts={"full_text_ready": len(ready), "deep_read_complete": len(ordered), "final_target": target_count, "final_actual": payload["actual_count"]})
    return payload


def complete(run_dir: Path, recommendations_path: Path) -> dict[str, Any]:
    directory = require_run(run_dir)
    plan = read_json(directory / "plan.json", {})
    _require_plan_unchanged(directory, plan)
    if clean((plan.get("workflow") or {}).get("stop_after")).lower() != "recommendation":
        raise ValueError("This run was not planned to finish at recommendation; create or update the proper active run plan")
    ranking = read_json(directory / "final_ranking.json", {})
    aggregate_reads = directory / "read.md"
    if ranking.get("reading_mode") != "codex_fast_three_batches" and (not aggregate_reads.is_file() or len(aggregate_reads.read_text(encoding="utf-8").strip()) < 200):
        raise ValueError("Aggregated read.md is missing or incomplete")
    if ranking.get("read_contract_version") != READ_CONTRACT_VERSION:
        raise ValueError("final_ranking.json uses a stale reading contract; regenerate all per-paper reads and ranking")
    metadata = read_json(directory / "metadata.json", {})
    shortlist = read_json(directory / "shortlist.json", {})
    fulltext = read_json(directory / "full_text_results.json", {})
    metadata_fingerprint = metadata.get("metadata_fingerprint")
    if metadata_fingerprint and (
        ranking.get("metadata_fingerprint") != metadata_fingerprint
        or ranking.get("shortlist_fingerprint") != stable_hash(shortlist)
        or ranking.get("fulltext_results_fingerprint") != stable_hash(fulltext)
    ):
        raise ValueError("final_ranking.json is stale because upstream metadata changed; rerun downstream stages")
    recommendations = ranking.get("recommendations") if isinstance(ranking, dict) else None
    if not isinstance(recommendations, list) or not recommendations:
        raise ValueError("final_ranking.json has no recommendations")
    text = recommendations_path.read_text(encoding="utf-8")
    if len(text.strip()) < 200:
        raise ValueError("recommendations.md is too short to contain useful recommendations")
    missing_titles = [clean(item.get("title") or (item.get("paper") or {}).get("title")) for item in recommendations if clean(item.get("title") or (item.get("paper") or {}).get("title")) not in text]
    if missing_titles:
        raise ValueError(f"recommendations.md does not mention {len(missing_titles)} final papers")
    target = directory / "recommendations.md"
    if recommendations_path.resolve() != target.resolve():
        write_text(target, text)
    state = update_run(directory, stage="complete", status="complete", counts={"final_actual": len(recommendations)})
    return {"status": "complete", "run_id": directory.name, "run_dir": str(directory), "recommendation_count": len(recommendations), "run_state": state}


def finish_stage(run_dir: Path, stage: str) -> dict[str, Any]:
    directory = require_run(run_dir)
    stage_name = clean(stage).lower()
    plan = read_json(directory / "plan.json", {})
    _require_plan_unchanged(directory, plan)
    planned_stop = clean((plan.get("workflow") or {}).get("stop_after")).lower()
    if planned_stop != stage_name:
        raise ValueError(f"Plan stop_after is {planned_stop or 'missing'}, not {stage_name}; do not mark an intermediate stage complete")
    if stage_name == "metadata":
        artifact = read_json(directory / "metadata.json", {})
        count = len(artifact.get("papers") or [])
        valid = artifact.get("status") == "complete" and count > 0
    elif stage_name == "shortlist":
        artifact = read_json(directory / "shortlist.json", {})
        metadata = read_json(directory / "metadata.json", {})
        count = len(artifact.get("papers") or [])
        valid = metadata.get("status") == "complete" and count > 0 and artifact.get("metadata_fingerprint") == metadata.get("metadata_fingerprint")
    elif stage_name == "fulltext":
        artifact = read_json(directory / "full_text_results.json", {})
        metadata = read_json(directory / "metadata.json", {})
        shortlist = read_json(directory / "shortlist.json", {})
        count = sum(item.get("full_text_available") is True for item in artifact.get("items") or [] if isinstance(item, dict))
        valid = (
            artifact.get("status") in {"complete", "complete_with_gaps"}
            and count > 0
            and artifact.get("metadata_fingerprint") == metadata.get("metadata_fingerprint")
            and artifact.get("shortlist_fingerprint") == stable_hash(shortlist)
        )
    elif stage_name == "reading":
        artifact = read_json(directory / "read_artifacts.json", {})
        if read_json(directory / "reading_mode.json", {}).get("mode") == "codex_fast_three_batches":
            fast_payload = read_json(directory / "codex_fast_batches.json", {})
            batches = fast_payload.get("batches") or []
            results = [read_json(Path(clean(batch.get("result_path"))), None) for batch in batches if isinstance(batch, dict)]
            count = sum(len(result) for result in results if isinstance(result, list))
            expected_items = {
                clean(item.get("identity")): (int(batch.get("batch_id") or 0), item)
                for batch in batches if isinstance(batch, dict)
                for item in batch.get("items") or [] if isinstance(item, dict)
            }
            flat = [item for result in results if isinstance(result, list) for item in result if isinstance(item, dict)]
            actual_ids = [clean(item.get("identity")) for item in flat]
            fulltext = read_json(directory / "full_text_results.json", {})
            cards_valid = all(
                identity in expected_items and _fast_card_valid(card, expected_items[identity][1], expected_items[identity][0])
                for card in flat for identity in [clean(card.get("identity"))]
            )
            valid = (
                _fast_reading_authorized(directory, plan)
                and fast_payload.get("authorization") in {"user_disabled_claude", "plan_claude_unavailable", "observed_claude_unavailable"}
                and bool(expected_items)
                and count == len(expected_items)
                and len(actual_ids) == len(set(actual_ids))
                and len(results) == 3
                and all(isinstance(result, list) for result in results)
                and set(actual_ids) == set(expected_items)
                and cards_valid
                and fast_payload.get("fulltext_results_fingerprint") == stable_hash(fulltext)
            )
        else:
            count = int(artifact.get("validated_count") or 0)
            fulltext = read_json(directory / "full_text_results.json", {})
            valid = (
                read_json(directory / "reading_mode.json", {}).get("mode") == "external_claude_per_paper"
                and clean((plan.get("workflow") or {}).get("reading_preference")).lower() != "codex_fast"
                and artifact.get("status") == "complete"
                and count > 0
                and artifact.get("fulltext_results_fingerprint") == stable_hash(fulltext)
            )
    else:
        raise ValueError("finish-stage supports metadata, shortlist, fulltext, or reading")
    if not valid:
        raise ValueError(f"Cannot finish at {stage_name}; its required artifact is missing or incomplete")
    state = update_run(directory, stage=f"{stage_name}_complete", status="complete", counts={f"{stage_name}_items": count})
    return {"status": "complete", "run_id": directory.name, "run_dir": str(directory), "completed_stage": stage_name, "item_count": count, "run_state": state}
