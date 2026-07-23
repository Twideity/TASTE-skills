from __future__ import annotations

from typing import Any


def complete_abstract_catalog(payload: dict[str, Any], spec: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    # Imported lazily to keep channel registration acyclic.
    from ..metadata import _complete_conference_cache
    return _complete_conference_cache(payload, spec)
