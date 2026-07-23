from __future__ import annotations

import importlib
import re
from typing import Any

CONFERENCE_IDS = (
    "neurips", "iclr", "icml", "kdd", "sigir", "cikm", "aaai",
    "iccv", "www", "cvpr", "acl", "ijcai", "eccv", "emnlp",
)
ALIASES = {
    "nips": "neurips",
    "sigkdd": "kdd",
    "thewebconference": "www",
    "thewebconf": "www",
    "webconference": "www",
    "webconf": "www",
}
ALL_IDS = (*CONFERENCE_IDS, "arxiv", "biorxiv")
DISPLAY_NAMES = {
    "neurips": "NeurIPS",
    "iclr": "ICLR",
    "icml": "ICML",
    "kdd": "KDD",
    "sigir": "SIGIR",
    "cikm": "CIKM",
    "aaai": "AAAI",
    "iccv": "ICCV",
    "www": "WWW",
    "cvpr": "CVPR",
    "acl": "ACL",
    "ijcai": "IJCAI",
    "eccv": "ECCV",
    "emnlp": "EMNLP",
}
DISPLAY_ALIASES = {
    "neurips": ["NIPS"],
    "kdd": ["SIGKDD"],
    "www": ["The Web Conference", "WebConf"],
}


def canonical(value: Any) -> str:
    token = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return ALIASES.get(token, token)


def get_channel(channel_id: str):
    channel_id = canonical(channel_id)
    if channel_id not in ALL_IDS:
        raise KeyError(f"Unsupported channel: {channel_id}")
    module = importlib.import_module(f"{__package__}.{channel_id}")
    return getattr(module, "CHANNEL", module)


def catalog_entries() -> list[dict[str, Any]]:
    entries = []
    for channel_id in CONFERENCE_IDS:
        channel = get_channel(channel_id)
        entries.append({
            "id": channel.id,
            "name": DISPLAY_NAMES[channel_id],
            "aliases": DISPLAY_ALIASES.get(channel_id, []),
            "official_source": channel.official_source,
            "metadata_schema": channel.metadata_schema,
            "metadata_workers": channel.metadata_workers,
            "pdf_workers": channel.pdf_workers,
            "require_complete_abstracts": True,
        })
    return entries


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
