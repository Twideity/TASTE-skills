from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .fulltext import acquire_many
from .metadata import clean, deduplicate, fetch_source, migrate_metadata_caches, paper_identity, validate_plan
from .reading_artifacts import READ_CONTRACT_VERSION
from .storage import ensure_run, now_iso, read_json, run_lock, stable_hash, update_run, write_json, write_text


METADATA_COMPONENTS = ("topic_fit", "transferability_potential", "abstract_specificity")
METADATA_MAXIMA = {"topic_fit": 50, "transferability_potential": 30, "abstract_specificity": 20}
FULLTEXT_COMPONENTS = ("match_score", "transferability_score")
FULLTEXT_MAXIMA = {"match_score": 10, "transferability_score": 10}


def _number(value: Any, *, label: str, minimum: float = 0, maximum: float = 100) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return result


def run_metadata(plan_path: Path, run_dir: Path | None = None) -> dict[str, Any]:
    directory = ensure_run(run_dir)
    with run_lock(directory, "metadata"):
        return _run_metadata_locked(plan_path, directory)


def _run_metadata_locked(plan_path: Path, directory: Path) -> dict[str, Any]:
    migrate_metadata_caches()
    plan = validate_plan(read_json(plan_path, {}))
    write_json(directory / "plan.json", plan)
    policy = clean(plan.get("cache_policy") or "reuse").lower()
    if policy not in {"reuse", "refresh", "only"}:
        raise ValueError("cache_policy must be reuse, refresh, or only")
    max_age_days = _number(plan.get("metadata_cache_max_age_days", 7), label="metadata_cache_max_age_days", maximum=3650)
    all_rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    warnings: list[str] = []
    sources = plan["sources"]
    update_run(directory, stage="metadata_crawl", counts={"sources": len(sources), "completed_sources": 0, "raw_papers": 0})
    receipt_dir = directory / "source_receipts"
    for index, spec in enumerate(sources, 1):
        try:
            papers, source_receipt = fetch_source(spec, policy=policy, max_age_days=max_age_days)
            all_rows.extend(papers)
            details = source_receipt.get("details") if isinstance(source_receipt, dict) else None
            coverage_status = details.get("status") if isinstance(details, dict) else "complete"
            row = {"index": index, "source": spec, "status": coverage_status, "paper_count": len(papers), "cache": source_receipt}
            if coverage_status != "complete":
                warnings.append(f"Source {index} coverage is {coverage_status}; inspect receipt and repair before scoring")
        except Exception as exc:
            row = {"index": index, "source": spec, "status": "error", "paper_count": 0, "error_type": type(exc).__name__, "message": str(exc)[:1000]}
            warnings.append(f"Source {index} failed: {type(exc).__name__}: {str(exc)[:200]}")
        receipts.append(row)
        write_json(receipt_dir / f"{index:03d}.json", row)
        update_run(directory, stage="metadata_crawl", counts={"sources": len(sources), "completed_sources": index, "raw_papers": len(all_rows)}, warnings=warnings)
    papers = deduplicate(all_rows)
    status = "complete" if papers and not warnings else "complete_with_gaps" if papers else "blocked"
    payload = {
        "schema_version": 1,
        "run_id": directory.name,
        "run_dir": str(directory),
        "status": status,
        "generated_at": now_iso(),
        "raw_count": len(all_rows),
        "deduplicated_count": len(papers),
        "metadata_fingerprint": stable_hash({"plan": plan, "papers": papers}),
        "papers": papers,
        "source_receipts": receipts,
        "warnings": warnings,
    }
    write_json(directory / "metadata.json", payload)
    update_run(directory, stage="metadata_ready" if papers else "metadata_blocked", status="active" if papers else "blocked", counts={"sources": len(sources), "completed_sources": len(sources), "raw_papers": len(all_rows), "metadata_papers": len(papers)}, warnings=warnings)
    return payload


def _score_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("scores", "papers", "items"):
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
    raise ValueError("Score artifact must be a list or contain scores/papers/items")


def build_shortlist(run_dir: Path, scores_path: Path, target: int) -> dict[str, Any]:
    directory = ensure_run(run_dir)
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
    target_count = max(1, int(target or 100))
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


def run_fulltext(run_dir: Path, workers: int) -> dict[str, Any]:
    directory = ensure_run(run_dir)
    shortlist = read_json(directory / "shortlist.json", {})
    papers = shortlist.get("papers") if isinstance(shortlist, dict) else None
    if not isinstance(papers, list) or not papers:
        raise ValueError("shortlist.json is missing or empty")
    metadata = read_json(directory / "metadata.json", {})
    if shortlist.get("metadata_fingerprint") != metadata.get("metadata_fingerprint"):
        raise ValueError("shortlist.json is stale because metadata changed; rerun scoring and shortlist")
    result = acquire_many(papers, directory, workers)
    result["metadata_fingerprint"] = metadata.get("metadata_fingerprint")
    result["shortlist_fingerprint"] = stable_hash(shortlist)
    write_json(directory / "full_text_results.json", result)
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


def finalize(run_dir: Path, evidence_path: Path, target: int) -> dict[str, Any]:
    directory = ensure_run(run_dir)
    metadata = read_json(directory / "metadata.json", {})
    shortlist = read_json(directory / "shortlist.json", {})
    read_artifacts = read_json(directory / "read_artifacts.json", {})
    if not isinstance(read_artifacts, dict) or read_artifacts.get("status") != "complete" or read_artifacts.get("read_contract_version") != READ_CONTRACT_VERSION:
        raise ValueError("Every acquired paper must have a validated single-paper read.md before finalization")
    reads = {clean(item.get("identity")): item for item in read_artifacts.get("items") or [] if isinstance(item, dict)}
    fulltext = read_json(directory / "full_text_results.json", {})
    metadata_fingerprint = metadata.get("metadata_fingerprint")
    if metadata_fingerprint and (shortlist.get("metadata_fingerprint") != metadata_fingerprint or fulltext.get("metadata_fingerprint") != metadata_fingerprint or fulltext.get("shortlist_fingerprint") != stable_hash(shortlist)):
        raise ValueError("Downstream artifacts are stale because metadata or shortlist changed; rerun from shortlist/fulltext")
    items = fulltext.get("items") if isinstance(fulltext, dict) else None
    if not isinstance(items, list):
        raise ValueError("full_text_results.json is missing")
    ready = {clean(item.get("identity")): item for item in items if isinstance(item, dict) and item.get("full_text_available") is True}
    cards = _jsonl(evidence_path)
    validated: dict[str, dict[str, Any]] = {}
    for index, card in enumerate(cards):
        identity = clean(card.get("identity"))
        if identity not in ready:
            raise ValueError(f"evidence card {index} is not a successfully acquired paper: {identity}")
        if identity not in reads:
            raise ValueError(f"evidence card {index} has no validated single-paper read.md: {identity}")
        if identity in validated:
            raise ValueError(f"Duplicate evidence card: {identity}")
        cited_path = Path(clean(card.get("full_text_path"))).expanduser()
        expected_path = Path(clean(ready[identity].get("text_path"))).expanduser()
        if not cited_path.is_file() or cited_path.resolve() != expected_path.resolve():
            raise ValueError(f"evidence card {index} must cite the exact acquired full-text path")
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
    target_count = max(1, int(target or 20))
    payload = {
        "schema_version": 1,
        "run_id": directory.name,
        "target_count": target_count,
        "actual_count": min(target_count, len(ordered)),
        "shortfall": max(0, target_count - len(ordered)),
        "read_and_scored_count": len(ordered),
        "read_contract_version": READ_CONTRACT_VERSION,
        "ranking": ordered,
        "recommendations": ordered[:target_count],
        "generated_at": now_iso(),
        "metadata_fingerprint": metadata.get("metadata_fingerprint"),
        "shortlist_fingerprint": stable_hash(shortlist),
    }
    write_json(directory / "evidence_cards.validated.json", {"schema_version": 1, "cards": ordered})
    write_json(directory / "final_ranking.json", payload)
    update_run(directory, stage="final_ranking_ready", counts={"full_text_ready": len(ready), "deep_read_complete": len(ordered), "final_target": target_count, "final_actual": payload["actual_count"]})
    return payload


def complete(run_dir: Path, recommendations_path: Path) -> dict[str, Any]:
    directory = ensure_run(run_dir)
    aggregate_reads = directory / "read.md"
    if not aggregate_reads.is_file() or len(aggregate_reads.read_text(encoding="utf-8").strip()) < 200:
        raise ValueError("Aggregated read.md is missing or incomplete")
    ranking = read_json(directory / "final_ranking.json", {})
    if ranking.get("read_contract_version") != READ_CONTRACT_VERSION:
        raise ValueError("final_ranking.json uses a stale reading contract; regenerate all per-paper reads and ranking")
    metadata = read_json(directory / "metadata.json", {})
    shortlist = read_json(directory / "shortlist.json", {})
    metadata_fingerprint = metadata.get("metadata_fingerprint")
    if metadata_fingerprint and (ranking.get("metadata_fingerprint") != metadata_fingerprint or ranking.get("shortlist_fingerprint") != stable_hash(shortlist)):
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
