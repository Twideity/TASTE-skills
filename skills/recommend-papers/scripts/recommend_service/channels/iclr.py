from __future__ import annotations
import time
from typing import Any
from .base import Channel
from .conference_common import complete_abstract_catalog
from .runtime import abstract_is_real, clean, finish
from .shared import explicit_pdf
from ..credentials import openreview_settings
from ..http import service_call
from ..storage import DATA_ROOT, read_json, write_json
ID="iclr"; SOURCE="OpenReview / ICLR Proceedings"
def _value(v):return v.get("value") if isinstance(v,dict) and "value" in v else v
def _values(*items):
    rows=[]
    for item in items:
        value=_value(item)
        values=value if isinstance(value,list) else [value]
        rows.extend(clean(entry) for entry in values if clean(entry))
    return list(dict.fromkeys(rows))
def _staged_page_ok(payload,venue_id,offset):
    try:
        papers=payload.get("papers")
        return isinstance(payload,dict) and payload.get("schema_version")==1 and payload.get("venue_id")==venue_id and payload.get("offset")==offset and isinstance(papers,list) and int(payload.get("source_count") or 0)==len(papers) and all(isinstance(row,dict) and abstract_is_real(row.get("abstract")) for row in papers)
    except (AttributeError,TypeError,ValueError):
        return False
def fetch_metadata(spec):
    import openreview
    year=int(spec["years"][0]); settings=openreview_settings()
    try:
        client,login_errors=service_call("openreview",lambda:openreview.api.OpenReviewClient(baseurl="https://api2.openreview.net",username=settings["username"] or None,password=settings["password"] or None),max_attempts=5)
    except Exception as exc:raise RuntimeError(f"ICLR OpenReview client initialization failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
    venue_id=clean(spec.get("openreview_venue_id")) or f"ICLR.cc/{year}/Conference"; rows=[]; offset=0; requests=[]
    stage_dir=DATA_ROOT/"state"/"venue-staging"/ID/str(year)/"openreview"/"pages"
    while True:
        page_limit=1000
        stage_path=stage_dir/f"{offset:06d}.json"; staged=read_json(stage_path,{})
        if _staged_page_ok(staged,venue_id,offset):
            page_rows=staged["papers"]; source_count=int(staged.get("source_count") or len(page_rows)); errors=[]; cache_status="staging_cache_hit"
        else:
            notes,errors=service_call("openreview",lambda:client.get_notes(content={"venueid":venue_id},limit=page_limit,offset=offset),max_attempts=5)
            source_count=len(notes)
            page_rows=[]
            for note in notes:
                content=getattr(note,"content",{}) or {}; title=clean(_value(content.get("title"))); abstract=clean(_value(content.get("abstract")))
                if not title:continue
                authors=_value(content.get("authors")) or []; authors=authors if isinstance(authors,list) else [clean(authors)]
                note_id=clean(getattr(note,"id","")); doi=clean(_value(content.get("doi")))
                categories=_values(content.get("primary_area"),content.get("subject_areas"),content.get("keywords"))
                page_rows.append({"title":title,"abstract":abstract,"authors":[clean(x) for x in authors if clean(x)],"published":f"{year}-01-01","year":year,"url":f"https://openreview.net/forum?id={note_id}","pdf_url":f"https://openreview.net/pdf?id={note_id}","venue":"ICLR","categories":categories,"identifiers":{"openreview_id":note_id,"doi":doi},"metadata":{"openreview_venue_id":venue_id}})
            if source_count==len(page_rows) and all(abstract_is_real(row.get("abstract")) for row in page_rows):
                write_json(stage_path,{"schema_version":1,"venue_id":venue_id,"offset":offset,"source_count":source_count,"papers":page_rows})
            cache_status="fetched"
        requests.append({"route":"content.venueid","venue_id":venue_id,"offset":offset,"source_count":source_count,"count":len(page_rows),"retry_errors":errors,"cache_status":cache_status})
        rows.extend(page_rows); offset+=source_count
        if source_count<page_limit:break
        time.sleep(2.1)
    result,receipt=finish(spec,rows,adapter="openreview",requests=requests,proof="openreview_venueid_pagination_exhausted",discovered_count=sum(int(item.get("source_count") or 0) for item in requests))
    receipt["login_retry_errors"]=login_errors
    return result,receipt
def pdf_candidates(paper:dict[str,Any]):
    rows=explicit_pdf(paper,"iclr_official_pdf",SOURCE); ids=paper.get("identifiers") if isinstance(paper.get("identifiers"),dict) else {}; note=clean(ids.get("openreview_id"))
    if note:rows.insert(0,{"url":f"https://openreview.net/pdf?id={note}","kind":"iclr_openreview_pdf","official_source":SOURCE})
    return list({r["url"]:r for r in rows}.values())
CHANNEL=Channel(ID,"conference",fetch_metadata,2,1,1,SOURCE,complete_abstract_catalog,pdf_candidates)
