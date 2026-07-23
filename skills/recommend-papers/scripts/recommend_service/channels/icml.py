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
ID="icml"; SOURCE="ICML / PMLR / OpenReview"

def _marker(text,start,ends):
    pos=text.lower().find(start.lower())
    if pos<0:return ""
    body=text[pos+len(start):]; stops=[body.lower().find(x.lower()) for x in ends]; stops=[x for x in stops if x>=0]
    return "\n".join(x.strip() for x in (body[:min(stops)] if stops else body).splitlines() if x.strip())

def _detail(row):
    r=response(row["url"],timeout=30); s=BeautifulSoup(r.text,"html.parser"); text=s.get_text("\n",strip=True)
    meta=s.find("meta",attrs={"name":"citation_abstract"})
    row["abstract"]=clean(meta.get("content")) if meta else _marker(text,"Abstract",["Video","Poster","Chat","BibTeX"])
    row["authors"]=[clean(x.get("content")) for x in s.find_all("meta",attrs={"name":"citation_author"}) if clean(x.get("content"))]
    pdf=s.find("meta",attrs={"name":"citation_pdf_url"}); row["pdf_url"]=urljoin(r.url,clean(pdf.get("content"))) if pdf else row.get("pdf_url","")
    row.setdefault("metadata",{})["detail_receipt"]=receipt(r)

def fetch_metadata(spec):
    year=int(spec["years"][0]); list_url=f"https://icml.cc/virtual/{year}/papers.html"; r=catalog_response(list_url,label="ICML",year=year,timeout=90)
    s=BeautifulSoup(r.text,"html.parser"); rows=[]; seen=set(); discovered=set()
    for a in s.find_all("a",href=True):
        href=str(a.get("href") or ""); title=clean(a.get_text(" ",strip=True)); url=urljoin(list_url,href)
        if not any(x in href for x in ("/poster/","/oral/","/paper/","/spotlight/")) or url in discovered: continue
        discovered.add(url)
        if not looks_like_title(title): continue
        seen.add(url); presentation=next((label for marker,label in (("/oral/","oral"),("/spotlight/","spotlight"),("/poster/","poster")) if marker in href),"paper")
        rows.append({"title":title,"abstract":"","authors":[],"published":f"{year}-01-01","year":year,"url":url,"pdf_url":"","venue":"ICML","categories":[],"presentation_type":presentation,"identifiers":{},"metadata":{"official_index":list_url,"presentation_type":presentation}})
    checkpointed_details(spec,rows,adapter="icml_official_virtual",enrich=_detail,workers=worker_count(spec,16))
    return finish(spec,rows,adapter="icml_official_virtual",requests=[receipt(r)],proof="official_icml_virtual_index_exhausted_and_all_details_enriched",discovered_count=len(discovered))

def pdf_candidates(paper:dict[str,Any]):
    rows=explicit_pdf(paper,"icml_official_pdf",SOURCE)
    for volume,pid in re.findall(r"proceedings\.mlr\.press/(v\d+)/([A-Za-z0-9_.-]+)\.html",values_blob(paper)):
        rows.extend([{"url":f"https://proceedings.mlr.press/{volume}/{pid}/{pid}.pdf","kind":"icml_pmlr_pdf","official_source":SOURCE},{"url":f"https://raw.githubusercontent.com/mlresearch/{volume}/main/assets/{pid}/{pid}.pdf","kind":"icml_pmlr_raw_pdf","official_source":SOURCE}])
    return list({r["url"]:r for r in rows}.values())
CHANNEL=Channel(ID,"conference",fetch_metadata,2,16,4,SOURCE,complete_abstract_catalog,pdf_candidates)
