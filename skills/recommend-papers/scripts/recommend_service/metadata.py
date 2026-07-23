from __future__ import annotations

import html
import hashlib
import json
import os
import shutil
import re
import xml.etree.ElementTree as ET
import calendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .http import get, receipt
from .storage import (
    DATA_ROOT,
    FULLTEXT_CACHE_ROOT,
    METADATA_CACHE_ROOT,
    now_iso,
    read_json,
    stable_hash,
    write_json,
)
from .channels import channel_for_spec
from .channels.registry import canonical, catalog_entries
from .channels.runtime import abstract_is_real, clean_abstract


ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
DEFAULT_ARXIV_AI_CATEGORIES = ("cs.AI", "cs.LG", "stat.ML", "cs.CL", "cs.CV", "cs.IR", "cs.RO", "eess.SY", "cs.MA", "cs.NE")
BIORXIV_RECHECK_DAYS = 3
BIORXIV_RECHECK_MAX_AGE_DAYS = 1.0


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
    row["abstract"] = clean_abstract(row.get("abstract"))
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


class _ArxivRangeTooLarge(RuntimeError):
    def __init__(self, total: int):
        super().__init__(f"arXiv range exposes {total} records")
        self.total = total


def _assert_complete_preprint_rows(
    rows: list[dict[str, Any]],
    *,
    source: str,
    expected_day: str | None = None,
    expected_category: str | None = None,
) -> None:
    invalid = []
    identities = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            invalid.append(f"<invalid-row-{index}>")
            identities.append(f"<invalid-row-{index}>")
            continue
        categories = row.get("categories") if isinstance(row.get("categories"), list) else []
        identifiers = row.get("identifiers") if isinstance(row.get("identifiers"), dict) else {}
        if source == "arxiv":
            identifier = clean(identifiers.get("arxiv_id")).lower()
        else:
            doi = clean(identifiers.get("doi")).lower()
            version = clean(identifiers.get("biorxiv_version") or row.get("version"))
            identifier = f"{doi}v{version}" if doi else ""
        identities.append(identifier)
        if (
            not clean(row.get("title"))
            or not abstract_is_real(row.get("abstract"))
            or not row.get("authors")
            or not identifier
            or (expected_day and date_text(row.get("published")) != expected_day)
            or (expected_category and expected_category not in categories)
        ):
            invalid.append(clean(row.get("title")) or "<untitled>")
    duplicate_count = len(identities) - len(set(identities))
    if invalid or duplicate_count:
        raise RuntimeError(
            f"{source} metadata is incomplete: invalid_records={len(invalid)}, "
            f"duplicate_records={duplicate_count}, examples={invalid[:5]}"
        )


def _preprint_shard_valid(
    payload: Any,
    *,
    source: str,
    day: str,
    category: str | None = None,
) -> bool:
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 2
        or clean(payload.get("source")).lower() != source
        or clean(payload.get("date")) != day
        or not isinstance(payload.get("papers"), list)
    ):
        return False
    try:
        _assert_complete_preprint_rows(
            payload["papers"],
            source=source,
            expected_day=day,
            expected_category=category,
        )
    except RuntimeError:
        return False
    if source == "biorxiv":
        try:
            if int(payload.get("server_total") or 0) != len(payload["papers"]):
                return False
        except (TypeError, ValueError):
            return False
    return True


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
            if staged_total >= 10000:
                raise _ArxivRangeTooLarge(staged_total)
            if total is not None and total != staged_total:
                raise RuntimeError(f"arXiv staged page total changed for {category} {start_date}..{end_date}")
            total = staged_total
            _assert_complete_preprint_rows(page_rows, source="arxiv", expected_category=category)
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
        if page_total >= 10000:
            raise _ArxivRangeTooLarge(page_total)
        if total is not None and total != page_total:
            raise RuntimeError(f"arXiv totalResults changed during pagination for {category} {start_date}..{end_date}: {total} -> {page_total}")
        total = page_total
        entries = root.findall("atom:entry", ARXIV_NS)
        page_rows = [_arxiv_entry(entry) for entry in entries]
        _assert_complete_preprint_rows(page_rows, source="arxiv", expected_category=category)
        write_json(stage_path, {"schema_version": 1, "query": search_query, "category": category, "start_date": start_date, "end_date": end_date, "offset": offset, "server_total": total, "papers": page_rows, "fetched_at": now_iso()})
        rows.extend(page_rows)
        offset += len(page_rows)
        if not entries:
            break
    if total is None or len(rows) != total:
        raise RuntimeError(f"arXiv exhaustive pagination failed for {category}: expected={total}, fetched={len(rows)}")
    return rows, {
        "status": "complete",
        "category": category,
        "query": search_query,
        "range_start": start_date,
        "range_end": end_date,
        "server_total": total,
        "fetched": len(rows),
        "exhausted": True,
        "truncated": False,
        "exhaustion_proof": "opensearch_total_results_reached",
        "requests": receipts,
    }


def _fetch_arxiv_safe_ranges(
    category: str, days: list[str]
) -> list[tuple[list[dict[str, Any]], dict[str, Any]]]:
    start_date, end_date = min(days), max(days)
    try:
        return [_fetch_arxiv_category_range(category, start_date, end_date)]
    except _ArxivRangeTooLarge as exc:
        stage_dir = _arxiv_page_stage_path(category, start_date, end_date, 0).parent
        if stage_dir.is_dir():
            shutil.rmtree(stage_dir)
        if len(days) == 1:
            raise RuntimeError(
                f"arXiv single-day shard is too large for safe exhaustive pagination: "
                f"{category} {days[0]} total={exc.total}"
            ) from exc
        midpoint = len(days) // 2
        return (
            _fetch_arxiv_safe_ranges(category, days[:midpoint])
            + _fetch_arxiv_safe_ranges(category, days[midpoint:])
        )


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
            usable = (
                day < today
                and _preprint_shard_valid(payload, source="arxiv", day=day, category=category)
                and payload.get("complete") is True
                and payload.get("provisional") is not True
                and (policy == "only" or _cache_fresh(payload, max_age_days))
            )
            if policy == "refresh" or not usable:
                missing.append(day)
            else:
                cached_by_day[day] = payload
        if missing:
            if policy == "only":
                raise FileNotFoundError(f"Missing usable arXiv day shards for {category}: {len(missing)}")
            chunk_receipts = []
            for chunk_days in _arxiv_month_chunks(missing):
                fetched: list[dict[str, Any]] = []
                safe_ranges = _fetch_arxiv_safe_ranges(category, chunk_days)
                for range_rows, _ in safe_ranges:
                    fetched.extend(range_rows)
                partitioned = {day: [] for day in chunk_days}
                for row in fetched:
                    published = date_text(row.get("published"))
                    if published in partitioned:
                        partitioned[published].append(normalize(row, "arxiv"))
                for day in chunk_days:
                    _assert_complete_preprint_rows(
                        partitioned[day],
                        source="arxiv",
                        expected_day=day,
                        expected_category=category,
                    )
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
                        "range_exhaustion_proof": "all_safe_subranges_reached_opensearch_total",
                        "range_start": min(chunk_days),
                        "range_end": max(chunk_days),
                    }
                    write_json(_arxiv_day_cache_path(category, day), payload)
                    cached_by_day[day] = payload
                for _, crawl_receipt in safe_ranges:
                    stage_dir = _arxiv_page_stage_path(
                        category,
                        crawl_receipt["range_start"],
                        crawl_receipt["range_end"],
                        0,
                    ).parent
                    if stage_dir.is_dir():
                        shutil.rmtree(stage_dir)
                chunk_receipts.append({
                    "status": "complete",
                    "category": category,
                    "server_total": sum(int(item.get("server_total") or 0) for _, item in safe_ranges),
                    "fetched": len(fetched),
                    "requests": [request for _, item in safe_ranges for request in item.get("requests") or []],
                    "exhausted": True,
                    "truncated": False,
                    "exhaustion_proof": "all_safe_subranges_reached_opensearch_total",
                    "subrange_count": len(safe_ranges),
                    "day_shards_written": len(chunk_days),
                })
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
    _assert_complete_preprint_rows(rows, source="biorxiv")
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
        proven = (
            _preprint_shard_valid(payload, source="biorxiv", day=day)
            and payload.get("complete") is True
            and payload.get("provisional") is not True
            and payload.get("exhausted") is True
            and payload.get("truncated") is False
            and bool(payload.get("exhaustion_proof"))
        )
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
                _assert_complete_preprint_rows(
                    partitioned[day],
                    source="biorxiv",
                    expected_day=day,
                )
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


def venue_metadata_audit(rows: list[dict[str, Any]], *, require_official_categories: bool = False) -> dict[str, Any]:
    count = len(rows)
    malformed = sum(not isinstance(row, dict) for row in rows)
    identities = [
        paper_identity(row) if isinstance(row, dict) else f"malformed:{index}"
        for index, row in enumerate(rows)
    ]
    unique = len(set(identities))
    titled = sum(isinstance(row, dict) and bool(clean(row.get("title"))) for row in rows)
    authored = sum(isinstance(row, dict) and bool(row.get("authors")) for row in rows)
    linked = sum(isinstance(row, dict) and bool(clean(row.get("url") or row.get("pdf_url"))) for row in rows)
    invalid_abstracts = [
        clean(row.get("title")) if isinstance(row, dict) else "<malformed-row>"
        for row in rows
        if not isinstance(row, dict) or not abstract_is_real(row.get("abstract"))
    ]
    abstracted = sum(isinstance(row, dict) and abstract_is_real(row.get("abstract")) for row in rows)
    categorized = sum(isinstance(row, dict) and bool(row.get("categories")) for row in rows)
    complete = bool(
        count
        and not malformed
        and unique == count
        and titled == count
        and authored / count >= 0.95
        and linked / count >= 0.95
    )
    complete = complete and abstracted == count
    if require_official_categories:
        complete = complete and categorized == count
    return {
        "record_count": count,
        "unique_identity_count": unique,
        "duplicate_count": count - unique,
        "malformed_record_count": malformed,
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
    payload_source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    try:
        venue_id = canonical(source.get("venue_id") or source.get("venue"))
        requested_years = [int(value) for value in source.get("years") or []]
        payload_years = [int(value) for value in payload_source.get("years") or []]
        channel = channel_for_spec(source)
        minimum = int(source.get("minimum_catalog_records") or 0)
    except (KeyError, TypeError, ValueError) as exc:
        return False, {
            "metadata_completeness_ok": False,
            "reason": "invalid_cache_contract",
            "error_type": type(exc).__name__,
        }
    expected_year = requested_years[0] if len(requested_years) == 1 else 0
    row_years = []
    row_venues = []
    for row in rows:
        if not isinstance(row, dict):
            row_years.append(0)
            row_venues.append("")
            continue
        try:
            row_years.append(int(row.get("year") or date_text(row.get("published"))[:4] or 0))
        except (TypeError, ValueError):
            row_years.append(0)
        row_venues.append(canonical(row.get("venue")))
    source_binding_ok = (
        clean(payload_source.get("type")).lower() == "venue"
        and canonical(payload_source.get("venue_id") or payload_source.get("venue")) == venue_id
        and payload_years == requested_years
    )
    cache_key_ok = (
        bool(payload_source)
        and clean(payload.get("cache_key")) == stable_hash(payload_source)
    )
    try:
        payload_schema = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError):
        payload_schema = 0
    payload_binding_ok = (
        clean(payload.get("channel")) == channel.id
        and payload_schema == channel.metadata_schema
    )
    row_binding_ok = bool(
        rows
        and all(year == expected_year for year in row_years)
        and all(value == venue_id for value in row_venues)
        and all(
            isinstance(row, dict)
            and clean(row.get("source_type")).lower() == "venue"
            for row in rows
        )
    )
    receipt_binding_ok = (
        receipt_data.get("count") == len(rows)
        and receipt_data.get("discovered_count") == len(rows)
        and receipt_data.get("requested_years") == requested_years
        and receipt_data.get("effective_years") == requested_years
        and canonical(receipt_data.get("channel")) == venue_id
    )
    audit = venue_metadata_audit(
        rows,
        require_official_categories=source.get("require_official_categories") is True,
    )
    audit.update({
        "requested_year": expected_year,
        "wrong_year_count": sum(year != expected_year for year in row_years),
        "wrong_venue_count": sum(value != venue_id for value in row_venues),
        "source_binding_ok": source_binding_ok,
        "cache_key_ok": cache_key_ok,
        "payload_binding_ok": payload_binding_ok,
        "row_binding_ok": row_binding_ok,
        "receipt_binding_ok": receipt_binding_ok,
        "minimum_catalog_records": minimum or None,
        "minimum_catalog_records_ok": not minimum or len(rows) >= minimum,
    })
    complete = (
        receipt_data.get("status") == "complete"
        and receipt_data.get("complete_catalog") is True
        and bool(receipt_data.get("exhaustion_proof"))
        and source_binding_ok
        and cache_key_ok
        and payload_binding_ok
        and row_binding_ok
        and receipt_binding_ok
        and (not minimum or len(rows) >= minimum)
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


SUPPORTED_SOURCE_TYPES = {"venue", "arxiv", "biorxiv"}

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
        if not isinstance(source, dict) or clean(source.get("type")).lower() not in SUPPORTED_SOURCE_TYPES:
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
    removed_unsupported_metadata_namespaces: list[str] = []
    removed_legacy_fulltext_aliases: list[str] = []
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
    for directory in [
        path
        for path in METADATA_CACHE_ROOT.iterdir()
        if path.is_dir()
        and path.name in {
            "generic", "crossref", "openalex", "europepmc",
            "journal", "nature", "science",
        }
    ]:
        removed_unsupported_metadata_namespaces.append(str(directory))
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
    for entry in catalog_entries():
        channel = channel_for_spec({"type": "venue", "venue_id": entry["id"]})
        channel_root = METADATA_CACHE_ROOT / entry["id"]
        for path in channel_root.glob("*.json") if channel_root.is_dir() else []:
            payload = read_json(path, {})
            source = payload.get("source") if isinstance(payload, dict) and isinstance(payload.get("source"), dict) else {}
            valid = False
            try:
                valid = (
                    path.stem == str(int((source.get("years") or [0])[0]))
                    and channel.validate_cache(payload, source)[0]
                )
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                removed_invalid_conference_caches.append(str(path))
                path.unlink()
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
    legacy_fulltext_aliases = FULLTEXT_CACHE_ROOT / "_aliases"
    if legacy_fulltext_aliases.is_dir():
        removed_legacy_fulltext_aliases.append(str(legacy_fulltext_aliases))
        shutil.rmtree(legacy_fulltext_aliases)
    recovered = _recover_conference_caches_from_runs()
    manifest_path = DATA_ROOT / "state" / "metadata-cache-migration.json"
    write_json(manifest_path, {
        "schema_version": 2,
        "migrated_at": now_iso(),
        "moves": moves,
        "removed_invalid_conference_caches": removed_invalid_conference_caches,
        "removed_unsupported_metadata_namespaces": removed_unsupported_metadata_namespaces,
        "removed_legacy_fulltext_aliases": removed_legacy_fulltext_aliases,
        "recovered_conferences": recovered,
    })
    return {
        "status": "complete",
        "moved_or_deduplicated": len(moves),
        "moves": moves,
        "removed_invalid_conference_caches": removed_invalid_conference_caches,
        "removed_unsupported_metadata_namespaces": removed_unsupported_metadata_namespaces,
        "removed_legacy_fulltext_aliases": removed_legacy_fulltext_aliases,
        "recovered_conferences": recovered,
        "inventory": metadata_cache_inventory(),
        "manifest": str(manifest_path),
    }


def fetch_source(spec: dict[str, Any], *, policy: str, max_age_days: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    identity = dict(spec)
    cache_key = stable_hash(identity)
    kind = clean(spec.get("type")).lower()
    if kind not in SUPPORTED_SOURCE_TYPES:
        raise ValueError(f"Unsupported metadata source type: {kind}")
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
    if kind != "venue":
        raise ValueError(f"Unsupported metadata source type: {kind}")
    channel = channel_for_spec(spec)
    papers, source_receipt = channel.fetch_metadata(spec)
    if kind == "venue" and source_receipt.get("complete_catalog") is not True:
        raise RuntimeError("Venue adapter did not prove complete-catalog acquisition")
    normalized = [normalize(item, kind) for item in papers if isinstance(item, dict) and clean(item.get("title"))]
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
    complete, audit = channel.validate_cache(payload, spec)
    if not complete:
        raise RuntimeError(
            "Conference metadata cannot be cached until its source, year, "
            f"catalog size, identities, and abstracts are proven: {audit}"
        )
    source_receipt["metadata_audit"] = audit
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
    venues = [
        item
        for item in catalog_entries()
        if not needle or needle in json.dumps(item).lower()
    ]
    return {
        "query": query,
        "count": len(venues),
        "venues": venues,
        "custom_venue_policy": (
            "Only registered channels are supported. Add a channel module and "
            "registry entry before using another venue."
        ),
    }
