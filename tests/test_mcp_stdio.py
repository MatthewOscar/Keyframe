from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client, types

ROOT = Path(__file__).parents[1]
FIXTURE_DIR = (Path(__file__).parent / "fixtures").resolve()
VIDEO_PATH = FIXTURE_DIR / "keyframe-synthetic.mp4"
REQUIRED_TOOLS = ("ffmpeg", "ffprobe", "tesseract", "node")
MISSING_TOOLS = tuple(tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        bool(MISSING_TOOLS),
        reason=f"native Keyframe tools are missing: {', '.join(MISSING_TOOLS)}",
    ),
]


@pytest.mark.asyncio
async def test_real_stdio_handshake_tool_discovery_progress_and_cached_query(
    tmp_path: Path,
) -> None:
    progress: list[tuple[float, float | None, str | None]] = []
    roots_requests = 0
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "video_context_mcp", "serve", "--transport", "stdio"],
        cwd=ROOT,
        env={
            "KEYFRAME_HOME": str(tmp_path / "home"),
            "PYTHONPATH": str(ROOT / "src"),
            "PATH": os.environ.get("PATH", ""),
        },
    )

    async def list_roots(_context: object) -> types.ListRootsResult:
        nonlocal roots_requests
        roots_requests += 1
        return types.ListRootsResult(roots=[types.Root(uri=FIXTURE_DIR.as_uri())])

    async def capture_progress(
        value: float,
        total: float | None,
        message: str | None,
    ) -> None:
        progress.append((value, total, message))

    with (tmp_path / "server.stderr").open("w+", encoding="utf-8") as stderr:
        with anyio.fail_after(20):
            async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    list_roots_callback=list_roots,
                ) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "Keyframe"

                    listed = await session.list_tools()
                    assert {tool.name for tool in listed.tools} == {
                        "video_ingest",
                        "video_get_transcript",
                        "video_search",
                        "video_list_moments",
                        "video_get_code",
                        "video_get_frame",
                    }

                    ingest = await session.call_tool(
                        "video_ingest",
                        {
                            "source": str(VIDEO_PATH),
                            "mode": "full",
                            "transcript_mode": "captions",
                            "max_duration_s": 30,
                        },
                        progress_callback=capture_progress,
                    )
                    assert ingest.isError is False
                    assert ingest.structuredContent is not None
                    assert ingest.structuredContent["has_transcript"] is True
                    assert ingest.structuredContent["keyframe_count"] >= 3
                    video_id = str(ingest.structuredContent["video_id"])

                    transcript = await session.call_tool(
                        "video_get_transcript", {"video_id": video_id, "limit": 1}
                    )
                    assert transcript.isError is False
                    assert transcript.structuredContent is not None
                    assert len(transcript.structuredContent["segments"]) == 1
                    assert transcript.structuredContent["has_more"] is True

                    said = await session.call_tool(
                        "video_search",
                        {
                            "video_id": video_id,
                            "query": "normalizing non-alphanumeric",
                            "channel": "said",
                            "limit": 3,
                        },
                    )
                    assert said.isError is False
                    assert said.structuredContent is not None
                    assert said.structuredContent["hits"][0]["channel"] == "said"

                    shown = await session.call_tool(
                        "video_search",
                        {
                            "video_id": video_id,
                            "query": "slugify",
                            "channel": "shown",
                            "limit": 3,
                        },
                    )
                    assert shown.isError is False
                    assert shown.structuredContent is not None
                    assert shown.structuredContent["hits"][0]["channel"] == "shown"
                    assert shown.structuredContent["hits"][0]["moment_id"] is not None

                    moments = await session.call_tool(
                        "video_list_moments",
                        {"video_id": video_id, "kind": "code", "limit": 20},
                    )
                    assert moments.isError is False
                    assert moments.structuredContent is not None
                    assert moments.structuredContent["moments"]
                    code_moment_id = str(moments.structuredContent["moments"][0]["moment_id"])

                    code = await session.call_tool(
                        "video_get_code",
                        {"video_id": video_id, "moment_id": code_moment_id},
                    )
                    assert code.isError is False
                    assert code.structuredContent is not None
                    assert code.structuredContent["moment_id"] == code_moment_id
                    assert "slugify" in str(code.structuredContent["code"]).lower()
                    assert [block.type for block in code.content] == ["text", "image"]

                    frame = await session.call_tool(
                        "video_get_frame", {"video_id": video_id, "t": 5.0}
                    )
                    assert frame.isError is False
                    assert frame.structuredContent is not None
                    assert frame.structuredContent["requested_t"] == 5.0
                    assert [block.type for block in frame.content] == ["text", "image"]

                    invalid_code = await session.call_tool("video_get_code", {"video_id": video_id})
                    assert invalid_code.isError is True
                    error_text = " ".join(
                        block.text for block in invalid_code.content if block.type == "text"
                    )
                    assert "exactly one of moment_id or t" in error_text

        stderr.seek(0)
        stderr_output = stderr.read()

    assert progress
    assert progress[0][0] == 0
    assert progress[-1][0] == 100
    assert roots_requests == 1
    assert stderr_output == ""


@pytest.mark.asyncio
async def test_stdio_local_ingest_requires_client_or_configured_root(tmp_path: Path) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "video_context_mcp", "serve", "--transport", "stdio"],
        cwd=ROOT,
        env={
            "KEYFRAME_HOME": str(tmp_path / "home"),
            "PYTHONPATH": str(ROOT / "src"),
            "PATH": os.environ.get("PATH", ""),
        },
    )

    with anyio.fail_after(10):
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "video_ingest",
                    {"source": str(VIDEO_PATH), "mode": "fast", "transcript_mode": "none"},
                )

    assert result.isError is True
    message = " ".join(block.text for block in result.content if block.type == "text")
    assert "No local-video roots are authorized" in message
