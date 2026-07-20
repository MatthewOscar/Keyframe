from __future__ import annotations

import base64
import hashlib
import json
import re
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
_COMPACT_CURSOR_PREFIX = "kf1"
_COMPACT_SCOPE_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_BASE36_RE = re.compile(r"[0-9a-z]+")
_CURSOR_KIND_CODES = {"transcript": "t", "search": "s", "moments": "m"}
MAX_CURSOR_LENGTH = 512
MAX_CURSOR_OFFSET = (1 << 63) - 1


def cursor_scope(kind: str, values: dict[str, Any]) -> str:
    canonical = json.dumps({"kind": kind, "values": values}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def encode_cursor(*, kind: str, offset: int, scope: str) -> str:
    kind_code = _CURSOR_KIND_CODES.get(kind)
    if (
        kind_code is None
        or not 0 <= offset <= MAX_CURSOR_OFFSET
        or _COMPACT_SCOPE_RE.fullmatch(scope) is None
    ):
        raise ValueError("Cursor kind, offset, or scope is invalid.")
    # The scope digest already binds the tool kind and every query filter. Keep
    # the token short so agents can copy it reliably without duplicating that
    # information in a long base64-encoded JSON object.
    return f"{_COMPACT_CURSOR_PREFIX}.{kind_code}.{_to_base36(offset)}.{scope}"


def decode_cursor(cursor: str | None, *, kind: str, scope: str) -> int:
    if cursor is None:
        return 0
    if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise CursorError(_INVALID_CURSOR_MESSAGE)
    if cursor.startswith(f"{_COMPACT_CURSOR_PREFIX}."):
        return _decode_compact_cursor(cursor, kind=kind, scope=scope)
    return _decode_legacy_cursor(cursor, kind=kind, scope=scope)


def _decode_compact_cursor(cursor: str, *, kind: str, scope: str) -> int:
    try:
        prefix, kind_code, encoded_offset, cursor_scope_value = cursor.split(".")
        if (
            prefix != _COMPACT_CURSOR_PREFIX
            or _BASE36_RE.fullmatch(encoded_offset) is None
        ):
            raise ValueError("invalid compact cursor")
        offset = int(encoded_offset, 36)
    except ValueError as exc:
        raise CursorError(_INVALID_CURSOR_MESSAGE) from exc
    if not 0 <= offset <= MAX_CURSOR_OFFSET:
        raise CursorError(_INVALID_CURSOR_MESSAGE)
    if kind_code != _CURSOR_KIND_CODES.get(kind) or cursor_scope_value != scope:
        raise CursorError(_CURSOR_SCOPE_MESSAGE)
    return offset


def _decode_legacy_cursor(cursor: str, *, kind: str, scope: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError(_INVALID_CURSOR_MESSAGE) from exc
    if not isinstance(payload, dict):
        raise CursorError(_INVALID_CURSOR_MESSAGE)
    # Scope already hashes the tool kind. Ignore the redundant legacy kind so
    # an otherwise intact v1 token remains usable; cross-query reuse still
    # fails on the scope check.
    if kind not in _CURSOR_KIND_CODES:
        raise CursorError(_INVALID_CURSOR_MESSAGE)
    if payload.get("v") != 1 or payload.get("scope") != scope:
        raise CursorError(_CURSOR_SCOPE_MESSAGE)
    offset = payload.get("offset")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or not 0 <= offset <= MAX_CURSOR_OFFSET
    ):
        raise CursorError(_INVALID_CURSOR_MESSAGE)
    return offset


def _to_base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    encoded = ""
    while value:
        value, remainder = divmod(value, 36)
        encoded = alphabet[remainder] + encoded
    return encoded
