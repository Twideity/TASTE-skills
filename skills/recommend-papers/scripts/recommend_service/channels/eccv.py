from __future__ import annotations
import re
from typing import Any
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from .base import Channel
from .conference_common import complete_abstract_catalog
from .runtime import catalog_response, checkpointed_details, clean, finish, looks_like_title, response, worker_count
from .shared import explicit_pdf, values_blob
from ..http import receipt
ID="eccv"; SOURCE="ECVA"
def _marker(text):
    pos=text.lower().find("abstract")
    if pos<0:return ""
    body=text[pos+8:]; stops=[body.lower().find(x) for x in ("video","poster","chat","bibtex")]; stops=[x for x in stops if x>=0]
    return clean(body[:min(stops)] if stops else body)
def _detail(row):
    r=response(row["url"],timeout=30); s=BeautifulSoup(r.text,"html.parser")
    meta=s.find("meta",attrs={"name":"citation_abstract"}); row["abstract"]=clean(meta.get("content")) if meta else _marker(s.get_text("\n",strip=True))
    row["authors"]=[clean(x.get("content")) for x in s.find_all("meta",attrs={"name":"citation_author"}) if clean(x.get("content"))]
    pdf=s.find("meta",attrs={"name":"citation_pdf_url"}); row["pdf_url"]=urljoin(r.url,clean(pdf.get("content"))) if pdf else row.get("pdf_url","")
    row.setdefault("metadata",{})["detail_receipt"]=receipt(r)
def fetch_metadata(spec):
    year=int(spec["years"][0])
    if year%2: raise ValueError("ECCV is held in even-numbered years")
    list_url=f"https://eccv.ecva.net/virtual/{year}/papers.html"; r=catalog_response(list_url,label="ECCV",year=year); s=BeautifulSoup(r.text,"html.parser"); rows=[]; seen=set(); discovered=set()
    for a in s.find_all("a",href=True):
        href=str(a.get("href") or ""); title=clean(a.get_text(" ",strip=True)); url=urljoin(list_url,href)
        if not ("/poster/" in href or "/paper/" in href) or url in discovered:continue
        discovered.add(url)
        if not looks_like_title(title):continue
        seen.add(url); rows.append({"title":title,"abstract":"","authors":[],"published":f"{year}-01-01","year":year,"url":url,"pdf_url":"","venue":"ECCV","categories":[],"identifiers":{},"metadata":{"official_index":list_url}})
    checkpointed_details(spec,rows,adapter="eccv_virtual",enrich=_detail,workers=worker_count(spec,16))
    return finish(spec,rows,adapter="eccv_virtual",requests=[receipt(r)],proof="official_ecva_index_exhausted_and_all_details_enriched",discovered_count=len(discovered))
def pdf_candidates(paper:dict[str,Any]):
    rows=explicit_pdf(paper,"eccv_ecva_pdf",SOURCE)
    for year,pid,page_year in re.findall(r"ecva\.net/papers/eccv_(\d{4})/papers_ECCV/html/(\d+)_ECCV_(\d{4})_paper\.php",values_blob(paper),re.I):
        if year==page_year:rows.append({"url":f"https://www.ecva.net/papers/eccv_{year}/papers_ECCV/papers/{int(pid):05d}.pdf","kind":"eccv_ecva_pdf","official_source":SOURCE})
    return list({r["url"]:r for r in rows}.values())
CHANNEL=Channel(ID,"conference",fetch_metadata,2,16,4,SOURCE,complete_abstract_catalog,pdf_candidates)
