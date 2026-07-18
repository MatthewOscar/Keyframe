from __future__ import annotations

import os
from pathlib import Path

import pytest

import video_context_mcp.config as config_module
from video_context_mcp.config import Settings
from video_context_mcp.errors import ConfigurationError


def test_settings_resolve_home_roots_and_overrides(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    extra = tmp_path / "videos"
    cwd.mkdir()
    extra.mkdir()
    settings = Settings.from_env(
        {
            "KEYFRAME_HOME": str(tmp_path / "state" / ".." / "keyframe"),
            "KEYFRAME_ALLOWED_ROOTS": os.pathsep.join((str(extra), str(cwd))),
            "KEYFRAME_FFMPEG": "/opt/keyframe/ffmpeg",
            "KEYFRAME_MAX_DURATION_S": "900",
            "KEYFRAME_MAX_LOCAL_FILE_BYTES": "1234",
            "KEYFRAME_MAX_REMOTE_FILE_BYTES": "5678",
            "KEYFRAME_MAX_SUBTITLE_BYTES": "99",
            "KEYFRAME_ALLOW_PRIVATE_URLS": "yes",
        },
        cwd=cwd,
    )

    assert settings.home == (tmp_path / "keyframe").resolve()
    assert settings.allowed_roots == (extra.resolve(), cwd.resolve())
    assert settings.ffmpeg_executable == "/opt/keyframe/ffmpeg"
    assert settings.default_max_duration_s == 900
    assert settings.max_local_file_bytes == 1234
    assert settings.max_remote_file_bytes == 5678
    assert settings.max_subtitle_bytes == 99
    assert settings.allow_private_urls is True


def test_settings_use_platform_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    default_home = tmp_path / "platform-home"
    monkeypatch.setattr(config_module, "user_data_path", lambda *_args, **_kwargs: default_home)

    settings = Settings.from_env({}, cwd=cwd)

    assert settings.home == default_home.resolve()
    assert settings.allowed_roots == ()


def test_ensure_directories_creates_runtime_layout(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path / "home", allowed_roots=(tmp_path,))

    settings.ensure_directories()
    settings.ensure_directories()

    assert settings.tmp_dir.is_dir()
    assert settings.cache_dir.is_dir()
    assert settings.artifacts_dir.is_dir()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KEYFRAME_MAX_DURATION_S", "0"),
        ("KEYFRAME_MAX_DURATION_S", "14401"),
        ("KEYFRAME_MAX_LOCAL_FILE_BYTES", "many"),
        ("KEYFRAME_ALLOW_PRIVATE_URLS", "sometimes"),
        ("KEYFRAME_FFPROBE", "   "),
    ],
)
def test_invalid_environment_values_raise(tmp_path: Path, name: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        Settings.from_env({name: value}, cwd=tmp_path)


def test_nonexistent_allowed_root_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        Settings.from_env(
            {"KEYFRAME_ALLOWED_ROOTS": str(tmp_path / "missing")},
            cwd=tmp_path,
        )
