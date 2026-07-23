from __future__ import annotations

import re
from typing import Any

import fitz

from ..http import get, receipt

fitz.TOOLS.mupdf_display_errors(False)
fitz.TOOLS.mupdf_display_warnings(False)


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


def official_pdf_abstract(row: dict[str, Any], *, source: str) -> None:
    """Fill a missing abstract from the paper's official PDF."""
    url = clean(row.get("pdf_url"))
    if not url:
        return
    response = get(url, timeout=60)
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        return
    document = fitz.open(stream=response.content, filetype="pdf")
    try:
        text = "\n".join(page.get_text("text") for page in list(document)[:2])
    finally:
        document.close()
    matches = re.finditer(
        r"(?is)\bAbstract\b\s*(.*?)(?:\n\s*(?:1\s+Introduction|Introduction|Keywords|Index Terms|References|Acknowledg(?:e)?ments?|1\s+[A-Z][^\n]{2,80})\b)",
        text,
    )
    for match in matches:
        abstract = re.sub(r"([A-Za-z])- ([a-z])", r"\1\2", clean(match.group(1)))
        abstract = re.sub(r"\s+([,.;:])", r"\1", abstract).strip(" .,;:")
        if len(abstract) < 80:
            continue
        row["abstract"] = abstract
        row.setdefault("metadata", {})["abstract_source"] = source
        row["metadata"]["abstract_pdf_receipt"] = receipt(response)
        return


def acl_pdf_abstract(row: dict[str, Any]) -> None:
    """Fill a missing ACL-family abstract from its official PDF, as TASTE does."""
    official_pdf_abstract(row, source="official_acl_pdf")
