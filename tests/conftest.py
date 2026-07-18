from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def keyframe_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "keyframe-home"
    monkeypatch.setenv("KEYFRAME_HOME", str(home))
    return home
