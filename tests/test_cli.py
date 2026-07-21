from __future__ import annotations

from pathlib import Path

import pytest

from video_context_mcp.cli import _is_loopback, main


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_loopback_hosts(host: str) -> None:
    assert _is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "example.com", "192.168.1.2"])
def test_non_loopback_hosts(host: str) -> None:
    assert not _is_loopback(host)


def test_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    main(["version"])
    assert capsys.readouterr().out.strip() == "0.2.6"


def test_cache_prune_command_reports_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("KEYFRAME_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KEYFRAME_PROXY_TTL_S", "300")
    monkeypatch.setenv("KEYFRAME_PROXY_CACHE_BYTES", "1024")

    main(["cache", "prune"])

    assert capsys.readouterr().out.strip() == (
        "Proxy cache pruned: removed 0 file(s) (0 bytes); retained 0 file(s) (0 bytes)."
    )
    assert (tmp_path / "home" / "cache" / "proxies").is_dir()


def test_cache_command_requires_prune_action() -> None:
    with pytest.raises(SystemExit, match="cache requires the `prune` action"):
        main(["cache"])


@pytest.mark.parametrize("command", ["serve", "doctor", "version"])
def test_prune_action_is_rejected_for_other_commands(command: str) -> None:
    with pytest.raises(SystemExit, match="only be used with the cache command"):
        main([command, "prune"])
