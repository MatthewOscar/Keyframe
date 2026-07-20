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
    IngestTimings,
    MomentKind,
    MomentPage,
    SearchChannel,
    SearchHit,
    SearchPage,
    StrictModel,
    TranscriptPage,
)
from video_context_mcp.server import _client_roots, _root_uri_to_path, create_server


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
            timings=IngestTimings(
                total_ms=12,
                cache_lookup_ms=1,
                acquisition_ms=4,
                visual_ms=7,
                index_commit_ms=1,
            ),
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
        requested_moment_id = kwargs.get("moment_id")
        requested_t = kwargs.get("t")
        return SimpleNamespace(
            result=FrameResult(
                video_id=video_id,
                moment_id="moment",
                start_s=0,
                end_s=2,
                requested_moment_id=(
                    str(requested_moment_id) if requested_moment_id is not None else None
                ),
                requested_t=float(requested_t) if requested_t is not None else None,
                requested_t_covered=True if requested_t is not None else None,
                actual_t=1,
                kind=MomentKind.CODE,
                region=FrameRegion.FULL,
                classification_confidence=0.88,
                ocr_text="print('hello')",
                ocr_confidence=0.9,
            ),
            image_data=b"image",
            mime_type="image/jpeg",
        )


class RecordingTranscriptService(FakeService):
    def __init__(self) -> None:
        self.limits: list[int] = []

    def get_transcript(self, video_id: str, **kwargs: object) -> TranscriptPage:
        self.limits.append(int(kwargs["limit"]))
        return super().get_transcript(video_id, **kwargs)


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


@pytest.mark.asyncio
async def test_client_roots_falls_back_when_list_roots_fails() -> None:
    class BrokenSession:
        client_params = SimpleNamespace(capabilities=SimpleNamespace(roots=object()))

        async def list_roots(self) -> object:
            raise RuntimeError("roots/list failed")

    roots = await _client_roots(SimpleNamespace(session=BrokenSession()))
    assert roots == ()


@pytest.mark.asyncio
async def test_client_roots_falls_back_when_list_roots_times_out() -> None:
    class TimedOutSession:
        client_params = SimpleNamespace(capabilities=SimpleNamespace(roots=object()))

        async def list_roots(self) -> object:
            raise TimeoutError

    roots = await _client_roots(SimpleNamespace(session=TimedOutSession()))
    assert roots == ()


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


class DurationGuardService(FakeService):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def ingest(self, source: str, **kwargs: object) -> IngestResult:
        self.calls.append((source, kwargs))
        if kwargs["max_duration_s"] == 1_800:
            raise SourceError(
                "Video duration is 2215.1s, above the configured 1800s limit. Retry "
                "video_ingest once with the exact same source and options, changing only "
                "max_duration_s=2216. Do not split or restage the source."
            )
        return super().ingest(source, **kwargs)


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

    ingest_schema = tools["video_ingest"].parameters["properties"]["max_duration_s"]
    assert ingest_schema["default"] == 1_800
    assert ingest_schema["maximum"] == 14_400
    transcript_tool = tools["video_get_transcript"]
    transcript_limit = transcript_tool.parameters["properties"]["limit"]
    assert transcript_limit["default"] == 200
    assert transcript_limit["maximum"] == 200
    assert "fewest safe pages" in transcript_limit["description"]
    transcript_video_id = transcript_tool.parameters["properties"]["video_id"]
    assert "byte-for-byte" in transcript_video_id["description"]
    ingest_video_id = tools["video_ingest"].output_schema["properties"]["video_id"]
    assert "byte-for-byte" in ingest_video_id["description"]
    assert "instead of searching plugin caches" in server.instructions
    assert "exact structured video_id byte-for-byte" in server.instructions
    for name in ("video_search", "video_list_moments"):
        properties = tools[name].parameters["properties"]
        required = set(tools[name].parameters.get("required", ()))
        for bound in ("start_s", "end_s"):
            assert bound in properties
            assert properties[bound]["default"] is None
            assert properties[bound]["anyOf"][0]["minimum"] == 0
            assert bound not in required
    frame_tool = tools["video_get_frame"]
    frame_properties = frame_tool.parameters["properties"]
    frame_required = set(frame_tool.parameters["required"])
    assert {"moment_id", "t"} <= frame_properties.keys()
    assert {"moment_id", "t"}.isdisjoint(frame_required)
    assert frame_tool.output_schema is not None
    for field in (
        "start_s",
        "end_s",
        "classification_confidence",
        "ocr_text",
        "ocr_confidence",
        "requested_moment_id",
        "requested_t",
        "requested_t_covered",
    ):
        assert field in frame_tool.output_schema["properties"]
    search_hit_schema = tools["video_search"].output_schema["$defs"]["SearchHit"]
    assert "video_get_frame" in search_hit_schema["properties"]["moment_id"]["description"]
    for name in ("video_get_transcript", "video_search", "video_list_moments"):
        cursor_schema = tools[name].parameters["properties"]["cursor"]
        assert "byte-for-byte" in cursor_schema["description"]
        assert cursor_schema["anyOf"][0]["maxLength"] == 512
        assert tools[name].output_schema is not None
        output_cursor = tools[name].output_schema["properties"]["next_cursor"]
        assert "byte-for-byte" in output_cursor["description"]


@pytest.mark.asyncio
async def test_structured_ingest_result_reports_request_local_timings() -> None:
    server = create_server(FakeService())
    result = await server._tool_manager.call_tool(
        "video_ingest", {"source": "/allowed/demo.mp4"}, convert_result=True
    )
    _unstructured, structured = result

    assert structured["timings"] == {
        "total_ms": 12,
        "cache_lookup_ms": 1,
        "acquisition_ms": 4,
        "transcription_ms": None,
        "visual_ms": 7,
        "index_commit_ms": 1,
    }


@pytest.mark.asyncio
async def test_transcript_tool_omitted_limit_invokes_service_with_200() -> None:
    service = RecordingTranscriptService()
    server = create_server(service)

    await server._tool_manager.call_tool(
        "video_get_transcript",
        {"video_id": "demo"},
        convert_result=True,
    )

    assert service.limits == [200]


@pytest.mark.asyncio
async def test_duration_guard_error_supports_one_same_source_retry() -> None:
    service = DurationGuardService()
    server = create_server(service)
    source = "/authorized/long meeting.mp4"
    original = {
        "source": source,
        "mode": "fast",
        "transcript_mode": "auto",
        "max_duration_s": 1_800,
        "refresh": False,
    }

    with pytest.raises(ToolError, match="changing only max_duration_s=2216"):
        await server._tool_manager.call_tool("video_ingest", original, convert_result=True)
    retry = {**original, "max_duration_s": 2_216}
    await server._tool_manager.call_tool("video_ingest", retry, convert_result=True)

    assert [call[0] for call in service.calls] == [source, source]
    first_options = service.calls[0][1]
    second_options = service.calls[1][1]
    for key in ("mode", "transcript_mode", "refresh", "client_roots"):
        assert first_options[key] == second_options[key]
    assert first_options["max_duration_s"] == 1_800
    assert second_options["max_duration_s"] == 2_216


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


@pytest.mark.asyncio
async def test_frame_tool_accepts_exact_moment_and_returns_structured_ocr() -> None:
    server = create_server(FakeService())
    result = await server._tool_manager.call_tool(
        "video_get_frame",
        {"video_id": "demo", "moment_id": "moment"},
        convert_result=True,
    )

    assert isinstance(result, CallToolResult)
    assert [block.type for block in result.content] == ["text", "image"]
    assert result.structuredContent is not None
    assert result.structuredContent["moment_id"] == "moment"
    assert result.structuredContent["requested_moment_id"] == "moment"
    assert result.structuredContent["requested_t"] is None
    assert result.structuredContent["start_s"] == 0
    assert result.structuredContent["end_s"] == 2
    assert result.structuredContent["ocr_text"] == "print('hello')"
    assert result.structuredContent["ocr_confidence"] == 0.9


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


def test_ingest_timings_reject_negative_durations() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        IngestTimings(total_ms=-1, cache_lookup_ms=0)


@pytest.mark.asyncio
async def test_visual_tool_rejects_non_finite_json_with_actionable_error() -> None:
    server = create_server(NonFiniteVisualService())

    with pytest.raises(ToolError, match="refresh=true"):
        await server._tool_manager.call_tool(
            "video_get_code",
            {"video_id": "demo", "moment_id": "moment"},
            convert_result=True,
        )
