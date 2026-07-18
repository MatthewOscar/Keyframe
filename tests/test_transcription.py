from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import video_context_mcp.transcription as transcription
from video_context_mcp.errors import ConfigurationError, ExtractionError


def test_whisper_extra_missing_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "speech.mp4"
    media.write_bytes(b"fixture")
    monkeypatch.setattr(transcription, "whisper_available", lambda: False)

    with pytest.raises(ConfigurationError, match=r"\[whisper\]"):
        transcription.transcribe_media(media)


def test_local_whisper_segments_are_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "speech-normalized.mp4"
    media.write_bytes(b"fixture")
    observed: dict[str, object] = {}

    class FakeModel:
        def __init__(self, name: str, **kwargs: object) -> None:
            observed["name"] = name
            observed["init"] = kwargs

        def transcribe(self, path: str, **kwargs: object) -> tuple[list[object], object]:
            observed["path"] = path
            observed["transcribe"] = kwargs
            return (
                [
                    SimpleNamespace(start=-0.5, end=1.25, text="  hello world  "),
                    SimpleNamespace(start=2, end=3, text="   "),
                ],
                SimpleNamespace(language="en"),
            )

    monkeypatch.setattr(transcription, "whisper_available", lambda: True)
    monkeypatch.setattr(
        transcription.importlib,
        "import_module",
        lambda _name: SimpleNamespace(WhisperModel=FakeModel),
    )
    progress: list[tuple[float, str]] = []

    segments = transcription.transcribe_media(media, progress=lambda *event: progress.append(event))

    assert observed["name"] == "base"
    assert observed["init"] == {"device": "cpu", "compute_type": "int8"}
    assert observed["transcribe"] == {"beam_size": 5, "vad_filter": True}
    assert len(segments) == 1
    assert segments[0].start_s == 0
    assert segments[0].end_s == 1.25
    assert segments[0].text == "hello world"
    assert segments[0].language == "en"
    assert segments[0].origin == "whisper"
    assert progress[-1] == (100, "Local Whisper transcription complete")


def test_invalid_whisper_timestamps_fail_honestly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "speech-invalid.mp4"
    media.write_bytes(b"fixture")

    class FakeModel:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def transcribe(self, *_args: object, **_kwargs: object) -> tuple[list[object], object]:
            return [SimpleNamespace(start="bad", end=1, text="evidence")], SimpleNamespace()

    monkeypatch.setattr(transcription, "whisper_available", lambda: True)
    monkeypatch.setattr(
        transcription.importlib,
        "import_module",
        lambda _name: SimpleNamespace(WhisperModel=FakeModel),
    )

    with pytest.raises(ExtractionError, match="invalid timestamps"):
        transcription.transcribe_media(media)


def test_lazy_whisper_iteration_failures_are_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "speech-lazy-failure.mp4"
    media.write_bytes(b"fixture")

    def failing_segments() -> object:
        yield SimpleNamespace(start=0, end=1, text="decoded evidence")
        raise RuntimeError("decoder worker stopped")

    class FakeModel:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def transcribe(self, *_args: object, **_kwargs: object) -> tuple[object, object]:
            return failing_segments(), SimpleNamespace(language="en")

    monkeypatch.setattr(transcription, "whisper_available", lambda: True)
    monkeypatch.setattr(
        transcription.importlib,
        "import_module",
        lambda _name: SimpleNamespace(WhisperModel=FakeModel),
    )

    with pytest.raises(
        ExtractionError,
        match=r"failed while decoding segments.*decoder worker stopped",
    ) as caught:
        transcription.transcribe_media(media)
    assert isinstance(caught.value.__cause__, RuntimeError)
