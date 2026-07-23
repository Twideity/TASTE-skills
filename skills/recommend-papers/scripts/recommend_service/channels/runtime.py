from __future__ import annotations

import hashlib
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..http import get
from ..storage import DATA_ROOT, read_json, write_json


_ABSTRACT_UI_CONTROL_RE = re.compile(
    r"(?:\s*(?:show\s+(?:more|less)|read\s+(?:more|less)|"
    r"\u663e\u793a\u66f4\u591a|\u663e\u793a\u8f83\u5c11|\u5c55\u5f00|\u6536\u8d77)\s*[\u3002. ]?\s*)+$",
    re.IGNORECASE,
)
_ABSTRACT_PLACEHOLDER_RE = re.compile(
    r"^(?:abstract\s*)?(?:not\s+available|unavailable|none|n/?a|tbd|"
    r"to\s+be\s+(?:added|announced|determined)|coming\s+soon)\.?$",
    re.IGNORECASE,
)


class AuthoritativeEmptyCatalog(RuntimeError):
    """The official production route was exhausted and exposed no records."""


class IncompleteCatalogError(RuntimeError):
    """The official route returned a corpus that cannot be published safely."""


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def clean_abstract(value: Any) -> str:
    return _ABSTRACT_UI_CONTROL_RE.sub("", clean(value)).strip()


def abstract_is_real(value: Any) -> bool:
    text = clean_abstract(value)
    if len(text) < 50 or len(re.findall(r"\b[\w'-]+\b", text, re.UNICODE)) < 8:
        return False
    lower = text.lower()
    if _ABSTRACT_PLACEHOLDER_RE.fullmatch(text):
        return False
    if lower.startswith("correct abstract if needed. retain xml formatting tags"):
        return False
    if re.search(r"\.\s+(?:proceedings|findings)\s+of\s+the\b.*\b(?:19|20)\d{2}\.?$", text, re.I):
        return False
    return True


def response(url: str, *, timeout: int = 60):
    result = get(url, timeout=timeout)
    result.raise_for_status()
    return result


def catalog_response(url: str, *, label: str, year: int, timeout: int = 60):
    result = get(url, timeout=timeout)
    if result.status_code == 404:
        raise AuthoritativeEmptyCatalog(f"{label} has no published catalog for {year}")
    result.raise_for_status()
    return result


def looks_like_title(value: Any) -> bool:
    text = clean(value)
    return len(text) >= 8 and len(text.split()) >= 2 and text.lower() not in {
        "view paper", "view details", "paper details", "download pdf", "abstract", "title tbd",
    }


def worker_count(spec: dict[str, Any], fallback: int = 1) -> int:
    try:
        return max(1, int(spec.get("_channel_metadata_workers") or fallback))
    except (TypeError, ValueError):
        return max(1, fallback)


def _detail_stage_dir(spec: dict[str, Any], adapter: str) -> Path:
    channel = re.sub(
        r"[^a-z0-9._-]+",
        "_",
        clean(spec.get("venue_id") or spec.get("venue") or "venue").lower(),
    )
    year = str(int((spec.get("years") or [0])[0]))
    adapter_key = re.sub(r"[^a-z0-9._-]+", "_", clean(adapter).lower())
    return DATA_ROOT / "state" / "venue-staging" / channel / year / adapter_key


def checkpointed_details(
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    adapter: str,
    enrich,
    workers: int,
) -> dict[str, int]:
    """Resume deterministic per-paper detail enrichment outside the cache."""
    stage_dir = _detail_stage_dir(spec, adapter)
    pending: list[tuple[dict[str, Any], Path, str]] = []
    restored = 0
    for row in rows:
        title_key = re.sub(r"[^a-z0-9]+", " ", clean(row.get("title")).lower()).strip()
        locator = clean(row.get("url") or row.get("pdf_url"))
        checkpoint_key = f"{title_key}|{locator}"
        digest = hashlib.sha256(checkpoint_key.encode("utf-8")).hexdigest()
        path = stage_dir / f"{digest}.json"
        payload = read_json(path, {})
        saved = payload.get("paper") if isinstance(payload, dict) else None
        if (
            isinstance(saved, dict)
            and payload.get("schema_version") == 1
            and clean(payload.get("checkpoint_key")) == checkpoint_key
            and abstract_is_real(saved.get("abstract"))
        ):
            row.update(saved)
            row.setdefault("metadata", {})["detail_checkpoint_reused"] = True
            restored += 1
        else:
            pending.append((row, path, checkpoint_key))

    completed = 0
    counter_lock = threading.Lock()

    def one(item: tuple[dict[str, Any], Path, str]) -> None:
        nonlocal completed
        row, path, checkpoint_key = item
        try:
            enrich(row)
            if abstract_is_real(row.get("abstract")):
                write_json(path, {
                    "schema_version": 1,
                    "checkpoint_key": checkpoint_key,
                    "paper": row,
                })
        except Exception as exc:
            row.setdefault("metadata", {})["detail_error"] = (
                f"{type(exc).__name__}: {str(exc)[:300]}"
            )
        with counter_lock:
            completed += 1

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(pending)))) as pool:
            futures = [pool.submit(one, item) for item in pending]
            for future in as_completed(futures):
                future.result()
    return {"restored": restored, "attempted": len(pending), "completed": completed}


def finish(
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    adapter: str,
    requests: list[dict[str, Any]],
    proof: str,
    discovered_count: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    discovered = len(rows) if discovered_count is None else int(discovered_count)
    minimum = int(spec.get("minimum_catalog_records") or 0)
    if not rows:
        if discovered:
            raise IncompleteCatalogError(
                f"{adapter} discovered {discovered} records but produced no usable metadata"
            )
        raise AuthoritativeEmptyCatalog(
            f"{adapter} official catalog exhausted with zero records for "
            f"{clean(spec.get('venue_id') or spec.get('venue'))} "
            f"{(spec.get('years') or ['unknown'])[0]}"
        )
    invalid = [
        clean(row.get("title"))
        for row in rows
        if not abstract_is_real(row.get("abstract"))
    ]
    if invalid or discovered != len(rows) or (minimum and len(rows) < minimum):
        raise IncompleteCatalogError(
            f"{adapter} full-metadata crawl incomplete: records={len(rows)}, "
            f"discovered={discovered}, minimum={minimum or 'unset'}, "
            f"invalid_abstracts={len(invalid)}, examples={invalid[:5]}"
        )
    result = {
        "status": "complete", "complete_catalog": True, "exhausted": True,
        "truncated": False, "exhaustion_proof": proof, "adapter": adapter,
        "count": len(rows), "discovered_count": discovered,
        "minimum_catalog_records": minimum or None, "requests": requests,
    }
    stage_dir = _detail_stage_dir(spec, adapter)
    if stage_dir.is_dir():
        shutil.rmtree(stage_dir)
    return rows, result
