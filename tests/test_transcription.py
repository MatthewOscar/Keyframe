from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_context_mcp._whisper_worker as whisper_worker
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

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        payload = json.dumps(
            {
                "language": "en",
                "segments": [
                    {"start_s": -0.5, "end_s": 1.25, "text": "  hello world  "},
                    {"start_s": 2, "end_s": 3, "text": "   "},
                ],
            }
        )
        return subprocess.CompletedProcess(command, 0, payload, "")

    monkeypatch.setattr(transcription, "whisper_available", lambda: True)
    monkeypatch.setattr(transcription.subprocess, "run", fake_run)
    progress: list[tuple[float, str]] = []

    segments = transcription.transcribe_media(media, progress=lambda *event: progress.append(event))

    command = observed["command"]
    assert isinstance(command, list)
    assert command[1:4] == ["-m", "video_context_mcp._whisper_worker", "base"]
    assert command[4] == str(media)
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["capture_output"] is True
    assert observed["encoding"] == "utf-8"
    assert observed["timeout"] == 1_800.0
    assert len(segments) == 1
    assert segments[0].start_s == 0
    assert segments[0].end_s == 1.25
    assert segments[0].text == "hello world"
    assert segments[0].language == "en"
    assert segments[0].origin == "whisper"
    assert progress[0] == (0, "Loading isolated local Whisper model base")
    assert progress[-1] == (100, "Local Whisper transcription complete")


def test_whisper_worker_normalizes_model_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "worker-normalized.mp4"
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

    monkeypatch.setattr(
        whisper_worker.importlib,
        "import_module",
        lambda _name: SimpleNamespace(WhisperModel=FakeModel),
    )

    payload = whisper_worker.decode_media(media, "base")

    assert observed["name"] == "base"
    assert observed["init"] == {"device": "cpu", "compute_type": "int8"}
    assert observed["path"] == str(media)
    assert observed["transcribe"] == {"beam_size": 5, "vad_filter": True}
    assert payload == {
        "language": "en",
        "segments": [{"start_s": 0.0, "end_s": 1.25, "text": "hello world"}],
    }


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

    monkeypatch.setattr(
        whisper_worker.importlib,
        "import_module",
        lambda _name: SimpleNamespace(WhisperModel=FakeModel),
    )

    with pytest.raises(whisper_worker.WhisperWorkerError, match="invalid start timestamp"):
        whisper_worker.decode_media(media, "base")


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

    monkeypatch.setattr(
        whisper_worker.importlib,
        "import_module",
        lambda _name: SimpleNamespace(WhisperModel=FakeModel),
    )

    with pytest.raises(
        whisper_worker.WhisperWorkerError,
        match=r"failed while decoding segments.*decoder worker stopped",
    ):
        whisper_worker.decode_media(media, "base")


def test_whisper_worker_failure_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "speech-worker-failure.mp4"
    media.write_bytes(b"fixture")
    monkeypatch.setattr(transcription, "whisper_available", lambda: True)
    monkeypatch.setattr(
        transcription.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            "",
            "Whisper worker failed: decoder worker stopped",
        ),
    )

    with pytest.raises(
        ExtractionError,
        match=r"failed with model 'base'.*decoder worker stopped",
    ):
        transcription.transcribe_media(media)


def test_malformed_whisper_worker_json_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "speech-malformed-json.mp4"
    media.write_bytes(b"fixture")
    monkeypatch.setattr(transcription, "whisper_available", lambda: True)
    monkeypatch.setattr(
        transcription.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "not-json", ""),
    )

    with pytest.raises(ExtractionError, match="malformed JSON evidence"):
        transcription.transcribe_media(media)


def test_whisper_worker_timeout_is_bounded_and_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "speech-timeout.mp4"
    media.write_bytes(b"fixture")
    observed: dict[str, object] = {}

    def timeout(command: list[str], **kwargs: object) -> object:
        observed["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, float(kwargs["timeout"]))

    monkeypatch.setattr(transcription, "whisper_available", lambda: True)
    monkeypatch.setattr(transcription.subprocess, "run", timeout)

    with pytest.raises(ExtractionError, match=r"timed out after 12s.*smaller") as caught:
        transcription.transcribe_media(media, timeout_s=12.5)

    assert observed["timeout"] == 12.5
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)


@pytest.mark.parametrize("timeout_s", [0, -1, float("nan"), float("inf")])
def test_whisper_worker_rejects_invalid_timeouts(tmp_path: Path, timeout_s: float) -> None:
    media = tmp_path / "speech-invalid-timeout.mp4"
    media.write_bytes(b"fixture")

    with pytest.raises(ValueError, match="timeout_s"):
        transcription.transcribe_media(media, timeout_s=timeout_s)


def test_whisper_worker_main_keeps_stdout_json_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    media = tmp_path / "speech.mp4"
    media.write_bytes(b"fixture")

    def fake_decode(_media: Path, _model: str) -> dict[str, object]:
        print("third-party diagnostic")
        return {
            "language": "ja",
            "segments": [{"start_s": 0, "end_s": 1, "text": "café 東京"}],
        }

    monkeypatch.setattr(whisper_worker, "decode_media", fake_decode)

    assert whisper_worker.main(["base", str(media)]) == 0
    captured = capsys.readouterr()
    assert captured.out.isascii()
    assert json.loads(captured.out) == {
        "language": "ja",
        "segments": [{"start_s": 0, "end_s": 1, "text": "café 東京"}],
    }
    assert captured.err.strip() == "third-party diagnostic"
