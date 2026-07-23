from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import fitz
from bs4 import BeautifulSoup

from .credentials import openreview_settings
from .conference_sources import official_pdf_candidates
from .http import bounded_request_policy, cooldown_remaining, get, receipt, service_call, service_for
from .metadata import clean, paper_identity
from .storage import FULLTEXT_CACHE_ROOT, now_iso, read_json, safe_write_target, stable_hash, update_run, write_json, write_text


MIN_FULL_TEXT_CHARS = 1200
ACQUISITION_SCHEMA_VERSION = 2
_cache_publish_lock = threading.Lock()
_openreview_client_lock = threading.Lock()
_openreview_client_cache: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}


def _openalex_params() -> dict[str, str]:
    api_key = clean(os.environ.get("OPENALEX_API_KEY"))
    return {"api_key": api_key} if api_key else {}


def _semantic_scholar_headers() -> dict[str, str]:
    api_key = clean(os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY"))
    return {"x-api-key": api_key} if api_key else {}


def _title_tokens(value: Any) -> set[str]:
    stop = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "toward", "towards", "with", "via", "using"}
    normalized = re.sub(r"[\u2010-\u2015]", "-", clean(value))
    return {token.lower() for token in re.findall(r"[A-Za-z0-9]+", normalized) if len(token) >= 2 and token.lower() not in stop}


def _title_similarity(left: Any, right: Any) -> float:
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def _author_family_tokens(value: Any) -> set[str]:
    names = [clean(item) for item in value] if isinstance(value, list) else re.split(r"[,;]", clean(value))
    families = set()
    for name in names:
        parts = [part.lower() for part in re.findall(r"[A-Za-z][A-Za-z-]+", name)]
        if parts:
            families.add(parts[-1])
    return families


def _best_full_text_title(paper: dict[str, Any], text: str) -> tuple[str, float, int]:
    """Find a title-like window before the abstract and bind it to author evidence."""
    lines = []
    for raw_line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()[:240].rstrip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith(("abstract", "references", "acknowledgments", "acknowledgements")):
            break
        if lower.startswith(("keywords", "introduction")) and lines:
            break
        lines.append(line)
        if len(lines) >= 36:
            break
    if not lines:
        return "", 0.0, 0
    expected_title = clean(paper.get("title"))
    best_title = ""
    best_similarity = 0.0
    for start in range(min(10, len(lines))):
        for count in range(1, min(10, len(lines) - start) + 1):
            candidate = " ".join(lines[start:start + count])
            candidate = re.sub(r"([A-Za-z]{2,})-\s+([A-Za-z]{1,3})(?=\b)", r"\1\2", candidate)
            candidate = re.sub(r"([A-Za-z])-\s+([A-Za-z])", r"\1 \2", candidate)
            candidate = re.sub(r"\s+", " ", candidate).strip()
            similarity = _title_similarity(expected_title, candidate)
            if similarity > best_similarity:
                best_title, best_similarity = candidate, similarity
    expected_authors = _author_family_tokens(paper.get("authors"))
    observed_tokens = {token.lower() for token in re.findall(r"[A-Za-z][A-Za-z-]+", " ".join(lines))}
    author_overlap = len(expected_authors & observed_tokens)
    return best_title, best_similarity, author_overlap


def _identity_ok(paper: dict[str, Any], text: str) -> bool:
    if not _title_tokens(paper.get("title")):
        return False
    _, similarity, author_overlap = _best_full_text_title(paper, text)
    expected_authors = _author_family_tokens(paper.get("authors"))
    if not expected_authors:
        return similarity >= 0.92
    return (
        (similarity >= 0.82 and author_overlap >= 1)
        or (similarity >= 0.78 and author_overlap >= 2)
        or (similarity >= 0.70 and author_overlap >= 4)
    )


def _body_ok(text: str) -> bool:
    lower = text.lower()
    markers = sum(marker in lower for marker in ("abstract", "introduction", "method", "methods", "results", "discussion", "references", "conclusion"))
    return len(text) >= MIN_FULL_TEXT_CHARS and markers >= 2


def _pdf_text(path: Path) -> str:
    document = fitz.open(path)
    try:
        return "\n\n".join(page.get_text("text") for page in document)
    finally:
        document.close()


def _identifier(paper: dict[str, Any], name: str) -> str:
    identifiers = paper.get("identifiers") if isinstance(paper.get("identifiers"), dict) else {}
    return clean(identifiers.get(name) or paper.get(name))


def _title_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean(value).lower())


def _openreview_client():
    import openreview

    settings = openreview_settings()
    identity = (settings["username"], str(settings["env_file"]))
    with _openreview_client_lock:
        cached_client = _openreview_client_cache.get(identity)
        if cached_client is not None:
            return cached_client[0], {**cached_client[1], "client_reused": True}

        def create():
            return openreview.api.OpenReviewClient(
                baseurl="https://api2.openreview.net",
                username=settings["username"] or None,
                password=settings["password"] or None,
            )

        client, retry_history = service_call("openreview", create, max_attempts=5)
        client_settings = {**settings, "client_reused": False, "login_retry_history": retry_history}
        _openreview_client_cache[identity] = (client, client_settings)
        return client, client_settings


def _authenticated_openreview_pdf(paper: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    note_id = _identifier(paper, "openreview_id")
    attempt: dict[str, Any] = {"kind": "openreview_authenticated_attachment", "openreview_id": note_id}
    if not note_id:
        return b"", {**attempt, "accepted": False, "reason": "openreview_id_missing"}
    try:
        client, settings = _openreview_client()
        failures = []
        for field_name in ("pdf", "originally_submitted_PDF"):
            try:
                content, retry_history = service_call(
                    "openreview",
                    lambda: client.get_attachment(field_name=field_name, id=note_id),
                    max_attempts=5,
                )
            except Exception as exc:
                failures.append({
                    "field_name": field_name,
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:300],
                    "retry_history": list(getattr(exc, "_taste_retry_history", []) or []),
                })
                continue
            attempt.update({
                "authenticated": settings["authenticated"],
                "attachment_field": field_name,
                "content_length": len(content or b""),
                "retry_history": retry_history,
                "client_login_retry_history": settings.get("login_retry_history") or [],
            })
            if not content:
                failures.append({"field_name": field_name, "reason": "empty_attachment"})
                continue
            if not content.startswith(b"%PDF"):
                failures.append({"field_name": field_name, "reason": "attachment_not_pdf"})
                continue
            if failures:
                attempt["attachment_fallbacks"] = failures
            return content, attempt
        failure_text = " ".join(clean(row.get("message")) for row in failures).lower()
        reason = "authentication_failed" if any(token in failure_text for token in ("auth", "credential", "password", "401")) else "attachment_not_available"
        return b"", {**attempt, "accepted": False, "reason": reason, "attachment_fallbacks": failures}
    except Exception as exc:
        message = str(exc)[:500]
        lower = message.lower()
        reason = "authentication_failed" if any(token in lower for token in ("auth", "credential", "password", "401")) else "attachment_not_available" if "404" in lower else "openreview_client_error"
        return b"", {**attempt, "accepted": False, "reason": reason, "error_type": type(exc).__name__, "message": message}


def _validate_pdf_content(paper: dict[str, Any], content: bytes, target_dir: Path, attempt: dict[str, Any]) -> tuple[str, Path | None, dict[str, Any]]:
    if not content.startswith(b"%PDF"):
        return "", None, {**attempt, "accepted": False, "reason": "not_pdf"}
    pdf_path = target_dir / "paper.pdf"
    pdf_path.write_bytes(content)
    try:
        text = _pdf_text(pdf_path)
        body_ok = _body_ok(text)
        identity_ok = _identity_ok(paper, text)
        attempt.update({"text_chars": len(text), "body_ok": body_ok, "identity_ok": identity_ok})
        if body_ok and identity_ok:
            attempt["accepted"] = True
            return text, pdf_path, attempt
        attempt.update({"accepted": False, "reason": "full_text_or_identity_validation_failed"})
    except Exception as exc:
        attempt.update({"accepted": False, "reason": "pdf_parse_failed", "error_type": type(exc).__name__, "message": str(exc)[:500]})
    pdf_path.unlink(missing_ok=True)
    return "", None, attempt


def _candidate_urls(paper: dict[str, Any], *, include_oa_lookups: bool = True) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    candidates: list[dict[str, str]] = []
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: Any, kind: str) -> None:
        value = clean(url)
        if value.startswith("http") and value not in seen:
            seen.add(value)
            candidates.append({"url": value, "kind": kind})

    for candidate in official_pdf_candidates(paper):
        add(candidate["url"], candidate["kind"])
    if not include_oa_lookups:
        for page_url in (clean(paper.get("url")), clean((paper.get("metadata") or {}).get("url") if isinstance(paper.get("metadata"), dict) else "")):
            if "eccv.ecva.net/virtual/" not in page_url.lower():
                continue
            try:
                response = get(page_url, timeout=30)
                receipts.append({"kind": "eccv_virtual_pdf_scan", **receipt(response)})
                if not response.ok:
                    continue
                soup = BeautifulSoup(response.text, "html.parser")
                for anchor in soup.find_all("a", href=True):
                    absolute = urljoin(response.url, str(anchor.get("href") or ""))
                    lowered = absolute.lower()
                    if not lowered.endswith(".pdf") or any(token in lowered for token in ("-supp.pdf", "supplement", "poster", "slides")):
                        continue
                    add(absolute, "conference_official_pdf")
            except Exception as exc:
                receipts.append({"kind": "eccv_virtual_pdf_scan", "url": page_url, "status": "error", "error_type": type(exc).__name__, "message": str(exc)[:500]})
    add(paper.get("pdf_url"), "metadata_pdf")
    arxiv_id = _identifier(paper, "arxiv_id")
    if arxiv_id:
        add(f"https://arxiv.org/pdf/{arxiv_id}", "arxiv_pdf")
    openreview_id = _identifier(paper, "openreview_id")
    if openreview_id:
        add(f"https://openreview.net/pdf?id={openreview_id}", "openreview_pdf")
        if include_oa_lookups:
            try:
                response = get("https://chatpaper.com/search", params={"keywords": clean(paper.get("title"))}, timeout=20)
                receipts.append({"kind": "chatpaper_openreview_cache_search", **receipt(response)})
                article_ids: list[str] = []
                if response.ok:
                    for match in re.finditer(r'(?:data-doc=["\']|/(?:zh-CN/)?paper/)(\d+)', response.text, flags=re.I):
                        if match.group(1) not in article_ids:
                            article_ids.append(match.group(1))
                        if len(article_ids) >= 3:
                            break
                for article_id in article_ids:
                    page = get(f"https://chatpaper.com/paper/{article_id}", timeout=20)
                    note_ids = set(re.findall(r"openreview\.net/(?:forum\?id=|pdf\?id=)([A-Za-z0-9_-]+)", page.text if page.ok else "", flags=re.I))
                    matched = openreview_id in note_ids
                    receipts.append({
                        "kind": "chatpaper_openreview_cache_page",
                        "article_id": article_id,
                        "openreview_id_match": matched,
                        "candidate_note_id_count": len(note_ids),
                        **receipt(page),
                    })
                    if matched:
                        add(
                            f"https://chatpaper.com/api/v1/articles/download/{article_id}",
                            "chatpaper_openreview_cached_pdf_exact_note_id",
                        )
            except Exception as exc:
                receipts.append({
                    "kind": "chatpaper_openreview_cache_search",
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:500],
                })
    doi = _identifier(paper, "doi")
    if doi and include_oa_lookups:
        openalex_cooldown = cooldown_remaining("openalex")
        if openalex_cooldown > 0:
            receipts.append({"kind": "openalex_lookup", "status": "skipped_cooldown", "cooldown_remaining_seconds": round(openalex_cooldown, 3)})
        else:
            try:
                with bounded_request_policy(max_attempts=2, max_wait_seconds=5.0, wall_timeout_seconds=30.0):
                    response = get(
                        f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='')}",
                        params=_openalex_params(),
                    )
                receipts.append({"kind": "openalex_lookup", **receipt(response)})
                if response.ok:
                    payload = response.json()
                    best = payload.get("best_oa_location") if isinstance(payload.get("best_oa_location"), dict) else {}
                    primary = payload.get("primary_location") if isinstance(payload.get("primary_location"), dict) else {}
                    add(best.get("pdf_url"), "openalex_best_oa_pdf")
                    add(primary.get("pdf_url"), "openalex_primary_pdf")
            except Exception as exc:
                receipts.append({"kind": "openalex_lookup", "status": "error", "error_type": type(exc).__name__, "message": str(exc)[:500]})
        email = clean(os.environ.get("UNPAYWALL_EMAIL") or os.environ.get("RECOMMEND_PAPERS_CONTACT_EMAIL"))
        if email:
            unpaywall_cooldown = cooldown_remaining("unpaywall")
            if unpaywall_cooldown > 0:
                receipts.append({"kind": "unpaywall_lookup", "status": "skipped_cooldown", "cooldown_remaining_seconds": round(unpaywall_cooldown, 3)})
            else:
                try:
                    with bounded_request_policy(max_attempts=2, max_wait_seconds=5.0, wall_timeout_seconds=30.0):
                        response = get(f"https://api.unpaywall.org/v2/{quote(doi, safe='')}", params={"email": email})
                    receipts.append({"kind": "unpaywall_lookup", **receipt(response)})
                    if response.ok:
                        payload = response.json()
                        best = payload.get("best_oa_location") if isinstance(payload.get("best_oa_location"), dict) else {}
                        add(best.get("url_for_pdf"), "unpaywall_pdf")
                except Exception as exc:
                    receipts.append({"kind": "unpaywall_lookup", "status": "error", "error_type": type(exc).__name__, "message": str(exc)[:500]})
    title = clean(paper.get("title"))
    if include_oa_lookups and title:
        # Current-year conference papers often expose accepted metadata before
        # proceedings PDFs.  Exact-title OA lookup is therefore required even
        # when no DOI has been assigned yet.  Never accept a fuzzy title match.
        semantic_cooldown = cooldown_remaining("semantic_scholar")
        if semantic_cooldown > 0:
            receipts.append({"kind": "semantic_scholar_title_lookup", "status": "skipped_cooldown", "cooldown_remaining_seconds": round(semantic_cooldown, 3)})
        else:
            try:
                with bounded_request_policy(max_attempts=2, max_wait_seconds=5.0, wall_timeout_seconds=30.0):
                    response = get(
                        "https://api.semanticscholar.org/graph/v1/paper/search",
                        params={"query": title, "limit": 5, "fields": "title,openAccessPdf,externalIds"},
                        headers=_semantic_scholar_headers(),
                        timeout=30,
                    )
                receipts.append({"kind": "semantic_scholar_title_lookup", **receipt(response)})
                if response.ok:
                    for item in response.json().get("data") or []:
                        if _title_key(item.get("title")) != _title_key(title):
                            continue
                        oa = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
                        add(oa.get("url"), "semantic_scholar_exact_title_pdf")
                        external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
                        if clean(external.get("ArXiv")):
                            add(f"https://arxiv.org/pdf/{clean(external.get('ArXiv'))}", "semantic_scholar_arxiv_pdf")
                        break
            except Exception as exc:
                receipts.append({"kind": "semantic_scholar_title_lookup", "status": "error", "error_type": type(exc).__name__, "message": str(exc)[:500]})

        hal_cooldown = cooldown_remaining("hal")
        if hal_cooldown > 0:
            receipts.append({"kind": "hal_title_lookup", "status": "skipped_cooldown", "cooldown_remaining_seconds": round(hal_cooldown, 3)})
        else:
            try:
                with bounded_request_policy(max_attempts=2, max_wait_seconds=5.0, wall_timeout_seconds=30.0):
                    response = get(
                        "https://api.archives-ouvertes.fr/search/",
                        params={"q": f'title_t:"{title}"', "fl": "title_s,fileMain_s", "rows": 5, "wt": "json"},
                        timeout=30,
                    )
                receipts.append({"kind": "hal_title_lookup", **receipt(response)})
                if response.ok:
                    for item in ((response.json().get("response") or {}).get("docs") or []):
                        candidate_title = item.get("title_s")
                        if isinstance(candidate_title, list):
                            candidate_title = candidate_title[0] if candidate_title else ""
                        if _title_key(candidate_title) != _title_key(title):
                            continue
                        file_url = item.get("fileMain_s")
                        if isinstance(file_url, list):
                            file_url = file_url[0] if file_url else ""
                        add(file_url, "hal_exact_title_pdf")
                        break
            except Exception as exc:
                receipts.append({"kind": "hal_title_lookup", "status": "error", "error_type": type(exc).__name__, "message": str(exc)[:500]})

        arxiv_cooldown = cooldown_remaining("arxiv")
        if arxiv_cooldown > 0:
            receipts.append({"kind": "arxiv_title_lookup", "status": "skipped_cooldown", "cooldown_remaining_seconds": round(arxiv_cooldown, 3)})
        else:
            try:
                with bounded_request_policy(max_attempts=2, max_wait_seconds=6.0, wall_timeout_seconds=30.0):
                    response = get(
                        "https://export.arxiv.org/api/query",
                        params={"search_query": f'ti:"{title}"', "start": 0, "max_results": 5},
                        timeout=30,
                    )
                receipts.append({"kind": "arxiv_title_lookup", **receipt(response)})
                if response.ok:
                    namespace = {"a": "http://www.w3.org/2005/Atom"}
                    root = ET.fromstring(response.content)
                    for entry in root.findall("a:entry", namespace):
                        candidate_title = clean(entry.findtext("a:title", default="", namespaces=namespace))
                        if _title_key(candidate_title) != _title_key(title):
                            continue
                        arxiv_url = clean(entry.findtext("a:id", default="", namespaces=namespace))
                        if arxiv_url:
                            add(arxiv_url.replace("/abs/", "/pdf/"), "arxiv_exact_title_pdf")
                        break
            except Exception as exc:
                receipts.append({"kind": "arxiv_title_lookup", "status": "error", "error_type": type(exc).__name__, "message": str(exc)[:500]})
    return candidates, receipts


def _download_pdf(paper: dict[str, Any], target_dir: Path) -> tuple[str, Path | None, list[dict[str, Any]]]:
    # Derive and try supplied/official locators before making any optional OA
    # index request.  A slow or rate-limited discovery service must not delay a
    # PDF that is already available from the conference.
    candidates, attempts = _candidate_urls(paper, include_oa_lookups=False)
    if _identifier(paper, "openreview_id"):
        content, attempt = _authenticated_openreview_pdf(paper)
        if content:
            text, pdf_path, attempt = _validate_pdf_content(paper, content, target_dir, attempt)
            attempts.append(attempt)
            if text:
                return text, pdf_path, attempts
        else:
            attempts.append(attempt)
    attempted_urls: set[str] = set()

    def try_candidates(items: list[dict[str, str]]) -> tuple[str, Path | None]:
        for candidate in items:
            url = candidate["url"]
            if url in attempted_urls:
                continue
            attempted_urls.add(url)
            attempt: dict[str, Any] = {"kind": candidate["kind"], "url": url}
            service = service_for(url)
            remaining = cooldown_remaining(service)
            if remaining > 0:
                attempt.update({
                    "accepted": False,
                    "status": "skipped_persisted_cooldown",
                    "service": service,
                    "cooldown_remaining_seconds": round(remaining, 3),
                })
                attempts.append(attempt)
                continue
            try:
                response = get(url, timeout=45)
                attempt.update(receipt(response))
                content = response.content
                if not response.ok:
                    attempt["accepted"] = False
                    if response.status_code == 403 and "openreview.net" in url:
                        attempt["reason"] = "challenge_403"
                elif not content.startswith(b"%PDF"):
                    attempt.update({"accepted": False, "reason": "not_pdf"})
                else:
                    text, pdf_path, attempt = _validate_pdf_content(paper, content, target_dir, attempt)
                    if text:
                        attempts.append(attempt)
                        return text, pdf_path
            except Exception as exc:
                attempt.update({"accepted": False, "error_type": type(exc).__name__, "message": str(exc)[:500]})
            attempts.append(attempt)
        return "", None

    text, pdf_path = try_candidates(candidates)
    if text:
        return text, pdf_path, attempts

    fallback_candidates, lookup_receipts = _candidate_urls(paper, include_oa_lookups=True)
    attempts.extend(lookup_receipts)
    text, pdf_path = try_candidates(fallback_candidates)
    if text:
        return text, pdf_path, attempts
    return "", None, attempts


def _html_text(url: str) -> tuple[str, dict[str, Any]]:
    response = get(url, timeout=30)
    result = {"kind": "same_paper_html", **receipt(response)}
    if not response.ok or "html" not in str(response.headers.get("Content-Type") or "").lower():
        return "", {**result, "accepted": False}
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    article = soup.find("article") or soup.find("main") or soup.body
    text = "\n".join(line.strip() for line in (article.get_text("\n") if article else "").splitlines() if line.strip())
    result.update({"text_chars": len(text), "body_ok": _body_ok(text), "accepted": _body_ok(text)})
    return text if result["accepted"] else "", result


def _pmc_xml(paper: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    doi = _identifier(paper, "doi")
    if not doi:
        return "", []
    remaining = cooldown_remaining("europepmc")
    if remaining > 0:
        return "", [{"kind": "europepmc", "status": "skipped_cooldown", "cooldown_remaining_seconds": round(remaining, 3)}]
    attempts = []
    try:
        response = get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={"query": f'DOI:"{doi}"', "format": "json", "pageSize": 5})
        attempts.append({"kind": "europepmc_search", **receipt(response)})
        if not response.ok:
            return "", attempts
        results = (((response.json().get("resultList") or {}).get("result")) or [])
        for item in results:
            pmcid = clean(item.get("pmcid"))
            if not pmcid:
                continue
            xml_response = get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML")
            attempt = {"kind": "europepmc_fulltext_xml", "pmcid": pmcid, **receipt(xml_response)}
            if xml_response.ok:
                root = ET.fromstring(xml_response.content)
                text = "\n".join(clean(value) for value in root.itertext() if clean(value))
                attempt.update({"text_chars": len(text), "body_ok": _body_ok(text), "identity_ok": _identity_ok(paper, text)})
                if _body_ok(text) and _identity_ok(paper, text):
                    attempt["accepted"] = True
                    attempts.append(attempt)
                    return text, attempts
            attempt["accepted"] = False
            attempts.append(attempt)
    except Exception as exc:
        attempts.append({"kind": "europepmc", "status": "error", "error_type": type(exc).__name__, "message": str(exc)[:500]})
    return "", attempts


def _acquire_with_bounded_requests(paper: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    work_dir = safe_write_target(work_dir)
    downloads = work_dir / "downloads"
    extracted = work_dir / "extracted"
    downloads.mkdir(parents=True, exist_ok=True)
    extracted.mkdir(parents=True, exist_ok=True)
    text, pdf_path, pdf_attempts = _download_pdf(paper, downloads)
    text_kind = "pdf" if text else ""
    html_attempts = []
    xml_attempts = []
    if not text:
        for url in (clean(paper.get("url")),):
            if not url.startswith("http"):
                continue
            service = "acm" if re.search(r"doi\.org/10\.1145(?:/|%2f)", url, re.I) else service_for(url)
            remaining = cooldown_remaining(service)
            if remaining > 0:
                html_attempts.append({
                    "kind": "same_paper_html",
                    "url": url,
                    "accepted": False,
                    "status": "skipped_persisted_cooldown",
                    "service": service,
                    "cooldown_remaining_seconds": round(remaining, 3),
                })
                continue
            try:
                candidate, attempt = _html_text(url)
            except Exception as exc:
                candidate, attempt = "", {"kind": "same_paper_html", "url": url, "accepted": False, "error_type": type(exc).__name__, "message": str(exc)[:500]}
            html_attempts.append(attempt)
            if candidate and _identity_ok(paper, candidate):
                text, text_kind = candidate, "html"
                break
    if not text:
        candidate, xml_attempts = _pmc_xml(paper)
        if candidate:
            text, text_kind = candidate, "xml"
    full_text_available = bool(text and _body_ok(text) and _identity_ok(paper, text))
    text_path: Path | None = None
    if full_text_available:
        text_path = extracted / ("full_text.txt" if text_kind == "pdf" else f"full_text_{text_kind}.txt")
        write_text(text_path, text.rstrip() + "\n")
    transient = _transient_acquisition(pdf_attempts, html_attempts, xml_attempts)
    return {
        "schema_version": ACQUISITION_SCHEMA_VERSION,
        "identity": paper_identity(paper),
        "paper": paper,
        "status": "ready" if full_text_available else "temporarily_unavailable" if transient["retryable_after_cooldown"] else "unavailable",
        "full_text_available": full_text_available,
        **({
            "retryable_after_cooldown": True,
            "cooldown_services": transient["cooldown_services"],
            "temporary_failure_reasons": transient["reasons"],
        } if not full_text_available and transient["retryable_after_cooldown"] else {}),
        "text_kind": text_kind if full_text_available else "",
        "text_chars": len(text) if full_text_available else 0,
        "text_path": str(text_path) if text_path else "",
        "pdf_path": str(pdf_path) if pdf_path and pdf_path.is_file() else "",
        "attempts": {"pdf": pdf_attempts, "html": html_attempts, "xml": xml_attempts},
        "completed_at": now_iso(),
    }


def acquire(paper: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    """Acquire one paper without allowing a server cooldown to pin a worker indefinitely."""
    try:
        wait_budget = max(0.0, float(os.environ.get("RECOMMEND_PAPERS_FULLTEXT_RETRY_AFTER_WAIT_CAP_SECONDS", "120") or 120))
    except ValueError:
        wait_budget = 120.0
    try:
        wall_budget = max(30.0, float(os.environ.get("RECOMMEND_PAPERS_FULLTEXT_REQUEST_WALL_BUDGET_SECONDS", "600") or 600))
    except ValueError:
        wall_budget = 600.0
    with bounded_request_policy(max_attempts=5, max_wait_seconds=wait_budget, wall_timeout_seconds=wall_budget):
        return _acquire_with_bounded_requests(paper, work_dir)


def _transient_acquisition(*groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify throttling/challenge failures without calling a paper absent."""
    services: set[str] = set()
    reasons: set[str] = set()

    def inspect(item: Any, inherited_openreview: bool = False) -> None:
        if isinstance(item, list):
            for child in item:
                inspect(child, inherited_openreview)
            return
        if not isinstance(item, dict):
            return
        url = clean(item.get("url"))
        kind = clean(item.get("kind")).lower()
        reason = clean(item.get("reason") or item.get("status")).lower()
        error_type = clean(item.get("error_type")).lower()
        message = clean(item.get("message")).lower()
        status_code = int(item.get("status_code") or 0)
        is_openreview = inherited_openreview or "openreview" in kind or "openreview.net" in url
        detected_service = ""
        for known in ("openreview", "arxiv", "biorxiv", "openalex", "semantic_scholar", "hal", "europepmc", "unpaywall", "acm"):
            if known in kind:
                detected_service = known
                break
        if is_openreview:
            detected_service = "openreview"
        elif url:
            detected_service = service_for(url)
        retry_rows = item.get("retry_history") if isinstance(item.get("retry_history"), list) else []
        retry_codes = {int(row.get("status_code") or 0) for row in retry_rows if isinstance(row, dict)}
        if status_code == 429 or 429 in retry_codes or "rate" in reason and "limit" in reason:
            services.add(detected_service)
            reasons.add("http_429_rate_limited")
        if status_code == 403 or reason in {"challenge_403", "http_403"} or "challenge" in reason:
            services.add(detected_service)
            reasons.add("openreview_403_challenge" if is_openreview else "http_403_access_challenge")
        if reason in {"skipped_cooldown", "service_cooldown_active", "openreview_service_cooldown_active"}:
            services.add(detected_service)
            reasons.add("service_cooldown_active")
        if error_type == "servicerequestdeferred" or "request deferred" in message or "cooldown" in message and "deferred" in message:
            services.add(detected_service)
            reasons.add("service_cooldown_active")
        for value in item.values():
            if isinstance(value, (dict, list)):
                inspect(value, is_openreview)

    for group in groups:
        inspect(group)
    services.discard("")
    return {
        "retryable_after_cooldown": bool(reasons),
        "cooldown_services": sorted(services),
        "reasons": sorted(reasons),
    }


def cache_key(paper: dict[str, Any]) -> str:
    return stable_hash({"schema": ACQUISITION_SCHEMA_VERSION, "identity": paper_identity(paper)})


def _cache_aliases(paper: dict[str, Any]) -> list[str]:
    identifiers = paper.get("identifiers") if isinstance(paper.get("identifiers"), dict) else {}
    aliases = []
    for name in ("doi", "arxiv_id", "openreview_id", "pmcid"):
        value = clean(identifiers.get(name) or paper.get(name)).lower()
        if value:
            aliases.append(f"{name}:{value}")
    for name in ("url", "pdf_url"):
        value = clean(paper.get(name))
        if value:
            aliases.append(f"{name}:{value}")
    title = re.sub(r"[^a-z0-9]+", "", clean(paper.get("title")).lower())
    authors = paper.get("authors") if isinstance(paper.get("authors"), list) else []
    first_author = re.sub(r"[^a-z0-9]+", "", clean(authors[0] if authors else "").lower())
    year = clean(paper.get("year") or str(paper.get("published") or "")[:4])
    if title:
        aliases.append(f"title:{title}|author:{first_author}|year:{year}")
    return list(dict.fromkeys(aliases))


def _alias_path(alias: str) -> Path:
    digest = stable_hash({"alias": alias})
    return FULLTEXT_CACHE_ROOT / "_aliases" / digest[:2] / f"{digest}.json"


def _cached_from_directory(paper: dict[str, Any], directory: Path) -> dict[str, Any] | None:
    manifest = directory / "acquisition.json"
    payload = read_json(manifest, {})
    if not isinstance(payload, dict) or payload.get("schema_version") != ACQUISITION_SCHEMA_VERSION or payload.get("full_text_available") is not True:
        return None
    cached_paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
    wanted_aliases = set(_cache_aliases(paper))
    cached_aliases = set(_cache_aliases(cached_paper))
    for prefix in ("doi:", "arxiv_id:", "openreview_id:", "pmcid:"):
        wanted_exact = {alias for alias in wanted_aliases if alias.startswith(prefix)}
        cached_exact = {alias for alias in cached_aliases if alias.startswith(prefix)}
        if wanted_exact and cached_exact and wanted_exact.isdisjoint(cached_exact):
            return None
    if not wanted_aliases & cached_aliases:
        return None
    text_path = Path(str(payload.get("text_path") or "")).expanduser()
    if not text_path.is_file() or text_path.stat().st_size < MIN_FULL_TEXT_CHARS:
        return None
    payload["cache_status"] = "hit"
    return payload


def cached(paper: dict[str, Any]) -> dict[str, Any] | None:
    direct = _cached_from_directory(paper, FULLTEXT_CACHE_ROOT / cache_key(paper))
    if direct:
        return direct
    for alias in _cache_aliases(paper):
        mapping = read_json(_alias_path(alias), {})
        cache_dir = Path(clean(mapping.get("cache_dir"))).expanduser() if isinstance(mapping, dict) else Path()
        if str(cache_dir) != ".":
            hit = _cached_from_directory(paper, cache_dir)
            if hit:
                hit["cache_alias"] = alias
                return hit
    return None


def _rewrite_root(value: Any, source: Path, destination: Path) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_root(item, source, destination) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_root(item, source, destination) for item in value]
    if isinstance(value, str):
        old = str(source.resolve(strict=False))
        if value == old or value.startswith(old + os.sep):
            return str(destination.resolve(strict=False)) + value[len(old):]
    return value


def publish_cache(paper: dict[str, Any], work_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("full_text_available") is not True:
        return result
    destination = safe_write_target(FULLTEXT_CACHE_ROOT / cache_key(paper))
    with _cache_publish_lock:
        existing = cached(paper)
        if existing:
            return existing
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=destination.name + ".", dir=destination.parent))
        try:
            for child in work_dir.iterdir():
                target = temporary / child.name
                shutil.copytree(child, target) if child.is_dir() else shutil.copy2(child, target)
            cached_result = _rewrite_root(result, work_dir, destination)
            write_json(temporary / "acquisition.json", cached_result)
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(temporary, destination)
            for alias in _cache_aliases(paper):
                write_json(_alias_path(alias), {"alias": alias, "cache_dir": str(destination), "updated_at": now_iso()})
            cached_result["cache_status"] = "miss_published"
            return cached_result
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def acquire_many(papers: list[dict[str, Any]], run_dir: Path, workers: int) -> dict[str, Any]:
    run_dir = safe_write_target(run_dir)
    input_dir = run_dir / "papers"
    input_dir.mkdir(parents=True, exist_ok=True)
    update_run(run_dir, stage="fulltext_acquisition", counts={"requested": len(papers), "completed": 0, "ready": 0})

    def one(pair: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        index, paper = pair
        identity = paper_identity(paper)
        paper_dir = input_dir / f"{index:04d}"
        receipt_path = paper_dir / "acquisition.json"
        prior = read_json(receipt_path, {})
        if isinstance(prior, dict) and prior.get("schema_version") == ACQUISITION_SCHEMA_VERSION and prior.get("identity") == identity and prior.get("full_text_available") is True:
            prior["index"] = index
            prior["cache_status"] = "run_resume"
            return prior
        hit = cached(paper)
        if hit:
            hit["index"] = index
            write_json(receipt_path, hit)
            return hit
        paper_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = acquire(paper, paper_dir)
            result = publish_cache(paper, paper_dir, result)
        except Exception as exc:
            result = {
                "schema_version": ACQUISITION_SCHEMA_VERSION,
                "identity": identity,
                "paper": paper,
                "status": "error",
                "full_text_available": False,
                "error_type": type(exc).__name__,
                "message": str(exc)[:1000],
                "completed_at": now_iso(),
                "cache_status": "miss",
            }
        result["index"] = index
        write_json(receipt_path, result)
        return result

    results: list[dict[str, Any]] = []
    worker_count = max(1, min(16, int(workers or 1), len(papers)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(one, pair): pair[0] for pair in enumerate(papers, 1)}
        for future in as_completed(futures):
            results.append(future.result())
            ready = sum(item.get("full_text_available") is True for item in results)
            update_run(run_dir, stage="fulltext_acquisition", counts={"requested": len(papers), "completed": len(results), "ready": ready})
    results.sort(key=lambda item: int(item["index"]))

    # Match TASTE Reading's cooldown-expiry recovery: throttled/challenged
    # papers are not permanent gaps.  Wait once for the shared service cooldown
    # and retry them serially so the recovery itself cannot cause another burst.
    retryable = [item for item in results if item.get("full_text_available") is not True and item.get("retryable_after_cooldown") is True]
    recovery = {"eligible_count": len(retryable), "attempted_count": 0, "recovered_count": 0, "worker_count": 1, "waited_seconds": 0.0}
    if retryable:
        services = sorted({service for item in retryable for service in item.get("cooldown_services") or []})
        remaining = max((cooldown_remaining(service) for service in services), default=0.0)
        try:
            wait_cap = max(0.0, float(os.environ.get("RECOMMEND_PAPERS_COOLDOWN_REQUEUE_WAIT_CAP_SECONDS", "180") or 180))
        except (TypeError, ValueError):
            wait_cap = 180.0
        recovery.update({"services": services, "cooldown_remaining_seconds": round(remaining, 3), "wait_cap_seconds": wait_cap})
        if remaining <= wait_cap:
            if remaining > 0:
                time.sleep(remaining)
                recovery["waited_seconds"] = round(remaining, 3)
            by_index = {int(item["index"]): item for item in results}
            for initial in retryable:
                index = int(initial["index"])
                retried = one((index, papers[index - 1]))
                recovery["attempted_count"] += 1
                retried["cooldown_requeue"] = {
                    "attempted": True,
                    "initial_status": initial.get("status"),
                    "initial_reasons": initial.get("temporary_failure_reasons") or [],
                }
                if retried.get("full_text_available") is True:
                    recovery["recovered_count"] += 1
                by_index[index] = retried
            results = [by_index[index] for index in sorted(by_index)]
        else:
            recovery["skipped_reason"] = "cooldown_exceeds_wait_cap"
    ready = sum(item.get("full_text_available") is True for item in results)
    payload = {
        "schema_version": ACQUISITION_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "status": "complete" if ready == len(results) else "complete_with_gaps",
        "requested_count": len(results),
        "full_text_ready_count": ready,
        "worker_count": worker_count,
        "channel_concurrency_policy": "independent_service_slots",
        "cooldown_requeue": recovery,
        "items": results,
        "completed_at": now_iso(),
    }
    return payload
