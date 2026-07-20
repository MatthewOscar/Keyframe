from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from video_context_mcp.errors import KeyframeError


class CursorError(KeyframeError):
    """An opaque page cursor is malformed or belongs to another query."""


_INVALID_CURSOR_MESSAGE = (
    "Invalid page cursor. Discard it and restart this query once without a cursor. For later "
    "pages, copy next_cursor byte-for-byte from the immediately preceding response; never "
    "decode, shorten, or reconstruct it."
)
_CURSOR_SCOPE_MESSAGE = (
    "Page cursor does not match this query or cache generation. Discard it and restart this "
    "query once without a cursor. On later pages, keep the same video ID and filters and copy "
    "next_cursor byte-for-byte."
)


def cursor_scope(kind: str, values: dict[str, Any]) -> str:
    canonical = json.dumps({"kind": kind, "values": values}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def encode_cursor(*, kind: str, offset: int, scope: str) -> str:
    payload = json.dumps(
        {"v": 1, "kind": kind, "offset": offset, "scope": scope},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str | None, *, kind: str, scope: str) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError(_INVALID_CURSOR_MESSAGE) from exc
    if not isinstance(payload, dict):
        raise CursorError(_INVALID_CURSOR_MESSAGE)
    if payload.get("v") != 1 or payload.get("kind") != kind or payload.get("scope") != scope:
        raise CursorError(_CURSOR_SCOPE_MESSAGE)
    offset = payload.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CursorError(_INVALID_CURSOR_MESSAGE)
    return offset
