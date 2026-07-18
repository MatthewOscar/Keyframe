"""Optional, local speech transcription through faster-whisper.

The dependency is imported lazily so the base package remains lightweight.  This
module deliberately returns evidence only; it never calls a general-purpose LLM.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from video_context_mcp.acquisition import TranscriptSegment
from video_context_mcp.errors import ConfigurationError, ExtractionError

ProgressCallback = Callable[[float, str], None]


def whisper_available() -> bool:
    """Return whether the optional faster-whisper package can be imported."""

    return importlib.util.find_spec("faster_whisper") is not None


def transcribe_media(
    media_path: Path,
    *,
    progress: ProgressCallback | None = None,
) -> tuple[TranscriptSegment, ...]:
    """Transcribe a validated local media file with a configurable local model."""

    if not whisper_available():
        raise ConfigurationError(
            "Whisper transcription is not installed. Install video-context-mcp[whisper] and retry."
        )
    if not media_path.is_file():
        raise ExtractionError(f"Media for Whisper transcription is missing: {media_path}")

    model_name = os.environ.get("KEYFRAME_WHISPER_MODEL", "base").strip() or "base"
    if progress is not None:
        progress(0, f"Loading local Whisper model {model_name}")
    try:
        module = importlib.import_module("faster_whisper")
        model_type = cast(Any, module).WhisperModel
        model = model_type(model_name, device="cpu", compute_type="int8")
        raw_segments, info = model.transcribe(
            str(media_path),
            beam_size=5,
            vad_filter=True,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        raise ExtractionError(
            f"Local Whisper transcription failed with model {model_name!r}: {exc}"
        ) from exc

    try:
        language_value = getattr(info, "language", None)
        language = str(language_value) if language_value else None
        segments: list[TranscriptSegment] = []
        # faster-whisper performs most decoding lazily while this iterator is
        # consumed, so failures here need the same actionable boundary as model
        # construction and the initial transcribe call above.
        for index, raw in enumerate(raw_segments):
            text = str(getattr(raw, "text", "")).strip()
            if not text:
                continue
            try:
                start_s = max(0.0, float(raw.start))
                end_s = max(start_s, float(raw.end))
            except (TypeError, ValueError) as exc:
                raise ExtractionError(
                    f"Whisper returned invalid timestamps for segment {index}."
                ) from exc
            segments.append(
                TranscriptSegment(
                    start_s=start_s,
                    end_s=end_s,
                    text=text,
                    language=language,
                    origin="whisper",
                )
            )
    except (KeyboardInterrupt, SystemExit):
        raise
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(
            f"Local Whisper transcription failed while decoding segments with model "
            f"{model_name!r}: {exc}"
        ) from exc
    if progress is not None:
        progress(100, "Local Whisper transcription complete")
    return tuple(segments)
