from __future__ import annotations

import re
from typing import Any


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def values_blob(paper: dict[str, Any]) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    identifiers = paper.get("identifiers") if isinstance(paper.get("identifiers"), dict) else {}
    return " ".join(clean(v) for v in (
        paper.get("url"), paper.get("pdf_url"), metadata.get("url"),
        metadata.get("pdf_url"), identifiers.get("doi"), metadata.get("doi"),
    ))


def explicit_pdf(paper: dict[str, Any], kind: str, source: str) -> list[dict[str, str]]:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    urls = [clean(paper.get("pdf_url")), clean(metadata.get("pdf_url"))]
    return [
        {"url": url, "kind": kind, "official_source": source}
        for url in dict.fromkeys(urls)
        if url.startswith("http")
    ]


def match_urls(pattern: str, blob: str, build) -> list[str]:
    return [build(*match.groups()) for match in re.finditer(pattern, blob, re.I)]
