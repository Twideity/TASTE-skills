from __future__ import annotations
import re
from typing import Any
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from .base import Channel
from .conference_common import complete_abstract_catalog
from .runtime import AuthoritativeEmptyCatalog, checkpointed_details, clean, finish, looks_like_title, response, worker_count
from .shared import explicit_pdf
from ..http import receipt
ID="aaai"; SOURCE="AAAI Proceedings"

def _key(v): return re.sub(r"[^a-z0-9]+"," ",clean(v).lower()).strip()
def _issues(year):
    archive="https://ojs.aaai.org/index.php/AAAI/issue/archive"; links=[]; receipts=[]; next_url=archive; seen_pages=set()
    while next_url and next_url not in seen_pages:
        if len(seen_pages)>=200: raise RuntimeError("AAAI OJS archive pagination exceeded 200 pages")
        seen_pages.add(next_url); r=response(next_url); receipts.append(receipt(r)); s=BeautifulSoup(r.text,"html.parser"); found=0
        for a in s.select("a.title[href], .obj_issue_summary a[href], a[href*='/issue/view/']"):
            label=clean(a.get_text(" ",strip=True)); context=clean(a.parent.get_text(" ",strip=True) if a.parent else label)
            if str(year) not in f"{label} {context}": continue
            pair=(label or str(year),urljoin(r.url,str(a.get("href"))))
            if pair not in links: links.append(pair); found+=1
        if links and not found: break
        next_link=s.select_one("a.next[href]"); next_url=urljoin(r.url,str(next_link.get("href"))) if next_link else ""
    return links,receipts
def _detail(row):
    r=response(row["url"],timeout=45); s=BeautifulSoup(r.text,"html.parser")
    node=s.select_one(".item.abstract .value, section.item.abstract, #abstract"); row["abstract"]=clean(node.get_text(" ",strip=True) if node else "")
    pdf=s.select_one('meta[name="citation_pdf_url"]'); row["pdf_url"]=urljoin(r.url,clean(pdf.get("content"))) if pdf else row.get("pdf_url","")
    doi=s.select_one('meta[name="citation_doi"]')
    if doi: row.setdefault("identifiers",{})["doi"]=clean(doi.get("content"))
    row.setdefault("metadata",{})["detail_receipt"]=receipt(r)
def fetch_metadata(spec):
    year=int(spec["years"][0]); issues,requests=_issues(year)
    if not issues: raise AuthoritativeEmptyCatalog(f"AAAI OJS has no published issue for {year}")
    rows=[]; seen=set(); discovered_count=0
    for label,url in issues:
        r=response(url); requests.append(receipt(r)); s=BeautifulSoup(r.text,"html.parser")
        articles=s.select(".obj_article_summary"); discovered_count+=len(articles)
        for article in articles:
            a=article.select_one("h3.title a[href], .title a[href]"); title=clean(a.get_text(" ",strip=True) if a else ""); detail=urljoin(url,str(a.get("href"))) if a else ""
            if not looks_like_title(title) or _key(title) in seen: continue
            seen.add(_key(title)); authors=article.select_one(".authors")
            rows.append({"title":title,"abstract":"","authors":[clean(authors.get_text(" ",strip=True))] if authors else [],"published":f"{year}-01-01","year":year,"url":detail,"pdf_url":"","venue":"AAAI","categories":[],"identifiers":{},"metadata":{"aaai_issue":label,"official_issue_url":url}})
    checkpointed_details(spec,rows,adapter="aaai_ojs",enrich=_detail,workers=worker_count(spec,8))
    return finish(spec,rows,adapter="aaai_ojs",requests=requests,proof="official_aaai_ojs_issues_exhausted_and_all_details_enriched",discovered_count=discovered_count)
def pdf_candidates(paper:dict[str,Any]): return explicit_pdf(paper,"aaai_ojs_pdf",SOURCE)
CHANNEL=Channel(ID,"conference",fetch_metadata,2,8,3,SOURCE,complete_abstract_catalog,pdf_candidates)
