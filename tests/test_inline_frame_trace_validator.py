from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evals.validate_inline_frame_trace import main, validate_trace

MARKDOWN = "![Keyframe frame at 01:18:52](</tmp/keyframe/rendered-frames/frame-abc.jpg>)"
RENDER_PATH = "/tmp/keyframe/rendered-frames/frame-abc.jpg"
IMAGE_BYTES = b"inline-frame-test-bytes"


def _event(event_type: str, item: dict[str, Any] | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {"type": event_type}
    if item is not None:
        event["item"] = item
    return event


def _call(
    item_id: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    result: dict[str, Any] | None = None,
    server: str = "keyframe",
) -> list[dict[str, Any]]:
    started = {
        "id": item_id,
        "type": "mcp_tool_call",
        "server": server,
        "tool": tool,
        "arguments": arguments,
        "result": None,
        "status": "in_progress",
    }
    completed = {
        **started,
        "result": result or {"structured_content": {}},
        "status": "completed",
    }
    return [_event("item.started", started), _event("item.completed", completed)]


def _valid_trace(
    *,
    final_text: str | None = None,
    ocr_text: str = "_ a -, a »_ @",
    ocr_confidence: float = 0.39,
) -> list[dict[str, Any]]:
    events = [_event("thread.started"), _event("turn.started")]
    skill_command = (
        "/bin/zsh -lc \"sed -n '1,220p' "
        "'/tmp/keyframe/skills/keyframe-video-rag/SKILL.md'\""
    )
    command_started = {
        "id": "skill",
        "type": "command_execution",
        "command": skill_command,
        "status": "in_progress",
    }
    events.extend(
        [
            _event("item.started", command_started),
            _event(
                "item.completed",
                {**command_started, "status": "completed", "exit_code": 0},
            ),
        ]
    )
    events.extend(
        _call(
            "ingest",
            "video_ingest",
            {
                "source": "https://youtu.be/s1fxZ-VWs2U",
                "mode": "fast",
                "transcript_mode": "auto",
            },
            result={
                "structured_content": {
                    "video_id": "youtube-s1fxZ-VWs2U",
                    "status": "ready",
                    "cache_hit": True,
                }
            },
        )
    )
    events.extend(
        _call(
            "search",
            "video_search",
            {
                "video_id": "youtube-s1fxZ-VWs2U",
                "query": "installing motherboard",
                "channel": "said",
                "start_s": 4_651,
                "end_s": 4_767,
            },
            result={
                "structured_content": {
                    "hits": [
                        {
                            "video_id": "youtube-s1fxZ-VWs2U",
                            "start_s": 4_679.159,
                            "channel": "said",
                            "action_phase": "announcement",
                        },
                        {
                            "video_id": "youtube-s1fxZ-VWs2U",
                            "start_s": 4_732.709,
                            "channel": "said",
                            "action_phase": "completed",
                        },
                    ]
                }
            },
        )
    )
    events.extend(
        _call(
            "frame",
            "video_get_frame",
            {
                "video_id": "youtube-s1fxZ-VWs2U",
                "t": 4_732.709,
                "region": "full",
                "quality": "auto",
            },
            result={
                "content": [
                    {
                        "type": "image",
                        "data": base64.b64encode(IMAGE_BYTES).decode("ascii"),
                        "mimeType": "image/jpeg",
                    },
                    {"type": "text", "text": MARKDOWN},
                ],
                "structured_content": {
                    "video_id": "youtube-s1fxZ-VWs2U",
                    "start_s": 4_729.0,
                    "end_s": 4_735.0,
                    "requested_t": 4_732.709,
                    "actual_t": 4_732.0,
                    "requested_t_covered": True,
                    "ocr_text": ocr_text,
                    "ocr_confidence": ocr_confidence,
                    "render_path": RENDER_PATH,
                    "render_markdown": MARKDOWN,
                }
            },
        )
    )
    events.append(
        _event(
            "item.completed",
            {
                "id": "answer",
                "type": "agent_message",
                "text": final_text
                or f"{MARKDOWN}\n\nSource: Linus Tech Tips PC-build guide\nTimestamp: `01:18:52`",
            },
        )
    )
    events.append(_event("turn.completed"))
    return events


def test_valid_trace_passes_and_deduplicates_started_completed_items() -> None:
    report = validate_trace(_valid_trace(), elapsed_s=17.064)

    assert report.passed is True
    assert report.errors == ()
    assert report.tool_call_counts == {
        "video_get_frame": 1,
        "video_ingest": 1,
        "video_search": 1,
    }


def _tool_items(events: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    return [
        item
        for event in events
        if isinstance((item := event.get("item")), dict) and item.get("tool") == tool
    ]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("failed_ingest", "video_ingest must complete successfully"),
        ("not_ready", "status must be 'ready'"),
        ("cold_cache", "cache_hit=true"),
        ("search_video_id", "video_search must reuse the ingest video_id"),
        ("frame_video_id", "video_get_frame must reuse the ingest video_id"),
        ("wrong_selected_hit", "must equal the preferred search hit start_s"),
        ("missing_actual_t", "must include numeric requested_t, actual_t"),
        ("uncovered", "requested_t_covered=true"),
    ],
)
def test_rejects_broken_success_and_evidence_chain(mutation: str, expected: str) -> None:
    events = _valid_trace()
    if mutation == "failed_ingest":
        _tool_items(events, "video_ingest")[-1]["status"] = "failed"
        _tool_items(events, "video_ingest")[-1]["error"] = "boom"
    elif mutation == "not_ready":
        _tool_items(events, "video_ingest")[-1]["result"]["structured_content"][
            "status"
        ] = "processing"
    elif mutation == "cold_cache":
        _tool_items(events, "video_ingest")[-1]["result"]["structured_content"][
            "cache_hit"
        ] = False
    elif mutation == "search_video_id":
        for item in _tool_items(events, "video_search"):
            item["arguments"]["video_id"] = "youtube-wrong"
    elif mutation == "frame_video_id":
        for item in _tool_items(events, "video_get_frame"):
            item["arguments"]["video_id"] = "youtube-wrong"
    elif mutation == "wrong_selected_hit":
        for item in _tool_items(events, "video_get_frame"):
            item["arguments"]["t"] = 4_730.0
        frame_result = _tool_items(events, "video_get_frame")[-1]["result"][
            "structured_content"
        ]
        frame_result["requested_t"] = 4_730.0
    elif mutation == "missing_actual_t":
        del _tool_items(events, "video_get_frame")[-1]["result"]["structured_content"][
            "actual_t"
        ]
    elif mutation == "uncovered":
        _tool_items(events, "video_get_frame")[-1]["result"]["structured_content"][
            "requested_t_covered"
        ] = False

    report = validate_trace(events, elapsed_s=12.0)

    assert report.passed is False
    assert any(expected in error for error in report.errors)


def test_rejects_out_of_order_evidence_calls() -> None:
    events = _valid_trace()
    search_positions = [
        index
        for index, event in enumerate(events)
        if event.get("item", {}).get("tool") == "video_search"
    ]
    frame_positions = [
        index
        for index, event in enumerate(events)
        if event.get("item", {}).get("tool") == "video_get_frame"
    ]
    search_pair = [events[index] for index in search_positions]
    frame_pair = [events[index] for index in frame_positions]
    for index, replacement in zip(
        search_positions + frame_positions,
        frame_pair + search_pair,
        strict=True,
    ):
        events[index] = replacement

    report = validate_trace(events, elapsed_s=12.0)

    assert report.passed is False
    assert "Keyframe calls must run in ingest -> search -> frame order" in report.errors


@pytest.mark.parametrize("image_count", [0, 2])
def test_requires_exactly_one_mcp_image(image_count: int) -> None:
    events = _valid_trace()
    content = _tool_items(events, "video_get_frame")[-1]["result"]["content"]
    image = content[0]
    content[:] = ([image] * image_count) + [content[-1]]

    report = validate_trace(events, elapsed_s=12.0)

    assert report.passed is False
    assert any("exactly one MCP image block" in error for error in report.errors)


def test_verifies_live_render_file_hash_and_image_byte_identity(tmp_path: Path) -> None:
    image_bytes = b"deterministic-rendered-frame"
    digest = hashlib.sha256(image_bytes).hexdigest()
    render_path = tmp_path / f"frame-{digest}.jpg"
    render_path.write_bytes(image_bytes)
    markdown = f"![Keyframe frame at 01:18:52](<{render_path}>)"
    events = _valid_trace(final_text=f"{markdown}\n\nTimestamp: 01:18:52")
    frame_item = _tool_items(events, "video_get_frame")[-1]
    frame_item["result"]["content"][0]["data"] = base64.b64encode(image_bytes).decode(
        "ascii"
    )
    frame_item["result"]["content"][-1]["text"] = markdown
    frame_item["result"]["structured_content"]["render_path"] = str(render_path)
    frame_item["result"]["structured_content"]["render_markdown"] = markdown

    report = validate_trace(events, elapsed_s=12.0, require_render_path=True)

    assert report.passed is True

    frame_item["result"]["content"][0]["data"] = base64.b64encode(b"different").decode(
        "ascii"
    )
    mismatch = validate_trace(events, elapsed_s=12.0, require_render_path=True)
    assert mismatch.passed is False
    assert "MCP image bytes do not equal render_path bytes" in mismatch.errors


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("latency", "elapsed_s must be <= 30"),
        ("forbidden_tool", "forbidden tool video_get_code"),
        ("duplicate_frame", "expected exactly one video_get_frame call, got 2"),
        ("browser", "non-Keyframe MCP workaround call"),
        ("shell", "forbidden shell/terminal action"),
        ("permission", "forbidden permission action event"),
    ],
)
def test_rejects_latency_duplicate_and_workaround_actions(mutation: str, expected: str) -> None:
    events = _valid_trace()
    elapsed = 30.001 if mutation == "latency" else 12.0
    if mutation == "forbidden_tool":
        events.extend(_call("code", "video_get_code", {"t": 4_732.0}))
    elif mutation == "duplicate_frame":
        events.extend(
            _call(
                "frame-2",
                "video_get_frame",
                {"t": 4_733.0, "region": "full", "quality": "auto"},
            )
        )
    elif mutation == "browser":
        events.extend(_call("browser", "open", {"url": "https://youtube.com"}, server="browser"))
    elif mutation == "shell":
        events.append(
            _event(
                "item.completed",
                {
                    "id": "download",
                    "type": "command_execution",
                    "command": "yt-dlp https://youtu.be/s1fxZ-VWs2U",
                },
            )
        )
    elif mutation == "permission":
        events.append(_event("permission.requested"))

    report = validate_trace(events, elapsed_s=elapsed)

    assert report.passed is False
    assert any(expected in error for error in report.errors)


@pytest.mark.parametrize(
    ("tool", "argument", "value", "expected"),
    [
        ("video_search", "channel", "shown", "channel must be 'said'"),
        ("video_search", "start_s", 4_650, "bounds must stay inside"),
        ("video_search", "end_s", 4_768, "bounds must stay inside"),
        ("video_search", "start_s", float("nan"), "must be finite"),
        ("video_get_frame", "t", 4_724.9, "t must be in"),
        ("video_get_frame", "t", 4_741.1, "t must be in"),
        ("video_get_frame", "region", "auto_crop", "region must be 'full'"),
        ("video_get_frame", "quality", "source", "quality must be 'auto'"),
    ],
)
def test_rejects_wrong_search_or_frame_selection(
    tool: str,
    argument: str,
    value: Any,
    expected: str,
) -> None:
    events = _valid_trace()
    for event in events:
        item = event.get("item", {})
        if item.get("type") == "mcp_tool_call" and item.get("tool") == tool:
            item["arguments"][argument] = value

    report = validate_trace(events, elapsed_s=12.0)

    assert report.passed is False
    assert any(expected in error for error in report.errors)


def test_requires_exact_angle_bracketed_render_markdown_in_final_output() -> None:
    altered = MARKDOWN.replace("](<", "](").replace(".jpg>)", ".jpg)")
    report = validate_trace(_valid_trace(final_text=altered), elapsed_s=12.0)

    assert report.passed is False
    assert "final agent output must contain returned render_markdown exactly once" in report.errors


@pytest.mark.parametrize(
    "claim",
    [
        "I selected the clearest frame.",
        "The photo shows the motherboard in the case.",
        "The motherboard is black.",
        "The CPU is near the lower center.",
        "This frame is a wide close-up.",
    ],
)
def test_rejects_no_vision_quality_component_and_layout_claims(claim: str) -> None:
    report = validate_trace(
        _valid_trace(final_text=f"{MARKDOWN}\n\n{claim}"),
        elapsed_s=12.0,
    )

    assert report.passed is False
    assert any("claim" in error for error in report.errors)


def test_allows_requested_quality_as_a_progress_goal_but_not_a_final_claim() -> None:
    events = _valid_trace()
    events.insert(
        2,
        _event(
            "item.completed",
            {
                "id": "progress",
                "type": "agent_message",
                "text": "I'll try to retrieve the requested clear frame.",
            },
        ),
    )

    report = validate_trace(events, elapsed_s=12.0)

    assert report.passed is True


def test_rejects_low_confidence_ocr_even_when_labeled() -> None:
    report = validate_trace(
        _valid_trace(final_text=f"{MARKDOWN}\n\nTesseract OCR: diagram-like text"),
        elapsed_s=12.0,
    )

    assert report.passed is False
    assert any("low-confidence or meaningless" in error for error in report.errors)


def test_accepts_meaningful_verbatim_tesseract_ocr() -> None:
    report = validate_trace(
        _valid_trace(
            final_text=f"{MARKDOWN}\n\nTesseract OCR: Motherboard Installation",
            ocr_text="STEP 9 Motherboard Installation",
            ocr_confidence=0.91,
        ),
        elapsed_s=12.0,
    )

    assert report.passed is True


def test_cli_returns_one_for_behavioral_failure_and_two_for_bad_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        "\n".join(json.dumps(event) for event in _valid_trace()) + "\n",
        encoding="utf-8",
    )

    assert main([str(trace_path), "--elapsed-s", "31"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is False
    assert main([str(tmp_path / "missing.jsonl"), "--elapsed-s", "1"]) == 2
    assert "could not read" in capsys.readouterr().err
