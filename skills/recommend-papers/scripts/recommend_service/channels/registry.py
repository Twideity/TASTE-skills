from __future__ import annotations

import importlib
import re
from typing import Any

CONFERENCE_IDS = (
    "neurips", "iclr", "icml", "kdd", "sigir", "cikm", "aaai",
    "iccv", "www", "cvpr", "acl", "ijcai", "eccv", "emnlp",
)
ALIASES = {"nips": "neurips", "sigkdd": "kdd", "thewebconference": "www"}
ALL_IDS = (*CONFERENCE_IDS, "arxiv", "biorxiv")


def canonical(value: Any) -> str:
    token = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return ALIASES.get(token, token)


def get_channel(channel_id: str):
    channel_id = canonical(channel_id)
    if channel_id not in ALL_IDS:
        raise KeyError(f"Unsupported channel: {channel_id}")
    module = importlib.import_module(f"{__package__}.{channel_id}")
    return getattr(module, "CHANNEL", module)


def channel_for_spec(spec: dict[str, Any]):
    kind = canonical(spec.get("type"))
    if kind in {"arxiv", "biorxiv"}:
        return get_channel(kind)
    channel_id = canonical(spec.get("venue_id") or spec.get("venue") or spec.get("name"))
    return get_channel(channel_id)


def channel_for_paper(paper: dict[str, Any]):
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    candidates = (paper.get("channel"), paper.get("venue_id"), paper.get("venue"),
                  paper.get("source"), metadata.get("venue_id"), metadata.get("venue"))
    for value in candidates:
        token = canonical(value)
        if token in ALL_IDS:
            return get_channel(token)
        for channel_id in CONFERENCE_IDS:
            if token.startswith(channel_id):
                return get_channel(channel_id)
    blob = " ".join(str(v or "").lower() for v in (
        paper.get("url"), paper.get("pdf_url"), metadata.get("url"), metadata.get("pdf_url")))
    markers = (
        ("arxiv.org", "arxiv"), ("biorxiv.org", "biorxiv"),
        ("aclanthology.org", "acl"), ("openaccess.thecvf.com", "cvpr"),
        ("ijcai.org", "ijcai"), ("ecva.net", "eccv"), ("aaai.org", "aaai"),
        ("neurips.cc", "neurips"),
    )
    for marker, channel_id in markers:
        if marker in blob:
            return get_channel(channel_id)
    raise KeyError("Paper does not identify a supported acquisition channel")
