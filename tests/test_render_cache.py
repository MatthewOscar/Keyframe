from __future__ import annotations

import hashlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path, PureWindowsPath

import pytest

import video_context_mcp.config as config_module
import video_context_mcp.render_cache as render_cache_module
from video_context_mcp.config import Settings
from video_context_mcp.constants import MAX_IMAGE_BYTES
from video_context_mcp.errors import CacheError, ConfigurationError
from video_context_mcp.render_cache import RenderedFrameCache, make_render_markdown


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    os_temp = tmp_path / "os temp"
    os_temp.mkdir()
    monkeypatch.setattr(config_module.tempfile, "tempdir", str(os_temp))
    settings = Settings(home=tmp_path / "home", allowed_roots=())
    settings.ensure_directories()
    return settings


def test_render_cache_reuses_content_hash_and_preserves_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    cache = RenderedFrameCache(settings, ttl_s=600)
    data = b"exact encoded jpeg bytes"
    monkeypatch.setattr(render_cache_module.time, "time", lambda: 1_000.0)

    first = cache.publish(data, "image/jpeg", timestamp_s=4_774.9)
    monkeypatch.setattr(render_cache_module.time, "time", lambda: 1_100.0)
    second = cache.publish(data, "image/jpeg", timestamp_s=4_774.9)

    digest = hashlib.sha256(data).hexdigest()
    assert first.path == second.path
    assert first.path.name == f"frame-{digest}.jpg"
    assert first.path.read_bytes() == data
    assert first.markdown == f"![Keyframe frame at 01:19:34](<{first.path.as_posix()}>)"
    assert second.markdown == first.markdown
    assert datetime.fromisoformat(first.expires_at).timestamp() == 1_600.0
    assert second.expires_at == first.expires_at
    assert tuple(settings.rendered_frames_dir.iterdir()) == (first.path,)


def test_render_cache_repairs_a_corrupt_deterministic_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    cache = RenderedFrameCache(settings)
    data = b"original image"
    published = cache.publish(data, "image/jpeg", timestamp_s=1)
    published.path.write_bytes(b"corrupt")

    repaired = cache.publish(data, "image/jpeg", timestamp_s=1)

    assert repaired.path == published.path
    assert repaired.path.read_bytes() == data


def test_render_cache_rejects_and_prunes_oversized_corrupt_entries_without_full_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    cache = RenderedFrameCache(settings)
    cache.prune()
    oversized = settings.rendered_frames_dir / f"frame-{'0' * 64}.jpg"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_IMAGE_BYTES + 1)

    result = cache.prune()

    assert result.removed_files == 1
    assert not oversized.exists()
    with pytest.raises(CacheError, match="image limit"):
        cache.publish(b"x" * (MAX_IMAGE_BYTES + 1), "image/jpeg", timestamp_s=1)


def test_render_cache_prunes_expired_corrupt_and_interrupted_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    cache = RenderedFrameCache(settings, ttl_s=5)
    monkeypatch.setattr(render_cache_module.time, "time", lambda: 100.0)
    expired = cache.publish(b"expired", "image/jpeg", timestamp_s=1)
    os.utime(expired.path, (90, 90))
    corrupt = settings.rendered_frames_dir / f"frame-{'0' * 64}.jpg"
    corrupt.write_bytes(b"wrong digest")
    staging = settings.rendered_frames_dir / ".staging-interrupted.jpg"
    staging.write_bytes(b"partial")

    result = cache.prune()

    assert result.removed_files == 3
    assert result.retained_files == 0
    assert not expired.path.exists()
    assert not corrupt.exists()
    assert not staging.exists()


def test_render_cache_evicts_oldest_entry_to_global_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    cache = RenderedFrameCache(settings, quota_bytes=6)
    monkeypatch.setattr(render_cache_module.time, "time", lambda: 100.0)
    first = cache.publish(b"1111", "image/jpeg", timestamp_s=1)
    monkeypatch.setattr(render_cache_module.time, "time", lambda: 101.0)
    second = cache.publish(b"2222", "image/jpeg", timestamp_s=2)

    assert not first.path.exists()
    assert second.path.read_bytes() == b"2222"
    assert sum(path.stat().st_size for path in settings.rendered_frames_dir.iterdir()) <= 6


def test_render_cache_publication_is_atomic_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    data = b"one concurrent image"

    def publish() -> Path:
        return (
            RenderedFrameCache(settings)
            .publish(
                data,
                "image/jpeg",
                timestamp_s=1,
            )
            .path
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = tuple(executor.map(lambda _index: publish(), range(24)))

    assert len(set(paths)) == 1
    assert paths[0].read_bytes() == data
    assert not tuple(settings.rendered_frames_dir.glob(".staging-*"))
    assert len(tuple(settings.rendered_frames_dir.glob("frame-*"))) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions are platform-specific")
def test_render_cache_uses_private_directory_and_file_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    rendered = RenderedFrameCache(settings).publish(
        b"private image",
        "image/jpeg",
        timestamp_s=1,
    )

    assert stat.S_IMODE(settings.rendered_frames_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(rendered.path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires extra privileges")
def test_render_cache_rejects_links_without_touching_their_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    cache = RenderedFrameCache(settings)
    cache.prune()
    data = b"safe image"
    digest = hashlib.sha256(data).hexdigest()
    candidate = settings.rendered_frames_dir / f"frame-{digest}.jpg"
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"do not modify")
    candidate.symlink_to(outside)

    rendered = cache.publish(data, "image/jpeg", timestamp_s=1)

    assert outside.read_bytes() == b"do not modify"
    assert not rendered.path.is_symlink()
    assert rendered.path.read_bytes() == data


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires extra privileges")
def test_render_cache_rejects_a_linked_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    settings.rendered_frames_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="symbolic link or junction"):
        RenderedFrameCache(settings).publish(b"image", "image/jpeg", timestamp_s=1)


def test_render_cache_rejects_a_junction_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    cache = RenderedFrameCache(settings)
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda path: path == settings.rendered_frames_dir,
        raising=False,
    )

    with pytest.raises(ConfigurationError, match="symbolic link or junction"):
        cache.publish(b"image", "image/jpeg", timestamp_s=1)


def test_startup_prune_does_not_create_an_absent_render_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    cache = RenderedFrameCache(settings)

    result = cache.prune(create=False)

    assert result.retained_files == 0
    assert not settings.rendered_frames_dir.exists()


def test_windows_render_markdown_uses_forward_slashes_and_angle_brackets() -> None:
    path = PureWindowsPath(
        r"C:\Users\Matthew Wyatt\AppData\Local\Temp\keyframe\rendered-frames\frame.jpg"
    )

    assert make_render_markdown(path, 4_774.9) == (
        "![Keyframe frame at 01:19:34]"
        "(<C:/Users/Matthew Wyatt/AppData/Local/Temp/keyframe/rendered-frames/frame.jpg>)"
    )


@pytest.mark.parametrize("unsafe", ["/tmp/line\nbreak.jpg", "/tmp/<frame>.jpg"])
def test_render_markdown_rejects_unsafe_destinations(unsafe: str) -> None:
    with pytest.raises(CacheError, match="unsafe for Markdown"):
        make_render_markdown(Path(unsafe), 1)
