from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..storage import METADATA_CACHE_ROOT

MetadataFetcher = Callable[[dict[str, Any]], tuple[list[dict[str, Any]], dict[str, Any]]]


@dataclass(frozen=True)
class Channel:
    """Complete channel contract.

    A channel owns source selection, retry/concurrency labels, cache namespace,
    cache acceptance and official PDF derivation.  Shared code may execute HTTP,
    validate identities and publish files, but may not change these decisions.
    """

    id: str
    kind: str
    metadata_fetcher: MetadataFetcher
    metadata_schema: int
    metadata_workers: int
    pdf_workers: int
    official_source: str
    cache_validator: Callable[[dict[str, Any], dict[str, Any]], tuple[bool, dict[str, Any]]]
    pdf_builder: Callable[[dict[str, Any]], list[dict[str, str]]]

    def metadata_cache_path(self, spec: dict[str, Any]) -> Path:
        if self.kind == "conference":
            years = spec.get("years") or []
            year = str(years[0]) if len(years) == 1 else "-".join(str(v) for v in years)
            return METADATA_CACHE_ROOT / self.id / f"{year}.json"
        raise ValueError(f"{self.id} uses partitioned cache paths")

    def validate_cache(
        self, payload: dict[str, Any], spec: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        if payload.get("channel") not in (None, self.id):
            return False, {"reason": "channel_mismatch"}
        if int(payload.get("schema_version") or 0) != self.metadata_schema:
            return False, {"reason": "schema_version_mismatch"}
        return self.cache_validator(payload, spec)

    def fetch_metadata(
        self, spec: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows, receipt = self.metadata_fetcher(spec)
        years = [int(value) for value in spec.get("years") or []]
        receipt = dict(receipt)
        receipt.update({
            "requested_years": years,
            "effective_years": years,
            "year_fallback": False,
            "year_fallback_reason": "",
            "channel": self.id,
            "channel_metadata_workers": self.metadata_workers,
        })
        return rows, receipt

    def pdf_candidates(self, paper: dict[str, Any]) -> list[dict[str, str]]:
        return self.pdf_builder(paper)
