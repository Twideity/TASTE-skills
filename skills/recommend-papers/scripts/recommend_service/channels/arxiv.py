from __future__ import annotations
from typing import Any
import re
from pathlib import Path
from .shared import clean
from ..storage import DATA_ROOT, METADATA_CACHE_ROOT

ID = "arxiv"
id = ID
kind = "arxiv"
METADATA_SCHEMA = 2
metadata_schema = METADATA_SCHEMA
METADATA_WORKERS = 1
metadata_workers = METADATA_WORKERS
PDF_WORKERS = 2
pdf_workers = PDF_WORKERS

def day_cache_path(category: str, day: str) -> Path:
    safe_category = re.sub(r"[^A-Za-z0-9._-]+", "_", category)
    return METADATA_CACHE_ROOT / ID / safe_category / f"{day}.json"

def page_stage_path(category: str, start_date: str, end_date: str, offset: int) -> Path:
    safe_category = re.sub(r"[^A-Za-z0-9._-]+", "_", category)
    return DATA_ROOT / "state" / f"{ID}-staging" / safe_category / f"{start_date}_{end_date}" / f"{offset:06d}.json"

def fetch_metadata(spec: dict[str, Any], *, policy: str, max_age_days: float):
    from ..metadata import fetch_arxiv_cached
    return fetch_arxiv_cached(spec, policy=policy, max_age_days=max_age_days)

def pdf_candidates(paper: dict[str, Any]):
    ids = paper.get("identifiers") if isinstance(paper.get("identifiers"), dict) else {}
    value = clean(ids.get("arxiv_id"))
    return [{"url": f"https://arxiv.org/pdf/{value}", "kind": "arxiv_official_pdf", "official_source": "arXiv"}] if value else []
