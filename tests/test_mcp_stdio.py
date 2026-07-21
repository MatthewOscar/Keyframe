from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client, types

ROOT = Path(__file__).parents[1]
FIXTURE_DIR = (Path(__file__).parent / "fixtures").resolve()
VIDEO_PATH = FIXTURE_DIR / "keyframe-synthetic.mp4"
REQUIRED_TOOLS = ("ffmpeg", "ffprobe", "tesseract", "node")
MISSING_TOOLS = tuple(tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None)
# This test runs both native visual passes plus MCP process teardown. Shared CI runner
# throughput varies, so keep this as a deadlock guard rather than a performance assertion.
REAL_STDIO_PIPELINE_TIMEOUT_S = 60

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
        with anyio.fail_after(REAL_STDIO_PIPELINE_TIMEOUT_S):
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
                            "mode": "fast",
                            "transcript_mode": "captions",
                            "max_duration_s": 30,
                        },
                        progress_callback=capture_progress,
                    )
                    assert ingest.isError is False
                    assert ingest.structuredContent is not None
                    assert ingest.structuredContent["has_transcript"] is True
                    assert ingest.structuredContent["visual_coverage"] == "probe"
                    assert 1 <= ingest.structuredContent["keyframe_count"] <= 12
                    video_id = str(ingest.structuredContent["video_id"])

                    transcript = await session.call_tool(
                        "video_get_transcript", {"video_id": video_id, "limit": 1}
                    )
                    assert transcript.isError is False
                    assert transcript.structuredContent is not None
                    assert len(transcript.structuredContent["segments"]) == 1
                    assert transcript.structuredContent["has_more"] is True
                    next_cursor = str(transcript.structuredContent["next_cursor"])
                    prefix, kind_code, _offset, scope = next_cursor.split(".")
                    oversized_cursor = f"{prefix}.{kind_code}.zzzzzzzzzzzzz.{scope}"
                    invalid_cursor = await session.call_tool(
                        "video_get_transcript",
                        {"video_id": video_id, "cursor": oversized_cursor},
                    )
                    assert invalid_cursor.isError is True
                    assert any(
                        "Invalid page cursor" in block.text
                        for block in invalid_cursor.content
                        if isinstance(block, types.TextContent)
                    )

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
                    assert shown.structuredContent["visual_coverage"] == "probe"
                    shown_moment_id = str(shown.structuredContent["hits"][0]["moment_id"])

                    exact_frame = await session.call_tool(
                        "video_get_frame",
                        {"video_id": video_id, "moment_id": shown_moment_id},
                    )
                    assert exact_frame.isError is False
                    assert exact_frame.structuredContent is not None
                    assert exact_frame.structuredContent["moment_id"] == shown_moment_id
                    assert exact_frame.structuredContent["requested_moment_id"] == shown_moment_id
                    assert exact_frame.structuredContent["requested_t"] is None
                    assert (
                        exact_frame.structuredContent["start_s"]
                        <= exact_frame.structuredContent["actual_t"]
                    )
                    assert (
                        exact_frame.structuredContent["actual_t"]
                        <= exact_frame.structuredContent["end_s"]
                    )
                    assert "slugify" in exact_frame.structuredContent["ocr_text"].lower()
                    assert 0 <= exact_frame.structuredContent["ocr_confidence"] <= 1
                    assert 0 <= exact_frame.structuredContent["classification_confidence"] <= 1
                    assert (
                        sum(isinstance(block, types.ImageContent) for block in exact_frame.content)
                        == 1
                    )
                    exact_image = next(
                        block
                        for block in exact_frame.content
                        if isinstance(block, types.ImageContent)
                    )
                    exact_render = Path(str(exact_frame.structuredContent["render_path"]))
                    assert exact_render.read_bytes() == base64.b64decode(exact_image.data)
                    assert exact_frame.structuredContent["render_markdown"].endswith(
                        f"(<{exact_render.as_posix()}>)"
                    )
                    assert str(exact_frame.structuredContent["render_expires_at"]).endswith(
                        "+00:00"
                    )

                    probe_moments = await session.call_tool(
                        "video_list_moments",
                        {"video_id": video_id, "kind": "any", "limit": 12},
                    )
                    assert probe_moments.isError is False
                    assert probe_moments.structuredContent is not None
                    assert probe_moments.structuredContent["visual_coverage"] == "probe"
                    assert probe_moments.structuredContent["moments"]
                    assert all(
                        moment["stable_seconds"] == 0
                        for moment in probe_moments.structuredContent["moments"]
                    )

                    full = await session.call_tool(
                        "video_ingest",
                        {
                            "source": str(VIDEO_PATH),
                            "mode": "full",
                            "transcript_mode": "captions",
                            "max_duration_s": 30,
                        },
                        progress_callback=capture_progress,
                    )
                    assert full.isError is False
                    assert full.structuredContent is not None
                    assert full.structuredContent["video_id"] == video_id
                    assert full.structuredContent["visual_coverage"] == "full"
                    assert full.structuredContent["keyframe_count"] >= 3

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
                    assert code.structuredContent["visual_coverage"] == "full"
                    assert "slugify" in str(code.structuredContent["code"]).lower()
                    assert [block.type for block in code.content] == ["text", "image", "text"]
                    assert code.content[2].text == code.structuredContent["render_markdown"]
                    code_image = next(
                        block for block in code.content if isinstance(block, types.ImageContent)
                    )
                    assert Path(str(code.structuredContent["render_path"])).read_bytes() == (
                        base64.b64decode(code_image.data)
                    )

                    frame = await session.call_tool(
                        "video_get_frame", {"video_id": video_id, "t": 5.0}
                    )
                    assert frame.isError is False
                    assert frame.structuredContent is not None
                    assert frame.structuredContent["requested_t"] == 5.0
                    assert frame.structuredContent["requested_moment_id"] is None
                    assert frame.structuredContent["requested_t_covered"] is True
                    assert [block.type for block in frame.content] == ["text", "image", "text"]
                    assert frame.content[2].text == frame.structuredContent["render_markdown"]
                    frame_image = next(
                        block for block in frame.content if isinstance(block, types.ImageContent)
                    )
                    assert Path(str(frame.structuredContent["render_path"])).read_bytes() == (
                        base64.b64decode(frame_image.data)
                    )

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
    assert roots_requests == 2
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


@pytest.mark.asyncio
async def test_stdio_plugin_stages_one_selected_upload_in_private_temp_root(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "video_context_mcp", "serve", "--transport", "stdio"],
        cwd=ROOT,
        env={
            "KEYFRAME_HOME": str(home),
            "KEYFRAME_ALLOW_TEMP_UPLOADS": "true",
            "PYTHONPATH": str(ROOT / "src"),
            "PATH": os.environ.get("PATH", ""),
        },
    )

    with anyio.fail_after(REAL_STDIO_PIPELINE_TIMEOUT_S):
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                denied = await session.call_tool(
                    "video_ingest",
                    {"source": str(VIDEO_PATH), "mode": "fast", "transcript_mode": "none"},
                )
                assert denied.isError is True
                denied_text = " ".join(
                    block.text for block in denied.content if block.type == "text"
                )
                staging_match = re.search(
                    r"Temporary upload root: (.+?)\. Create a unique per-upload child directory",
                    denied_text,
                )
                assert staging_match is not None
                upload_dir = Path(staging_match.group(1))
                assert upload_dir.name == "uploads"
                assert upload_dir.parent.name.startswith("keyframe-")

                upload_child = Path(tempfile.mkdtemp(prefix="upload-", dir=upload_dir))
                if os.name == "posix":
                    assert stat.S_IMODE(upload_child.stat().st_mode) == 0o700
                try:
                    staged = upload_child / VIDEO_PATH.name
                    shutil.copy2(VIDEO_PATH, staged)
                    ingested = await session.call_tool(
                        "video_ingest",
                        {"source": str(staged), "mode": "fast", "transcript_mode": "none"},
                    )
                finally:
                    shutil.rmtree(upload_child)

    assert ingested.isError is False
    assert ingested.structuredContent is not None
    assert ingested.structuredContent["status"] == "ready"
    assert ingested.structuredContent["source_type"] == "local"
    assert not upload_child.exists()
