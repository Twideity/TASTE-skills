from __future__ import annotations
from typing import Any
from .base import Channel
from .conference_common import complete_abstract_catalog
from .runtime import clean
from .shared import explicit_pdf
from .acm_shared import fetch_acm_venue
ID="kdd"; SOURCE="ACM Digital Library"
def fetch_metadata(spec): owned=dict(spec,venue_id=ID); return fetch_acm_venue(owned)
def pdf_candidates(paper:dict[str,Any]):
    rows=explicit_pdf(paper,"kdd_official_pdf",SOURCE); ids=paper.get("identifiers") if isinstance(paper.get("identifiers"),dict) else {}; doi=clean(ids.get("doi"))
    if doi.startswith("10.1145/"):rows.insert(0,{"url":f"https://dl.acm.org/doi/pdf/{doi}","kind":"kdd_acm_pdf","official_source":SOURCE})
    return list({r["url"]:r for r in rows}.values())
CHANNEL=Channel(ID,"conference",fetch_metadata,2,1,2,SOURCE,complete_abstract_catalog,pdf_candidates)
