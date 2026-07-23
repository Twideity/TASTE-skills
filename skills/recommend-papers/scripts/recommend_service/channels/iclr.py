from __future__ import annotations
import time
from typing import Any
from .base import Channel
from .conference_common import complete_abstract_catalog
from .runtime import clean, finish
from .shared import explicit_pdf
from ..credentials import openreview_settings
from ..http import service_call
ID="iclr"; SOURCE="OpenReview / ICLR Proceedings"
def _value(v):return v.get("value") if isinstance(v,dict) and "value" in v else v
def fetch_metadata(spec):
    import openreview
    year=int(spec["years"][0]); settings=openreview_settings()
    try:
        client,login_errors=service_call("openreview",lambda:openreview.api.OpenReviewClient(baseurl="https://api2.openreview.net",username=settings["username"] or None,password=settings["password"] or None),max_attempts=5)
    except Exception as exc:raise RuntimeError(f"ICLR OpenReview client initialization failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
    venue_id=clean(spec.get("openreview_venue_id")) or f"ICLR.cc/{year}/Conference"; rows=[]; offset=0; requests=[]
    while True:
        page_limit=1000
        notes,errors=service_call("openreview",lambda:client.get_notes(content={"venueid":venue_id},limit=page_limit,offset=offset),max_attempts=5)
        requests.append({"route":"content.venueid","venue_id":venue_id,"offset":offset,"count":len(notes),"retry_errors":errors})
        for note in notes:
            content=getattr(note,"content",{}) or {}; title=clean(_value(content.get("title"))); abstract=clean(_value(content.get("abstract")))
            if not title:continue
            authors=_value(content.get("authors")) or []; authors=authors if isinstance(authors,list) else [clean(authors)]
            note_id=clean(getattr(note,"id","")); doi=clean(_value(content.get("doi")))
            rows.append({"title":title,"abstract":abstract,"authors":[clean(x) for x in authors if clean(x)],"published":f"{year}-01-01","year":year,"url":f"https://openreview.net/forum?id={note_id}","pdf_url":f"https://openreview.net/pdf?id={note_id}","venue":"ICLR","categories":[],"identifiers":{"openreview_id":note_id,"doi":doi},"metadata":{"openreview_venue_id":venue_id}})
        offset+=len(notes)
        if len(notes)<page_limit:break
        time.sleep(2.1)
    result,receipt=finish(spec,rows,adapter="openreview",requests=requests,proof="openreview_venueid_pagination_exhausted",discovered_count=len(rows))
    receipt["login_retry_errors"]=login_errors
    return result,receipt
def pdf_candidates(paper:dict[str,Any]):
    rows=explicit_pdf(paper,"iclr_official_pdf",SOURCE); ids=paper.get("identifiers") if isinstance(paper.get("identifiers"),dict) else {}; note=clean(ids.get("openreview_id"))
    if note:rows.insert(0,{"url":f"https://openreview.net/pdf?id={note}","kind":"iclr_openreview_pdf","official_source":SOURCE})
    return list({r["url"]:r for r in rows}.values())
CHANNEL=Channel(ID,"conference",fetch_metadata,2,1,1,SOURCE,complete_abstract_catalog,pdf_candidates)
