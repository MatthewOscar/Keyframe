from __future__ import annotations

import os
import stat
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
    assert settings.allow_temp_uploads is False
    assert settings.authorized_local_roots == ()


def test_temp_upload_root_is_explicit_private_and_not_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os_temp = tmp_path / "os-temp"
    cwd = tmp_path / "untrusted-cwd"
    os_temp.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(config_module.tempfile, "tempdir", str(os_temp))

    settings = Settings.from_env({"KEYFRAME_ALLOW_TEMP_UPLOADS": "true"}, cwd=cwd)
    settings.ensure_directories()

    assert settings.allow_temp_uploads is True
    assert settings.allowed_roots == ()
    assert settings.authorized_local_roots == (settings.upload_dir,)
    assert settings.upload_dir == settings.tmp_dir / "uploads"
    assert settings.upload_dir.is_dir()
    assert cwd.resolve() not in settings.authorized_local_roots
    if os.name == "posix":
        assert stat.S_IMODE(settings.tmp_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(settings.upload_dir.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode hardening is platform-specific")
def test_temp_upload_root_tightens_preexisting_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.setattr(config_module.tempfile, "tempdir", str(os_temp))
    settings = Settings(
        home=tmp_path / "home",
        allowed_roots=(),
        allow_temp_uploads=True,
    )
    settings.tmp_dir.mkdir(mode=0o777)
    settings.upload_dir.mkdir(mode=0o777)
    settings.tmp_dir.chmod(0o777)
    settings.upload_dir.chmod(0o777)

    settings.ensure_directories()

    assert stat.S_IMODE(settings.tmp_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.upload_dir.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires extra privileges")
def test_temp_namespace_rejects_symbolic_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os_temp = tmp_path / "os-temp"
    target = tmp_path / "attacker-controlled"
    os_temp.mkdir()
    target.mkdir()
    monkeypatch.setattr(config_module.tempfile, "tempdir", str(os_temp))
    settings = Settings(home=tmp_path / "home", allowed_roots=())
    settings.tmp_dir.symlink_to(target, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="symbolic link or junction"):
        settings.ensure_directories()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="ownership IDs are unavailable")
def test_temp_namespace_rejects_foreign_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.setattr(config_module.tempfile, "tempdir", str(os_temp))
    settings = Settings(home=tmp_path / "home", allowed_roots=())
    settings.tmp_dir.mkdir()
    actual_uid = settings.tmp_dir.lstat().st_uid
    monkeypatch.setattr(config_module.os, "getuid", lambda: actual_uid + 1)

    with pytest.raises(ConfigurationError, match="not owned by the current user"):
        settings.ensure_directories()


def test_ensure_directories_creates_runtime_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.setattr(config_module.tempfile, "tempdir", str(os_temp))
    settings = Settings(home=tmp_path / "home", allowed_roots=(tmp_path,))

    settings.ensure_directories()
    settings.ensure_directories()

    assert settings.tmp_dir.is_dir()
    assert settings.cache_dir.is_dir()
    assert settings.artifacts_dir.is_dir()
    assert settings.tmp_dir.parent == os_temp.resolve()


def test_temp_namespaces_are_isolated_by_keyframe_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.setattr(config_module.tempfile, "tempdir", str(os_temp))

    first = Settings(home=tmp_path / "first-home", allowed_roots=())
    same = Settings(home=tmp_path / "first-home", allowed_roots=())
    second = Settings(home=tmp_path / "second-home", allowed_roots=())

    assert first.tmp_dir == same.tmp_dir
    assert first.tmp_dir != second.tmp_dir
    assert first.tmp_dir.parent == os_temp.resolve()
    assert second.tmp_dir.parent == os_temp.resolve()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KEYFRAME_MAX_DURATION_S", "0"),
        ("KEYFRAME_MAX_DURATION_S", "14401"),
        ("KEYFRAME_MAX_LOCAL_FILE_BYTES", "many"),
        ("KEYFRAME_ALLOW_PRIVATE_URLS", "sometimes"),
        ("KEYFRAME_ALLOW_TEMP_UPLOADS", "sometimes"),
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
