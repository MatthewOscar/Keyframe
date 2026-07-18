"""Application service joining acquisition, extraction, storage, and MCP contracts."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

from filelock import FileLock, Timeout
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from video_context_mcp.acquisition import (
    AcquiredSource,
    SourceKind,
    acquire_source,
    classify_source,
    validate_local_path,
)
from video_context_mcp.acquisition import (
    TranscriptSegment as AcquiredTranscriptSegment,
)
from video_context_mcp.config import Settings
from video_context_mcp.constants import (
    MAX_CONFIGURABLE_DURATION_S,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_EDGE,
    MAX_MOMENT_LIMIT,
    MAX_SEARCH_LIMIT,
    MAX_TRANSCRIPT_LIMIT,
    PIPELINE_VERSION,
)
from video_context_mcp.cursors import cursor_scope, decode_cursor, encode_cursor
from video_context_mcp.errors import CacheError, ConfigurationError, ExtractionError, SourceError
from video_context_mcp.models import (
    Chapter,
    CodeResult,
    FrameRegion,
    FrameResult,
    IngestMode,
    IngestResult,
    MomentKind,
    MomentPage,
    MomentSummary,
    SearchChannel,
    SearchPage,
    TranscriptMode,
    TranscriptPage,
    TranscriptSegment,
    VideoRecord,
    VisualMoment,
)
from video_context_mcp.storage import KeyframeStore
from video_context_mcp.transcription import transcribe_media, whisper_available
from video_context_mcp.vision import (
    VisualMoment as ExtractedVisualMoment,
)
from video_context_mcp.vision import (
    auto_crop_text_region,
    encode_image,
    extract_visual_moments,
)

ProgressCallback = Callable[[float, str], None]
AcquireFunction = Callable[..., AcquiredSource]
VisionFunction = Callable[..., list[ExtractedVisualMoment]]
TranscribeFunction = Callable[..., tuple[AcquiredTranscriptSegment, ...]]

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VisualPayload[T: BaseModel]:
    """A structured tool result paired with one bounded source image."""

    result: T
    image_data: bytes
    mime_type: str


class KeyframeService:
    """Synchronous local video-index service used by both supported transports."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: KeyframeStore | None = None,
        acquire: AcquireFunction = acquire_source,
        extract_visuals: VisionFunction = extract_visual_moments,
        transcribe: TranscribeFunction = transcribe_media,
        has_whisper: Callable[[], bool] = whisper_available,
    ) -> None:
        self.settings = settings
        self.settings.ensure_directories()
        self.store = store or KeyframeStore(self.settings.cache_dir / "keyframe.sqlite3")
        self.store.initialize()
        self._acquire = acquire
        self._extract_visuals = extract_visuals
        self._transcribe = transcribe
        self._has_whisper = has_whisper
        self._locks_dir = self.settings.home / "locks"
        self._locks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._global_ingest_lock = FileLock(str(self._locks_dir / "ingest-global.lock"))
        self._recover_interrupted_work()

    @classmethod
    def from_env(cls) -> KeyframeService:
        return cls(settings=Settings.from_env())

    def ingest(
        self,
        source: str,
        *,
        mode: IngestMode = IngestMode.FAST,
        transcript_mode: TranscriptMode = TranscriptMode.AUTO,
        max_duration_s: int = 1_800,
        refresh: bool = False,
        progress: ProgressCallback | None = None,
        client_roots: Sequence[Path] = (),
    ) -> IngestResult:
        """Build or reuse a complete cache entry for one video."""

        try:
            selected_mode = IngestMode(mode)
            selected_transcript_mode = TranscriptMode(transcript_mode)
        except ValueError as exc:
            raise SourceError(f"Unsupported ingestion option: {exc}") from exc
        if (
            not isinstance(max_duration_s, int)
            or isinstance(max_duration_s, bool)
            or max_duration_s < 1
            or max_duration_s > MAX_CONFIGURABLE_DURATION_S
        ):
            raise SourceError(
                f"max_duration_s must be between 1 and {MAX_CONFIGURABLE_DURATION_S}."
            )

        request_settings = self._settings_with_client_roots(client_roots)
        normalized_source = self._normalize_source(source, request_settings)
        self._notify(progress, 2, "Validated video source")
        if not refresh:
            cached = self.store.find_by_source(normalized_source, pipeline_version=PIPELINE_VERSION)
            if (
                cached is not None
                and cached.duration_s <= max_duration_s
                and self._cache_satisfies(cached, selected_mode, selected_transcript_mode)
                and self._cache_source_is_current(cached)
            ):
                self._notify(progress, 100, "Using cached video index")
                return self._ingest_result(cached, cache_hit=True)

        lock_key = hashlib.sha256(normalized_source.encode("utf-8")).hexdigest()
        lock = FileLock(str(self._locks_dir / f"{lock_key}.lock"))
        try:
            with (
                self._global_ingest_lock.acquire(timeout=max_duration_s + 300),
                lock.acquire(timeout=max_duration_s + 300),
            ):
                if not refresh:
                    cached = self.store.find_by_source(
                        normalized_source, pipeline_version=PIPELINE_VERSION
                    )
                    if (
                        cached is not None
                        and cached.duration_s <= max_duration_s
                        and self._cache_satisfies(cached, selected_mode, selected_transcript_mode)
                        and self._cache_source_is_current(cached)
                    ):
                        return self._ingest_result(cached, cache_hit=True)
                return self._ingest_locked(
                    normalized_source,
                    mode=selected_mode,
                    transcript_mode=selected_transcript_mode,
                    max_duration_s=max_duration_s,
                    refresh=refresh,
                    progress=progress,
                    request_settings=request_settings,
                )
        except Timeout as exc:
            raise CacheError(
                "Another process is still ingesting this source. Wait for it to finish and retry."
            ) from exc

    def _ingest_locked(
        self,
        source: str,
        *,
        mode: IngestMode,
        transcript_mode: TranscriptMode,
        max_duration_s: int,
        refresh: bool,
        progress: ProgressCallback | None,
        request_settings: Settings,
    ) -> IngestResult:
        self._notify(progress, 8, "Inspecting video metadata and captions")
        acquired = self._acquire(
            source,
            request_settings,
            mode=mode.value,
            transcript_mode=transcript_mode.value,
            max_duration_s=max_duration_s,
            refresh=refresh,
        )
        extra_warnings: list[str] = []
        try:
            should_whisper = not acquired.transcript and transcript_mode in {
                TranscriptMode.AUTO,
                TranscriptMode.WHISPER,
            }
            if should_whisper and self._has_whisper() and acquired.media_path is None:
                acquired.cleanup()
                acquired = self._acquire(
                    source,
                    request_settings,
                    mode=mode.value,
                    transcript_mode=TranscriptMode.WHISPER.value,
                    max_duration_s=max_duration_s,
                    refresh=refresh,
                )
                extra_warnings.append("No captions were available; used local Whisper fallback.")

            fingerprint = self._fingerprint(acquired)
            video_id = self._video_id(acquired)
            existing = self.store.find_by_fingerprint(fingerprint)
            if (
                existing is not None
                and not refresh
                and self._cache_satisfies(existing, mode, transcript_mode)
            ):
                if acquired.metadata.kind is SourceKind.LOCAL:
                    existing = existing.model_copy(
                        update={
                            "source": acquired.metadata.source,
                            "local_source_path": acquired.metadata.source,
                            "local_source_size": acquired.metadata.file_size_bytes,
                            "local_source_mtime_ns": acquired.metadata.content_mtime_ns,
                        }
                    )
                    self.store.save_video(
                        existing,
                        self.store.segments_for_video(existing.video_id),
                        self.store.moments_for_video(existing.video_id),
                    )
                return self._ingest_result(existing, cache_hit=True)

            transcript_source: Sequence[AcquiredTranscriptSegment] = acquired.transcript
            if not transcript_source and transcript_mode in {
                TranscriptMode.AUTO,
                TranscriptMode.WHISPER,
            }:
                if not self._has_whisper():
                    if transcript_mode is TranscriptMode.WHISPER:
                        raise ConfigurationError(
                            "Whisper transcription was requested but the optional dependency is "
                            "missing. Install video-context-mcp[whisper] and retry."
                        )
                    extra_warnings.append(
                        "No captions were available and optional Whisper is not installed."
                    )
                elif acquired.media_path is None:
                    raise ExtractionError(
                        "Whisper fallback requires local media, but acquisition produced none."
                    )
                else:
                    self._notify(progress, 20, "Transcribing speech locally with Whisper")
                    transcript_source = self._transcribe(
                        acquired.media_path,
                        progress=lambda value, message: self._notify(
                            progress, 20 + value * 0.1, message
                        ),
                    )
                    if transcript_source and not extra_warnings:
                        extra_warnings.append("Used local Whisper speech transcription.")

            segments = self._segments(video_id, transcript_source)
            prior = self.store.get_video(video_id)
            prior_moments = self.store.moments_for_video(video_id) if prior is not None else []

            published_run: Path | None = None
            if mode is IngestMode.FULL:
                if acquired.media_path is None:
                    raise ExtractionError(
                        "Full ingestion requires a local media file; retry the acquisition."
                    )
                self._notify(progress, 30, "Extracting stable visual moments")
                try:
                    moments, published_run = self._extract_and_publish(
                        video_id,
                        acquired.media_path,
                        progress=progress,
                    )
                except OSError as exc:
                    raise ExtractionError(
                        "Could not stage visual artifacts. Check free disk space and permissions "
                        f"under KEYFRAME_HOME ({self.settings.home}), then retry."
                    ) from exc
                indexed_mode = IngestMode.FULL
                if not moments:
                    extra_warnings.append(
                        "No stable visual moments met the retention threshold; transcript search "
                        "remains available."
                    )
            elif prior is not None and prior.indexed_mode is IngestMode.FULL:
                moments = prior_moments
                indexed_mode = IngestMode.FULL
            else:
                moments = []
                indexed_mode = IngestMode.FAST

            try:
                self._assert_local_source_unchanged(acquired)
                if acquired.owns_media:
                    acquired.cleanup()
                warnings = _unique_strings((*acquired.warnings, *extra_warnings))
                video = VideoRecord(
                    video_id=video_id,
                    source=acquired.metadata.source,
                    source_kind=acquired.metadata.kind.value,
                    availability=acquired.metadata.availability,
                    source_fingerprint=fingerprint,
                    title=acquired.metadata.title,
                    duration_s=acquired.metadata.duration_s,
                    chapters=tuple(
                        Chapter(start_s=item.start_s, end_s=item.end_s, title=item.title)
                        for item in acquired.chapters
                    ),
                    has_transcript=bool(segments),
                    transcript_mode=transcript_mode,
                    indexed_mode=indexed_mode,
                    keyframe_count=len(moments),
                    status="ready",
                    warnings=warnings,
                    local_source_path=(
                        acquired.metadata.source
                        if acquired.metadata.kind is SourceKind.LOCAL
                        else None
                    ),
                    local_source_size=(
                        acquired.metadata.file_size_bytes
                        if acquired.metadata.kind is SourceKind.LOCAL
                        else None
                    ),
                    local_source_mtime_ns=(
                        acquired.metadata.content_mtime_ns
                        if acquired.metadata.kind is SourceKind.LOCAL
                        else None
                    ),
                    pipeline_version=PIPELINE_VERSION,
                )
                self._notify(progress, 92, "Committing the local index")
                self.store.save_video(video, segments, moments)
            except BaseException:
                if published_run is not None:
                    shutil.rmtree(published_run, ignore_errors=True)
                raise
            if mode is IngestMode.FULL:
                self._remove_superseded_runs(prior_moments, keep=published_run)
            self._notify(progress, 100, "Video index ready")
            return self._ingest_result(video, cache_hit=False)
        finally:
            active_failure = sys.exception()
            try:
                acquired.cleanup()
            except SourceError:
                if active_failure is None:
                    raise
                logger.exception(
                    "Temporary media cleanup also failed while handling %s",
                    type(active_failure).__name__,
                )

    def get_transcript(
        self,
        video_id: str,
        *,
        start_s: float | None = None,
        end_s: float | None = None,
        cursor: str | None = None,
        limit: int = 40,
    ) -> TranscriptPage:
        self._require_video(video_id)
        _validate_limit(limit, MAX_TRANSCRIPT_LIMIT, "transcript")
        if start_s is not None:
            _validate_timestamp(start_s, "Transcript start_s")
        if end_s is not None:
            _validate_timestamp(end_s, "Transcript end_s")
        if start_s is not None and end_s is not None and start_s > end_s:
            raise SourceError("Transcript start_s must not be greater than end_s.")
        scope = cursor_scope(
            "transcript",
            {"video_id": video_id, "start_s": start_s, "end_s": end_s},
        )
        offset = decode_cursor(cursor, kind="transcript", scope=scope)
        segments, has_more = self.store.transcript_page(
            video_id,
            start_s=start_s,
            end_s=end_s,
            offset=offset,
            limit=limit,
        )
        next_cursor = (
            encode_cursor(
                kind="transcript",
                offset=offset + len(segments),
                scope=scope,
            )
            if has_more
            else None
        )
        return TranscriptPage(
            video_id=video_id,
            segments=tuple(segments),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def search(
        self,
        query: str,
        *,
        video_id: str | None = None,
        channel: SearchChannel = SearchChannel.ALL,
        cursor: str | None = None,
        limit: int = 10,
    ) -> SearchPage:
        if video_id is not None:
            self._require_video(video_id)
        try:
            selected_channel = SearchChannel(channel)
        except ValueError as exc:
            raise SourceError(f"Unsupported search channel: {channel!r}.") from exc
        _validate_limit(limit, MAX_SEARCH_LIMIT, "search")
        normalized_query = query.strip()
        if not normalized_query:
            raise SourceError("Search query must not be empty.")
        scope = cursor_scope(
            "search",
            {
                "query": normalized_query,
                "video_id": video_id,
                "channel": selected_channel.value,
            },
        )
        offset = decode_cursor(cursor, kind="search", scope=scope)
        hits, has_more = self.store.search(
            normalized_query,
            video_id=video_id,
            channel=selected_channel,
            offset=offset,
            limit=limit,
        )
        next_cursor = (
            encode_cursor(kind="search", offset=offset + len(hits), scope=scope)
            if has_more
            else None
        )
        return SearchPage(
            query=normalized_query,
            hits=tuple(hits),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def list_moments(
        self,
        video_id: str,
        *,
        kind: MomentKind = MomentKind.ANY,
        cursor: str | None = None,
        limit: int = 20,
    ) -> MomentPage:
        self._require_video(video_id)
        try:
            selected_kind = MomentKind(kind)
        except ValueError as exc:
            raise SourceError(f"Unsupported moment kind: {kind!r}.") from exc
        _validate_limit(limit, MAX_MOMENT_LIMIT, "moment")
        scope = cursor_scope("moments", {"video_id": video_id, "kind": selected_kind.value})
        offset = decode_cursor(cursor, kind="moments", scope=scope)
        moments, has_more = self.store.moment_page(
            video_id,
            kind=selected_kind,
            offset=offset,
            limit=limit,
        )
        summaries = tuple(
            MomentSummary(
                moment_id=moment.moment_id,
                start_s=moment.start_s,
                end_s=moment.end_s,
                kind=moment.kind,
                classification_confidence=moment.classification_confidence,
                stable_seconds=moment.stable_seconds,
                ocr_preview=_preview(moment.ocr_text),
                ocr_confidence=moment.ocr_confidence,
                language_guess=moment.language_guess,
                parses=moment.parses,
            )
            for moment in moments
        )
        next_cursor = (
            encode_cursor(
                kind="moments",
                offset=offset + len(summaries),
                scope=scope,
            )
            if has_more
            else None
        )
        return MomentPage(
            video_id=video_id,
            moments=summaries,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def get_code(
        self,
        video_id: str,
        *,
        moment_id: str | None = None,
        t: float | None = None,
    ) -> VisualPayload[CodeResult]:
        self._require_video(video_id)
        if (moment_id is None) == (t is None):
            raise SourceError("Provide exactly one of moment_id or t.")
        if t is not None:
            _validate_timestamp(t, "Code timestamp")
        if moment_id is not None:
            moment = self.store.get_moment(moment_id)
            if moment is None or moment.video_id != video_id:
                raise CacheError(
                    f"Visual moment {moment_id!r} was not found in video {video_id!r}."
                )
        else:
            assert t is not None
            moment = self.store.nearest_moment(
                video_id,
                t,
                code_only=True,
                tolerance_s=5.0,
            )
            if moment is None:
                raise CacheError(
                    "No code or terminal moment was retained within 5 seconds. "
                    "Use video_list_moments or verify that the video was ingested in full mode."
                )
        if moment.kind not in {MomentKind.CODE, MomentKind.TERMINAL}:
            raise CacheError(
                f"Moment {moment.moment_id!r} is classified as {moment.kind.value}, not code or terminal."
            )
        image_path = moment.crop_path or moment.frame_path
        image_data, mime_type = self._read_artifact(image_path)
        return VisualPayload(
            result=CodeResult(
                video_id=video_id,
                moment_id=moment.moment_id,
                requested_t=t,
                actual_t=moment.actual_t,
                language_guess=moment.language_guess,
                code=moment.code,
                parses=moment.parses,
                confidence=moment.ocr_confidence,
                classification_confidence=moment.classification_confidence,
                kind=moment.kind,
                notes=moment.notes,
            ),
            image_data=image_data,
            mime_type=mime_type,
        )

    def get_frame(
        self,
        video_id: str,
        *,
        t: float,
        region: FrameRegion = FrameRegion.FULL,
    ) -> VisualPayload[FrameResult]:
        self._require_video(video_id)
        _validate_timestamp(t, "Frame timestamp")
        try:
            selected_region = FrameRegion(region)
        except ValueError as exc:
            raise SourceError(f"Unsupported frame region: {region!r}.") from exc
        moment = self.store.nearest_moment(
            video_id,
            t,
            code_only=False,
            tolerance_s=None,
        )
        if moment is None:
            raise CacheError(
                "No retained frames are available. Re-ingest this video with mode='full'."
            )
        image_path = (
            moment.crop_path
            if selected_region is FrameRegion.AUTO_CROP and moment.crop_path is not None
            else moment.frame_path
        )
        image_data, mime_type = self._read_artifact(image_path)
        return VisualPayload(
            result=FrameResult(
                video_id=video_id,
                moment_id=moment.moment_id,
                requested_t=t,
                actual_t=moment.actual_t,
                kind=moment.kind,
                region=selected_region,
            ),
            image_data=image_data,
            mime_type=mime_type,
        )

    def _extract_and_publish(
        self,
        video_id: str,
        media_path: Path,
        *,
        progress: ProgressCallback | None,
    ) -> tuple[list[VisualMoment], Path | None]:
        with tempfile.TemporaryDirectory(prefix="ingest-", dir=self.settings.tmp_dir) as raw_temp:
            work_dir = Path(raw_temp)

            def visual_progress(message: str, fraction: float) -> None:
                self._notify(progress, 30 + max(0.0, min(1.0, fraction)) * 55, message)

            extracted = self._extract_visuals(
                media_path,
                work_dir / "analysis",
                ffmpeg_binary=self.settings.ffmpeg_executable,
                ffprobe_binary=self.settings.ffprobe_executable,
                node_binary=self.settings.node_executable,
                tesseract_binary=self.settings.tesseract_executable,
                progress=visual_progress,
            )
            if not extracted:
                return [], None

            run_name = f"p{PIPELINE_VERSION}-{uuid.uuid4().hex[:12]}"
            staged_run = work_dir / "publish"
            staged_run.mkdir(parents=True)
            final_run = self.settings.artifacts_dir / video_id / run_name
            final_run.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            moments: list[VisualMoment] = []
            for index, item in enumerate(extracted):
                frame_name = f"frame-{index:05d}.jpg"
                crop_name = f"crop-{index:05d}.jpg"
                encoded_frame = encode_image(item.frame_path)
                (staged_run / frame_name).write_bytes(encoded_frame.data)
                cropped, _ = auto_crop_text_region(item.frame_path, item.ocr)
                encoded_crop = encode_image(cropped)
                (staged_run / crop_name).write_bytes(encoded_crop.data)
                notes = [f"Visual classification confidence: {item.kind_confidence:.2f}."]
                if item.ocr.confidence < 0.70:
                    notes.append(
                        "OCR confidence is below 0.70; treat the source frame as authoritative."
                    )
                if item.language is not None and item.parses is False:
                    notes.append(
                        "Reconstructed code did not parse; verify it against the source frame."
                    )
                moment_id = f"{video_id}:m:{index}"
                moments.append(
                    VisualMoment(
                        moment_id=moment_id,
                        video_id=video_id,
                        actual_t=item.timestamp_s,
                        start_s=item.start_s,
                        end_s=item.end_s,
                        kind=MomentKind(item.kind),
                        classification_confidence=item.kind_confidence,
                        stable_seconds=item.stable_seconds,
                        ocr_text=item.ocr.text,
                        ocr_confidence=item.ocr.confidence,
                        language_guess=item.language,
                        code=item.ocr.text,
                        parses=item.parses,
                        notes=tuple(notes),
                        frame_path=str((final_run / frame_name).relative_to(self.settings.home)),
                        crop_path=str((final_run / crop_name).relative_to(self.settings.home)),
                    )
                )
            os.replace(staged_run, final_run)
            return moments, final_run

    def _read_artifact(self, relative_path: str) -> tuple[bytes, str]:
        candidate = (self.settings.home / relative_path).resolve(strict=False)
        artifacts_root = self.settings.artifacts_dir.resolve(strict=False)
        if not candidate.is_relative_to(artifacts_root):
            raise CacheError("Cached image path escaped the Keyframe artifact directory.")
        if not candidate.is_file():
            raise CacheError(
                f"Cached source image is missing: {relative_path}. Re-ingest the video in full mode."
            )
        try:
            size = candidate.stat().st_size
            if size > MAX_IMAGE_BYTES:
                raise CacheError(
                    f"Cached image is {size} bytes, above the {MAX_IMAGE_BYTES}-byte MCP limit."
                )
            with Image.open(candidate) as image:
                image.verify()
            with Image.open(candidate) as image:
                if max(image.size) > MAX_IMAGE_EDGE:
                    raise CacheError(
                        f"Cached image edge exceeds the {MAX_IMAGE_EDGE}-pixel MCP limit."
                    )
                if image.format != "JPEG":
                    raise CacheError("Cached image is not a validated JPEG artifact.")
            return candidate.read_bytes(), "image/jpeg"
        except CacheError:
            raise
        except (OSError, UnidentifiedImageError) as exc:
            raise CacheError(f"Cached image could not be validated: {relative_path}.") from exc

    def _normalize_source(self, source: str, settings: Settings) -> str:
        kind = classify_source(source)
        if kind is SourceKind.LOCAL:
            return str(validate_local_path(source, settings))
        return source.strip()

    def _settings_with_client_roots(self, roots: Sequence[Path]) -> Settings:
        authorized = list(self.settings.allowed_roots)
        for value in roots:
            try:
                root = Path(value).expanduser().resolve(strict=True)
            except OSError as exc:
                raise SourceError(f"Advertised MCP root does not exist: {value}") from exc
            if not (root.is_dir() or root.is_file()):
                raise SourceError(f"Advertised MCP root is not a directory or regular file: {root}")
            if root not in authorized:
                authorized.append(root)
        return replace(self.settings, allowed_roots=tuple(authorized))

    @staticmethod
    def _video_id(acquired: AcquiredSource) -> str:
        raw = acquired.metadata.video_id
        if acquired.metadata.kind is SourceKind.LOCAL:
            return raw
        provider = _SAFE_ID_RE.sub("-", acquired.metadata.provider.lower()).strip("-_") or "remote"
        identity = _SAFE_ID_RE.sub("-", raw).strip("-_")[:96]
        if not identity:
            identity = "video"
        digest = hashlib.sha256(f"{acquired.metadata.provider}\0{raw}".encode()).hexdigest()[:10]
        return f"{provider}-{identity}-{digest}"

    @staticmethod
    def _fingerprint(acquired: AcquiredSource) -> str:
        metadata = acquired.metadata
        if metadata.kind is SourceKind.LOCAL:
            digest = metadata.content_sha256
            if digest is None:
                raise ExtractionError("Local acquisition did not provide its SHA-256 identity.")
            return f"sha256:{digest}:pipeline:{PIPELINE_VERSION}"
        return f"{metadata.provider}:{metadata.video_id}:pipeline:{PIPELINE_VERSION}"

    @staticmethod
    def _assert_local_source_unchanged(acquired: AcquiredSource) -> None:
        """Revalidate a caller-owned file immediately before publishing its index."""

        metadata = acquired.metadata
        if metadata.kind is not SourceKind.LOCAL:
            return
        if (
            metadata.file_size_bytes is None
            or metadata.content_mtime_ns is None
            or metadata.content_sha256 is None
        ):
            raise ExtractionError("Local acquisition did not provide a complete source identity.")
        path = Path(metadata.source)
        try:
            before = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = path.stat()
        except OSError as exc:
            raise SourceError(
                "Local video became unavailable during ingestion; nothing was published."
            ) from exc
        if (
            before.st_size != metadata.file_size_bytes
            or before.st_mtime_ns != metadata.content_mtime_ns
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or digest.hexdigest() != metadata.content_sha256
        ):
            raise SourceError(
                "Local video changed during ingestion; nothing was published. Retry the ingest."
            )

    @staticmethod
    def _segments(
        video_id: str, values: Sequence[AcquiredTranscriptSegment]
    ) -> list[TranscriptSegment]:
        segments: list[TranscriptSegment] = []
        for index, value in enumerate(values):
            start_s = float(value.start_s)
            end_s = float(value.end_s)
            text = value.text.strip()
            if not text:
                continue
            segments.append(
                TranscriptSegment(
                    segment_id=f"{video_id}:s:{index}",
                    start_s=max(0.0, start_s),
                    end_s=max(start_s, end_s),
                    text=text,
                    source=value.origin,
                )
            )
        return segments

    def _cache_satisfies(
        self,
        video: VideoRecord,
        requested_mode: IngestMode,
        requested_transcript: TranscriptMode,
    ) -> bool:
        mode_ready = requested_mode is IngestMode.FAST or video.indexed_mode is IngestMode.FULL
        if not mode_ready:
            return False
        if requested_transcript is TranscriptMode.NONE:
            return True
        if video.transcript_mode is requested_transcript:
            return not (
                requested_transcript is TranscriptMode.AUTO
                and not video.has_transcript
                and self._has_whisper()
            )
        return (
            requested_transcript is TranscriptMode.AUTO
            and video.has_transcript
            and video.transcript_mode in {TranscriptMode.CAPTIONS, TranscriptMode.WHISPER}
        )

    @staticmethod
    def _cache_source_is_current(video: VideoRecord) -> bool:
        """Use a cheap stat guard before reusing a content-hashed local cache."""

        if video.source_kind != SourceKind.LOCAL.value:
            return True
        if (
            video.local_source_path is None
            or video.local_source_size is None
            or video.local_source_mtime_ns is None
        ):
            return False
        try:
            current = Path(video.local_source_path).stat()
        except OSError:
            return False
        return (
            current.st_size == video.local_source_size
            and current.st_mtime_ns == video.local_source_mtime_ns
        )

    @staticmethod
    def _ingest_result(video: VideoRecord, *, cache_hit: bool) -> IngestResult:
        return IngestResult(
            video_id=video.video_id,
            title=video.title,
            duration_s=video.duration_s,
            source_type=video.source_kind,
            availability=video.availability,
            chapters=video.chapters,
            has_transcript=video.has_transcript,
            transcript_mode=video.transcript_mode,
            keyframe_count=video.keyframe_count,
            indexed_mode=video.indexed_mode,
            status=video.status,
            warnings=video.warnings,
            cache_hit=cache_hit,
            pipeline_version=video.pipeline_version,
        )

    def _require_video(self, video_id: str) -> VideoRecord:
        video = self.store.get_video(video_id)
        if video is None or video.status != "ready":
            raise CacheError(
                f"Video {video_id!r} is not indexed. Call video_ingest first and use its video_id."
            )
        return video

    def _recover_interrupted_work(self) -> None:
        try:
            with self._global_ingest_lock.acquire(timeout=0):
                for child in self.settings.tmp_dir.iterdir():
                    if child.name.startswith(("ingest-", "acquire-")):
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)

                referenced_runs = {
                    run
                    for value in self.store.artifact_paths()
                    if (run := self._artifact_run(self.settings.home / value)) is not None
                }
                for video_dir in self.settings.artifacts_dir.iterdir():
                    if not video_dir.is_dir():
                        continue
                    for run_dir in video_dir.iterdir():
                        if run_dir.is_dir() and run_dir not in referenced_runs:
                            shutil.rmtree(run_dir, ignore_errors=True)
                    with suppress(OSError):
                        video_dir.rmdir()
        except Timeout:
            # Another process is actively ingesting into this KEYFRAME_HOME.
            # Its own completion or a later process startup will perform cleanup.
            return

    def _remove_superseded_runs(
        self,
        moments: Sequence[VisualMoment],
        *,
        keep: Path | None,
    ) -> None:
        runs = {
            run
            for moment in moments
            if (run := self._artifact_run(self.settings.home / moment.frame_path)) is not None
        }
        for run in runs:
            if keep is None or run != keep:
                shutil.rmtree(run, ignore_errors=True)

    def _artifact_run(self, path: Path) -> Path | None:
        resolved = path.resolve(strict=False)
        root = self.settings.artifacts_dir.resolve(strict=False)
        if not resolved.is_relative_to(root):
            return None
        relative = resolved.relative_to(root)
        if len(relative.parts) < 3:
            return None
        return root / relative.parts[0] / relative.parts[1]

    @staticmethod
    def _notify(progress: ProgressCallback | None, value: float, message: str) -> None:
        if progress is not None:
            progress(max(0.0, min(100.0, value)), message)


def _validate_limit(value: int, maximum: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > maximum:
        raise SourceError(f"{label.capitalize()} page limit must be between 1 and {maximum}.")


def _validate_timestamp(value: float, label: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise SourceError(f"{label} must be a finite non-negative number.")
    if value < 0:
        raise SourceError(f"{label} must be a finite non-negative number.")


def _preview(text: str, *, length: int = 240) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= length else f"{compact[: length - 1]}…"


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value.strip()))
