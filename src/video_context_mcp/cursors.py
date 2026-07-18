from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from video_context_mcp.errors import KeyframeError


class CursorError(KeyframeError):
    """An opaque page cursor is malformed or belongs to another query."""


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
        raise CursorError("Invalid page cursor. Start again without cursor.") from exc
    if not isinstance(payload, dict):
        raise CursorError("Invalid page cursor. Start again without cursor.")
    if payload.get("v") != 1 or payload.get("kind") != kind or payload.get("scope") != scope:
        raise CursorError("Cursor does not belong to this query. Start again without cursor.")
    offset = payload.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CursorError("Invalid page cursor offset. Start again without cursor.")
    return offset
