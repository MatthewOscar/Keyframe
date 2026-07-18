from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import ValidationError

from video_context_mcp.errors import SourceError
from video_context_mcp.models import (
    CodeResult,
    FrameRegion,
    FrameResult,
    IngestMode,
    IngestResult,
    MomentKind,
    MomentPage,
    SearchChannel,
    SearchHit,
    SearchPage,
    StrictModel,
    TranscriptPage,
)
from video_context_mcp.server import _root_uri_to_path, create_server


class FakeService:
    def ingest(self, source: str, **kwargs: object) -> IngestResult:
        return IngestResult(
            video_id="demo",
            title="Demo",
            duration_s=1,
            source_type="local",
            has_transcript=False,
            keyframe_count=0,
            indexed_mode=IngestMode.FAST,
            status="ready",
            cache_hit=False,
        )

    def get_transcript(self, video_id: str, **kwargs: object) -> TranscriptPage:
        return TranscriptPage(video_id=video_id, segments=())

    def search(self, query: str, **kwargs: object) -> SearchPage:
        return SearchPage(query=query, hits=())

    def list_moments(self, video_id: str, **kwargs: object) -> MomentPage:
        return MomentPage(video_id=video_id, moments=())

    def get_code(self, video_id: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            result=CodeResult(
                video_id=video_id,
                moment_id="moment",
                actual_t=1,
                language_guess="python",
                code="print('hello')",
                parses=True,
                confidence=0.9,
                kind=MomentKind.CODE,
            ),
            image_data=b"image",
            mime_type="image/jpeg",
        )

    def get_frame(self, video_id: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            result=FrameResult(
                video_id=video_id,
                moment_id="moment",
                requested_t=1,
                actual_t=1,
                kind=MomentKind.CODE,
                region=FrameRegion.FULL,
            ),
            image_data=b"image",
            mime_type="image/jpeg",
        )


def test_mcp_root_uri_is_canonicalized_and_rejects_nonlocal_forms(tmp_path: Path) -> None:
    root = tmp_path / "video files"
    root.mkdir()

    assert _root_uri_to_path(root.as_uri()) == root.resolve()
    with pytest.raises(SourceError, match="local file"):
        _root_uri_to_path("https://example.com/videos")
    with pytest.raises(SourceError, match="UNC"):
        _root_uri_to_path("file://server/share")
    with pytest.raises(SourceError, match="UNC"):
        _root_uri_to_path("file:///%2Fserver/share")
    with pytest.raises(SourceError, match="UNC"):
        _root_uri_to_path("file:///%5C%5Cserver%5Cshare")
    with pytest.raises(SourceError, match="queries"):
        _root_uri_to_path(f"{root.as_uri()}?scope=wide")


class NonFiniteVisualService(FakeService):
    def get_code(self, video_id: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            result=CodeResult.model_construct(
                video_id=video_id,
                moment_id="moment",
                actual_t=1,
                language_guess="python",
                code="print('hello')",
                parses=True,
                confidence=float("nan"),
                classification_confidence=0,
                kind=MomentKind.CODE,
                notes=(),
            ),
            image_data=b"image",
            mime_type="image/jpeg",
        )


def test_exact_tool_surface_and_annotations() -> None:
    server = create_server(FakeService())
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    assert set(tools) == {
        "video_ingest",
        "video_get_transcript",
        "video_search",
        "video_list_moments",
        "video_get_code",
        "video_get_frame",
    }
    assert tools["video_ingest"].annotations is not None
    assert tools["video_ingest"].annotations.readOnlyHint is False
    assert tools["video_search"].annotations is not None
    assert tools["video_search"].annotations.readOnlyHint is True
    assert tools["video_get_code"].output_schema is not None


@pytest.mark.asyncio
async def test_structured_search_result() -> None:
    server = create_server(FakeService())
    result = await server._tool_manager.call_tool(
        "video_search", {"query": "python"}, convert_result=True
    )
    unstructured, structured = result
    assert unstructured
    assert structured == {
        "query": "python",
        "hits": [],
        "visual_coverage": None,
        "next_cursor": None,
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_visual_tool_returns_text_image_and_structured_content() -> None:
    server = create_server(FakeService())
    result = await server._tool_manager.call_tool(
        "video_get_code",
        {"video_id": "demo", "moment_id": "moment"},
        convert_result=True,
    )
    assert isinstance(result, CallToolResult)
    assert [block.type for block in result.content] == ["text", "image"]
    assert result.structuredContent is not None
    assert result.structuredContent["code"] == "print('hello')"


@pytest.mark.parametrize(
    "non_finite", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "neg-inf"]
)
def test_models_reject_non_finite_numbers_globally(non_finite: float) -> None:
    assert StrictModel.model_config["allow_inf_nan"] is False
    with pytest.raises(ValidationError, match="finite number"):
        SearchHit(
            video_id="demo",
            start_s=0,
            end_s=1,
            channel=SearchChannel.SAID,
            snippet="python",
            score=non_finite,
        )


@pytest.mark.asyncio
async def test_visual_tool_rejects_non_finite_json_with_actionable_error() -> None:
    server = create_server(NonFiniteVisualService())

    with pytest.raises(ToolError, match="refresh=true"):
        await server._tool_manager.call_tool(
            "video_get_code",
            {"video_id": "demo", "moment_id": "moment"},
            convert_result=True,
        )
