"""Validate the macOS no-vision inline-frame regression trace.

Run after a warm-cache Codex evaluation and pass the separately measured wall
time (Codex JSONL does not contain a trustworthy end-to-end duration):

    python evals/validate_inline_frame_trace.py trace.jsonl --elapsed-s 17.064

The command prints one deterministic JSON report and exits with status 1 when
the trace violates the regression contract. Status 2 means the input itself
could not be read.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MOTHERBOARD_CHAPTER_START_S = 4_651.0
MOTHERBOARD_CHAPTER_END_S = 4_767.0
FRAME_MIN_T = 4_725.0
FRAME_MAX_T = 4_741.0
MAX_ELAPSED_S = 30.0

EXPECTED_TOOLS = ("video_ingest", "video_search", "video_get_frame")
FORBIDDEN_KEYFRAME_TOOLS = (
    "video_get_code",
    "video_list_moments",
    "video_get_transcript",
)

_QUALITY_CLAIM = re.compile(
    r"\b(?:best|clear(?:er|est)?|clean(?:er|est)?|detailed|high[- ]quality|"
    r"representative|sharp(?:er|est)?|well[- ]lit)\b",
    re.IGNORECASE,
)
_VISUAL_CLAIM = re.compile(
    r"\b(?:appears?\s+to|contains?|depicts?|features?|pictured|shows?|"
    r"text\s+reads|visible|you\s+can\s+see)\b",
    re.IGNORECASE,
)
_COMPONENT = (
    r"(?:board|cable|case|chassis|connector|cooler|cpu|fan|gpu|graphics\s+card|"
    r"header|heatsink|memory|motherboard|power\s+supply|processor|psu|ram|"
    r"screw|slot|socket|standoff)"
)
_POSITION_OR_STATE = (
    r"(?:above|aligned|below|bottom|center(?:ed)?|connected|inside|installed|"
    r"left|lower|mounted|near|next\s+to|right|seated|top|upper)"
)
_COMPONENT_STATE_CLAIM = re.compile(
    rf"\b(?:{_COMPONENT}\b.{{0,70}}\b{_POSITION_OR_STATE}|"
    rf"{_POSITION_OR_STATE}\b.{{0,70}}\b{_COMPONENT})\b",
    re.IGNORECASE,
)
_FRAMING_CLAIM = re.compile(
    r"\b(?:frame|image|photo|shot|still)\b.{0,50}"
    r"\b(?:angle|close[- ]up|crop(?:ped)?|full[- ]frame|wide)\b",
    re.IGNORECASE,
)
_UNLABELED_TEXT_CLAIM = re.compile(
    r"^\s*(?:extracted\s+text|ocr|on[- ]screen\s+text|visible\s+text)\s*:",
    re.IGNORECASE,
)
_SHELL_CONTROL = re.compile(r"(?:&&|\|\||\$\(|`|[|;]|>>?|<)")
_READ_COMMAND = re.compile(r"\b(?:cat|Get-Content|head|sed|tail|type)\b", re.IGNORECASE)
_MEDIA_OR_MUTATING_COMMAND = re.compile(
    r"\b(?:browser|cp|curl|ffmpeg|ffprobe|mkdir|mv|open|osascript|playwright|"
    r"rm|screencapture|wget|yt-dlp)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ValidationReport:
    passed: bool
    elapsed_s: float
    tool_call_counts: dict[str, int]
    errors: tuple[str, ...]

    def to_json(self) -> str:
        """Return a stable, machine-readable representation."""

        return json.dumps(asdict(self), indent=2, sort_keys=True)


class TraceInputError(ValueError):
    """Raised when a JSONL trace is malformed rather than behaviorally invalid."""


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load nonblank JSON objects from *path*, reporting the failing line."""

    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TraceInputError(f"could not read {path}: {exc}") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraceInputError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise TraceInputError(f"{path}:{line_number}: expected a JSON object")
        events.append(event)
    if not events:
        raise TraceInputError(f"{path}: trace contains no JSON events")
    return events


def _logical_items(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse Codex's started/completed event pair into one logical item."""

    by_id: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    anonymous: list[tuple[int, dict[str, Any]]] = []
    for position, event in enumerate(events):
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", ""))
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            key = (item_type, item_id)
            if key in by_id:
                first_position, previous = by_id[key]
                merged = {**previous, **item}
                by_id[key] = (first_position, merged)
            else:
                by_id[key] = (position, dict(item))
        elif event.get("type") != "item.started":
            anonymous.append((position, dict(item)))
    return [item for _, item in sorted((*by_id.values(), *anonymous), key=lambda entry: entry[0])]


def _structured_result(item: dict[str, Any]) -> dict[str, Any]:
    result = _tool_result(item)
    if not result:
        return {}

    for key in ("structured_content", "structuredContent"):
        structured = result.get(key)
        if isinstance(structured, str):
            try:
                structured = json.loads(structured)
            except json.JSONDecodeError:
                structured = None
        if isinstance(structured, dict):
            return structured
    if "render_markdown" in result:
        return result

    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    return {}


def _tool_result(item: dict[str, Any]) -> dict[str, Any]:
    """Return a decoded MCP result envelope when the trace contains one."""

    result = item.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return {}
    if not isinstance(result, dict):
        return {}
    return result


def _is_allowed_skill_load(command: str) -> bool:
    if "SKILL.md" not in command or not _READ_COMMAND.search(command):
        return False
    if _MEDIA_OR_MUTATING_COMMAND.search(command):
        return False
    # Shell wrappers add quotes and -lc, but a skill read never needs a pipeline,
    # redirection, command chain, or substitution.
    return not _SHELL_CONTROL.search(command)


def _meaningful_ocr(text: str, confidence: Any) -> bool:
    try:
        score = float(confidence)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(score) or score < 0.5:
        return False
    tokens = re.findall(r"[A-Za-z0-9]+", text)
    alpha_tokens = [token for token in tokens if re.search(r"[A-Za-z]", token)]
    alpha_chars = sum(sum(character.isalpha() for character in token) for token in alpha_tokens)
    return bool(alpha_tokens) and alpha_chars >= 4 and len(alpha_tokens) / max(len(tokens), 1) >= 0.5


def _validate_agent_text(
    agent_texts: Sequence[str],
    frame_result: dict[str, Any],
    render_markdown: str,
) -> list[str]:
    errors: list[str] = []
    source_ocr = str(frame_result.get("ocr_text", ""))
    source_ocr_meaningful = _meaningful_ocr(
        source_ocr,
        frame_result.get("ocr_confidence"),
    )
    source_tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9]+", source_ocr)}

    final_message_number = len(agent_texts)
    for message_number, text in enumerate(agent_texts, start=1):
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped == render_markdown:
                continue
            if stripped.lower().startswith(("provenance:", "source:", "timestamp:")):
                continue
            if stripped.startswith("Tesseract OCR:"):
                payload = stripped.removeprefix("Tesseract OCR:").strip().strip("`\"' ")
                if not source_ocr_meaningful or not _meaningful_ocr(payload, 1.0):
                    errors.append(
                        f"agent message {message_number} reports OCR that is low-confidence or meaningless"
                    )
                    continue
                claimed_tokens = {
                    token.lower() for token in re.findall(r"[A-Za-z0-9]+", payload)
                }
                if not claimed_tokens or not claimed_tokens <= source_tokens:
                    errors.append(
                        f"agent message {message_number} reports Tesseract OCR not present in the frame result"
                    )
                continue
            if "ocr" in stripped.lower() or _UNLABELED_TEXT_CLAIM.search(stripped):
                errors.append(
                    f"agent message {message_number} contains an unlabeled or non-verbatim OCR claim"
                )
                continue
            # A pre-retrieval progress message can repeat the user's desired quality as a goal;
            # only the final no-vision answer can falsely assert that the returned pixels meet it.
            if message_number == final_message_number and _QUALITY_CLAIM.search(stripped):
                errors.append(f"agent message {message_number} makes a visual-quality claim")
            if _VISUAL_CLAIM.search(stripped):
                errors.append(f"agent message {message_number} makes an unsupported visual claim")
            if _COMPONENT_STATE_CLAIM.search(stripped):
                errors.append(
                    f"agent message {message_number} makes an unsupported component/state claim"
                )
            if message_number == final_message_number and _FRAMING_CLAIM.search(stripped):
                errors.append(f"agent message {message_number} makes an unsupported framing claim")
            if message_number == final_message_number:
                errors.append(
                    f"agent message {message_number} makes an unsupported free-form final claim"
                )
    return errors


def _absolute_render_path(value: str) -> bool:
    """Recognize absolute POSIX and Windows paths on any validation host."""

    return Path(value).is_absolute() or bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _validate_render_artifact(
    frame_item: dict[str, Any],
    frame_result: dict[str, Any],
    render_markdown: str,
    *,
    require_render_path: bool,
) -> list[str]:
    """Validate that the result exposes one image and the same render artifact."""

    errors: list[str] = []
    render_path_value = frame_result.get("render_path")
    if not isinstance(render_path_value, str) or not render_path_value:
        return ["video_get_frame result is missing render_path"]
    if not _absolute_render_path(render_path_value):
        errors.append("video_get_frame render_path must be absolute")

    markdown_match = re.fullmatch(r"!\[[^\]\r\n]+\]\(<([^<>\r\n]+)>\)", render_markdown)
    if markdown_match is None:
        errors.append("render_markdown must use an angle-bracketed image destination")
    elif markdown_match.group(1) != render_path_value:
        errors.append("render_markdown destination must equal render_path byte-for-byte")

    result = _tool_result(frame_item)
    content = result.get("content")
    if not isinstance(content, list):
        errors.append("video_get_frame result is missing MCP content blocks")
        content = []
    image_blocks: list[dict[str, Any]] = []
    omitted_image_blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            image_blocks.append(block)
        elif block.get("type") == "text" and "image content omitted" in str(
            block.get("text", "")
        ).casefold():
            omitted_image_blocks.append(block)
    if len(image_blocks) + len(omitted_image_blocks) != 1:
        errors.append("video_get_frame must expose exactly one MCP image block or host placeholder")
    standalone_markdown = [
        block
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and block.get("text") == render_markdown
    ]
    if len(standalone_markdown) != 1 or not content or content[-1] != standalone_markdown[0]:
        errors.append("video_get_frame must end with exactly one standalone render_markdown block")

    render_path = Path(render_path_value)
    artifact_bytes: bytes | None = None
    if render_path.exists():
        try:
            artifact_bytes = render_path.read_bytes()
        except OSError as exc:
            errors.append(f"could not read render_path: {exc}")
    elif require_render_path:
        errors.append("render_path does not exist at validation time")

    if artifact_bytes is not None:
        filename_match = re.fullmatch(r"frame-([0-9a-f]{64})\.[A-Za-z0-9]+", render_path.name)
        if filename_match is None:
            errors.append("render_path filename must contain the image SHA-256")
        elif hashlib.sha256(artifact_bytes).hexdigest() != filename_match.group(1):
            errors.append("render_path filename hash does not match its bytes")

    if len(image_blocks) == 1:
        encoded = image_blocks[0].get("data")
        if not isinstance(encoded, str) or not encoded:
            errors.append("MCP image block is missing base64 data")
        else:
            try:
                image_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                errors.append("MCP image block contains invalid base64 data")
            else:
                if artifact_bytes is not None and image_bytes != artifact_bytes:
                    errors.append("MCP image bytes do not equal render_path bytes")
    return errors


def validate_trace(
    events: Sequence[dict[str, Any]],
    elapsed_s: float,
    *,
    require_render_path: bool = False,
) -> ValidationReport:
    """Validate one no-vision motherboard-frame Codex JSONL event sequence."""

    errors: list[str] = []
    if not math.isfinite(elapsed_s) or elapsed_s < 0:
        errors.append("elapsed_s must be a finite non-negative number")
    elif elapsed_s > MAX_ELAPSED_S:
        errors.append(f"elapsed_s must be <= {MAX_ELAPSED_S:g}, got {elapsed_s:g}")

    logical_items = _logical_items(events)
    mcp_calls = [item for item in logical_items if item.get("type") == "mcp_tool_call"]
    tool_counts = Counter(str(item.get("tool", "")) for item in mcp_calls)

    for tool in EXPECTED_TOOLS:
        if tool_counts[tool] != 1:
            errors.append(f"expected exactly one {tool} call, got {tool_counts[tool]}")
    for tool in FORBIDDEN_KEYFRAME_TOOLS:
        if tool_counts[tool] != 0:
            errors.append(f"forbidden tool {tool} was called {tool_counts[tool]} time(s)")

    for item in mcp_calls:
        server = str(item.get("server", ""))
        if server.lower() != "keyframe":
            errors.append(
                f"non-Keyframe MCP workaround call: server={server!r}, tool={item.get('tool')!r}"
            )

    expected_calls = {
        tool: next((item for item in mcp_calls if item.get("tool") == tool), None)
        for tool in EXPECTED_TOOLS
    }
    if all(expected_calls.values()):
        positions = {
            tool: logical_items.index(item)
            for tool, item in expected_calls.items()
            if item is not None
        }
        if not (
            positions["video_ingest"]
            < positions["video_search"]
            < positions["video_get_frame"]
        ):
            errors.append("Keyframe calls must run in ingest -> search -> frame order")
    for tool, item in expected_calls.items():
        if item is None:
            continue
        if item.get("status") != "completed" or item.get("error") not in (None, ""):
            errors.append(f"{tool} must complete successfully")
        if not _tool_result(item):
            errors.append(f"{tool} must return a result")

    commands = [item for item in logical_items if item.get("type") == "command_execution"]
    allowed_skill_loads = 0
    for item in commands:
        command = str(item.get("command", ""))
        if _is_allowed_skill_load(command):
            allowed_skill_loads += 1
        else:
            errors.append(f"forbidden shell/terminal action: {command!r}")
    if allowed_skill_loads > 1:
        errors.append(f"expected at most one read-only SKILL.md load, got {allowed_skill_loads}")

    forbidden_item_terms = ("approval", "browser", "permission", "web_search", "web-search")
    for item in logical_items:
        item_type = str(item.get("type", "")).lower()
        if any(term in item_type for term in forbidden_item_terms):
            errors.append(f"forbidden workaround action type: {item_type!r}")
        if item_type not in {"agent_message", "command_execution", "error", "mcp_tool_call"}:
            routing_text = " ".join(
                str(item.get(key, "")) for key in ("name", "server", "tool")
            ).lower()
            if any(term in routing_text for term in ("browser", "permission", "web")):
                errors.append(f"forbidden workaround tool routing: {routing_text!r}")
    for event in events:
        event_type = str(event.get("type", "")).lower()
        if any(term in event_type for term in ("approval", "permission")):
            errors.append(f"forbidden permission action event: {event_type!r}")

    ingests = [item for item in mcp_calls if item.get("tool") == "video_ingest"]
    ingest_result: dict[str, Any] = {}
    video_id = ""
    if len(ingests) == 1:
        ingest_result = _structured_result(ingests[0])
        video_id_value = ingest_result.get("video_id")
        if isinstance(video_id_value, str) and video_id_value:
            video_id = video_id_value
        else:
            errors.append("video_ingest result is missing video_id")
        if ingest_result.get("status") != "ready":
            errors.append("video_ingest result status must be 'ready'")
        if ingest_result.get("cache_hit") is not True:
            errors.append("warm-cache video_ingest result must report cache_hit=true")

    searches = [item for item in mcp_calls if item.get("tool") == "video_search"]
    selected_hit: dict[str, Any] = {}
    if len(searches) == 1:
        arguments = searches[0].get("arguments")
        if not isinstance(arguments, dict):
            errors.append("video_search arguments are missing")
        else:
            if video_id and arguments.get("video_id") != video_id:
                errors.append("video_search must reuse the ingest video_id byte-for-byte")
            if arguments.get("channel") != "said":
                errors.append("video_search channel must be 'said'")
            try:
                start_s = float(arguments["start_s"])
                end_s = float(arguments["end_s"])
            except (KeyError, TypeError, ValueError):
                errors.append("video_search must include numeric start_s and end_s")
            else:
                if not math.isfinite(start_s) or not math.isfinite(end_s):
                    errors.append("video_search start_s and end_s must be finite")
                elif (
                    start_s < MOTHERBOARD_CHAPTER_START_S
                    or end_s > MOTHERBOARD_CHAPTER_END_S
                ):
                    errors.append(
                        "video_search bounds must stay inside the motherboard chapter "
                        f"[{MOTHERBOARD_CHAPTER_START_S:g}, {MOTHERBOARD_CHAPTER_END_S:g}]"
                    )
                if start_s >= end_s:
                    errors.append("video_search start_s must be less than end_s")

        search_result = _structured_result(searches[0])
        hits = search_result.get("hits")
        if not isinstance(hits, list) or not hits:
            errors.append("video_search result must contain hits")
        else:
            eligible_hits = [
                hit
                for hit in hits
                if isinstance(hit, dict)
                and hit.get("channel") == "said"
                and hit.get("video_id") == video_id
            ]
            completed_hits = [
                hit for hit in eligible_hits if hit.get("action_phase") == "completed"
            ]
            in_progress_hits = [
                hit for hit in eligible_hits if hit.get("action_phase") == "in_progress"
            ]
            if completed_hits:
                selected_hit = completed_hits[0]
            elif in_progress_hits:
                selected_hit = in_progress_hits[0]
            else:
                errors.append(
                    "video_search result needs a completed or in_progress said action hit"
                )

    frames = [item for item in mcp_calls if item.get("tool") == "video_get_frame"]
    frame_result: dict[str, Any] = {}
    render_markdown = ""
    if len(frames) == 1:
        arguments = frames[0].get("arguments")
        if not isinstance(arguments, dict):
            errors.append("video_get_frame arguments are missing")
        else:
            if video_id and arguments.get("video_id") != video_id:
                errors.append("video_get_frame must reuse the ingest video_id byte-for-byte")
            try:
                frame_t = float(arguments["t"])
            except (KeyError, TypeError, ValueError):
                errors.append("video_get_frame must select a numeric t")
            else:
                if not FRAME_MIN_T <= frame_t <= FRAME_MAX_T:
                    errors.append(
                        f"video_get_frame t must be in [{FRAME_MIN_T:g}, {FRAME_MAX_T:g}], "
                        f"got {frame_t:g}"
                    )
                if selected_hit:
                    try:
                        selected_t = float(selected_hit["start_s"])
                    except (KeyError, TypeError, ValueError):
                        errors.append("selected search hit is missing numeric start_s")
                    else:
                        if not math.isclose(frame_t, selected_t, rel_tol=0, abs_tol=1e-6):
                            errors.append(
                                "video_get_frame t must equal the preferred search hit start_s"
                            )
            if "moment_id" in arguments and arguments.get("moment_id") is not None:
                errors.append("video_get_frame must select by t, not moment_id")
            if arguments.get("region") != "full":
                errors.append("video_get_frame region must be 'full'")
            if arguments.get("quality") != "auto":
                errors.append("video_get_frame quality must be 'auto'")

        frame_result = _structured_result(frames[0])
        if video_id and frame_result.get("video_id") != video_id:
            errors.append("video_get_frame result must preserve the ingest video_id")
        try:
            requested_t = float(frame_result["requested_t"])
            actual_t = float(frame_result["actual_t"])
            result_start_s = float(frame_result["start_s"])
            result_end_s = float(frame_result["end_s"])
        except (KeyError, TypeError, ValueError):
            errors.append(
                "video_get_frame result must include numeric requested_t, actual_t, start_s, and end_s"
            )
        else:
            if not all(
                math.isfinite(value)
                for value in (requested_t, actual_t, result_start_s, result_end_s)
            ):
                errors.append("video_get_frame result timestamps must be finite")
            if isinstance(arguments, dict):
                try:
                    argument_t = float(arguments["t"])
                except (KeyError, TypeError, ValueError):
                    pass
                else:
                    if not math.isclose(requested_t, argument_t, rel_tol=0, abs_tol=1e-6):
                        errors.append("video_get_frame requested_t must equal its t argument")
            if not FRAME_MIN_T <= actual_t <= FRAME_MAX_T:
                errors.append(
                    f"video_get_frame actual_t must be in [{FRAME_MIN_T:g}, {FRAME_MAX_T:g}]"
                )
            if not result_start_s <= actual_t <= result_end_s:
                errors.append("video_get_frame actual_t must fall inside its retained interval")
        if frame_result.get("requested_t_covered") is not True:
            errors.append("video_get_frame must report requested_t_covered=true")
        value = frame_result.get("render_markdown")
        if isinstance(value, str) and value:
            render_markdown = value
            errors.extend(
                _validate_render_artifact(
                    frames[0],
                    frame_result,
                    render_markdown,
                    require_render_path=require_render_path,
                )
            )
        else:
            errors.append("video_get_frame result is missing render_markdown")

    agent_texts = [
        str(item.get("text"))
        for item in logical_items
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str)
    ]
    if not agent_texts:
        errors.append("trace has no agent_message output")
    elif render_markdown:
        final_text = agent_texts[-1]
        if final_text.count(render_markdown) != 1:
            errors.append("final agent output must contain returned render_markdown exactly once")
        errors.extend(_validate_agent_text(agent_texts, frame_result, render_markdown))

    # Keep messages deterministic even when one item violates overlapping rules.
    unique_errors = tuple(dict.fromkeys(errors))
    return ValidationReport(
        passed=not unique_errors,
        elapsed_s=elapsed_s,
        tool_call_counts=dict(sorted(tool_counts.items())),
        errors=unique_errors,
    )


def validate_trace_file(path: Path, elapsed_s: float) -> ValidationReport:
    return validate_trace(load_jsonl(path), elapsed_s, require_render_path=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="Codex --json JSONL trace")
    parser.add_argument(
        "--elapsed-s",
        type=float,
        required=True,
        help="separately measured end-to-end agent wall time in seconds",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = validate_trace_file(args.trace, args.elapsed_s)
    except TraceInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(report.to_json())
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
