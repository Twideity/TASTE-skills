from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from bs4 import BeautifulSoup

from .base import Channel
from .conference_common import complete_abstract_catalog
from .runtime import clean, finish, probe_limit
from .shared import explicit_pdf, values_blob
from ..http import get, receipt

ID = "emnlp"
SOURCE = "ACL Anthology"


def _detail(row: dict[str, Any]) -> None:
    response = get(str(row["url"]), timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    node = soup.select_one("#abstract, .card-body.acl-abstract, div.acl-abstract")
    if node:
        row["abstract"] = re.sub(r"^abstract\s*[:—-]?\s*", "", clean(node.get_text(" ", strip=True)), flags=re.I)
    row.setdefault("metadata", {})["detail_receipt"] = receipt(response)


def fetch_metadata(spec):
    year = int(spec["years"][0])
    requests = []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_url, wanted_volume in (
        (f"https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{year}.emnlp.xml", ""),
        (f"https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{year}.findings.xml", "emnlp"),
    ):
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
                if not title:
                    continue
                anthology_id = clean(node.findtext("url")) or f"{year}.emnlp-{volume.get('id')}.{node.get('id')}"
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
                rows.append({"title": title, "abstract": abstract, "authors": authors, "published": f"{year}-01-01", "year": year, "url": paper_url, "pdf_url": paper_url.rstrip("/") + ".pdf", "venue": "EMNLP", "categories": [], "identifiers": {"doi": clean(node.findtext("doi")), "acl_anthology_id": anthology_id}, "metadata": {"official_xml": source_url}})
    limit = probe_limit(spec)
    selected = rows[:limit] if limit else rows
    missing = [row for row in selected if not clean(row.get("abstract"))]
    with ThreadPoolExecutor(max_workers=max(1, min(3 if limit else 8, len(missing)))) as pool:
        list(pool.map(_detail, missing))
    return finish(spec, selected, adapter="acl_anthology", requests=requests, proof="official_acl_anthology_xml_exhausted_and_all_abstracts_present", discovered_count=len(rows))


def pdf_candidates(paper: dict[str, Any]):
    rows = explicit_pdf(paper, "emnlp_anthology_pdf", SOURCE)
    for pid in re.findall(r"aclanthology\.org/([0-9]{4}\.[A-Za-z0-9-]+\.\d+)/?", values_blob(paper)):
        rows.append({"url": f"https://aclanthology.org/{pid}.pdf", "kind": "emnlp_anthology_pdf", "official_source": SOURCE})
    return list({row["url"]: row for row in rows}.values())


CHANNEL = Channel(ID, "conference", fetch_metadata, 2, 8, 6, SOURCE, complete_abstract_catalog, pdf_candidates)
