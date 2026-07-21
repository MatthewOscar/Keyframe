from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from filelock import FileLock
from PIL import Image

import video_context_mcp.service as service_module
from video_context_mcp.acquisition import (
    AcquiredSource,
    SourceKind,
    SourceMetadata,
)
from video_context_mcp.acquisition import (
    TranscriptSegment as AcquiredTranscriptSegment,
)
from video_context_mcp.config import Settings
from video_context_mcp.constants import (
    GIF_FULL_MAX_SAMPLES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_EDGE,
    PIPELINE_VERSION,
    VISUAL_PROBE_MAX_EDGE,
)
from video_context_mcp.cursors import CursorError
from video_context_mcp.errors import CacheError, ExtractionError, SourceError
from video_context_mcp.models import (
    FrameEvidenceQuality,
    FrameQuality,
    IngestMode,
    MomentKind,
    SearchChannel,
    TranscriptMode,
    TranscriptView,
    VideoRecord,
    VisualCoverage,
    VisualMoment,
)
from video_context_mcp.service import KeyframeService
from video_context_mcp.storage import KeyframeStore
from video_context_mcp.vision import BoundingBox, OCRLine, OCRResult, SampledFrame
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
    has_audio: bool = True,
    duration_s: float = 10.0,
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
                duration_s=duration_s,
                provider="local",
                file_size_bytes=len(contents),
                has_audio=has_audio,
                content_sha256=digest,
                content_mtime_ns=stat.st_mtime_ns,
            ),
            transcript=transcript,
            media_path=path,
        )

    return acquire, calls


def _fake_timed_acquirer(
    segments: tuple[AcquiredTranscriptSegment, ...],
    *,
    duration_s: float,
) -> Callable[..., AcquiredSource]:
    base_acquire, _calls = _fake_acquirer(duration_s=duration_s)

    def acquire(source: str, settings: Settings, **kwargs: object) -> AcquiredSource:
        acquired = base_acquire(source, settings, **kwargs)
        acquired.transcript = segments
        return acquired

    return acquire


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


def _fake_probe_extractor(
    *, kinds: tuple[str, ...] = ("code", "slide")
) -> Callable[..., list[ExtractedVisualMoment]]:
    full_extractor = _fake_visual_extractor(kinds=kinds[:1])

    def extract(
        media_path: Path,
        work_dir: Path,
        **kwargs: object,
    ) -> list[ExtractedVisualMoment]:
        moments = full_extractor(media_path, work_dir, **kwargs)
        return [
            replace(
                moment,
                start_s=moment.timestamp_s,
                end_s=moment.timestamp_s,
                stable_seconds=0.0,
            )
            for moment in moments
        ]

    return extract


def _install_targeted_frame_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    observed_max_edges: list[int] | None = None,
) -> None:
    def sample(
        _media_path: Path,
        work_dir: Path,
        timestamps_s: tuple[float, ...],
        *,
        max_edge: int,
        **_kwargs: object,
    ) -> list[SampledFrame]:
        if observed_max_edges is not None:
            observed_max_edges.append(max_edge)
        work_dir.mkdir(parents=True, exist_ok=True)
        frame = work_dir / "targeted.jpg"
        Image.new("RGB", (640, 360), (21, 34, 55)).save(frame, format="JPEG")
        return [SampledFrame(timestamps_s[0], frame, "0" * 16)]

    def analyze(run: object, **_kwargs: object) -> ExtractedVisualMoment:
        representative = run.representative  # type: ignore[attr-defined]
        line = OCRLine(
            text="targeted evidence",
            indent_spaces=0,
            confidence=0.93,
            box=BoundingBox(left=20, top=20, width=240, height=30),
        )
        ocr = OCRResult(
            text=line.text,
            lines=(line,),
            confidence=0.93,
            source_width=640,
            source_height=360,
        )
        return ExtractedVisualMoment(
            timestamp_s=representative.timestamp_s,
            start_s=representative.timestamp_s,
            end_s=representative.timestamp_s,
            stable_seconds=0,
            frame_path=representative.path,
            kind="slide",
            kind_confidence=0.86,
            ocr=ocr,
            language=None,
            parses=None,
            crop_box=BoundingBox(left=20, top=20, width=240, height=30),
        )

    monkeypatch.setattr(service_module, "sample_frames_at_timestamps", sample)
    monkeypatch.setattr(service_module, "analyze_stable_run", analyze)


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
        probe_visuals=_fake_probe_extractor(kinds=kinds),
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
    assert first.timings is not None
    assert first.timings.acquisition_ms is not None
    assert first.timings.visual_ms is not None
    assert first.timings.index_commit_ms is not None
    assert first.timings.transcription_ms is None
    assert cached.timings is not None
    assert cached.timings.acquisition_ms is None
    assert cached.timings.visual_ms is None
    assert cached.timings.index_commit_ms is None
    assert cached.timings.transcription_ms is None
    assert cached.timings.cache_lookup_ms <= cached.timings.total_ms

    source.write_bytes(b"second, materially different video revision")
    changed = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )

    assert changed.video_id != first.video_id
    assert changed.cache_hit is False
    assert len(calls) == 2


def test_post_lock_cache_race_reports_only_lookup_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, calls = _fake_acquirer()
    service = _service(settings, acquire)
    service.ingest(str(source), transcript_mode=TranscriptMode.CAPTIONS)
    original_find = service.store.find_by_source
    lookups = 0

    def find_after_lock(source_value: str, *, pipeline_version: str) -> VideoRecord | None:
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            return None
        return original_find(source_value, pipeline_version=pipeline_version)

    monkeypatch.setattr(service.store, "find_by_source", find_after_lock)
    raced = service.ingest(str(source), transcript_mode=TranscriptMode.CAPTIONS)

    assert raced.cache_hit is True
    assert len(calls) == 1
    assert lookups == 2
    assert raced.timings is not None
    assert raced.timings.acquisition_ms is None
    assert raced.timings.transcription_ms is None
    assert raced.timings.visual_ms is None
    assert raced.timings.index_commit_ms is None
    assert raced.timings.cache_lookup_ms <= raced.timings.total_ms


def test_visual_scratch_uses_os_temp_and_publication_stays_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.setattr(service_module.tempfile, "tempdir", str(os_temp))

    analysis_dirs: list[Path] = []
    publication_moves: list[tuple[Path, Path]] = []
    base_extractor = _fake_visual_extractor()
    original_replace = service_module.os.replace

    def extract(
        media_path: Path,
        work_dir: Path,
        **kwargs: object,
    ) -> list[ExtractedVisualMoment]:
        analysis_dirs.append(work_dir)
        return base_extractor(media_path, work_dir, **kwargs)

    def record_replace(
        source_path: str | bytes | Path, destination_path: str | bytes | Path
    ) -> None:
        source_candidate = Path(source_path)
        destination_candidate = Path(destination_path)
        if destination_candidate.is_relative_to(settings.artifacts_dir):
            publication_moves.append((source_candidate, destination_candidate))
        original_replace(source_path, destination_path)

    monkeypatch.setattr(service_module.os, "replace", record_replace)
    service = KeyframeService(
        settings=settings,
        acquire=acquire,
        extract_visuals=extract,
        has_whisper=lambda: False,
    )

    result = service.ingest(
        str(source),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.CAPTIONS,
    )

    assert len(analysis_dirs) == 1
    assert analysis_dirs[0].is_relative_to(os_temp)
    assert not analysis_dirs[0].is_relative_to(settings.home)
    assert not analysis_dirs[0].parent.exists()
    assert len(publication_moves) == 1
    staged_run, final_run = publication_moves[0]
    assert staged_run.parent == final_run.parent
    assert staged_run.name.startswith(".staging-")
    assert final_run.is_dir()
    moments = service.store.moments_for_video(result.video_id)
    assert moments
    assert all((settings.home / moment.frame_path).is_file() for moment in moments)
    assert not any(settings.tmp_dir.iterdir())


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
    assert revalidated.timings is not None
    assert revalidated.timings.acquisition_ms is not None
    assert revalidated.timings.index_commit_ms is not None
    assert revalidated.timings.transcription_ms is None
    assert revalidated.timings.visual_ms is None
    assert cached.video_id == first.video_id
    assert cached.cache_hit is True
    assert len(calls) == 2


def test_total_timing_includes_cleanup_after_remote_fingerprint_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, source_root = _settings(tmp_path)
    media = _source_file(source_root)
    calls = 0
    clock_s = 0.0

    class DelayedCleanupSource(AcquiredSource):
        def cleanup(self) -> None:
            nonlocal clock_s
            clock_s += 0.25
            super().cleanup()

    def acquire(source: str, _settings: Settings, **_kwargs: object) -> AcquiredSource:
        nonlocal calls
        calls += 1
        source_type = DelayedCleanupSource if calls == 2 else AcquiredSource
        return source_type(
            metadata=SourceMetadata(
                source=source,
                kind=SourceKind.YOUTUBE,
                video_id="same-provider-id",
                title="Remote alias",
                duration_s=10,
                provider="youtube",
            ),
            transcript=(AcquiredTranscriptSegment(0, 1, "same remote evidence"),),
            media_path=media,
        )

    service = _service(settings, acquire)
    service.ingest(
        "https://www.youtube.com/watch?v=first-alias",
        transcript_mode=TranscriptMode.CAPTIONS,
    )
    monkeypatch.setattr(service_module.time, "perf_counter", lambda: clock_s)

    alias = service.ingest(
        "https://youtu.be/same-provider-id",
        transcript_mode=TranscriptMode.CAPTIONS,
    )

    assert alias.cache_hit is True
    assert alias.timings is not None
    assert alias.timings.total_ms == 250
    assert alias.timings.acquisition_ms == 0
    assert alias.timings.visual_ms is None
    assert alias.timings.transcription_ms is None
    assert alias.timings.index_commit_ms is None


def test_fast_remote_ingest_retains_bounded_proxy_and_reports_it(tmp_path: Path) -> None:
    settings, _source_root = _settings(tmp_path)
    owned_dir = settings.tmp_dir / "acquire-owned"
    media = owned_dir / "probe.mp4"
    media_bytes = b"silent low-resolution proxy"

    def acquire(source: str, _settings: Settings, **_kwargs: object) -> AcquiredSource:
        owned_dir.mkdir(parents=True, exist_ok=True)
        media.write_bytes(media_bytes)
        return AcquiredSource(
            metadata=SourceMetadata(
                source=source,
                kind=SourceKind.YOUTUBE,
                video_id="retained-proxy",
                title="Remote proxy fixture",
                duration_s=10,
                provider="youtube",
                has_audio=False,
            ),
            transcript=(AcquiredTranscriptSegment(0, 1, "remote evidence"),),
            media_path=media,
            owns_media=True,
            media_profile="probe_video",
            media_contains_audio=False,
            _temp_dir=owned_dir,
        )

    service = _service(settings, acquire)
    first = service.ingest(
        "https://www.youtube.com/watch?v=retained-proxy",
        mode=IngestMode.FAST,
        transcript_mode=TranscriptMode.CAPTIONS,
    )

    assert first.proxy_cached is True
    assert first.proxy_size_bytes == len(media_bytes)
    assert first.proxy_expires_at is not None
    assert not owned_dir.exists()
    proxy = service._proxy_cache.get(first.video_id)
    assert proxy is not None
    assert proxy.path.read_bytes() == media_bytes

    cached = service.ingest(
        "https://www.youtube.com/watch?v=retained-proxy",
        mode=IngestMode.FAST,
        transcript_mode=TranscriptMode.CAPTIONS,
    )
    assert cached.cache_hit is True
    assert cached.proxy_cached is True
    assert cached.proxy_size_bytes == len(media_bytes)


def test_proxy_publication_failure_does_not_fail_remote_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _source_root = _settings(tmp_path)
    owned_dir = settings.tmp_dir / "acquire-owned"
    media = owned_dir / "probe.mp4"

    def acquire(source: str, _settings: Settings, **_kwargs: object) -> AcquiredSource:
        owned_dir.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"probe")
        return AcquiredSource(
            metadata=SourceMetadata(
                source=source,
                kind=SourceKind.YOUTUBE,
                video_id="proxy-failure",
                title="Remote proxy failure fixture",
                duration_s=10,
                provider="youtube",
                has_audio=False,
            ),
            transcript=(AcquiredTranscriptSegment(0, 1, "remote evidence"),),
            media_path=media,
            owns_media=True,
            media_profile="probe_video",
            _temp_dir=owned_dir,
        )

    service = _service(settings, acquire)

    def fail_promote(*_args: object, **_kwargs: object) -> None:
        raise CacheError("simulated cache failure")

    monkeypatch.setattr(service._proxy_cache, "promote", fail_promote)
    result = service.ingest(
        "https://www.youtube.com/watch?v=proxy-failure",
        mode=IngestMode.FAST,
        transcript_mode=TranscriptMode.CAPTIONS,
    )

    assert result.status == "ready"
    assert result.proxy_cached is False
    assert any("simulated cache failure" in warning for warning in result.warnings)
    assert not owned_dir.exists()


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
    assert refreshed.visual_coverage is VisualCoverage.FULL
    assert refreshed.keyframe_count == len(prior)
    assert {
        moment.frame_path for moment in service.store.moments_for_video(full.video_id)
    } == prior_paths
    assert all((settings.home / path).is_file() for path in prior_paths)


def test_remote_fast_refresh_rebuilds_probe_instead_of_reusing_stale_full_visuals(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    media = _source_file(source_root)
    calls = 0

    def acquire(_source: str, _settings: Settings, **_kwargs: object) -> AcquiredSource:
        nonlocal calls
        calls += 1
        return AcquiredSource(
            metadata=SourceMetadata(
                source="https://www.youtube.com/watch?v=stable-id",
                kind=SourceKind.YOUTUBE,
                video_id="stable-id",
                title=f"Remote revision {calls}",
                duration_s=10,
                provider="youtube",
            ),
            transcript=(AcquiredTranscriptSegment(0, 1, f"remote transcript revision {calls}"),),
            media_path=media,
        )

    service = _service(settings, acquire)
    full = service.ingest(
        "https://www.youtube.com/watch?v=stable-id",
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.CAPTIONS,
    )
    full_paths = {moment.frame_path for moment in service.store.moments_for_video(full.video_id)}

    refreshed = service.ingest(
        "https://www.youtube.com/watch?v=stable-id",
        mode=IngestMode.FAST,
        transcript_mode=TranscriptMode.CAPTIONS,
        refresh=True,
    )

    assert calls == 2
    assert refreshed.cache_hit is False
    assert refreshed.title == "Remote revision 2"
    assert refreshed.visual_coverage is VisualCoverage.PROBE
    assert refreshed.indexed_mode is IngestMode.FAST
    assert refreshed.keyframe_count == 1
    assert not any((settings.home / path).exists() for path in full_paths)


def test_fast_probe_is_queryable_and_full_upgrade_replaces_it_atomically(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, calls = _fake_acquirer()
    service = _service(settings, acquire)

    fast = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )
    probe_moments = service.store.moments_for_video(fast.video_id)
    probe_paths = {moment.frame_path for moment in probe_moments}

    assert fast.visual_coverage is VisualCoverage.PROBE
    assert fast.indexed_mode is IngestMode.FAST
    assert fast.keyframe_count == 1
    assert service.list_moments(fast.video_id).visual_coverage is VisualCoverage.PROBE
    assert (
        service.search(
            "example_0", video_id=fast.video_id, channel=SearchChannel.SHOWN
        ).visual_coverage
        is VisualCoverage.PROBE
    )
    assert not service.search("example_1", video_id=fast.video_id, channel=SearchChannel.SHOWN).hits
    assert service.get_frame(fast.video_id, t=2).result.visual_coverage is VisualCoverage.PROBE
    old_probe_id = probe_moments[0].moment_id

    full = service.ingest(
        str(source), mode=IngestMode.FULL, transcript_mode=TranscriptMode.CAPTIONS
    )
    full_moments = service.store.moments_for_video(full.video_id)

    assert full.cache_hit is False
    assert full.visual_coverage is VisualCoverage.FULL
    assert full.indexed_mode is IngestMode.FULL
    assert full.keyframe_count == 2
    assert len(calls) == 2
    assert {moment.moment_id for moment in full_moments}.isdisjoint({old_probe_id})
    assert service.search("example_1", video_id=full.video_id, channel=SearchChannel.SHOWN).hits
    assert not any((settings.home / path).exists() for path in probe_paths)
    with pytest.raises(CacheError, match="was not found"):
        service.get_code(full.video_id, moment_id=old_probe_id)

    cached_fast = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )
    assert cached_fast.cache_hit is True
    assert cached_fast.visual_coverage is VisualCoverage.FULL
    assert cached_fast.indexed_mode is IngestMode.FULL


def test_visual_cursor_is_rejected_after_probe_upgrades_to_full(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    service = KeyframeService(
        settings=settings,
        acquire=acquire,
        probe_visuals=_fake_visual_extractor(),
        extract_visuals=_fake_visual_extractor(),
        has_whisper=lambda: False,
    )
    fast = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )
    probe_page = service.list_moments(fast.video_id, limit=1)
    assert probe_page.next_cursor is not None

    full = service.ingest(
        str(source), mode=IngestMode.FULL, transcript_mode=TranscriptMode.CAPTIONS
    )
    assert full.visual_coverage is VisualCoverage.FULL
    with pytest.raises(CursorError, match="does not match"):
        service.list_moments(full.video_id, cursor=probe_page.next_cursor, limit=1)


def test_legacy_none_coverage_never_satisfies_fast_ingest(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    contents = source.read_bytes()
    stat = source.stat()
    digest = hashlib.sha256(contents).hexdigest()
    acquire, calls = _fake_acquirer()
    service = _service(settings, acquire)
    service.store.save_video(
        VideoRecord(
            video_id=f"local-{digest[:16]}",
            source=str(source),
            source_kind="local",
            availability="local",
            source_fingerprint=f"sha256:{digest}:pipeline:{PIPELINE_VERSION}",
            title="Legacy transcript-only index",
            duration_s=10,
            has_transcript=False,
            transcript_mode=TranscriptMode.CAPTIONS,
            indexed_mode=IngestMode.FAST,
            visual_coverage=VisualCoverage.NONE,
            keyframe_count=0,
            local_source_path=str(source),
            local_source_size=len(contents),
            local_source_mtime_ns=stat.st_mtime_ns,
            pipeline_version=PIPELINE_VERSION,
        ),
        (),
        (),
    )

    result = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )

    assert result.cache_hit is False
    assert result.visual_coverage is VisualCoverage.PROBE
    assert result.keyframe_count == 1
    assert len(calls) == 1


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
    transcript_next = service.get_transcript(
        ingested.video_id,
        cursor=transcript.next_cursor,
        limit=1,
    )
    assert transcript_next.segments[0].segment_id != transcript.segments[0].segment_id
    with pytest.raises(CursorError, match="does not match"):
        service.get_transcript(
            ingested.video_id,
            start_s=0.5,
            cursor=transcript.next_cursor,
            limit=1,
        )
    transcript_restart = service.get_transcript(ingested.video_id, limit=1)
    assert transcript_restart.segments == transcript.segments
    prefix, kind_code, _offset, scope = transcript.next_cursor.split(".")
    oversized_cursor = f"{prefix}.{kind_code}.zzzzzzzzzzzzz.{scope}"
    with pytest.raises(CursorError, match="restart this query once"):
        service.get_transcript(ingested.video_id, cursor=oversized_cursor)

    moments = service.list_moments(ingested.video_id, kind=MomentKind.ANY, limit=1)
    assert moments.next_cursor is not None
    moments_next = service.list_moments(
        ingested.video_id,
        kind=MomentKind.ANY,
        cursor=moments.next_cursor,
        limit=1,
    )
    assert moments_next.moments[0].moment_id != moments.moments[0].moment_id
    with pytest.raises(CursorError, match="does not match"):
        service.list_moments(
            ingested.video_id,
            kind=MomentKind.CODE,
            cursor=moments.next_cursor,
            limit=1,
        )
    moments_restart = service.list_moments(
        ingested.video_id,
        kind=MomentKind.ANY,
        limit=1,
    )
    assert moments_restart.moments == moments.moments

    search = service.search(
        "evidence",
        video_id=ingested.video_id,
        channel=SearchChannel.SAID,
        limit=1,
    )
    assert search.next_cursor is not None
    search_next = service.search(
        "evidence",
        video_id=ingested.video_id,
        channel=SearchChannel.SAID,
        cursor=search.next_cursor,
        limit=1,
    )
    assert search_next.hits[0].segment_id != search.hits[0].segment_id
    with pytest.raises(CursorError, match="does not match"):
        service.search(
            "beta",
            video_id=ingested.video_id,
            channel=SearchChannel.SAID,
            cursor=search.next_cursor,
            limit=1,
        )
    search_restart = service.search(
        "evidence",
        video_id=ingested.video_id,
        channel=SearchChannel.SAID,
        limit=1,
    )
    assert search_restart.hits == search.hits


def test_default_transcript_pages_cover_383_segments_in_two_calls(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    transcript_texts = tuple(f"segment {index}" for index in range(383))
    acquire, _calls = _fake_acquirer(
        transcript_texts=transcript_texts,
        duration_s=383.0,
    )
    service = _service(settings, acquire)
    ingested = service.ingest(
        str(source),
        mode=IngestMode.FAST,
        transcript_mode=TranscriptMode.CAPTIONS,
    )

    first = service.get_transcript(ingested.video_id)
    assert len(first.segments) == 200
    assert first.has_more is True
    assert first.next_cursor is not None
    assert first.next_cursor.startswith("kf1.")

    second = service.get_transcript(ingested.video_id, cursor=first.next_cursor)
    assert len(second.segments) == 183
    assert second.has_more is False
    assert second.next_cursor is None

    combined = (*first.segments, *second.segments)
    assert [segment.text for segment in combined] == list(transcript_texts)
    assert len({segment.segment_id for segment in combined}) == 383

    explicitly_small = service.get_transcript(ingested.video_id, limit=40)
    assert len(explicitly_small.segments) == 40
    assert explicitly_small.has_more is True

    replacement = "0" if ingested.video_id[-1] != "0" else "1"
    mistyped_video_id = f"{ingested.video_id[:-1]}{replacement}"
    recovered = service.get_transcript(mistyped_video_id)
    assert recovered.video_id == ingested.video_id
    assert recovered.segments == first.segments


def test_compact_transcript_deoverlaps_only_rolling_automatic_captions(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire = _fake_timed_acquirer(
        (
            AcquiredTranscriptSegment(
                0,
                4,
                "we are building",
                origin="automatic_captions",
            ),
            AcquiredTranscriptSegment(
                2,
                6,
                "are building a very",
                origin="automatic_captions",
            ),
            AcquiredTranscriptSegment(
                4,
                8,
                "a very very fast PC",
                origin="automatic_captions",
            ),
            AcquiredTranscriptSegment(8, 9, "yes", origin="automatic_captions"),
            AcquiredTranscriptSegment(9, 10, "yes", origin="automatic_captions"),
        ),
        duration_s=10,
    )
    service = _service(settings, acquire)
    ingested = service.ingest(
        str(source),
        mode=IngestMode.FAST,
        transcript_mode=TranscriptMode.AUTO,
    )

    exact = service.get_transcript(ingested.video_id)
    compact = service.get_transcript(ingested.video_id, view=TranscriptView.COMPACT)

    assert exact.view is TranscriptView.EXACT
    assert [segment.text for segment in exact.segments] == [
        "we are building",
        "are building a very",
        "a very very fast PC",
        "yes",
        "yes",
    ]
    assert compact.view is TranscriptView.COMPACT
    assert len(compact.segments) == 1
    assert compact.segments[0].text == "we are building a very very fast PC yes yes"
    assert compact.segments[0].segment_id == f"{ingested.video_id}:compact:0"


def test_compact_transcript_preserves_repetition_outside_automatic_rolls(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire = _fake_timed_acquirer(
        (
            AcquiredTranscriptSegment(0, 3, "repeat this", origin="captions"),
            AcquiredTranscriptSegment(2, 4, "repeat this", origin="captions"),
            AcquiredTranscriptSegment(4, 5, "again", origin="whisper"),
            AcquiredTranscriptSegment(5, 6, "again", origin="whisper"),
        ),
        duration_s=6,
    )
    service = _service(settings, acquire)
    ingested = service.ingest(str(source), transcript_mode=TranscriptMode.CAPTIONS)

    compact = service.get_transcript(ingested.video_id, view="compact")

    assert compact.segments[0].text == "repeat this repeat this again again"
    assert compact.segments[0].source == "mixed"


def test_compact_transcript_time_bounds_select_minute_bins(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire = _fake_timed_acquirer(
        (
            AcquiredTranscriptSegment(10, 11, "minute zero"),
            AcquiredTranscriptSegment(70, 71, "minute one"),
            AcquiredTranscriptSegment(130, 131, "minute two"),
        ),
        duration_s=180,
    )
    service = _service(settings, acquire)
    ingested = service.ingest(str(source), transcript_mode=TranscriptMode.CAPTIONS)

    compact = service.get_transcript(
        ingested.video_id,
        view=TranscriptView.COMPACT,
        start_s=60,
        end_s=119,
    )

    assert [(segment.start_s, segment.end_s, segment.text) for segment in compact.segments] == [
        (60, 120, "minute one")
    ]


def test_transcript_cursor_cannot_cross_exact_and_compact_views(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire = _fake_timed_acquirer(
        (
            AcquiredTranscriptSegment(1, 2, "first"),
            AcquiredTranscriptSegment(61, 62, "second"),
        ),
        duration_s=120,
    )
    service = _service(settings, acquire)
    ingested = service.ingest(str(source), transcript_mode=TranscriptMode.CAPTIONS)

    compact = service.get_transcript(
        ingested.video_id,
        view=TranscriptView.COMPACT,
        limit=1,
    )
    exact = service.get_transcript(ingested.video_id, limit=1)
    assert compact.next_cursor is not None
    assert exact.next_cursor is not None

    with pytest.raises(CursorError, match="does not match"):
        service.get_transcript(
            ingested.video_id,
            view=TranscriptView.EXACT,
            cursor=compact.next_cursor,
            limit=1,
        )
    with pytest.raises(CursorError, match="does not match"):
        service.get_transcript(
            ingested.video_id,
            view=TranscriptView.COMPACT,
            cursor=exact.next_cursor,
            limit=1,
        )


def test_6889_second_compact_transcript_fits_one_maximum_page(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire = _fake_timed_acquirer(
        tuple(
            AcquiredTranscriptSegment(second, second + 0.5, f"word-{second}")
            for second in range(6_889)
        ),
        duration_s=6_889,
    )
    service = _service(settings, acquire)
    ingested = service.ingest(str(source), transcript_mode=TranscriptMode.CAPTIONS)

    compact = service.get_transcript(ingested.video_id, view=TranscriptView.COMPACT)

    assert compact.view is TranscriptView.COMPACT
    assert len(compact.segments) == 115
    assert compact.has_more is False
    assert compact.next_cursor is None
    assert compact.segments[0].start_s == 0
    assert compact.segments[-1].start_s == 6_840
    assert compact.segments[-1].end_s == 6_889


def test_compact_transcript_paginates_more_than_200_minute_blocks(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire = _fake_timed_acquirer(
        tuple(
            AcquiredTranscriptSegment(minute * 60, minute * 60 + 1, f"minute-{minute}")
            for minute in range(201)
        ),
        duration_s=12_060,
    )
    service = _service(settings, acquire)
    ingested = service.ingest(str(source), transcript_mode=TranscriptMode.CAPTIONS)

    first = service.get_transcript(ingested.video_id, view=TranscriptView.COMPACT)
    assert len(first.segments) == 200
    assert first.has_more is True
    assert first.next_cursor is not None

    second = service.get_transcript(
        ingested.video_id,
        view=TranscriptView.COMPACT,
        cursor=first.next_cursor,
    )
    assert [segment.text for segment in second.segments] == ["minute-200"]
    assert second.has_more is False
    assert second.next_cursor is None


def test_local_video_id_typo_recovery_refuses_ambiguous_match(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    ingested = service.ingest(
        str(source),
        mode=IngestMode.FAST,
        transcript_mode=TranscriptMode.CAPTIONS,
    )
    original = service.store.get_video(ingested.video_id)
    assert original is not None

    final_replacement = "0" if ingested.video_id[-1] != "0" else "1"
    requested = f"{ingested.video_id[:-1]}{final_replacement}"
    alternate_replacement = "0" if requested[-2] != "0" else "1"
    alternate_id = f"{requested[:-2]}{alternate_replacement}{requested[-1]}"
    service.store.save_video(
        original.model_copy(
            update={
                "video_id": alternate_id,
                "source": "ambiguous-local-fixture.mp4",
                "source_fingerprint": "sha256:ambiguous:pipeline:test",
                "has_transcript": False,
                "local_source_path": None,
                "local_source_size": None,
                "local_source_mtime_ns": None,
            }
        ),
        (),
        (),
    )

    with pytest.raises(CacheError, match="Copy video_id byte-for-byte"):
        service.get_transcript(requested)


def test_local_video_id_typo_is_canonicalized_across_read_tools(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    ingested = service.ingest(
        str(source),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.CAPTIONS,
    )
    mistyped_video_id = f"{ingested.video_id[:-1]}Z"

    transcript = service.get_transcript(mistyped_video_id)
    search = service.search("alpha", video_id=mistyped_video_id)
    moments = service.list_moments(mistyped_video_id)
    code = service.get_code(mistyped_video_id, t=2)
    frame = service.get_frame(mistyped_video_id, t=2)

    assert transcript.video_id == ingested.video_id
    assert search.hits and {hit.video_id for hit in search.hits} == {ingested.video_id}
    assert moments.video_id == ingested.video_id
    assert code.result.video_id == ingested.video_id
    assert frame.result.video_id == ingested.video_id


def test_remote_video_ids_are_never_fuzzy_matched(tmp_path: Path) -> None:
    settings, _source_root = _settings(tmp_path)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    service.store.save_video(
        VideoRecord(
            video_id="youtube-0123456789abcdef",
            source="https://www.youtube.com/watch?v=fixture",
            source_kind="youtube",
            source_fingerprint="youtube:fixture:pipeline:test",
            title="Remote fixture",
            duration_s=10,
            has_transcript=False,
            indexed_mode=IngestMode.FAST,
            visual_coverage=VisualCoverage.PROBE,
            keyframe_count=0,
            pipeline_version=PIPELINE_VERSION,
        ),
        (),
        (),
    )

    with pytest.raises(CacheError, match="Copy video_id byte-for-byte"):
        service.get_transcript("youtube-0123456789abcdee")


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
    assert Path(selected.result.render_path).read_bytes() == selected.image_data
    assert selected.result.render_markdown == (
        f"![Keyframe frame at 00:02](<{Path(selected.result.render_path).as_posix()}>)"
    )
    assert selected.result.render_expires_at.endswith("+00:00")


def test_get_frame_round_trips_an_exact_moment_and_keeps_timestamp_compatibility(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    ingested = service.ingest(
        str(source), mode=IngestMode.FULL, transcript_mode=TranscriptMode.CAPTIONS
    )

    listed = service.list_moments(ingested.video_id, kind=MomentKind.ANY)
    assert len(listed.moments) == 2
    exact_id = listed.moments[1].moment_id
    stored = service.store.get_moment(exact_id)
    assert stored is not None

    exact = service.get_frame(ingested.video_id, moment_id=exact_id)

    assert exact.result.moment_id == exact_id
    assert exact.result.requested_moment_id == exact_id
    assert exact.result.requested_t is None
    assert exact.result.requested_t_covered is None
    assert exact.result.start_s == stored.start_s
    assert exact.result.end_s == stored.end_s
    assert exact.result.actual_t == stored.actual_t
    assert exact.result.ocr_text == stored.ocr_text
    assert exact.result.ocr_confidence == stored.ocr_confidence
    assert exact.result.classification_confidence == stored.classification_confidence
    assert exact.result.requested_quality is FrameQuality.AUTO
    assert exact.result.evidence_quality is FrameEvidenceQuality.RETAINED
    assert (exact.result.width, exact.result.height) == (320, 180)
    assert exact.image_data
    assert Path(exact.result.render_path).read_bytes() == exact.image_data
    assert exact.result.render_markdown == (
        f"![Keyframe frame at 00:07](<{Path(exact.result.render_path).as_posix()}>)"
    )
    assert exact.result.render_expires_at.endswith("+00:00")

    timestamp = service.get_frame(ingested.video_id, t=2.0)
    assert timestamp.result.requested_moment_id is None
    assert timestamp.result.requested_t == 2.0
    assert timestamp.result.requested_t_covered is True
    assert timestamp.result.moment_id == listed.moments[0].moment_id

    with pytest.raises(SourceError, match="exactly one"):
        service.get_frame(ingested.video_id)
    with pytest.raises(SourceError, match="exactly one"):
        service.get_frame(ingested.video_id, moment_id=exact_id, t=7.0)

    indexed = service.store.get_video(ingested.video_id)
    assert indexed is not None
    other_video_id = "local-0000000000000000"
    service.store.save_video(
        indexed.model_copy(
            update={
                "video_id": other_video_id,
                "source": str(source_root / "other.mp4"),
                "source_fingerprint": "sha256:other:pipeline:test",
                "has_transcript": False,
                "keyframe_count": 0,
                "local_source_path": None,
                "local_source_size": None,
                "local_source_mtime_ns": None,
            }
        ),
        (),
        (),
    )
    with pytest.raises(CacheError, match="was not found in video"):
        service.get_frame(other_video_id, moment_id=exact_id)


def test_frame_auto_seeks_an_authorized_local_source_for_a_probe_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    ingested = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )
    observed_max_edges: list[int] = []
    _install_targeted_frame_fakes(monkeypatch, observed_max_edges=observed_max_edges)

    frame = service.get_frame(ingested.video_id, t=7.0)

    assert frame.result.moment_id is None
    assert frame.result.requested_t == 7.0
    assert frame.result.requested_t_covered is True
    assert frame.result.actual_t == 7.0
    assert frame.result.evidence_quality is FrameEvidenceQuality.SOURCE
    assert frame.result.requested_quality is FrameQuality.AUTO
    assert (frame.result.width, frame.result.height) == (640, 360)
    assert frame.result.ocr_text == "targeted evidence"
    assert observed_max_edges == [MAX_IMAGE_EDGE]
    assert Path(frame.result.render_path).read_bytes() == frame.image_data
    assert frame.result.render_markdown == (
        f"![Keyframe frame at 00:07](<{Path(frame.result.render_path).as_posix()}>)"
    )


def test_frame_probe_quality_forces_a_bounded_local_seek(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    ingested = service.ingest(
        str(source), mode=IngestMode.FAST, transcript_mode=TranscriptMode.CAPTIONS
    )
    observed_max_edges: list[int] = []
    _install_targeted_frame_fakes(monkeypatch, observed_max_edges=observed_max_edges)

    frame = service.get_frame(ingested.video_id, t=2.0, quality=FrameQuality.PROBE)

    assert frame.result.moment_id is None
    assert frame.result.evidence_quality is FrameEvidenceQuality.PROBE
    assert frame.result.requested_quality is FrameQuality.PROBE
    assert observed_max_edges == [VISUAL_PROBE_MAX_EDGE]


def test_frame_auto_seeks_a_retained_remote_proxy_for_a_probe_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _source_root = _settings(tmp_path)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    run_dir = settings.artifacts_dir / "video" / "manual"
    run_dir.mkdir(parents=True)
    retained = run_dir / "frame.jpg"
    Image.new("RGB", (100, 50), "black").save(retained, format="JPEG")
    _seed_artifact_moment(service, relative_path=str(retained.relative_to(settings.home)))
    video = service.store.get_video("video")
    moment = service.store.get_moment("video:m:0")
    assert video is not None and moment is not None
    service.store.save_video(
        video.model_copy(
            update={
                "source": "https://www.youtube.com/watch?v=fixture",
                "source_kind": "youtube",
                "duration_s": 10,
            }
        ),
        (),
        (moment,),
    )
    proxy_dir = settings.proxy_dir / "video"
    proxy_dir.mkdir(parents=True)
    (proxy_dir / "media.mp4").write_bytes(b"bounded proxy")
    observed_max_edges: list[int] = []
    _install_targeted_frame_fakes(monkeypatch, observed_max_edges=observed_max_edges)

    frame = service.get_frame("video", t=7.0)

    assert frame.result.moment_id is None
    assert frame.result.evidence_quality is FrameEvidenceQuality.PROBE
    assert frame.result.requested_quality is FrameQuality.AUTO
    assert observed_max_edges == [VISUAL_PROBE_MAX_EDGE]


def test_remote_source_quality_is_rejected_without_opening_remote_ffmpeg_access(
    tmp_path: Path,
) -> None:
    settings, _source_root = _settings(tmp_path)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    run_dir = settings.artifacts_dir / "video" / "manual"
    run_dir.mkdir(parents=True)
    retained = run_dir / "frame.jpg"
    Image.new("RGB", (100, 50), "black").save(retained, format="JPEG")
    _seed_artifact_moment(service, relative_path=str(retained.relative_to(settings.home)))
    video = service.store.get_video("video")
    moment = service.store.get_moment("video:m:0")
    assert video is not None and moment is not None
    service.store.save_video(
        video.model_copy(
            update={
                "source": "https://www.youtube.com/watch?v=fixture",
                "source_kind": "youtube",
            }
        ),
        (),
        (moment,),
    )

    with pytest.raises(SourceError, match="does not let FFmpeg fetch remote URLs"):
        service.get_frame("video", t=1.0, quality=FrameQuality.SOURCE)


def test_frame_auto_preserves_nearest_retained_fallback_without_a_proxy(
    tmp_path: Path,
) -> None:
    settings, _source_root = _settings(tmp_path)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    run_dir = settings.artifacts_dir / "video" / "manual"
    run_dir.mkdir(parents=True)
    retained = run_dir / "frame.jpg"
    Image.new("RGB", (100, 50), "black").save(retained, format="JPEG")
    _seed_artifact_moment(service, relative_path=str(retained.relative_to(settings.home)))
    video = service.store.get_video("video")
    moment = service.store.get_moment("video:m:0")
    assert video is not None and moment is not None
    service.store.save_video(
        video.model_copy(
            update={
                "source": "https://www.youtube.com/watch?v=fixture",
                "source_kind": "youtube",
                "duration_s": 10,
            }
        ),
        (),
        (moment,),
    )

    frame = service.get_frame("video", t=7.0)

    assert frame.result.moment_id == moment.moment_id
    assert frame.result.requested_t_covered is False
    assert frame.result.evidence_quality is FrameEvidenceQuality.RETAINED


def test_visual_time_bounds_exclude_disjoint_candidates_and_scope_cursors(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    service = _service(settings, acquire)
    ingested = service.ingest(
        str(source), mode=IngestMode.FULL, transcript_mode=TranscriptMode.CAPTIONS
    )

    early = service.list_moments(ingested.video_id, end_s=4.5)
    late = service.list_moments(ingested.video_id, start_s=4.5)
    assert len(early.moments) == 1
    assert len(late.moments) == 1
    assert early.moments[0].end_s < late.moments[0].start_s

    early_search = service.search(
        "return",
        video_id=ingested.video_id,
        channel=SearchChannel.SHOWN,
        end_s=4.5,
    )
    late_search = service.search(
        "return",
        video_id=ingested.video_id,
        channel=SearchChannel.SHOWN,
        start_s=4.5,
    )
    assert [hit.moment_id for hit in early_search.hits] == [early.moments[0].moment_id]
    assert [hit.moment_id for hit in late_search.hits] == [late.moments[0].moment_id]

    moment_page = service.list_moments(
        ingested.video_id,
        start_s=0,
        end_s=9,
        limit=1,
    )
    assert moment_page.next_cursor is not None
    with pytest.raises(CursorError, match="does not match"):
        service.list_moments(
            ingested.video_id,
            start_s=0.1,
            end_s=9,
            cursor=moment_page.next_cursor,
            limit=1,
        )
    with pytest.raises(CursorError, match="does not match"):
        service.list_moments(
            ingested.video_id,
            start_s=0,
            end_s=8.9,
            cursor=moment_page.next_cursor,
            limit=1,
        )

    search_page = service.search(
        "return",
        video_id=ingested.video_id,
        channel=SearchChannel.SHOWN,
        start_s=0,
        end_s=9,
        limit=1,
    )
    assert search_page.next_cursor is not None
    with pytest.raises(CursorError, match="does not match"):
        service.search(
            "return",
            video_id=ingested.video_id,
            channel=SearchChannel.SHOWN,
            start_s=0.1,
            end_s=9,
            cursor=search_page.next_cursor,
            limit=1,
        )
    with pytest.raises(CursorError, match="does not match"):
        service.search(
            "return",
            video_id=ingested.video_id,
            channel=SearchChannel.SHOWN,
            start_s=0,
            end_s=8.9,
            cursor=search_page.next_cursor,
            limit=1,
        )


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
        with pytest.raises(SourceError, match="finite non-negative"):
            service.search("verified", video_id="video", start_s=value)
        with pytest.raises(SourceError, match="finite non-negative"):
            service.list_moments("video", end_s=value)

    with pytest.raises(SourceError, match="start_s must not be greater"):
        service.search("verified", video_id="video", start_s=2, end_s=1)
    with pytest.raises(SourceError, match="start_s must not be greater"):
        service.list_moments("video", start_s=2, end_s=1)


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
        visual_coverage=VisualCoverage.FULL,
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


class _PostCommitFailingStore(KeyframeStore):
    fail_after_commit = False

    def save_video(
        self,
        video: VideoRecord,
        segments: tuple[object, ...] | list[object],
        moments: tuple[object, ...] | list[object],
    ) -> None:
        super().save_video(video, segments, moments)  # type: ignore[arg-type]
        if self.fail_after_commit:
            raise RuntimeError("simulated post-commit failure")


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


def test_post_commit_failure_never_deletes_artifacts_referenced_by_database(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer()
    store = _PostCommitFailingStore(settings.cache_dir / "keyframe.sqlite3")
    service = _service(settings, acquire, store=store)
    store.fail_after_commit = True

    with pytest.raises(RuntimeError, match="simulated post-commit failure"):
        service.ingest(
            str(source),
            mode=IngestMode.FAST,
            transcript_mode=TranscriptMode.CAPTIONS,
        )

    committed = store.find_by_source(str(source), pipeline_version=PIPELINE_VERSION)
    assert committed is not None
    assert committed.visual_coverage is VisualCoverage.PROBE
    referenced = store.moments_for_video(committed.video_id)
    assert referenced
    assert all((settings.home / moment.frame_path).is_file() for moment in referenced)
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


def test_full_whisper_ingest_overlaps_transcription_and_visual_extraction(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer(transcript_texts=())
    whisper_started = threading.Event()
    visuals_started = threading.Event()
    base_extractor = _fake_visual_extractor()

    def transcribe(
        _media_path: Path,
        **_kwargs: object,
    ) -> tuple[AcquiredTranscriptSegment, ...]:
        assert _kwargs["timeout_s"] == 300.0
        whisper_started.set()
        assert visuals_started.wait(timeout=2), "visual extraction did not overlap Whisper"
        return (AcquiredTranscriptSegment(0, 1, "parallel speech", origin="whisper"),)

    def extract(
        media_path: Path,
        work_dir: Path,
        **kwargs: object,
    ) -> list[ExtractedVisualMoment]:
        visuals_started.set()
        assert whisper_started.wait(timeout=2), "Whisper did not start before visual extraction"
        return base_extractor(media_path, work_dir, **kwargs)

    service = KeyframeService(
        settings=settings,
        acquire=acquire,
        extract_visuals=extract,
        transcribe=transcribe,
        has_whisper=lambda: True,
    )
    progress: list[tuple[float, str]] = []

    result = service.ingest(
        str(source),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.WHISPER,
        progress=lambda value, message: progress.append((value, message)),
    )

    assert result.has_transcript is True
    assert result.keyframe_count == 2
    assert result.timings is not None
    assert result.timings.transcription_ms is not None
    assert result.timings.visual_ms is not None
    assert result.timings.total_ms >= result.timings.transcription_ms
    assert result.timings.total_ms >= result.timings.visual_ms
    assert [value for value, _message in progress] == sorted(value for value, _message in progress)
    assert any(message == "Local Whisper transcription complete" for _value, message in progress)


def test_audio_less_auto_ingest_skips_whisper_and_reuses_cache(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = source_root / "animation.gif"
    source.write_bytes(b"animated visual states")
    acquire, calls = _fake_acquirer(transcript_texts=(), has_audio=False)
    transcribe_calls = 0

    def transcribe(
        _media_path: Path,
        **_kwargs: object,
    ) -> tuple[AcquiredTranscriptSegment, ...]:
        nonlocal transcribe_calls
        transcribe_calls += 1
        raise AssertionError("audio-less media must not invoke Whisper")

    service = KeyframeService(
        settings=settings,
        acquire=acquire,
        extract_visuals=_fake_visual_extractor(),
        transcribe=transcribe,
        has_whisper=lambda: True,
    )

    first = service.ingest(
        str(source),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.AUTO,
    )
    second = service.ingest(
        str(source),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.AUTO,
    )

    assert first.has_audio is False
    assert first.has_transcript is False
    assert "Source has no audio stream; speech transcription was skipped." in first.warnings
    assert second.cache_hit is True
    assert calls == [str(source.resolve())]
    assert transcribe_calls == 0


def test_audio_less_explicit_whisper_fails_actionably(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = source_root / "animation.gif"
    source.write_bytes(b"animated visual states")
    acquire, _calls = _fake_acquirer(transcript_texts=(), has_audio=False)
    service = KeyframeService(
        settings=settings,
        acquire=acquire,
        extract_visuals=_fake_visual_extractor(),
        has_whisper=lambda: True,
    )

    with pytest.raises(SourceError, match="source has no audio stream"):
        service.ingest(
            str(source),
            mode=IngestMode.FULL,
            transcript_mode=TranscriptMode.WHISPER,
        )


def test_full_gif_uses_dense_bounded_visual_sampling(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = source_root / "animation.gif"
    source.write_bytes(b"animated visual states")
    acquire, _calls = _fake_acquirer(transcript_texts=(), has_audio=False)
    observed: dict[str, object] = {}
    base_extractor = _fake_visual_extractor()

    def extract(media_path: Path, work_dir: Path, **kwargs: object) -> list[ExtractedVisualMoment]:
        observed.update(kwargs)
        return base_extractor(media_path, work_dir, **kwargs)

    service = KeyframeService(
        settings=settings,
        acquire=acquire,
        extract_visuals=extract,
        has_whisper=lambda: True,
    )

    service.ingest(
        str(source),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.AUTO,
    )

    assert observed["fps"] == 8.0
    assert observed["distance_threshold"] == 0
    assert observed["min_stable_seconds"] == 0.125
    assert observed["max_moments"] == 48


def test_full_gif_sampling_cap_holds_for_long_sources(tmp_path: Path) -> None:
    settings, source_root = _settings(tmp_path)
    source = source_root / "long-animation.gif"
    source.write_bytes(b"long animated visual states")
    duration_s = 14_400.0
    acquire, _calls = _fake_acquirer(transcript_texts=(), has_audio=False, duration_s=duration_s)
    observed: dict[str, object] = {}
    base_extractor = _fake_visual_extractor()

    def extract(media_path: Path, work_dir: Path, **kwargs: object) -> list[ExtractedVisualMoment]:
        observed.update(kwargs)
        return base_extractor(media_path, work_dir, **kwargs)

    service = KeyframeService(
        settings=settings,
        acquire=acquire,
        extract_visuals=extract,
        has_whisper=lambda: True,
    )

    service.ingest(
        str(source),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.AUTO,
        max_duration_s=int(duration_s),
    )

    sample_fps = observed["fps"]
    assert isinstance(sample_fps, float)
    assert sample_fps * duration_s <= GIF_FULL_MAX_SAMPLES + 1e-9


def test_zero_moment_full_whisper_reports_both_outcomes_and_can_seek_source_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer(transcript_texts=())

    def transcribe(
        _media_path: Path,
        **_kwargs: object,
    ) -> tuple[AcquiredTranscriptSegment, ...]:
        return (AcquiredTranscriptSegment(0, 1, "speech survived", origin="whisper"),)

    def extract(
        _media_path: Path,
        _work_dir: Path,
        **_kwargs: object,
    ) -> list[ExtractedVisualMoment]:
        return []

    service = KeyframeService(
        settings=settings,
        acquire=acquire,
        extract_visuals=extract,
        transcribe=transcribe,
        has_whisper=lambda: True,
    )
    result = service.ingest(
        str(source),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.WHISPER,
    )

    assert result.visual_coverage is VisualCoverage.FULL
    assert result.keyframe_count == 0
    assert any("No stable visual moments" in warning for warning in result.warnings)
    assert "Used local Whisper speech transcription." in result.warnings
    with pytest.raises(CacheError, match="full visual index"):
        service.get_code(result.video_id, t=1)
    _install_targeted_frame_fakes(monkeypatch)
    frame = service.get_frame(result.video_id, t=1)
    assert frame.result.evidence_quality is FrameEvidenceQuality.SOURCE
    assert frame.result.moment_id is None


def test_failed_parallel_whisper_removes_visual_run_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    settings, source_root = _settings(tmp_path)
    source = _source_file(source_root)
    acquire, _calls = _fake_acquirer(transcript_texts=())
    visuals_finished = threading.Event()
    base_extractor = _fake_visual_extractor()

    def transcribe(
        _media_path: Path,
        **_kwargs: object,
    ) -> tuple[AcquiredTranscriptSegment, ...]:
        assert visuals_finished.wait(timeout=2)
        raise ExtractionError("simulated isolated Whisper failure")

    def extract(
        media_path: Path,
        work_dir: Path,
        **kwargs: object,
    ) -> list[ExtractedVisualMoment]:
        moments = base_extractor(media_path, work_dir, **kwargs)
        visuals_finished.set()
        return moments

    service = KeyframeService(
        settings=settings,
        acquire=acquire,
        extract_visuals=extract,
        transcribe=transcribe,
        has_whisper=lambda: True,
    )

    with pytest.raises(ExtractionError, match="simulated isolated Whisper failure"):
        service.ingest(
            str(source),
            mode=IngestMode.FULL,
            transcript_mode=TranscriptMode.WHISPER,
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
