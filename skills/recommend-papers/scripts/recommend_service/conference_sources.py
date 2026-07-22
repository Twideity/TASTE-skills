from __future__ import annotations

"""Deterministic official-PDF derivation migrated from TASTE Reading."""

import re
from typing import Any


ALIASES = {
    "nips": "nips", "neurips": "nips", "iclr": "iclr", "icml": "icml",
    "sigkdd": "sigkdd", "kdd": "sigkdd", "sigir": "sigir", "cikm": "cikm",
    "aaai": "aaai", "iccv": "iccv", "www": "www", "thewebconference": "www",
    "cvpr": "cvpr", "acl": "acl", "ijcai": "ijcai", "eccv": "eccv", "emnlp": "emnlp",
}

OFFICIAL_SOURCE = {
    "nips": "NeurIPS Proceedings", "iclr": "OpenReview", "icml": "OpenReview/PMLR",
    "sigkdd": "ACM Digital Library", "sigir": "ACM Digital Library", "cikm": "ACM Digital Library",
    "www": "ACM Digital Library", "aaai": "AAAI Proceedings", "cvpr": "CVF Open Access",
    "iccv": "CVF Open Access", "eccv": "ECVA", "acl": "ACL Anthology",
    "emnlp": "ACL Anthology", "ijcai": "IJCAI Proceedings",
}


def _is_official_pdf_url(url: str) -> bool:
    lowered = str(url or "").lower()
    if not lowered.startswith("http") or any(term in lowered for term in ("supplemental", "/supp/", "poster", "slides")):
        return False
    hosts = (
        "papers.nips.cc", "proceedings.neurips.cc", "proceedings.iclr.cc",
        "openreview.net/pdf", "proceedings.mlr.press", "dl.acm.org/doi/pdf",
        "ojs.aaai.org/index.php/aaai/article/view", "ojs.aaai.org/index.php/aaai/article/download",
        "openaccess.thecvf.com/content/",
        "aclanthology.org/", "ijcai.org/proceedings/", "ecva.net/papers/",
    )
    pmlr_raw = bool(re.match(
        r"https?://raw\.githubusercontent\.com/mlresearch/v\d+/[^/]+/assets/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.pdf(?:\?.*)?$",
        str(url or ""), re.I,
    ))
    return (any(host in lowered for host in hosts) or pmlr_raw) and (
        lowered.endswith(".pdf") or "/pdf" in lowered or "article/view" in lowered or "article/download" in lowered
    )


def _channel(paper: dict[str, Any]) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    values = [paper.get("venue"), paper.get("source"), metadata.get("venue"), paper.get("url"), paper.get("pdf_url")]
    for value in values:
        text = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
        for alias, channel in ALIASES.items():
            if text == alias or text.startswith(alias):
                return channel
    blob = " ".join(str(value or "") for value in values).lower()
    for marker, channel in (("aclanthology.org", "acl"), ("openaccess.thecvf.com", "cvpr"), ("ijcai", "ijcai"), ("aaai", "aaai"), ("eccv", "eccv"), ("kdd", "sigkdd")):
        if marker in blob:
            return channel
    return ""


def _derived(channel: str, blob: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"https?://(?:papers\.nips\.cc|proceedings\.neurips\.cc)/paper_files/paper/(\d{4})/hash/([A-Za-z0-9]+)-Abstract-([^\"'<>\s/]+)\.html", blob):
        year, paper_hash, track = match.groups()
        urls.append(f"https://proceedings.neurips.cc/paper_files/paper/{year}/file/{paper_hash}-Paper-{track}.pdf")
    for match in re.finditer(r"https?://proceedings\.iclr\.cc/paper_files/paper/(\d{4})/hash/([A-Za-z0-9]+)-Abstract-([^\"'<>\s/]+)\.html", blob):
        year, paper_hash, track = match.groups()
        urls.append(f"https://proceedings.iclr.cc/paper_files/paper/{year}/file/{paper_hash}-Paper-{track}.pdf")
    for match in re.finditer(r"https?://proceedings\.mlr\.press/(v\d+)/([A-Za-z0-9_.-]+)\.html", blob):
        volume, paper_id = match.groups()
        urls.extend([f"https://proceedings.mlr.press/{volume}/{paper_id}/{paper_id}.pdf", f"https://raw.githubusercontent.com/mlresearch/{volume}/main/assets/{paper_id}/{paper_id}.pdf"])
    for match in re.finditer(r"https?://openaccess\.thecvf\.com/content/([^/]+)/html/([^\"'<>\s]+)\.html", blob):
        event, paper_id = match.groups()
        urls.append(f"https://openaccess.thecvf.com/content/{event}/papers/{paper_id}.pdf")
    for match in re.finditer(
        r"https?://(?:www\.)?ecva\.net/papers/eccv_(\d{4})/papers_ECCV/html/(\d+)_ECCV_(\d{4})_paper\.php",
        blob,
        flags=re.I,
    ):
        year, paper_id, page_year = match.groups()
        if year == page_year:
            urls.append(f"https://www.ecva.net/papers/eccv_{year}/papers_ECCV/papers/{int(paper_id):05d}.pdf")
    for match in re.finditer(r"https?://aclanthology\.org/([0-9]{4}\.[A-Za-z0-9-]+\.\d+)/?", blob):
        urls.append(f"https://aclanthology.org/{match.group(1)}.pdf")
    doi = re.search(r"\b(10\.1145/\d+(?:\.\d+)?)\b", blob)
    if doi:
        urls.append("https://dl.acm.org/doi/pdf/" + doi.group(1))
    if channel == "ijcai":
        match = re.search(r"ijcai\.org/proceedings/(\d{4})/(\d+)", blob.lower())
        if match:
            urls.append(f"https://www.ijcai.org/proceedings/{match.group(1)}/{int(match.group(2)):04d}.pdf")
    if channel == "aaai":
        match = re.search(r"ojs\.aaai\.org/index\.php/aaai/article/view/(\d+)/(\d+)", blob.lower())
        if match:
            urls.append(f"https://ojs.aaai.org/index.php/AAAI/article/view/{match.group(1)}/{match.group(2)}")
    return list(dict.fromkeys(urls))


def official_pdf_candidates(paper: dict[str, Any]) -> list[dict[str, str]]:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    identifiers = paper.get("identifiers") if isinstance(paper.get("identifiers"), dict) else {}
    channel = _channel(paper)
    values = [paper.get("url"), paper.get("pdf_url"), identifiers.get("doi"), metadata.get("url"), metadata.get("pdf_url"), metadata.get("doi")]
    blob = " ".join(str(value or "") for value in values)
    urls = [str(paper.get("pdf_url") or "").strip(), str(metadata.get("pdf_url") or "").strip(), *_derived(channel, blob)]
    return [
        {"url": url, "kind": "conference_official_pdf", "official_source": OFFICIAL_SOURCE.get(channel, "")}
        for url in dict.fromkeys(urls)
        if _is_official_pdf_url(url)
    ]
