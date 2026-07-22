from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import requests
from filelock import FileLock

from .storage import HTTP_STATE_ROOT, now_iso, read_json, write_json


USER_AGENT = "TASTE-Recommend-Papers/1.0"
DEFAULT_TIMEOUT = 30
MIN_INTERVALS = {
    "arxiv": 5.1,
    "biorxiv": 1.0,
    "openreview": 2.0,
    "dblp": 1.0,
    "crossref": 1.0,
    "openalex": 1.0,
    "europepmc": 0.25,
    "unpaywall": 0.25,
    "generic": 0.25,
}
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def service_for(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "arxiv.org" in host:
        return "arxiv"
    if "biorxiv.org" in host or "medrxiv.org" in host:
        return "biorxiv"
    if "openreview.net" in host:
        return "openreview"
    if "dblp.org" in host:
        return "dblp"
    if "crossref.org" in host:
        return "crossref"
    if "openalex.org" in host:
        return "openalex"
    if "europepmc.org" in host or "ebi.ac.uk" in host:
        return "europepmc"
    if "unpaywall.org" in host:
        return "unpaywall"
    return "generic"


def _state_path(service: str) -> Path:
    clean = re.sub(r"[^a-z0-9_.-]+", "_", service.lower()) or "generic"
    return HTTP_STATE_ROOT / f"{clean}.json"


@contextmanager
def request_slot(service: str) -> Iterator[None]:
    with _locks_guard:
        lock = _locks.setdefault(service, threading.Lock())
    with lock:
        slot_path = _state_path(service).with_name(".http-slot.lock")
        slot_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(slot_path)):
            state = read_json(_state_path(service), {})
            if not isinstance(state, dict):
                state = {}
            now = time.time()
            cooldown_until = float(state.get("cooldown_until") or 0)
            if cooldown_until > now:
                time.sleep(cooldown_until - now)
            now = time.time()
            last_request = float(state.get("last_request_at") or 0)
            wait = max(0.0, MIN_INTERVALS.get(service, MIN_INTERVALS["generic"]) - (now - last_request))
            if wait:
                time.sleep(wait)
            yield
            # Reload because the request body may have persisted a Retry-After
            # cooldown while this context was active.
            state = read_json(_state_path(service), {})
            if not isinstance(state, dict):
                state = {}
            state["last_request_at"] = time.time()
            state["updated_at"] = now_iso()
            write_json(_state_path(service), state)


def _retry_after(response: requests.Response, fallback: float = 120.0) -> float:
    raw = str(response.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return min(86400.0, float(raw))
    if raw:
        try:
            return max(0.0, min(86400.0, parsedate_to_datetime(raw).timestamp() - time.time()))
        except Exception:
            pass
    return fallback


def get(url: str, *, params: dict | None = None, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT, stream: bool = False) -> requests.Response:
    service = service_for(url)
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    response = None
    retry_history = []
    retryable_statuses = {429, 500, 502, 503, 504}
    max_attempts = 8 if service == "arxiv" else 5
    for attempt in range(1, max_attempts + 1):
        with request_slot(service):
            response = requests.get(url, params=params, headers=merged, timeout=timeout, stream=stream)
            state = read_json(_state_path(service), {}) or {}
            if response.status_code in retryable_statuses:
                fallback = min(300.0, 30.0 * (2 ** (attempt - 1)))
                retry_after = _retry_after(response, fallback)
                retry_history.append({"attempt": attempt, "status_code": response.status_code, "retry_after_seconds": retry_after})
                state.update({
                    "cooldown_until": time.time() + retry_after,
                    "reason": f"http_{response.status_code}",
                    "retry_attempt": attempt,
                    "updated_at": now_iso(),
                })
            elif response.status_code == 403:
                state.update({"cooldown_until": time.time() + _retry_after(response, 60.0), "reason": "http_403", "updated_at": now_iso()})
            else:
                for key in ("cooldown_until", "reason", "retry_attempt"):
                    state.pop(key, None)
                state["updated_at"] = now_iso()
            write_json(_state_path(service), state)
        if response.status_code in retryable_statuses and attempt < max_attempts:
            continue
        setattr(response, "_taste_retry_history", retry_history)
        return response
    assert response is not None
    return response


def receipt(response: requests.Response) -> dict:
    return {
        "url": response.url,
        "status_code": response.status_code,
        "content_type": str(response.headers.get("Content-Type") or ""),
        "content_length": len(response.content or b""),
        "retry_history": list(getattr(response, "_taste_retry_history", []) or []),
    }
