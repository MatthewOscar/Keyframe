from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from video_context_mcp.errors import ExtractionError
from video_context_mcp.vision import (
    BoundingBox,
    OCRLine,
    OCRResult,
    SampledFrame,
    StableRun,
    analyze_stable_run,
    auto_crop_text_region,
    check_parse,
    classify_visual,
    encode_image,
    extract_ocr,
    group_stable_runs,
    guess_language,
    is_dark_screen,
    preprocess_for_ocr,
    sample_frames,
)


def _ocr(text: str, *, confidence: float = 0.9) -> OCRResult:
    lines = tuple(
        OCRLine(
            text=line.strip(),
            indent_spaces=len(line) - len(line.lstrip()),
            confidence=confidence,
            box=BoundingBox(10, 10 + index * 20, max(10, len(line) * 8), 16),
        )
        for index, line in enumerate(text.splitlines())
        if line.strip()
    )
    return OCRResult(text, lines, confidence if lines else 0.0, 640, 360)


def _tsv_bytes(columns: dict[str, list[object]]) -> bytes:
    headers = tuple(columns)
    row_count = len(columns[headers[0]])
    rows = ["\t".join(headers)]
    rows.extend(
        "\t".join(str(columns[header][index]) for header in headers) for index in range(row_count)
    )
    return ("\n".join(rows) + "\n").encode()


def test_groups_adjacent_hashes_before_filtering_for_stability(tmp_path: Path) -> None:
    frames = [
        SampledFrame(0.0, tmp_path / "0.jpg", "0000000000000000"),
        SampledFrame(1.0, tmp_path / "1.jpg", "0000000000000003"),
        SampledFrame(2.0, tmp_path / "2.jpg", "ffffffffffffffff"),
        SampledFrame(3.0, tmp_path / "3.jpg", "fffffffffffffffc"),
        SampledFrame(4.0, tmp_path / "4.jpg", "aaaaaaaaaaaaaaaa"),
    ]

    runs = group_stable_runs(frames, distance_threshold=2, min_stable_seconds=2.0)

    assert [tuple(frame.timestamp_s for frame in run.frames) for run in runs] == [
        (0.0, 1.0),
        (2.0, 3.0),
    ]
    assert [run.representative.timestamp_s for run in runs] == [1.0, 3.0]
    assert [run.stable_seconds for run in runs] == [2.0, 2.0]
    assert [run.end_s for run in runs] == [2.0, 4.0]


def test_stability_grouping_rejects_out_of_order_frames(tmp_path: Path) -> None:
    frames = [
        SampledFrame(1.0, tmp_path / "1.jpg", "0" * 16),
        SampledFrame(0.0, tmp_path / "0.jpg", "0" * 16),
    ]
    with pytest.raises(ValueError, match="ordered"):
        group_stable_runs(frames)


def test_sample_frames_captures_ffmpeg_and_hashes_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"fixture")
    observed: dict[str, object] = {"commands": []}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands = observed["commands"]
        assert isinstance(commands, list)
        commands.append(command)
        if command[0] == "ffprobe":
            observed["ffprobe_kwargs"] = kwargs
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"streams": [{"duration": "1.0"}], "format": {}}),
                "",
            )
        observed["ffmpeg_command"] = command
        observed["ffmpeg_kwargs"] = kwargs
        pattern = Path(command[-1])
        Image.new("RGB", (64, 36), "white").save(str(pattern).replace("%08d", "00000000"))
        Image.new("RGB", (64, 36), "black").save(str(pattern).replace("%08d", "00000001"))
        stderr = "\n".join(
            (
                "[Parsed_showinfo_2] n: 0 pts: 14 pts_time:7 duration:1 duration_time:0.5",
                "[Parsed_showinfo_2] n: 1 pts: 15 pts_time:7.5 duration:1 duration_time:0.5",
            )
        )
        return subprocess.CompletedProcess(command, 0, "", stderr)

    monkeypatch.setattr("video_context_mcp.vision.subprocess.run", fake_run)

    frames = sample_frames(video, tmp_path / "work", fps=2.0)

    assert [frame.timestamp_s for frame in frames] == [7.0, 7.5]
    assert all(len(frame.phash) == 16 for frame in frames)
    ffmpeg_kwargs = observed["ffmpeg_kwargs"]
    assert isinstance(ffmpeg_kwargs, dict)
    assert ffmpeg_kwargs["stdin"] is subprocess.DEVNULL
    assert ffmpeg_kwargs["capture_output"] is True
    assert ffmpeg_kwargs["text"] is True
    assert ffmpeg_kwargs["check"] is False
    assert ffmpeg_kwargs["timeout"] == 60.0
    command = observed["ffmpeg_command"]
    assert isinstance(command, list)
    assert "-copyts" in command
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    filters = next(
        value for value in command if isinstance(value, str) and value.startswith("fps=2,")
    )
    assert "scale=" in filters
    assert filters.endswith(",showinfo")


def test_sample_frames_surfaces_ffmpeg_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"fixture")

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                '{"streams": [{"duration": "2"}]}',
                "",
            )
        return subprocess.CompletedProcess(command, 1, "", "invalid data found")

    monkeypatch.setattr("video_context_mcp.vision.subprocess.run", fake_run)
    with pytest.raises(ExtractionError, match="invalid data found"):
        sample_frames(video, tmp_path / "work")


def test_sample_frames_surfaces_bounded_ffmpeg_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"fixture")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                '{"streams": [{"duration": "240"}]}',
                "",
            )
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("video_context_mcp.vision.subprocess.run", fake_run)
    with pytest.raises(ExtractionError, match=r"timed out after 210s.*240\.0s video"):
        sample_frames(video, tmp_path / "work")


@pytest.mark.e2e
def test_sample_frames_preserves_nonzero_source_presentation_timestamps(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg and FFprobe are required")
    video = tmp_path / "nonzero-start.mp4"
    created = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x36:r=10:d=2",
            "-vf",
            "setpts=PTS+5/TB",
            "-copyts",
            "-c:v",
            "mpeg4",
            "-y",
            str(video),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert created.returncode == 0, created.stderr

    frames = sample_frames(video, tmp_path / "work")

    assert [frame.timestamp_s for frame in frames] == pytest.approx([5.0, 6.0])


def test_preprocess_inverts_dark_screens_and_upscales() -> None:
    image = Image.new("L", (80, 30), 5)
    ImageDraw.Draw(image).rectangle((20, 8, 55, 20), fill=245)

    assert is_dark_screen(image) is True
    processed = preprocess_for_ocr(image, threshold_method="otsu")

    assert processed.size == (160, 60)
    pixels = np.asarray(processed)
    assert pixels[2, 2] == 255
    assert pixels[30, 70] == 0


def test_extract_ocr_reconstructs_indentation_from_word_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Coordinates are from the 2x OCR image. Median source glyph width is 4px;
    # the second line starts 16 source pixels later, which reconstructs 4 spaces.
    data: dict[str, list[object]] = {
        "text": ["def", "f():", "return", "1"],
        "conf": ["96", "94", "92", "90"],
        "left": [20, 52, 52, 108],
        "top": [20, 20, 60, 60],
        "width": [24, 32, 48, 8],
        "height": [20, 20, 20, 20],
        "page_num": [1, 1, 1, 1],
        "block_num": [1, 1, 1, 1],
        "par_num": [1, 1, 1, 1],
        "line_num": [1, 1, 2, 2],
    }
    observed: dict[str, object] = {}

    def fake_image_to_data(*_args: object, **kwargs: object) -> bytes:
        observed.update(kwargs)
        return _tsv_bytes(data)

    monkeypatch.setattr("video_context_mcp.vision.pytesseract.image_to_data", fake_image_to_data)

    result = extract_ocr(Image.new("RGB", (100, 60), "white"))

    assert result.text == "def f():\n    return 1"
    assert result.lines[1].indent_spaces == 4
    assert result.lines[0].box == BoundingBox(10, 10, 32, 10)
    assert result.confidence == pytest.approx(0.93)
    assert all(0.0 <= line.confidence <= 1.0 for line in result.lines)
    assert observed["timeout"] == 30.0
    assert observed["output_type"] == "bytes"


def test_extract_ocr_replaces_invalid_utf8_in_untrusted_tsv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data: dict[str, list[object]] = {
        "text": ["slugify"],
        "conf": ["90"],
        "left": [20],
        "top": [20],
        "width": [56],
        "height": [20],
        "page_num": [1],
        "block_num": [1],
        "par_num": [1],
        "line_num": [1],
    }
    malformed = _tsv_bytes(data).replace(b"slugify", b"slug\x89ify")
    monkeypatch.setattr(
        "video_context_mcp.vision.pytesseract.image_to_data",
        lambda *_args, **_kwargs: malformed,
    )

    result = extract_ocr(Image.new("RGB", (100, 60), "white"))

    assert result.text == "slug\ufffdify"


def test_extract_ocr_surfaces_tesseract_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr("video_context_mcp.vision.pytesseract.image_to_data", timeout)

    with pytest.raises(ExtractionError, match=r"timed out after 2s.*one frame"):
        extract_ocr(Image.new("RGB", (100, 60), "white"), timeout_s=2)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("def greet(name):\n    return f'Hi {name}'", "python"),
        ('{"name": "Keyframe", "tools": 6}', "json"),
        ("const total = values.reduce((a, b) => a + b, 0);", "javascript"),
        ("interface Frame { timestamp: number }", "typescript"),
        ("ordinary prose with no syntax", None),
    ],
)
def test_language_guess_is_conservative(text: str, expected: str | None) -> None:
    assert guess_language(text) == expected


def test_parse_checks_python_json_and_indeterminate_languages() -> None:
    assert check_parse("def ok():\n    return 1", "python") is True
    assert check_parse("def broken(:\n    pass", "python") is False
    assert check_parse('{"valid": true}', "json") is True
    assert check_parse('{"invalid": }', "json") is False
    assert check_parse("interface Frame { at: number }", "typescript") is None
    assert check_parse("plain words", None) is None


def test_javascript_parse_check_uses_captured_node_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("video_context_mcp.vision.subprocess.run", fake_run)

    assert check_parse("const answer = 42;", "javascript") is True
    assert observed["command"][1] == "--check"
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["check"] is False
    assert observed["stdin"] is subprocess.DEVNULL


def test_javascript_parse_check_reports_missing_node(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr("video_context_mcp.vision.subprocess.run", missing)
    with pytest.raises(ExtractionError, match="Node executable"):
        check_parse("const answer = 42;", "javascript", node_binary="missing-node")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$ uv run pytest\n$ echo done", "terminal"),
        ("def add(a, b):\n    result = a + b\n    return result", "code"),
        ("Keyframe Architecture\nLocal video retrieval\nSix MCP tools", "slide"),
        ("video -> sampler -> OCR -> index", "diagram"),
        ("A single ordinary sentence.", "other"),
    ],
)
def test_visual_classification_is_honest(text: str, expected: str) -> None:
    kind, confidence = classify_visual(_ocr(text))
    assert kind == expected
    assert 0.0 <= confidence <= 1.0


def test_auto_crop_uses_padded_text_union() -> None:
    image = Image.new("RGB", (400, 300), "white")
    ocr = OCRResult(
        "first\nsecond",
        (
            OCRLine("first", 0, 0.9, BoundingBox(100, 50, 80, 20)),
            OCRLine("second", 0, 0.9, BoundingBox(110, 100, 100, 20)),
        ),
        0.9,
        400,
        300,
    )

    cropped, box = auto_crop_text_region(image, ocr)

    assert box == BoundingBox(88, 38, 134, 94)
    assert cropped.size == (134, 94)


def test_auto_crop_without_ocr_returns_full_frame() -> None:
    image = Image.new("RGB", (120, 80), "white")
    cropped, box = auto_crop_text_region(image, OCRResult("", (), 0.0, 120, 80))
    assert cropped.size == image.size
    assert box == BoundingBox(0, 0, 120, 80)


def test_encoded_images_are_deterministic_and_bounded() -> None:
    rng = np.random.default_rng(7)
    image = Image.fromarray(rng.integers(0, 256, (900, 1_800, 3), dtype=np.uint8), "RGB")

    first = encode_image(image, max_edge=1_000, max_bytes=120_000)
    second = encode_image(image, max_edge=1_000, max_bytes=120_000)

    assert max(first.width, first.height) <= 1_000
    assert len(first.data) <= 120_000
    assert first.mime_type == "image/jpeg"
    assert first == second


def test_analyze_stable_run_preserves_honest_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame_path = tmp_path / "frame.jpg"
    Image.new("RGB", (320, 180), "white").save(frame_path)
    frame = SampledFrame(1.0, frame_path, "0" * 16)
    run = StableRun(0.0, 2.0, 2.0, (frame,), frame)
    ocr = _ocr("def broken(:\n    return")
    monkeypatch.setattr("video_context_mcp.vision.extract_ocr", lambda *_a, **_k: ocr)
    monkeypatch.setattr("video_context_mcp.vision.classify_visual", lambda *_a, **_k: ("code", 0.8))

    moment = analyze_stable_run(run)

    assert moment.kind == "code"
    assert moment.language == "python"
    assert moment.parses is False
    assert moment.frame_path == frame_path
