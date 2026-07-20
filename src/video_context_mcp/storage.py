from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path

from video_context_mcp.errors import CacheError
from video_context_mcp.models import (
    Chapter,
    IngestMode,
    MomentKind,
    SearchChannel,
    SearchHit,
    TranscriptMode,
    TranscriptSegment,
    VideoRecord,
    VisualCoverage,
    VisualMoment,
)

SCHEMA_VERSION = 5
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


class KeyframeStore:
    """Versioned SQLite cache with a unified transcript/OCR FTS index."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CacheError(
                f"Keyframe could not prepare the cache directory for {self.database_path}. "
                "Verify KEYFRAME_HOME is writable and that the disk has free space."
            ) from exc
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS videos (
                    video_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    availability TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    duration_s REAL NOT NULL CHECK(duration_s >= 0),
                    chapters_json TEXT NOT NULL,
                    has_transcript INTEGER NOT NULL,
                    has_audio INTEGER NOT NULL DEFAULT 1,
                    transcript_mode TEXT NOT NULL,
                    indexed_mode TEXT NOT NULL,
                    visual_coverage TEXT NOT NULL,
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

                CREATE TABLE IF NOT EXISTS transcript_segments (
                    segment_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
                    start_s REAL NOT NULL,
                    end_s REAL NOT NULL,
                    text TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS transcript_video_time
                    ON transcript_segments(video_id, start_s, segment_id);

                CREATE TABLE IF NOT EXISTS moments (
                    moment_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
                    actual_t REAL NOT NULL,
                    start_s REAL NOT NULL,
                    end_s REAL NOT NULL,
                    kind TEXT NOT NULL,
                    classification_confidence REAL NOT NULL,
                    stable_seconds REAL NOT NULL,
                    ocr_text TEXT NOT NULL,
                    ocr_confidence REAL NOT NULL,
                    language_guess TEXT,
                    code TEXT NOT NULL,
                    parses INTEGER,
                    notes_json TEXT NOT NULL,
                    frame_path TEXT NOT NULL,
                    crop_path TEXT
                );
                CREATE INDEX IF NOT EXISTS moments_video_time
                    ON moments(video_id, start_s, moment_id);

                CREATE TABLE IF NOT EXISTS search_content (
                    id INTEGER PRIMARY KEY,
                    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    ref_id TEXT NOT NULL,
                    start_s REAL NOT NULL,
                    end_s REAL NOT NULL,
                    text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS search_video_channel
                    ON search_content(video_id, channel);

                CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                    text,
                    content='search_content',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );
                """
            )
            version = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                current_version = int(version["value"])
                if current_version == 1:
                    self._migrate_v1_to_v2(connection)
                    current_version = 2
                if current_version == 2:
                    self._migrate_v2_to_v3(connection)
                    current_version = 3
                if current_version == 3:
                    self._migrate_v3_to_v4(connection)
                    current_version = 4
                if current_version == 4:
                    self._migrate_v4_to_v5(connection)
                    current_version = 5
                if current_version != SCHEMA_VERSION:
                    raise CacheError(
                        f"Unsupported Keyframe cache schema {version['value']}; expected "
                        f"{SCHEMA_VERSION}."
                    )

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        """Record whether visuals are absent, sparsely probed, or fully indexed."""

        video_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(videos)").fetchall()
        }
        if "visual_coverage" not in video_columns:
            connection.execute(
                "ALTER TABLE videos ADD COLUMN visual_coverage TEXT NOT NULL DEFAULT 'none'"
            )
            connection.execute(
                "UPDATE videos SET visual_coverage = 'full' WHERE indexed_mode = 'full'"
            )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            ("4",),
        )

    @staticmethod
    def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
        """Persist whether speech transcription is possible for the source."""

        video_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(videos)").fetchall()
        }
        if "has_audio" not in video_columns:
            connection.execute(
                "ALTER TABLE videos ADD COLUMN has_audio INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            ("5",),
        )

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        """Add transcript provenance and explicit classification confidence."""

        video_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(videos)").fetchall()
        }
        if "transcript_mode" not in video_columns:
            connection.execute(
                "ALTER TABLE videos ADD COLUMN transcript_mode TEXT NOT NULL DEFAULT 'auto'"
            )
        moment_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(moments)").fetchall()
        }
        if "classification_confidence" not in moment_columns:
            connection.execute(
                """ALTER TABLE moments ADD COLUMN classification_confidence
                REAL NOT NULL DEFAULT 0.0"""
            )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            ("2",),
        )

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        """Persist public, unlisted, or local source availability honestly."""

        video_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(videos)").fetchall()
        }
        if "availability" not in video_columns:
            connection.execute(
                "ALTER TABLE videos ADD COLUMN availability TEXT NOT NULL DEFAULT 'public'"
            )
            connection.execute(
                "UPDATE videos SET availability = 'local' WHERE source_kind = 'local'"
            )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            ("3",),
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        failure_in_flight = False
        try:
            connection = sqlite3.connect(self.database_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 30000")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            failure_in_flight = True
            if connection is not None:
                self._rollback_quietly(connection)
            if self._is_cache_database_failure(exc):
                raise self._cache_error(exc) from exc
            raise
        except BaseException:
            failure_in_flight = True
            if connection is not None:
                self._rollback_quietly(connection)
            raise
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error as exc:
                    if not failure_in_flight:
                        if self._is_cache_database_failure(exc):
                            raise self._cache_error(exc) from exc
                        raise

    @staticmethod
    def _rollback_quietly(connection: sqlite3.Connection) -> None:
        """Best-effort rollback without masking the operation's original failure."""

        with suppress(sqlite3.Error):
            connection.rollback()

    def _cache_error(self, error: sqlite3.Error) -> CacheError:
        """Translate SQLite failures into stable, actionable public errors."""

        error_name = str(getattr(error, "sqlite_errorname", "")).upper()
        error_text = str(error).lower()
        if "BUSY" in error_name or "LOCKED" in error_name or "locked" in error_text:
            action = "Close other Keyframe processes using this cache, then retry."
        elif (
            "CORRUPT" in error_name
            or "NOTADB" in error_name
            or "malformed" in error_text
            or "not a database" in error_text
        ):
            action = "Move the cache database aside and re-ingest the affected videos."
        elif any(
            marker in error_name for marker in ("CANTOPEN", "IOERR", "FULL", "PERM", "READONLY")
        ):
            action = (
                "Verify KEYFRAME_HOME is writable and that the disk has free space, then retry."
            )
        else:
            action = (
                "Retry once; if the failure persists, move the cache database aside and re-ingest."
            )
        return CacheError(
            f"Keyframe cache database operation failed at {self.database_path}: {error}. {action}"
        )

    @staticmethod
    def _is_cache_database_failure(error: sqlite3.Error) -> bool:
        """Distinguish cache/runtime failures from caller and data-contract mistakes."""

        return isinstance(error, sqlite3.DatabaseError) and not isinstance(
            error,
            (
                sqlite3.DataError,
                sqlite3.IntegrityError,
                sqlite3.NotSupportedError,
                sqlite3.ProgrammingError,
            ),
        )

    def find_by_fingerprint(self, fingerprint: str) -> VideoRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM videos WHERE source_fingerprint = ?", (fingerprint,)
            ).fetchone()
        return self._video_from_row(row) if row else None

    def find_by_source(self, source: str, *, pipeline_version: str) -> VideoRecord | None:
        """Return the newest ready entry for an exact normalized source string."""

        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM videos
                WHERE source = ? AND pipeline_version = ? AND status = 'ready'
                ORDER BY updated_at DESC LIMIT 1""",
                (source, pipeline_version),
            ).fetchone()
        return self._video_from_row(row) if row else None

    def get_video(self, video_id: str) -> VideoRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return self._video_from_row(row) if row else None

    def save_video(
        self,
        video: VideoRecord,
        segments: Sequence[TranscriptSegment],
        moments: Sequence[VisualMoment],
    ) -> None:
        """Atomically replace a video's metadata, artifacts, and search index."""
        with self.connect() as connection:
            old_search_rows = connection.execute(
                "SELECT id FROM search_content WHERE video_id = ?", (video.video_id,)
            ).fetchall()
            for row in old_search_rows:
                connection.execute("DELETE FROM search_fts WHERE rowid = ?", (row["id"],))
            connection.execute("DELETE FROM search_content WHERE video_id = ?", (video.video_id,))
            connection.execute(
                "DELETE FROM transcript_segments WHERE video_id = ?", (video.video_id,)
            )
            connection.execute("DELETE FROM moments WHERE video_id = ?", (video.video_id,))
            connection.execute(
                """
                INSERT INTO videos(
                    video_id, source, source_kind, availability, source_fingerprint, title, duration_s,
                    chapters_json, has_transcript, has_audio, transcript_mode, indexed_mode,
                    visual_coverage,
                    keyframe_count, status,
                    warnings_json, local_source_path, local_source_size,
                    local_source_mtime_ns, pipeline_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    source=excluded.source,
                    source_kind=excluded.source_kind,
                    availability=excluded.availability,
                    source_fingerprint=excluded.source_fingerprint,
                    title=excluded.title,
                    duration_s=excluded.duration_s,
                    chapters_json=excluded.chapters_json,
                    has_transcript=excluded.has_transcript,
                    has_audio=excluded.has_audio,
                    transcript_mode=excluded.transcript_mode,
                    indexed_mode=excluded.indexed_mode,
                    visual_coverage=excluded.visual_coverage,
                    keyframe_count=excluded.keyframe_count,
                    status=excluded.status,
                    warnings_json=excluded.warnings_json,
                    local_source_path=excluded.local_source_path,
                    local_source_size=excluded.local_source_size,
                    local_source_mtime_ns=excluded.local_source_mtime_ns,
                    pipeline_version=excluded.pipeline_version,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    video.video_id,
                    video.source,
                    video.source_kind,
                    video.availability,
                    video.source_fingerprint,
                    video.title,
                    video.duration_s,
                    json.dumps([chapter.model_dump(mode="json") for chapter in video.chapters]),
                    int(video.has_transcript),
                    int(video.has_audio),
                    video.transcript_mode.value,
                    video.indexed_mode.value,
                    video.visual_coverage.value,
                    video.keyframe_count,
                    video.status,
                    json.dumps(video.warnings),
                    video.local_source_path,
                    video.local_source_size,
                    video.local_source_mtime_ns,
                    video.pipeline_version,
                ),
            )
            for segment in segments:
                connection.execute(
                    """INSERT INTO transcript_segments
                    (segment_id, video_id, start_s, end_s, text, source)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        segment.segment_id,
                        video.video_id,
                        segment.start_s,
                        segment.end_s,
                        segment.text,
                        segment.source,
                    ),
                )
                self._insert_search(
                    connection,
                    video.video_id,
                    SearchChannel.SAID,
                    segment.segment_id,
                    segment.start_s,
                    segment.end_s,
                    segment.text,
                )
            for moment in moments:
                connection.execute(
                    """INSERT INTO moments(
                    moment_id, video_id, actual_t, start_s, end_s, kind,
                    classification_confidence, stable_seconds,
                    ocr_text, ocr_confidence, language_guess, code, parses,
                    notes_json, frame_path, crop_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        moment.moment_id,
                        moment.video_id,
                        moment.actual_t,
                        moment.start_s,
                        moment.end_s,
                        moment.kind.value,
                        moment.classification_confidence,
                        moment.stable_seconds,
                        moment.ocr_text,
                        moment.ocr_confidence,
                        moment.language_guess,
                        moment.code,
                        None if moment.parses is None else int(moment.parses),
                        json.dumps(moment.notes),
                        moment.frame_path,
                        moment.crop_path,
                    ),
                )
                if moment.ocr_text.strip():
                    self._insert_search(
                        connection,
                        video.video_id,
                        SearchChannel.SHOWN,
                        moment.moment_id,
                        moment.start_s,
                        moment.end_s,
                        moment.ocr_text,
                    )

    def transcript_page(
        self,
        video_id: str,
        *,
        start_s: float | None,
        end_s: float | None,
        offset: int,
        limit: int,
    ) -> tuple[list[TranscriptSegment], bool]:
        clauses = ["video_id = ?"]
        values: list[object] = [video_id]
        if start_s is not None:
            clauses.append("end_s >= ?")
            values.append(start_s)
        if end_s is not None:
            clauses.append("start_s <= ?")
            values.append(end_s)
        values.extend([limit + 1, offset])
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM transcript_segments
                WHERE {" AND ".join(clauses)}
                ORDER BY start_s, segment_id LIMIT ? OFFSET ?""",
                values,
            ).fetchall()
        has_more = len(rows) > limit
        return [self._segment_from_row(row) for row in rows[:limit]], has_more

    def moment_page(
        self,
        video_id: str,
        *,
        kind: MomentKind,
        offset: int,
        limit: int,
    ) -> tuple[list[VisualMoment], bool]:
        clauses = ["video_id = ?"]
        values: list[object] = [video_id]
        if kind is not MomentKind.ANY:
            clauses.append("kind = ?")
            values.append(kind.value)
        values.extend([limit + 1, offset])
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM moments WHERE {" AND ".join(clauses)}
                ORDER BY start_s, moment_id LIMIT ? OFFSET ?""",
                values,
            ).fetchall()
        has_more = len(rows) > limit
        return [self._moment_from_row(row) for row in rows[:limit]], has_more

    def search(
        self,
        query: str,
        *,
        video_id: str | None,
        channel: SearchChannel,
        offset: int,
        limit: int,
    ) -> tuple[list[SearchHit], bool]:
        strict_query, broad_query = _fts_queries(query)
        filters: list[str] = []
        filter_values: list[object] = []
        if video_id is not None:
            filters.append("content.video_id = ?")
            filter_values.append(video_id)
        if channel is not SearchChannel.ALL:
            filters.append("content.channel = ?")
            filter_values.append(channel.value)
        with self.connect() as connection:
            match_query = strict_query
            if broad_query != strict_query:
                existence = connection.execute(
                    f"""SELECT 1
                    FROM search_fts
                    JOIN search_content AS content ON content.id = search_fts.rowid
                    WHERE {" AND ".join(["search_fts MATCH ?", *filters])}
                    LIMIT 1""",
                    [strict_query, *filter_values],
                ).fetchone()
                if existence is None:
                    match_query = broad_query
            clauses = ["search_fts MATCH ?", *filters]
            values = [match_query, *filter_values, limit + 1, offset]
            rows = connection.execute(
                f"""
                SELECT content.*, bm25(search_fts) AS rank,
                       snippet(search_fts, 0, '[', ']', ' … ', 18) AS snippet
                FROM search_fts
                JOIN search_content AS content ON content.id = search_fts.rowid
                WHERE {" AND ".join(clauses)}
                ORDER BY rank, content.start_s, content.id
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        has_more = len(rows) > limit
        hits = []
        for row in rows[:limit]:
            row_channel = SearchChannel(row["channel"])
            hits.append(
                SearchHit(
                    video_id=row["video_id"],
                    start_s=row["start_s"],
                    end_s=row["end_s"],
                    channel=row_channel,
                    snippet=row["snippet"],
                    score=round(-float(row["rank"]), 8),
                    segment_id=row["ref_id"] if row_channel is SearchChannel.SAID else None,
                    moment_id=row["ref_id"] if row_channel is SearchChannel.SHOWN else None,
                )
            )
        return hits, has_more

    def get_moment(self, moment_id: str) -> VisualMoment | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM moments WHERE moment_id = ?", (moment_id,)
            ).fetchone()
        return self._moment_from_row(row) if row else None

    def segments_for_video(self, video_id: str) -> list[TranscriptSegment]:
        """Return all transcript segments for ingestion refresh bookkeeping."""

        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM transcript_segments
                WHERE video_id = ? ORDER BY start_s, segment_id""",
                (video_id,),
            ).fetchall()
        return [self._segment_from_row(row) for row in rows]

    def moments_for_video(self, video_id: str) -> list[VisualMoment]:
        """Return all moments for ingestion refresh and artifact bookkeeping."""

        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM moments WHERE video_id = ? ORDER BY start_s, moment_id""",
                (video_id,),
            ).fetchall()
        return [self._moment_from_row(row) for row in rows]

    def artifact_paths(self) -> set[str]:
        """Return every relative artifact path referenced by the committed cache."""

        with self.connect() as connection:
            rows = connection.execute("SELECT frame_path, crop_path FROM moments").fetchall()
        return {
            str(value) for row in rows for value in (row["frame_path"], row["crop_path"]) if value
        }

    def nearest_moment(
        self,
        video_id: str,
        t: float,
        *,
        code_only: bool,
        tolerance_s: float | None,
    ) -> VisualMoment | None:
        clauses = ["video_id = ?"]
        values: list[object] = [video_id]
        if code_only:
            clauses.append("kind IN ('code', 'terminal')")
        if tolerance_s is not None:
            clauses.append(
                "(? BETWEEN start_s AND end_s OR MIN(ABS(start_s - ?), ABS(end_s - ?)) <= ?)"
            )
            values.extend([t, t, t, tolerance_s])
        values.extend([t, t, t])
        with self.connect() as connection:
            row = connection.execute(
                f"""SELECT * FROM moments WHERE {" AND ".join(clauses)}
                ORDER BY CASE WHEN ? BETWEEN start_s AND end_s THEN 0 ELSE 1 END,
                         MIN(ABS(start_s - ?), ABS(end_s - ?)), start_s
                LIMIT 1""",
                values,
            ).fetchone()
        return self._moment_from_row(row) if row else None

    @staticmethod
    def _insert_search(
        connection: sqlite3.Connection,
        video_id: str,
        channel: SearchChannel,
        ref_id: str,
        start_s: float,
        end_s: float,
        text: str,
    ) -> None:
        cursor = connection.execute(
            """INSERT INTO search_content(video_id, channel, ref_id, start_s, end_s, text)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (video_id, channel.value, ref_id, start_s, end_s, text),
        )
        rowid = cursor.lastrowid
        connection.execute("INSERT INTO search_fts(rowid, text) VALUES (?, ?)", (rowid, text))

    @staticmethod
    def _video_from_row(row: sqlite3.Row) -> VideoRecord:
        return VideoRecord(
            video_id=row["video_id"],
            source=row["source"],
            source_kind=row["source_kind"],
            availability=row["availability"],
            source_fingerprint=row["source_fingerprint"],
            title=row["title"],
            duration_s=row["duration_s"],
            chapters=tuple(
                Chapter.model_validate(item) for item in json.loads(row["chapters_json"])
            ),
            has_transcript=bool(row["has_transcript"]),
            has_audio=bool(row["has_audio"]),
            transcript_mode=TranscriptMode(row["transcript_mode"]),
            indexed_mode=IngestMode(row["indexed_mode"]),
            visual_coverage=VisualCoverage(row["visual_coverage"]),
            keyframe_count=row["keyframe_count"],
            status=row["status"],
            warnings=tuple(json.loads(row["warnings_json"])),
            local_source_path=row["local_source_path"],
            local_source_size=row["local_source_size"],
            local_source_mtime_ns=row["local_source_mtime_ns"],
            pipeline_version=row["pipeline_version"],
        )

    @staticmethod
    def _segment_from_row(row: sqlite3.Row) -> TranscriptSegment:
        return TranscriptSegment(
            segment_id=row["segment_id"],
            start_s=row["start_s"],
            end_s=row["end_s"],
            text=row["text"],
            source=row["source"],
        )

    @staticmethod
    def _moment_from_row(row: sqlite3.Row) -> VisualMoment:
        parses = row["parses"]
        return VisualMoment(
            moment_id=row["moment_id"],
            video_id=row["video_id"],
            actual_t=row["actual_t"],
            start_s=row["start_s"],
            end_s=row["end_s"],
            kind=MomentKind(row["kind"]),
            classification_confidence=row["classification_confidence"],
            stable_seconds=row["stable_seconds"],
            ocr_text=row["ocr_text"],
            ocr_confidence=row["ocr_confidence"],
            language_guess=row["language_guess"],
            code=row["code"],
            parses=None if parses is None else bool(parses),
            notes=tuple(json.loads(row["notes_json"])),
            frame_path=row["frame_path"],
            crop_path=row["crop_path"],
        )


def _fts_queries(query: str) -> tuple[str, str]:
    tokens = _TOKEN_RE.findall(query.strip())
    if not tokens:
        raise CacheError("Search query must contain at least one letter or number.")
    quoted = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:32]]
    return " AND ".join(quoted), " OR ".join(quoted)
