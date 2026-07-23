"""Channel-owned metadata, full-text and cache policies.

Only the registry is public.  Callers must not branch on venue names.
"""

from .registry import channel_for_paper, channel_for_spec, get_channel

__all__ = ["channel_for_paper", "channel_for_spec", "get_channel"]
