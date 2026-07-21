from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import video_context_mcp.proxy_cache as proxy_cache_module
from video_context_mcp.config import Settings
from video_context_mcp.errors import CacheError
from video_context_mcp.proxy_cache import ProxyCache


def _settings(
    tmp_path: Path,
    *,
    ttl_s: int = 300,
    quota_bytes: int = 1_024,
) -> Settings:
    return Settings(
        home=tmp_path / "home",
        allowed_roots=(),
        proxy_cache_ttl_s=ttl_s,
        proxy_cache_bytes=quota_bytes,
    )


def test_video_only_promotion_moves_owned_media_and_get_can_touch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(proxy_cache_module.time, "time", lambda: now)
    source = tmp_path / "probe.webm"
    source.write_bytes(b"visual-only")
    cache = ProxyCache(_settings(tmp_path))

    published = cache.promote(
        "youtube-abc_123",
        source,
        contains_audio=False,
        ffmpeg_binary="unused",
    )

    assert published is not None
    assert not source.exists()
    assert published.path == cache.root / "youtube-abc_123" / "media.webm"
    assert published.path.read_bytes() == b"visual-only"
    assert published.size_bytes == len(b"visual-only")
    assert datetime.fromisoformat(published.expires_at).timestamp() == 1_300.0
    if os.name == "posix":
        assert published.path.stat().st_mode & 0o777 == 0o600

    now = 1_100.0
    touched = cache.get("youtube-abc_123", touch=True)

    assert touched is not None
    assert touched.path.stat().st_mtime == 1_100.0
    assert datetime.fromisoformat(touched.expires_at).timestamp() == 1_400.0


def test_promotion_and_touch_work_without_no_follow_utime_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows lacks os.utime(..., follow_symlinks=False)."""

    now = 1_000.0
    observed_kwargs: list[dict[str, object]] = []
    original_utime = os.utime

    def portable_utime(
        path: os.PathLike[str] | str,
        times: tuple[float, float],
        **kwargs: object,
    ) -> None:
        observed_kwargs.append(kwargs)
        original_utime(path, times, **kwargs)

    monkeypatch.setattr(proxy_cache_module.os, "supports_follow_symlinks", set())
    monkeypatch.setattr(proxy_cache_module.os, "utime", portable_utime)
    monkeypatch.setattr(proxy_cache_module.time, "time", lambda: now)
    source = tmp_path / "probe.mp4"
    source.write_bytes(b"visual-only")
    cache = ProxyCache(_settings(tmp_path))

    published = cache.promote(
        "portable-utime",
        source,
        contains_audio=False,
        ffmpeg_binary="unused",
    )
    assert published is not None

    now = 1_100.0
    touched = cache.get("portable-utime", touch=True)

    assert touched is not None
    assert touched.path.stat().st_mtime == 1_100.0
    assert observed_kwargs == [{}, {}]


def test_get_removes_an_expired_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(proxy_cache_module.time, "time", lambda: now)
    source = tmp_path / "probe.mp4"
    source.write_bytes(b"expired")
    cache = ProxyCache(_settings(tmp_path, ttl_s=60))
    published = cache.promote(
        "expired-video",
        source,
        contains_audio=False,
        ffmpeg_binary="unused",
    )
    assert published is not None

    now = 1_061.0

    assert cache.get("expired-video") is None
    assert not (cache.root / "expired-video").exists()


def test_prune_reports_expired_and_retained_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ProxyCache(_settings(tmp_path, ttl_s=100))
    expired = cache.root / "expired" / "media.mp4"
    retained = cache.root / "retained" / "media.mp4"
    expired.parent.mkdir(parents=True)
    retained.parent.mkdir(parents=True)
    expired.write_bytes(b"old")
    retained.write_bytes(b"current")
    os.utime(expired, (1_000.0, 1_000.0))
    os.utime(retained, (1_150.0, 1_150.0))
    monkeypatch.setattr(proxy_cache_module.time, "time", lambda: 1_200.0)

    result = cache.prune()

    assert result.removed_files == 1
    assert result.removed_bytes == len(b"old")
    assert result.retained_files == 1
    assert result.retained_bytes == len(b"current")
    assert not expired.parent.exists()
    assert retained.is_file()


def test_promotion_evicts_the_least_recently_used_proxy_to_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(proxy_cache_module.time, "time", lambda: now)
    cache = ProxyCache(_settings(tmp_path, quota_bytes=10))
    first = tmp_path / "first.mp4"
    first.write_bytes(b"111111")
    assert cache.promote(
        "first",
        first,
        contains_audio=False,
        ffmpeg_binary="unused",
    ) is not None

    now = 1_001.0
    second = tmp_path / "second.mp4"
    second.write_bytes(b"222222")
    published = cache.promote(
        "second",
        second,
        contains_audio=False,
        ffmpeg_binary="unused",
    )

    assert published is not None
    assert cache.get("first") is None
    assert cache.get("second") is not None
    assert not (cache.root / "first").exists()


def test_oversized_proxy_is_not_taken_into_cache(tmp_path: Path) -> None:
    source = tmp_path / "oversized.mp4"
    source.write_bytes(b"too-large")
    cache = ProxyCache(_settings(tmp_path, quota_bytes=4))

    published = cache.promote(
        "oversized",
        source,
        contains_audio=False,
        ffmpeg_binary="unused",
    )

    assert published is None
    assert source.read_bytes() == b"too-large"
    assert not cache.root.exists()


@pytest.mark.parametrize(
    ("ttl_s", "quota_bytes"),
    [(0, 1_024), (300, 0)],
)
def test_disabled_cache_does_not_take_ownership_of_media(
    tmp_path: Path,
    ttl_s: int,
    quota_bytes: int,
) -> None:
    source = tmp_path / "probe.mp4"
    source.write_bytes(b"keep me")
    cache = ProxyCache(_settings(tmp_path, ttl_s=ttl_s, quota_bytes=quota_bytes))

    published = cache.promote(
        "disabled",
        source,
        contains_audio=False,
        ffmpeg_binary="unused",
    )

    assert published is None
    assert source.read_bytes() == b"keep me"
    assert not cache.root.exists()


def test_audio_promotion_uses_video_only_stream_copy_and_keeps_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "probe-with-audio.mp4"
    source.write_bytes(b"original-with-audio")
    cache = ProxyCache(_settings(tmp_path))
    observed_command: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed_command.extend(command)
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
        Path(command[-1]).write_bytes(b"silent-video")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(proxy_cache_module.subprocess, "run", fake_run)

    published = cache.promote(
        "with-audio",
        source,
        contains_audio=True,
        ffmpeg_binary="trusted-ffmpeg",
    )

    assert published is not None
    assert published.path.name == "media.mkv"
    assert published.path.read_bytes() == b"silent-video"
    assert source.read_bytes() == b"original-with-audio"
    assert observed_command[0] == "trusted-ffmpeg"
    assert observed_command[observed_command.index("-map") + 1] == "0:v:0"
    assert "-an" in observed_command
    assert "-sn" in observed_command
    assert "-dn" in observed_command
    assert observed_command[observed_command.index("-c:v") + 1] == "copy"


def test_failed_audio_remux_cleans_staging_and_preserves_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "probe-with-audio.mp4"
    source.write_bytes(b"original-with-audio")
    cache = ProxyCache(_settings(tmp_path))

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 1, "", "remux failed")

    monkeypatch.setattr(proxy_cache_module.subprocess, "run", fake_run)

    with pytest.raises(CacheError, match="Could not create a silent cached proxy: remux failed"):
        cache.promote(
            "failed-remux",
            source,
            contains_audio=True,
            ffmpeg_binary="trusted-ffmpeg",
        )

    assert source.read_bytes() == b"original-with-audio"
    assert list((cache.root / "failed-remux").glob(".staging-*")) == []
    assert cache.get("failed-remux") is None


def test_timed_out_audio_remux_cleans_partial_output_and_preserves_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "probe-with-audio.mp4"
    source.write_bytes(b"original-with-audio")
    cache = ProxyCache(_settings(tmp_path))

    def timeout(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, 300)

    monkeypatch.setattr(proxy_cache_module.subprocess, "run", timeout)

    with pytest.raises(CacheError, match="Timed out while stripping audio"):
        cache.promote(
            "timed-out-remux",
            source,
            contains_audio=True,
            ffmpeg_binary="trusted-ffmpeg",
        )

    assert source.read_bytes() == b"original-with-audio"
    assert list((cache.root / "timed-out-remux").glob(".staging-*")) == []
    assert cache.get("timed-out-remux") is None


def test_unsafe_video_id_is_rejected_without_moving_media(tmp_path: Path) -> None:
    source = tmp_path / "probe.mp4"
    source.write_bytes(b"safe")
    cache = ProxyCache(_settings(tmp_path))

    with pytest.raises(CacheError, match="Unsafe video ID"):
        cache.promote(
            "../escape",
            source,
            contains_audio=False,
            ffmpeg_binary="unused",
        )

    assert source.read_bytes() == b"safe"
    assert not (tmp_path / "home" / "cache" / "escape").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires extra privileges")
def test_proxy_cache_rejects_a_symbolic_link_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outside = tmp_path / "attacker-controlled"
    outside.mkdir()
    settings.proxy_dir.parent.mkdir(parents=True)
    settings.proxy_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CacheError, match="root must not be a symbolic link"):
        ProxyCache(settings)

    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires extra privileges")
def test_get_rejects_symlinked_media_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    cache = ProxyCache(_settings(tmp_path))
    entry = cache.root / "linked"
    entry.mkdir(parents=True)
    (entry / "media.mp4").symlink_to(outside)

    assert cache.get("linked") is None
    assert outside.read_bytes() == b"outside"
    cache.prune()
    assert not entry.exists()
