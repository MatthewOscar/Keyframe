from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

from video_context_mcp.acquisition import acquire_source
from video_context_mcp.config import Settings
from video_context_mcp.constants import MAX_IMAGE_BYTES
from video_context_mcp.models import (
    FrameRegion,
    IngestMode,
    MomentKind,
    SearchChannel,
    TranscriptMode,
    VisualCoverage,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
VIDEO_PATH = (FIXTURE_DIR / "keyframe-synthetic.mp4").resolve()
GOLDEN_PATH = FIXTURE_DIR / "golden.json"
REQUIRED_TOOLS = ("ffmpeg", "ffprobe", "tesseract", "node")
MISSING_TOOLS = tuple(tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        bool(MISSING_TOOLS),
        reason=f"native Keyframe tools are missing: {', '.join(MISSING_TOOLS)}",
    ),
]


def _golden() -> dict[str, Any]:
    loaded = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _settings(home: Path) -> Settings:
    return Settings(
        home=home,
        allowed_roots=(FIXTURE_DIR.resolve(),),
        ffmpeg_executable=shutil.which("ffmpeg") or "ffmpeg",
        ffprobe_executable=shutil.which("ffprobe") or "ffprobe",
        tesseract_executable=shutil.which("tesseract") or "tesseract",
        node_executable=shutil.which("node") or "node",
    )


def test_first_party_fixture_matches_golden_metadata(tmp_path: Path) -> None:
    golden = _golden()
    digest = hashlib.sha256(VIDEO_PATH.read_bytes()).hexdigest()
    assert digest == golden["sha256"]

    settings = _settings(tmp_path / "keyframe-home")
    acquired = acquire_source(
        VIDEO_PATH,
        settings,
        mode="full",
        transcript_mode="captions",
    )
    try:
        metadata = golden["metadata"]
        transcript = golden["transcript"]
        assert acquired.metadata.video_id == golden["video_id"]
        assert acquired.metadata.title == metadata["title"]
        assert acquired.metadata.duration_s == pytest.approx(
            metadata["duration_s"], abs=metadata["duration_tolerance_s"]
        )
        assert acquired.metadata.width == metadata["width"]
        assert acquired.metadata.height == metadata["height"]
        assert acquired.metadata.fps == pytest.approx(metadata["fps"])
        assert len(acquired.transcript) == transcript["segment_count"]
        assert acquired.transcript[1].start_s == transcript["expected_start_s"]
        assert acquired.transcript[1].text == transcript["expected_text"]
        assert acquired.media_path == VIDEO_PATH
        assert acquired.owns_media is False
    finally:
        acquired.cleanup()

    assert VIDEO_PATH.is_file(), "cleanup must never remove caller-owned local media"


def test_full_local_video_rag_round_trip_and_persistent_cache(tmp_path: Path) -> None:
    # Imported here so collection and the metadata test remain useful while the
    # independently developed service layer is being assembled.
    from video_context_mcp.service import KeyframeService

    golden = _golden()
    settings = _settings(tmp_path / "keyframe-home")
    service = KeyframeService(settings=settings)
    progress_events: list[tuple[float, str]] = []

    ingested = service.ingest(
        str(VIDEO_PATH),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.CAPTIONS,
        max_duration_s=30,
        refresh=False,
        progress=lambda value, message: progress_events.append((value, message)),
    )

    assert ingested.video_id == golden["video_id"]
    assert ingested.title == golden["metadata"]["title"]
    assert ingested.indexed_mode is IngestMode.FULL
    assert ingested.visual_coverage is VisualCoverage.FULL
    assert ingested.cache_hit is False
    assert ingested.has_transcript is True
    assert ingested.keyframe_count >= 3
    assert progress_events
    assert progress_events[-1][0] >= 90

    transcript = service.get_transcript(ingested.video_id, limit=40)
    assert [segment.text for segment in transcript.segments][1] == golden["transcript"][
        "expected_text"
    ]
    assert transcript.has_more is False

    said = service.search(
        golden["transcript"]["golden_query"],
        video_id=ingested.video_id,
        channel=SearchChannel.SAID,
        limit=3,
    )
    assert said.hits
    assert said.hits[0].channel is SearchChannel.SAID
    assert said.hits[0].start_s == golden["transcript"]["expected_start_s"]

    shown = service.search(
        golden["visual"]["golden_query"],
        video_id=ingested.video_id,
        channel=SearchChannel.SHOWN,
        limit=3,
    )
    assert shown.hits
    assert shown.hits[0].channel is SearchChannel.SHOWN
    assert shown.hits[0].moment_id is not None

    moments = service.list_moments(
        ingested.video_id,
        kind=MomentKind.ANY,
        limit=20,
    )
    code_moment = next(moment for moment in moments.moments if moment.kind is MomentKind.CODE)
    assert 0 < code_moment.classification_confidence <= 1
    assert code_moment.start_s == golden["visual"]["expected_start_s"]
    assert code_moment.end_s == golden["visual"]["expected_end_s"]
    assert golden["visual"]["expected_symbol"] in code_moment.ocr_preview.lower()

    code_payload = service.get_code(
        ingested.video_id,
        moment_id=code_moment.moment_id,
        t=None,
    )
    assert code_payload.mime_type == "image/jpeg"
    assert 0 < len(code_payload.image_data) <= MAX_IMAGE_BYTES
    assert code_payload.result.moment_id == code_moment.moment_id
    assert code_payload.result.language_guess == "python"
    assert golden["visual"]["expected_symbol"] in code_payload.result.code.lower()
    assert code_payload.result.parses is True or code_payload.result.notes

    requested_t = golden["visual"]["requested_frame_s"]
    frame_payload = service.get_frame(
        ingested.video_id,
        t=requested_t,
        region=FrameRegion.FULL,
    )
    assert frame_payload.mime_type == "image/jpeg"
    assert 0 < len(frame_payload.image_data) <= MAX_IMAGE_BYTES
    assert frame_payload.result.requested_t == requested_t
    assert frame_payload.result.actual_t == pytest.approx(5.0, abs=1.0)

    restarted = KeyframeService(settings=settings)
    persisted = restarted.search(
        golden["visual"]["golden_query"],
        video_id=ingested.video_id,
        channel=SearchChannel.SHOWN,
        limit=3,
    )
    assert persisted.hits
    assert persisted.hits[0].moment_id == shown.hits[0].moment_id

    started = time.perf_counter()
    cached = restarted.ingest(
        str(VIDEO_PATH),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.CAPTIONS,
        max_duration_s=30,
        refresh=False,
    )
    elapsed = time.perf_counter() - started
    assert cached.video_id == ingested.video_id
    assert cached.cache_hit is True
    assert elapsed < 1.0

    assert VIDEO_PATH.is_file()
    assert not any(settings.tmp_dir.iterdir())


def test_animated_gif_visual_round_trip_skips_speech_and_caches(tmp_path: Path) -> None:
    from video_context_mcp.service import KeyframeService

    animation = tmp_path / "workflow.gif"
    font = ImageFont.load_default(size=48)
    frames: list[Image.Image] = []
    for label, color in (
        ("STATE ALPHA", (230, 245, 255)),
        ("STATE BRAVO", (255, 240, 210)),
        ("STATE CHARLIE", (225, 255, 225)),
    ):
        frame = Image.new("RGB", (640, 360), color)
        drawing = ImageDraw.Draw(frame)
        drawing.rectangle((30, 30, 610, 330), outline="black", width=8)
        drawing.text((100, 145), label, fill="black", font=font)
        frames.append(frame)
    frames[0].save(
        animation,
        save_all=True,
        append_images=frames[1:],
        duration=1_000,
        loop=0,
    )
    settings = Settings(
        home=tmp_path / "gif-home",
        allowed_roots=(tmp_path.resolve(),),
        ffmpeg_executable=shutil.which("ffmpeg") or "ffmpeg",
        ffprobe_executable=shutil.which("ffprobe") or "ffprobe",
        tesseract_executable=shutil.which("tesseract") or "tesseract",
        node_executable=shutil.which("node") or "node",
    )
    service = KeyframeService(settings=settings)

    probed = service.ingest(
        str(animation),
        mode=IngestMode.FAST,
        transcript_mode=TranscriptMode.AUTO,
        max_duration_s=30,
    )
    assert probed.has_audio is False
    assert probed.visual_coverage is VisualCoverage.PROBE
    assert 1 <= probed.keyframe_count <= 12

    ingested = service.ingest(
        str(animation),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.AUTO,
        max_duration_s=30,
    )

    assert ingested.has_audio is False
    assert ingested.has_transcript is False
    assert ingested.visual_coverage is VisualCoverage.FULL
    assert ingested.keyframe_count >= 3
    assert any("no audio stream" in warning for warning in ingested.warnings)
    shown = service.search(
        "BRAVO",
        video_id=ingested.video_id,
        channel=SearchChannel.SHOWN,
        limit=3,
    )
    assert shown.hits
    frame = service.get_frame(ingested.video_id, t=1.5, region=FrameRegion.FULL)
    assert frame.mime_type == "image/jpeg"
    assert 0 < len(frame.image_data) <= MAX_IMAGE_BYTES

    cached = service.ingest(
        str(animation),
        mode=IngestMode.FULL,
        transcript_mode=TranscriptMode.AUTO,
        max_duration_s=30,
    )
    assert cached.cache_hit is True
    assert cached.video_id == ingested.video_id
    assert animation.is_file()


def test_fixture_generator_refuses_an_implicit_overwrite(tmp_path: Path) -> None:
    from tests.fixtures.generate_fixture import generate

    output = tmp_path / "already-there.mp4"
    output.write_bytes(b"owned by caller")
    with pytest.raises(FileExistsError, match="--force"):
        generate(
            output,
            ffmpeg=shutil.which("ffmpeg") or "ffmpeg",
            force=False,
        )
    assert output.read_bytes() == b"owned by caller"


def test_sidecar_is_named_for_automatic_local_discovery() -> None:
    expected = VIDEO_PATH.with_name(f"{VIDEO_PATH.stem}.en.vtt")
    assert expected.is_file()
    assert os.path.commonpath((VIDEO_PATH, expected)) == str(FIXTURE_DIR.resolve())
