"""Private faster-whisper worker used to isolate PyAV from OpenCV."""

from __future__ import annotations

import importlib
import json
import math
import sys
from collections.abc import Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, cast


class WhisperWorkerError(RuntimeError):
    """An actionable failure while decoding speech evidence."""


def decode_media(media_path: Path, model_name: str) -> dict[str, object]:
    """Decode speech and return a JSON-serializable transcript payload."""

    if not media_path.is_file():
        raise WhisperWorkerError(f"Whisper media is missing: {media_path}")
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
        raise WhisperWorkerError(f"could not initialize or run the model: {exc}") from exc

    language_value = getattr(info, "language", None)
    language = str(language_value) if language_value else None
    segments: list[dict[str, object]] = []
    try:
        for index, raw in enumerate(raw_segments):
            text = str(getattr(raw, "text", "")).strip()
            if not text:
                continue
            start_s = _timestamp(getattr(raw, "start", None), index=index, field="start")
            end_s = _timestamp(getattr(raw, "end", None), index=index, field="end")
            start_s = max(0.0, start_s)
            end_s = max(start_s, end_s)
            segments.append({"start_s": start_s, "end_s": end_s, "text": text})
    except (KeyboardInterrupt, SystemExit):
        raise
    except WhisperWorkerError:
        raise
    except Exception as exc:
        raise WhisperWorkerError(f"failed while decoding segments: {exc}") from exc
    return {"language": language, "segments": segments}


def _timestamp(value: object, *, index: int, field: str) -> float:
    if isinstance(value, bool):
        raise WhisperWorkerError(f"invalid {field} timestamp for segment {index}")
    try:
        timestamp = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise WhisperWorkerError(f"invalid {field} timestamp for segment {index}") from exc
    if not math.isfinite(timestamp):
        raise WhisperWorkerError(f"non-finite {field} timestamp for segment {index}")
    return timestamp


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or not args[0].strip():
        print("usage: python -m video_context_mcp._whisper_worker MODEL MEDIA", file=sys.stderr)
        return 2
    try:
        # Third-party model/download code may print diagnostics. Keep stdout a
        # strict JSON channel so the parent can validate it deterministically.
        with redirect_stdout(sys.stderr):
            payload = decode_media(Path(args[1]), args[0])
    except WhisperWorkerError as exc:
        print(f"Whisper worker failed: {exc}", file=sys.stderr)
        return 1
    # ASCII-only JSON is locale-independent on Windows while json.loads still
    # reconstructs the original multilingual Unicode text in the parent.
    json.dump(payload, sys.stdout, ensure_ascii=True, allow_nan=False)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the parent subprocess
    raise SystemExit(main())
