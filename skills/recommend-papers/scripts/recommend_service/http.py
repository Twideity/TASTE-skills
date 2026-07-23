from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
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
    # The anonymous OpenAIRE Search API is limited to 60 requests/hour.  ACM
    # enrichment batches many DOI values into one request, but keep starts
    # serialized and conservatively spaced as well.
    "openaire": 1.1,
    "hal": 0.5,
    "chatpaper": 10.1,
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
    "openaire": 1,
    "hal": 3,
    "chatpaper": 1,
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
_request_policy: ContextVar[dict[str, float | int | None]] = ContextVar(
    "recommend_papers_request_policy",
    default={"max_attempts": None, "max_wait_seconds": None, "deadline": None},
)


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
    if "openaire.eu" in host:
        return "openaire"
    if "archives-ouvertes.fr" in host:
        return "hal"
    if host == "chatpaper.com" or host.endswith(".chatpaper.com"):
        return "chatpaper"
    # Keep cooldowns and cross-process request slots isolated by host.  A 403
    # from one conference site must not stall every other official proceedings
    # host merely because both previously fell into a global "generic" bucket.
    normalized = re.sub(r"[^a-z0-9.-]+", "_", host.split(":", 1)[0])
    return f"host-{normalized}" if normalized else "generic"


def _service_family(service: str) -> str:
    return service.split("--", 1)[0]


def _effective_service(service: str) -> str:
    """Keep keyed OpenAlex traffic out of an exhausted anonymous quota state."""
    if service == "openalex":
        api_key = str(os.environ.get("OPENALEX_API_KEY") or "").strip()
        if api_key:
            digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
            return f"openalex--key-{digest}"
    if service == "openaire":
        token = str(os.environ.get("OPENAIRE_ACCESS_TOKEN") or "").strip()
        if token:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
            return f"openaire--token-{digest}"
    return service


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
    family = _service_family(service)
    fallback = configured.get("default_host") if family.startswith("host-") else configured.get("generic")
    value = configured.get(service, configured.get(family, DEFAULT_CONCURRENCY.get(family, fallback if fallback is not None else DEFAULT_CONCURRENCY["generic"])))
    try:
        ceiling = 3 if family == "openreview" else 32
        return max(1, min(ceiling, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY.get(family, DEFAULT_CONCURRENCY["generic"])


def _mutate_state(service: str, callback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    path = _state_path(service)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(_state_lock_path(service))):
        state = read_json(path, {})
        if not isinstance(state, dict):
            state = {}
        callback(state)
        state["updated_at"] = now_iso()
        write_json(path, state)
        return state


@contextmanager
def _cross_process_slot(service: str, limit: int, *, max_wait_seconds: float | None = None) -> Iterator[None]:
    HTTP_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    selected: FileLock | None = None
    started = time.monotonic()
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
            if max_wait_seconds is not None and time.monotonic() - started >= max_wait_seconds:
                raise ServiceRequestDeferred(
                    f"{service} request deferred while waiting for a cross-process slot ({max_wait_seconds:.1f}s budget exhausted)"
                )
            remaining = None if max_wait_seconds is None else max(0.0, max_wait_seconds - (time.monotonic() - started))
            time.sleep(min(0.05, remaining) if remaining is not None else 0.05)
    try:
        yield
    finally:
        selected.release()


def cooldown_remaining(service: str) -> float:
    service = _effective_service(service)
    state = read_json(_state_path(service), {})
    if not isinstance(state, dict):
        return 0.0
    remaining = float(state.get("cooldown_until") or 0) - time.time()
    if remaining <= 0 and any(key in state for key in ("cooldown_until", "reason", "retry_attempt")):
        def clear_expired(current: dict[str, Any]) -> None:
            if float(current.get("cooldown_until") or 0) <= time.time():
                for key in ("cooldown_until", "reason", "retry_attempt"):
                    current.pop(key, None)

        current = _mutate_state(service, clear_expired)
        remaining = float(current.get("cooldown_until") or 0) - time.time()
        state = current
    hourly_remaining = 0.0
    if service == "openaire":
        now = time.time()
        timestamps = sorted(
            float(value) for value in (state.get("request_timestamps") or [])
            if now - float(value) < 3600.0
        )
        if len(timestamps) >= 60:
            hourly_remaining = max(0.0, timestamps[0] + 3600.0 - now)
    return max(0.0, remaining, hourly_remaining)


def _minimum_optional(current: float | int | None, requested: float | int) -> float | int:
    return requested if current is None else min(current, requested)


def _policy_remaining(policy: dict[str, float | int | None]) -> float | None:
    deadline = policy.get("deadline")
    return None if deadline is None else max(0.0, float(deadline) - time.monotonic())


@contextmanager
def bounded_request_policy(*, max_attempts: int, max_wait_seconds: float, wall_timeout_seconds: float = 30.0) -> Iterator[None]:
    """Bound retries/cooldown waits for lightweight availability probes only."""
    current = _request_policy.get()
    requested_deadline = time.monotonic() + max(0.1, float(wall_timeout_seconds))
    token = _request_policy.set({
        "max_attempts": int(_minimum_optional(current.get("max_attempts"), max(1, int(max_attempts)))),
        "max_wait_seconds": float(_minimum_optional(current.get("max_wait_seconds"), max(0.0, float(max_wait_seconds)))),
        "deadline": float(_minimum_optional(current.get("deadline"), requested_deadline)),
    })
    try:
        yield
    finally:
        _request_policy.reset(token)


@contextmanager
def request_slot(service: str, *, max_wait_seconds: float | None = None) -> Iterator[None]:
    service = _effective_service(service)
    family = _service_family(service)
    limit = concurrency_limit(service)
    with _locks_guard:
        semaphore = _semaphores.setdefault((service, limit), threading.BoundedSemaphore(limit))
    with semaphore:
        with _cross_process_slot(service, limit, max_wait_seconds=max_wait_seconds):
            # Never sleep while holding the state-file lock.  Recheck after
            # sleeping so another process cannot reserve the same start time.
            while True:
                reservation = {"wait": 0.0, "ready": False}

                def reserve(state: dict[str, Any]) -> None:
                    now = time.time()
                    if float(state.get("cooldown_until") or 0) <= now:
                        for key in ("cooldown_until", "reason", "retry_attempt"):
                            state.pop(key, None)
                    cooldown_wait = max(0.0, float(state.get("cooldown_until") or 0) - now)
                    spacing_wait = max(
                        0.0,
                        MIN_INTERVALS.get(family, MIN_INTERVALS["generic"])
                        - (now - float(state.get("last_request_at") or 0)),
                    )
                    hourly_wait = 0.0
                    if service == "openaire":
                        timestamps = [
                            float(value) for value in (state.get("request_timestamps") or [])
                            if now - float(value) < 3600.0
                        ]
                        state["request_timestamps"] = timestamps
                        if len(timestamps) >= 60:
                            hourly_wait = max(0.0, timestamps[0] + 3600.0 - now)
                    reservation["wait"] = max(cooldown_wait, spacing_wait, hourly_wait)
                    if reservation["wait"] <= 0:
                        state["last_request_at"] = now
                        if service == "openaire":
                            state.setdefault("request_timestamps", []).append(now)
                        reservation["ready"] = True

                _mutate_state(service, reserve)
                if reservation["ready"]:
                    break
                wait = float(reservation["wait"])
                service_cap = (
                    _positive_float_env("DBLP_MAX_RETRY_AFTER_WAIT_SEC", 10.0) if family == "dblp"
                    else _positive_float_env("ACM_MAX_COOLDOWN_WAIT_SEC", 5.0) if family == "acm"
                    else None
                )
                effective_cap = (
                    min(float(max_wait_seconds), float(service_cap))
                    if max_wait_seconds is not None and service_cap is not None
                    else max_wait_seconds if max_wait_seconds is not None
                    else service_cap
                )
                if effective_cap is not None and wait > effective_cap:
                    raise ServiceRequestDeferred(
                        f"{service} request deferred for persisted cooldown/spacing ({wait:.1f}s remaining; wait budget {effective_cap:.1f}s)"
                    )
                time.sleep(wait)
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
    policy = _request_policy.get()
    policy_attempts = policy.get("max_attempts")
    attempts = max(1, int(policy_attempts if policy_attempts is not None else (max_attempts or 1)))
    max_wait_seconds = policy.get("max_wait_seconds")
    for attempt in range(1, attempts + 1):
        remaining = _policy_remaining(policy)
        if remaining is not None and remaining <= 0:
            raise ServiceRequestDeferred(f"{service} request deferred: operation wall-clock budget exhausted")
        slot_wait = min(float(max_wait_seconds), remaining) if max_wait_seconds is not None and remaining is not None else (float(max_wait_seconds) if max_wait_seconds is not None else remaining)
        with request_slot(service, max_wait_seconds=slot_wait):
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
    service = _effective_service(service_for(url))
    family = _service_family(service)
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    response = None
    retry_history = []
    retryable_statuses = {429, 500, 502, 503, 504}
    policy = _request_policy.get()
    configured_attempts = policy.get("max_attempts")
    max_wait_seconds = policy.get("max_wait_seconds")
    # Match TASTE's bounded arXiv behavior: three attempts are enough to
    # distinguish a transient request from a sustained shared-IP throttle.
    # Daily/month staging makes a later invocation resumable; eight attempts
    # with exponential 30/60/120/... waits could pin one page for 22.5 minutes.
    max_attempts = max(1, int(configured_attempts)) if configured_attempts is not None else (3 if family == "arxiv" else 5)
    candidates = _dblp_url_candidates(url) if family == "dblp" else [url]
    candidate_index = 0
    for attempt in range(1, max_attempts + 1):
        candidate_url = candidates[min(candidate_index, len(candidates) - 1)]
        try:
            remaining = _policy_remaining(policy)
            if remaining is not None and remaining <= 0:
                raise ServiceRequestDeferred(f"{service} request deferred: operation wall-clock budget exhausted")
            slot_wait = min(float(max_wait_seconds), remaining) if max_wait_seconds is not None and remaining is not None else (float(max_wait_seconds) if max_wait_seconds is not None else remaining)
            request_timeout = min(float(timeout), max(0.1, remaining)) if remaining is not None else timeout
            with request_slot(service, max_wait_seconds=slot_wait):
                response = requests.get(candidate_url, params=params, headers=merged, timeout=request_timeout, stream=stream)
                if response.status_code in retryable_statuses:
                    # DBLP's latest upstream adapter switches official mirrors
                    # immediately for transient server failures and uses its
                    # one-second channel spacing when 429 omits Retry-After.
                    fallback = (1.0 if response.status_code == 429 else 0.0) if family == "dblp" else min(300.0, 30.0 * (2 ** (attempt - 1)))
                    retry_after = _retry_after(response, fallback)
                    retry_history.append({"attempt": attempt, "status_code": response.status_code, "retry_after_seconds": retry_after})
                    _record_response(service, response.status_code, retry_after, attempt)
                elif response.status_code == 403:
                    _record_response(service, response.status_code, _retry_after(response, 60.0), attempt)
                else:
                    _record_response(service, response.status_code)
        except requests.RequestException as exc:
            retry_history.append({"attempt": attempt, "error_type": type(exc).__name__, "message": str(exc)[:300]})
            if family == "dblp":
                candidate_index = min(candidate_index + 1, len(candidates) - 1)
            if attempt >= max_attempts:
                raise
            time.sleep(min(8.0, float(2 ** (attempt - 1))))
            continue
        if response.status_code in retryable_statuses and attempt < max_attempts:
            if family == "dblp" and response.status_code != 429:
                candidate_index = min(candidate_index + 1, len(candidates) - 1)
            continue
        setattr(response, "_taste_retry_history", retry_history)
        return response
    assert response is not None
    return response


def post(url: str, *, params: dict | None = None, json_body: dict | list | None = None, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> requests.Response:
    service = _effective_service(service_for(url))
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    response = None
    retry_history = []
    policy = _request_policy.get()
    configured_attempts = policy.get("max_attempts")
    max_wait_seconds = policy.get("max_wait_seconds")
    attempts = max(1, int(configured_attempts)) if configured_attempts is not None else 5
    for attempt in range(1, attempts + 1):
        try:
            remaining = _policy_remaining(policy)
            if remaining is not None and remaining <= 0:
                raise ServiceRequestDeferred(f"{service} request deferred: operation wall-clock budget exhausted")
            slot_wait = min(float(max_wait_seconds), remaining) if max_wait_seconds is not None and remaining is not None else (float(max_wait_seconds) if max_wait_seconds is not None else remaining)
            request_timeout = min(float(timeout), max(0.1, remaining)) if remaining is not None else timeout
            with request_slot(service, max_wait_seconds=slot_wait):
                response = requests.post(url, params=params, json=json_body, headers=merged, timeout=request_timeout)
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
            if attempt >= attempts:
                raise
            time.sleep(min(8.0, float(2 ** (attempt - 1))))
            continue
        if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
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
