from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from video_context_mcp.config import Settings
from video_context_mcp.models import SearchChannel
from video_context_mcp.service import KeyframeService

ROOT = Path(__file__).parents[1]
SAMPLE = ROOT / "samples" / "4geeks-function-tutorial"


def test_licensed_public_sample_checksums_and_persistent_queries(tmp_path: Path) -> None:
    checksums = (SAMPLE / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    for line in checksums:
        expected, relative = line.split(maxsplit=1)
        payload = (SAMPLE / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected

    copied_home = tmp_path / "keyframe-home"
    shutil.copytree(SAMPLE / "keyframe-home", copied_home)
    service = KeyframeService(
        settings=Settings(home=copied_home, allowed_roots=(tmp_path.resolve(),))
    )

    video = service.store.get_video("youtube-XazswkTqKJI-186e345191")
    assert video is not None
    assert video.title.startswith("11 How to declare a new function")
    assert video.has_transcript is True
    assert video.keyframe_count == 3

    said = service.search(
        "define a function",
        video_id=video.video_id,
        channel=SearchChannel.SAID,
        limit=3,
    )
    shown = service.search(
        "generate random",
        video_id=video.video_id,
        channel=SearchChannel.SHOWN,
        limit=3,
    )
    assert said.hits and said.hits[0].channel is SearchChannel.SAID
    assert shown.hits and shown.hits[0].channel is SearchChannel.SHOWN

    attribution = (SAMPLE / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "4Geeks Academy" in attribution
    assert "Creative Commons Attribution 3.0" in attribution
