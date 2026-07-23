from __future__ import annotations

"""TASTE-derived ACM-family title-pool and exact-identity enrichment engine.

Only KDD, SIGIR, CIKM and WWW may import this module.  Other conference channels
own their complete metadata logic in their own module.
"""

import hashlib
import html
import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
import fitz
from filelock import FileLock

from ..http import bounded_request_policy, cooldown_remaining, get, post, receipt
from ..storage import DATA_ROOT, METADATA_CACHE_ROOT, read_json, write_json

# Some official conference-program PDFs contain thousands of interactive
# Screen annotations.  MuPDF can still extract their text correctly, but emits
# one native stderr diagnostic per annotation.  Keep those non-actionable
# diagnostics out of normal skill output; Python exceptions and our explicit
# PDF/content validation remain active.
fitz.TOOLS.mupdf_display_errors(False)
fitz.TOOLS.mupdf_display_warnings(False)


def clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def title_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean(value).lower())


def _openalex_params(params: dict[str, Any] | None = None) -> dict[str, Any]:
    values = dict(params or {})
    api_key = clean(os.environ.get("OPENALEX_API_KEY"))
    if api_key:
        values["api_key"] = api_key
    return values


def _semantic_scholar_headers() -> dict[str, str]:
    api_key = clean(os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY"))
    return {"x-api-key": api_key} if api_key else {}


def _openaire_headers() -> dict[str, str]:
    token = clean(os.environ.get("OPENAIRE_ACCESS_TOKEN"))
    return {"Authorization": f"Bearer {token}"} if token else {}


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def looks_like_title(value: str) -> bool:
    text = clean(value)
    return len(text) >= 8 and len(text.split()) >= 2 and text.lower() not in {
        "view paper", "view details", "paper details", "download pdf", "abstract", "title tbd"
    }


def _response(url: str, *, timeout: int = 60):
    response = get(url, timeout=timeout, headers={
        "User-Agent": "Mozilla/5.0 (compatible; TASTE-Recommend-Papers/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    response.raise_for_status()
    return response


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        node = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if node and clean(node.get("content")):
            return clean(node.get("content"))
    return ""


def _abstract_from_soup(soup: BeautifulSoup) -> str:
    # Generic description/og:description fields are commonly bibliographic
    # snippets (author + venue + year), not abstracts.  Likewise, wildcard
    # abstract selectors can match hidden correction-form help.  Only accept
    # fields whose semantics or exact DOM role identify real abstract text.
    value = _meta(soup, "citation_abstract")
    if value and len(value) >= 80:
        return re.sub(r"^abstract\s*[:—-]?\s*", "", value, flags=re.I)
    selectors = (
        "#abstract", ".abstract", ".abstractInFull", ".paper-abstract", "section.abstract", "div.abstract",
        "div[itemprop='description']",
    )
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = clean(node.get_text(" ", strip=True))
            text = re.sub(r"^abstract\s*[:—-]?\s*", "", text, flags=re.I)
            if len(text) >= 80:
                return text
    return ""


def _authors_from_soup(soup: BeautifulSoup) -> list[str]:
    values = [clean(node.get("content")) for node in soup.find_all("meta", attrs={"name": "citation_author"})]
    if values:
        return [value for value in values if value]
    value = _meta(soup, "dc.creator", "author")
    parsed = [clean(part) for part in re.split(r"\s*(?:;|,\s+and\s+)\s*", value) if clean(part)]
    if parsed:
        return parsed
    # ICML/ECCV virtual pages publish authoritative authors in schema.org
    # CreativeWork JSON-LD rather than citation meta tags.
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(node.string or node.get_text() or "null")
        except (TypeError, ValueError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            authors = item.get("author") or item.get("creator") or []
            authors = authors if isinstance(authors, list) else [authors]
            names = [clean(author.get("name") if isinstance(author, dict) else author) for author in authors]
            names = [name for name in names if name]
            if names:
                return names
    organizers = soup.select_one(".event-organizers")
    if organizers:
        return [clean(part) for part in re.split(r"\s*[⋅·;]\s*", organizers.get_text(" ", strip=True)) if clean(part)]
    return []


def _pdf_from_soup(soup: BeautifulSoup, base: str) -> str:
    value = _meta(soup, "citation_pdf_url")
    if value:
        return urljoin(base, value)
    for node in soup.find_all("a", href=True):
        href = str(node.get("href") or "")
        label = clean(node.get_text(" ", strip=True)).lower()
        if re.search(r"\.pdf(?:$|[?#])", href, re.I) or label in {"pdf", "paper", "download pdf"}:
            return urljoin(base, href)
    return ""


def _doi_from_soup(soup: BeautifulSoup) -> str:
    value = _meta(soup, "citation_doi", "dc.identifier")
    match = re.search(r"10\.\d{4,9}/[^\s<>]+", value, re.I)
    return match.group(0).rstrip(".,)") if match else ""


def _official_pdf_abstract(row: dict[str, Any]) -> str:
    pdf_url = clean(row.get("pdf_url"))
    if not pdf_url:
        return ""
    try:
        response = get(pdf_url, timeout=90, headers={"User-Agent": "Mozilla/5.0 (compatible; TASTE-Recommend-Papers/1.0)"})
        row.setdefault("metadata", {})["pdf_abstract_receipt"] = receipt(response)
        if not response.ok or not response.content.startswith(b"%PDF"):
            return ""
        document = fitz.open(stream=response.content, filetype="pdf")
        try:
            text = "\n".join(page.get_text("text") for page in list(document)[:3])
        finally:
            document.close()
        match = re.search(
            r"\bAbstract\b\s*[:—-]?\s*(.{80,8000}?)(?=\n\s*(?:1\.?\s+Introduction|I\.?\s+Introduction|Introduction|Keywords?|Index Terms|CCS Concepts)\b)",
            text, re.I | re.S,
        )
        return clean(match.group(1)) if match else ""
    except Exception as exc:
        row.setdefault("metadata", {})["pdf_abstract_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return ""


def _enrich_detail(row: dict[str, Any]) -> dict[str, Any]:
    url = clean(row.get("url"))
    if not url:
        return row
    try:
        response = _response(url)
        soup = BeautifulSoup(response.text, "html.parser")
        row["abstract"] = clean(row.get("abstract")) or _abstract_from_soup(soup)
        row["authors"] = row.get("authors") or _authors_from_soup(soup)
        row["pdf_url"] = clean(row.get("pdf_url")) or _pdf_from_soup(soup, url)
        identifiers = row.setdefault("identifiers", {})
        identifiers["doi"] = clean(identifiers.get("doi")) or _doi_from_soup(soup)
        row.setdefault("metadata", {})["detail_receipt"] = receipt(response)
    except Exception as exc:
        row.setdefault("metadata", {})["detail_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    if not clean(row.get("abstract")) and clean(row.get("pdf_url")):
        row["abstract"] = _official_pdf_abstract(row)
    return row


def _enrich_all(rows: list[dict[str, Any]], worker_count: int = 8) -> list[dict[str, Any]]:
    pending = [row for row in rows if not clean(row.get("abstract"))]
    if not pending:
        return rows
    if worker_count <= 1:
        for row in pending:
            _enrich_detail(row)
        return rows
    with ThreadPoolExecutor(max_workers=max(1, min(worker_count, len(pending)))) as pool:
        futures = [pool.submit(_enrich_detail, row) for row in pending]
        for future in as_completed(futures):
            future.result()
    return rows


def _result(rows: list[dict[str, Any]], *, adapter: str, requests: list[dict[str, Any]], proof: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    missing = [row.get("title") for row in rows if not clean(row.get("abstract"))]
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


def _detail_workers(spec: dict[str, Any], formal_workers: int = 8) -> int:
    return formal_workers


def _selected_rows(spec: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rows


def _finish_rows(
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    adapter: str,
    requests: list[dict[str, Any]],
    proof: str,
    discovered_count: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return _result(rows, adapter=adapter, requests=requests, proof=proof)


def _openalex_abstract(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, offsets in index.items():
        if isinstance(offsets, list):
            positions.extend((int(offset), str(word)) for offset in offsets if isinstance(offset, int))
    return clean(" ".join(word for _, word in sorted(positions)))


def _try_acm_pdf_abstract(row: dict[str, Any]) -> bool:
    identifiers = row.setdefault("identifiers", {})
    doi = clean(identifiers.get("doi"))
    attempts = row.setdefault("metadata", {}).setdefault("indexed_enrichment", [])
    if not doi or cooldown_remaining("acm") > 0:
        return False
    try:
        response = get(f"https://dl.acm.org/doi/pdf/{doi}", timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        attempts.append({"source": "acm_dl_pdf", **receipt(response)})
        if response.ok and response.content.startswith(b"%PDF"):
            document = fitz.open(stream=response.content, filetype="pdf")
            try:
                text = "\n".join(page.get_text("text") for page in list(document)[:3])
            finally:
                document.close()
            match = re.search(r"\bAbstract\b\s*[:—-]?\s*(.{80,6000}?)(?=\n\s*(?:1\.?\s+Introduction|Keywords|CCS Concepts|ACM Reference Format)\b)", text, re.I | re.S)
            if match:
                row["abstract"] = clean(match.group(1))
                row["pdf_url"] = response.url
                return True
    except Exception as exc:
        attempts.append({"source": "acm_dl_pdf", "error": str(exc)[:300]})
    return False


def _indexed_enrich_once(row: dict[str, Any], *, allow_remote_arxiv: bool = True) -> dict[str, Any]:
    identifiers = row.setdefault("identifiers", {})
    doi = clean(identifiers.get("doi"))
    attempts = row.setdefault("metadata", {}).setdefault("indexed_enrichment", [])
    if not clean(row.get("abstract")) and clean(row.get("pdf_url")):
        abstract = _official_pdf_abstract(row)
        attempts.append({"source": "indexed_oa_pdf_for_acm", "status": "abstract_extracted" if abstract else "no_abstract_extracted"})
        if abstract:
            row["abstract"] = abstract
    if doi and not clean(row.get("abstract")) and cooldown_remaining("openalex") <= 0:
        try:
            response = get(f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='')}", params=_openalex_params(), timeout=45)
            attempts.append({"source": "openalex_doi_for_acm", **receipt(response)})
            if response.ok:
                payload = response.json()
                row["abstract"] = _openalex_abstract(payload.get("abstract_inverted_index"))
                best = payload.get("best_oa_location") if isinstance(payload.get("best_oa_location"), dict) else {}
                row["pdf_url"] = clean(row.get("pdf_url")) or clean(best.get("pdf_url"))
        except Exception as exc:
            attempts.append({"source": "openalex_doi_for_acm", "error": str(exc)[:300]})
    if not clean(row.get("abstract")) and cooldown_remaining("openalex") <= 0:
        try:
            response = get(
                "https://api.openalex.org/works",
                params=_openalex_params({"search": clean(row.get("title")), "per-page": 5}),
                timeout=45,
            )
            attempts.append({"source": "openalex_title_for_acm", **receipt(response)})
            if response.ok:
                for work in (response.json().get("results") or []):
                    if title_key(work.get("display_name")) == title_key(row.get("title")):
                        row["abstract"] = _openalex_abstract(work.get("abstract_inverted_index"))
                        best = work.get("best_oa_location") if isinstance(work.get("best_oa_location"), dict) else {}
                        row["pdf_url"] = clean(row.get("pdf_url")) or clean(best.get("pdf_url"))
                        break
        except Exception as exc:
            attempts.append({"source": "openalex_title_for_acm", "error": str(exc)[:300]})
    if not clean(row.get("abstract")) and cooldown_remaining("semantic_scholar") <= 0:
        try:
            key = f"DOI:{doi}" if doi else f"CorpusID:{quote(clean(row.get('title')), safe='')}"
            if doi:
                response = get(
                    f"https://api.semanticscholar.org/graph/v1/paper/{quote(key, safe=':')}",
                    params={"fields": "title,abstract,openAccessPdf"},
                    headers=_semantic_scholar_headers(),
                    timeout=45,
                )
                attempts.append({"source": "semantic_scholar_doi_for_acm", **receipt(response)})
                if response.ok:
                    payload = response.json()
                    row["abstract"] = clean(payload.get("abstract"))
                    oa = payload.get("openAccessPdf") if isinstance(payload.get("openAccessPdf"), dict) else {}
                    row["pdf_url"] = clean(row.get("pdf_url")) or clean(oa.get("url"))
        except Exception as exc:
            attempts.append({"source": "semantic_scholar_doi_for_acm", "error": str(exc)[:300]})
    if not clean(row.get("abstract")) and cooldown_remaining("semantic_scholar") <= 0:
        try:
            response = get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": clean(row.get("title")), "limit": 5, "fields": "title,abstract,openAccessPdf,externalIds"},
                headers=_semantic_scholar_headers(),
                timeout=45,
            )
            attempts.append({"source": "semantic_scholar_title_for_acm", **receipt(response)})
            if response.ok:
                for item in response.json().get("data") or []:
                    if title_key(item.get("title")) == title_key(row.get("title")):
                        row["abstract"] = clean(item.get("abstract"))
                        oa = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
                        row["pdf_url"] = clean(row.get("pdf_url")) or clean(oa.get("url"))
                        break
        except Exception as exc:
            attempts.append({"source": "semantic_scholar_title_for_acm", "error": str(exc)[:300]})
    if not clean(row.get("abstract")) and cooldown_remaining("openreview") <= 0:
        try:
            response = get(
                "https://api2.openreview.net/notes/search",
                params={"term": clean(row.get("title")).rstrip("."), "content": "title", "limit": 10},
                timeout=30,
            )
            attempts.append({"source": "openreview_exact_title_for_acm", **receipt(response)})
            if response.ok:
                expected_authors = {title_key(value) for value in row.get("authors") or [] if title_key(value)}
                expected_doi = clean((row.get("identifiers") or {}).get("doi")).lower()
                for note in response.json().get("notes") or []:
                    content = note.get("content") if isinstance(note, dict) and isinstance(note.get("content"), dict) else {}
                    value = lambda item: item.get("value") if isinstance(item, dict) and "value" in item else item
                    if title_key(value(content.get("title"))) != title_key(row.get("title")):
                        continue
                    abstract = clean(value(content.get("abstract")))
                    candidate_doi = clean(value(content.get("doi"))).lower()
                    candidate_authors = {
                        title_key(author)
                        for author in (value(content.get("authors")) or [])
                        if title_key(author)
                    }
                    if (
                        len(abstract) < 80
                        or expected_doi and candidate_doi and expected_doi != candidate_doi
                        or expected_authors and candidate_authors and expected_authors.isdisjoint(candidate_authors)
                    ):
                        continue
                    row["abstract"] = abstract
                    note_id = clean(note.get("id"))
                    if note_id:
                        row["pdf_url"] = clean(row.get("pdf_url")) or f"https://openreview.net/pdf?id={note_id}"
                    break
        except Exception as exc:
            attempts.append({"source": "openreview_exact_title_for_acm", "error": str(exc)[:300]})
    if not clean(row.get("abstract")):
        try:
            response = get("https://api.archives-ouvertes.fr/search/", params={"q": f'title_t:\"{clean(row.get("title"))}\"', "fl": "title_s,abstract_s,doiId_s,fileMain_s,uri_s", "rows": 5, "wt": "json"}, timeout=45)
            attempts.append({"source": "hal_title_for_acm", **receipt(response)})
            if response.ok:
                for item in ((response.json().get("response") or {}).get("docs") or []):
                    candidate_title = item.get("title_s")
                    if isinstance(candidate_title, list):
                        candidate_title = candidate_title[0] if candidate_title else ""
                    if title_key(candidate_title) != title_key(row.get("title")):
                        continue
                    abstract = item.get("abstract_s")
                    if isinstance(abstract, list):
                        abstract = abstract[0] if abstract else ""
                    row["abstract"] = clean(abstract)
                    file_url = item.get("fileMain_s")
                    if isinstance(file_url, list):
                        file_url = file_url[0] if file_url else ""
                    row["pdf_url"] = clean(row.get("pdf_url")) or clean(file_url)
                    break
        except Exception as exc:
            attempts.append({"source": "hal_title_for_acm", "error": str(exc)[:300]})
    if not clean(row.get("abstract")) and clean(row.get("pdf_url")):
        abstract = _official_pdf_abstract(row)
        attempts.append({"source": "indexed_oa_pdf_for_acm", "status": "abstract_extracted" if abstract else "no_abstract_extracted"})
        if abstract:
            row["abstract"] = abstract
    recent_arxiv_miss = (
        float(row.get("metadata", {}).get("checkpoint_mtime") or 0) >= time.time() - 86400
        and any(
            item.get("source") == "arxiv_title_match_for_acm" and item.get("status_code") == 200
            for item in attempts if isinstance(item, dict)
        )
    )
    if not clean(row.get("abstract")) and allow_remote_arxiv and not recent_arxiv_miss and cooldown_remaining("arxiv") <= 0:
        try:
            response = get("https://export.arxiv.org/api/query", params={"search_query": f'ti:\"{clean(row.get("title"))}\"', "start": 0, "max_results": 5}, timeout=90)
            attempts.append({"source": "arxiv_title_match_for_acm", **receipt(response)})
            if response.ok:
                ns = {"a": "http://www.w3.org/2005/Atom"}
                root = ET.fromstring(response.content)
                for entry in root.findall("a:entry", ns):
                    candidate_title = clean(entry.findtext("a:title", default="", namespaces=ns))
                    if title_key(candidate_title) != title_key(row.get("title")):
                        continue
                    row["abstract"] = clean(entry.findtext("a:summary", default="", namespaces=ns))
                    arxiv_url = clean(entry.findtext("a:id", default="", namespaces=ns))
                    if arxiv_url:
                        row["pdf_url"] = clean(row.get("pdf_url")) or arxiv_url.replace("/abs/", "/pdf/")
                    break
        except Exception as exc:
            attempts.append({"source": "arxiv_title_match_for_acm", "error": str(exc)[:300]})
    elif not clean(row.get("abstract")) and recent_arxiv_miss:
        attempts.append({"source": "arxiv_title_match_for_acm", "status": "skipped_recent_exact_title_miss"})
    return row


def _indexed_enrich(row: dict[str, Any], *, allow_remote_arxiv: bool = True) -> dict[str, Any]:
    # These are optional per-paper fallbacks, not the authoritative base crawl.
    # One throttled index must not hold a complete venue run for 22.5 minutes.
    with bounded_request_policy(max_attempts=2, max_wait_seconds=15.0):
        return _indexed_enrich_once(row, allow_remote_arxiv=allow_remote_arxiv)


def _batch_local_arxiv_enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reuse exact-title arXiv cache matches before making remote title calls."""
    wanted = {title_key(row.get("title")): row for row in rows if not clean(row.get("abstract"))}
    wanted.pop("", None)
    if not wanted:
        return rows
    arxiv_root = METADATA_CACHE_ROOT / "arxiv"
    if not arxiv_root.is_dir():
        return rows
    for path in arxiv_root.glob("*/*.json"):
        payload = read_json(path, {})
        papers = payload.get("papers") if isinstance(payload, dict) else []
        for paper in papers or []:
            if not isinstance(paper, dict):
                continue
            key = title_key(paper.get("title"))
            row = wanted.get(key)
            abstract = clean(paper.get("abstract"))
            if row is None or not abstract:
                continue
            row["abstract"] = abstract
            row["pdf_url"] = clean(row.get("pdf_url")) or clean(paper.get("pdf_url"))
            row.setdefault("metadata", {}).setdefault("indexed_enrichment", []).append({
                "source": "local_arxiv_exact_title_cache",
                "cache_path": str(path),
            })
            wanted.pop(key, None)
        if not wanted:
            break
    return rows


def _acm_stage_dir(venue_id: str, year: int):
    return DATA_ROOT / "state" / "venue-staging" / "acm" / venue_id / str(year)


def _acm_checkpoint_path(venue_id: str, year: int, row: dict[str, Any]):
    return _acm_stage_dir(venue_id, year) / f"{stable_id('paper', title_key(row.get('title'))).split(':', 1)[1]}.json"


def _restore_acm_checkpoints(venue_id: str, year: int, rows: list[dict[str, Any]]) -> int:
    restored = 0
    for row in rows:
        checkpoint_path = _acm_checkpoint_path(venue_id, year, row)
        payload = read_json(checkpoint_path, {})
        saved = payload.get("paper") if isinstance(payload, dict) else None
        if not isinstance(saved, dict) or title_key(saved.get("title")) != title_key(row.get("title")):
            continue
        for key in ("abstract", "pdf_url"):
            if clean(saved.get(key)):
                row[key] = saved[key]
        saved_metadata = saved.get("metadata") if isinstance(saved.get("metadata"), dict) else {}
        if saved_metadata.get("indexed_enrichment"):
            row.setdefault("metadata", {})["indexed_enrichment"] = saved_metadata["indexed_enrichment"]
        if saved_metadata.get("openaire_repository_urls"):
            row.setdefault("metadata", {})["openaire_repository_urls"] = saved_metadata["openaire_repository_urls"]
        if checkpoint_path.is_file():
            row.setdefault("metadata", {})["checkpoint_mtime"] = checkpoint_path.stat().st_mtime
        if clean(row.get("abstract")):
            restored += 1
    return restored


def _restore_acm_partial_metadata(venue_id: str, year: int, rows: list[dict[str, Any]]) -> int:
    """Reuse verified per-row fields without treating an old partial corpus as a cache hit."""
    venue = DBLP_TEMPLATES[venue_id][0]
    payload = read_json(METADATA_CACHE_ROOT / venue.lower() / f"{year}.json", {})
    saved_rows = payload.get("papers") if isinstance(payload, dict) and isinstance(payload.get("papers"), list) else []
    saved_by_title = {title_key(row.get("title")): row for row in saved_rows if isinstance(row, dict) and title_key(row.get("title"))}
    restored = 0
    for row in rows:
        saved = saved_by_title.get(title_key(row.get("title")))
        if not isinstance(saved, dict) or not clean(saved.get("abstract")):
            continue
        row["abstract"] = clean(saved.get("abstract"))
        row["pdf_url"] = clean(row.get("pdf_url")) or clean(saved.get("pdf_url"))
        row.setdefault("metadata", {}).setdefault("indexed_enrichment", []).append({
            "source": "partial_metadata_exact_title_checkpoint",
            "conference": venue,
            "year": year,
        })
        restored += 1
    return restored


def _save_acm_checkpoint(venue_id: str, year: int, row: dict[str, Any]) -> None:
    write_json(_acm_checkpoint_path(venue_id, year, row), {
        "schema_version": 1,
        "venue_id": venue_id,
        "year": year,
        "paper": row,
    })


def _write_acm_progress(venue_id: str, year: int, rows: list[dict[str, Any]], phase: str) -> None:
    write_json(_acm_stage_dir(venue_id, year) / "progress.json", {
        "schema_version": 1,
        "venue_id": venue_id,
        "year": year,
        "phase": phase,
        "total": len(rows),
        "abstracts_complete": sum(bool(clean(row.get("abstract"))) for row in rows),
        "remaining": sum(not clean(row.get("abstract")) for row in rows),
    })


def _batch_openalex_enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match the original ACM DOI enrichment semantics without one request/paper."""
    if cooldown_remaining("openalex") > 0:
        for row in rows:
            row.setdefault("metadata", {}).setdefault("indexed_enrichment", []).append({"source": "openalex_doi_for_acm_batch", "status": "skipped_persisted_long_cooldown", "cooldown_remaining_seconds": round(cooldown_remaining("openalex"), 3)})
        return rows
    by_doi = {
        clean((row.get("identifiers") or {}).get("doi")).lower(): row
        for row in rows
        if not clean(row.get("abstract")) and clean((row.get("identifiers") or {}).get("doi"))
    }
    dois = sorted(by_doi)
    for offset in range(0, len(dois), 50):
        chunk = dois[offset:offset + 50]
        try:
            with bounded_request_policy(max_attempts=2, max_wait_seconds=15.0):
                response = get(
                    "https://api.openalex.org/works",
                    params=_openalex_params({"filter": "doi:" + "|".join(chunk), "per-page": 50, "select": "id,doi,display_name,abstract_inverted_index,best_oa_location,primary_location,authorships"}),
                    timeout=90,
                )
            if not response.ok:
                continue
            for item in response.json().get("results") or []:
                doi = clean(item.get("doi")).lower().removeprefix("https://doi.org/")
                row = by_doi.get(doi)
                if not row or title_key(item.get("display_name")) != title_key(row.get("title")):
                    continue
                abstract = _openalex_abstract(item.get("abstract_inverted_index"))
                if abstract:
                    row["abstract"] = abstract
                    row.setdefault("metadata", {}).setdefault("indexed_enrichment", []).append({"source": "openalex_doi_for_acm_batch", **receipt(response)})
                best = item.get("best_oa_location") if isinstance(item.get("best_oa_location"), dict) else {}
                primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
                row["pdf_url"] = clean(row.get("pdf_url")) or clean(best.get("pdf_url")) or clean(primary.get("pdf_url"))
        except Exception:
            continue
    return rows


def _batch_semantic_scholar_enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Anonymous S2 commonly asks for a short pause. Let the bounded request
    # governor honor a small active cooldown instead of turning it into a
    # false permanent abstract gap; defer only genuinely long waits.
    if cooldown_remaining("semantic_scholar") > 15:
        return rows
    by_doi = {
        clean((row.get("identifiers") or {}).get("doi")).lower(): row
        for row in rows
        if not clean(row.get("abstract")) and clean((row.get("identifiers") or {}).get("doi"))
    }
    dois = sorted(by_doi)
    for offset in range(0, len(dois), 500):
        chunk = dois[offset:offset + 500]
        try:
            with bounded_request_policy(max_attempts=2, max_wait_seconds=15.0):
                response = post(
                    "https://api.semanticscholar.org/graph/v1/paper/batch",
                    params={"fields": "title,abstract,openAccessPdf,externalIds"},
                    json_body={"ids": [f"DOI:{doi}" for doi in chunk]},
                    headers=_semantic_scholar_headers(),
                    timeout=120,
                )
            if not response.ok:
                continue
            for item in response.json() or []:
                if not isinstance(item, dict):
                    continue
                external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
                doi = clean(external.get("DOI")).lower()
                row = by_doi.get(doi)
                if not row or title_key(item.get("title")) != title_key(row.get("title")):
                    continue
                if clean(item.get("abstract")):
                    row["abstract"] = clean(item.get("abstract"))
                    row.setdefault("metadata", {}).setdefault("indexed_enrichment", []).append({"source": "semantic_scholar_doi_for_acm_batch", **receipt(response)})
                oa = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
                row["pdf_url"] = clean(row.get("pdf_url")) or clean(oa.get("url"))
        except Exception:
            continue
    return rows


def _openaire_values(value: Any) -> list[str]:
    """Flatten OpenAIRE's JSON-encoded XML scalar/list representation."""
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        scalar = item.get("$") if isinstance(item, dict) else item
        text = clean(scalar)
        if text:
            result.append(text)
    return result


def _openaire_pdf_url(result: dict[str, Any]) -> str:
    pids = result.get("pid") if isinstance(result.get("pid"), list) else [result.get("pid")]
    for item in pids:
        if not isinstance(item, dict) or clean(item.get("@classid")).lower() != "arxiv":
            continue
        arxiv_id = clean(item.get("$"))
        if arxiv_id:
            return f"https://arxiv.org/pdf/{arxiv_id}"

    children = result.get("children") if isinstance(result.get("children"), dict) else {}
    child_rows = children.get("result") if isinstance(children.get("result"), list) else [children.get("result")]
    for child in child_rows:
        if not isinstance(child, dict):
            continue
        instances = child.get("instance") if isinstance(child.get("instance"), list) else [child.get("instance")]
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            urls = _openaire_values(instance.get("url"))
            web = instance.get("webresource")
            if isinstance(web, dict):
                urls.extend(_openaire_values(web.get("url")))
            for url in urls:
                if re.search(r"\.pdf(?:$|[?#])", url, re.I):
                    return url
                if re.search(r"arxiv\.org/abs/", url, re.I):
                    return re.sub(r"/abs/", "/pdf/", url, flags=re.I)
    return ""


def _batch_openaire_enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use OpenAIRE's documented multi-DOI query for exact ACM enrichment.

    Query only rows still missing an abstract.  This keeps an anonymous full
    WWW run below OpenAIRE's hourly request allowance after the higher-yield
    Semantic Scholar batch and prevents one-request-per-paper behavior.
    """
    if cooldown_remaining("openaire") > 15:
        return rows
    by_doi = {
        clean((row.get("identifiers") or {}).get("doi")).lower(): row
        for row in rows
        if not clean(row.get("abstract")) and clean((row.get("identifiers") or {}).get("doi"))
    }
    dois = sorted(by_doi)
    for offset in range(0, len(dois), 50):
        chunk = dois[offset:offset + 50]
        try:
            with bounded_request_policy(max_attempts=2, max_wait_seconds=15.0):
                response = get(
                    "https://api.openaire.eu/search/publications",
                    params={"doi": ",".join(chunk), "format": "json", "size": 100},
                    headers=_openaire_headers(),
                    timeout=90,
                )
            if not response.ok:
                continue
            results = (((response.json().get("response") or {}).get("results") or {}).get("result") or [])
            if isinstance(results, dict):
                results = [results]
            for wrapper in results:
                entity = ((wrapper.get("metadata") or {}).get("oaf:entity") or {}) if isinstance(wrapper, dict) else {}
                item = entity.get("oaf:result") if isinstance(entity, dict) else None
                if not isinstance(item, dict):
                    continue
                pids = item.get("pid") if isinstance(item.get("pid"), list) else [item.get("pid")]
                record_dois = {
                    clean(pid.get("$")).lower()
                    for pid in pids
                    if isinstance(pid, dict) and clean(pid.get("@classid")).lower() == "doi"
                }
                titles = _openaire_values(item.get("title"))
                for doi in record_dois & by_doi.keys():
                    row = by_doi[doi]
                    if not any(title_key(title) == title_key(row.get("title")) for title in titles):
                        continue
                    descriptions = [value for value in _openaire_values(item.get("description")) if len(value) >= 80]
                    if descriptions:
                        row["abstract"] = max(descriptions, key=len)
                    row["pdf_url"] = clean(row.get("pdf_url")) or _openaire_pdf_url(item)
                    if clean(row.get("abstract")) or clean(row.get("pdf_url")):
                        row.setdefault("metadata", {}).setdefault("indexed_enrichment", []).append({
                            "source": "openaire_doi_for_acm_batch",
                            **receipt(response),
                        })
        except Exception:
            continue
    return rows


def _openaire_v3_locations(item: dict[str, Any]) -> tuple[str, list[str]]:
    urls: list[str] = []
    pids = item.get("pids") if isinstance(item.get("pids"), list) else []
    for pid in pids:
        if not isinstance(pid, dict):
            continue
        if clean(pid.get("scheme")).lower() == "arxiv" and clean(pid.get("value")):
            urls.append(f"https://arxiv.org/pdf/{clean(pid.get('value'))}")
    for instance in item.get("instances") or []:
        if isinstance(instance, dict):
            urls.extend(clean(url) for url in (instance.get("urls") or []) if clean(url))
    urls = list(dict.fromkeys(urls))
    direct = next((url for url in urls if re.search(r"\.pdf(?:$|[?#])", url, re.I)), "")
    return direct, [url for url in urls if not re.fullmatch(r"https?://(?:dx\.)?doi\.org/.+", url, re.I)]


def _openaire_v3_dois(item: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for pid in item.get("pids") or []:
        if isinstance(pid, dict) and clean(pid.get("scheme")).lower() == "doi":
            values.add(clean(pid.get("value")).lower())
    for instance in item.get("instances") or []:
        if not isinstance(instance, dict):
            continue
        for identifier in instance.get("alternateIdentifiers") or []:
            if isinstance(identifier, dict) and clean(identifier.get("scheme")).lower() == "doi":
                values.add(clean(identifier.get("value")).lower())
    return values


def _batch_openaire_title_enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve not-yet-registered ACM DOIs through exact OpenAIRE titles.

    OpenAIRE accepts at most four logical operators, hence five titles per
    request.  This path matters for current-year WWW records whose official
    ACM DOI exists in the program before Crossref and DOI-based indexes ingest
    it.  Exact normalized title equality remains mandatory.
    """
    if cooldown_remaining("openaire") > 15:
        return rows
    by_title = {
        title_key(row.get("title")): row
        for row in rows
        if not clean(row.get("abstract")) and title_key(row.get("title"))
    }
    titles = [clean(row.get("title")).rstrip(".") for row in by_title.values()]
    for offset in range(0, len(titles), 5):
        chunk = [title.replace('"', "") for title in titles[offset:offset + 5]]
        expression = "(" + " OR ".join(f'"{title}"' for title in chunk) + ")"
        try:
            with bounded_request_policy(max_attempts=2, max_wait_seconds=15.0):
                response = get(
                    "https://api.openaire.eu/graph/v3/research-products",
                    params={"mainTitle": expression, "pageSize": 100},
                    headers=_openaire_headers(),
                    timeout=90,
                )
            if not response.ok:
                continue
            for item in response.json().get("results") or []:
                # OpenAIRE can return a same-title companion dataset carrying
                # the paper DOI.  Its dataset description is not a paper
                # abstract and its files are not paper full text.
                if not isinstance(item, dict) or clean(item.get("type")).lower() != "publication":
                    continue
                row = by_title.get(title_key(item.get("mainTitle")))
                if row is None:
                    continue
                expected_doi = clean((row.get("identifiers") or {}).get("doi")).lower()
                if expected_doi and expected_doi not in _openaire_v3_dois(item):
                    continue
                descriptions = [clean(value) for value in (item.get("descriptions") or []) if len(clean(value)) >= 80]
                if descriptions:
                    row["abstract"] = max(descriptions, key=len)
                direct_pdf, repository_urls = _openaire_v3_locations(item)
                row["pdf_url"] = clean(row.get("pdf_url")) or direct_pdf
                if repository_urls:
                    metadata = row.setdefault("metadata", {})
                    metadata["openaire_repository_urls"] = list(dict.fromkeys([
                        *(metadata.get("openaire_repository_urls") or []),
                        *repository_urls,
                    ]))
                if clean(row.get("abstract")) or clean(row.get("pdf_url")) or repository_urls:
                    row.setdefault("metadata", {}).setdefault("indexed_enrichment", []).append({
                        "source": "openaire_exact_title_for_acm_batch",
                        **receipt(response),
                    })
        except Exception:
            continue
    return rows


def _chatpaper_cache_path():
    return DATA_ROOT / "state" / "chatpaper-acm-abstracts.json"


def _save_chatpaper_cache(cache: dict[str, Any]) -> None:
    path = _chatpaper_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        existing = read_json(path, {})
        if isinstance(existing, dict):
            existing.setdefault("titles", {}).update(cache.get("titles") or {})
            existing.setdefault("venues", {}).update(cache.get("venues") or {})
            completed = list(dict.fromkeys([*(existing.get("completed_tracks") or []), *(cache.get("completed_tracks") or [])]))
            existing["completed_tracks"] = completed
            cache.clear()
            cache.update(existing)
        write_json(path, cache)


def _chatpaper_scalar(payload: list[Any], reference: Any) -> Any:
    value = reference
    seen: set[int] = set()
    while type(value) is int and 0 <= value < len(payload) and value not in seen:
        seen.add(value)
        value = payload[value]
    if isinstance(value, list) and len(value) == 2 and value[0] in {"ShallowReactive", "Reactive", "Ref", "ShallowRef"}:
        return _chatpaper_scalar(payload, value[1])
    return value


def _chatpaper_page_rows(page_text: str, venue: str, year: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page_text, "html.parser")
    node = soup.select_one("#__NUXT_DATA__")
    try:
        payload = json.loads((node.string or node.get_text()) if node else "")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(payload, list):
        return rows
    for value in payload:
        if not isinstance(value, dict) or not {"title", "abstract"}.issubset(value):
            continue
        title = re.sub(r"^\d+\.\s*", "", clean(_chatpaper_scalar(payload, value.get("title"))))
        abstract = clean(_chatpaper_scalar(payload, value.get("abstract")))
        paper_id = clean(_chatpaper_scalar(payload, value.get("id")))
        source_id = clean(_chatpaper_scalar(payload, value.get("source_id")))
        article_url = clean(_chatpaper_scalar(payload, value.get("article_url")))
        pdf_url = clean(_chatpaper_scalar(payload, value.get("pdf_url")))
        key = title_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        doi_match = re.search(r"10\.\d{4,9}/[^\s<>]+", f"{source_id} {article_url}", re.I)
        rows.append({
            "title": title,
            "abstract": abstract if len(abstract) >= 80 else "",
            "doi": doi_match.group(0).rstrip(".,)").lower() if doi_match else "",
            "url": f"https://chatpaper.com/paper/{paper_id}" if paper_id.isdigit() else "",
            "pdf_url": pdf_url,
            "venue": venue,
            "year": year,
        })
    return rows


def _chatpaper_track_ids(venue: str, year: int, cache: dict[str, Any]) -> list[int]:
    venue_key = f"{venue}:{year}"
    venue_cache = cache.setdefault("venues", {})
    saved = venue_cache.get(venue_key) if isinstance(venue_cache, dict) else None
    if isinstance(saved, dict) and isinstance(saved.get("track_ids"), list) and saved.get("track_ids"):
        return [int(value) for value in saved["track_ids"] if str(value).isdigit()]
    root = get("https://chatpaper.com/venues", timeout=30)
    root.raise_for_status()
    soup = BeautifulSoup(root.text, "html.parser")
    target = f"{venue} {year}".lower()
    first_id = ""
    for link in soup.find_all("a", href=True):
        if target not in clean(link.get_text(" ", strip=True)).lower():
            continue
        match = re.search(r"[?&]id=(\d+)", str(link.get("href") or ""))
        if match:
            first_id = match.group(1)
            break
    track_ids: list[int] = []
    if first_id:
        page = get(f"https://chatpaper.com/venues?id={first_id}&page=1", timeout=30)
        page.raise_for_status()
        track_soup = BeautifulSoup(page.text, "html.parser")
        for link in track_soup.find_all("a", href=True):
            if "Papers" not in clean(link.get_text(" ", strip=True)):
                continue
            match = re.search(r"[?&]id=(\d+)", str(link.get("href") or ""))
            if match and int(match.group(1)) not in track_ids:
                track_ids.append(int(match.group(1)))
        if not track_ids:
            match = re.search(r"[?&]id=(\d+)", page.url)
            if match:
                track_ids.append(int(match.group(1)))
    venue_cache[venue_key] = {"track_ids": track_ids, "updated_at": time.time()}
    _save_chatpaper_cache(cache)
    return track_ids


def _chatpaper_enrich(rows: list[dict[str, Any]], venue_id: str, year: int) -> dict[str, Any]:
    venue = DBLP_TEMPLATES[venue_id][0]
    missing = {title_key(row.get("title")): row for row in rows if not clean(row.get("abstract")) and title_key(row.get("title"))}
    stats = {"source": "chatpaper_title_for_acm", "attempted": len(missing), "abstracts_filled": 0, "track_ids": []}
    if not missing:
        return stats
    cache = read_json(_chatpaper_cache_path(), {})
    if not isinstance(cache, dict):
        cache = {}
    cached_titles = cache.setdefault("titles", {})

    def apply(item: dict[str, Any]) -> bool:
        key = title_key(item.get("title"))
        row = missing.get(key)
        item_doi = clean(item.get("doi")).lower()
        if row is None and item_doi:
            row = next((candidate for candidate in missing.values() if clean((candidate.get("identifiers") or {}).get("doi")).lower() == item_doi), None)
        abstract = clean(item.get("abstract"))
        if row is None or len(abstract) < 80:
            return False
        row_doi = clean((row.get("identifiers") or {}).get("doi")).lower()
        if row_doi and item_doi and row_doi != item_doi:
            return False
        row["abstract"] = abstract
        row["pdf_url"] = clean(row.get("pdf_url")) or clean(item.get("pdf_url"))
        row.setdefault("metadata", {}).setdefault("indexed_enrichment", []).append({
            "source": "chatpaper_title_for_acm",
            "url": clean(item.get("url")),
            "identity_gate": "exact_normalized_title_plus_doi_when_available",
        })
        missing.pop(title_key(row.get("title")), None)
        stats["abstracts_filled"] += 1
        return True

    for key in list(missing):
        item = cached_titles.get(f"{venue}:{year}:{key}") if isinstance(cached_titles, dict) else None
        if isinstance(item, dict):
            apply(item)
    if missing and isinstance(cached_titles, dict):
        for item in cached_titles.values():
            if isinstance(item, dict) and item.get("venue") == venue and int(item.get("year") or 0) == year:
                apply(item)
    if not missing:
        return stats
    try:
        track_ids = _chatpaper_track_ids(venue, year, cache)
    except Exception as exc:
        stats["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return stats
    stats["track_ids"] = track_ids
    completed_tracks = cache.setdefault("completed_tracks", [])
    for track_id in track_ids:
        track_key = f"{venue}:{year}:{track_id}"
        if track_key in completed_tracks:
            continue
        seen_track_titles: set[str] = set()
        for page_number in range(1, 81):
            try:
                response = get(f"https://chatpaper.com/venues?id={track_id}&page={page_number}", timeout=30)
                response.raise_for_status()
            except Exception as exc:
                stats.setdefault("errors", []).append(f"track={track_id} page={page_number}: {type(exc).__name__}: {str(exc)[:200]}")
                break
            page_rows = _chatpaper_page_rows(response.text, venue, year)
            if not page_rows:
                completed_tracks.append(track_key)
                _save_chatpaper_cache(cache)
                break
            new_rows = [item for item in page_rows if title_key(item.get("title")) not in seen_track_titles]
            if not new_rows:
                completed_tracks.append(track_key)
                _save_chatpaper_cache(cache)
                break
            for item in new_rows:
                key = title_key(item.get("title"))
                seen_track_titles.add(key)
                if key in missing and not clean(item.get("abstract")) and clean(item.get("url")):
                    try:
                        paper_page = get(clean(item.get("url")), timeout=30)
                        paper_page.raise_for_status()
                        paper_soup = BeautifulSoup(paper_page.text, "html.parser")
                        page_title_node = paper_soup.select_one(".doc-name-main")
                        page_abstract_node = paper_soup.select_one("#abstract.doc-abstract") or paper_soup.select_one(".doc-abstract")
                        page_title = re.sub(r"^\d+\.\s*", "", clean(page_title_node.get_text(" ", strip=True) if page_title_node else ""))
                        page_abstract = clean(page_abstract_node.get_text(" ", strip=True) if page_abstract_node else "")
                        if title_key(page_title) == key and len(page_abstract) >= 80:
                            item["title"] = page_title
                            item["abstract"] = page_abstract
                    except Exception as exc:
                        stats.setdefault("errors", []).append(f"paper={item.get('url')}: {type(exc).__name__}: {str(exc)[:200]}")
                if clean(item.get("abstract")):
                    cached_titles[f"{venue}:{year}:{key}"] = item
                apply(item)
            _save_chatpaper_cache(cache)
            if not missing:
                return stats
    stats["remaining"] = len(missing)
    return stats


DBLP_TEMPLATES = {
    "kdd": ("KDD", "https://dblp.org/db/conf/kdd/kdd{year}.xml"),
    "sigir": ("SIGIR", "https://dblp.org/db/conf/sigir/sigir{year}.xml"),
    "cikm": ("CIKM", "https://dblp.org/db/conf/cikm/cikm{year}.xml"),
    "www": ("WWW", "https://dblp.org/db/conf/www/www{year}.xml"),
}


def _dblp_xml_urls_from_index(index_html: str, index_url: str, venue_id: str, year: int) -> list[str]:
    """Discover every DBLP TOC volume for a venue/year.

    DBLP sometimes splits one proceedings edition into multiple files (KDD
    2025 is ``kdd2025-1`` plus ``kdd2025-2``), so synthesizing one fixed
    ``<venue><year>.xml`` URL is not an exhaustion proof.
    """
    soup = BeautifulSoup(index_html, "html.parser")
    prefix = f"/db/conf/{venue_id}/"
    urls: list[str] = []
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(index_url, str(anchor.get("href") or ""))
        path = re.sub(r"[?#].*$", "", absolute)
        if prefix not in path or not path.lower().endswith(".html"):
            continue
        filename = path.rsplit("/", 1)[-1]
        if str(year) not in filename or filename.lower() == "index.html":
            continue
        xml_url = re.sub(r"\.html$", ".xml", path, flags=re.I)
        if xml_url not in urls:
            urls.append(xml_url)
    return urls


def _dblp_year_xml_urls(venue_id: str, year: int) -> tuple[list[str], list[dict[str, Any]]]:
    index_url = f"https://dblp.org/db/conf/{venue_id}/index.html"
    try:
        response = _response(index_url, timeout=90)
        urls = _dblp_xml_urls_from_index(response.text, response.url, venue_id, year)
        if urls:
            return urls, [receipt(response)]
    except Exception:
        pass
    # Retain a deterministic fallback for temporary index-page failures.  The
    # subsequent request must still succeed before the crawl can be certified.
    return [DBLP_TEMPLATES[venue_id][1].format(year=year)], []


def _official_acm_title_seed(venue_id: str, year: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Use the same official accepted/proceedings pages prioritized by TASTE.

    These pages are title-pool evidence.  Their rows are subsequently merged with
    DBLP DOI records and enriched; title-only official pages are never accepted on
    their own as complete metadata.
    """
    urls: list[str] = []
    if venue_id == "www":
        urls = [
            f"https://www{year}.thewebconf.org/program/full-schedule.html",
            f"https://www{year}.thewebconf.org/accepted/research-tracks.html",
        ]
    elif venue_id == "sigir":
        urls = [
            f"https://sigir{year}.org/en-AU/pages/program/accepted-papers",
            f"https://sigir{year}.dei.unipd.it/accepted-papers.html",
            f"https://sigir{year}.dei.unipd.it/proceedings.html",
        ]
    elif venue_id == "cikm":
        urls = [
            f"https://www.cikm{year}.org/program/accepted-papers",
            f"https://cikm{year}.org/program/accepted-papers",
            f"https://cikm{year}.org/program/proceedings",
        ]
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()
    venue = DBLP_TEMPLATES[venue_id][0]
    for url in urls:
        try:
            response = _response(url, timeout=60)
        except Exception:
            continue
        receipts.append(receipt(response))
        page_text = response.text
        if venue_id == "sigir":
            # Current SIGIR pages place the accepted-paper markup in escaped
            # Next.js flight data rather than in the visible DOM.
            page_text = html.unescape(page_text).replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
            embedded_paragraphs = "".join(re.findall(r"<p(?:\s[^>]*)?>.*?</p>", page_text, re.I | re.S))
            soup = BeautifulSoup(embedded_paragraphs, "html.parser")
        else:
            soup = BeautifulSoup(page_text, "html.parser")
        if venue_id == "cikm":
            candidates = soup.find_all("tr")
        elif venue_id == "sigir":
            candidates = [node for node in soup.find_all("p") if node.find("i") and re.search(r"\[[a-z]+]", node.get_text(" ", strip=True), re.I)]
        elif venue_id == "www":
            candidates = soup.select("li.paper-item") or [node for node in soup.find_all("li") if node.select_one(".paper-id")]
        else:
            candidates = soup.select("article, tr, li, .paper, .accepted-paper, .paper-item, .program-item")
        for node in candidates:
            anchor = node.find("a", href=True)
            cells = node.find_all(["td", "th"], recursive=False)
            if venue_id == "cikm" and len(cells) >= 2:
                title = clean(cells[1].get_text(" ", strip=True))
            elif venue_id == "cikm":
                continue
            elif venue_id == "sigir":
                title_node = node.find("i")
                title = clean(title_node.get_text(" ", strip=True) if title_node else "")
            else:
                title_node = node.select_one(".title, .paper-title, h3, h4") or anchor
                title = clean(title_node.get_text(" ", strip=True) if title_node else node.get_text(" ", strip=True))
                if venue_id in {"www", "sigir"}:
                    title = re.sub(r"^\s*(?:\([^)]*\)|\[[^]]*\]|[a-z]{1,5}\d{2,})\s*", "", title, flags=re.I)
                    title = re.split(r"\s+(?:—|–|--)\s+", title, maxsplit=1)[0]
            title = re.sub(r"^(?:research|applied|paper)\s*[:#-]?\s*", "", title, flags=re.I)
            key = title_key(title)
            if not looks_like_title(title) or key in seen or len(title) > 500:
                continue
            # Reject navigation and prose blocks mistaken for titles.
            if len(title.split()) > 40 or any(marker in title.lower() for marker in ("call for papers", "accepted papers", "conference program", "privacy policy")):
                continue
            seen.add(key)
            detail_url = urljoin(url, str(anchor.get("href") or "")) if anchor else ""
            text = clean(node.get_text(" ", strip=True))
            doi_match = re.search(r"10\.1145/\d+(?:\.\d+)?", f"{detail_url} {text}", re.I)
            authors: list[str] = []
            if venue_id == "cikm" and len(cells) >= 3:
                for affiliation in cells[2].find_all("i"):
                    affiliation.decompose()
                authors = [clean(value) for value in re.split(r"\s*(?:,|\band\b)\s*", cells[2].get_text(" ", strip=True), flags=re.I) if clean(value)]
            elif venue_id == "www":
                author_node = node.select_one(".paper-authors")
                authors = [clean(value) for value in re.split(r"\s*(?:,|\band\b)\s*", author_node.get_text(" ", strip=True) if author_node else "", flags=re.I) if clean(value)]
            elif venue_id == "sigir" and title_node:
                remainder = node.get_text(" ", strip=True).replace("\\n", " ")
                remainder = remainder.split(title, 1)[-1]
                authors = [clean(value) for value in re.split(r"\s*(?:,|\band\b)\s*", remainder, flags=re.I) if clean(value)]
            rows.append({"title": title, "abstract": "", "authors": authors, "published": f"{year}-01-01", "year": year, "url": detail_url, "pdf_url": "", "venue": venue, "categories": [], "identifiers": {"doi": doi_match.group(0) if doi_match else ""}, "metadata": {"official_accepted_source": url}})
        if rows:
            break
    return rows, receipts


def _official_cikm_proceedings(year: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read CIKM's published proceedings mirror when it embeds every abstract.

    The accepted-papers table is only a preliminary title pool. CIKM 2025's
    official proceedings page is materially stronger: each published item has
    an ACM DOI, authors, and the full abstract in the page itself. Treat that
    page as the authoritative corpus instead of unioning it with broader DBLP
    or preliminary accepted-paper rows that cannot all be abstract-enriched.
    """
    url = f"https://cikm{year}.org/program/proceedings"
    try:
        response = _response(url, timeout=120)
    except Exception:
        return [], []
    soup = BeautifulSoup(response.text, "html.parser")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select('a.sub_title_3[href*="dl.acm.org/doi/"]'):
        title = clean(anchor.get_text(" ", strip=True))
        key = title_key(title)
        if not looks_like_title(title) or key in seen:
            continue
        abstract_node = anchor.find_next("div", class_="sub_txt")
        authors_node = anchor.find_next("ul", class_="proceed_name_list_han")
        abstract = clean(abstract_node.get_text(" ", strip=True) if abstract_node else "")
        authors = [clean(node.get_text(" ", strip=True)) for node in authors_node.select("li")] if authors_node else []
        detail_url = urljoin(url, str(anchor.get("href") or ""))
        doi_match = re.search(r"10\.1145/\d+(?:\.\d+)?", detail_url, re.I)
        session_node = anchor.find_previous(
            lambda node: getattr(node, "name", None) in {"div", "h2", "h3", "h4"}
            and "SESSION:" in clean(node.get_text(" ", strip=True))
        )
        session = re.sub(
            r"^SESSION:\s*", "", clean(session_node.get_text(" ", strip=True) if session_node else ""), flags=re.I
        )
        seen.add(key)
        rows.append({
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "published": f"{year}-01-01",
            "year": year,
            "url": detail_url,
            "pdf_url": f"https://dl.acm.org/doi/pdf/{doi_match.group(0)}" if doi_match else "",
            "venue": "CIKM",
            "categories": [session] if session else [],
            "identifiers": {"doi": doi_match.group(0) if doi_match else ""},
            "metadata": {"official_proceedings_source": url, "session": session},
        })
    # A partial/template page is not an authoritative proceedings corpus. The
    # common validator below remains the sole completion gate for every venue.
    if not rows or any(len(clean(row.get("abstract"))) < 80 or not row.get("authors") for row in rows):
        return [], [receipt(response)]
    return rows, [receipt(response)]


def _official_sigir_program(year: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract the paper catalog and embedded abstracts from SIGIR's program PDF."""
    url = f"https://sigir{year}.org/SIGIR{year}_program.pdf"
    try:
        response = get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0 (compatible; TASTE-Recommend-Papers/1.0)"})
        response.raise_for_status()
        if not response.content.startswith(b"%PDF"):
            return [], [receipt(response)]
        document = fitz.open(stream=response.content, filetype="pdf")
    except Exception:
        return [], []
    blocks: list[tuple[int, float, str, int]] = []
    try:
        for page_index, page in enumerate(document):
            page_blocks: list[tuple[int, float, str, int]] = []
            for block in page.get_text("blocks", sort=False):
                text = "\n".join(line.strip() for line in block[4].splitlines() if line.strip())
                if not text:
                    continue
                column = 0 if float(block[0]) < 290 else 1
                page_blocks.append((column, float(block[1]), text, page_index + 1))
            blocks.extend(sorted(page_blocks, key=lambda item: (item[0], item[1])))
    finally:
        document.close()
    track_pattern = re.compile(
        r"\[(Full|Short|Industry|Resource|Reproducibility|Demo|Low-Resource|Perspective|TOIS)\]\s*$",
        re.I,
    )
    headings: list[tuple[int, str, str, int]] = []
    for index, (_column, _top, text, page_number) in enumerate(blocks):
        flattened = clean(text)
        match = track_pattern.search(flattened)
        if not match:
            continue
        title = clean(track_pattern.sub("", flattened))
        if looks_like_title(title):
            headings.append((index, title, match.group(1), page_number))
    rows: list[dict[str, Any]] = []
    for heading_index, (block_index, title, track, page_number) in enumerate(headings):
        end = headings[heading_index + 1][0] if heading_index + 1 < len(headings) else len(blocks)
        lines: list[str] = []
        for _column, _top, text, _page in blocks[block_index + 1:end]:
            lines.extend(line.strip() for line in text.splitlines() if line.strip())
        author_end = 0
        author_text = ""
        for line_index, line in enumerate(lines[:40]):
            author_text = clean(f"{author_text} {line}")
            if (
                "(" in author_text
                and author_text.count("(") == author_text.count(")")
                and re.search(r"\)\s*$", line)
                and not re.search(r"\)\s*;\s*$", line)
            ):
                author_end = line_index + 1
                break
        authors = [clean(part.split("(", 1)[0]) for part in author_text.split(";") if clean(part.split("(", 1)[0])]
        abstract_lines: list[str] = []
        for line in lines[author_end:]:
            if re.search(
                r"·\s*[^·]+\s*·\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
                line,
                re.I,
            ):
                break
            if re.fullmatch(rf"(?:SIGIR\s*{year}|Program|\d+)", line, re.I):
                continue
            abstract_lines.append(line)
        abstract = clean(" ".join(abstract_lines))
        abstract = re.sub(r"^\\begin\{abstract\}\s*", "", abstract, flags=re.I)
        abstract = re.sub(r"\\end\{abstract\}\s*$", "", abstract, flags=re.I)
        abstract = re.split(
            r"\s+[A-Z][^.!?]{2,80}\s+·\s+[^·]{2,40}\s+·\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
            abstract,
            maxsplit=1,
            flags=re.I,
        )[0]
        rows.append({
            "title": title,
            "abstract": abstract if len(abstract) >= 80 else "",
            "authors": authors,
            "published": f"{year}-01-01",
            "year": year,
            "url": url,
            "pdf_url": "",
            "venue": "SIGIR",
            "categories": [track],
            "identifiers": {},
            "metadata": {"official_program_source": url, "program_page": page_number, "track": track},
        })
    # Reject a cover-only or malformed booklet; individual missing abstracts
    # remain eligible for the same exact-identity fallback used by all ACM rows.
    if len(rows) < 100 or not all(row.get("authors") for row in rows):
        return [], [receipt(response)]
    return rows, [receipt(response)]


def _merge_title_rows(primary: list[dict[str, Any]], enrichment: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_title = {title_key(row.get("title")): row for row in enrichment if title_key(row.get("title"))}
    out: list[dict[str, Any]] = []
    for row in primary:
        match = by_title.get(title_key(row.get("title")))
        if match:
            for key in ("authors", "url", "pdf_url", "abstract"):
                if not row.get(key) and match.get(key):
                    row[key] = match[key]
            identifiers = row.setdefault("identifiers", {})
            identifiers.update({key: value for key, value in (match.get("identifiers") or {}).items() if value and not identifiers.get(key)})
            row.setdefault("metadata", {}).update(match.get("metadata") or {})
        out.append(row)
    return out


def fetch_acm_venue(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    venue_id = clean(spec.get("venue_id")).lower()
    venue, _template = DBLP_TEMPLATES[venue_id]
    year = int(spec["years"][0])
    if venue_id == "cikm":
        proceedings_rows, proceedings_receipts = _official_cikm_proceedings(year)
        if proceedings_rows:
            discovered_count = len(proceedings_rows)
            selected = _selected_rows(spec, proceedings_rows)
            result_rows, details = _finish_rows(
                spec,
                selected,
                adapter="cikm_official_proceedings",
                requests=proceedings_receipts,
                proof="official_cikm_proceedings_page_exhausted_with_embedded_abstracts",
                discovered_count=discovered_count,
            )
            details.update({
                "official_proceedings_count": discovered_count,
                "official_title_pool_count": discovered_count,
                "checkpoint_mode": "not_needed_official_embedded_abstracts",
            })
            return result_rows, details
    sigir_program_rows: list[dict[str, Any]] = []
    sigir_program_receipts: list[dict[str, Any]] = []
    if venue_id == "sigir":
        sigir_program_rows, sigir_program_receipts = _official_sigir_program(year)
    urls, index_receipts = _dblp_year_xml_urls(venue_id, year)
    if sigir_program_rows:
        official_rows, official_receipts = sigir_program_rows, sigir_program_receipts
    else:
        official_rows, official_receipts = _official_acm_title_seed(venue_id, year)
    rows = []
    requests = [*index_receipts, *official_receipts]
    seen = set()
    for url in urls:
        response = get(url, timeout=120)
        requests.append(receipt(response))
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for record in root.iter("inproceedings"):
            if clean(record.findtext("year")) not in {"", str(year)}:
                continue
            node = record.find("title")
            title = clean("".join(node.itertext()) if node is not None else "")
            key = title_key(title)
            if not looks_like_title(title) or key in seen:
                continue
            seen.add(key)
            authors = [clean("".join(a.itertext())) for a in record.findall("author")]
            ee = [clean("".join(n.itertext())) for n in record.findall("ee")]
            doi = ""
            for value in ee:
                match = re.search(r"doi\.org/(10\.\d{4,9}/[^\s]+)", value, re.I)
                if match:
                    doi = match.group(1).rstrip(".,)")
                    break
            rows.append({"title": title, "abstract": "", "authors": authors, "published": f"{year}-01-01", "year": year, "url": next((value for value in ee if value), ""), "pdf_url": next((value for value in ee if re.search(r"\.pdf(?:$|[?#])", value, re.I)), ""), "venue": venue, "categories": [], "identifiers": {"doi": doi, "dblp_key": clean(record.attrib.get("key"))}, "metadata": {"dblp_seed_url": url, "seed_only": True}})
    if sigir_program_rows:
        rows = _merge_title_rows(sigir_program_rows, rows)
        for row in rows:
            row.setdefault("metadata", {})["official_title_pool_observed"] = True
    elif official_rows:
        official_keys = {title_key(row.get("title")) for row in official_rows}
        dblp_by_title = {title_key(row.get("title")): row for row in rows}
        missing_from_dblp = [row for row in official_rows if title_key(row.get("title")) not in dblp_by_title]
        rows = _merge_title_rows(rows, official_rows) + missing_from_dblp
        for row in rows:
            row.setdefault("metadata", {})["official_title_pool_observed"] = title_key(row.get("title")) in official_keys
    discovered_count = len(rows)
    rows = _selected_rows(spec, rows)
    restored_partial_metadata = _restore_acm_partial_metadata(venue_id, year, rows)
    restored_checkpoints = _restore_acm_checkpoints(venue_id, year, rows)
    _batch_local_arxiv_enrich(rows)
    # TASTE permits indexed abstract enrichment for ACM venues, but never accepts
    # the DBLP title seed by itself as a verified cache.
    _batch_openalex_enrich(rows)
    _batch_semantic_scholar_enrich(rows)
    _batch_openaire_enrich(rows)
    chatpaper_stats = _chatpaper_enrich(rows, venue_id, year)
    # ChatPaper venue pages resolve many current-year ACM titles in a handful
    # of requests.  Run that high-yield exact-title source before OpenAIRE's
    # five-title search so one conference cannot consume the anonymous
    # 60-request hourly allowance on records the venue page already contains.
    _batch_openaire_title_enrich(rows)
    for row in rows:
        _save_acm_checkpoint(venue_id, year, row)
    _write_acm_progress(venue_id, year, rows, "batch_index_enrichment")
    missing_rows = [row for row in rows if not clean(row.get("abstract"))]
    # ACM often challenges automated clients. Stop at the first persisted
    # cooldown so queued requests cannot each wait through the same window.
    for row in list(missing_rows):
        if cooldown_remaining("acm") > 0:
            break
        _try_acm_pdf_abstract(row)
        _save_acm_checkpoint(venue_id, year, row)
        _write_acm_progress(venue_id, year, rows, "acm_pdf_enrichment")
    missing_rows = [row for row in rows if not clean(row.get("abstract"))]
    # A long OpenAlex/OpenAIRE cooldown must not abort the source while other
    # independent exact-identity routes (Semantic Scholar, HAL, arXiv, public
    # PDFs) remain usable.  Each fallback below checks its own channel budget
    # and skips only the unavailable service, so continuing is bounded and
    # resumable rather than expanding the cooled-down request queue.
    arxiv_attempts = 0
    before_by_title = {
        title_key(row.get("title")): sum(
            item.get("source") == "arxiv_title_match_for_acm"
            for item in row.setdefault("metadata", {}).setdefault("indexed_enrichment", [])
        )
        for row in missing_rows
    }
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(missing_rows)))) as pool:
        futures = {pool.submit(_indexed_enrich, row, allow_remote_arxiv=True): row for row in missing_rows}
        for future in as_completed(futures):
            row = futures[future]
            future.result()
            after = sum(
                item.get("source") == "arxiv_title_match_for_acm"
                and item.get("status") != "skipped_run_request_budget"
                for item in row["metadata"]["indexed_enrichment"]
            )
            arxiv_attempts += max(0, after - before_by_title[title_key(row.get("title"))])
            _save_acm_checkpoint(venue_id, year, row)
            _write_acm_progress(venue_id, year, rows, "indexed_fallback_enrichment")
    if sigir_program_rows:
        proof = "official_sigir_program_pdf_exhausted_with_exact_identity_enrichment"
    else:
        proof = "official_accepted_pool_merged_with_dblp_and_taste_indexed_abstract_enrichment" if official_rows else "complete_dblp_title_pool_with_taste_indexed_abstract_enrichment"
    result_rows, details = _finish_rows(
        spec,
        rows,
        adapter=f"{venue_id}_acm_enriched",
        requests=requests,
        proof=proof,
        discovered_count=discovered_count,
    )
    details.update({
        "official_title_pool_count": len(official_rows),
        "dblp_seed_count": discovered_count - max(0, len([row for row in official_rows if title_key(row.get('title')) not in seen])),
        "restored_checkpoints": restored_checkpoints,
        "restored_partial_metadata_rows": restored_partial_metadata,
        "checkpoint_mode": "per_paper_resumable",
        "chatpaper_abstract_enrichment": chatpaper_stats,
        "remote_arxiv_fallback_attempts": arxiv_attempts,
        "remote_arxiv_fallback_coverage": "all_remaining_rows",
    })
    stage_dir = _acm_stage_dir(venue_id, year)
    if stage_dir.is_dir():
        shutil.rmtree(stage_dir)
    return result_rows, details
