from __future__ import annotations

"""Official conference metadata adapters migrated from TASTE Finding.

The important contract is deliberately strict: a returned conference corpus is the
complete official title pool for one year and every row has an abstract.  Index-only
records are enriched from their official detail page (or, for ACM proceedings, from
the same indexed enrichment sources used by TASTE) before the corpus is accepted.
"""

import hashlib
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
import fitz

from .http import cooldown_remaining, get, post, receipt


def clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def title_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean(value).lower())


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
    value = _meta(soup, "citation_abstract", "dc.description", "description", "og:description")
    if value and len(value) >= 80:
        return re.sub(r"^abstract\s*[:—-]?\s*", "", value, flags=re.I)
    selectors = (
        "#abstract", ".abstract", ".abstractInFull", "section.abstract", "div.abstract",
        "[class*='abstract']", "[id*='abstract']", "div[itemprop='description']",
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


def fetch_neurips_official(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    year = int(spec["years"][0])
    list_urls = [
        f"https://proceedings.neurips.cc/paper_files/paper/{year}",
        f"https://papers.nips.cc/paper_files/paper/{year}",
    ]
    response = None
    for list_url in list_urls:
        try:
            response = _response(list_url, timeout=90)
            break
        except Exception:
            continue
    if response is None:
        raise RuntimeError(f"NeurIPS official proceedings index unavailable for {year}")
    soup = BeautifulSoup(response.text, "html.parser")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        title = clean(anchor.get_text(" ", strip=True))
        if not looks_like_title(title) or "-Abstract-" not in href or not href.endswith(".html"):
            continue
        detail_url = urljoin(response.url, href)
        if detail_url in seen:
            continue
        seen.add(detail_url)
        rows.append({"title": title, "abstract": "", "authors": [], "published": f"{year}-01-01", "year": year, "url": detail_url, "pdf_url": "", "venue": "NeurIPS", "categories": [], "identifiers": {}, "metadata": {"official_index": response.url}})
    _enrich_all(rows)
    return _result(rows, adapter="neurips_official_papers", requests=[receipt(response)], proof="official_neurips_proceedings_index_exhausted_and_all_details_enriched")


def fetch_icml_official(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    year = int(spec["years"][0])
    list_url = f"https://icml.cc/virtual/{year}/papers.html"
    response = _response(list_url, timeout=90)
    soup = BeautifulSoup(response.text, "html.parser")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        title = clean(anchor.get_text(" ", strip=True))
        detail_url = urljoin(list_url, href)
        if not looks_like_title(title) or not any(marker in href for marker in ("/poster/", "/oral/", "/paper/", "/spotlight/")) or detail_url in seen:
            continue
        seen.add(detail_url)
        presentation = next((label for marker, label in (("/oral/", "oral"), ("/spotlight/", "spotlight"), ("/poster/", "poster")) if marker in href), "paper")
        rows.append({"title": title, "abstract": "", "authors": [], "published": f"{year}-01-01", "year": year, "url": detail_url, "pdf_url": "", "venue": "ICML", "categories": [presentation], "presentation_type": presentation, "identifiers": {}, "metadata": {"official_index": list_url}})
    _enrich_all(rows)
    return _result(rows, adapter="icml_official_virtual", requests=[receipt(response)], proof="official_icml_virtual_index_exhausted_and_all_details_enriched")


def fetch_cvf(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    venue = clean(spec.get("venue") or spec.get("name") or spec.get("venue_id")).upper()
    year = int(spec["years"][0])
    list_url = f"https://openaccess.thecvf.com/{venue}{year}?day=all"
    response = _response(list_url)
    soup = BeautifulSoup(response.text, "html.parser")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in soup.select("dt.ptitle a[href], dt a[href]"):
        title = clean(node.get_text(" ", strip=True))
        detail_url = urljoin(list_url, str(node.get("href") or ""))
        if not looks_like_title(title) or detail_url in seen:
            continue
        seen.add(detail_url)
        parent = node.find_parent("dt")
        authors: list[str] = []
        pdf_url = ""
        for sibling in parent.find_next_siblings(["dd", "dt"]) if parent else []:
            if sibling.name == "dt":
                break
            if not authors:
                authors = [clean(a.get_text(" ", strip=True)) for a in sibling.select("form.authsearch a, a[onclick*='authsearch']") if clean(a.get_text(" ", strip=True))]
            for anchor in sibling.find_all("a", href=True):
                if re.search(r"\.pdf(?:$|[?#])", str(anchor.get("href")), re.I):
                    pdf_url = urljoin(list_url, str(anchor.get("href")))
                    break
        rows.append({"title": title, "abstract": "", "authors": authors, "published": f"{year}-01-01", "year": year, "url": detail_url, "pdf_url": pdf_url, "venue": venue, "categories": [], "identifiers": {}, "metadata": {"official_index": list_url}})
    _enrich_all(rows)
    return _result(rows, adapter="cvf_openaccess", requests=[receipt(response)], proof="official_cvf_index_exhausted_and_all_details_enriched")


def fetch_acl_anthology(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    venue = clean(spec.get("venue") or spec.get("name") or spec.get("venue_id")).upper()
    collection = "emnlp" if venue == "EMNLP" else "acl"
    year = int(spec["years"][0])
    sources = [(f"https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{year}.{collection}.xml", "")]
    if collection == "emnlp":
        sources.append((f"https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{year}.findings.xml", "emnlp"))
    rows: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_url, wanted_volume in sources:
        response = get(source_url, timeout=90)
        requests.append(receipt(response))
        if response.status_code == 404:
            continue
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for volume in root.findall("volume"):
            if wanted_volume and clean(volume.get("id")).lower() != wanted_volume:
                continue
            for node in volume.findall("paper"):
                title_node = node.find("title")
                title = clean("".join(title_node.itertext()) if title_node is not None else "")
                if not looks_like_title(title):
                    continue
                anthology_id = clean(node.findtext("url")) or f"{year}.{collection}-{volume.get('id')}.{node.get('id')}"
                paper_url = f"https://aclanthology.org/{anthology_id.strip('/')}/"
                if paper_url in seen:
                    continue
                seen.add(paper_url)
                authors = []
                for author in node.findall("author"):
                    parts = [clean("".join(part.itertext())) for key in ("first", "middle", "last") for part in author.findall(key) if clean("".join(part.itertext()))]
                    if parts:
                        authors.append(" ".join(parts))
                abstract_node = node.find("abstract")
                abstract = clean("".join(abstract_node.itertext()) if abstract_node is not None else "")
                rows.append({"title": title, "abstract": abstract, "authors": authors, "published": f"{year}-01-01", "year": year, "url": paper_url, "pdf_url": paper_url.rstrip("/") + ".pdf", "venue": venue, "categories": [], "identifiers": {"doi": clean(node.findtext("doi")), "acl_anthology_id": anthology_id}, "metadata": {"official_xml": source_url}})
    _enrich_all(rows)
    return _result(rows, adapter="acl_anthology", requests=requests, proof="official_acl_anthology_xml_exhausted_and_all_abstracts_present")


def _parse_ijcai_accepted(html_text: str, *, year: int, page_url: str) -> list[dict[str, Any]]:
    """Parse the current-year IJCAI accepted-paper feed.

    IJCAI publishes this page before the numbered proceedings directory exists.
    It is therefore a first-class live metadata channel, not evidence that the
    year is unavailable.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in soup.select(".ij-paper"):
        title_node = node.select_one(".ij-ptitle")
        title = clean(title_node.get_text(" ", strip=True) if title_node else "")
        if not looks_like_title(title) or title_key(title) in seen:
            continue
        seen.add(title_key(title))
        paper_id_node = node.select_one(".ij-pid")
        paper_id = clean(paper_id_node.get_text(" ", strip=True) if paper_id_node else "").lstrip("#")
        authors = [clean(author.get_text(" ", strip=True)) for author in node.select(".ij-author")]
        if len(authors) == 1:
            authors = [clean(part) for part in re.split(r"\s*(?:,|;|\band\b)\s*", authors[0]) if clean(part)]
        abstract_node = node.select_one(".ij-abstract")
        abstract = clean(abstract_node.get_text(" ", strip=True) if abstract_node else "")
        categories = []
        for category in node.select(".ij-kw"):
            value = clean(category.get_text(" ", strip=True))
            if value and value not in categories:
                categories.append(value)
        anchor = f"#paper-{paper_id}" if paper_id else ""
        rows.append({
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "published": f"{year}-01-01",
            "year": year,
            "url": page_url + anchor,
            "pdf_url": "",
            "venue": "IJCAI",
            "categories": categories,
            "identifiers": {"ijcai_paper_id": paper_id},
            "metadata": {"official_accepted_papers_page": page_url},
        })
    return rows


def fetch_ijcai(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    year = int(spec["years"][0])
    list_url = f"https://www.ijcai.org/proceedings/{year}/"
    requests: list[dict[str, Any]] = []
    response = get(list_url, timeout=60)
    requests.append(receipt(response))
    soup = BeautifulSoup(response.text, "html.parser") if response.ok else BeautifulSoup("", "html.parser")
    rows = []
    for wrapper in soup.select("div.paper_wrapper"):
        title_node = wrapper.select_one(".title")
        title = clean(title_node.get_text(" ", strip=True) if title_node else "")
        if not looks_like_title(title):
            continue
        authors_node = wrapper.select_one(".authors")
        detail = wrapper.select_one("a[href*='/proceedings/'][href]")
        pdf = wrapper.find("a", string=re.compile("pdf", re.I))
        detail_url = urljoin(list_url, str(detail.get("href"))) if detail else ""
        rows.append({"title": title, "abstract": "", "authors": [clean(authors_node.get_text(" ", strip=True))] if authors_node else [], "published": f"{year}-01-01", "year": year, "url": detail_url, "pdf_url": urljoin(list_url, str(pdf.get("href"))) if pdf else "", "venue": "IJCAI", "categories": [], "identifiers": {}, "metadata": {"official_index": list_url}})
    if rows:
        _enrich_all(rows)
        return _result(rows, adapter="ijcai_proceedings", requests=requests, proof="official_ijcai_index_exhausted_and_all_details_enriched")

    accepted_url = f"https://{year}.ijcai.org/accepted-papers/"
    accepted_response = _response(accepted_url)
    requests.append(receipt(accepted_response))
    rows = _parse_ijcai_accepted(accepted_response.text, year=year, page_url=accepted_url)
    for row in rows:
        if not clean(row.get("abstract")):
            _indexed_enrich(row)
    return _result(rows, adapter="ijcai_accepted_papers", requests=requests, proof="official_ijcai_accepted_papers_page_exhausted")


def fetch_eccv(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    year = int(spec["years"][0])
    if year % 2:
        raise ValueError("ECCV is held in even-numbered years")
    list_url = f"https://eccv.ecva.net/virtual/{year}/papers.html"
    response = _response(list_url)
    soup = BeautifulSoup(response.text, "html.parser")
    rows = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        title = clean(anchor.get_text(" ", strip=True))
        url = urljoin(list_url, href)
        if not looks_like_title(title) or not ("/poster/" in href or "/paper/" in href) or url in seen:
            continue
        seen.add(url)
        rows.append({"title": title, "abstract": "", "authors": [], "published": f"{year}-01-01", "year": year, "url": url, "pdf_url": "", "venue": "ECCV", "categories": [], "identifiers": {}, "metadata": {"official_index": list_url}})
    _enrich_all(rows)
    return _result(rows, adapter="eccv_virtual", requests=[receipt(response)], proof="official_ecva_index_exhausted_and_all_details_enriched")


def _aaai_issue_links(year: int) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    archive = "https://ojs.aaai.org/index.php/AAAI/issue/archive"
    links: list[tuple[str, str]] = []
    receipts: list[dict[str, Any]] = []
    page = 1
    while page <= 20:
        response = _response(archive if page == 1 else f"{archive}/{page}")
        receipts.append(receipt(response))
        soup = BeautifulSoup(response.text, "html.parser")
        found = 0
        for anchor in soup.select("a.title[href], .obj_issue_summary a[href], a[href*='/issue/view/']"):
            label = clean(anchor.get_text(" ", strip=True))
            context = clean(anchor.parent.get_text(" ", strip=True) if anchor.parent else label)
            if str(year) not in f"{label} {context}":
                continue
            url = urljoin(response.url, str(anchor.get("href")))
            pair = (label or str(year), url)
            if pair not in links:
                links.append(pair)
                found += 1
        if links and not found:
            break
        next_node = soup.select_one("a.next[href]")
        if not next_node:
            break
        page += 1
    return links, receipts


def fetch_aaai(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    year = int(spec["years"][0])
    issues, requests = _aaai_issue_links(year)
    if not issues:
        raise RuntimeError(f"AAAI OJS has no published issue for {year}")
    rows = []
    seen = set()
    for issue_label, issue_url in issues:
        response = _response(issue_url)
        requests.append(receipt(response))
        soup = BeautifulSoup(response.text, "html.parser")
        for article in soup.select(".obj_article_summary"):
            anchor = article.select_one("h3.title a[href], .title a[href]")
            title = clean(anchor.get_text(" ", strip=True) if anchor else "")
            url = urljoin(issue_url, str(anchor.get("href"))) if anchor else ""
            if not looks_like_title(title) or title_key(title) in seen:
                continue
            seen.add(title_key(title))
            authors_node = article.select_one(".authors")
            rows.append({"title": title, "abstract": "", "authors": [clean(authors_node.get_text(" ", strip=True))] if authors_node else [], "published": f"{year}-01-01", "year": year, "url": url, "pdf_url": "", "venue": "AAAI", "categories": [], "identifiers": {}, "metadata": {"aaai_issue": issue_label, "official_issue_url": issue_url}})
    _enrich_all(rows)
    return _result(rows, adapter="aaai_ojs", requests=requests, proof="official_aaai_ojs_issues_exhausted_and_all_details_enriched")


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


def _indexed_enrich(row: dict[str, Any]) -> dict[str, Any]:
    identifiers = row.setdefault("identifiers", {})
    doi = clean(identifiers.get("doi"))
    attempts = row.setdefault("metadata", {}).setdefault("indexed_enrichment", [])
    if doi and not clean(row.get("abstract")) and cooldown_remaining("openalex") <= 0:
        try:
            response = get(f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='')}", timeout=45)
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
            query = quote(clean(row.get("title")), safe="")
            response = get(f"https://api.openalex.org/works?search={query}&per-page=5", timeout=45)
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
                response = get(f"https://api.semanticscholar.org/graph/v1/paper/{quote(key, safe=':')}?fields=title,abstract,openAccessPdf", timeout=45)
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
            response = get("https://api.semanticscholar.org/graph/v1/paper/search", params={"query": clean(row.get("title")), "limit": 5, "fields": "title,abstract,openAccessPdf,externalIds"}, timeout=45)
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
    if not clean(row.get("abstract")) and cooldown_remaining("arxiv") <= 0:
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
    return row


def _batch_openalex_enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match the original ACM DOI enrichment semantics without one request/paper."""
    if cooldown_remaining("openalex") > 0:
        for row in rows:
            row.setdefault("metadata", {}).setdefault("indexed_enrichment", []).append({"source": "openalex_doi_for_acm_batch", "status": "skipped_persisted_long_cooldown", "cooldown_remaining_seconds": round(cooldown_remaining("openalex"), 3)})
        return rows
    by_doi = {clean((row.get("identifiers") or {}).get("doi")).lower(): row for row in rows if clean((row.get("identifiers") or {}).get("doi"))}
    dois = sorted(by_doi)
    for offset in range(0, len(dois), 50):
        chunk = dois[offset:offset + 50]
        try:
            response = get("https://api.openalex.org/works", params={"filter": "doi:" + "|".join(chunk), "per-page": 50, "select": "id,doi,display_name,abstract_inverted_index,best_oa_location,primary_location,authorships"}, timeout=90)
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
    if cooldown_remaining("semantic_scholar") > 0:
        return rows
    by_doi = {clean((row.get("identifiers") or {}).get("doi")).lower(): row for row in rows if clean((row.get("identifiers") or {}).get("doi"))}
    dois = sorted(by_doi)
    for offset in range(0, len(dois), 500):
        chunk = dois[offset:offset + 500]
        try:
            response = post("https://api.semanticscholar.org/graph/v1/paper/batch", params={"fields": "title,abstract,openAccessPdf,externalIds"}, json_body={"ids": [f"DOI:{doi}" for doi in chunk]}, timeout=120)
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
        urls = [f"https://www{year}.thewebconf.org/accepted/research-tracks.html"]
    elif venue_id == "sigir":
        urls = [f"https://sigir{year}.dei.unipd.it/accepted-papers.html", f"https://sigir{year}.dei.unipd.it/proceedings.html"]
    elif venue_id == "cikm":
        urls = [f"https://cikm{year}.org/program/proceedings"]
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
        soup = BeautifulSoup(response.text, "html.parser")
        candidates = soup.select("article, tr, li, .paper, .accepted-paper, .paper-item, .program-item")
        if not candidates:
            candidates = soup.find_all(["p", "div"])
        for node in candidates:
            anchor = node.find("a", href=True)
            title_node = node.select_one(".title, .paper-title, h3, h4") or anchor
            title = clean(title_node.get_text(" ", strip=True) if title_node else "")
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
            rows.append({"title": title, "abstract": "", "authors": [], "published": f"{year}-01-01", "year": year, "url": detail_url, "pdf_url": "", "venue": venue, "categories": [], "identifiers": {"doi": doi_match.group(0) if doi_match else ""}, "metadata": {"official_accepted_source": url}})
    return rows, receipts


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
    urls, index_receipts = _dblp_year_xml_urls(venue_id, year)
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
    if official_rows:
        official_keys = {title_key(row.get("title")) for row in official_rows}
        dblp_by_title = {title_key(row.get("title")): row for row in rows}
        missing_from_dblp = [row for row in official_rows if title_key(row.get("title")) not in dblp_by_title]
        rows = _merge_title_rows(rows, official_rows) + missing_from_dblp
        for row in rows:
            row.setdefault("metadata", {})["official_title_pool_observed"] = title_key(row.get("title")) in official_keys
    # TASTE permits indexed abstract enrichment for ACM venues, but never accepts
    # the DBLP title seed by itself as a verified cache.
    _batch_openalex_enrich(rows)
    _batch_semantic_scholar_enrich(rows)
    missing_rows = [row for row in rows if not clean(row.get("abstract"))]
    # ACM often challenges automated clients.  Probe serially and stop at the
    # first persisted cooldown so queued workers cannot each wait 60 seconds.
    for row in list(missing_rows):
        if cooldown_remaining("acm") > 0:
            break
        _try_acm_pdf_abstract(row)
    missing_rows = [row for row in rows if not clean(row.get("abstract"))]
    # Source cooldown is process-wide.  Keep fallback enrichment sequential so
    # workers cannot all pass a preflight check and then queue behind the same
    # newly-issued 403/429 cooldown.
    for row in missing_rows:
        _indexed_enrich(row)
    proof = "official_accepted_pool_merged_with_dblp_and_taste_indexed_abstract_enrichment" if official_rows else "complete_dblp_title_pool_with_taste_indexed_abstract_enrichment"
    result_rows, details = _result(rows, adapter=f"{venue_id}_acm_enriched", requests=requests, proof=proof)
    details.update({"official_title_pool_count": len(official_rows), "dblp_seed_count": len(rows) - max(0, len([row for row in official_rows if title_key(row.get('title')) not in seen]))})
    return result_rows, details


ADAPTERS: dict[str, Callable[[dict[str, Any]], tuple[list[dict[str, Any]], dict[str, Any]]]] = {
    "neurips_official": fetch_neurips_official,
    "icml_official": fetch_icml_official,
    "cvf_openaccess": fetch_cvf,
    "acl_anthology": fetch_acl_anthology,
    "ijcai_proceedings": fetch_ijcai,
    "eccv_virtual": fetch_eccv,
    "aaai_ojs": fetch_aaai,
    "acm_enriched": fetch_acm_venue,
}


def fetch_official_venue(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    adapter = clean(spec.get("adapter")).lower()
    if adapter not in ADAPTERS:
        raise ValueError(f"Unsupported official venue adapter: {adapter}")
    return ADAPTERS[adapter](spec)
