from __future__ import annotations

import html
import hashlib
import json
import os
import shutil
import re
import time
import xml.etree.ElementTree as ET
import calendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from bs4 import BeautifulSoup

from .credentials import openreview_settings
from .http import ServiceRequestDeferred, bounded_request_policy, get, receipt, service_call
from .storage import DATA_ROOT, METADATA_CACHE_ROOT, now_iso, read_json, require_run, stable_hash, update_run, write_json
from .channels import channel_for_spec


ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
DEFAULT_ARXIV_AI_CATEGORIES = ("cs.AI", "cs.LG", "stat.ML", "cs.CL", "cs.CV", "cs.IR", "cs.RO", "eess.SY", "cs.MA", "cs.NE")
BIORXIV_RECHECK_DAYS = 3
BIORXIV_RECHECK_MAX_AGE_DAYS = 1.0
PRIORITY_VENUE_NAMES = {
    "neurips": "NeurIPS", "nips": "NeurIPS", "iclr": "ICLR", "icml": "ICML",
    "kdd": "KDD", "sigkdd": "KDD", "sigir": "SIGIR", "cikm": "CIKM",
    "www": "WWW", "aaai": "AAAI", "iccv": "ICCV", "cvpr": "CVPR",
    "acl": "ACL", "ijcai": "IJCAI", "eccv": "ECCV", "emnlp": "EMNLP",
}
KNOWN_CONFERENCE_RELEASE_DATES = {
    ("ICLR", 2026): "2026-04-23", ("ICLR", 2025): "2025-04-24",
    ("NEURIPS", 2026): "2026-12-06", ("NEURIPS", 2025): "2025-12-02",
    ("ICML", 2026): "2026-05-08", ("ICML", 2025): "2025-07-13",
    ("KDD", 2026): "2026-08-09", ("KDD", 2025): "2025-08-03",
    ("SIGIR", 2026): "2026-07-20", ("SIGIR", 2025): "2025-07-13",
    ("CIKM", 2026): "2026-11-09", ("WWW", 2026): "2026-04-13",
    ("AAAI", 2026): "2026-01-20", ("CVPR", 2026): "2026-06-03",
    ("ICCV", 2026): "2026-12-31", ("ECCV", 2026): "2026-09-08",
    ("ACL", 2026): "2026-07-05", ("IJCAI", 2026): "2026-08-15",
    ("EMNLP", 2026): "2026-11-01",
}
DEFAULT_VENUES = [
    {"id": "neurips", "name": "NeurIPS", "aliases": ["NIPS"], "adapter": "neurips_official", "fallback_adapters": ["openreview"], "query": "NeurIPS", "openreview_venue_id_template": "NeurIPS.cc/{year}/Conference", "require_complete_abstracts": True, "require_official_categories": False, "dblp_volume_template": "https://dblp.org/db/conf/nips/neurips{year}.xml"},
    {"id": "iclr", "name": "ICLR", "aliases": [], "adapter": "openreview", "query": "ICLR", "openreview_venue_id_template": "ICLR.cc/{year}/Conference", "require_complete_abstracts": True, "require_official_categories": True},
    {"id": "icml", "name": "ICML", "aliases": [], "adapter": "openreview", "fallback_adapters": ["icml_official"], "query": "ICML", "openreview_venue_id_template": "ICML.cc/{year}/Conference", "require_complete_abstracts": True, "require_official_categories": True, "dblp_volume_template": "https://dblp.org/db/conf/icml/icml{year}.xml"},
    {"id": "kdd", "name": "KDD", "aliases": ["SIGKDD"], "adapter": "acm_enriched", "query": "KDD", "require_complete_abstracts": True},
    {"id": "sigir", "name": "SIGIR", "aliases": [], "adapter": "acm_enriched", "query": "SIGIR", "require_complete_abstracts": True},
    {"id": "cikm", "name": "CIKM", "aliases": [], "adapter": "acm_enriched", "query": "CIKM", "require_complete_abstracts": True},
    {"id": "www", "name": "WWW", "aliases": ["The Web Conference", "WebConf"], "adapter": "acm_enriched", "query": "WWW", "require_complete_abstracts": True},
    {"id": "aaai", "name": "AAAI", "aliases": [], "adapter": "aaai_ojs", "query": "AAAI", "require_complete_abstracts": True},
    {"id": "iccv", "name": "ICCV", "aliases": [], "adapter": "cvf_openaccess", "query": "ICCV", "require_complete_abstracts": True},
    {"id": "cvpr", "name": "CVPR", "aliases": [], "adapter": "cvf_openaccess", "query": "CVPR", "require_complete_abstracts": True},
    {"id": "acl", "name": "ACL", "aliases": [], "adapter": "acl_anthology", "query": "ACL", "require_complete_abstracts": True},
    {"id": "ijcai", "name": "IJCAI", "aliases": [], "adapter": "ijcai_proceedings", "query": "IJCAI", "require_complete_abstracts": True},
    {"id": "eccv", "name": "ECCV", "aliases": [], "adapter": "eccv_virtual", "fallback_adapters": ["openreview"], "openreview_venue_id_template": "thecvf.com/ECCV/{year}/Conference", "openreview_venue_value_template": "ECCV {year}", "query": "ECCV", "require_complete_abstracts": True},
    {"id": "emnlp", "name": "EMNLP", "aliases": [], "adapter": "acl_anthology", "query": "EMNLP", "require_complete_abstracts": True},
    {"id": "recomb", "name": "RECOMB", "aliases": [], "adapter": "dblp", "query": "RECOMB"},
    {"id": "ismb", "name": "ISMB", "aliases": [], "adapter": "dblp", "query": "ISMB"},
]


def clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def date_text(value: Any) -> str:
    text = clean(value)[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def paper_identity(paper: dict[str, Any]) -> str:
    identifiers = paper.get("identifiers") if isinstance(paper.get("identifiers"), dict) else {}
    for key in ("doi", "arxiv_id", "openreview_id", "pmcid"):
        value = clean(identifiers.get(key) or paper.get(key)).lower()
        if value:
            return f"{key}:{value}"
    title = re.sub(r"[^a-z0-9]+", "", clean(paper.get("title")).lower())
    authors = paper.get("authors") if isinstance(paper.get("authors"), list) else []
    first_author = re.sub(r"[^a-z0-9]+", "", clean(authors[0] if authors else "").lower())
    year = clean(paper.get("year") or date_text(paper.get("published"))[:4])
    return f"title:{title}|author:{first_author}|year:{year}"


def normalize(paper: dict[str, Any], source_type: str) -> dict[str, Any]:
    row = dict(paper)
    row["title"] = clean(row.get("title"))
    row["abstract"] = clean(row.get("abstract"))
    authors = row.get("authors")
    if isinstance(authors, str):
        authors = re.split(r"\s*(?:,|;|\band\b)\s*", authors)
    row["authors"] = [clean(item) for item in authors or [] if clean(item)] if isinstance(authors, list) else []
    row["published"] = date_text(row.get("published"))
    if not row.get("year") and row["published"]:
        row["year"] = int(row["published"][:4])
    row["source_type"] = source_type
    row["identity"] = paper_identity(row)
    return row


def _arxiv_query(spec: dict[str, Any]) -> str:
    categories = [clean(item) for item in spec.get("categories") or DEFAULT_ARXIV_AI_CATEGORIES if clean(item)]
    return "(" + " OR ".join(f"cat:{item}" for item in categories) + ")"


def _days(start_date: str, end_date: str) -> list[str]:
    first = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    if first > last:
        raise ValueError("arXiv start_date must not be after end_date")
    return [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]


def _arxiv_day_cache_path(category: str, day: str) -> Path:
    return channel_for_spec({"type": "arxiv"}).day_cache_path(category, day)


def _arxiv_page_stage_path(category: str, start_date: str, end_date: str, offset: int) -> Path:
    return channel_for_spec({"type": "arxiv"}).page_stage_path(category, start_date, end_date, offset)


def _biorxiv_day_cache_path(day: str) -> Path:
    return channel_for_spec({"type": "biorxiv"}).day_cache_path(day)


def _biorxiv_page_stage_path(start_date: str, end_date: str, cursor: int) -> Path:
    return channel_for_spec({"type": "biorxiv"}).page_stage_path(start_date, end_date, cursor)


def _arxiv_month_chunks(days: list[str]) -> list[list[str]]:
    chunks: dict[str, list[str]] = {}
    for day in sorted(days):
        chunks.setdefault(day[:7], []).append(day)
    return list(chunks.values())


def _arxiv_entry(entry: ET.Element) -> dict[str, Any]:
    published = date_text(entry.findtext("atom:published", default="", namespaces=ARXIV_NS))
    entry_url = clean(entry.findtext("atom:id", default="", namespaces=ARXIV_NS))
    arxiv_id = entry_url.rstrip("/").rsplit("/", 1)[-1].split("v", 1)[0]
    links = {link.attrib.get("title") or link.attrib.get("rel") or "": link.attrib.get("href") or "" for link in entry.findall("atom:link", ARXIV_NS)}
    return {
        "title": entry.findtext("atom:title", default="", namespaces=ARXIV_NS),
        "abstract": entry.findtext("atom:summary", default="", namespaces=ARXIV_NS),
        "authors": [author.findtext("atom:name", default="", namespaces=ARXIV_NS) for author in entry.findall("atom:author", ARXIV_NS)],
        "published": published,
        "url": entry_url,
        "pdf_url": links.get("pdf") or f"https://arxiv.org/pdf/{arxiv_id}",
        "venue": "arXiv",
        "categories": [item.attrib.get("term") for item in entry.findall("atom:category", ARXIV_NS) if item.attrib.get("term")],
        "identifiers": {"arxiv_id": arxiv_id, "doi": clean(entry.findtext("arxiv:doi", default="", namespaces=ARXIV_NS))},
    }


def _fetch_arxiv_category_range(category: str, start_date: str, end_date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    search_query = f"cat:{category} AND submittedDate:[{start_date.replace('-', '')}0000 TO {end_date.replace('-', '')}2359]"
    rows: list[dict[str, Any]] = []
    receipts = []
    page_size = 200
    total = None
    offset = 0
    while total is None or offset < total:
        stage_path = _arxiv_page_stage_path(category, start_date, end_date, offset)
        staged = read_json(stage_path, {})
        if isinstance(staged, dict) and staged.get("schema_version") == 1 and staged.get("query") == search_query and staged.get("offset") == offset and isinstance(staged.get("papers"), list):
            page_rows = staged["papers"]
            staged_total = int(staged.get("server_total") or 0)
            if total is not None and total != staged_total:
                raise RuntimeError(f"arXiv staged page total changed for {category} {start_date}..{end_date}")
            total = staged_total
            rows.extend(page_rows)
            receipts.append({"status": "staging_cache_hit", "offset": offset, "count": len(page_rows), "stage_path": str(stage_path)})
            offset += len(page_rows)
            if not page_rows:
                break
            continue
        response = get("https://export.arxiv.org/api/query", params={
            "search_query": search_query,
            "start": offset,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "ascending",
        }, timeout=120)
        receipts.append(receipt(response))
        response.raise_for_status()
        root = ET.fromstring(response.text)
        total_text = root.findtext(f"{{{OPENSEARCH_NS}}}totalResults")
        page_total = int(total_text or 0)
        if total is not None and total != page_total:
            raise RuntimeError(f"arXiv totalResults changed during pagination for {category} {start_date}..{end_date}: {total} -> {page_total}")
        total = page_total
        entries = root.findall("atom:entry", ARXIV_NS)
        page_rows = [_arxiv_entry(entry) for entry in entries]
        write_json(stage_path, {"schema_version": 1, "query": search_query, "category": category, "start_date": start_date, "end_date": end_date, "offset": offset, "server_total": total, "papers": page_rows, "fetched_at": now_iso()})
        rows.extend(page_rows)
        offset += len(page_rows)
        if not entries:
            break
    if total is None or len(rows) != total:
        raise RuntimeError(f"arXiv exhaustive pagination failed for {category}: expected={total}, fetched={len(rows)}")
    return rows, {"status": "complete", "category": category, "query": search_query, "server_total": total, "fetched": len(rows), "exhausted": True, "truncated": False, "exhaustion_proof": "opensearch_total_results_reached", "requests": receipts}


def fetch_arxiv(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raise RuntimeError("Use fetch_arxiv_cached so category/day completeness can be persisted and verified")


def fetch_arxiv_cached(spec: dict[str, Any], *, policy: str, max_age_days: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start_date = date_text(spec.get("start_date"))
    end_date = date_text(spec.get("end_date"))
    if not start_date or not end_date:
        raise ValueError("arXiv requires concrete start_date and end_date")
    days = _days(start_date, end_date)
    today = date.today().isoformat()
    categories = [clean(item) for item in spec.get("categories") or DEFAULT_ARXIV_AI_CATEGORIES if clean(item)]
    all_rows: list[dict[str, Any]] = []
    category_receipts = []
    for category in categories:
        cached_by_day: dict[str, dict[str, Any]] = {}
        missing = []
        for day in days:
            payload = read_json(_arxiv_day_cache_path(category, day), {})
            usable = day < today and isinstance(payload, dict) and payload.get("schema_version") == 2 and payload.get("complete") is True and payload.get("provisional") is not True and (policy == "only" or _cache_fresh(payload, max_age_days))
            if policy == "refresh" or not usable:
                missing.append(day)
            else:
                cached_by_day[day] = payload
        if missing:
            if policy == "only":
                raise FileNotFoundError(f"Missing usable arXiv day shards for {category}: {len(missing)}")
            chunk_receipts = []
            for chunk_days in _arxiv_month_chunks(missing):
                fetched, crawl_receipt = _fetch_arxiv_category_range(category, min(chunk_days), max(chunk_days))
                if int(crawl_receipt.get("server_total") or 0) >= 10000:
                    raise RuntimeError(f"arXiv monthly shard is too large for safe exhaustive pagination: {category} {chunk_days[0][:7]}")
                partitioned = {day: [] for day in chunk_days}
                for row in fetched:
                    published = date_text(row.get("published"))
                    if published in partitioned:
                        partitioned[published].append(normalize(row, "arxiv"))
                for day in chunk_days:
                    closed_day = day < today
                    payload = {
                        "schema_version": 2,
                        "source": "arxiv",
                        "category": category,
                        "date": day,
                        "fetched_at": now_iso(),
                        "complete": closed_day,
                        "provisional": not closed_day,
                        "temporal_status": "closed_day" if closed_day else "open_current_or_future_day",
                        "papers": partitioned[day],
                        "range_exhaustion_proof": crawl_receipt["exhaustion_proof"],
                        "range_start": min(chunk_days),
                        "range_end": max(chunk_days),
                    }
                    write_json(_arxiv_day_cache_path(category, day), payload)
                    cached_by_day[day] = payload
                stage_dir = _arxiv_page_stage_path(category, min(chunk_days), max(chunk_days), 0).parent
                if stage_dir.is_dir():
                    shutil.rmtree(stage_dir)
                chunk_receipts.append({**crawl_receipt, "day_shards_written": len(chunk_days)})
            category_receipts.append({
                "status": "complete",
                "category": category,
                "cache_status": "refreshed",
                "day_shards_written": len(missing),
                "chunks": chunk_receipts,
                "server_total": sum(int(item.get("server_total") or 0) for item in chunk_receipts),
                "fetched": sum(int(item.get("fetched") or 0) for item in chunk_receipts),
                "requests": sum(len(item.get("requests") or []) for item in chunk_receipts),
                "exhausted": True,
                "truncated": False,
                "exhaustion_proof": "all_month_chunks_reached_opensearch_total",
                "closed_day_shards": sum(day < today for day in days),
                "provisional_days": [day for day in days if day >= today],
            })
        else:
            category_receipts.append({"status": "complete", "category": category, "cache_status": "hit", "day_shards": len(days), "closed_day_shards": len(days), "provisional_days": [], "exhausted": True, "truncated": False, "exhaustion_proof": "all_closed_daily_shards_complete"})
        for day in days:
            all_rows.extend(cached_by_day[day].get("papers") or [])
    deduplicated = deduplicate(all_rows)
    return deduplicated, {
        "status": "complete",
        "cache_layout": "metadata/arxiv/<category>/<YYYY-MM-DD>.json",
        "categories": categories,
        "start_date": start_date,
        "end_date": end_date,
        "day_count": len(days),
        "category_day_shards": len(categories) * len(days),
        "raw_count": len(all_rows),
        "count": len(deduplicated),
        "exhausted": True,
        "truncated": False,
        "exhaustion_proof": "all_closed_category_days_complete_current_day_provisional" if any(day >= today for day in days) else "all_closed_category_days_complete",
        "closed_days_complete": all(int(item.get("closed_day_shards") or 0) == sum(day < today for day in days) for item in category_receipts),
        "provisional_days": [day for day in days if day >= today],
        "ignored_task_queries": [clean(item) for item in spec.get("queries") or [] if clean(item)],
        "category_receipts": category_receipts,
    }


def _biorxiv_item(item: dict[str, Any]) -> dict[str, Any]:
    doi = clean(item.get("doi"))
    version = clean(item.get("version") or "1")
    return {
        "title": clean(item.get("title")),
        "abstract": clean(item.get("abstract")),
        "authors": clean(item.get("authors")),
        "published": date_text(item.get("date")),
        "url": f"https://www.biorxiv.org/content/{doi}v{version}",
        "pdf_url": f"https://www.biorxiv.org/content/{doi}v{version}.full.pdf",
        "venue": "bioRxiv",
        "category": clean(item.get("category")),
        "version": version,
        "identifiers": {"doi": doi, "biorxiv_version": version},
    }


def _fetch_biorxiv_range(start_date: str, end_date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    receipts = []
    cursor = 0
    server_total: int | None = None
    exhausted = False
    while True:
        stage_path = _biorxiv_page_stage_path(start_date, end_date, cursor)
        staged = read_json(stage_path, {})
        if isinstance(staged, dict) and staged.get("schema_version") == 1 and staged.get("start_date") == start_date and staged.get("end_date") == end_date and staged.get("cursor") == cursor and isinstance(staged.get("collection"), list):
            collection = staged["collection"]
            page_total = staged.get("server_total")
            page_receipt = staged.get("request_receipt") or {"status": "staged"}
        else:
            response = get(f"https://api.biorxiv.org/details/biorxiv/{start_date}/{end_date}/{cursor}/json")
            page_receipt = receipt(response)
            response.raise_for_status()
            payload = response.json()
            collection = payload.get("collection") if isinstance(payload, dict) else []
            messages = payload.get("messages") if isinstance(payload, dict) else []
            page_total = int((messages[0] if messages else {}).get("total") or 0) if isinstance(messages, list) else 0
            if not isinstance(collection, list):
                raise RuntimeError(f"bioRxiv returned an invalid collection for {start_date}..{end_date} cursor={cursor}")
            write_json(stage_path, {"schema_version": 1, "start_date": start_date, "end_date": end_date, "cursor": cursor, "server_total": page_total, "collection": collection, "request_receipt": page_receipt, "fetched_at": now_iso()})
        receipts.append(page_receipt)
        if page_total is not None:
            page_total = int(page_total)
            if server_total is None:
                server_total = page_total
            elif page_total != server_total:
                # A changing total is safe only when it grows while the source is still open.
                server_total = max(server_total, page_total)
        if not isinstance(collection, list) or not collection:
            exhausted = True
            break
        for item in collection:
            if isinstance(item, dict):
                rows.append(_biorxiv_item(item))
        cursor += len(collection)
        if server_total is not None and cursor >= server_total:
            exhausted = True
            break
    if server_total is not None and cursor < server_total:
        raise RuntimeError(f"bioRxiv exhaustive pagination failed for {start_date}..{end_date}: expected={server_total}, scanned={cursor}")
    return rows, {
        "status": "complete",
        "requests": receipts,
        "count": len(rows),
        "server_total": server_total,
        "server_total_scanned": cursor,
        "exhausted": exhausted,
        "truncated": False,
        "next_cursor": None,
        "exhaustion_proof": "biorxiv_cursor_reached_server_total" if server_total is not None and cursor >= server_total else "biorxiv_empty_final_page",
    }


def fetch_biorxiv(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start_date = date_text(spec.get("start_date"))
    end_date = date_text(spec.get("end_date"))
    if not start_date or not end_date:
        raise ValueError("bioRxiv requires concrete start_date and end_date")
    return _fetch_biorxiv_range(start_date, end_date)


def _biorxiv_latest_versions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        normalized = normalize(row, "biorxiv")
        key = normalized["identity"]
        current = selected.get(key)
        try:
            version = int(clean(normalized.get("version") or "0"))
        except ValueError:
            version = 0
        try:
            current_version = int(clean((current or {}).get("version") or "0"))
        except ValueError:
            current_version = 0
        if current is None or (version, clean(normalized.get("published")), len(clean(normalized.get("abstract")))) > (current_version, clean(current.get("published")), len(clean(current.get("abstract")))):
            selected[key] = normalized
    return list(selected.values())


def fetch_biorxiv_cached(spec: dict[str, Any], *, policy: str, max_age_days: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start_date = date_text(spec.get("start_date"))
    end_date = date_text(spec.get("end_date"))
    if not start_date or not end_date:
        raise ValueError("bioRxiv requires concrete start_date and end_date")
    days = _days(start_date, end_date)
    today_date = date.today()
    today = today_date.isoformat()
    recent_cutoff = (today_date - timedelta(days=BIORXIV_RECHECK_DAYS)).isoformat()
    cached_by_day: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for day in days:
        payload = read_json(_biorxiv_day_cache_path(day), {})
        proven = isinstance(payload, dict) and payload.get("schema_version") == 2 and payload.get("complete") is True and payload.get("provisional") is not True and payload.get("exhausted") is True and payload.get("truncated") is False and bool(payload.get("exhaustion_proof"))
        recent_fresh = day < recent_cutoff or _cache_fresh(payload, min(max_age_days, BIORXIV_RECHECK_MAX_AGE_DAYS))
        usable = day < today and proven and (policy == "only" or recent_fresh)
        if policy == "refresh" or not usable:
            missing.append(day)
        else:
            cached_by_day[day] = payload
    chunk_receipts: list[dict[str, Any]] = []
    if missing:
        if policy == "only":
            raise FileNotFoundError(f"Missing usable bioRxiv day shards: {len(missing)}")
        for chunk_days in _arxiv_month_chunks(missing):
            chunk_start, chunk_end = min(chunk_days), max(chunk_days)
            fetched, crawl_receipt = _fetch_biorxiv_range(chunk_start, chunk_end)
            partitioned = {day: [] for day in chunk_days}
            for row in fetched:
                published = date_text(row.get("published"))
                if published in partitioned:
                    partitioned[published].append(row)
            for day in chunk_days:
                closed_day = day < today
                payload = {
                    "schema_version": 2,
                    "source": "biorxiv",
                    "date": day,
                    "fetched_at": now_iso(),
                    "complete": closed_day,
                    "provisional": not closed_day,
                    "temporal_status": "closed_day" if closed_day else "open_current_or_future_day",
                    "server_total": len(partitioned[day]),
                    "exhausted": True,
                    "truncated": False,
                    "exhaustion_proof": crawl_receipt["exhaustion_proof"],
                    "range_start": chunk_start,
                    "range_end": chunk_end,
                    "papers": partitioned[day],
                }
                write_json(_biorxiv_day_cache_path(day), payload)
                cached_by_day[day] = payload
            stage_dir = _biorxiv_page_stage_path(chunk_start, chunk_end, 0).parent
            if stage_dir.is_dir():
                shutil.rmtree(stage_dir)
            chunk_receipts.append({**crawl_receipt, "day_shards_written": len(chunk_days)})
    rows = [paper for day in days for paper in (cached_by_day[day].get("papers") or [])]
    selected = _biorxiv_latest_versions(rows)
    return selected, {
        "status": "complete",
        "cache_layout": "metadata/biorxiv/<YYYY-MM-DD>.json",
        "start_date": start_date,
        "end_date": end_date,
        "day_count": len(days),
        "day_shards": len(days),
        "cache_status": "refreshed" if missing else "hit",
        "day_shards_written": len(missing),
        "raw_count": len(rows),
        "count": len(selected),
        "exhausted": True,
        "truncated": False,
        "exhaustion_proof": "all_closed_days_complete_current_day_provisional" if any(day >= today for day in days) else "all_closed_days_complete",
        "closed_days_complete": all(day >= today or cached_by_day[day].get("complete") is True for day in days),
        "provisional_days": [day for day in days if day >= today],
        "recheck_days": BIORXIV_RECHECK_DAYS,
        "recheck_max_age_days": BIORXIV_RECHECK_MAX_AGE_DAYS,
        "ignored_task_queries": [clean(item) for item in spec.get("queries") or [] if clean(item)],
        "ignored_task_categories": [clean(item) for item in spec.get("categories") or [] if clean(item)],
        "chunks": chunk_receipts,
    }


def fetch_dblp_venue(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    venue_name = clean(spec.get("venue") or spec.get("query") or spec.get("venue_id"))
    years = [int(item) for item in spec.get("years") or []]
    if not venue_name or not years:
        raise ValueError("DBLP venue source requires venue/query and years")
    template = clean(spec.get("dblp_volume_url") or spec.get("dblp_volume_template"))
    if not template:
        raise ValueError("Full DBLP venue crawl requires dblp_volume_url or dblp_volume_template")
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    probe_limit = max(0, int(spec.get("_probe_limit") or 0))
    for year in years:
        url = template.format(year=year) if "{year}" in template else template
        response = None
        errors = []
        for attempt in range(1, 4):
            try:
                response = get(url, timeout=120)
                response.raise_for_status()
                break
            except Exception as exc:
                errors.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)[:300]})
                if attempt < 3:
                    time.sleep(attempt * 2)
        if response is None or not response.ok:
            raise RuntimeError(f"Unable to download complete DBLP venue volume after 3 attempts: {url}; errors={errors}")
        receipts.append(receipt(response))
        receipts[-1]["retry_errors"] = errors
        root = ET.fromstring(response.content)
        year_rows = 0
        for record in root.iter("inproceedings"):
            record_year = clean(record.findtext("year"))
            if record_year and record_year != str(year):
                continue
            authors = [clean("".join(author.itertext())) for author in record.findall("author")]
            title_node = record.find("title")
            title = clean("".join(title_node.itertext()) if title_node is not None else "")
            urls = [clean("".join(node.itertext())) for node in record.findall("ee") if clean("".join(node.itertext()))]
            doi = next((match.group(1).rstrip(".,)") for value in urls if (match := re.search(r"doi\.org/(10\.\d{4,9}/[^\s]+)", value, re.I))), "")
            key = clean(record.attrib.get("key"))
            rows.append({
                "title": title,
                "abstract": "",
                "authors": authors,
                "published": f"{year}-01-01",
                "year": year,
                "url": f"https://dblp.org/rec/{key}" if key else (urls[0] if urls else ""),
                "pdf_url": next((value for value in urls if value.lower().endswith(".pdf")), ""),
                "venue": clean(record.findtext("booktitle")) or venue_name,
                "identifiers": {"doi": doi, "dblp_key": key},
            })
            year_rows += 1
            if probe_limit and len(rows) >= probe_limit:
                break
        receipts[-1]["parsed_inproceedings"] = year_rows
        if probe_limit and len(rows) >= probe_limit:
            break
    if probe_limit:
        return rows[:probe_limit], {
            "status": "sample_complete" if rows else "empty",
            "probe_only": True,
            "complete_catalog": False,
            "exhausted": False,
            "truncated": bool(rows),
            "exhaustion_proof": "probe_sample_only_not_a_complete_catalog",
            "requests": receipts,
            "count": min(len(rows), probe_limit),
            "sample_limit": probe_limit,
            "metadata_sample_complete": bool(rows),
            "adapter": "dblp",
        }
    audit = venue_metadata_audit(rows)
    if not audit["metadata_completeness_ok"]:
        raise RuntimeError(f"DBLP venue metadata completeness audit failed: {audit}")
    return rows, {"status": "complete", "complete_catalog": True, "exhaustion_proof": "complete_dblp_volume_xml", "requests": receipts, "count": len(rows), "metadata_audit": audit}


def _regular_venue_year(venue_id: str, year: int) -> bool:
    if venue_id == "iccv":
        return year % 2 == 1
    if venue_id == "eccv":
        return year % 2 == 0
    return True


def _venue_year_candidates(spec: dict[str, Any], *, as_of: date | None = None, max_backfill_years: int = 3) -> tuple[list[int], list[str]]:
    years = [int(item) for item in spec.get("years") or []]
    if len(years) != 1:
        raise ValueError("Venue source requires exactly one requested year")
    requested = years[0]
    venue_id = clean(spec.get("venue_id")).lower()
    venue_name = PRIORITY_VENUE_NAMES.get(venue_id, clean(spec.get("venue") or venue_id).upper())
    cutoff = as_of or date.today()
    reasons: list[str] = []
    out: list[int] = []
    for candidate in range(requested, requested - max(0, max_backfill_years) - 1, -1):
        if not _regular_venue_year(venue_id, candidate):
            reasons.append(f"{venue_name} {candidate} has no regular proceedings edition")
            continue
        release_text = KNOWN_CONFERENCE_RELEASE_DATES.get((venue_name.upper(), candidate))
        if release_text and date.fromisoformat(release_text) > cutoff:
            reasons.append(
                f"{venue_name} {candidate} archival proceedings are expected after {cutoff.isoformat()}, "
                "but live official, OpenReview, accepted-paper, virtual, ACM, and DBLP channels must still be probed"
            )
        out.append(candidate)
    # If no authoritative release signal is known, retain the requested year
    # first and let source availability decide whether to backfill.
    if not out:
        for candidate in range(requested, requested - max(0, max_backfill_years) - 1, -1):
            if _regular_venue_year(venue_id, candidate):
                out.append(candidate)
    return out, reasons


def _fetch_venue_exact(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    adapter = clean(spec.get("adapter")).lower()
    venue_id = clean(spec.get("venue_id"))
    known = next((item for item in DEFAULT_VENUES if item["id"] == venue_id), None)
    merged = dict(known or {})
    merged.update({key: value for key, value in spec.items() if value not in (None, "", [])})
    adapter = adapter or clean(merged.get("adapter")).lower() or "dblp"
    merged.setdefault("venue", merged.get("name") or merged.get("query") or venue_id)
    if known:
        rows, details = channel_for_spec(merged).fetch_metadata(merged)
        minimum_catalog_records = max(1, int(merged.get("minimum_catalog_records") or 1))
        if len(rows) < minimum_catalog_records:
            raise RuntimeError(
                f"{venue_id} channel catalog has only {len(rows)} records, below "
                f"minimum_catalog_records={minimum_catalog_records}"
            )
        return rows, details
    if adapter == "openreview":
        rows, details = fetch_openreview_venue(merged)
        minimum_catalog_records = max(1, int(merged.get("minimum_catalog_records") or 1))
        if len(rows) < minimum_catalog_records:
            raise RuntimeError(
                f"OpenReview venue catalog has only {len(rows)} records, below the required "
                f"minimum_catalog_records={minimum_catalog_records}; pagination exhaustion alone does not prove "
                "that an early or partially public conference catalog is complete"
            )
        return rows, details
    if adapter == "dblp":
        return fetch_dblp_venue(merged)
    if adapter in {"neurips_official", "icml_official", "acm_enriched", "aaai_ojs", "cvf_openaccess", "acl_anthology", "ijcai_proceedings", "eccv_virtual"}:
        rows, details = channel_for_spec(merged).fetch_metadata(merged)
        normalized = [normalize(row, "venue") for row in rows]
        try:
            minimum_catalog_records = max(1, int(merged.get("minimum_catalog_records") or 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("minimum_catalog_records must be a positive integer") from exc
        if len(normalized) < minimum_catalog_records:
            raise RuntimeError(
                f"Official venue catalog has only {len(normalized)} records, below the required "
                f"minimum_catalog_records={minimum_catalog_records}; pagination exhaustion alone does not prove "
                "that an early or partially public conference catalog is complete"
            )
        audit = venue_metadata_audit(
            normalized,
            require_official_categories=bool(merged.get("require_official_categories")),
        )
        if not audit["metadata_completeness_ok"]:
            raise RuntimeError(f"Official venue metadata completeness audit failed: {audit}")
        details["metadata_audit"] = audit
        return normalized, details
    raise ValueError(f"Unsupported venue adapter: {adapter}")


def fetch_venue(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested_year = int((spec.get("years") or [0])[0])
    # The year must already have been resolved by probe_venue.  Formal
    # acquisition is exact-year and must never hide a failure by crawling an
    # older edition or reusing an older cache.
    candidates, reasons = [requested_year], []
    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None
    for year in candidates:
        known = next((item for item in DEFAULT_VENUES if item["id"] == clean(spec.get("venue_id"))), {})
        primary_adapter = clean(spec.get("adapter") or known.get("adapter")).lower()
        fallback_adapters = [clean(value).lower() for value in known.get("fallback_adapters") or []]
        adapters = ([primary_adapter] if known else list(dict.fromkeys(
            item for item in [primary_adapter, *fallback_adapters] if item
        ))) or [""]
        for adapter in adapters:
            candidate = dict(spec)
            candidate["years"] = [year]
            if adapter:
                candidate["adapter"] = adapter
            try:
                rows, details = _fetch_venue_exact(candidate)
                attempts.append({"year": year, "status": "available" if rows else "empty", "count": len(rows), "adapter": details.get("adapter") or adapter})
                if not rows:
                    continue
                details.update({
                    "requested_years": [requested_year],
                    "effective_years": [year],
                    "year_fallback": year != requested_year,
                    "year_fallback_reason": " ".join(reasons + ([f"using latest available {clean(spec.get('venue_id')).upper()} year {year}"] if year != requested_year else [])),
                    "year_attempts": attempts,
                })
                return rows, details
            except Exception as exc:
                last_error = exc
                attempts.append({"year": year, "adapter": adapter, "status": "error", "error_type": type(exc).__name__, "message": str(exc)[:500]})
                reasons.append(f"{year} {adapter or 'default'} source unavailable: {type(exc).__name__}: {str(exc)[:160]}")
    if last_error:
        raise RuntimeError(f"No usable venue year for requested {requested_year}; attempts={attempts}") from last_error
    raise RuntimeError(f"No usable venue year for requested {requested_year}; attempts={attempts}")


def _openreview_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def fetch_openreview_venue(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import openreview

    years = [int(item) for item in spec.get("years") or []]
    venue_name = clean(spec.get("venue") or spec.get("query") or spec.get("venue_id") or "ICLR")
    if not years:
        raise ValueError("OpenReview venue source requires one resolved year")
    settings = openreview_settings()
    try:
        client, login_errors = service_call(
            "openreview",
            lambda: openreview.api.OpenReviewClient(
                baseurl="https://api2.openreview.net",
                username=settings["username"] or None,
                password=settings["password"] or None,
            ),
            max_attempts=5,
        )
    except Exception as exc:
        raise RuntimeError(
            f"OpenReview official client initialization failed after shared-channel retries: "
            f"{type(exc).__name__}: {str(exc)[:300]}"
        ) from exc
    rows: list[dict[str, Any]] = []
    requests_info = []
    probe_limit = max(0, int(spec.get("_probe_limit") or 0))
    # A three-record diagnostic sample is sufficient to inspect field quality,
    # but it cannot distinguish a real conference catalog from an early
    # OpenReview venue containing only a handful of public notes.  During a
    # probe, inspect at most ten notes while still returning only three
    # diagnostic samples.  This remains bounded and prevents false latest-year
    # resolution such as ECCV 2026's four preliminary records.
    probe_discovery_limit = max(probe_limit, 10) if probe_limit else 0
    for year in years:
        venue_id = clean(spec.get("openreview_venue_id"))
        if not venue_id:
            template = clean(spec.get("openreview_venue_id_template")) or f"{venue_name}.cc/{{year}}/Conference"
            venue_id = template.format(year=year)
        content_queries = [{"venueid": venue_id}]
        venue_value_template = clean(spec.get("openreview_venue_value_template"))
        if venue_value_template:
            content_queries.append({"venue": venue_value_template.format(year=year)})
        for content_query in content_queries:
            offset = 0
            rows_before_query = len(rows)
            while True:
                page_limit = min(1000, probe_discovery_limit - len(rows)) if probe_limit else 1000
                if page_limit <= 0:
                    break
                notes, retry_errors = service_call(
                    "openreview",
                    lambda content_query=content_query, page_limit=page_limit, offset=offset: client.get_notes(content=content_query, limit=page_limit, offset=offset),
                    max_attempts=5,
                )
                notes = notes or []
                requests_info.append({"venue_id": venue_id, "content_query": content_query, "offset": offset, "limit": page_limit, "count": len(notes), "authenticated": settings["authenticated"], "retry_errors": retry_errors})
                if not notes:
                    break
                for note in notes:
                    content = note.content if isinstance(note.content, dict) else {}
                    note_id = clean(note.id)
                    authors = _openreview_value(content.get("authors")) or []
                    timestamp = getattr(note, "pdate", None) or getattr(note, "cdate", None)
                    published = datetime.fromtimestamp(timestamp / 1000, timezone.utc).date().isoformat() if timestamp else f"{year}-01-01"
                    rows.append({
                        "title": _openreview_value(content.get("title")),
                        "abstract": _openreview_value(content.get("abstract")),
                        "authors": authors if isinstance(authors, list) else clean(authors),
                        "published": published,
                        "year": year,
                        "url": f"https://openreview.net/forum?id={note_id}",
                        "pdf_url": f"https://openreview.net/pdf?id={note_id}",
                        "venue": venue_name,
                        "categories": _openreview_value(content.get("primary_area")) or _openreview_value(content.get("subject_areas")) or [],
                        "keywords": _openreview_value(content.get("keywords")) or [],
                        "presentation_type": _openreview_value(content.get("venue")) or "",
                        "identifiers": {"openreview_id": note_id, "doi": clean(_openreview_value(content.get("doi")))},
                    })
                offset += len(notes)
                if probe_limit and len(rows) >= probe_discovery_limit:
                    break
                if len(notes) < page_limit:
                    break
                time.sleep(2.1)
            if len(rows) > rows_before_query:
                break
        if probe_limit and len(rows) >= probe_discovery_limit:
            break
    if probe_limit:
        samples = rows[:probe_limit]
        missing = sum(not clean(row.get("abstract")) for row in samples)
        return samples, {
            "status": "sample_complete" if samples and not missing else ("sample_partial" if samples else "empty"),
            "probe_only": True,
            "complete_catalog": False,
            "exhausted": False,
            "truncated": bool(samples),
            "exhaustion_proof": "probe_sample_only_not_a_complete_catalog",
            "client": "openreview-py",
            "authenticated": settings["authenticated"],
            "credential_file": str(settings["env_file"]),
            "login_retry_errors": login_errors,
            "requests": requests_info,
            "count": len(samples),
            "discovered_record_count": len(rows),
            "probe_catalog_floor": probe_discovery_limit,
            "sample_limit": probe_limit,
            "missing_sample_abstracts": missing,
            "metadata_sample_complete": bool(samples) and missing == 0,
            "adapter": "openreview",
        }
    audit = venue_metadata_audit(rows, require_official_categories=bool(spec.get("require_official_categories")))
    if not audit["metadata_completeness_ok"]:
        raise RuntimeError(f"OpenReview venue metadata completeness audit failed: {audit}")
    return rows, {
        "status": "complete",
        "complete_catalog": True,
        "exhaustion_proof": "openreview_pagination_reached_final_page",
        "client": "openreview-py",
        "authenticated": settings["authenticated"],
        "credential_file": str(settings["env_file"]),
        "login_retry_errors": login_errors,
        "requests": requests_info,
        "count": len(rows),
        "metadata_audit": audit,
    }


def _conference_abstract_is_real(value: Any) -> bool:
    text = clean(value)
    if not text:
        return False
    lower = text.lower()
    if lower.startswith("correct abstract if needed. retain xml formatting tags"):
        return False
    # ACL Anthology's OpenGraph description is a citation, normally formatted
    # as "Authors. Proceedings/Findings ... YEAR.".  It was previously
    # accepted as an abstract, so reject that exact bibliographic shape at
    # every formal cache boundary as well as fixing the page extractor.
    if re.search(r"\.\s+(?:proceedings|findings)\s+of\s+the\b.*\b(?:19|20)\d{2}\.?$", text, re.I):
        return False
    return True


def venue_metadata_audit(rows: list[dict[str, Any]], *, require_official_categories: bool = False) -> dict[str, Any]:
    count = len(rows)
    identities = [paper_identity(row) for row in rows]
    unique = len(set(identities))
    titled = sum(bool(clean(row.get("title"))) for row in rows)
    authored = sum(bool(row.get("authors")) for row in rows)
    linked = sum(bool(clean(row.get("url") or row.get("pdf_url"))) for row in rows)
    invalid_abstracts = [
        clean(row.get("title"))
        for row in rows
        if clean(row.get("abstract")) and not _conference_abstract_is_real(row.get("abstract"))
    ]
    abstracted = sum(_conference_abstract_is_real(row.get("abstract")) for row in rows)
    categorized = sum(bool(row.get("categories")) for row in rows)
    complete = bool(count and unique and titled == count and authored / count >= 0.95 and linked / count >= 0.95)
    complete = complete and abstracted == count
    if require_official_categories:
        complete = complete and categorized == count
    return {
        "record_count": count,
        "unique_identity_count": unique,
        "duplicate_count": count - unique,
        "title_coverage": round(titled / count, 6) if count else 0.0,
        "author_coverage": round(authored / count, 6) if count else 0.0,
        "link_coverage": round(linked / count, 6) if count else 0.0,
        "abstract_coverage": round(abstracted / count, 6) if count else 0.0,
        "invalid_abstract_count": len(invalid_abstracts),
        "invalid_abstract_examples": invalid_abstracts[:5],
        "category_coverage": round(categorized / count, 6) if count else 0.0,
        "full_abstract_required": True,
        "official_categories_expected": require_official_categories,
        "metadata_completeness_ok": complete,
    }


def _complete_conference_cache(payload: Any, source: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Apply the single formal conference-cache contract at every cache boundary."""
    if not isinstance(payload, dict):
        return False, {}
    receipt_data = payload.get("receipt") if isinstance(payload.get("receipt"), dict) else {}
    rows = payload.get("papers") if isinstance(payload.get("papers"), list) else []
    venue_id = clean(source.get("venue_id") or source.get("venue")).lower()
    audit = venue_metadata_audit(
        rows,
        require_official_categories=venue_id in {"iclr", "icml"},
    )
    complete = (
        receipt_data.get("status") == "complete"
        and receipt_data.get("complete_catalog") is True
        and bool(receipt_data.get("exhaustion_proof"))
        and audit["metadata_completeness_ok"]
    )
    # Older caches could mark a smaller OpenReview subset as the complete
    # NeurIPS catalog after the larger official proceedings pool had only a
    # handful of missing abstracts.  NeurIPS caches must preserve the official
    # pool and use exact-identity fallbacks only to fill fields within it.
    if venue_id in {"neurips", "nips"}:
        complete = complete and clean(receipt_data.get("adapter")) == "neurips_official_papers"
    if venue_id == "eccv":
        complete = complete and clean(receipt_data.get("adapter")) == "eccv_virtual"
    return complete, audit


def _crossref_date(item: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "issued", "created"):
        value = item.get(key)
        parts = (value or {}).get("date-parts") if isinstance(value, dict) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            values = list(parts[0]) + [1, 1]
            try:
                return date(int(values[0]), int(values[1]), int(values[2])).isoformat()
            except ValueError:
                continue
    return ""


def fetch_crossref(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limit = max(1, int(spec.get("limit") or 5000))
    start_date = date_text(spec.get("start_date"))
    end_date = date_text(spec.get("end_date"))
    queries = [clean(item) for item in spec.get("queries") or [] if clean(item)] or [""]
    journals = [clean(item) for item in spec.get("journals") or [] if clean(item)]
    rows = []
    receipts = []
    for query in queries:
        cursor = "*"
        while len(rows) < limit:
            filters = ["type:journal-article"]
            if start_date:
                filters.append(f"from-pub-date:{start_date}")
            if end_date:
                filters.append(f"until-pub-date:{end_date}")
            params = {"filter": ",".join(filters), "rows": min(1000, limit - len(rows)), "cursor": cursor}
            if query:
                params["query.bibliographic"] = query
            response = get("https://api.crossref.org/works", params=params)
            receipts.append(receipt(response))
            response.raise_for_status()
            message = response.json().get("message") or {}
            items = message.get("items") or []
            if not items:
                break
            for item in items:
                containers = item.get("container-title") or []
                container = clean(containers[0] if containers else "")
                if journals and not any(name.lower() in container.lower() for name in journals):
                    continue
                title_values = item.get("title") or []
                abstract = BeautifulSoup(str(item.get("abstract") or ""), "html.parser").get_text(" ")
                doi = clean(item.get("DOI"))
                links = item.get("link") or []
                pdf_url = next((clean(link.get("URL")) for link in links if "pdf" in clean(link.get("content-type")).lower()), "")
                rows.append({
                    "title": title_values[0] if title_values else "",
                    "abstract": abstract,
                    "authors": [" ".join(filter(None, [clean(author.get("given")), clean(author.get("family"))])) for author in item.get("author") or []],
                    "published": _crossref_date(item),
                    "url": clean(item.get("URL")) or (f"https://doi.org/{doi}" if doi else ""),
                    "pdf_url": pdf_url,
                    "venue": container,
                    "identifiers": {"doi": doi},
                })
                if len(rows) >= limit:
                    break
            next_cursor = clean(message.get("next-cursor"))
            if not next_cursor or next_cursor == cursor or len(items) < int(params["rows"]):
                break
            cursor = next_cursor
        if len(rows) >= limit:
            break
    return rows[:limit], {"status": "complete", "requests": receipts, "count": len(rows[:limit])}


FETCHERS: dict[str, Callable[[dict[str, Any]], tuple[list[dict[str, Any]], dict[str, Any]]]] = {
    "arxiv": fetch_arxiv,
    "biorxiv": fetch_biorxiv,
    "venue": fetch_venue,
    "journal": fetch_crossref,
    "nature": fetch_crossref,
    "science": fetch_crossref,
}

WORKFLOW_MODES = {"comprehensive", "focused", "incremental", "metadata_only"}
STOP_AFTER_STAGES = {"metadata", "shortlist", "fulltext", "reading", "recommendation"}
READING_PREFERENCES = {"auto", "external_claude", "codex_fast"}


def _validated_workflow(plan: dict[str, Any], request_scope: dict[str, Any]) -> dict[str, Any]:
    raw = plan.get("workflow")
    if raw is None:
        mode = "comprehensive" if not request_scope["user_specified_time"] and not request_scope["user_specified_channels"] else "focused"
        raw = {"mode": mode}
    if not isinstance(raw, dict):
        raise ValueError("workflow must be an object when provided")
    mode = clean(raw.get("mode") or "comprehensive").lower()
    if mode not in WORKFLOW_MODES:
        raise ValueError(f"workflow.mode must be one of {sorted(WORKFLOW_MODES)}")
    stop_after = clean(raw.get("stop_after") or ("metadata" if mode == "metadata_only" else "recommendation")).lower()
    if stop_after not in STOP_AFTER_STAGES:
        raise ValueError(f"workflow.stop_after must be one of {sorted(STOP_AFTER_STAGES)}")
    reading = clean(raw.get("reading_preference") or "auto").lower()
    if reading not in READING_PREFERENCES:
        raise ValueError(f"workflow.reading_preference must be one of {sorted(READING_PREFERENCES)}")
    if reading == "codex_fast" and raw.get("user_disabled_claude") is not True and raw.get("claude_unavailable") is not True:
        raise ValueError("codex_fast requires user_disabled_claude=true or claude_unavailable=true")
    if reading == "codex_fast" and raw.get("user_disabled_claude") is True:
        preference_scope = clean(raw.get("reading_preference_scope") or "conversation").lower()
        if preference_scope not in {"conversation", "current_turn"}:
            raise ValueError("workflow.reading_preference_scope must be conversation or current_turn")
        raw = {
            **raw,
            "reading_preference_scope": preference_scope,
            "conversation_reading_preference_locked": raw.get("conversation_reading_preference_locked", preference_scope == "conversation"),
        }
    rationale = clean(raw.get("rationale"))
    question = clean(raw.get("research_question") or plan.get("research_question"))
    if mode != "comprehensive" and (not rationale or not question):
        raise ValueError("non-comprehensive workflow requires research_question and workflow.rationale")
    normalized = {**raw, "mode": mode, "research_question": question, "rationale": rationale, "stop_after": stop_after, "reading_preference": reading}
    for key in ("shortlist_target", "final_target"):
        if key in normalized:
            try:
                value = int(normalized[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"workflow.{key} must be a positive integer") from exc
            if value < 1:
                raise ValueError(f"workflow.{key} must be a positive integer")
            normalized[key] = value
    return normalized


def validate_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or not isinstance(plan.get("sources"), list) or not plan["sources"]:
        raise ValueError("Plan requires a non-empty sources array")
    request_scope = plan.get("request_scope")
    if not isinstance(request_scope, dict) or not isinstance(request_scope.get("user_specified_time"), bool) or not isinstance(request_scope.get("user_specified_channels"), bool):
        raise ValueError("Plan requires request_scope with boolean user_specified_time and user_specified_channels")
    as_of = date_text(request_scope.get("as_of_date"))
    if not as_of:
        raise ValueError("Plan requires request_scope.as_of_date as a concrete ISO date")
    request_scope["as_of_date"] = as_of
    workflow = _validated_workflow(plan, request_scope)
    plan["workflow"] = workflow
    decisions = plan.get("channel_decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("Plan requires channel_decisions")
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict) or clean(decision.get("decision")).lower() not in {"include", "exclude"} or not clean(decision.get("channel")) or not clean(decision.get("reason")):
            raise ValueError(f"channel_decisions[{index}] requires channel, include/exclude decision, and reason")
    considered = {clean(item.get("channel")).lower() for item in decisions}
    if workflow["mode"] == "comprehensive":
        for baseline in ("neurips/nips", "iclr", "icml", "arxiv"):
            aliases = {baseline}
            if baseline == "neurips/nips":
                aliases.update({"neurips", "nips"})
            if not considered & aliases:
                raise ValueError(f"Baseline channel not considered: {baseline}")
    for index, source in enumerate(plan["sources"]):
        if not isinstance(source, dict) or clean(source.get("type")).lower() not in FETCHERS:
            raise ValueError(f"sources[{index}] has unsupported type")
        kind = clean(source.get("type")).lower()
        if kind in {"arxiv", "biorxiv"} and (not date_text(source.get("start_date")) or not date_text(source.get("end_date"))):
            raise ValueError(f"sources[{index}] {kind} requires concrete start_date and end_date")
        if kind in {"arxiv", "biorxiv"} and any(key in source for key in ("limit", "max_results", "sample_limit")):
            raise ValueError(f"sources[{index}] {kind} cannot use a result limit; every selected date shard must be complete")
        if kind == "venue":
            if len(source.get("years") or []) != 1:
                raise ValueError(f"sources[{index}] venue requires exactly one resolved year")
            if source.get("complete_catalog") is not True:
                raise ValueError(f"sources[{index}] venue must set complete_catalog=true")
            if source.get("require_complete_abstracts") is False:
                raise ValueError(
                    f"sources[{index}] venue cannot disable abstracts; every formal conference record must have a real abstract"
                )
            source["require_complete_abstracts"] = True
            if any(key in source for key in ("limit", "max_results", "sample_limit")):
                raise ValueError(f"sources[{index}] venue cannot use a result limit; conference metadata must be complete")
            if any(source.get(key) for key in ("queries", "categories", "tracks")):
                raise ValueError(f"sources[{index}] venue cannot apply topic/category/track filters during acquisition")
            if "minimum_catalog_records" in source:
                try:
                    minimum_catalog_records = int(source["minimum_catalog_records"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"sources[{index}].minimum_catalog_records must be a positive integer") from exc
                if minimum_catalog_records < 1:
                    raise ValueError(f"sources[{index}].minimum_catalog_records must be a positive integer")
                source["minimum_catalog_records"] = minimum_catalog_records
    if workflow["mode"] == "comprehensive" and not request_scope["user_specified_time"] and not request_scope["user_specified_channels"]:
        as_of = date_text(request_scope.get("as_of_date"))
        if not as_of:
            raise ValueError("Default research scope requires request_scope.as_of_date")
        as_of_date = date.fromisoformat(as_of)
        month = as_of_date.month - 6
        year = as_of_date.year
        if month <= 0:
            month += 12
            year -= 1
        trailing_start = date(year, month, min(as_of_date.day, calendar.monthrange(year, month)[1])).isoformat()
        baseline_venues = {"neurips", "nips", "iclr", "icml"}
        venue_ids = {clean(source.get("venue_id")).lower() for source in plan["sources"] if clean(source.get("type")).lower() == "venue"}
        if not ({"neurips", "nips"} & venue_ids) or not {"iclr", "icml"}.issubset(venue_ids):
            raise ValueError("Default research scope requires NeurIPS/NIPS, ICLR, and ICML sources")
        arxiv_sources = [source for source in plan["sources"] if clean(source.get("type")).lower() == "arxiv"]
        if len(arxiv_sources) != 1 or date_text(arxiv_sources[0].get("start_date")) != trailing_start or date_text(arxiv_sources[0].get("end_date")) != as_of:
            raise ValueError("Default research scope requires exactly one arXiv source covering the trailing six calendar months")
        for source in plan["sources"]:
            kind = clean(source.get("type")).lower()
            if kind == "venue" and source.get("latest_usable_year") is not True:
                raise ValueError("Every default-scope conference must be resolved to its latest usable metadata year")
        adaptive = [source for source in plan["sources"] if not (clean(source.get("type")).lower() == "venue" and clean(source.get("venue_id")).lower() in baseline_venues) and clean(source.get("type")).lower() != "arxiv"]
        if not 1 <= len(adaptive) <= 3:
            raise ValueError("Default research scope requires one to three topic-adaptive channels")
        for source in adaptive:
            kind = clean(source.get("type")).lower()
            if kind != "venue" and (date_text(source.get("start_date")) != trailing_start or date_text(source.get("end_date")) != as_of):
                raise ValueError("Adaptive non-conference channels must use the same trailing six-month window as arXiv")
    return plan


def _cache_fresh(payload: dict[str, Any], max_age_days: float) -> bool:
    try:
        stamp = datetime.fromisoformat(str(payload.get("fetched_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - stamp).total_seconds() <= max(0.0, max_age_days) * 86400


def _source_cache_path(spec: dict[str, Any], cache_key: str) -> Path:
    kind = re.sub(r"[^a-z0-9._-]+", "_", clean(spec.get("type")).lower()) or "unknown"
    if kind == "venue":
        return channel_for_spec(spec).metadata_cache_path(spec)
    start = date_text(spec.get("start_date")) or "undated"
    end = date_text(spec.get("end_date")) or "undated"
    return METADATA_CACHE_ROOT / kind / f"{start}_{end}_{cache_key[:12]}.json"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _move_preserving(path: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_hash = _file_sha256(path)
    final = destination
    if final.exists():
        if os.path.samefile(path, final):
            return {"source": str(path), "destination": str(final), "sha256": source_hash, "action": "already_canonical"}
        if _file_sha256(final) == source_hash:
            path.unlink()
            return {"source": str(path), "destination": str(final), "sha256": source_hash, "action": "deduplicated"}
        final = destination.with_name(f"{destination.stem}.{source_hash[:12]}{destination.suffix}")
    os.replace(path, final)
    return {"source": str(path), "destination": str(final), "sha256": source_hash, "action": "moved"}


def metadata_cache_inventory() -> dict[str, Any]:
    legacy_finding = METADATA_CACHE_ROOT.parent / "finding-runtime" / "cache" / "finding_cache"
    flat = [path for path in METADATA_CACHE_ROOT.glob("*.json") if re.fullmatch(r"[0-9a-f]{64}\.json", path.name)]
    source_files = [path for path in METADATA_CACHE_ROOT.glob("**/*.json") if "arxiv/.staging" not in path.as_posix() and "/.state/" not in path.as_posix()]
    arxiv_days = [path for path in (METADATA_CACHE_ROOT / "arxiv").glob("*/*.json") if path.parent.name != ".staging"]
    biorxiv_days = [path for path in (METADATA_CACHE_ROOT / "biorxiv").glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", path.name)]
    legacy_files = list(legacy_finding.glob("*.json")) if legacy_finding.is_dir() else []
    return {
        "authoritative_root": str(METADATA_CACHE_ROOT),
        "source_cache_files": len(source_files),
        "arxiv_day_shards": len(arxiv_days),
        "biorxiv_day_shards": len(biorxiv_days),
        "legacy_flat_files": len(flat),
        "legacy_finding_files": len(legacy_files),
        "legacy_finding_path": str(legacy_finding),
        "top_level_directories": sorted(path.name for path in METADATA_CACHE_ROOT.parent.iterdir() if path.is_dir()) if METADATA_CACHE_ROOT.parent.is_dir() else [],
        "unified": not flat and not legacy_files and set(path.name for path in METADATA_CACHE_ROOT.parent.iterdir() if path.is_dir()) <= {"fulltext", "metadata"},
    }


def _recover_conference_caches_from_runs() -> list[dict[str, Any]]:
    recovered = []
    if clean(os.environ.get("RECOMMEND_PAPERS_DISABLE_RUN_CACHE_RECOVERY")).lower() in {"1", "true", "yes"}:
        return recovered
    runs_root = DATA_ROOT / "runs"
    if not runs_root.is_dir():
        return recovered
    for receipt_path in sorted(runs_root.glob("*/source_receipts/*.json"), reverse=True):
        row = read_json(receipt_path, {})
        source = row.get("source") if isinstance(row, dict) and isinstance(row.get("source"), dict) else {}
        if clean(source.get("type")).lower() != "venue" or len(source.get("years") or []) != 1:
            continue
        details = ((row.get("cache") or {}).get("details") or {}) if isinstance(row.get("cache"), dict) else {}
        target = _source_cache_path(source, stable_hash(source))
        if target.is_file():
            existing = read_json(target, {})
            if _complete_conference_cache(existing, source)[0]:
                continue
        metadata = read_json(receipt_path.parent.parent / "metadata.json", {})
        venue_id = clean(source.get("venue_id") or source.get("venue")).lower()
        venue_names = {"iclr": {"iclr"}, "icml": {"icml"}, "neurips": {"neurips", "nips"}}.get(venue_id, {venue_id})
        papers = [paper for paper in metadata.get("papers") or [] if isinstance(paper, dict) and clean(paper.get("venue")).lower() in venue_names]
        if len(papers) != int(row.get("paper_count") or 0) or len(papers) != int(details.get("count") or 0):
            continue
        channel = channel_for_spec(source)
        payload = {"schema_version": channel.metadata_schema, "channel": channel.id, "cache_key": stable_hash(source), "source": source, "fetched_at": clean((row.get("cache") or {}).get("fetched_at")) or now_iso(), "papers": papers, "receipt": details}
        complete, audit = _complete_conference_cache(payload, source)
        if not complete:
            continue
        payload["receipt"]["metadata_audit"] = audit
        write_json(target, payload)
        recovered.append({"venue": venue_id, "year": int(source["years"][0]), "papers": len(papers), "destination": str(target), "source_run": receipt_path.parent.parent.name})
    return recovered


def migrate_metadata_caches() -> dict[str, Any]:
    METADATA_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    moves: list[dict[str, Any]] = []
    removed_invalid_conference_caches: list[str] = []
    for path in sorted(METADATA_CACHE_ROOT.glob("*.json")):
        if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
            continue
        payload = read_json(path, {})
        source = payload.get("source") if isinstance(payload, dict) else {}
        kind = clean(source.get("type")).lower() if isinstance(source, dict) else ""
        destination = _source_cache_path(source if isinstance(source, dict) else {}, path.stem)
        moves.append(_move_preserving(path, destination))
    sources_root = METADATA_CACHE_ROOT / "sources"
    if sources_root.is_dir():
        venue_groups: dict[tuple[str, int], list[tuple[Path, dict[str, Any]]]] = {}
        for path in sorted(sources_root.glob("*/*.json")):
            payload = read_json(path, {})
            source = payload.get("source") if isinstance(payload, dict) and isinstance(payload.get("source"), dict) else {}
            kind = clean(source.get("type")).lower()
            if kind == "venue" and len(source.get("years") or []) == 1:
                venue_groups.setdefault((clean(source.get("venue_id") or source.get("venue")).lower(), int(source["years"][0])), []).append((path, payload))
            else:
                moves.append(_move_preserving(path, _source_cache_path(source, path.stem)))
        for (venue, year), candidates in venue_groups.items():
            complete = [
                (path, payload)
                for path, payload in candidates
                if _complete_conference_cache(payload, payload.get("source") if isinstance(payload.get("source"), dict) else {})[0]
            ]
            if not complete:
                for path, _ in candidates:
                    path.unlink()
                continue
            winner, payload = max(complete, key=lambda item: (str(item[1].get("fetched_at") or ""), len(item[1].get("papers") or [])))
            source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
            channel = channel_for_spec(source)
            payload["schema_version"] = channel.metadata_schema
            payload["channel"] = channel.id
            write_json(winner, payload)
            moves.append(_move_preserving(winner, channel.metadata_cache_path(source)))
            for path, _ in candidates:
                if path.exists():
                    path.unlink()
    legacy_finding = METADATA_CACHE_ROOT.parent / "finding-runtime" / "cache" / "finding_cache"
    if legacy_finding.is_dir():
        for path in sorted(legacy_finding.glob("*.json")):
            path.unlink()
    old_http = METADATA_CACHE_ROOT.parent / "http-state"
    if old_http.is_dir():
        for path in old_http.glob("*.json"):
            service = path.stem
            moves.append(_move_preserving(path, METADATA_CACHE_ROOT / ".state" / "http" / f"{service}.json"))
    for path in list(METADATA_CACHE_ROOT.glob("*/.http-state*.json")):
        label = path.parent.name + ("-" + path.stem.split(".http-state", 1)[-1].lstrip(".") if path.stem != ".http-state" else "")
        moves.append(_move_preserving(path, METADATA_CACHE_ROOT / ".state" / "http" / f"{label}.json"))
    for directory in [path for path in METADATA_CACHE_ROOT.iterdir() if path.is_dir() and path.name in {"generic", "crossref", "openalex", "europepmc"}]:
        shutil.rmtree(directory)
    conference_root = METADATA_CACHE_ROOT / "conference"
    for path in conference_root.glob("*/*.json") if conference_root.is_dir() else []:
        payload = read_json(path, {})
        source = payload.get("source") if isinstance(payload, dict) and isinstance(payload.get("source"), dict) else {}
        try:
            channel = channel_for_spec(source)
        except KeyError:
            removed_invalid_conference_caches.append(str(path))
            path.unlink()
            continue
        if not _complete_conference_cache(payload, source)[0]:
            removed_invalid_conference_caches.append(str(path))
            path.unlink()
            continue
        payload["schema_version"] = channel.metadata_schema
        payload["channel"] = channel.id
        write_json(path, payload)
        moves.append(_move_preserving(path, channel.metadata_cache_path(source)))
    if conference_root.is_dir():
        shutil.rmtree(conference_root)
    for path in (METADATA_CACHE_ROOT / "biorxiv").glob("*.json") if (METADATA_CACHE_ROOT / "biorxiv").is_dir() else []:
        payload = read_json(path, {})
        is_daily = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", path.name))
        if not is_daily or not isinstance(payload, dict) or payload.get("schema_version") != 2 or payload.get("exhausted") is not True or payload.get("truncated") is not False or not payload.get("exhaustion_proof"):
            path.unlink()
    for extra in (METADATA_CACHE_ROOT / "imports", sources_root, METADATA_CACHE_ROOT.parent / "finding-runtime", old_http):
        if extra.exists():
            shutil.rmtree(extra)
    for junk in METADATA_CACHE_ROOT.glob("**/.DS_Store"):
        junk.unlink()
    legacy_arxiv_staging = METADATA_CACHE_ROOT / "arxiv" / ".staging"
    if legacy_arxiv_staging.exists():
        shutil.rmtree(legacy_arxiv_staging)
    legacy_biorxiv_staging = METADATA_CACHE_ROOT / "biorxiv" / ".staging"
    if legacy_biorxiv_staging.exists():
        shutil.rmtree(legacy_biorxiv_staging)
    today = date.today().isoformat()
    for path in (METADATA_CACHE_ROOT / "arxiv").glob("*/*.json"):
        if path.stem < today:
            continue
        payload = read_json(path, {})
        if isinstance(payload, dict):
            payload.update({"complete": False, "provisional": True, "temporal_status": "open_current_or_future_day"})
            write_json(path, payload)
    for path in (METADATA_CACHE_ROOT / "biorxiv").glob("*.json"):
        if path.stem < today:
            continue
        payload = read_json(path, {})
        if isinstance(payload, dict):
            payload.update({"complete": False, "provisional": True, "temporal_status": "open_current_or_future_day"})
            write_json(path, payload)
    for junk in (METADATA_CACHE_ROOT / "migration-manifest.json", METADATA_CACHE_ROOT.parent / ".DS_Store"):
        if junk.is_file():
            junk.unlink()
    old_internal_state = METADATA_CACHE_ROOT / ".state" / "http"
    if old_internal_state.is_dir():
        for path in old_internal_state.glob("*.json"):
            moves.append(_move_preserving(path, DATA_ROOT / "state" / "http" / path.name))
    if (METADATA_CACHE_ROOT / ".state").exists():
        shutil.rmtree(METADATA_CACHE_ROOT / ".state")
    recovered = _recover_conference_caches_from_runs()
    manifest_path = DATA_ROOT / "state" / "metadata-cache-migration.json"
    write_json(manifest_path, {
        "schema_version": 2,
        "migrated_at": now_iso(),
        "moves": moves,
        "removed_invalid_conference_caches": removed_invalid_conference_caches,
        "recovered_conferences": recovered,
    })
    return {
        "status": "complete",
        "moved_or_deduplicated": len(moves),
        "moves": moves,
        "removed_invalid_conference_caches": removed_invalid_conference_caches,
        "recovered_conferences": recovered,
        "inventory": metadata_cache_inventory(),
        "manifest": str(manifest_path),
    }


def fetch_source(spec: dict[str, Any], *, policy: str, max_age_days: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    identity = dict(spec)
    cache_key = stable_hash(identity)
    kind = clean(spec.get("type")).lower()
    if kind == "arxiv":
        papers, arxiv_receipt = channel_for_spec(spec).fetch_metadata(
            spec, policy=policy, max_age_days=max_age_days
        )
        return papers, {"status": "cache_hit" if all(item.get("cache_status") == "hit" for item in arxiv_receipt["category_receipts"]) else "cache_miss", "count": len(papers), "details": arxiv_receipt}
    if kind == "biorxiv":
        papers, biorxiv_receipt = channel_for_spec(spec).fetch_metadata(
            spec, policy=policy, max_age_days=max_age_days
        )
        return papers, {"status": "cache_hit" if biorxiv_receipt["cache_status"] == "hit" else "cache_miss", "count": len(papers), "details": biorxiv_receipt}
    cache_specs = [spec]
    cache_paths = [_source_cache_path(item, stable_hash(item)) for item in cache_specs]
    cache_path = cache_paths[0]
    legacy_path = METADATA_CACHE_ROOT / f"{cache_key}.json"
    if not cache_path.exists() and legacy_path.is_file():
        _move_preserving(legacy_path, cache_path)
    cached = {}
    if policy in {"reuse", "only"}:
        for candidate_path in cache_paths:
            candidate_payload = read_json(candidate_path, {})
            if isinstance(candidate_payload, dict) and candidate_payload.get("papers") and (policy == "only" or _cache_fresh(candidate_payload, max_age_days)):
                cached = candidate_payload
                cache_path = candidate_path
                break
    if policy in {"reuse", "only"} and isinstance(cached, dict) and cached.get("papers"):
        cached_receipt = cached.get("receipt") if isinstance(cached.get("receipt"), dict) else {}
        cache_has_required_coverage = (
            cached_receipt.get("complete_catalog") is True if kind == "venue"
            else cached_receipt.get("exhausted") is True and cached_receipt.get("truncated") is False and bool(cached_receipt.get("exhaustion_proof")) if kind == "biorxiv"
            else True
        )
        if kind == "venue":
            cache_has_required_coverage, _audit = channel_for_spec(spec).validate_cache(cached, spec)
        if cache_has_required_coverage:
            return cached["papers"], {"status": "cache_hit", "cache_path": str(cache_path), "fetched_at": cached.get("fetched_at"), "count": len(cached["papers"]), "details": cached_receipt}
    if policy == "only":
        raise FileNotFoundError(f"No usable metadata cache for source {cache_key}")
    channel = channel_for_spec(spec) if kind == "venue" else None
    papers, source_receipt = (
        channel.fetch_metadata(spec) if channel is not None else FETCHERS[kind](spec)
    )
    if kind == "venue" and source_receipt.get("complete_catalog") is not True:
        raise RuntimeError("Venue adapter did not prove complete-catalog acquisition")
    normalized = [normalize(item, kind) for item in papers if isinstance(item, dict) and clean(item.get("title"))]
    if kind == "venue":
        candidate_payload = {"papers": normalized, "receipt": source_receipt}
        complete, audit = _complete_conference_cache(candidate_payload, spec)
        if not complete:
            raise RuntimeError(f"Conference metadata cannot be cached until every record has a real abstract: {audit}")
        source_receipt["metadata_audit"] = audit
    if kind == "venue" and isinstance(source_receipt.get("effective_years"), list) and len(source_receipt["effective_years"]) == 1:
        effective_spec = dict(spec)
        effective_spec["years"] = [int(source_receipt["effective_years"][0])]
        cache_path = _source_cache_path(effective_spec, stable_hash(effective_spec))
    payload = {
        "schema_version": channel.metadata_schema if channel is not None else 1,
        "channel": channel.id if channel is not None else kind,
        "cache_key": cache_key,
        "source": spec,
        "fetched_at": now_iso(),
        "papers": normalized,
        "receipt": source_receipt,
    }
    write_json(cache_path, payload)
    return normalized, {"status": "cache_miss", "cache_path": str(cache_path), "fetched_at": payload["fetched_at"], "count": len(normalized), "details": source_receipt}


def deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_identity: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["identity"]
        current = by_identity.get(key)
        if current is None or len(clean(row.get("abstract"))) > len(clean(current.get("abstract"))):
            by_identity[key] = row
    return list(by_identity.values())


def catalog(query: str = "") -> dict[str, Any]:
    needle = clean(query).lower()
    venues = [dict(item) for item in DEFAULT_VENUES if not needle or needle in json.dumps(item).lower()]
    return {"query": query, "count": len(venues), "venues": venues, "custom_venue_policy": "Use a built-in official adapter whenever the venue is catalogued. Custom venues may use OpenReview with an official venue ID or DBLP complete-volume XML, but title-only DBLP metadata is not a verified priority-venue corpus."}


def _probe_venue_exact(spec: dict[str, Any], sample_limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch only a bounded exact-year sample; never invoke formal year fallback."""
    venue_id = clean(spec.get("venue_id"))
    known = next((item for item in DEFAULT_VENUES if item["id"] == venue_id), None)
    merged = dict(known or {})
    merged.update({key: value for key, value in spec.items() if value not in (None, "", [])})
    adapter = clean(merged.get("adapter")).lower() or "dblp"
    merged["adapter"] = adapter
    merged["_probe_limit"] = max(1, int(sample_limit or 1))
    merged.setdefault("venue", merged.get("name") or merged.get("query") or venue_id)
    if known:
        rows, details = channel_for_spec(merged).fetch_metadata(merged)
        if adapter == "openreview" and int(details.get("discovered_record_count") or len(rows)) < int(details.get("probe_catalog_floor") or 10):
            raise RuntimeError(
                "authoritative incomplete OpenReview probe catalog: "
                f"discovered={details.get('discovered_record_count') or len(rows)}, floor={details.get('probe_catalog_floor') or 10}"
            )
        return rows, details
    if adapter == "openreview":
        rows, details = fetch_openreview_venue(merged)
        if known and int(details.get("discovered_record_count") or len(rows)) < int(details.get("probe_catalog_floor") or 10):
            raise RuntimeError(
                "authoritative incomplete OpenReview probe catalog: "
                f"discovered={details.get('discovered_record_count') or len(rows)}, floor={details.get('probe_catalog_floor') or 10}"
            )
        return rows, details
    if adapter == "dblp":
        return fetch_dblp_venue(merged)
    return channel_for_spec(merged).fetch_metadata(merged)


def _transient_probe_error(exc: BaseException) -> bool:
    if isinstance(exc, ServiceRequestDeferred):
        return True
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ServiceRequestDeferred):
            return True
        response = getattr(current, "response", None)
        if response is not None and getattr(response, "status_code", None) in {429, 500, 502, 503, 504}:
            return True
        text = f"{type(current).__name__}: {current}".lower()
        if any(marker in text for marker in (
            "429", "rate limit", "ratelimit", "too many requests", "timeout", "timed out",
            "connection", "temporarily", "503", "502", "500", "cooldown", "deferred",
        )):
            return True
        current = current.__cause__ or current.__context__
    return False


def _authoritative_probe_empty(exc: BaseException) -> bool:
    """Classify exact-year archival absence without treating it as a crawler failure."""
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 404:
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in (
        "404 client error",
        "official proceedings index unavailable",
        "has no published issue for",
        "authoritative incomplete openreview probe catalog",
    ))


def probe_venue(
    spec: dict[str, Any],
    start_year: int,
    lookback: int,
    sample_limit: int,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    try:
        probe_wall_timeout = max(5.0, min(300.0, float(os.environ.get("RECOMMEND_PAPERS_PROBE_WALL_TIMEOUT_SECONDS", "60") or 60)))
    except (TypeError, ValueError):
        probe_wall_timeout = 60.0
    for year in range(start_year, start_year - max(1, lookback), -1):
        if not _regular_venue_year(clean(spec.get("venue_id")).lower(), year):
            attempts.append({"year": year, "status": "not_scheduled", "count": 0})
            continue
        known = next((item for item in DEFAULT_VENUES if item["id"] == clean(spec.get("venue_id"))), {})
        primary = clean(spec.get("adapter") or known.get("adapter")).lower()
        # A built-in channel owns and audits its own fallback sequence.  Trying
        # catalog fallback adapters here would execute the same channel more
        # than once and make the receipt look like independent source evidence.
        adapters = ([primary] if known else list(dict.fromkeys(
            item for item in [primary, *[clean(value).lower() for value in known.get("fallback_adapters") or []]] if item
        ))) or [""]
        transient_for_year = False
        error_for_year = False
        for adapter in adapters:
            candidate = dict(spec)
            candidate["years"] = [year]
            if adapter:
                candidate["adapter"] = adapter
            try:
                # Match TASTE's probe semantics: one bounded title sample and
                # short detail budget, never a hidden full venue crawl.
                with bounded_request_policy(max_attempts=2, max_wait_seconds=5.0, wall_timeout_seconds=probe_wall_timeout):
                    rows, source_receipt = _probe_venue_exact(candidate, sample_limit)
                attempts.append({"year": year, "adapter": adapter, "status": "available" if rows else "empty", "count": len(rows), "details": source_receipt})
                if rows:
                    result = {
                        "status": "probe_available",
                        "requested_year": start_year,
                        "resolved_year": year,
                        "year_fallback": year != start_year,
                        "year_fallback_reason": f"all configured {start_year} channels returned authoritative empty catalogs" if year != start_year else "",
                        "attempts": attempts,
                        "samples": [normalize(row, "venue") for row in rows[:sample_limit]],
                        "probe_only": True,
                        "formal_metadata_required": True,
                    }
                    break
            except Exception as exc:
                if _authoritative_probe_empty(exc):
                    attempts.append({
                        "year": year,
                        "adapter": adapter,
                        "status": "empty",
                        "count": 0,
                        "authoritative_absence": True,
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    })
                    continue
                transient = _transient_probe_error(exc)
                transient_for_year = transient_for_year or transient
                error_for_year = True
                attempts.append({
                    "year": year,
                    "adapter": adapter,
                    "status": "transient_error" if transient else "error",
                    "count": 0,
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:500],
                })
        if result is not None:
            break
        # A transient failure on any configured current-year route makes the
        # year unresolved; it cannot authorize fallback to an older edition.
        if transient_for_year:
            result = {
                "status": "temporarily_unresolved",
                "requested_year": start_year,
                "resolved_year": None,
                "year_fallback": False,
                "attempts": attempts,
                "samples": [],
                "probe_only": True,
                "formal_metadata_required": True,
            }
            break
        if error_for_year:
            result = {
                "status": "probe_error",
                "requested_year": start_year,
                "resolved_year": None,
                "year_fallback": False,
                "attempts": attempts,
                "samples": [],
                "probe_only": True,
                "formal_metadata_required": True,
            }
            break
    if result is None:
        result = {
            "status": "unavailable",
            "requested_year": start_year,
            "resolved_year": None,
            "year_fallback": False,
            "attempts": attempts,
            "samples": [],
            "probe_only": True,
            "formal_metadata_required": True,
        }
    diagnostic_samples = list(result.pop("samples", []) or [])
    result.update({
        "artifact_role": "venue_year_diagnostic_only",
        "research_output": False,
        "complete_catalog": False,
        "observed_sample_count": len(diagnostic_samples),
        "next_required_stage": "metadata",
        "message": "Diagnostic year/channel probe only. No papers from this response may be reported as research results; run formal metadata acquisition next.",
    })
    if run_dir is not None:
        run_dir = require_run(run_dir)
        venue_key = re.sub(r"[^a-z0-9_.-]+", "_", clean(spec.get("venue_id") or spec.get("venue") or "venue").lower())
        receipt_path = run_dir / "venue_probes" / f"{venue_key}-{start_year}.json"
        payload = {"schema_version": 1, "created_at": now_iso(), "source": spec, **result, "diagnostic_samples": diagnostic_samples}
        write_json(receipt_path, payload)
        state = read_json(run_dir / "run.json", {})
        counts = dict(state.get("counts") or {}) if isinstance(state, dict) else {}
        counts["venue_probes"] = len(list((run_dir / "venue_probes").glob("*.json")))
        update_run(run_dir, stage="venue_probe", counts=counts)
        result["receipt_path"] = str(receipt_path)
    return result
