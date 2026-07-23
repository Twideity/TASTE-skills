from __future__ import annotations
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import Channel
from .conference_common import complete_abstract_catalog
from .runtime import AuthoritativeEmptyCatalog, checkpointed_details, clean, finish, looks_like_title, response, worker_count
from .shared import explicit_pdf, values_blob
from ..http import get, receipt

ID = "neurips"
SOURCE = "NeurIPS Proceedings"

def _extract_between_markers(text: str, start: str, markers: list[str]) -> str:
    index = text.lower().find(start.lower())
    if index < 0:
        return ""
    body = text[index + len(start):]
    positions = [body.lower().find(marker.lower()) for marker in markers]
    positions = [position for position in positions if position >= 0]
    if positions:
        body = body[:min(positions)]
    return "\n".join(line.strip() for line in body.splitlines() if line.strip()).strip()


def _detail(row: dict[str, Any]) -> dict[str, Any]:
    """TASTE Finding's NeurIPS marker parser, adapted to our receipt schema."""
    detail_response = response(str(row["url"]), timeout=30)
    soup = BeautifulSoup(detail_response.text, "html.parser")
    text = soup.get_text("\n", strip=True)
    row["abstract"] = _extract_between_markers(text, "Abstract", [
        "\nVideo\n", "\nSpotlight\n", "\nPoster\n", "\nName Change Policy\n",
        "\nChat is not available", "\nSuccessful Page Load",
    ])
    row["authors"] = [
        clean(node.get("content"))
        for node in soup.find_all("meta", attrs={"name": "citation_author"})
        if clean(node.get("content"))
    ]
    pdf_meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    row["pdf_url"] = urljoin(detail_response.url, clean(pdf_meta.get("content"))) if pdf_meta else ""
    doi_meta = soup.find("meta", attrs={"name": "citation_doi"})
    doi = clean(doi_meta.get("content")) if doi_meta else ""
    if doi:
        row.setdefault("identifiers", {})["doi"] = doi
    row.setdefault("metadata", {})["detail_receipt"] = receipt(detail_response)
    return row


def fetch_metadata(spec):
    year = int(spec["years"][0])
    list_response = None
    not_found = 0
    for list_url in (
        f"https://proceedings.neurips.cc/paper_files/paper/{year}",
        f"https://papers.nips.cc/paper_files/paper/{year}",
    ):
        try:
            candidate = get(list_url, timeout=90)
            if candidate.status_code == 404:
                not_found += 1
                continue
            candidate.raise_for_status()
            list_response = candidate
            break
        except Exception:
            continue
    if list_response is None:
        if not_found == 2:
            raise AuthoritativeEmptyCatalog(f"NeurIPS has no published proceedings catalog for {year}")
        raise RuntimeError(f"NeurIPS official proceedings index unavailable for {year}")
    soup = BeautifulSoup(list_response.text, "html.parser")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    discovered: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        title = clean(anchor.get_text(" ", strip=True))
        if "-Abstract-" not in href or not href.endswith(".html"):
            continue
        detail_url = urljoin(list_response.url, href)
        if detail_url in discovered:
            continue
        discovered.add(detail_url)
        if not looks_like_title(title):
            continue
        seen.add(detail_url)
        rows.append({"title": title, "abstract": "", "authors": [], "published": f"{year}-01-01", "year": year, "url": detail_url, "pdf_url": "", "venue": "NeurIPS", "categories": [], "identifiers": {}, "metadata": {"official_index": list_response.url}})
    workers = worker_count(spec, 8)
    checkpointed_details(
        spec,
        rows,
        adapter="neurips_official_papers",
        enrich=_detail,
        workers=workers,
    )
    result, details = finish(
        spec, rows, adapter="neurips_official_papers",
        requests=[receipt(list_response)],
        proof="official_neurips_proceedings_index_exhausted_and_all_details_enriched",
        discovered_count=len(discovered),
    )
    details["detail_parser"] = "taste_neurips_marker_parser"
    details["detail_workers"] = workers
    return result, details
def pdf_candidates(paper: dict[str, Any]):
    rows = explicit_pdf(paper, "neurips_official_pdf", SOURCE)
    for year, digest, track in re.findall(r"(?:papers\.nips\.cc|proceedings\.neurips\.cc)/paper_files/paper/(\d{4})/hash/([A-Za-z0-9]+)-Abstract-([^\"'<>\s/]+)\.html", values_blob(paper)):
        rows.append({"url": f"https://proceedings.neurips.cc/paper_files/paper/{year}/file/{digest}-Paper-{track}.pdf", "kind": "neurips_official_pdf", "official_source": SOURCE})
    return list({row["url"]: row for row in rows}.values())

CHANNEL = Channel(ID, "conference", fetch_metadata, 2, 8, 4, SOURCE, complete_abstract_catalog, pdf_candidates)
