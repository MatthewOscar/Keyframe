"""Optional, isolated local speech transcription through faster-whisper.

OpenCV and PyAV bundle different FFmpeg builds on macOS. Loading both in the
long-lived MCP process produces duplicate Objective-C class registrations and
can destabilize native media handling. Keyframe therefore runs faster-whisper
in a short-lived worker process and validates its JSON evidence at the boundary.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from video_context_mcp.acquisition import TranscriptSegment
from video_context_mcp.errors import ConfigurationError, ExtractionError

ProgressCallback = Callable[[float, str], None]
_DEFAULT_WHISPER_TIMEOUT_S = 1_800.0


def whisper_available() -> bool:
    """Return whether the optional faster-whisper package can be imported."""

    return importlib.util.find_spec("faster_whisper") is not None


def transcribe_media(
    media_path: Path,
    *,
    progress: ProgressCallback | None = None,
    timeout_s: float = _DEFAULT_WHISPER_TIMEOUT_S,
) -> tuple[TranscriptSegment, ...]:
    """Transcribe validated media in an isolated faster-whisper worker."""

    if timeout_s <= 0 or not math.isfinite(timeout_s):
        raise ValueError("timeout_s must be a positive finite number")
    if not whisper_available():
        raise ConfigurationError(
            "Whisper transcription is not installed. Install video-context-mcp[whisper] and retry."
        )
    if not media_path.is_file():
        raise ExtractionError(f"Media for Whisper transcription is missing: {media_path}")

    model_name = os.environ.get("KEYFRAME_WHISPER_MODEL", "base").strip() or "base"
    if progress is not None:
        progress(0, f"Loading isolated local Whisper model {model_name}")
    command = [
        sys.executable,
        "-m",
        "video_context_mcp._whisper_worker",
        model_name,
        str(media_path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(
            f"Local Whisper transcription timed out after {timeout_s:.0f}s with model "
            f"{model_name!r}. Retry with captions or a smaller KEYFRAME_WHISPER_MODEL."
        ) from exc
    except OSError as exc:
        raise ExtractionError(
            f"Could not start the isolated Whisper worker with {sys.executable!r}: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = _bounded_detail(completed.stderr) or "worker exited without an error message"
        raise ExtractionError(
            f"Local Whisper transcription failed with model {model_name!r}: {detail}"
        )
    try:
        payload: Any = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            "The isolated Whisper worker returned malformed JSON evidence. "
            "Retry the ingest; if this persists, run video-context-mcp doctor."
        ) from exc

    segments = _segments_from_payload(payload)
    if progress is not None:
        progress(100, "Local Whisper transcription complete")
    return segments


def _segments_from_payload(payload: Any) -> tuple[TranscriptSegment, ...]:
    if not isinstance(payload, Mapping):
        raise ExtractionError("The isolated Whisper worker returned an invalid result object.")
    language_value = payload.get("language")
    if language_value is not None and not isinstance(language_value, str):
        raise ExtractionError("The isolated Whisper worker returned an invalid language value.")
    language = language_value or None
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes)):
        raise ExtractionError("The isolated Whisper worker returned an invalid segment list.")

    segments: list[TranscriptSegment] = []
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, Mapping):
            raise ExtractionError(
                f"The isolated Whisper worker returned an invalid segment at index {index}."
            )
        text_value = raw.get("text")
        if not isinstance(text_value, str):
            raise ExtractionError(
                f"The isolated Whisper worker returned invalid text for segment {index}."
            )
        text = text_value.strip()
        if not text:
            continue
        start_s = _finite_timestamp(raw.get("start_s"), index=index, field="start_s")
        end_s = _finite_timestamp(raw.get("end_s"), index=index, field="end_s")
        start_s = max(0.0, start_s)
        end_s = max(start_s, end_s)
        segments.append(
            TranscriptSegment(
                start_s=start_s,
                end_s=end_s,
                text=text,
                language=language,
                origin="whisper",
            )
        )
    return tuple(segments)


def _finite_timestamp(value: object, *, index: int, field: str) -> float:
    if isinstance(value, bool):
        raise ExtractionError(
            f"The isolated Whisper worker returned invalid {field} for segment {index}."
        )
    try:
        timestamp = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ExtractionError(
            f"The isolated Whisper worker returned invalid {field} for segment {index}."
        ) from exc
    if not math.isfinite(timestamp):
        raise ExtractionError(
            f"The isolated Whisper worker returned non-finite {field} for segment {index}."
        )
    return timestamp


def _bounded_detail(value: str, *, limit: int = 2_000) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}…"
