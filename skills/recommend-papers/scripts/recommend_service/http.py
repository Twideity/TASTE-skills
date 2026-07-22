from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import requests
from filelock import FileLock, Timeout

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
    "acm": 0.5,
    "semantic_scholar": 0.5,
    "hal": 0.5,
    "generic": 0.25,
}
DEFAULT_CONCURRENCY = {
    "arxiv": 1,
    "biorxiv": 2,
    # TASTE serializes official OpenReview client/API/PDF access through one
    # shared service gate.  Keep that safe default while still allowing an
    # operator to raise the channel to OpenReview's observed ceiling of three.
    "openreview": 1,
    # Latest TASTE serializes DBLP starts.  The public service is particularly
    # prone to 429s during complete multi-volume venue crawls.
    "dblp": 1,
    "crossref": 4,
    "openalex": 4,
    "europepmc": 4,
    "unpaywall": 2,
    "acm": 1,
    "semantic_scholar": 2,
    "hal": 3,
    "host-aclanthology.org": 6,
    "host-raw.githubusercontent.com": 4,
    "host-openaccess.thecvf.com": 6,
    "host-proceedings.neurips.cc": 4,
    "host-proceedings.mlr.press": 6,
    "host-ojs.aaai.org": 3,
    "host-www.ijcai.org": 4,
    "host-eccv.ecva.net": 4,
    "generic": 2,
}
_locks_guard = threading.Lock()
_semaphores: dict[tuple[str, int], threading.BoundedSemaphore] = {}


def service_for(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "arxiv.org" in host:
        return "arxiv"
    if "biorxiv.org" in host or "medrxiv.org" in host:
        return "biorxiv"
    if "openreview.net" in host:
        return "openreview"
    if host in {"dblp.org", "www.dblp.org", "dblp.uni-trier.de", "dblp.dagstuhl.de"}:
        return "dblp"
    if "crossref.org" in host:
        return "crossref"
    if "openalex.org" in host:
        return "openalex"
    if "europepmc.org" in host or "ebi.ac.uk" in host:
        return "europepmc"
    if "unpaywall.org" in host:
        return "unpaywall"
    if "dl.acm.org" in host:
        return "acm"
    if "semanticscholar.org" in host:
        return "semantic_scholar"
    if "archives-ouvertes.fr" in host:
        return "hal"
    # Keep cooldowns and cross-process request slots isolated by host.  A 403
    # from one conference site must not stall every other official proceedings
    # host merely because both previously fell into a global "generic" bucket.
    normalized = re.sub(r"[^a-z0-9.-]+", "_", host.split(":", 1)[0])
    return f"host-{normalized}" if normalized else "generic"


def _state_path(service: str) -> Path:
    clean = re.sub(r"[^a-z0-9_.-]+", "_", service.lower()) or "generic"
    return HTTP_STATE_ROOT / f"{clean}.json"


def _lock_key(service: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", service.lower()) or "generic"


def _state_lock_path(service: str) -> Path:
    return HTTP_STATE_ROOT / f".{_lock_key(service)}.state.lock"


def _slot_path(service: str, index: int) -> Path:
    return HTTP_STATE_ROOT / f".{_lock_key(service)}.slot-{index:02d}.lock"


class ServiceRequestDeferred(RuntimeError):
    """A channel is cooling down longer than an interactive run should wait."""


_DBLP_HOSTS = ("dblp.org", "dblp.uni-trier.de", "dblp.dagstuhl.de")


def _dblp_url_candidates(url: str) -> list[str]:
    """Return equivalent official DBLP mirrors, preserving path/query exactly."""
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if host == "www.dblp.org":
        host = "dblp.org"
    if host not in _DBLP_HOSTS:
        return [url]
    hosts = [host, *[candidate for candidate in _DBLP_HOSTS if candidate != host]]
    return [parsed._replace(scheme="https", netloc=candidate).geturl() for candidate in hosts]


def _positive_float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def concurrency_limit(service: str) -> int:
    """Return the independent request capacity for one API or official host.

    Operators may override defaults with a JSON object in
    RECOMMEND_PAPERS_HTTP_CONCURRENCY, for example
    {"openreview": 3, "host-aclanthology.org": 8, "default_host": 4}.
    """
    configured: dict[str, Any] = {}
    raw = str(os.environ.get("RECOMMEND_PAPERS_HTTP_CONCURRENCY") or "").strip()
    if raw:
        try:
            payload = json.loads(raw)
            configured = payload if isinstance(payload, dict) else {}
        except (TypeError, ValueError):
            configured = {}
    fallback = configured.get("default_host") if service.startswith("host-") else configured.get("generic")
    value = configured.get(service, DEFAULT_CONCURRENCY.get(service, fallback if fallback is not None else DEFAULT_CONCURRENCY["generic"]))
    try:
        ceiling = 3 if service == "openreview" else 32
        return max(1, min(ceiling, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY.get(service, DEFAULT_CONCURRENCY["generic"])


def _mutate_state(service: str, callback: Callable[[dict[str, Any]], None]) -> None:
    path = _state_path(service)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(_state_lock_path(service))):
        state = read_json(path, {})
        if not isinstance(state, dict):
            state = {}
        callback(state)
        state["updated_at"] = now_iso()
        write_json(path, state)


@contextmanager
def _cross_process_slot(service: str, limit: int) -> Iterator[None]:
    HTTP_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    selected: FileLock | None = None
    while selected is None:
        for index in range(limit):
            candidate = FileLock(str(_slot_path(service, index)))
            try:
                candidate.acquire(timeout=0)
                selected = candidate
                break
            except Timeout:
                continue
        if selected is None:
            time.sleep(0.05)
    try:
        yield
    finally:
        selected.release()


def cooldown_remaining(service: str) -> float:
    state = read_json(_state_path(service), {})
    if not isinstance(state, dict):
        return 0.0
    return max(0.0, float(state.get("cooldown_until") or 0) - time.time())


@contextmanager
def request_slot(service: str) -> Iterator[None]:
    limit = concurrency_limit(service)
    with _locks_guard:
        semaphore = _semaphores.setdefault((service, limit), threading.BoundedSemaphore(limit))
    with semaphore:
        with _cross_process_slot(service, limit):
            # Serialize only request start times and state updates.  The network
            # transfer itself occupies one channel slot but does not hold up a
            # different slot or any unrelated channel.
            def reserve(state: dict[str, Any]) -> None:
                now = time.time()
                cooldown_until = float(state.get("cooldown_until") or 0)
                if cooldown_until > now:
                    remaining = cooldown_until - now
                    if service == "dblp" and remaining > _positive_float_env("DBLP_MAX_RETRY_AFTER_WAIT_SEC", 10.0):
                        raise ServiceRequestDeferred(
                            f"DBLP request deferred for Retry-After cooldown ({remaining:.0f}s remaining)"
                        )
                    time.sleep(remaining)
                now = time.time()
                last_request = float(state.get("last_request_at") or 0)
                wait = max(0.0, MIN_INTERVALS.get(service, MIN_INTERVALS["generic"]) - (now - last_request))
                if wait:
                    time.sleep(wait)
                state["last_request_at"] = time.time()

            _mutate_state(service, reserve)
            yield


def _record_response(service: str, status_code: int, retry_after: float = 0.0, retry_attempt: int = 0) -> None:
    def update(state: dict[str, Any]) -> None:
        now = time.time()
        if status_code in {429, 500, 502, 503, 504}:
            state["cooldown_until"] = max(float(state.get("cooldown_until") or 0), now + retry_after)
            state["reason"] = f"http_{status_code}"
            state["retry_attempt"] = retry_attempt
        elif status_code == 403:
            state["cooldown_until"] = max(float(state.get("cooldown_until") or 0), now + retry_after)
            state["reason"] = "http_403"
        elif float(state.get("cooldown_until") or 0) <= now:
            for key in ("cooldown_until", "reason", "retry_attempt"):
                state.pop(key, None)

    _mutate_state(service, update)


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


def retry_after_from_exception(exc: BaseException, fallback: float = 60.0) -> float:
    """Extract an OpenReview-style wait from an exception without leaking it.

    openreview-py raises RateLimitError instead of returning the underlying
    response, so the normal HTTP Retry-After parser is not available here.
    """
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "headers", None) is not None:
        try:
            return _retry_after(response, fallback)
        except Exception:
            pass
    message = str(exc)
    match = re.search(r"(?:try again in|retry(?:ing)?(?: after| in)?)\s*(\d+(?:\.\d+)?)\s*(?:s|sec|seconds?)", message, re.I)
    if match:
        return min(86400.0, max(0.0, float(match.group(1))))
    return fallback


def service_call(service: str, callback: Callable[[], Any], *, max_attempts: int = 5) -> tuple[Any, list[dict[str, Any]]]:
    """Run a non-requests client call through the shared channel governor.

    This is primarily for openreview-py.  It makes authenticated client calls
    share exactly the same cross-thread/process slots and cooldown as direct
    PDF/HTML requests, which prevents one access route from rate-limiting the
    others behind its back.
    """
    history: list[dict[str, Any]] = []
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        with request_slot(service):
            try:
                return callback(), history
            except Exception as exc:
                message = str(exc)
                lowered = message.lower()
                rate_limited = "429" in lowered or "ratelimit" in lowered or "rate limit" in lowered or "too many requests" in lowered
                forbidden = "403" in lowered or "forbidden" in lowered
                if rate_limited:
                    wait_seconds = retry_after_from_exception(exc, 60.0)
                    history.append({
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "status_code": 429,
                        "retry_after_seconds": wait_seconds,
                        "message": message[:300],
                    })
                    _record_response(service, 429, wait_seconds, attempt)
                    if attempt < attempts:
                        # The next request_slot observes the shared cooldown.
                        # Do not sleep separately and double the wait.
                        continue
                elif forbidden:
                    wait_seconds = retry_after_from_exception(exc, 60.0)
                    history.append({
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "status_code": 403,
                        "retry_after_seconds": wait_seconds,
                        "message": message[:300],
                    })
                    _record_response(service, 403, wait_seconds, attempt)
                else:
                    history.append({"attempt": attempt, "error_type": type(exc).__name__, "message": message[:300]})
                setattr(exc, "_taste_retry_history", history)
                raise
    raise RuntimeError(f"{service} client retry loop exited unexpectedly")


def get(url: str, *, params: dict | None = None, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT, stream: bool = False) -> requests.Response:
    service = service_for(url)
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    response = None
    retry_history = []
    retryable_statuses = {429, 500, 502, 503, 504}
    max_attempts = 8 if service == "arxiv" else 5
    candidates = _dblp_url_candidates(url) if service == "dblp" else [url]
    candidate_index = 0
    for attempt in range(1, max_attempts + 1):
        candidate_url = candidates[min(candidate_index, len(candidates) - 1)]
        try:
            with request_slot(service):
                response = requests.get(candidate_url, params=params, headers=merged, timeout=timeout, stream=stream)
                if response.status_code in retryable_statuses:
                    # DBLP's latest upstream adapter switches official mirrors
                    # immediately for transient server failures and uses its
                    # one-second channel spacing when 429 omits Retry-After.
                    fallback = (1.0 if response.status_code == 429 else 0.0) if service == "dblp" else min(300.0, 30.0 * (2 ** (attempt - 1)))
                    retry_after = _retry_after(response, fallback)
                    retry_history.append({"attempt": attempt, "status_code": response.status_code, "retry_after_seconds": retry_after})
                    _record_response(service, response.status_code, retry_after, attempt)
                elif response.status_code == 403:
                    _record_response(service, response.status_code, _retry_after(response, 60.0), attempt)
                else:
                    _record_response(service, response.status_code)
        except requests.RequestException as exc:
            retry_history.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)[:300]})
            if service == "dblp":
                candidate_index = min(candidate_index + 1, len(candidates) - 1)
            if attempt >= max_attempts:
                raise
            time.sleep(min(8.0, float(2 ** (attempt - 1))))
            continue
        if response.status_code in retryable_statuses and attempt < max_attempts:
            if service == "dblp" and response.status_code != 429:
                candidate_index = min(candidate_index + 1, len(candidates) - 1)
            continue
        setattr(response, "_taste_retry_history", retry_history)
        return response
    assert response is not None
    return response


def post(url: str, *, params: dict | None = None, json_body: dict | list | None = None, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> requests.Response:
    service = service_for(url)
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    response = None
    retry_history = []
    for attempt in range(1, 6):
        try:
            with request_slot(service):
                response = requests.post(url, params=params, json=json_body, headers=merged, timeout=timeout)
                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = _retry_after(response, min(300.0, 30.0 * (2 ** (attempt - 1))))
                    retry_history.append({"attempt": attempt, "status_code": response.status_code, "retry_after_seconds": retry_after})
                    _record_response(service, response.status_code, retry_after, attempt)
                elif response.status_code == 403:
                    _record_response(service, response.status_code, _retry_after(response, 60.0), attempt)
                else:
                    _record_response(service, response.status_code)
        except requests.RequestException as exc:
            retry_history.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)[:300]})
            if attempt >= 5:
                raise
            time.sleep(min(8.0, float(2 ** (attempt - 1))))
            continue
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 5:
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
