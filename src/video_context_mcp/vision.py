"""Deterministic frame sampling, OCR, and visual-moment extraction.

This module deliberately contains no cache or MCP concerns.  Callers provide an
ingestion work directory, persist the returned dataclasses as appropriate, and
remove the work directory when an ingestion is complete.
"""

from __future__ import annotations

import ast
import io
import json
import math
import os
import re
import shlex
import statistics
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal, cast

import cv2
import imagehash
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageOps

from video_context_mcp.constants import (
    FRAME_SAMPLE_FPS,
    GIF_PROBE_MAX_SAMPLES,
    GIF_PROBE_SAMPLE_FPS,
    MAX_ANALYSIS_FRAME_EDGE,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_EDGE,
    MIN_STABLE_SECONDS,
    PHASH_DISTANCE_THRESHOLD,
    VISUAL_PROBE_CHAPTER_CANDIDATES,
    VISUAL_PROBE_MAX_EDGE,
    VISUAL_PROBE_MAX_MOMENTS,
    VISUAL_PROBE_UNIFORM_CANDIDATES,
)
from video_context_mcp.errors import ExtractionError

VisualKind = Literal["code", "terminal", "slide", "diagram", "other"]
Language = Literal["python", "json", "javascript", "typescript"]
ThresholdMethod = Literal["auto", "adaptive", "otsu"]
ProgressCallback = Callable[[str, float], None]

_FFPROBE_TIMEOUT_S = 15.0
_FRAME_SAMPLING_MIN_TIMEOUT_S = 60.0
_FRAME_SAMPLING_MAX_TIMEOUT_S = 1_800.0
_TESSERACT_TIMEOUT_S = 30.0
_PROBE_FRAME_TIMEOUT_S = 30.0
_DEFAULT_OCR_WORKERS = max(1, min(2, os.cpu_count() or 1))
_SHOWINFO_TIMESTAMP_RE = re.compile(
    r"\bn:\s*(?P<index>\d+)\s+pts:\s*\S+\s+pts_time:"
    r"(?P<timestamp>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\b"
)


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """A pixel box in source-image coordinates."""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


@dataclass(frozen=True, slots=True)
class SampledFrame:
    """A sampled video frame and its 64-bit perceptual hash."""

    timestamp_s: float
    path: Path
    phash: str


@dataclass(frozen=True, slots=True)
class StableRun:
    """Adjacent, perceptually similar frames stable for a time interval."""

    start_s: float
    end_s: float
    stable_seconds: float
    frames: tuple[SampledFrame, ...]
    representative: SampledFrame


@dataclass(frozen=True, slots=True)
class OCRLine:
    """One OCR line, with indentation separate from its unindented text."""

    text: str
    indent_spaces: int
    confidence: float
    box: BoundingBox


@dataclass(frozen=True, slots=True)
class OCRResult:
    """OCR text and line geometry expressed in source-image coordinates."""

    text: str
    lines: tuple[OCRLine, ...]
    confidence: float
    source_width: int
    source_height: int


@dataclass(frozen=True, slots=True)
class VisualMoment:
    """A stable representative frame plus its deterministic visual evidence."""

    timestamp_s: float
    start_s: float
    end_s: float
    stable_seconds: float
    frame_path: Path
    kind: VisualKind
    kind_confidence: float
    ocr: OCRResult
    language: Language | None
    parses: bool | None
    crop_box: BoundingBox


@dataclass(frozen=True, slots=True)
class EncodedImage:
    """A bounded JPEG suitable for an MCP image content block."""

    data: bytes
    mime_type: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class VisualProbePlan:
    """Deterministic chapter-priority and uniform timestamps for sparse scouting."""

    chapter_timestamps_s: tuple[float, ...]
    uniform_timestamps_s: tuple[float, ...]

    @property
    def candidate_count(self) -> int:
        return len(self.chapter_timestamps_s) + len(self.uniform_timestamps_s)


@dataclass(frozen=True, slots=True)
class _OCRWord:
    text: str
    confidence: float
    box: BoundingBox


def _open_image(image: Image.Image | Path) -> Image.Image:
    if isinstance(image, Image.Image):
        return image.copy()
    try:
        with Image.open(image) as opened:
            return ImageOps.exif_transpose(opened).copy()
    except (OSError, ValueError) as exc:
        raise ExtractionError(f"Could not read image frame {image}: {exc}") from exc


def perceptual_hash(image: Image.Image | Path) -> str:
    """Return a deterministic 64-bit pHash as sixteen lowercase hex digits."""

    loaded = _open_image(image)
    try:
        return str(imagehash.phash(loaded.convert("RGB"), hash_size=8))
    except (OSError, ValueError) as exc:
        raise ExtractionError(f"Could not hash image frame: {exc}") from exc


def phash_distance(left: str, right: str) -> int:
    """Return Hamming distance between two equal-width hexadecimal hashes."""

    if len(left) != len(right) or not left:
        raise ValueError("Perceptual hashes must be non-empty and have equal width")
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError as exc:
        raise ValueError("Perceptual hashes must be hexadecimal") from exc


def _probe_video_duration(video_path: Path, ffprobe_binary: str) -> float:
    """Return a bounded-time native duration probe for sampling timeout policy."""

    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=duration:format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_FFPROBE_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise ExtractionError(
            f"FFprobe executable '{ffprobe_binary}' was not found; install FFmpeg and retry"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(
            f"FFprobe timed out after {_FFPROBE_TIMEOUT_S:.0f}s while inspecting the video"
        ) from exc
    except OSError as exc:
        raise ExtractionError(f"Could not start FFprobe: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise ExtractionError(f"FFprobe could not determine video duration: {detail[-2_000:]}")
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        candidates = [stream.get("duration") for stream in streams if isinstance(stream, dict)]
        format_info = payload.get("format")
        if isinstance(format_info, dict):
            candidates.append(format_info.get("duration"))
        duration: float | None = None
        for candidate in candidates:
            if candidate is None or candidate == "N/A":
                continue
            value = float(str(candidate))
            if math.isfinite(value) and value > 0:
                duration = value
                break
        if duration is None:
            raise ValueError("duration was unavailable")
    except (AttributeError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExtractionError(
            "FFprobe did not return a positive video duration; verify that the file has a valid "
            "visual stream"
        ) from exc
    return duration


def _frame_sampling_timeout(duration_s: float) -> float:
    """Allow decode time proportional to media duration without an unbounded wait."""

    return min(
        _FRAME_SAMPLING_MAX_TIMEOUT_S,
        max(_FRAME_SAMPLING_MIN_TIMEOUT_S, 30.0 + duration_s * 0.75),
    )


def _showinfo_timestamps(stderr: str, expected_count: int) -> list[float]:
    """Parse the presentation timestamps emitted after FFmpeg's FPS filter."""

    by_index: dict[int, float] = {}
    for match in _SHOWINFO_TIMESTAMP_RE.finditer(stderr):
        index = int(match.group("index"))
        timestamp = float(match.group("timestamp"))
        if index in by_index or not math.isfinite(timestamp):
            raise ExtractionError("FFmpeg returned malformed frame presentation timestamps")
        # Public tool timestamps are non-negative. Preserve positive non-zero source
        # starts exactly while normalizing unusual negative edit-list preroll to zero.
        by_index[index] = max(0.0, timestamp)
    if set(by_index) != set(range(expected_count)):
        raise ExtractionError(
            "FFmpeg did not report a presentation timestamp for every sampled frame; "
            "retry with a supported FFmpeg build"
        )
    return [by_index[index] for index in range(expected_count)]


def sample_frames(
    video_path: Path,
    work_dir: Path,
    *,
    fps: float = FRAME_SAMPLE_FPS,
    ffmpeg_binary: str = "ffmpeg",
    ffprobe_binary: str = "ffprobe",
    max_edge: int = MAX_ANALYSIS_FRAME_EDGE,
) -> list[SampledFrame]:
    """Sample a local video with FFmpeg and hash each resulting JPEG.

    FFmpeg output is isolated in a fresh directory beneath ``work_dir`` so a
    failed or repeated run can never mistake stale frames for new output.
    """

    if fps <= 0 or not math.isfinite(fps):
        raise ValueError("fps must be a positive finite number")
    if max_edge < 64:
        raise ValueError("max_edge must be at least 64 pixels")
    if not video_path.is_file():
        raise ExtractionError(f"Video file does not exist or is not a file: {video_path}")

    work_dir.mkdir(parents=True, exist_ok=True)
    frame_dir = Path(tempfile.mkdtemp(prefix="sampled-frames-", dir=work_dir))
    pattern = frame_dir / "frame-%08d.jpg"
    duration_s = _probe_video_duration(video_path, ffprobe_binary)
    timeout_s = _frame_sampling_timeout(duration_s)
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostats",
        "-nostdin",
        "-copyts",
        *(["-ignore_loop", "1"] if video_path.suffix.lower() == ".gif" else []),
        "-i",
        str(video_path),
        "-vf",
        (
            f"fps={fps:.12g},"
            f"scale=w='min(iw,{max_edge})':h='min(ih,{max_edge})':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2,showinfo"
        ),
        "-fps_mode",
        "passthrough",
        "-q:v",
        "2",
        "-start_number",
        "0",
        "-y",
        str(pattern),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise ExtractionError(
            f"FFmpeg executable '{ffmpeg_binary}' was not found; install FFmpeg and retry"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(
            f"FFmpeg frame sampling timed out after {timeout_s:.0f}s for a "
            f"{duration_s:.1f}s video; retry on a faster machine or ingest a shorter source"
        ) from exc
    except OSError as exc:
        raise ExtractionError(f"Could not start FFmpeg: {exc}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise ExtractionError(f"FFmpeg frame sampling failed: {detail[-2_000:]}")

    paths = sorted(frame_dir.glob("frame-*.jpg"))
    if not paths:
        raise ExtractionError(
            "FFmpeg completed without producing frames; verify that the video has a visual stream"
        )

    timestamps = _showinfo_timestamps(completed.stderr, len(paths))
    sampled: list[SampledFrame] = []
    for timestamp_s, path in zip(timestamps, paths, strict=True):
        sampled.append(
            SampledFrame(
                timestamp_s=timestamp_s,
                path=path,
                phash=perceptual_hash(path),
            )
        )
    return sampled


def plan_visual_probe(
    duration_s: float,
    chapters: Sequence[tuple[float, float]],
    *,
    chapter_candidates: int = VISUAL_PROBE_CHAPTER_CANDIDATES,
    uniform_candidates: int = VISUAL_PROBE_UNIFORM_CANDIDATES,
) -> VisualProbePlan:
    """Plan bounded timestamps that cover chapters and the whole timeline."""

    if duration_s <= 0 or not math.isfinite(duration_s):
        raise ValueError("duration_s must be a positive finite number")
    if chapter_candidates < 0 or uniform_candidates < 1:
        raise ValueError("probe candidate counts must be non-negative and include uniform frames")

    normalized_chapters: list[tuple[float, float]] = []
    for start_s, end_s in chapters:
        if not math.isfinite(start_s) or not math.isfinite(end_s) or end_s < start_s:
            continue
        start = min(duration_s, max(0.0, start_s))
        end = min(duration_s, max(start, end_s))
        normalized_chapters.append((start, end))
    normalized_chapters.sort()

    all_chapter_times = _deduplicate_timestamps(
        [
            _clamp_probe_timestamp(start_s + (end_s - start_s) * 0.5, duration_s)
            for start_s, end_s in normalized_chapters
        ]
    )
    selected_count = min(chapter_candidates, len(all_chapter_times))
    chapter_times = [
        all_chapter_times[index]
        for index in _evenly_spaced_indexes(len(all_chapter_times), selected_count)
    ]

    duration_scaled_uniform_count = max(4, math.ceil(duration_s / 30.0))
    uniform_count = min(uniform_candidates, duration_scaled_uniform_count)
    uniform_times = [
        _clamp_probe_timestamp((index + 0.5) * duration_s / uniform_count, duration_s)
        for index in range(uniform_count)
    ]

    unique_chapters = chapter_times
    unique_uniform = _deduplicate_timestamps(
        [
            value
            for value in uniform_times
            if all(abs(value - chapter) > 0.05 for chapter in unique_chapters)
        ]
    )
    return VisualProbePlan(tuple(unique_chapters), tuple(unique_uniform))


def _evenly_spaced_indexes(length: int, count: int) -> tuple[int, ...]:
    if count <= 0 or length <= 0:
        return ()
    if count >= length:
        return tuple(range(length))
    if count == 1:
        return (length // 2,)
    return tuple(round(index * (length - 1) / (count - 1)) for index in range(count))


def _clamp_probe_timestamp(timestamp_s: float, duration_s: float) -> float:
    latest = max(0.0, duration_s - min(0.05, duration_s * 0.01))
    return min(latest, max(0.0, timestamp_s))


def _deduplicate_timestamps(values: Sequence[float]) -> list[float]:
    deduplicated: list[float] = []
    for value in sorted(values):
        if not deduplicated or abs(value - deduplicated[-1]) > 0.05:
            deduplicated.append(value)
    return deduplicated


def sample_frames_at_timestamps(
    video_path: Path,
    work_dir: Path,
    timestamps_s: Sequence[float],
    *,
    max_edge: int = VISUAL_PROBE_MAX_EDGE,
    ffmpeg_binary: str = "ffmpeg",
    workers: int = _DEFAULT_OCR_WORKERS,
) -> list[SampledFrame]:
    """Seek to a bounded list of timestamps without decoding the full timeline."""

    if not video_path.is_file():
        raise ExtractionError(f"Video file does not exist or is not a file: {video_path}")
    if max_edge < 64:
        raise ValueError("max_edge must be at least 64 pixels")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if any(value < 0 or not math.isfinite(value) for value in timestamps_s):
        raise ValueError("probe timestamps must be finite non-negative numbers")
    normalized = _deduplicate_timestamps(timestamps_s)
    if not normalized:
        return []

    work_dir.mkdir(parents=True, exist_ok=True)
    frame_dir = Path(tempfile.mkdtemp(prefix="probe-frames-", dir=work_dir))

    def sample(index: int, requested_t: float) -> SampledFrame:
        output_path = frame_dir / f"frame-{index:05d}.jpg"
        command = [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-nostdin",
            "-ss",
            f"{requested_t:.6f}",
            "-copyts",
            *(["-ignore_loop", "1"] if video_path.suffix.lower() == ".gif" else []),
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            "1",
            "-vf",
            (
                f"scale=w='min(iw,{max_edge})':h='min(ih,{max_edge})':"
                "force_original_aspect_ratio=decrease:force_divisible_by=2,showinfo"
            ),
            "-fps_mode",
            "passthrough",
            "-q:v",
            "2",
            "-update",
            "1",
            "-y",
            str(output_path),
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_PROBE_FRAME_TIMEOUT_S,
            )
        except FileNotFoundError as exc:
            raise ExtractionError(
                f"FFmpeg executable '{ffmpeg_binary}' was not found; install FFmpeg and retry"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ExtractionError(
                f"FFmpeg timed out after {_PROBE_FRAME_TIMEOUT_S:.0f}s while probing frame "
                f"{requested_t:.3f}s"
            ) from exc
        except OSError as exc:
            raise ExtractionError(f"Could not start FFmpeg visual probe: {exc}") from exc
        if completed.returncode != 0 or not output_path.is_file():
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise ExtractionError(
                f"FFmpeg could not extract probe frame at {requested_t:.3f}s: {detail[-2_000:]}"
            )
        actual_t = _first_showinfo_timestamp(completed.stderr, fallback=requested_t)
        return SampledFrame(actual_t, output_path, perceptual_hash(output_path))

    worker_count = min(workers, len(normalized))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="keyframe-probe",
    ) as executor:
        futures: dict[Future[SampledFrame], int] = {
            executor.submit(sample, index, timestamp_s): index
            for index, timestamp_s in enumerate(normalized)
        }
        ordered: dict[int, SampledFrame] = {}
        try:
            for future in as_completed(futures):
                ordered[futures[future]] = future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return [ordered[index] for index in range(len(normalized))]


def _first_showinfo_timestamp(stderr: str, *, fallback: float) -> float:
    for match in _SHOWINFO_TIMESTAMP_RE.finditer(stderr):
        timestamp = float(match.group("timestamp"))
        if int(match.group("index")) == 0 and math.isfinite(timestamp):
            return max(0.0, timestamp)
    return fallback


def _sample_interval(frames: Sequence[SampledFrame]) -> float:
    differences = [
        current.timestamp_s - previous.timestamp_s
        for previous, current in pairwise(frames)
        if current.timestamp_s > previous.timestamp_s
    ]
    return statistics.median(differences) if differences else 1.0 / FRAME_SAMPLE_FPS


def group_stable_runs(
    frames: Sequence[SampledFrame],
    *,
    distance_threshold: int = PHASH_DISTANCE_THRESHOLD,
    min_stable_seconds: float = MIN_STABLE_SECONDS,
) -> list[StableRun]:
    """Group adjacent frames first, then retain only sufficiently stable runs."""

    if distance_threshold < 0:
        raise ValueError("distance_threshold must be non-negative")
    if min_stable_seconds <= 0 or not math.isfinite(min_stable_seconds):
        raise ValueError("min_stable_seconds must be a positive finite number")
    if not frames:
        return []
    if any(current.timestamp_s < previous.timestamp_s for previous, current in pairwise(frames)):
        raise ValueError("frames must be ordered by non-decreasing timestamp")

    interval = _sample_interval(frames)
    groups: list[list[SampledFrame]] = [[frames[0]]]
    for frame in frames[1:]:
        previous = groups[-1][-1]
        time_gap = frame.timestamp_s - previous.timestamp_s
        is_adjacent = time_gap <= interval * 1.5 + 1e-9
        is_similar = phash_distance(previous.phash, frame.phash) <= distance_threshold
        if is_adjacent and is_similar:
            groups[-1].append(frame)
        else:
            groups.append([frame])

    runs: list[StableRun] = []
    for group in groups:
        stable_seconds = group[-1].timestamp_s - group[0].timestamp_s + interval
        if stable_seconds + 1e-9 < min_stable_seconds:
            continue
        runs.append(
            StableRun(
                start_s=group[0].timestamp_s,
                end_s=group[-1].timestamp_s + interval,
                stable_seconds=stable_seconds,
                frames=tuple(group),
                representative=group[len(group) // 2],
            )
        )
    return runs


def is_dark_screen(image: Image.Image | Path) -> bool:
    """Conservatively identify a dark screen from grayscale luminance."""

    gray = np.asarray(_open_image(image).convert("L"), dtype=np.uint8)
    return bool(np.median(gray) < 110 and np.mean(gray) < 125)


def preprocess_for_ocr(
    image: Image.Image | Path,
    *,
    threshold_method: ThresholdMethod = "auto",
) -> Image.Image:
    """Normalize a frame into a high-resolution black-on-white OCR image."""

    if threshold_method not in {"auto", "adaptive", "otsu"}:
        raise ValueError(f"Unsupported threshold method: {threshold_method}")
    loaded = _open_image(image).convert("L")
    gray: NDArray[np.uint8] = np.asarray(loaded, dtype=np.uint8)
    if is_dark_screen(loaded):
        gray = cast(NDArray[np.uint8], cv2.bitwise_not(gray))

    upscaled = cast(
        NDArray[np.uint8],
        cv2.resize(
            gray,
            None,
            fx=2.0,
            fy=2.0,
            interpolation=cv2.INTER_CUBIC,
        ),
    )
    blurred = cast(NDArray[np.uint8], cv2.GaussianBlur(upscaled, (3, 3), 0))
    selected_method = threshold_method
    if selected_method == "auto":
        # Unevenly lit scenes benefit from a local threshold; screen captures
        # with a compact histogram preserve glyphs better under Otsu.
        selected_method = "adaptive" if float(np.std(blurred)) >= 55.0 else "otsu"

    if selected_method == "adaptive":
        binary = cast(
            NDArray[np.uint8],
            cv2.adaptiveThreshold(
                blurred,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                31,
                11,
            ),
        )
    else:
        _, thresholded = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        binary = cast(NDArray[np.uint8], thresholded)
    return Image.fromarray(binary, mode="L")


def _integer_at(values: Sequence[object], index: int, field: str) -> int:
    try:
        return int(float(str(values[index])))
    except (IndexError, TypeError, ValueError) as exc:
        raise ExtractionError(f"Tesseract returned an invalid '{field}' field") from exc


def _string_at(values: Sequence[object], index: int, field: str) -> str:
    try:
        return str(values[index])
    except IndexError as exc:
        raise ExtractionError(f"Tesseract returned an invalid '{field}' field") from exc


def _required_column(
    data: Mapping[str, Sequence[object]], field: str, expected_length: int
) -> Sequence[object]:
    values = data.get(field)
    if values is None or len(values) != expected_length:
        raise ExtractionError(f"Tesseract returned a malformed '{field}' column")
    return values


def _join_words(words: Sequence[_OCRWord], median_glyph_width: float) -> str:
    pieces: list[str] = []
    previous_right: int | None = None
    for word in words:
        if previous_right is not None:
            gap = max(0, word.box.left - previous_right)
            spaces = max(1, min(8, round(gap / max(median_glyph_width, 1.0))))
            pieces.append(" " * spaces)
        pieces.append(word.text)
        previous_right = word.box.right
    return "".join(pieces)


def _run_tesseract_tsv(
    image: Image.Image,
    *,
    tesseract_binary: str,
    tesseract_config: str,
    timeout_s: float,
) -> bytes:
    """Run Tesseract directly so all untrusted native output stays byte-oriented."""

    try:
        config_args = shlex.split(tesseract_config, posix=os.name != "nt")
    except ValueError as exc:
        raise ExtractionError(f"Invalid Tesseract configuration: {exc}") from exc
    encoded = io.BytesIO()
    try:
        image.save(encoded, format="PNG", optimize=False)
    except (OSError, ValueError) as exc:
        raise ExtractionError(f"Could not prepare a frame for Tesseract OCR: {exc}") from exc
    command = [tesseract_binary, "stdin", "stdout", *config_args, "tsv"]
    try:
        completed = subprocess.run(
            command,
            input=encoded.getvalue(),
            capture_output=True,
            check=False,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise ExtractionError(
            "Tesseract was not found; install Tesseract 5 and ensure it is on PATH"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(
            f"Tesseract OCR timed out after {timeout_s:.0f}s for one frame; "
            "retry with a shorter or lower-resolution source"
        ) from exc
    except OSError as exc:
        raise ExtractionError(f"Could not start Tesseract OCR: {exc}") from exc
    if completed.returncode != 0:
        detail_bytes = completed.stderr or completed.stdout
        detail = detail_bytes.decode("utf-8", errors="replace").strip()
        raise ExtractionError(
            f"Tesseract OCR failed: {(detail or 'no diagnostic output')[-2_000:]}"
        )
    return completed.stdout


def _parse_tesseract_tsv(payload: bytes) -> Mapping[str, Sequence[object]]:
    document = payload.decode("utf-8", errors="replace")
    lines = document.splitlines()
    if not lines:
        return {}
    headers = lines[0].split("\t")
    if not headers or any(not header for header in headers) or len(headers) != len(set(headers)):
        raise ExtractionError("Tesseract returned a malformed TSV header")
    columns: dict[str, list[object]] = {header: [] for header in headers}
    for line in lines[1:]:
        if not line:
            continue
        values = line.split("\t")
        if len(values) < len(headers):
            values.extend("" for _ in range(len(headers) - len(values)))
        elif len(values) > len(headers):
            values = [*values[: len(headers) - 1], "\t".join(values[len(headers) - 1 :])]
        for header, value in zip(headers, values, strict=True):
            columns[header].append(value)
    return columns


def extract_ocr(
    image: Image.Image | Path,
    *,
    tesseract_config: str = "--psm 6",
    threshold_method: ThresholdMethod = "auto",
    tesseract_binary: str = "tesseract",
    timeout_s: float = _TESSERACT_TIMEOUT_S,
) -> OCRResult:
    """Extract line-aware OCR with normalized confidence and indentation."""

    if timeout_s <= 0 or not math.isfinite(timeout_s):
        raise ValueError("timeout_s must be a positive finite number")
    source = _open_image(image)
    prepared = preprocess_for_ocr(source, threshold_method=threshold_method)
    payload = _run_tesseract_tsv(
        prepared,
        tesseract_binary=tesseract_binary,
        tesseract_config=tesseract_config,
        timeout_s=timeout_s,
    )
    data = _parse_tesseract_tsv(payload)
    text_values = data.get("text")
    if text_values is None:
        raise ExtractionError("Tesseract returned a malformed 'text' column")
    count = len(text_values)
    columns = {
        field: _required_column(data, field, count)
        for field in (
            "conf",
            "left",
            "top",
            "width",
            "height",
            "page_num",
            "block_num",
            "par_num",
            "line_num",
        )
    }

    scale = 2.0
    grouped: dict[tuple[int, int, int, int], list[_OCRWord]] = defaultdict(list)
    all_words: list[_OCRWord] = []
    for index in range(count):
        text = _string_at(text_values, index, "text").strip()
        if not text:
            continue
        confidence_raw = _integer_at(columns["conf"], index, "conf")
        if confidence_raw < 0:
            continue
        confidence = min(1.0, max(0.0, confidence_raw / 100.0))
        left = max(0, round(_integer_at(columns["left"], index, "left") / scale))
        top = max(0, round(_integer_at(columns["top"], index, "top") / scale))
        width = max(1, round(_integer_at(columns["width"], index, "width") / scale))
        height = max(1, round(_integer_at(columns["height"], index, "height") / scale))
        word = _OCRWord(text, confidence, BoundingBox(left, top, width, height))
        key = tuple(
            _integer_at(columns[field], index, field)
            for field in ("page_num", "block_num", "par_num", "line_num")
        )
        grouped[cast(tuple[int, int, int, int], key)].append(word)
        all_words.append(word)

    if not all_words:
        return OCRResult("", (), 0.0, source.width, source.height)

    glyph_widths = [
        word.box.width / max(1, len(word.text)) for word in all_words if word.box.width > 0
    ]
    median_glyph_width = statistics.median(glyph_widths) if glyph_widths else 8.0

    ordered_groups = sorted(
        grouped.values(),
        key=lambda words: (
            min(word.box.top for word in words),
            min(word.box.left for word in words),
        ),
    )
    base_left = min(min(word.box.left for word in words) for words in ordered_groups)
    lines: list[OCRLine] = []
    for words in ordered_groups:
        ordered_words = sorted(words, key=lambda word: word.box.left)
        left = min(word.box.left for word in ordered_words)
        top = min(word.box.top for word in ordered_words)
        right = max(word.box.right for word in ordered_words)
        bottom = max(word.box.bottom for word in ordered_words)
        indent_spaces = max(
            0,
            min(32, round((left - base_left) / max(median_glyph_width, 1.0))),
        )
        lines.append(
            OCRLine(
                text=_join_words(ordered_words, median_glyph_width),
                indent_spaces=indent_spaces,
                confidence=sum(word.confidence for word in ordered_words) / len(ordered_words),
                box=BoundingBox(left, top, right - left, bottom - top),
            )
        )

    reconstructed = "\n".join(" " * line.indent_spaces + line.text for line in lines)
    confidence = sum(word.confidence for word in all_words) / len(all_words)
    return OCRResult(reconstructed, tuple(lines), confidence, source.width, source.height)


_TERMINAL_PROMPT = re.compile(r"^(?:(?:[\w.-]+@)?[\w./~:-]+\s*)?[$#>%]\s+|^(?:>>>|\.\.\.)\s*")
_CODE_LEADER = re.compile(
    r"^\s*(?:async\s+def|def|class|from|import|function|const|let|var|if|elif|else|for|while|return|try|except|catch|with)\b"
)


def _image_has_diagram_lines(image: Image.Image) -> bool:
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    edges = cast(NDArray[np.uint8], cv2.Canny(gray, 70, 180))
    minimum_length = max(20, min(image.size) // 8)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=35,
        minLineLength=minimum_length,
        maxLineGap=8,
    )
    return lines is not None and len(lines) >= 6


def classify_visual(
    ocr: OCRResult,
    image: Image.Image | Path | None = None,
) -> tuple[VisualKind, float]:
    """Return a conservative heuristic kind and confidence in ``[0, 1]``."""

    nonempty = [line for line in ocr.text.splitlines() if line.strip()]
    stripped = [line.strip() for line in nonempty]
    terminal_hits = sum(bool(_TERMINAL_PROMPT.match(line)) for line in stripped)
    terminal_output = sum(
        any(
            token in line.lower()
            for token in ("command not found", "permission denied", "exit code")
        )
        for line in stripped
    )
    if terminal_hits >= 2 or (terminal_hits >= 1 and terminal_output >= 1):
        return "terminal", min(0.92, 0.68 + terminal_hits * 0.08)

    arrow_hits = sum(
        any(token in line for token in ("->", "=>", "→", "<-", "↔")) for line in stripped
    )
    if arrow_hits >= 1 and len(stripped) <= 12:
        return "diagram", min(0.82, 0.58 + arrow_hits * 0.08)

    language = guess_language(ocr.text)
    code_leaders = sum(bool(_CODE_LEADER.match(line)) for line in nonempty)
    punctuation_lines = sum(
        bool(re.search(r"(?:[{};]|==|!=|:=|\w+\s*=\s*[^=])", line)) for line in nonempty
    )
    indented_lines = sum(bool(line[:1].isspace()) for line in nonempty)
    code_score = code_leaders * 2 + punctuation_lines + min(indented_lines, 2)
    if language is not None and len(nonempty) >= 2 and code_score >= 4:
        return "code", min(0.94, 0.62 + code_score * 0.04)

    loaded: Image.Image | None = _open_image(image) if image is not None else None
    if loaded is not None and len(stripped) <= 10 and _image_has_diagram_lines(loaded):
        return "diagram", 0.56

    word_counts = [len(line.split()) for line in stripped]
    if (
        2 <= len(stripped) <= 8
        and word_counts
        and statistics.mean(word_counts) <= 8
        and ocr.confidence >= 0.55
    ):
        return "slide", 0.56
    return "other", 0.35


def guess_language(text: str) -> Language | None:
    """Conservatively infer one of the languages with a supported parse policy."""

    stripped = text.strip()
    if not stripped:
        return None
    try:
        decoded = json.loads(stripped)
        if isinstance(decoded, (dict, list)):
            return "json"
    except (json.JSONDecodeError, TypeError):
        pass

    if re.search(r"^\s*(?:interface|type|enum|namespace)\s+\w+", text, re.MULTILINE) or re.search(
        r"\b(?:const|let|function)\s+\w+\s*(?::\s*[A-Za-z_$][\w.$<>\[\]| ]*)",
        text,
    ):
        return "typescript"
    if re.search(
        r"^\s*(?:async\s+def|def|class|from\s+\S+\s+import|import\s+\w+|except\b|elif\b)",
        text,
        re.MULTILINE,
    ):
        return "python"
    if re.search(r"\b(?:function|const|let|var)\s+[$A-Za-z_]", text) or (
        "=>" in text and ";" in text
    ):
        return "javascript"
    return None


def check_parse(
    text: str,
    language: Language | None = None,
    *,
    node_binary: str = "node",
) -> bool | None:
    """Parse-check supported text; TypeScript and unknown text are indeterminate."""

    selected = language if language is not None else guess_language(text)
    if selected == "python":
        try:
            ast.parse(text)
        except (SyntaxError, ValueError, TypeError):
            return False
        return True
    if selected == "json":
        try:
            json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return False
        return True
    if selected in {None, "typescript"}:
        return None

    with tempfile.TemporaryDirectory(prefix="keyframe-node-check-") as temp_dir:
        script = Path(temp_dir) / "snippet.js"
        try:
            script.write_text(text, encoding="utf-8")
            completed = subprocess.run(
                [node_binary, "--check", str(script)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise ExtractionError(
                f"Node executable '{node_binary}' was not found; install Node 22+ to parse-check JavaScript"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ExtractionError("Node timed out while parse-checking JavaScript") from exc
        except OSError as exc:
            raise ExtractionError(
                f"Could not start Node for JavaScript parse checking: {exc}"
            ) from exc
    return completed.returncode == 0


def auto_crop_text_region(
    image: Image.Image | Path,
    ocr: OCRResult,
    *,
    padding_ratio: float = 0.03,
) -> tuple[Image.Image, BoundingBox]:
    """Crop to the union of OCR lines, padded and clamped to source bounds."""

    if padding_ratio < 0 or not math.isfinite(padding_ratio):
        raise ValueError("padding_ratio must be a non-negative finite number")
    loaded = _open_image(image)
    if not ocr.lines:
        full = BoundingBox(0, 0, loaded.width, loaded.height)
        return loaded, full

    left = min(line.box.left for line in ocr.lines)
    top = min(line.box.top for line in ocr.lines)
    right = max(line.box.right for line in ocr.lines)
    bottom = max(line.box.bottom for line in ocr.lines)
    padding = max(8, round(max(loaded.size) * padding_ratio))
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(loaded.width, right + padding)
    bottom = min(loaded.height, bottom + padding)
    if right <= left or bottom <= top:
        full = BoundingBox(0, 0, loaded.width, loaded.height)
        return loaded, full
    box = BoundingBox(left, top, right - left, bottom - top)
    return loaded.crop((box.left, box.top, box.right, box.bottom)), box


def _jpeg_ready(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image.copy()
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def encode_image(
    image: Image.Image | Path,
    *,
    max_edge: int = MAX_IMAGE_EDGE,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> EncodedImage:
    """Encode a deterministic JPEG below both edge and byte limits."""

    if max_edge <= 0 or max_bytes <= 0:
        raise ValueError("max_edge and max_bytes must be positive")
    working = _jpeg_ready(_open_image(image))
    if max(working.size) > max_edge:
        scale = max_edge / max(working.size)
        resized = (
            max(1, round(working.width * scale)),
            max(1, round(working.height * scale)),
        )
        working = working.resize(resized, Image.Resampling.LANCZOS)

    qualities = (90, 82, 74, 66, 58, 50, 42, 34, 26)
    while True:
        for quality in qualities:
            buffer = io.BytesIO()
            working.save(
                buffer,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=False,
                subsampling=2,
            )
            data = buffer.getvalue()
            if len(data) <= max_bytes:
                return EncodedImage(data, "image/jpeg", working.width, working.height)
        if max(working.size) <= 64:
            break
        new_size = (
            max(1, round(working.width * 0.8)),
            max(1, round(working.height * 0.8)),
        )
        working = working.resize(new_size, Image.Resampling.LANCZOS)
    raise ExtractionError(
        f"Could not encode image below {max_bytes} bytes without reducing it below 64 pixels"
    )


def analyze_stable_run(
    run: StableRun,
    *,
    node_binary: str = "node",
    tesseract_config: str = "--psm 6",
    tesseract_binary: str = "tesseract",
) -> VisualMoment:
    """Analyze one retained stable run without persisting any derived state."""

    image = _open_image(run.representative.path)
    ocr = extract_ocr(
        image,
        tesseract_config=tesseract_config,
        tesseract_binary=tesseract_binary,
    )
    kind, kind_confidence = classify_visual(ocr, image)
    language = guess_language(ocr.text) if kind in {"code", "terminal"} else None
    parses = check_parse(ocr.text, language, node_binary=node_binary) if language else None
    _, crop_box = auto_crop_text_region(image, ocr)
    return VisualMoment(
        timestamp_s=run.representative.timestamp_s,
        start_s=run.start_s,
        end_s=run.end_s,
        stable_seconds=run.stable_seconds,
        frame_path=run.representative.path,
        kind=kind,
        kind_confidence=kind_confidence,
        ocr=ocr,
        language=language,
        parses=parses,
        crop_box=crop_box,
    )


def _analyze_runs(
    runs: Sequence[StableRun],
    *,
    node_binary: str,
    tesseract_config: str,
    tesseract_binary: str,
    ocr_workers: int,
    analysis_message: str,
    progress: ProgressCallback | None,
) -> list[VisualMoment]:
    if not runs:
        return []
    if progress is not None:
        progress(analysis_message, 0.2)
    worker_count = min(ocr_workers, len(runs))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="keyframe-ocr",
    ) as executor:
        futures: dict[Future[VisualMoment], int] = {
            executor.submit(
                analyze_stable_run,
                run,
                node_binary=node_binary,
                tesseract_config=tesseract_config,
                tesseract_binary=tesseract_binary,
            ): index
            for index, run in enumerate(runs)
        }
        ordered: dict[int, VisualMoment] = {}
        try:
            for completed_count, future in enumerate(as_completed(futures), start=1):
                ordered[futures[future]] = future.result()
                if progress is not None:
                    progress(
                        analysis_message,
                        0.2 + (completed_count / len(runs)) * 0.8,
                    )
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return [ordered[index] for index in range(len(runs))]


def _select_visual_probe_frames(
    chapter_frames: Sequence[SampledFrame],
    uniform_frames: Sequence[SampledFrame],
    *,
    max_moments: int,
) -> list[SampledFrame]:
    """Keep chapter coverage, then add the most perceptually diverse candidates."""

    if max_moments < 1:
        raise ValueError("max_moments must be a positive integer")
    selected = list(chapter_frames[:max_moments])
    pool = list(uniform_frames)
    while pool and len(selected) < max_moments:
        if selected:
            best = max(
                pool,
                key=lambda candidate: (
                    min(phash_distance(candidate.phash, retained.phash) for retained in selected),
                    -candidate.timestamp_s,
                ),
            )
        else:
            best = pool[0]
        selected.append(best)
        pool.remove(best)
    return sorted(selected, key=lambda frame: frame.timestamp_s)


def _nearest_unique_frames(
    frames: Sequence[SampledFrame],
    timestamps_s: Sequence[float],
) -> list[SampledFrame]:
    if not frames:
        return []
    selected: list[SampledFrame] = []
    for timestamp_s in timestamps_s:
        nearest = min(
            frames,
            key=lambda frame: (abs(frame.timestamp_s - timestamp_s), frame.timestamp_s),
        )
        if nearest not in selected:
            selected.append(nearest)
    return selected


def extract_visual_probe(
    video_path: Path,
    work_dir: Path,
    *,
    chapter_timestamps_s: Sequence[float],
    uniform_timestamps_s: Sequence[float],
    max_moments: int = VISUAL_PROBE_MAX_MOMENTS,
    max_edge: int = VISUAL_PROBE_MAX_EDGE,
    ffmpeg_binary: str = "ffmpeg",
    ffprobe_binary: str = "ffprobe",
    node_binary: str = "node",
    tesseract_config: str = "--psm 6",
    tesseract_binary: str = "tesseract",
    ocr_workers: int = _DEFAULT_OCR_WORKERS,
    progress: ProgressCallback | None = None,
) -> list[VisualMoment]:
    """Extract a bounded visual scout without scanning the full timeline."""

    if not isinstance(max_moments, int) or isinstance(max_moments, bool) or max_moments < 1:
        raise ValueError("max_moments must be a positive integer")
    if not isinstance(ocr_workers, int) or isinstance(ocr_workers, bool) or ocr_workers < 1:
        raise ValueError("ocr_workers must be a positive integer")
    if progress is not None:
        progress("Sampling sparse visual probe", 0.0)
    if video_path.suffix.lower() == ".gif":
        duration_s = _probe_video_duration(video_path, ffprobe_binary)
        sample_fps = min(GIF_PROBE_SAMPLE_FPS, GIF_PROBE_MAX_SAMPLES / duration_s)
        decoded_frames = sample_frames(
            video_path,
            work_dir / "gif",
            fps=sample_fps,
            ffmpeg_binary=ffmpeg_binary,
            ffprobe_binary=ffprobe_binary,
            max_edge=max_edge,
        )
        chapter_frames = _nearest_unique_frames(decoded_frames, chapter_timestamps_s)
        chapter_paths = {frame.path for frame in chapter_frames}
        uniform_frames = [
            frame
            for frame in _nearest_unique_frames(decoded_frames, uniform_timestamps_s)
            if frame.path not in chapter_paths
        ]
    else:
        chapter_frames = sample_frames_at_timestamps(
            video_path,
            work_dir / "chapters",
            chapter_timestamps_s,
            max_edge=max_edge,
            ffmpeg_binary=ffmpeg_binary,
            workers=ocr_workers,
        )
        uniform_frames = sample_frames_at_timestamps(
            video_path,
            work_dir / "uniform",
            uniform_timestamps_s,
            max_edge=max_edge,
            ffmpeg_binary=ffmpeg_binary,
            workers=ocr_workers,
        )
    selected = _select_visual_probe_frames(
        chapter_frames,
        uniform_frames,
        max_moments=max_moments,
    )
    runs = [
        StableRun(
            start_s=frame.timestamp_s,
            end_s=frame.timestamp_s,
            stable_seconds=0.0,
            frames=(frame,),
            representative=frame,
        )
        for frame in selected
    ]
    moments = _analyze_runs(
        runs,
        node_binary=node_binary,
        tesseract_config=tesseract_config,
        tesseract_binary=tesseract_binary,
        ocr_workers=ocr_workers,
        analysis_message="Analyzing sparse probe frames",
        progress=progress,
    )
    if progress is not None:
        progress("Sparse visual probe complete", 1.0)
    return moments


def extract_visual_moments(
    video_path: Path,
    work_dir: Path,
    *,
    fps: float = FRAME_SAMPLE_FPS,
    max_edge: int = MAX_ANALYSIS_FRAME_EDGE,
    ffmpeg_binary: str = "ffmpeg",
    ffprobe_binary: str = "ffprobe",
    node_binary: str = "node",
    distance_threshold: int = PHASH_DISTANCE_THRESHOLD,
    min_stable_seconds: float = MIN_STABLE_SECONDS,
    tesseract_config: str = "--psm 6",
    tesseract_binary: str = "tesseract",
    ocr_workers: int = _DEFAULT_OCR_WORKERS,
    max_moments: int | None = None,
    progress: ProgressCallback | None = None,
) -> list[VisualMoment]:
    """Sample, stabilize, OCR, and classify a video into visual moments."""

    if not isinstance(ocr_workers, int) or isinstance(ocr_workers, bool) or ocr_workers < 1:
        raise ValueError("ocr_workers must be a positive integer")
    if max_moments is not None and (
        not isinstance(max_moments, int)
        or isinstance(max_moments, bool)
        or max_moments < 1
    ):
        raise ValueError("max_moments must be a positive integer or None")

    if progress is not None:
        progress("Sampling video frames", 0.0)
    frames = sample_frames(
        video_path,
        work_dir,
        fps=fps,
        ffmpeg_binary=ffmpeg_binary,
        ffprobe_binary=ffprobe_binary,
        max_edge=max_edge,
    )
    runs = group_stable_runs(
        frames,
        distance_threshold=distance_threshold,
        min_stable_seconds=min_stable_seconds,
    )
    if max_moments is not None and len(runs) > max_moments:
        runs = [
            runs[index]
            for index in _evenly_spaced_indexes(len(runs), max_moments)
        ]
    moments = _analyze_runs(
        runs,
        node_binary=node_binary,
        tesseract_config=tesseract_config,
        tesseract_binary=tesseract_binary,
        ocr_workers=ocr_workers,
        analysis_message="Analyzing stable frames",
        progress=progress,
    )
    if progress is not None:
        progress("Visual extraction complete", 1.0)
    return moments
