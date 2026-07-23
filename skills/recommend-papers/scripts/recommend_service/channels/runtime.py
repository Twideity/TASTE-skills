from __future__ import annotations

import re
from typing import Any

from ..http import get


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def response(url: str, *, timeout: int = 60):
    result = get(url, timeout=timeout)
    result.raise_for_status()
    return result


def probe_limit(spec: dict[str, Any]) -> int:
    try:
        return max(0, int(spec.get("_probe_limit") or 0))
    except (TypeError, ValueError):
        return 0


def looks_like_title(value: Any) -> bool:
    text = clean(value)
    return len(text) >= 8 and len(text.split()) >= 2 and text.lower() not in {
        "view paper", "view details", "paper details", "download pdf", "abstract", "title tbd",
    }


def finish(
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    adapter: str,
    requests: list[dict[str, Any]],
    proof: str,
    discovered_count: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limit = probe_limit(spec)
    if limit:
        samples = rows[:limit]
        missing = sum(not clean(row.get("abstract")) for row in samples)
        return samples, {
            "status": "sample_complete" if samples and not missing else ("sample_partial" if samples else "empty"),
            "probe_only": True,
            "complete_catalog": False,
            "exhausted": False,
            "truncated": bool(samples),
            "exhaustion_proof": "probe_sample_only_not_a_complete_catalog",
            "adapter": adapter,
            "count": len(samples),
            "sample_limit": limit,
            "discovered_title_count": len(rows) if discovered_count is None else discovered_count,
            "missing_sample_abstracts": missing,
            "metadata_sample_complete": bool(samples) and missing == 0,
            "requests": requests,
        }
    missing = [clean(row.get("title")) for row in rows if not clean(row.get("abstract"))]
    if not rows or missing:
        raise RuntimeError(
            f"{adapter} full-metadata crawl incomplete: records={len(rows)}, "
            f"missing_abstracts={len(missing)}, examples={missing[:5]}"
        )
    return rows, {
        "status": "complete", "complete_catalog": True, "exhausted": True,
        "truncated": False, "exhaustion_proof": proof, "adapter": adapter,
        "count": len(rows), "requests": requests,
    }
