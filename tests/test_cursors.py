from __future__ import annotations

import pytest

from video_context_mcp.cursors import CursorError, cursor_scope, decode_cursor, encode_cursor


def test_cursor_round_trip_and_scope() -> None:
    scope = cursor_scope("search", {"query": "python", "channel": "all"})
    cursor = encode_cursor(kind="search", offset=10, scope=scope)
    assert decode_cursor(cursor, kind="search", scope=scope) == 10


@pytest.mark.parametrize("cursor", ["not-base64!", "W10", ""])
def test_invalid_cursor_is_actionable(cursor: str) -> None:
    with pytest.raises(CursorError, match="restart this query once") as raised:
        decode_cursor(cursor, kind="search", scope="expected")
    assert "next_cursor byte-for-byte" in str(raised.value)
    assert "never decode, shorten, or reconstruct it" in str(raised.value)


def test_cursor_cannot_be_reused_for_another_query() -> None:
    cursor = encode_cursor(kind="search", offset=5, scope="first")
    with pytest.raises(CursorError, match="does not match") as raised:
        decode_cursor(cursor, kind="search", scope="second")
    assert "same video ID and filters" in str(raised.value)
    assert "next_cursor byte-for-byte" in str(raised.value)
