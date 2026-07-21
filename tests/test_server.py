from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent
from pydantic import ValidationError

import video_context_mcp.server as server_module
from video_context_mcp.errors import SourceError
from video_context_mcp.models import (
    CodeResult,
    FrameEvidenceQuality,
    FrameQuality,
    FrameRegion,
    FrameResult,
    IngestEvidenceBundle,
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
    TranscriptSegment,
    TranscriptView,
    VisualCoverage,
)
from video_context_mcp.server import _client_roots, _root_uri_to_path, create_server


class FakeService:
    def frame_source_is_local(self, video_id: str) -> bool:
        del video_id
        return True

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
                render_path="/tmp/keyframe/rendered-frames/frame.jpg",
                render_markdown=(
                    "![Keyframe frame at 00:01](</tmp/keyframe/rendered-frames/frame.jpg>)"
                ),
                render_expires_at="2026-07-27T00:00:00+00:00",
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
                width=320,
                height=180,
                classification_confidence=0.88,
                ocr_text="print('hello')",
                ocr_confidence=0.9,
                render_path="/tmp/keyframe/rendered-frames/frame.jpg",
                render_markdown=(
                    "![Keyframe frame at 00:01](</tmp/keyframe/rendered-frames/frame.jpg>)"
                ),
                render_expires_at="2026-07-27T00:00:00+00:00",
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


class BundledIngestService(FakeService):
    def ingest(self, source: str, **kwargs: object) -> IngestResult:
        result = super().ingest(source, **kwargs)
        return result.model_copy(
            update={
                "evidence_bundle": IngestEvidenceBundle(
                    scope_end_s=1,
                    transcript=TranscriptPage(
                        video_id="demo",
                        segments=(
                            TranscriptSegment(
                                segment_id="demo:compact:0",
                                start_s=0,
                                end_s=1,
                                text="short evidence",
                            ),
                        ),
                        view=TranscriptView.COMPACT,
                    ),
                    moments=MomentPage(
                        video_id="demo",
                        moments=(),
                        visual_coverage=VisualCoverage.PROBE,
                    ),
                )
            }
        )


class RemoteUncoveredFrameService(FakeService):
    def frame_source_is_local(self, video_id: str) -> bool:
        del video_id
        return False

    def get_frame(self, video_id: str, **kwargs: object) -> SimpleNamespace:
        payload = super().get_frame(video_id, **kwargs)
        payload.result = payload.result.model_copy(update={"requested_t_covered": False})
        return payload


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
    for name in (
        "video_get_transcript",
        "video_search",
        "video_list_moments",
        "video_get_code",
        "video_get_frame",
    ):
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.idempotentHint is True
    assert tools["video_get_code"].output_schema is not None

    ingest_schema = tools["video_ingest"].parameters["properties"]["max_duration_s"]
    assert ingest_schema["default"] == 1_800
    assert ingest_schema["maximum"] == 14_400
    transcript_tool = tools["video_get_transcript"]
    transcript_limit = transcript_tool.parameters["properties"]["limit"]
    assert transcript_limit["default"] == 200
    assert transcript_limit["maximum"] == 200
    assert "200 minutes" in transcript_limit["description"]
    transcript_view = transcript_tool.parameters["properties"]["view"]
    assert transcript_view["default"] == "exact"
    transcript_view_definition = transcript_tool.parameters["$defs"]["TranscriptView"]
    assert transcript_view_definition["enum"] == ["exact", "compact"]
    assert transcript_tool.output_schema["properties"]["view"]["default"] == "exact"
    transcript_video_id = transcript_tool.parameters["properties"]["video_id"]
    assert "byte-for-byte" in transcript_video_id["description"]
    ingest_video_id = tools["video_ingest"].output_schema["properties"]["video_id"]
    assert "byte-for-byte" in ingest_video_id["description"]
    ingest_output = tools["video_ingest"].output_schema["properties"]
    assert ingest_output["proxy_cached"]["default"] is False
    assert "targeted timestamp seeks" in ingest_output["proxy_cached"]["description"]
    assert {
        "retrieval_guidance",
        "evidence_bundle",
        "proxy_size_bytes",
        "proxy_expires_at",
    } <= ingest_output.keys()
    evidence_bundle = ingest_output["evidence_bundle"]
    assert evidence_bundle["default"] is None
    assert evidence_bundle["anyOf"][0]["$ref"] == "#/$defs/IngestEvidenceBundle"
    bundle_definition = tools["video_ingest"].output_schema["$defs"]["IngestEvidenceBundle"]
    assert {"transcript", "transcript_omitted_reason", "moments"} <= set(
        bundle_definition["properties"]
    )
    assert "Duration changes batching, never intent" in ingest_output["retrieval_guidance"][
        "default"
    ]
    assert "up to 10 minutes" in evidence_bundle["description"]
    for name in ("video_search", "video_list_moments"):
        properties = tools[name].parameters["properties"]
        required = set(tools[name].parameters.get("required", ()))
        for bound in ("start_s", "end_s"):
            assert bound in properties
            assert properties[bound]["default"] is None
            assert properties[bound]["anyOf"][0]["minimum"] == 0
            assert bound not in required
        assert "half-open temporal window" in properties["start_s"]["description"]
        assert "Inclusive start" in properties["start_s"]["description"]
        assert "Exclusive end" in properties["end_s"]["description"]
    assert "local library, never the web" in tools["video_search"].description
    assert "does not discover videos" in tools["video_ingest"].description
    assert "action-phase heuristic" in tools["video_search"].description
    assert (
        "every untimed no-vision single-image physical-action share"
        in tools["video_search"].parameters["properties"]["channel"]["description"]
    )
    assert "Skip this call when a short ingest bundle" in transcript_tool.description
    assert "Skip this call when the short ingest bundle" in tools[
        "video_list_moments"
    ].description
    frame_tool = tools["video_get_frame"]
    frame_properties = frame_tool.parameters["properties"]
    frame_required = set(frame_tool.parameters["required"])
    assert {"moment_id", "t"} <= frame_properties.keys()
    assert {"moment_id", "t"}.isdisjoint(frame_required)
    assert frame_properties["quality"]["default"] == "auto"
    assert "copy render_markdown byte-for-byte" in frame_tool.description
    assert "For a user-supplied selector, skip search" in frame_tool.description
    assert "never open a browser, run shell" in frame_tool.description
    assert "request permission" in frame_tool.description
    assert frame_tool.title == "Show or share a video photo, screenshot, still, or frame"
    assert "render_markdown byte-for-byte as the entire reply" in frame_tool.description
    assert frame_tool.parameters["$defs"]["FrameQuality"]["enum"] == [
        "auto",
        "probe",
        "source",
    ]
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
        "requested_quality",
        "evidence_quality",
        "width",
        "height",
        "render_path",
        "render_markdown",
        "render_expires_at",
    ):
        assert field in frame_tool.output_schema["properties"]
    assert (
        "must be the entire final response"
        in frame_tool.output_schema["properties"]["render_markdown"]["description"]
    )
    code_output = tools["video_get_code"].output_schema["properties"]
    assert tools["video_get_code"].title == "Extract code or terminal text only"
    assert "photo" not in tools["video_get_code"].description.lower()
    assert "screenshot" not in tools["video_get_code"].description.lower()
    assert "crop is already visual evidence" in tools["video_get_code"].description
    assert {"render_path", "render_markdown", "render_expires_at"} <= code_output.keys()
    search_hit_schema = tools["video_search"].output_schema["$defs"]["SearchHit"]
    assert "context" in search_hit_schema["properties"]
    assert "action_phase" in search_hit_schema["properties"]
    assert (
        "span multiple action phases" in search_hit_schema["properties"]["context"]["description"]
    )
    assert (
        "matched spoken cue at start_s"
        in search_hit_schema["properties"]["action_phase"]["description"]
    )
    assert "video_get_frame" in search_hit_schema["properties"]["moment_id"]["description"]
    for name in ("video_get_transcript", "video_search", "video_list_moments"):
        cursor_schema = tools[name].parameters["properties"]["cursor"]
        assert "byte-for-byte" in cursor_schema["description"]
        assert cursor_schema["anyOf"][0]["maxLength"] == 512
        assert tools[name].output_schema is not None
        output_cursor = tools[name].output_schema["properties"]["next_cursor"]
        assert "byte-for-byte" in output_cursor["description"]


def test_exact_frame_selector_skips_search_in_direct_server_guidance() -> None:
    server = create_server(FakeService())
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    ingest_guidance = tools["video_ingest"].output_schema["properties"]["retrieval_guidance"][
        "default"
    ]

    assert "preserve an exact selector and skip search" in server.instructions
    assert "preserve an exact selector" in ingest_guidance
    assert "For a user-supplied selector, skip search" in tools["video_get_frame"].description


def test_markdown_only_final_is_scoped_to_a_sole_image_deliverable() -> None:
    server = create_server(FakeService())
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    frame_markdown = tools["video_get_frame"].output_schema["properties"]["render_markdown"][
        "description"
    ]

    normalized_server = " ".join(server.instructions.lower().split())
    normalized_schema = " ".join(frame_markdown.lower().split())
    assert "one image is the sole deliverable" in normalized_server
    assert "sole requested deliverable is one image" in normalized_schema
    assert "image input is omitted or unsupported" in normalized_schema


def test_direct_frame_metadata_limits_vision_candidate_retrieval() -> None:
    server = create_server(FakeService())
    frame_description = {tool.name: tool for tool in server._tool_manager.list_tools()}[
        "video_get_frame"
    ].description

    assert "Never repeat the same visual selector" in server.instructions
    assert "image-capable model may inspect at most two distinct candidates" in frame_description
    assert "fetch the same selector twice" in frame_description


def test_server_metadata_exposes_multi_evidence_call_budget() -> None:
    server = create_server(FakeService())
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    assert "four searches" in server.instructions
    assert "two bounded transcript calls" in server.instructions
    assert "two visual calls" in server.instructions
    assert "four for accuracy" in server.instructions
    assert "Direct transcript/export requests" in server.instructions
    assert "paginate exact cues for the requested scope" in server.instructions
    assert "before/after claims" in server.instructions
    assert "sequential distinct images" in server.instructions

    descriptions = [tool.description for tool in tools.values()]
    assert len(server.instructions) < 2_500
    assert sum(map(len, descriptions)) < 4_000
    assert all("REQUEST FIDELITY" not in description for description in descriptions)
    assert all("four searches" not in description for description in descriptions)


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
async def test_structured_ingest_returns_short_bundle_without_image_content() -> None:
    server = create_server(BundledIngestService())
    unstructured, structured = await server._tool_manager.call_tool(
        "video_ingest", {"source": "/allowed/demo.mp4"}, convert_result=True
    )

    assert structured["evidence_bundle"]["transcript"]["view"] == "compact"
    assert structured["evidence_bundle"]["transcript"]["segments"][0]["text"] == (
        "short evidence"
    )
    assert all(block.type != "image" for block in unstructured)


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
async def test_transcript_tool_exposes_compact_view() -> None:
    class CompactTranscriptService(FakeService):
        def get_transcript(self, video_id: str, **kwargs: object) -> TranscriptPage:
            return TranscriptPage(
                video_id=video_id,
                segments=(),
                view=TranscriptView(kwargs["view"]),
            )

    server = create_server(CompactTranscriptService())
    result = await server._tool_manager.call_tool(
        "video_get_transcript",
        {"video_id": "demo", "view": "compact"},
        convert_result=True,
    )
    _unstructured, structured = result

    assert structured["view"] == "compact"


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
    assert [block.type for block in result.content] == ["text", "image", "text"]
    assert result.structuredContent is not None
    assert result.structuredContent["code"] == "print('hello')"
    assert result.structuredContent["render_path"].endswith("frame.jpg")
    assert result.content[2].text == result.structuredContent["render_markdown"]
    image_blocks = [block for block in result.content if isinstance(block, ImageContent)]
    assert len(image_blocks) == 1
    assert base64.b64decode(image_blocks[0].data) == b"image"


@pytest.mark.asyncio
async def test_frame_tool_accepts_exact_moment_and_returns_structured_ocr() -> None:
    server = create_server(FakeService())
    result = await server._tool_manager.call_tool(
        "video_get_frame",
        {"video_id": "demo", "moment_id": "moment"},
        convert_result=True,
    )

    assert isinstance(result, CallToolResult)
    assert [block.type for block in result.content] == ["text", "image", "text"]
    assert result.structuredContent is not None
    assert result.structuredContent["moment_id"] == "moment"
    assert result.structuredContent["requested_moment_id"] == "moment"
    assert result.structuredContent["requested_t"] is None
    assert result.structuredContent["start_s"] == 0
    assert result.structuredContent["end_s"] == 2
    assert result.structuredContent["ocr_text"] == "print('hello')"
    assert result.structuredContent["ocr_confidence"] == 0.9
    assert result.structuredContent["requested_quality"] == FrameQuality.AUTO.value
    assert result.structuredContent["evidence_quality"] == FrameEvidenceQuality.RETAINED.value
    assert result.structuredContent["width"] == 320
    assert result.structuredContent["height"] == 180
    assert result.structuredContent["render_path"].endswith("frame.jpg")
    assert result.structuredContent["render_markdown"].startswith("![Keyframe frame")
    assert result.content[2].text == result.structuredContent["render_markdown"]
    image_blocks = [block for block in result.content if isinstance(block, ImageContent)]
    assert len(image_blocks) == 1
    assert base64.b64decode(image_blocks[0].data) == b"image"


@pytest.mark.asyncio
async def test_remote_frame_fallback_never_requests_local_workspace_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_roots(_ctx: object) -> tuple[Path, ...]:
        raise AssertionError("remote frame retrieval must not ask for local roots")

    monkeypatch.setattr(server_module, "_client_roots", forbidden_roots)
    server = create_server(RemoteUncoveredFrameService())

    result = await server._tool_manager.call_tool(
        "video_get_frame",
        {"video_id": "remote-demo", "t": 30.0, "quality": "auto"},
        convert_result=True,
    )

    assert isinstance(result, CallToolResult)
    assert result.structuredContent is not None
    assert result.structuredContent["requested_t_covered"] is False


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
