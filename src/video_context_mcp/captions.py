"""Shared helpers for de-overlapping rolling automatic captions."""

from __future__ import annotations

import re
from collections.abc import Sequence

_TOKEN_EDGE_RE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)
_BOUNDARY_EPSILON_SECONDS = 0.02
_BRIDGE_MAX_SECONDS = 0.05


def caption_token_overlap(previous: Sequence[str], current: Sequence[str]) -> int:
    previous_normalized = [_normalize_caption_token(token) for token in previous]
    current_normalized = [_normalize_caption_token(token) for token in current]
    for length in range(min(len(previous), len(current)), 0, -1):
        if previous_normalized[-length:] == current_normalized[:length]:
            return length
    return 0


def is_rolling_caption_pair(
    *,
    previous_start_s: float,
    previous_end_s: float,
    previous_text: str,
    current_start_s: float,
    current_end_s: float,
    current_text: str,
) -> bool:
    """Recognize overlapping cues and YouTube's short boundary-bridge cues."""

    if current_start_s < previous_end_s:
        return True
    if abs(current_start_s - previous_end_s) > _BOUNDARY_EPSILON_SECONDS:
        return False
    previous_duration = max(0.0, previous_end_s - previous_start_s)
    current_duration = max(0.0, current_end_s - current_start_s)
    return (
        min(previous_duration, current_duration) <= _BRIDGE_MAX_SECONDS
        or "\n" in previous_text
        or "\n" in current_text
    )


def _normalize_caption_token(token: str) -> str:
    stripped = _TOKEN_EDGE_RE.sub("", token.casefold())
    return stripped or token.casefold()
