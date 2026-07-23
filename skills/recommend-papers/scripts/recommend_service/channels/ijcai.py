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
from ..http import get, receipt
ID="ijcai"; SOURCE="IJCAI Proceedings"
def _key(v):return re.sub(r"[^a-z0-9]+"," ",clean(v).lower()).strip()
def _detail(row):
    r=response(row["url"],timeout=40); s=BeautifulSoup(r.text,"html.parser")
    node=s.select_one(".abstract, #abstract, div.col-md-12 > p"); text=clean(node.get_text(" ",strip=True) if node else "")
    row["abstract"]=re.sub(r"^abstract\s*[:—-]?\s*","",text,flags=re.I); row.setdefault("metadata",{})["detail_receipt"]=receipt(r)
def _accepted(html,year,url):
    s=BeautifulSoup(html,"html.parser"); rows=[]; seen=set()
    for node in s.select(".ij-paper"):
        t=node.select_one(".ij-ptitle"); title=clean(t.get_text(" ",strip=True) if t else "")
        if not looks_like_title(title) or _key(title) in seen:continue
        seen.add(_key(title)); pidnode=node.select_one(".ij-pid"); pid=clean(pidnode.get_text(" ",strip=True) if pidnode else "").lstrip("#")
        authors=[clean(x.get_text(" ",strip=True)) for x in node.select(".ij-author")]; abstract=node.select_one(".ij-abstract")
        rows.append({"title":title,"abstract":clean(abstract.get_text(" ",strip=True) if abstract else ""),"authors":authors,"published":f"{year}-01-01","year":year,"url":url+(f"#paper-{pid}" if pid else ""),"pdf_url":"","venue":"IJCAI","categories":[clean(x.get_text(" ",strip=True)) for x in node.select(".ij-kw") if clean(x.get_text(" ",strip=True))],"identifiers":{"ijcai_paper_id":pid},"metadata":{"official_accepted_papers_page":url}})
    return rows
def fetch_metadata(spec):
    year=int(spec["years"][0]); limit=probe_limit(spec); list_url=f"https://www.ijcai.org/proceedings/{year}/"; r=get(list_url,timeout=60); requests=[receipt(r)]; rows=[]
    if r.ok:
        s=BeautifulSoup(r.text,"html.parser")
        for wrapper in s.select("div.paper_wrapper"):
            t=wrapper.select_one(".title"); title=clean(t.get_text(" ",strip=True) if t else "")
            if not looks_like_title(title):continue
            authors=wrapper.select_one(".authors"); detail=wrapper.select_one("a[href*='/proceedings/'][href]"); pdf=wrapper.find("a",string=re.compile("pdf",re.I))
            rows.append({"title":title,"abstract":"","authors":[clean(authors.get_text(" ",strip=True))] if authors else [],"published":f"{year}-01-01","year":year,"url":urljoin(list_url,str(detail.get("href"))) if detail else "","pdf_url":urljoin(list_url,str(pdf.get("href"))) if pdf else "","venue":"IJCAI","categories":[],"identifiers":{},"metadata":{"official_index":list_url}})
    if rows:
        selected=rows[:limit] if limit else rows
        with ThreadPoolExecutor(max_workers=max(1,min(3 if limit else 8,len(selected)))) as pool:list(pool.map(_detail,selected))
        return finish(spec,selected,adapter="ijcai_proceedings",requests=requests,proof="official_ijcai_index_exhausted_and_all_details_enriched",discovered_count=len(rows))
    url=f"https://{year}.ijcai.org/accepted-papers/"; accepted=response(url); requests.append(receipt(accepted)); rows=_accepted(accepted.text,year,url); selected=rows[:limit] if limit else rows
    return finish(spec,selected,adapter="ijcai_accepted_papers",requests=requests,proof="official_ijcai_accepted_papers_page_exhausted",discovered_count=len(rows))
def pdf_candidates(paper:dict[str,Any]):
    rows=explicit_pdf(paper,"ijcai_official_pdf",SOURCE)
    for year,pid in re.findall(r"ijcai\.org/proceedings/(\d{4})/(\d+)",values_blob(paper)):
        rows.append({"url":f"https://www.ijcai.org/proceedings/{year}/{int(pid):04d}.pdf","kind":"ijcai_official_pdf","official_source":SOURCE})
    return list({r["url"]:r for r in rows}.values())
CHANNEL=Channel(ID,"conference",fetch_metadata,2,8,4,SOURCE,complete_abstract_catalog,pdf_candidates)
