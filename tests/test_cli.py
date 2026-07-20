from __future__ import annotations

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
    assert capsys.readouterr().out.strip() == "0.1.1"
