from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

import fitz
from bs4 import BeautifulSoup

from .credentials import openreview_settings
from .http import get, receipt
from .metadata import clean, paper_identity
from .storage import FULLTEXT_CACHE_ROOT, now_iso, read_json, safe_write_target, stable_hash, update_run, write_json, write_text


MIN_FULL_TEXT_CHARS = 1200
_cache_publish_lock = threading.Lock()
_openreview_clients = threading.local()


def _title_tokens(value: Any) -> set[str]:
    stop = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "with", "via", "using"}
    return {token.lower() for token in re.findall(r"[A-Za-z0-9]+", clean(value)) if len(token) >= 3 and token.lower() not in stop}


def _identity_ok(paper: dict[str, Any], text: str) -> bool:
    expected = _title_tokens(paper.get("title"))
    if not expected:
        return False
    observed = _title_tokens(text[:20000])
    overlap = len(expected & observed) / max(1, len(expected))
    authors = paper.get("authors") if isinstance(paper.get("authors"), list) else []
    author_tokens = {token.lower() for author in authors[:3] for token in re.findall(r"[A-Za-z]{3,}", clean(author))}
    return overlap >= 0.55 or (overlap >= 0.4 and bool(author_tokens & observed))


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


def _openreview_client():
    import openreview

    settings = openreview_settings()
    client = getattr(_openreview_clients, "client", None)
    identity = (settings["username"], str(settings["env_file"]))
    if client is None or getattr(_openreview_clients, "identity", None) != identity:
        client = openreview.api.OpenReviewClient(
            baseurl="https://api2.openreview.net",
            username=settings["username"] or None,
            password=settings["password"] or None,
        )
        _openreview_clients.client = client
        _openreview_clients.identity = identity
    return client, settings


def _authenticated_openreview_pdf(paper: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    note_id = _identifier(paper, "openreview_id")
    attempt: dict[str, Any] = {"kind": "openreview_authenticated_attachment", "openreview_id": note_id}
    if not note_id:
        return b"", {**attempt, "accepted": False, "reason": "openreview_id_missing"}
    try:
        client, settings = _openreview_client()
        content = client.get_attachment(field_name="pdf", id=note_id)
        attempt.update({"authenticated": settings["authenticated"], "content_length": len(content or b"")})
        if not content:
            return b"", {**attempt, "accepted": False, "reason": "attachment_not_available"}
        if not content.startswith(b"%PDF"):
            return b"", {**attempt, "accepted": False, "reason": "attachment_not_pdf"}
        return content, attempt
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


def _candidate_urls(paper: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    candidates: list[dict[str, str]] = []
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: Any, kind: str) -> None:
        value = clean(url)
        if value.startswith("http") and value not in seen:
            seen.add(value)
            candidates.append({"url": value, "kind": kind})

    add(paper.get("pdf_url"), "metadata_pdf")
    arxiv_id = _identifier(paper, "arxiv_id")
    if arxiv_id:
        add(f"https://arxiv.org/pdf/{arxiv_id}", "arxiv_pdf")
    openreview_id = _identifier(paper, "openreview_id")
    if openreview_id:
        add(f"https://openreview.net/pdf?id={openreview_id}", "openreview_pdf")
    doi = _identifier(paper, "doi")
    if doi:
        try:
            response = get(f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='')}")
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
            try:
                response = get(f"https://api.unpaywall.org/v2/{quote(doi, safe='')}", params={"email": email})
                receipts.append({"kind": "unpaywall_lookup", **receipt(response)})
                if response.ok:
                    payload = response.json()
                    best = payload.get("best_oa_location") if isinstance(payload.get("best_oa_location"), dict) else {}
                    add(best.get("url_for_pdf"), "unpaywall_pdf")
            except Exception as exc:
                receipts.append({"kind": "unpaywall_lookup", "status": "error", "error_type": type(exc).__name__, "message": str(exc)[:500]})
    return candidates, receipts


def _download_pdf(paper: dict[str, Any], target_dir: Path) -> tuple[str, Path | None, list[dict[str, Any]]]:
    candidates, attempts = _candidate_urls(paper)
    if _identifier(paper, "openreview_id"):
        content, attempt = _authenticated_openreview_pdf(paper)
        if content:
            text, pdf_path, attempt = _validate_pdf_content(paper, content, target_dir, attempt)
            attempts.append(attempt)
            if text:
                return text, pdf_path, attempts
        else:
            attempts.append(attempt)
    for candidate in candidates:
        url = candidate["url"]
        attempt: dict[str, Any] = {"kind": candidate["kind"], "url": url}
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
                    return text, pdf_path, attempts
        except Exception as exc:
            attempt.update({"accepted": False, "error_type": type(exc).__name__, "message": str(exc)[:500]})
        attempts.append(attempt)
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


def acquire(paper: dict[str, Any], work_dir: Path) -> dict[str, Any]:
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
    return {
        "schema_version": 1,
        "identity": paper_identity(paper),
        "paper": paper,
        "status": "ready" if full_text_available else "unavailable",
        "full_text_available": full_text_available,
        "text_kind": text_kind if full_text_available else "",
        "text_chars": len(text) if full_text_available else 0,
        "text_path": str(text_path) if text_path else "",
        "pdf_path": str(pdf_path) if pdf_path and pdf_path.is_file() else "",
        "attempts": {"pdf": pdf_attempts, "html": html_attempts, "xml": xml_attempts},
        "completed_at": now_iso(),
    }


def cache_key(paper: dict[str, Any]) -> str:
    return stable_hash({"schema": 1, "identity": paper_identity(paper)})


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
    if not isinstance(payload, dict) or payload.get("full_text_available") is not True:
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
        if isinstance(prior, dict) and prior.get("identity") == identity and prior.get("full_text_available") is True:
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
                "schema_version": 1,
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
    ready = sum(item.get("full_text_available") is True for item in results)
    payload = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "status": "complete" if ready == len(results) else "complete_with_gaps",
        "requested_count": len(results),
        "full_text_ready_count": ready,
        "worker_count": worker_count,
        "items": results,
        "completed_at": now_iso(),
    }
    write_json(run_dir / "full_text_results.json", payload)
    update_run(run_dir, stage="fulltext_complete", status="active", counts={"requested": len(results), "completed": len(results), "ready": ready}, warnings=[] if ready == len(results) else [f"Full text unavailable for {len(results) - ready} papers"])
    return payload
