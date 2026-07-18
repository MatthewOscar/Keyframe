from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_context_mcp.errors import CacheError
from video_context_mcp.models import (
    Chapter,
    IngestMode,
    MomentKind,
    SearchChannel,
    TranscriptSegment,
    VideoRecord,
    VisualMoment,
)
from video_context_mcp.storage import KeyframeStore


@pytest.fixture
def store(tmp_path: Path) -> KeyframeStore:
    value = KeyframeStore(tmp_path / "cache.sqlite3")
    value.initialize()
    return value


def sample_video() -> VideoRecord:
    return VideoRecord(
        video_id="file_deadbeef",
        source="/tmp/demo.mp4",
        source_kind="local",
        availability="local",
        source_fingerprint="sha256:deadbeef:pipeline:1",
        title="Exception tutorial",
        duration_s=10,
        chapters=(Chapter(start_s=0, end_s=10, title="Demo"),),
        has_transcript=True,
        indexed_mode=IngestMode.FULL,
        keyframe_count=1,
        warnings=(),
        local_source_path="/tmp/demo.mp4",
        pipeline_version="1",
    )


def sample_segment() -> TranscriptSegment:
    return TranscriptSegment(
        segment_id="file_deadbeef:s:0",
        start_s=1,
        end_s=3,
        text="catch multiple exception types",
        source="captions",
    )


def sample_moment() -> VisualMoment:
    return VisualMoment(
        moment_id="file_deadbeef:m:0",
        video_id="file_deadbeef",
        actual_t=5,
        start_s=4,
        end_s=7,
        kind=MomentKind.CODE,
        stable_seconds=3,
        ocr_text="except (ValueError, TypeError):",
        ocr_confidence=0.91,
        language_guess="python",
        code="except (ValueError, TypeError):\n    pass",
        parses=False,
        notes=("Partial block",),
        frame_path="videos/file_deadbeef/frame.jpg",
    )


def test_round_trip_and_unified_search(store: KeyframeStore) -> None:
    store.save_video(sample_video(), [sample_segment()], [sample_moment()])

    video = store.get_video("file_deadbeef")
    assert video is not None and video.title == "Exception tutorial"
    assert store.find_by_fingerprint(video.source_fingerprint) == video

    said, said_more = store.search(
        "multiple exception",
        video_id=video.video_id,
        channel=SearchChannel.SAID,
        offset=0,
        limit=10,
    )
    shown, shown_more = store.search(
        "ValueError", video_id=video.video_id, channel=SearchChannel.SHOWN, offset=0, limit=10
    )
    assert not said_more and said[0].segment_id == sample_segment().segment_id
    assert not shown_more and shown[0].moment_id == sample_moment().moment_id


def test_save_replaces_old_fts_entries(store: KeyframeStore) -> None:
    store.save_video(sample_video(), [sample_segment()], [sample_moment()])
    replacement = sample_segment().model_copy(update={"text": "brand new transcript"})
    store.save_video(sample_video(), [replacement], [])

    old_hits, _ = store.search(
        "multiple", video_id=None, channel=SearchChannel.ALL, offset=0, limit=10
    )
    new_hits, _ = store.search(
        "brand", video_id=None, channel=SearchChannel.ALL, offset=0, limit=10
    )
    assert old_hits == []
    assert len(new_hits) == 1


def test_pages_and_nearest_moment(store: KeyframeStore) -> None:
    store.save_video(sample_video(), [sample_segment()], [sample_moment()])
    segments, has_more = store.transcript_page(
        "file_deadbeef", start_s=None, end_s=None, offset=0, limit=1
    )
    moments, moments_more = store.moment_page(
        "file_deadbeef", kind=MomentKind.ANY, offset=0, limit=1
    )
    nearest = store.nearest_moment("file_deadbeef", 5, code_only=True, tolerance_s=5)
    assert segments == [sample_segment()] and not has_more
    assert moments == [sample_moment()] and not moments_more
    assert nearest == sample_moment()


def test_empty_search_query_is_rejected(store: KeyframeStore) -> None:
    with pytest.raises(CacheError, match="letter or number"):
        store.search("!!!", video_id=None, channel=SearchChannel.ALL, offset=0, limit=10)


def test_initialize_translates_corrupt_database_error(tmp_path: Path) -> None:
    database_path = tmp_path / "cache.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")

    with pytest.raises(CacheError, match="Move the cache database aside") as raised:
        KeyframeStore(database_path).initialize()

    assert isinstance(raised.value.__cause__, sqlite3.DatabaseError)


def test_read_translates_sqlite_failure(
    store: KeyframeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)

    with pytest.raises(CacheError, match="Close other Keyframe processes") as raised:
        store.get_video("file_deadbeef")

    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)


def test_failed_save_is_translated_and_rolled_back(
    store: KeyframeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save_video(sample_video(), [sample_segment()], [sample_moment()])
    replacement = sample_segment().model_copy(update={"text": "replacement transcript"})

    def fail_search_insert(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(KeyframeStore, "_insert_search", staticmethod(fail_search_insert))

    with pytest.raises(CacheError, match="cache database operation failed") as raised:
        store.save_video(sample_video(), [replacement], [])

    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert store.segments_for_video("file_deadbeef") == [sample_segment()]
    assert store.moments_for_video("file_deadbeef") == [sample_moment()]


def test_store_does_not_translate_programming_or_validation_errors(store: KeyframeStore) -> None:
    with pytest.raises(sqlite3.ProgrammingError), store.connect() as connection:
        connection.execute("SELECT ?", ())

    store.save_video(sample_video(), [sample_segment()], [sample_moment()])
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE videos SET chapters_json = ? WHERE video_id = ?",
            ('[{"start_s": 0, "title": ""}]', "file_deadbeef"),
        )

    with pytest.raises(ValidationError):
        store.get_video("file_deadbeef")
