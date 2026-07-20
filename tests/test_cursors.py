from __future__ import annotations

import pytest

from video_context_mcp.cursors import (
    MAX_CURSOR_LENGTH,
    CursorError,
    cursor_scope,
    decode_cursor,
    encode_cursor,
)


def test_cursor_round_trip_and_scope() -> None:
    scope = cursor_scope("search", {"query": "python", "channel": "all"})
    cursor = encode_cursor(kind="search", offset=10, scope=scope)
    assert cursor.startswith("kf1.")
    assert len(cursor) < 40
    assert decode_cursor(cursor, kind="search", scope=scope) == 10


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64!",
        "W10",
        "",
        "kf1.s.+1.scope",
        "kf1.s.1.extra.scope",
        "kf1.s..scope",
        f"kf1.s.1.{'a' * MAX_CURSOR_LENGTH}",
        "kf1.s.zzzzzzzzzzzzz.scope",
    ],
)
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


def test_compact_cursor_cannot_be_reused_for_another_tool() -> None:
    cursor = encode_cursor(kind="search", offset=5, scope="same-scope")
    with pytest.raises(CursorError, match="does not match"):
        decode_cursor(cursor, kind="transcript", scope="same-scope")


def test_cursor_encoder_rejects_offset_beyond_sqlite_integer_range() -> None:
    with pytest.raises(ValueError, match="offset"):
        encode_cursor(kind="transcript", offset=1 << 63, scope="scope")


def test_legacy_cursor_with_redundant_kind_typo_uses_intact_scope() -> None:
    # Captured from the desktop regression: the model changed "transcript" to
    # "trmcript" inside the old base64 token but preserved the query-bound
    # scope and offset. Kind is already part of the scope digest.
    cursor = (
        "eyJraW5kIjoidHJtY3JpcHQiLCJvZmZzZXQiOjIwMCwic2NvcGUiOiIyMzMzMjQxY2Fh"
        "MjkyZTlkIiwidiI6MX0"
    )
    assert (
        decode_cursor(
            cursor,
            kind="transcript",
            scope="2333241caa292e9d",
        )
        == 200
    )
