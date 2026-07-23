from __future__ import annotations

from typing import Any

from ..http import get


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def response(url: str, *, timeout: int = 60):
    result = get(url, timeout=timeout)
    result.raise_for_status()
    return result


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
