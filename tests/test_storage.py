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
    SpeechActionPhase,
    TranscriptSegment,
    VideoRecord,
    VisualCoverage,
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
        has_audio=True,
        indexed_mode=IngestMode.FULL,
        visual_coverage=VisualCoverage.FULL,
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
    assert video.visual_coverage is VisualCoverage.FULL
    assert store.find_by_fingerprint(video.source_fingerprint) == video
    assert store.transcript_sources(video.video_id) == frozenset({"captions"})

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


def test_search_broadens_only_when_all_terms_have_no_scoped_match(
    store: KeyframeStore,
) -> None:
    second_segment = sample_segment().model_copy(
        update={
            "segment_id": "file_deadbeef:s:1",
            "start_s": 5,
            "end_s": 6,
            "text": "absentword appears elsewhere",
        }
    )
    store.save_video(sample_video(), [sample_segment(), second_segment], [sample_moment()])

    broad, _ = store.search(
        "multiple absentword",
        video_id=sample_video().video_id,
        channel=SearchChannel.SAID,
        offset=0,
        limit=10,
    )
    strict, _ = store.search(
        "multiple exception",
        video_id=sample_video().video_id,
        channel=SearchChannel.SAID,
        offset=0,
        limit=10,
    )
    wrong_channel, _ = store.search(
        "multiple absentword",
        video_id=sample_video().video_id,
        channel=SearchChannel.SHOWN,
        offset=0,
        limit=10,
    )

    assert {hit.segment_id for hit in broad} == {
        sample_segment().segment_id,
        second_segment.segment_id,
    }
    assert strict[0].segment_id == sample_segment().segment_id
    assert wrong_channel == []

    first_page, has_more = store.search(
        "multiple absentword",
        video_id=sample_video().video_id,
        channel=SearchChannel.SAID,
        offset=0,
        limit=1,
    )
    second_page, second_has_more = store.search(
        "multiple absentword",
        video_id=sample_video().video_id,
        channel=SearchChannel.SAID,
        offset=1,
        limit=1,
    )
    assert has_more is True
    assert second_has_more is False
    assert first_page[0].segment_id != second_page[0].segment_id


def test_said_search_adds_deoverlapped_rolling_caption_context(
    store: KeyframeStore,
) -> None:
    segments = [
        TranscriptSegment(
            segment_id="file_deadbeef:s:10",
            start_s=10,
            end_s=12,
            text="with that done it's time to",
            source="automatic_captions",
        ),
        TranscriptSegment(
            segment_id="file_deadbeef:s:11",
            start_s=11,
            end_s=13,
            text="it's time to screw the board down now",
            source="automatic_captions",
        ),
        TranscriptSegment(
            segment_id="file_deadbeef:s:12",
            start_s=12.5,
            end_s=15,
            text="the board down now each case differs",
            source="automatic_captions",
        ),
    ]
    video = sample_video().model_copy(update={"duration_s": 20})
    store.save_video(video, segments, [sample_moment()])

    hits, has_more = store.search(
        "board",
        video_id=video.video_id,
        channel=SearchChannel.SAID,
        start_s=10,
        end_s=16,
        offset=0,
        limit=10,
    )

    assert has_more is False
    assert hits
    assert {hit.context for hit in hits} == {
        "with that done it's time to screw the board down now each case differs"
    }
    assert {hit.start_s: hit.action_phase for hit in hits} == {
        11: SpeechActionPhase.ANNOUNCEMENT,
        12.5: SpeechActionPhase.UNKNOWN,
    }


def test_said_search_context_deoverlaps_boundary_bridged_youtube_cues(
    store: KeyframeStore,
) -> None:
    segments = [
        TranscriptSegment(
            segment_id="file_deadbeef:s:20",
            start_s=10,
            end_s=12,
            text="do not fully tighten\nleaving a little slack",
            source="automatic_captions",
        ),
        TranscriptSegment(
            segment_id="file_deadbeef:s:21",
            start_s=12,
            end_s=12.01,
            text="leaving a little slack",
            source="automatic_captions",
        ),
        TranscriptSegment(
            segment_id="file_deadbeef:s:22",
            start_s=12.01,
            end_s=14,
            text="leaving a little slack\nmotherboard as you are putting it in",
            source="automatic_captions",
        ),
        TranscriptSegment(
            segment_id="file_deadbeef:s:23",
            start_s=14,
            end_s=14.01,
            text="motherboard as you are putting it in",
            source="automatic_captions",
        ),
        TranscriptSegment(
            segment_id="file_deadbeef:s:24",
            start_s=14.01,
            end_s=16,
            text="motherboard as you are putting it in\nadd the rest of the screws",
            source="automatic_captions",
        ),
    ]
    video = sample_video().model_copy(update={"duration_s": 20})
    store.save_video(video, segments, [sample_moment()])

    hits, _has_more = store.search(
        "motherboard",
        video_id=video.video_id,
        channel=SearchChannel.SAID,
        start_s=10,
        end_s=17,
        offset=0,
        limit=10,
    )

    assert hits
    for hit in hits:
        assert hit.context is not None
        assert hit.action_phase is SpeechActionPhase.IN_PROGRESS
        assert hit.context.count("leaving a little slack") == 1
        assert hit.context.count("motherboard as you are putting it in") == 1
        assert hit.context.count("add the rest of the screws") == 1


def test_said_search_labels_completed_action_context(store: KeyframeStore) -> None:
    segment = TranscriptSegment(
        segment_id="file_deadbeef:s:25",
        start_s=4,
        end_s=6,
        text="the motherboard should now be perfectly aligned and tightened down completely",
        source="captions",
    )
    store.save_video(sample_video(), [segment], [sample_moment()])

    hits, _has_more = store.search(
        "motherboard aligned",
        video_id=sample_video().video_id,
        channel=SearchChannel.SAID,
        offset=0,
        limit=10,
    )

    assert hits[0].action_phase is SpeechActionPhase.COMPLETED


@pytest.mark.parametrize(
    ("matched_text", "query", "expected_phase"),
    [
        (
            "the motherboard should now be aligned and down completely",
            "aligned",
            SpeechActionPhase.COMPLETED,
        ),
        (
            "while we're mounting the motherboard onto the standoffs",
            "standoffs",
            SpeechActionPhase.IN_PROGRESS,
        ),
    ],
)
def test_said_search_phase_comes_from_matched_cue_not_mixed_neighbor_context(
    store: KeyframeStore,
    matched_text: str,
    query: str,
    expected_phase: SpeechActionPhase,
) -> None:
    segments = [
        TranscriptSegment(
            segment_id="file_deadbeef:s:40",
            start_s=10,
            end_s=12,
            text="next up we're going to install the motherboard",
            source="captions",
        ),
        TranscriptSegment(
            segment_id="file_deadbeef:s:41",
            start_s=14,
            end_s=16,
            text=matched_text,
            source="captions",
        ),
        TranscriptSegment(
            segment_id="file_deadbeef:s:42",
            start_s=17,
            end_s=19,
            text="the installation is all set and finished installing",
            source="captions",
        ),
    ]
    video = sample_video().model_copy(update={"duration_s": 20})
    store.save_video(video, segments, [sample_moment()])

    hits, _has_more = store.search(
        query,
        video_id=video.video_id,
        channel=SearchChannel.SAID,
        offset=0,
        limit=10,
    )

    assert len(hits) == 1
    assert hits[0].start_s == 14
    assert hits[0].segment_id == "file_deadbeef:s:41"
    assert hits[0].context is not None
    assert "going to install" in hits[0].context
    assert "finished installing" in hits[0].context
    assert hits[0].action_phase is expected_phase


def test_said_search_context_stays_inside_caller_time_bounds(
    store: KeyframeStore,
) -> None:
    segments = [
        TranscriptSegment(
            segment_id="file_deadbeef:s:30",
            start_s=7,
            end_s=10,
            text="previous chapter secret",
            source="captions",
        ),
        TranscriptSegment(
            segment_id="file_deadbeef:s:31",
            start_s=10,
            end_s=12,
            text="board installation begins",
            source="captions",
        ),
        TranscriptSegment(
            segment_id="file_deadbeef:s:32",
            start_s=12,
            end_s=15,
            text="next chapter cable routing",
            source="captions",
        ),
    ]
    video = sample_video().model_copy(update={"duration_s": 20})
    store.save_video(video, segments, [sample_moment()])

    hits, _has_more = store.search(
        "board",
        video_id=video.video_id,
        channel=SearchChannel.SAID,
        start_s=10,
        end_s=12,
        offset=0,
        limit=10,
    )

    assert len(hits) == 1
    assert hits[0].context == "board installation begins"


@pytest.mark.parametrize(
    ("coverage", "indexed_mode"),
    (
        (VisualCoverage.NONE, IngestMode.FAST),
        (VisualCoverage.PROBE, IngestMode.FAST),
        (VisualCoverage.FULL, IngestMode.FULL),
    ),
)
def test_visual_coverage_round_trip(
    store: KeyframeStore,
    coverage: VisualCoverage,
    indexed_mode: IngestMode,
) -> None:
    video = sample_video().model_copy(
        update={
            "indexed_mode": indexed_mode,
            "visual_coverage": coverage,
            "keyframe_count": 0,
        }
    )

    store.save_video(video, (), ())

    persisted = store.get_video(video.video_id)
    assert persisted is not None
    assert persisted.indexed_mode is indexed_mode
    assert persisted.visual_coverage is coverage


def test_v3_migration_maps_legacy_visual_coverage_honestly_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-v3.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO metadata(key, value) VALUES('schema_version', '3');

            CREATE TABLE videos (
                video_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                availability TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                duration_s REAL NOT NULL CHECK(duration_s >= 0),
                chapters_json TEXT NOT NULL,
                has_transcript INTEGER NOT NULL,
                transcript_mode TEXT NOT NULL,
                indexed_mode TEXT NOT NULL,
                keyframe_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                local_source_path TEXT,
                local_source_size INTEGER,
                local_source_mtime_ns INTEGER,
                pipeline_version TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT INTO videos(
                video_id, source, source_kind, availability, source_fingerprint,
                title, duration_s, chapters_json, has_transcript, transcript_mode,
                indexed_mode, keyframe_count, status, warnings_json, pipeline_version
            ) VALUES
                (
                    'legacy-fast', '/tmp/fast.mp4', 'local', 'local',
                    'sha256:fast:pipeline:1', 'Legacy fast', 10, '[]', 1,
                    'captions', 'fast', 0, 'ready', '[]', '1'
                ),
                (
                    'legacy-full', '/tmp/full.mp4', 'local', 'local',
                    'sha256:full:pipeline:1', 'Legacy full', 10, '[]', 1,
                    'captions', 'full', 0, 'ready', '[]', '1'
                );
            """
        )

    store = KeyframeStore(database_path)
    store.initialize()
    store.initialize()

    legacy_fast = store.get_video("legacy-fast")
    legacy_full = store.get_video("legacy-full")
    assert legacy_fast is not None
    assert legacy_full is not None
    assert legacy_fast.visual_coverage is VisualCoverage.NONE
    assert legacy_full.visual_coverage is VisualCoverage.FULL

    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(videos)")]

    assert version == ("5",)
    assert columns.count("visual_coverage") == 1
    assert columns.count("has_audio") == 1


def test_v4_migration_defaults_legacy_audio_capability_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-v4.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata(key, value) VALUES('schema_version', '4');
            CREATE TABLE videos (
                video_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                availability TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                duration_s REAL NOT NULL,
                chapters_json TEXT NOT NULL,
                has_transcript INTEGER NOT NULL,
                transcript_mode TEXT NOT NULL,
                indexed_mode TEXT NOT NULL,
                visual_coverage TEXT NOT NULL,
                keyframe_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                local_source_path TEXT,
                local_source_size INTEGER,
                local_source_mtime_ns INTEGER,
                pipeline_version TEXT NOT NULL
            );
            INSERT INTO videos VALUES(
                'legacy', '/tmp/legacy.mp4', 'local', 'local', 'legacy-fingerprint',
                'Legacy', 2, '[]', 0, 'auto', 'fast', 'probe', 1, 'ready', '[]',
                NULL, NULL, NULL, '2'
            );
            """
        )

    store = KeyframeStore(database_path)
    store.initialize()
    store.initialize()

    video = store.get_video("legacy")
    assert video is not None
    assert video.has_audio is True
    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(videos)")]
    assert version == ("5",)
    assert columns.count("has_audio") == 1


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


def test_full_save_replaces_probe_moments_and_shown_fts(store: KeyframeStore) -> None:
    probe_video = sample_video().model_copy(
        update={
            "indexed_mode": IngestMode.FAST,
            "visual_coverage": VisualCoverage.PROBE,
        }
    )
    probe_moment = sample_moment().model_copy(
        update={
            "moment_id": "file_deadbeef:m:probe:0",
            "ocr_text": "probesentinel",
            "code": "probesentinel",
        }
    )
    store.save_video(probe_video, [sample_segment()], [probe_moment])

    full_video = sample_video()
    full_moment = sample_moment().model_copy(
        update={
            "moment_id": "file_deadbeef:m:full:0",
            "ocr_text": "fullsentinel",
            "code": "fullsentinel",
        }
    )
    store.save_video(full_video, [sample_segment()], [full_moment])

    persisted = store.get_video(full_video.video_id)
    assert persisted is not None
    assert persisted.visual_coverage is VisualCoverage.FULL
    assert store.moments_for_video(full_video.video_id) == [full_moment]
    assert store.get_moment(probe_moment.moment_id) is None

    old_hits, _ = store.search(
        "probesentinel",
        video_id=full_video.video_id,
        channel=SearchChannel.SHOWN,
        offset=0,
        limit=10,
    )
    new_hits, _ = store.search(
        "fullsentinel",
        video_id=full_video.video_id,
        channel=SearchChannel.SHOWN,
        offset=0,
        limit=10,
    )
    assert old_hits == []
    assert [hit.moment_id for hit in new_hits] == [full_moment.moment_id]


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


def test_transcript_time_bounds_are_half_open(store: KeyframeStore) -> None:
    early = sample_segment()
    late = early.model_copy(
        update={
            "segment_id": "file_deadbeef:s:1",
            "start_s": 3,
            "end_s": 5,
            "text": "later chapter evidence",
        }
    )
    store.save_video(sample_video(), [early, late], [sample_moment()])

    before, before_more = store.transcript_page(
        sample_video().video_id,
        start_s=None,
        end_s=3,
        offset=0,
        limit=10,
    )
    after, after_more = store.transcript_page(
        sample_video().video_id,
        start_s=3,
        end_s=None,
        offset=0,
        limit=10,
    )

    assert before == [early] and before_more is False
    assert after == [late] and after_more is False


def test_visual_time_bounds_exclude_disjoint_moments_and_search_hits(
    store: KeyframeStore,
) -> None:
    early = sample_moment()
    late = early.model_copy(
        update={
            "moment_id": "file_deadbeef:m:1",
            "actual_t": 9,
            "start_s": 7,
            "end_s": 10,
            "stable_seconds": 3,
            "ocr_text": "except ValueError in the later example",
            "code": "except ValueError:\n    pass",
        }
    )
    store.save_video(sample_video(), [sample_segment()], [early, late])

    early_page, early_more = store.moment_page(
        sample_video().video_id,
        kind=MomentKind.ANY,
        start_s=None,
        end_s=7,
        offset=0,
        limit=10,
    )
    late_page, late_more = store.moment_page(
        sample_video().video_id,
        kind=MomentKind.ANY,
        start_s=7,
        end_s=None,
        offset=0,
        limit=10,
    )
    assert early_page == [early] and early_more is False
    assert late_page == [late] and late_more is False

    early_hits, early_hits_more = store.search(
        "ValueError",
        video_id=sample_video().video_id,
        channel=SearchChannel.SHOWN,
        start_s=None,
        end_s=7,
        offset=0,
        limit=10,
    )
    late_hits, late_hits_more = store.search(
        "ValueError",
        video_id=sample_video().video_id,
        channel=SearchChannel.SHOWN,
        start_s=7,
        end_s=None,
        offset=0,
        limit=10,
    )
    assert [hit.moment_id for hit in early_hits] == [early.moment_id]
    assert [hit.moment_id for hit in late_hits] == [late.moment_id]
    assert early_hits_more is False
    assert late_hits_more is False


def test_half_open_bounds_include_zero_duration_evidence_at_start_only(
    store: KeyframeStore,
) -> None:
    point_segment = sample_segment().model_copy(
        update={
            "segment_id": "file_deadbeef:s:point",
            "start_s": 3,
            "end_s": 3,
            "text": "point transcript marker",
        }
    )
    point_moment = sample_moment().model_copy(
        update={
            "moment_id": "file_deadbeef:m:point",
            "actual_t": 3,
            "start_s": 3,
            "end_s": 3,
            "stable_seconds": 0,
            "ocr_text": "point visual marker",
        }
    )
    store.save_video(sample_video(), [point_segment], [point_moment])

    transcript_at_start, _ = store.transcript_page(
        sample_video().video_id, start_s=3, end_s=4, offset=0, limit=10
    )
    transcript_at_end, _ = store.transcript_page(
        sample_video().video_id, start_s=2, end_s=3, offset=0, limit=10
    )
    moments_at_start, _ = store.moment_page(
        sample_video().video_id,
        kind=MomentKind.ANY,
        start_s=3,
        end_s=4,
        offset=0,
        limit=10,
    )
    moments_at_end, _ = store.moment_page(
        sample_video().video_id,
        kind=MomentKind.ANY,
        start_s=2,
        end_s=3,
        offset=0,
        limit=10,
    )
    hits_at_start, _ = store.search(
        "point visual",
        video_id=sample_video().video_id,
        channel=SearchChannel.SHOWN,
        start_s=3,
        end_s=4,
        offset=0,
        limit=10,
    )
    hits_at_end, _ = store.search(
        "point visual",
        video_id=sample_video().video_id,
        channel=SearchChannel.SHOWN,
        start_s=2,
        end_s=3,
        offset=0,
        limit=10,
    )

    assert transcript_at_start == [point_segment]
    assert transcript_at_end == []
    assert moments_at_start == [point_moment]
    assert moments_at_end == []
    assert [hit.moment_id for hit in hits_at_start] == [point_moment.moment_id]
    assert hits_at_end == []


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
