from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import video_context_mcp.acquisition as acquisition
from video_context_mcp.acquisition import (
    SourceKind,
    acquire_local,
    acquire_remote,
    classify_source,
    parse_subtitles,
    validate_local_path,
    validate_remote_url,
)
from video_context_mcp.config import Settings
from video_context_mcp.errors import SourceError

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        home=tmp_path / "home",
        allowed_roots=(tmp_path.resolve(),),
        max_local_file_bytes=1_000_000,
        max_remote_file_bytes=1_000_000,
        max_subtitle_bytes=100_000,
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("video.mp4", SourceKind.LOCAL),
        ("https://youtu.be/abc", SourceKind.YOUTUBE),
        ("https://youtube.com/watch?v=abc", SourceKind.YOUTUBE),
        ("https://www.youtube.com/watch?v=abc", SourceKind.YOUTUBE),
        ("https://www.loom.com/share/abc", SourceKind.LOOM),
        ("https://cdn.example.com/demo.mp4", SourceKind.DIRECT),
    ],
)
def test_classify_source(source: str, expected: SourceKind) -> None:
    assert classify_source(source) is expected


@pytest.mark.parametrize(
    "source",
    [
        "ftp://example.com/video.mp4",
        "https://user:secret@example.com/video.mp4",
        "https://example.com/video.mp4#t=1",
        "http://127.0.0.1/video.mp4",
        "http://[::1]/video.mp4",
        "http://localhost/video.mp4",
        "http://10.0.0.8/video.mp4",
    ],
)
def test_remote_url_rejects_unsafe_sources(source: str) -> None:
    with pytest.raises(SourceError):
        validate_remote_url(source, resolve_dns=False)


def test_remote_url_rejects_hostname_resolving_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        acquisition.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("192.168.1.10", 443))],
    )

    with pytest.raises(SourceError, match="private or non-public"):
        validate_remote_url("https://media.example/video.mp4")


def test_remote_url_allows_public_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        acquisition.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", (PUBLIC_IP, 443))],
    )

    assert validate_remote_url("https://media.example/video.mp4") == (
        "https://media.example/video.mp4"
    )


def test_local_validation_resolves_symlinks_and_enforces_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    target = outside / "secret.mp4"
    target.write_bytes(b"video")
    symlink = allowed / "video.mp4"
    symlink.symlink_to(target)
    local_settings = Settings(home=tmp_path / "home", allowed_roots=(allowed.resolve(),))

    with pytest.raises(SourceError, match="outside the authorized roots"):
        validate_local_path(symlink, local_settings)


def test_local_validation_rejects_extension_and_size(tmp_path: Path) -> None:
    wrong = tmp_path / "video.txt"
    wrong.write_text("not video")
    with pytest.raises(SourceError, match="extension"):
        validate_local_path(wrong, Settings(home=tmp_path / "home", allowed_roots=(tmp_path,)))

    large = tmp_path / "video.mp4"
    large.write_bytes(b"12345")
    with pytest.raises(SourceError, match="above"):
        validate_local_path(
            large,
            Settings(home=tmp_path / "home", allowed_roots=(tmp_path,), max_local_file_bytes=4),
        )


def test_relative_local_path_resolves_only_under_explicit_roots(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "demo.mp4").write_bytes(b"one")
    settings = Settings(home=tmp_path / "home", allowed_roots=(first, second))

    assert validate_local_path("demo.mp4", settings) == (first / "demo.mp4").resolve()
    (second / "demo.mp4").write_bytes(b"two")
    with pytest.raises(SourceError, match="ambiguous"):
        validate_local_path("demo.mp4", settings)


def test_file_root_authorizes_only_that_exact_file(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed.mp4"
    sibling = tmp_path / "sibling.mp4"
    allowed.write_bytes(b"allowed")
    sibling.write_bytes(b"sibling")
    settings = Settings(home=tmp_path / "home", allowed_roots=(allowed.resolve(),))

    assert validate_local_path(allowed, settings) == allowed.resolve()
    assert validate_local_path(allowed.name, settings) == allowed.resolve()
    with pytest.raises(SourceError, match="outside the authorized roots"):
        validate_local_path(sibling, settings)


def test_temp_upload_staging_requires_collision_safe_child(tmp_path: Path) -> None:
    settings = Settings(
        home=tmp_path / "home",
        allowed_roots=(),
        allow_temp_uploads=True,
    )
    settings.ensure_directories()
    direct = settings.upload_dir / "selected.mp4"
    direct.write_bytes(b"unsafe shared basename")
    first_upload = settings.upload_dir / "upload-11111111"
    second_upload = settings.upload_dir / "upload-22222222"
    first_upload.mkdir(mode=0o700)
    second_upload.mkdir(mode=0o700)
    first_staged = first_upload / "selected.mp4"
    second_staged = second_upload / "selected.mp4"
    first_staged.write_bytes(b"first selected attachment")
    second_staged.write_bytes(b"second selected attachment")
    outside = settings.tmp_dir / "outside.mp4"
    outside.write_bytes(b"not explicitly staged")

    with pytest.raises(SourceError, match="cannot be placed directly in the shared upload root"):
        validate_local_path(direct, settings)
    assert validate_local_path(first_staged, settings) == first_staged.resolve()
    assert validate_local_path(second_staged, settings) == second_staged.resolve()
    with pytest.raises(SourceError, match="Temporary upload root"):
        validate_local_path(outside, settings)


def test_temp_upload_hint_covers_missing_relative_and_absolute_paths(tmp_path: Path) -> None:
    settings = Settings(
        home=tmp_path / "home",
        allowed_roots=(),
        allow_temp_uploads=True,
    )
    settings.ensure_directories()

    for source in ("missing.mp4", tmp_path / "also-missing.mp4"):
        with pytest.raises(SourceError) as error:
            validate_local_path(source, settings)
        message = str(error.value)
        assert f"Temporary upload root: {settings.upload_dir}." in message
        assert "unique per-upload child directory" in message


def test_temp_upload_error_does_not_list_unrelated_authorized_roots(tmp_path: Path) -> None:
    durable_root = tmp_path / "private-videos"
    durable_root.mkdir()
    outside = tmp_path / "selected.mp4"
    outside.write_bytes(b"selected attachment")
    settings = Settings(
        home=tmp_path / "home",
        allowed_roots=(durable_root.resolve(),),
        allow_temp_uploads=True,
    )
    settings.ensure_directories()

    with pytest.raises(SourceError) as error:
        validate_local_path(outside, settings)

    message = str(error.value)
    assert str(durable_root) not in message
    assert str(settings.upload_dir) in message


def test_ambiguous_relative_error_uses_hint_without_leaking_matches(tmp_path: Path) -> None:
    first = tmp_path / "first-private-root"
    second = tmp_path / "second-private-root"
    first.mkdir()
    second.mkdir()
    (first / "selected.mp4").write_bytes(b"first")
    (second / "selected.mp4").write_bytes(b"second")
    settings = Settings(
        home=tmp_path / "home",
        allowed_roots=(first.resolve(), second.resolve()),
        allow_temp_uploads=True,
    )
    settings.ensure_directories()

    with pytest.raises(SourceError) as error:
        validate_local_path("selected.mp4", settings)

    message = str(error.value)
    assert "ambiguous across authorized roots" in message
    assert str(first) not in message
    assert str(second) not in message
    assert str(settings.upload_dir) in message


def test_exact_file_root_does_not_authorize_sibling_sidecar(tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    sidecar = tmp_path / "demo.en.vtt"
    video.write_bytes(b"video")
    sidecar.write_text("WEBVTT\n", encoding="utf-8")

    exact_file = Settings(home=tmp_path / "file-home", allowed_roots=(video.resolve(),))
    assert acquisition._sidecar_candidates(video.resolve(), exact_file) == []

    directory_and_file = Settings(
        home=tmp_path / "directory-home",
        allowed_roots=(tmp_path.resolve(), video.resolve()),
    )
    candidates = acquisition._sidecar_candidates(video.resolve(), directory_and_file)
    assert candidates == [(sidecar, sidecar.resolve())]


def test_local_caption_sidecar_cannot_escape_authorized_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    video = allowed / "demo.mp4"
    video.write_bytes(b"video")
    escaped = outside / "demo.en.vtt"
    escaped.write_text("WEBVTT\n", encoding="utf-8")
    (allowed / "demo.en.vtt").symlink_to(escaped)
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(_probe_document()), stderr=""
    )
    monkeypatch.setattr(acquisition.subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(SourceError, match="sidecar escapes"):
        acquire_local(
            video,
            Settings(home=tmp_path / "home", allowed_roots=(allowed.resolve(),)),
        )


def test_parse_subtitles_normalizes_tags_and_duplicate_cues() -> None:
    document = """WEBVTT

00:00:01.000 --> 00:00:02.000 position:10%
<c.colorE5E5E5>Hello&nbsp;world</c>

00:00:02,000 --> 00:00:03,500
Hello world

00:00:04.000 --> 00:00:05.000
second   line
continues
"""

    segments = parse_subtitles(document, language="en")

    assert len(segments) == 2
    assert segments[0].start_s == 1
    assert segments[0].end_s == 3.5
    assert segments[0].text == "Hello world"
    assert segments[1].text == "second line\ncontinues"


def _probe_document(
    *,
    duration: str = "12.5",
    subtitle: bool = False,
    chapters: bool = False,
    audio: bool = True,
) -> dict[str, Any]:
    streams: list[dict[str, Any]] = [
        {
            "index": 0,
            "codec_type": "video",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
        }
    ]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": "aac"})
    if subtitle:
        streams.append(
            {
                "index": 2,
                "codec_type": "subtitle",
                "codec_name": "webvtt",
                "tags": {"language": "en"},
            }
        )
    document: dict[str, Any] = {
        "format": {"duration": duration, "tags": {"title": "Demo"}},
        "streams": streams,
    }
    if chapters:
        document["chapters"] = [
            {"start_time": "0", "end_time": "4.5", "tags": {"title": "Local intro"}}
        ]
    return document


def test_local_acquisition_normalizes_metadata_and_sidecar(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video bytes")
    (tmp_path / "demo.en.vtt").write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nWelcome\n",
        encoding="utf-8",
    )
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(_probe_document(chapters=True)), stderr=""
    )
    monkeypatch.setattr(acquisition.subprocess, "run", lambda *_args, **_kwargs: completed)

    acquired = acquire_local(video, settings, transcript_mode="captions")

    assert acquired.metadata.kind is SourceKind.LOCAL
    assert acquired.metadata.title == "Demo"
    assert acquired.metadata.duration_s == 12.5
    assert acquired.metadata.width == 1920
    assert acquired.metadata.fps == pytest.approx(29.97, rel=0.001)
    assert acquired.metadata.has_audio is True
    assert acquired.metadata.video_id.startswith("local-")
    assert acquired.chapters[0].title == "Local intro"
    assert acquired.transcript[0].text == "Welcome"
    assert acquired.transcript[0].language == "en"
    assert acquired.transcript[0].origin == "sidecar"
    assert acquired.media_path == video.resolve()
    acquired.cleanup()
    assert video.exists()


def test_local_animated_gif_is_accepted_as_visual_only(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    animation = tmp_path / "steps.GIF"
    animation.write_bytes(b"animated gif")
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(_probe_document(duration="2.5", audio=False)),
        stderr="",
    )
    monkeypatch.setattr(acquisition.subprocess, "run", lambda *_args, **_kwargs: completed)

    acquired = acquire_local(animation, settings, transcript_mode="auto")

    assert acquired.metadata.duration_s == 2.5
    assert acquired.metadata.has_audio is False
    assert acquired.media_path == animation.resolve()


def test_static_gif_failure_is_actionable(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "static.gif"
    image.write_bytes(b"static gif")
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(_probe_document(duration="0", audio=False)),
        stderr="",
    )
    monkeypatch.setattr(acquisition.subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(SourceError, match="supports animated GIFs"):
        acquire_local(image, settings)


def test_local_acquisition_reads_embedded_subtitles(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")
    ffprobe = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(_probe_document(subtitle=True)), stderr=""
    )
    ffmpeg = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=b"WEBVTT\n\n00:00:02.000 --> 00:00:03.000\nEmbedded\n",
        stderr=b"",
    )
    responses = iter((ffprobe, ffmpeg))
    monkeypatch.setattr(acquisition.subprocess, "run", lambda *_args, **_kwargs: next(responses))

    acquired = acquire_local(video, settings)

    assert acquired.transcript[0].text == "Embedded"
    assert acquired.transcript[0].origin == "embedded"


def test_local_acquisition_enforces_duration_before_pipeline(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "long.mp4"
    video.write_bytes(b"video")
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(_probe_document(duration="301")), stderr=""
    )
    monkeypatch.setattr(acquisition.subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(SourceError, match="above"):
        acquire_local(video, settings, max_duration_s=300)


def _remote_info(**updates: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": "abc123",
        "title": "Remote demo",
        "duration": 42.0,
        "availability": "public",
        "extractor_key": "Youtube",
        "webpage_url": f"https://{PUBLIC_IP}/watch?v=abc123",
        "uploader": "Keyframe",
        "width": 1280,
        "height": 720,
        "fps": 30,
        "chapters": [{"start_time": 0, "end_time": 10, "title": "Intro"}],
    }
    info.update(updates)
    return info


class _FakeResponse:
    def __init__(self, url: str, payload: bytes) -> None:
        self._url = url
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, limit: int) -> bytes:
        return self._payload[:limit]


def _install_fake_ydl(
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, Any],
    *,
    download_result: dict[str, Any] | None = None,
    downloaded_has_audio: bool = True,
) -> list[tuple[dict[str, Any], bool]]:
    calls: list[tuple[dict[str, Any], bool]] = []

    class FakeYDL:
        def __init__(self, options: dict[str, Any]) -> None:
            self.options = options

        def __enter__(self) -> FakeYDL:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def extract_info(self, _source: str, *, download: bool) -> dict[str, Any]:
            calls.append((self.options, download))
            if not download:
                return metadata
            result = dict(metadata if download_result is None else download_result)
            output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            output.write_bytes(b"downloaded video")
            result["_filename"] = str(output)
            return result

    monkeypatch.setattr(acquisition, "_get_youtube_dl", lambda *_args: FakeYDL)
    monkeypatch.setattr(
        acquisition,
        "_probe_downloaded_media",
        lambda *_args, **_kwargs: downloaded_has_audio,
    )
    return calls


def test_remote_fast_mode_downloads_low_resolution_video_probe_with_captions(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtitle_url = f"https://{PUBLIC_IP}/captions.vtt"
    info = _remote_info(
        subtitles={"en": [{"ext": "vtt", "url": subtitle_url}]},
        automatic_captions={"en": [{"ext": "vtt", "url": f"https://{PUBLIC_IP}/auto.vtt"}]},
    )
    calls = _install_fake_ydl(monkeypatch, info)
    monkeypatch.setattr(
        acquisition,
        "_open_validated_subtitle",
        lambda *_args, **_kwargs: _FakeResponse(
            subtitle_url,
            b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nManual caption\n",
        ),
    )

    acquired = acquire_remote(f"https://{PUBLIC_IP}/video", settings)

    assert acquired.metadata.video_id == "abc123"
    assert acquired.metadata.provider == "youtube"
    assert acquired.metadata.availability == "public"
    assert acquired.transcript[0].text == "Manual caption"
    assert acquired.transcript[0].origin == "captions"
    assert acquired.chapters[0].title == "Intro"
    assert acquired.media_path is not None
    temp_dir = acquired.media_path.parent
    assert [download for _, download in calls] == [False, True]
    options = calls[0][0]
    assert options["noplaylist"] is True
    assert options["cookiefile"] is None
    assert options["proxy"] == ""
    assert options["allow_unplayable_formats"] is False
    assert options["external_downloader"] == "native"
    assert options["hls_prefer_native"] is True
    assert options["js_runtimes"] == {"node": {"path": settings.node_executable}}
    assert calls[1][0]["format"] == ("bestvideo[height<=360]/best[height<=360]/worstvideo/worst")

    acquired.cleanup()

    assert not temp_dir.exists()
    assert acquired.media_path is None


def test_remote_unlisted_availability_is_preserved(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ydl(monkeypatch, _remote_info(availability="unlisted"))

    acquired = acquire_remote(
        f"https://{PUBLIC_IP}/video",
        settings,
        transcript_mode="none",
    )

    assert acquired.metadata.availability == "unlisted"
    acquired.cleanup()


def test_remote_audio_capability_is_preserved(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_ydl(
        monkeypatch,
        _remote_info(acodec="none"),
        downloaded_has_audio=False,
    )

    acquired = acquire_remote(f"https://{PUBLIC_IP}/animation.gif", settings)

    assert acquired.metadata.has_audio is False
    assert calls[1][0]["format"] == (
        "bestvideo[height<=360]/best[height<=360]/worstvideo/worst"
    )
    acquired.cleanup()


def test_remote_audio_formats_override_video_only_top_level_metadata() -> None:
    info = _remote_info(
        acodec="none",
        requested_formats=[
            {"vcodec": "avc1", "acodec": "none"},
            {"vcodec": "none", "acodec": "opus"},
        ],
    )

    assert acquisition._remote_has_audio(info) is True


def test_audio_less_download_overrides_incomplete_initial_metadata(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ydl(
        monkeypatch,
        _remote_info(),
        downloaded_has_audio=False,
    )

    acquired = acquire_remote(f"https://{PUBLIC_IP}/video", settings)

    assert acquired.metadata.has_audio is False
    acquired.cleanup()


def test_audio_bearing_full_download_overrides_incorrect_initial_metadata(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ydl(
        monkeypatch,
        _remote_info(acodec="none"),
        downloaded_has_audio=True,
    )

    acquired = acquire_remote(
        f"https://{PUBLIC_IP}/video",
        settings,
        mode="full",
        transcript_mode="none",
    )

    assert acquired.metadata.has_audio is True
    acquired.cleanup()


def test_video_only_probe_does_not_erase_source_audio_metadata(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ydl(
        monkeypatch,
        _remote_info(acodec="aac"),
        downloaded_has_audio=False,
    )

    acquired = acquire_remote(
        f"https://{PUBLIC_IP}/video",
        settings,
        transcript_mode="none",
    )

    assert acquired.metadata.has_audio is True
    acquired.cleanup()


@pytest.mark.parametrize("has_audio", [False, True])
def test_downloaded_media_probe_returns_audio_stream_presence(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    has_audio: bool,
) -> None:
    media = tmp_path / "downloaded.mp4"
    media.write_bytes(b"downloaded media")
    probe = _probe_document(duration="42", audio=has_audio)
    monkeypatch.setattr(acquisition, "_run_json_command", lambda *_args, **_kwargs: probe)

    assert (
        acquisition._probe_downloaded_media(media, settings, max_duration_s=60) is has_audio
    )


def test_auto_transcript_falls_back_when_manual_caption_download_fails(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manual_url = f"https://{PUBLIC_IP}/manual.vtt"
    automatic_url = f"https://{PUBLIC_IP}/automatic.vtt"
    calls = _install_fake_ydl(
        monkeypatch,
        _remote_info(
            subtitles={"en": [{"ext": "vtt", "url": manual_url}]},
            automatic_captions={"en": [{"ext": "vtt", "url": automatic_url}]},
        ),
    )

    def open_caption(request: Any, *_args: object, **_kwargs: object) -> _FakeResponse:
        if request.full_url == manual_url:
            raise OSError("manual track expired")
        return _FakeResponse(
            automatic_url,
            b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAutomatic caption\n",
        )

    monkeypatch.setattr(acquisition, "_open_validated_subtitle", open_caption)

    acquired = acquire_remote(f"https://{PUBLIC_IP}/video", settings)

    assert acquired.transcript[0].text == "Automatic caption"
    assert acquired.transcript[0].origin == "automatic_captions"
    assert any("Manual captions could not be read" in warning for warning in acquired.warnings)
    assert calls[1][0]["format"] == ("bestvideo[height<=360]/best[height<=360]/worstvideo/worst")
    acquired.cleanup()


def test_remote_fast_captionless_auto_downloads_low_resolution_av_probe(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_ydl(monkeypatch, _remote_info())

    acquired = acquire_remote(
        f"https://{PUBLIC_IP}/video",
        settings,
        mode="fast",
        transcript_mode="auto",
    )

    assert acquired.media_path is not None
    assert acquired.transcript == ()
    assert any("No readable captions" in warning for warning in acquired.warnings)
    assert [download for _, download in calls] == [False, True]
    assert calls[1][0]["format"] == "best[height<=360]/worst"
    acquired.cleanup()


def test_remote_full_mode_downloads_then_cleanup_is_idempotent(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os_temp = settings.home.parent / "os-temp"
    os_temp.mkdir()
    monkeypatch.setattr(acquisition.tempfile, "tempdir", str(os_temp))
    calls = _install_fake_ydl(monkeypatch, _remote_info())

    acquired = acquire_remote(
        f"https://{PUBLIC_IP}/video",
        settings,
        mode="full",
        transcript_mode="none",
    )
    assert acquired.media_path is not None
    media_path = acquired.media_path
    temp_dir = media_path.parent
    assert media_path.read_bytes() == b"downloaded video"
    assert media_path.is_relative_to(os_temp)
    assert not media_path.is_relative_to(settings.home)
    assert [download for _, download in calls] == [False, True]
    assert calls[1][0]["format"] == "bv*+ba/b"

    acquired.cleanup()
    acquired.cleanup()

    assert not temp_dir.exists()
    assert list(settings.tmp_dir.iterdir()) == []
    assert acquired.media_path is None


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"_type": "playlist", "entries": []}, "Playlists"),
        ({"is_live": True}, "Livestreams"),
        ({"age_limit": 18}, "Age-restricted"),
        ({"availability": "private"}, "authentication"),
        ({"has_drm": True}, "DRM"),
        ({"duration": 500}, "above"),
        ({"filesize": 1_000_001}, "above"),
        ({"width": 10_000, "height": 10_000}, "extraction limit"),
    ],
)
def test_remote_restrictions_are_checked_before_download(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, Any],
    message: str,
) -> None:
    calls = _install_fake_ydl(monkeypatch, _remote_info(**updates))

    with pytest.raises(SourceError, match=message):
        acquire_remote(
            f"https://{PUBLIC_IP}/video",
            settings,
            mode="full",
            max_duration_s=300,
        )

    assert [download for _, download in calls] == [False]
    assert not settings.tmp_dir.exists()


def test_remote_download_failure_removes_temporary_directory(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = _remote_info()
    calls = 0

    class FailingYDL:
        def __init__(self, options: dict[str, Any]) -> None:
            self.options = options

        def __enter__(self) -> FailingYDL:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def extract_info(self, _source: str, *, download: bool) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if not download:
                return info
            Path(self.options["outtmpl"].replace("%(ext)s", "part")).write_bytes(b"partial")
            raise RuntimeError("network interrupted")

    monkeypatch.setattr(acquisition, "_get_youtube_dl", lambda *_args: FailingYDL)

    with pytest.raises(SourceError, match="network interrupted"):
        acquire_remote(
            f"https://{PUBLIC_IP}/video",
            settings,
            mode="full",
            transcript_mode="none",
        )

    assert calls == 2
    assert settings.tmp_dir.is_dir()
    assert list(settings.tmp_dir.iterdir()) == []


def test_remote_whisper_mode_downloads_even_when_fast(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_ydl(monkeypatch, _remote_info())

    acquired = acquire_remote(
        f"https://{PUBLIC_IP}/video",
        settings,
        mode="fast",
        transcript_mode="whisper",
    )

    assert [download for _, download in calls] == [False, True]
    assert "Whisper" in acquired.warnings[0]
    assert calls[1][0]["format"] == "best[height<=360]/worst"
    acquired.cleanup()


def test_refresh_disables_yt_dlp_cache(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_ydl(monkeypatch, _remote_info())

    acquired = acquire_remote(
        f"https://{PUBLIC_IP}/video",
        settings,
        transcript_mode="none",
        refresh=True,
    )

    assert calls[0][0]["cachedir"] is False
    assert calls[1][0]["cachedir"] is False
    acquired.cleanup()


def test_remote_download_revalidates_identity_and_cleans_up(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_ydl(
        monkeypatch,
        _remote_info(),
        download_result=_remote_info(id="different-video"),
    )

    with pytest.raises(SourceError, match="changed video identity"):
        acquire_remote(
            f"https://{PUBLIC_IP}/video",
            settings,
            mode="full",
            transcript_mode="none",
        )

    assert [download for _, download in calls] == [False, True]
    assert list(settings.tmp_dir.iterdir()) == []


def test_download_options_honor_custom_ffmpeg_and_enforce_streaming_size(
    settings: Settings,
) -> None:
    configured = Settings(
        home=settings.home,
        allowed_roots=settings.allowed_roots,
        ffmpeg_executable="/opt/keyframe/ffmpeg",
        max_remote_file_bytes=100,
    )

    options = acquisition._base_ydl_options(configured, refresh=False)
    hook = acquisition._download_size_hook(configured)

    assert options["ffmpeg_location"] == "/opt/keyframe/ffmpeg"
    hook({"filename": "video.mp4", "downloaded_bytes": 60})
    hook({"filename": "audio.m4a", "downloaded_bytes": 40})
    with pytest.raises(SourceError, match="exceeded"):
        hook({"filename": "audio.m4a", "downloaded_bytes": 41})


def test_safe_connection_rejects_private_dns_answer_before_socket_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        acquisition.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("169.254.169.254", 443))],
    )

    with pytest.raises(OSError, match="private or non-public"):
        acquisition._safe_create_connection(("rebound.example", 443))


def test_subtitle_opener_ignores_environment_proxies(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "socks5h://127.0.0.1:8888")

    opener = acquisition._validated_subtitle_opener(settings)
    handler_names = {type(handler).__name__ for handler in opener.handlers}

    assert "ProxyHandler" not in handler_names
    assert "_ValidatedSubtitleHTTPHandler" in handler_names
    assert "_ValidatedSubtitleHTTPSHandler" in handler_names


def test_ytdlp_handler_rejects_request_local_proxies(settings: Settings) -> None:
    youtube_dl = acquisition._get_youtube_dl(settings)

    with youtube_dl(acquisition._base_ydl_options(settings, refresh=False)) as ydl:
        handler = ydl._request_director.handlers["Urllib"]
        assert handler._get_proxies(SimpleNamespace(proxies={})) == {"all": None}
        with pytest.raises(SourceError, match="Per-request proxies"):
            handler._get_proxies(SimpleNamespace(proxies={"https": "socks5h://127.0.0.1:8888"}))


def test_ytdlp_rejects_external_network_protocol_before_dispatch(
    settings: Settings,
    tmp_path: Path,
) -> None:
    youtube_dl = acquisition._get_youtube_dl(settings)

    with (
        youtube_dl(acquisition._base_ydl_options(settings, refresh=False)) as ydl,
        pytest.raises(SourceError, match=r"unsupported media protocol.*rtmp"),
    ):
        ydl.dl(
            str(tmp_path / "source.mp4"),
            {"url": "rtmp://media.example/video", "protocol": "rtmp"},
        )


def test_ytdlp_rejects_unsafe_hls_before_ffmpeg_fallback(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yt_dlp.downloader.external import FFmpegFD

    manifest_url = f"https://{PUBLIC_IP}/playlist.m3u8"
    manifest = b'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"\n#EXTINF:2.0,\nsegment.ts\n'
    ffmpeg_called = False

    def forbidden_ffmpeg(*_args: object, **_kwargs: object) -> bool:
        nonlocal ffmpeg_called
        ffmpeg_called = True
        return False

    monkeypatch.setattr(FFmpegFD, "real_download", forbidden_ffmpeg)
    youtube_dl = acquisition._get_youtube_dl(settings)

    with youtube_dl(acquisition._base_ydl_options(settings, refresh=False)) as ydl:
        monkeypatch.setattr(
            ydl,
            "urlopen",
            lambda _request: _FakeResponse(manifest_url, manifest),
        )
        with pytest.raises(SourceError, match="will not delegate remote access to FFmpeg"):
            ydl.dl(
                str(tmp_path / "source.mp4"),
                {
                    "id": "unsafe-hls",
                    "url": manifest_url,
                    "protocol": "m3u8_native",
                    "ext": "mp4",
                    "is_live": False,
                },
            )

    assert ffmpeg_called is False


def test_ytdlp_pins_preflighted_safe_hls_manifest(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yt_dlp.downloader.hls import HlsFD

    manifest_url = f"https://{PUBLIC_IP}/playlist.m3u8"
    manifest = (
        b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\n"
        + f"https://{PUBLIC_IP}/segment.ts\n".encode()
        + b"#EXT-X-ENDLIST\n"
    )
    observed: dict[str, Any] = {}

    def native_download(_self: object, _filename: str, info: dict[str, Any]) -> bool:
        observed.update(info)
        return True

    monkeypatch.setattr(HlsFD, "real_download", native_download)
    youtube_dl = acquisition._get_youtube_dl(settings)

    with youtube_dl(acquisition._base_ydl_options(settings, refresh=False)) as ydl:
        monkeypatch.setattr(
            ydl,
            "urlopen",
            lambda _request: _FakeResponse(manifest_url, manifest),
        )
        result = ydl.dl(
            str(tmp_path / "source.mp4"),
            {
                "id": "safe-hls",
                "url": manifest_url,
                "protocol": "m3u8_native",
                "ext": "mp4",
                "is_live": False,
            },
        )

    assert result == (True, True)
    assert observed["hls_media_playlist_data"] == manifest.decode()


def test_ytdlp_factory_uses_only_the_validated_urllib_handler(settings: Settings) -> None:
    youtube_dl = acquisition._get_youtube_dl(settings)

    with youtube_dl(acquisition._base_ydl_options(settings, refresh=False)) as ydl:
        assert set(ydl._request_director.handlers) == {"Urllib"}
