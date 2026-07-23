from __future__ import annotations
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from .base import Channel
from .conference_common import complete_abstract_catalog
from .runtime import clean, finish, looks_like_title, probe_limit, response
from .shared import explicit_pdf, values_blob
from ..http import receipt
ID = "cvpr"; SOURCE = "CVF Open Access"

def _detail(row):
    r=response(row["url"],timeout=45); s=BeautifulSoup(r.text,"html.parser")
    node=s.select_one("#abstract"); row["abstract"]=clean(node.get_text(" ",strip=True) if node else "")
    row.setdefault("metadata",{})["detail_receipt"]=receipt(r)

def fetch_metadata(spec):
    year=int(spec["years"][0]); list_url=f"https://openaccess.thecvf.com/CVPR{year}?day=all"
    r=response(list_url); s=BeautifulSoup(r.text,"html.parser"); rows=[]; seen=set()
    for node in s.select("dt.ptitle a[href], dt a[href]"):
        title=clean(node.get_text(" ",strip=True)); url=urljoin(list_url,str(node.get("href") or ""))
        if not looks_like_title(title) or url in seen: continue
        seen.add(url); parent=node.find_parent("dt"); authors=[]; pdf=""
        for sibling in parent.find_next_siblings(["dd","dt"]) if parent else []:
            if sibling.name=="dt": break
            if not authors: authors=[clean(a.get_text(" ",strip=True)) for a in sibling.select("form.authsearch a, a[onclick*='authsearch']") if clean(a.get_text(" ",strip=True))]
            link=sibling.find("a",href=re.compile(r"\.pdf(?:$|[?#])",re.I))
            if link: pdf=urljoin(list_url,str(link.get("href"))); break
        rows.append({"title":title,"abstract":"","authors":authors,"published":f"{year}-01-01","year":year,"url":url,"pdf_url":pdf,"venue":"CVPR","categories":[],"identifiers":{},"metadata":{"official_index":list_url}})
    limit=probe_limit(spec); selected=rows[:limit] if limit else rows
    with ThreadPoolExecutor(max_workers=max(1,min(3 if limit else 8,len(selected)))) as pool: list(pool.map(_detail,selected))
    return finish(spec,selected,adapter="cvf_openaccess",requests=[receipt(r)],proof="official_cvf_index_exhausted_and_all_details_enriched",discovered_count=len(rows))

def pdf_candidates(paper: dict[str,Any]):
    rows=explicit_pdf(paper,"cvpr_cvf_pdf",SOURCE)
    for event,pid in re.findall(r"openaccess\.thecvf\.com/content/([^/]+)/html/([^\"'<>\s]+)\.html",values_blob(paper)):
        rows.append({"url":f"https://openaccess.thecvf.com/content/{event}/papers/{pid}.pdf","kind":"cvpr_cvf_pdf","official_source":SOURCE})
    return list({r["url"]:r for r in rows}.values())
CHANNEL=Channel(ID,"conference",fetch_metadata,2,8,6,SOURCE,complete_abstract_catalog,pdf_candidates)
