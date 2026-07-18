from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from filelock import FileLock
from PIL import Image

from video_context_mcp.acquisition import (
    AcquiredSource,
    SourceKind,
    SourceMetadata,
)
from video_context_mcp.acquisition import (
    TranscriptSegment as AcquiredTranscriptSegment,
)
from video_context_mcp.config import Settings
from video_context_mcp.constants import MAX_IMAGE_BYTES, MAX_IMAGE_EDGE, PIPELINE_VERSION
from video_context_mcp.cursors import CursorError
from video_context_mcp.errors import CacheError, SourceError
from video_context_mcp.models import (
    IngestMode,
    MomentKind,
    TranscriptMode,
    VideoRecord,
    VisualMoment,
)
from video_context_mcp.service import KeyframeService
from video_context_mcp.storage import KeyframeStore
from video_context_mcp.vision import BoundingBox, OCRLine, OCRResult
from video_context_mcp.vision import VisualMoment as ExtractedVisualMoment


def _settings(tmp_path: Path) -> tuple[Settings, Path]:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    return (
        Settings(home=tmp_path / "home", allowed_roots=(source_root.resolve(),)),
        source_root,
    )


def _source_file(source_root: Path, contents: bytes = b"first video revision") -> Path:
    source = source_root / "demo.mp4"
    source.write_bytes(contents)
    return source.resolve()


def _fake_acquirer(
    *,
    transcript_texts: tuple[str, ...] = ("alpha evidence", "beta evidence", "gamma evidence"),
) -> tuple[Callable[..., AcquiredSource], list[str]]:
    calls: list[str] = []

    def acquire(source: str, settings: Settings, **_kwargs: object) -> AcquiredSource:
        del settings
        calls.append(source)
        path = Path(source)
        contents = path.read_bytes()
        stat = path.stat()
        digest = hashlib.sha256(contents).hexdigest()
        transcript = tuple(
            AcquiredTranscriptSegment(
                start_s=float(index),
                end_s=float(index + 1),
                text=text,
            )
            for index, text in enumerate(transcript_texts)
        )
        return AcquiredSource(
            metadata=SourceMetadata(
                source=str(path),
                kind=SourceKind.LOCAL,
                video_id=f"local-{digest[:16]}",
                title="Fake local video",
                duration_s=10.0,
                provider="local",
                file_size_bytes=len(contents),
                content_sha256=digest,
                content_mtime_ns=stat.st_mtime_ns,
            ),
            transcript=transcript,
            media_path=path,
        )

    return acquire, calls


def _fake_visual_extractor(
    *, kinds: tuple[str, ...] = ("code", "slide")
) -> Callable[..., list[ExtractedVisualMoment]]:
    def extract(
        _media_path: Path,
        work_dir: Path,
        **_kwargs: object,
    ) -> list[ExtractedVisualMoment]:
        work_dir.mkdir(parents=True, exist_ok=True)
        moments: list[ExtractedVisualMoment] = []
        for index, kind in enumerate(kinds):
            frame = work_dir / f"source-{index}.jpg"
            Image.new("RGB", (320, 180), (32 + index, 48, 64)).save(frame, format="JPEG")
            line = OCRLine(
                text=f"def example_{index}(): return {index}",
                indent_spaces=0,
                confidence=0.91,
                box=BoundingBox(left=20, top=20, width=220, height=30),
            )
            ocr = OCRResult(
                text=line.text,
                lines=(line,),
                confidence=0.91,
                source_width=320,
                source_height=180,
            )
            moments.append(
                ExtractedVisualMoment(
                    timestamp_s=float(index * 5 + 2),
                    start_s=float(index * 5),
                    end_s=float(index * 5 + 4),
                    stable_seconds=4.0,
                    frame_path=frame,
                    kind=kind,  # type: ignore[arg-type]
                    kind_confidence=0.88,
                    ocr=ocr,
                    language="python" if kind == "code" else None,
                    parses=True if kind == "code" else None,
                    crop_box=BoundingBox(left=20, top=20, width=220, height=30),
                )
            )
        return moments

    return extract


def _service(
    settings: Settings,
    acquire: Callable[..., AcquiredSource],
    *,
    store: KeyframeStore | None = None,
    kinds: tuple[str, ...] = ("code", "slide"),
) -> KeyframeService:
    return KeyframeService(
        settings=settings,
        store=store,
        acquire=acquire,
        extract_visuals=_fake_visual_extractor(kinds=kinds),
        has_whisper=lambda: False,
    )


def test_local_cache_identity_reuses_unchanged_source_and_hashes_changed_file(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, calls = _fake_acquirer()
    service = _service(settings, acquire)

    first = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )
    cached = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )

    assert cached.video_id == first.video_id
    assert cached.cache_hit is True
    assert len(calls) == 1

    source.write_bytes(b"second, materially different video revision")
    changed = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )

    assert changed.video_id != first.video_id
    assert changed.cache_hit is False
    assert len(calls) == 2


def test_request_local_mcp_roots_authorize_without_mutating_service_settings(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "client-root"
    source_root.mkdir()
    source = _source_file(source_root)
    settings = Settings(home=tmp_path / "home", allowed_roots=())
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)

    with pytest.raises(SourceError, match="No local-video roots"):
        service.ingest(str(source), transcript_mode=TranscriptMode.CAPTIONS)

    result = service.ingest(
        str(source),
        transcript_mode=TranscriptMode.CAPTIONS,
        client_roots=(source_root,),
    )

    assert result.source_type == "local"
    assert service.settings.allowed_roots == ()


def test_revalidated_same_content_updates_the_cheap_local_cache_guard(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, calls = _fake_acquirer()
    service = _service(settings, acquire)
    first = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )

    original = source.read_bytes()
    source.write_bytes(original)
    revalidated = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )
    cached = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )

    assert revalidated.video_id == first.video_id
    assert revalidated.cache_hit is True
    assert cached.video_id == first.video_id
    assert cached.cache_hit is True
    assert len(calls) == 2


def test_fast_refresh_preserves_a_previously_published_full_visual_index(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)

    full = service.ingest(
        str(source), mode=IngestMode.FULL, transcript_mode=TranscriptMode.CAPTIONS
    )
    prior = service.store.moments_for_video(full.video_id)
    prior_paths = {moment.frame_path for moment in prior}

    refreshed = service.ingest(
        str(source),
        mode=IngestMode.FAST,
        transcript_mode=TranscriptMode.CAPTIONS,
        refresh=True,
    )

    assert refreshed.cache_hit is False
    assert refreshed.indexed_mode is IngestMode.FULL
    assert refreshed.keyframe_count == len(prior)
    assert {
        moment.frame_path for moment in service.store.moments_for_video(full.video_id)
    } == prior_paths
    assert all((settings.home / path).is_file() for path in prior_paths)


def test_cache_identity_includes_requested_transcript_mode(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    base_acquire, calls = _fake_acquirer()

    def acquire(source_value: str, settings_value: Settings, **kwargs: object) -> AcquiredSource:
        acquired = base_acquire(source_value, settings_value, **kwargs)
        if kwargs.get("transcript_mode") == "none":
            acquired.transcript = ()
        return acquired

    service = _service(settings, acquire)
    without_transcript = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.NONE
    )
    with_captions = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )
    cached_captions = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )

    assert without_transcript.has_transcript is False
    assert with_captions.has_transcript is True
    assert with_captions.transcript_mode is TranscriptMode.CAPTIONS
    assert cached_captions.cache_hit is True
    assert len(calls) == 2


def test_page_cursors_are_bound_to_the_exact_query_scope(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    ingested = service.ingest(
        str(source), mode=IngestMode.FULL, transcript_mode=TranscriptMode.CAPTIONS
    )

    transcript = service.get_transcript(ingested.video_id, limit=1)
    assert transcript.next_cursor is not None
    with pytest.raises(CursorError, match="does not belong"):
        service.get_transcript(
            ingested.video_id,
            start_s=0.5,
            cursor=transcript.next_cursor,
            limit=1,
        )

    moments = service.list_moments(ingested.video_id, kind=MomentKind.ANY, limit=1)
    assert moments.next_cursor is not None
    with pytest.raises(CursorError, match="does not belong"):
        service.list_moments(
            ingested.video_id,
            kind=MomentKind.CODE,
            cursor=moments.next_cursor,
            limit=1,
        )


def test_get_code_requires_exactly_one_selector_and_rejects_non_code_moments(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    ingested = service.ingest(
        str(source), mode=IngestMode.FULL, transcript_mode=TranscriptMode.CAPTIONS
    )
    moments = service.store.moments_for_video(ingested.video_id)

    with pytest.raises(SourceError, match="exactly one"):
        service.get_code(ingested.video_id)
    with pytest.raises(SourceError, match="exactly one"):
        service.get_code(ingested.video_id, moment_id=moments[0].moment_id, t=2.0)
    with pytest.raises(CacheError, match="not code or terminal"):
        service.get_code(ingested.video_id, moment_id=moments[1].moment_id)

    selected = service.get_code(ingested.video_id, moment_id=moments[0].moment_id)
    assert selected.result.requested_t is None
    assert selected.result.moment_id == moments[0].moment_id
    assert selected.image_data


def test_code_timestamp_selects_a_moment_containing_the_requested_time(tmp_path: Path) -> None:
    settings, _source_root = _settings(tmp_path)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    run_dir = settings.artifacts_dir / "video" / "manual"
    run_dir.mkdir(parents=True)
    frame = run_dir / "frame.jpg"
    Image.new("RGB", (100, 50), "black").save(frame, format="JPEG")
    relative = str(frame.relative_to(settings.home))
    _seed_artifact_moment(service, relative_path=relative, end_s=20)

    selected = service.get_code("video", t=15)

    assert selected.result.moment_id == "video:m:0"
    assert selected.result.requested_t == 15


def test_direct_service_calls_reject_non_finite_timestamps(tmp_path: Path) -> None:
    settings, _source_root = _settings(tmp_path)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    run_dir = settings.artifacts_dir / "video" / "manual"
    run_dir.mkdir(parents=True)
    frame = run_dir / "frame.jpg"
    Image.new("RGB", (100, 50), "black").save(frame, format="JPEG")
    _seed_artifact_moment(service, relative_path=str(frame.relative_to(settings.home)))

    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(SourceError, match="finite non-negative"):
            service.get_frame("video", t=value)
        with pytest.raises(SourceError, match="finite non-negative"):
            service.get_transcript("video", start_s=value)


def _seed_artifact_moment(
    service: KeyframeService,
    *,
    relative_path: str,
    moment_id: str = "video:m:0",
    end_s: float = 2,
) -> None:
    video = VideoRecord(
        video_id="video",
        source="fixture",
        source_kind="local",
        source_fingerprint=f"sha256:fixture:pipeline:{PIPELINE_VERSION}",
        title="Fixture",
        duration_s=end_s,
        has_transcript=False,
        indexed_mode=IngestMode.FULL,
        keyframe_count=1,
        pipeline_version=PIPELINE_VERSION,
    )
    moment = VisualMoment(
        moment_id=moment_id,
        video_id=video.video_id,
        actual_t=1,
        start_s=0,
        end_s=end_s,
        kind=MomentKind.CODE,
        stable_seconds=end_s,
        ocr_text="print('verified')",
        ocr_confidence=0.9,
        language_guess="python",
        code="print('verified')",
        parses=True,
        frame_path=relative_path,
    )
    service.store.save_video(video, (), (moment,))


def test_visual_reads_reject_paths_outside_the_artifact_directory(tmp_path: Path) -> None:
    settings, _source_root = _settings(tmp_path)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    escaped = tmp_path / "escaped.jpg"
    Image.new("RGB", (10, 10), "red").save(escaped, format="JPEG")
    _seed_artifact_moment(service, relative_path="../escaped.jpg")

    with pytest.raises(CacheError, match="escaped"):
        service.get_frame("video", t=1)


def test_visual_reads_enforce_encoded_size_and_dimensions(tmp_path: Path) -> None:
    settings, _source_root = _settings(tmp_path)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    run_dir = settings.artifacts_dir / "video" / "manual"
    run_dir.mkdir(parents=True)
    relative = str((run_dir / "frame.jpg").relative_to(settings.home))

    (settings.home / relative).write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))
    _seed_artifact_moment(service, relative_path=relative)
    with pytest.raises(CacheError, match="above"):
        service.get_frame("video", t=1)

    Image.new("RGB", (MAX_IMAGE_EDGE + 1, 1), "blue").save(settings.home / relative, format="JPEG")
    with pytest.raises(CacheError, match="edge exceeds"):
        service.get_frame("video", t=1)


class _FailingStore(KeyframeStore):
    fail_saves = False

    def save_video(
        self,
        video: VideoRecord,
        segments: tuple[object, ...] | list[object],
        moments: tuple[object, ...] | list[object],
    ) -> None:
        if self.fail_saves:
            raise RuntimeError("simulated atomic commit failure")
        super().save_video(video, segments, moments)  # type: ignore[arg-type]


def test_failed_refresh_removes_new_run_but_preserves_committed_artifacts(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    store = _FailingStore(settings.cache_dir / "keyframe.sqlite3")
    service = _service(settings, acquire, store=store)
    first = service.ingest(
        str(source), mode=IngestMode.FULL, transcript_mode=TranscriptMode.CAPTIONS
    )
    committed = service.store.moments_for_video(first.video_id)
    committed_paths = {moment.frame_path for moment in committed}
    committed_runs = {Path(path).parts[2] for path in committed_paths}

    store.fail_saves = True
    with pytest.raises(RuntimeError, match="simulated atomic commit failure"):
        service.ingest(
            str(source),
            mode=IngestMode.FULL,
            transcript_mode=TranscriptMode.CAPTIONS,
            refresh=True,
        )

    assert {
        moment.frame_path for moment in store.moments_for_video(first.video_id)
    } == committed_paths
    assert all((settings.home / path).is_file() for path in committed_paths)
    published_runs = {path.name for path in (settings.artifacts_dir / first.video_id).iterdir()}
    assert published_runs == committed_runs
    assert not any(settings.tmp_dir.iterdir())


def test_source_change_during_extraction_publishes_neither_index_nor_artifacts(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    base_extractor = _fake_visual_extractor()

    def mutating_extractor(
        media_path: Path,
        work_dir: Path,
        **kwargs: object,
    ) -> list[ExtractedVisualMoment]:
        moments = base_extractor(media_path, work_dir, **kwargs)
        media_path.write_bytes(b"source changed while visual extraction was running")
        return moments

    service = KeyframeService(
        settings=settings,
        acquire=acquire,
        extract_visuals=mutating_extractor,
        has_whisper=lambda: False,
    )

    with pytest.raises(SourceError, match="changed during ingestion"):
        service.ingest(
            str(source),
            mode=IngestMode.FULL,
            transcript_mode=TranscriptMode.CAPTIONS,
        )

    assert service.store.find_by_source(str(source), pipeline_version=PIPELINE_VERSION) is None
    assert not any(path.is_file() for path in settings.artifacts_dir.rglob("*"))
    assert not any(settings.tmp_dir.iterdir())


def test_startup_recovers_interrupted_work_and_only_unreferenced_artifact_runs(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    first_service = _service(settings, acquire)
    ingested = first_service.ingest(
        str(source), mode=IngestMode.FULL, transcript_mode=TranscriptMode.CAPTIONS
    )
    referenced = first_service.store.moments_for_video(ingested.video_id)[0]
    referenced_run = (settings.home / referenced.frame_path).parent

    interrupted = settings.tmp_dir / "ingest-crashed"
    interrupted.mkdir()
    (interrupted / "partial.jpg").write_bytes(b"partial")
    acquire_partial = settings.tmp_dir / "acquire-crashed.tmp"
    acquire_partial.write_bytes(b"partial")
    unrelated_tmp = settings.tmp_dir / "keep-user-named-file"
    unrelated_tmp.write_bytes(b"keep")
    orphan_run = settings.artifacts_dir / ingested.video_id / "orphan-run"
    orphan_run.mkdir()
    (orphan_run / "frame.jpg").write_bytes(b"partial")

    _service(settings, acquire)

    assert not interrupted.exists()
    assert not acquire_partial.exists()
    assert not orphan_run.exists()
    assert unrelated_tmp.read_bytes() == b"keep"
    assert referenced_run.is_dir()


def test_startup_recovery_does_not_delete_another_process_active_run(tmp_path: Path) -> None:
    settings, _source_root = _settings(tmp_path)
    acquire, _calls = _fake_acquirer()
    _service(settings, acquire)
    orphan_run = settings.artifacts_dir / "video" / "active-run"
    orphan_run.mkdir(parents=True)
    (orphan_run / "frame.jpg").write_bytes(b"in progress")

    external_lock = FileLock(str(settings.home / "locks" / "ingest-global.lock"))
    with external_lock:
        _service(settings, acquire)
        assert orphan_run.is_dir()

    _service(settings, acquire)
    assert not orphan_run.exists()
