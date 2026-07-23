from __future__ import annotations
from typing import Any
from pathlib import Path
from .shared import clean
from ..storage import DATA_ROOT, METADATA_CACHE_ROOT

ID = "biorxiv"
id = ID
kind = "biorxiv"
METADATA_SCHEMA = 2
metadata_schema = METADATA_SCHEMA
METADATA_WORKERS = 1
metadata_workers = METADATA_WORKERS
PDF_WORKERS = 2
pdf_workers = PDF_WORKERS

def day_cache_path(day: str) -> Path:
    return METADATA_CACHE_ROOT / ID / f"{day}.json"

def page_stage_path(start_date: str, end_date: str, cursor: int) -> Path:
    return DATA_ROOT / "state" / f"{ID}-staging" / f"{start_date}_{end_date}" / f"{cursor:06d}.json"

def fetch_metadata(spec: dict[str, Any], *, policy: str, max_age_days: float):
    from ..metadata import fetch_biorxiv_cached
    return fetch_biorxiv_cached(spec, policy=policy, max_age_days=max_age_days)

def pdf_candidates(paper: dict[str, Any]):
    ids = paper.get("identifiers") if isinstance(paper.get("identifiers"), dict) else {}
    doi = clean(ids.get("doi"))
    version = clean(ids.get("biorxiv_version")) or "1"
    return [{"url": f"https://www.biorxiv.org/content/{doi}v{version}.full.pdf", "kind": "biorxiv_official_pdf", "official_source": "bioRxiv"}] if doi else []
