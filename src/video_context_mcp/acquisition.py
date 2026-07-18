"""Validated local and remote video acquisition.

This module owns the security boundary between untrusted source strings and the
extraction pipeline.  It performs metadata-only checks before a remote download,
never asks yt-dlp for cookies or credentials, and makes ownership of temporary
media explicit through :class:`AcquiredSource`.
"""

from __future__ import annotations

import hashlib
import html
import http.client
import ipaddress
import json
import logging
import math
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, cast
from urllib.parse import SplitResult, urljoin, urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from video_context_mcp.config import Settings
from video_context_mcp.constants import (
    MAX_CONFIGURABLE_DURATION_S,
    MAX_SOURCE_FRAME_EDGE,
    MAX_SOURCE_FRAME_PIXELS,
    SUPPORTED_VIDEO_EXTENSIONS,
)
from video_context_mcp.errors import ConfigurationError, SourceError

logger = logging.getLogger(__name__)
_SOCKET_DEFAULT_TIMEOUT: Any = socket._GLOBAL_DEFAULT_TIMEOUT  # type: ignore[attr-defined]

type AcquisitionMode = Literal["fast", "full"]
type TranscriptMode = Literal["auto", "captions", "whisper", "none"]
type Availability = Literal["local", "public", "unlisted"]

_SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
_TEXT_SUBTITLE_CODECS = {
    "ass",
    "mov_text",
    "srt",
    "ssa",
    "subrip",
    "text",
    "ttml",
    "webvtt",
}
_TIMELINE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})(?:\s+.*)?$"
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[^\S\r\n]+")
_IN_PROCESS_YTDLP_PROTOCOLS = frozenset(
    {
        "http",
        "https",
        "m3u8_native",
        "http_dash_segments",
        "http_dash_segments_generator",
    }
)


class SourceKind(StrEnum):
    """A normalized acquisition strategy."""

    LOCAL = "local"
    DIRECT = "direct"
    YOUTUBE = "youtube"
    LOOM = "loom"


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """A timestamped transcript segment, in seconds."""

    start_s: float
    end_s: float
    text: str
    language: str | None = None
    origin: str = "captions"


@dataclass(frozen=True, slots=True)
class Chapter:
    """A named interval supplied by the source provider."""

    start_s: float
    end_s: float
    title: str


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Provider-independent metadata needed by the pipeline and cache."""

    source: str
    kind: SourceKind
    video_id: str
    title: str
    duration_s: float
    provider: str
    webpage_url: str | None = None
    uploader: str | None = None
    file_size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    content_sha256: str | None = None
    content_mtime_ns: int | None = None
    availability: Availability = "public"


@dataclass(slots=True)
class AcquiredSource:
    """A validated source and any temporary media owned by Keyframe.

    Call :meth:`cleanup` (or use this object as a context manager) after the
    extraction pipeline has finished.  Cleanup is idempotent and never removes a
    caller-owned local file.
    """

    metadata: SourceMetadata
    transcript: tuple[TranscriptSegment, ...] = ()
    chapters: tuple[Chapter, ...] = ()
    media_path: Path | None = None
    warnings: tuple[str, ...] = ()
    owns_media: bool = False
    _temp_dir: Path | None = field(default=None, repr=False)

    def cleanup(self) -> None:
        temp_dir = self._temp_dir
        if temp_dir is not None:
            _remove_owned_temp_dir(temp_dir)
            self._temp_dir = None
        if self.owns_media:
            self.media_path = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.cleanup()
        except SourceError:
            if exc_type is None:
                raise
            logger.exception("Temporary media cleanup also failed while handling %s", exc_type)


def _validate_modes(mode: str, transcript_mode: str) -> None:
    if mode not in {"fast", "full"}:
        raise SourceError(f"Unsupported ingestion mode {mode!r}; expected 'fast' or 'full'.")
    if transcript_mode not in {"auto", "captions", "whisper", "none"}:
        raise SourceError(
            "Unsupported transcript mode "
            f"{transcript_mode!r}; expected auto, captions, whisper, or none."
        )


def _effective_duration_limit(settings: Settings, requested: int | None) -> int:
    limit = settings.default_max_duration_s if requested is None else requested
    if limit <= 0 or limit > MAX_CONFIGURABLE_DURATION_S:
        raise SourceError(
            f"max_duration_s must be between 1 and {MAX_CONFIGURABLE_DURATION_S}, got {limit}."
        )
    return limit


def validate_local_path(source: str | Path, settings: Settings) -> Path:
    """Resolve and validate a caller-owned video path against configured roots."""

    candidate = Path(source).expanduser()
    if not settings.allowed_roots:
        raise SourceError(
            "No local-video roots are authorized. Open the file or its directory as an MCP "
            "workspace root, or set KEYFRAME_ALLOWED_ROOTS explicitly."
        )
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise SourceError(
                f"Local video does not exist or cannot be resolved: {candidate}"
            ) from exc
    else:
        matches: list[Path] = []
        for root in settings.allowed_roots:
            try:
                if root.is_dir():
                    possible = (root / candidate).resolve(strict=True)
                elif root.is_file():
                    possible = (root.parent / candidate).resolve(strict=True)
                    if possible != root:
                        continue
                else:
                    continue
            except OSError:
                continue
            if possible not in matches:
                matches.append(possible)
        if not matches:
            raise SourceError(
                f"Relative local video {candidate!s} was not found under any authorized root."
            )
        if len(matches) > 1:
            choices = ", ".join(str(path) for path in matches)
            raise SourceError(
                f"Relative local video {candidate!s} is ambiguous across authorized roots "
                f"({choices}); use an absolute path."
            )
        resolved = matches[0]
    if not resolved.is_file():
        raise SourceError(f"Local video must be a regular file: {resolved}")
    if resolved.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_VIDEO_EXTENSIONS))
        raise SourceError(
            f"Unsupported local video extension {resolved.suffix!r}; use one of {supported}."
        )
    if not any(resolved.is_relative_to(root) for root in settings.allowed_roots):
        roots = ", ".join(str(root) for root in settings.allowed_roots)
        raise SourceError(f"Local video is outside KEYFRAME_ALLOWED_ROOTS ({roots}): {resolved}")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise SourceError(f"Could not inspect local video: {resolved}") from exc
    if size > settings.max_local_file_bytes:
        raise SourceError(
            f"Local video is {size} bytes, above the configured "
            f"{settings.max_local_file_bytes}-byte limit."
        )
    return resolved


def _split_http_url(source: str) -> SplitResult:
    try:
        parsed = urlsplit(source)
        _ = parsed.port
    except ValueError as exc:
        raise SourceError(f"Invalid source URL: {source!r}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SourceError("Remote video URLs must use http or https.")
    if not parsed.hostname:
        raise SourceError("Remote video URL must include a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise SourceError("Credentials in video URLs are not supported.")
    if parsed.fragment:
        raise SourceError("Video URL fragments are not supported; remove the #fragment.")
    return parsed


def _is_disallowed_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    return not ip.is_global


def validate_remote_url(
    source: str,
    *,
    allow_private: bool = False,
    resolve_dns: bool = True,
) -> str:
    """Validate an HTTP(S) URL and reject private-network targets by default."""

    normalized = source.strip()
    parsed = _split_http_url(normalized)
    hostname = cast(str, parsed.hostname).rstrip(".").lower()
    if allow_private:
        return normalized

    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise SourceError("Loopback video URLs are blocked by default.")

    try:
        literal_address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal_address = None
    if literal_address is not None:
        if not literal_address.is_global:
            raise SourceError("Private, loopback, link-local, and reserved video URLs are blocked.")
        return normalized

    if not resolve_dns:
        return normalized

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        addresses = {
            str(item[4][0]) for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise SourceError(f"Could not resolve video URL hostname {hostname!r}.") from exc
    if not addresses:
        raise SourceError(f"Video URL hostname {hostname!r} did not resolve to an address.")
    if any(_is_disallowed_address(address) for address in addresses):
        raise SourceError(
            f"Video URL hostname {hostname!r} resolves to a private or non-public address."
        )
    return normalized


def classify_source(source: str | Path) -> SourceKind:
    """Classify a source without performing DNS or filesystem I/O."""

    if isinstance(source, Path):
        return SourceKind.LOCAL
    value = source.strip()
    if not value:
        raise SourceError("Video source must not be empty.")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        if "://" in value:
            raise SourceError("Remote video URLs must use http or https.")
        return SourceKind.LOCAL
    parsed = _split_http_url(value)
    host = cast(str, parsed.hostname).rstrip(".").lower()
    if (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    ):
        return SourceKind.YOUTUBE
    if host == "loom.com" or host.endswith(".loom.com"):
        return SourceKind.LOOM
    return SourceKind.DIRECT


def acquire_source(
    source: str | Path,
    settings: Settings,
    *,
    mode: AcquisitionMode = "fast",
    transcript_mode: TranscriptMode = "auto",
    max_duration_s: int | None = None,
    refresh: bool = False,
) -> AcquiredSource:
    """Validate and acquire a local file or supported remote video."""

    _validate_modes(mode, transcript_mode)
    limit = _effective_duration_limit(settings, max_duration_s)
    kind = classify_source(source)
    if kind is SourceKind.LOCAL:
        return acquire_local(
            source,
            settings,
            mode=mode,
            transcript_mode=transcript_mode,
            max_duration_s=limit,
        )
    return acquire_remote(
        str(source),
        settings,
        mode=mode,
        transcript_mode=transcript_mode,
        max_duration_s=limit,
        refresh=refresh,
    )


def _run_json_command(command: Sequence[str], *, label: str) -> Mapping[str, Any]:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise ConfigurationError(f"{label} executable was not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceError(f"{label} timed out while inspecting the video.") from exc
    if result.returncode != 0:
        detail = (
            result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        )
        raise SourceError(f"{label} could not inspect the video: {detail}")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SourceError(f"{label} returned invalid JSON while inspecting the video.") from exc
    if not isinstance(parsed, Mapping):
        raise SourceError(f"{label} returned an unexpected metadata document.")
    return cast(Mapping[str, Any], parsed)


def _as_float(value: object) -> float | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _as_int(value: object) -> int | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _parse_fps(value: object) -> float | None:
    if not isinstance(value, str) or value in {"", "0/0"}:
        return None
    numerator, separator, denominator = value.partition("/")
    try:
        fps = float(numerator) / float(denominator) if separator else float(numerator)
    except (ValueError, ZeroDivisionError):
        return None
    return fps if fps > 0 else None


def _validate_frame_dimensions(width: int | None, height: int | None) -> None:
    if width is None or height is None:
        return
    if width <= 0 or height <= 0:
        raise SourceError("Video frame dimensions must be positive when reported.")
    if max(width, height) > MAX_SOURCE_FRAME_EDGE or width * height > MAX_SOURCE_FRAME_PIXELS:
        raise SourceError(
            f"Video frames are {width}x{height}, above the supported extraction limit "
            f"({MAX_SOURCE_FRAME_EDGE}px edge and {MAX_SOURCE_FRAME_PIXELS} pixels)."
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp_seconds(value: str) -> float:
    hours, minutes, rest = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def _clean_caption_text(lines: Sequence[str]) -> str:
    text = "\n".join(lines)
    text = html.unescape(_TAG_RE.sub("", text))
    cleaned_lines = [_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in cleaned_lines if line).strip()


def parse_subtitles(
    document: str,
    *,
    language: str | None = None,
    origin: str = "captions",
) -> tuple[TranscriptSegment, ...]:
    """Parse the common WebVTT/SRT cue subset into normalized segments."""

    lines = document.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments: list[TranscriptSegment] = []
    index = 0
    while index < len(lines):
        match = _TIMELINE_RE.fullmatch(lines[index].strip())
        if match is None:
            index += 1
            continue
        start_s = _timestamp_seconds(match.group("start"))
        end_s = _timestamp_seconds(match.group("end"))
        index += 1
        cue_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            cue_lines.append(lines[index])
            index += 1
        text = _clean_caption_text(cue_lines)
        if text and end_s >= start_s:
            segment = TranscriptSegment(start_s, end_s, text, language, origin)
            if segments and segments[-1].text == segment.text:
                previous = segments[-1]
                segments[-1] = TranscriptSegment(
                    previous.start_s,
                    max(previous.end_s, segment.end_s),
                    previous.text,
                    previous.language,
                    previous.origin,
                )
            else:
                segments.append(segment)
        index += 1
    return tuple(segments)


def _sidecar_candidates(
    video_path: Path,
    settings: Settings,
) -> list[tuple[Path, Path]]:
    matching_directory_roots = [
        root for root in settings.allowed_roots if root.is_dir() and video_path.is_relative_to(root)
    ]
    # An exact file root authorizes that video, but not sibling caption files.
    if not matching_directory_roots:
        return []
    source_root = max(matching_directory_roots, key=lambda root: len(root.parts))
    candidates: list[tuple[Path, Path]] = []
    for candidate in video_path.parent.glob(f"{video_path.stem}.*"):
        if candidate.suffix.lower() not in _SUBTITLE_EXTENSIONS:
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise SourceError(f"Subtitle sidecar cannot be resolved safely: {candidate}") from exc
        if not resolved.is_file() or not resolved.is_relative_to(source_root):
            raise SourceError(
                f"Subtitle sidecar escapes the video's authorized source root: {candidate}"
            )
        candidates.append((candidate, resolved))
    return sorted(
        candidates,
        key=lambda pair: (
            pair[0].stem != video_path.stem,
            pair[0].suffix.lower() != ".vtt",
            pair[0].name.lower(),
        ),
    )


def _sidecar_language(video_path: Path, sidecar: Path) -> str | None:
    remainder = sidecar.stem.removeprefix(video_path.stem).lstrip(".")
    return remainder or None


def _read_sidecar(path: Path, *, max_bytes: int) -> str:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise SourceError(f"Subtitle sidecar exceeds the {max_bytes}-byte limit: {path}")
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise SourceError(f"Could not read subtitle sidecar: {path}") from exc


def _extract_embedded_subtitles(
    video_path: Path,
    stream: Mapping[str, Any],
    settings: Settings,
) -> tuple[TranscriptSegment, ...]:
    index = _as_int(stream.get("index"))
    if index is None:
        return ()
    command = [
        settings.ffmpeg_executable,
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(video_path),
        "-map",
        f"0:{index}",
        "-f",
        "webvtt",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"FFmpeg executable was not found: {settings.ffmpeg_executable}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceError("FFmpeg timed out while reading embedded subtitles.") from exc
    if result.returncode != 0:
        logger.warning("FFmpeg could not decode embedded subtitle stream %s", index)
        return ()
    if len(result.stdout) > settings.max_subtitle_bytes:
        raise SourceError(
            f"Embedded subtitles exceed the {settings.max_subtitle_bytes}-byte limit."
        )
    tags = cast(
        Mapping[str, Any],
        stream.get("tags") if isinstance(stream.get("tags"), Mapping) else {},
    )
    language = str(tags.get("language")) if tags.get("language") else None
    return parse_subtitles(
        result.stdout.decode("utf-8", errors="replace"),
        language=language,
        origin="embedded",
    )


def acquire_local(
    source: str | Path,
    settings: Settings,
    *,
    mode: AcquisitionMode = "fast",
    transcript_mode: TranscriptMode = "auto",
    max_duration_s: int | None = None,
) -> AcquiredSource:
    """Inspect a local video in place and discover sidecar/embedded captions."""

    _validate_modes(mode, transcript_mode)
    limit = _effective_duration_limit(settings, max_duration_s)
    path = validate_local_path(source, settings)
    probe = _run_json_command(
        [
            settings.ffprobe_executable,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-of",
            "json",
            str(path),
        ],
        label="ffprobe",
    )
    streams_value = probe.get("streams", [])
    streams = (
        [item for item in streams_value if isinstance(item, Mapping)]
        if isinstance(streams_value, list)
        else []
    )
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    if video_stream is None:
        raise SourceError(f"No video stream was found in {path}.")
    width = _as_int(video_stream.get("width"))
    height = _as_int(video_stream.get("height"))
    _validate_frame_dimensions(width, height)
    format_info = cast(
        Mapping[str, Any],
        probe.get("format") if isinstance(probe.get("format"), Mapping) else {},
    )
    duration = _as_float(format_info.get("duration"))
    if duration is None:
        duration = max((_as_float(item.get("duration")) or 0.0 for item in streams), default=0.0)
    if duration <= 0:
        raise SourceError(f"Could not determine a positive duration for {path}.")
    if duration > limit:
        raise SourceError(
            f"Video duration is {duration:.1f}s, above the configured {limit}s limit."
        )

    tags = cast(
        Mapping[str, Any],
        format_info.get("tags") if isinstance(format_info.get("tags"), Mapping) else {},
    )
    title_value = tags.get("title")
    title = str(title_value).strip() if title_value else path.stem
    warnings: list[str] = []
    transcript: tuple[TranscriptSegment, ...] = ()
    if transcript_mode in {"auto", "captions"}:
        candidates = _sidecar_candidates(path, settings)
        if candidates:
            sidecar_name, sidecar = candidates[0]
            transcript = parse_subtitles(
                _read_sidecar(sidecar, max_bytes=settings.max_subtitle_bytes),
                language=_sidecar_language(path, sidecar_name),
                origin="sidecar",
            )
        if not transcript:
            subtitle_stream = next(
                (
                    item
                    for item in streams
                    if item.get("codec_type") == "subtitle"
                    and str(item.get("codec_name", "")).lower() in _TEXT_SUBTITLE_CODECS
                ),
                None,
            )
            if subtitle_stream is not None:
                transcript = _extract_embedded_subtitles(path, subtitle_stream, settings)
        if not transcript:
            warnings.append("No readable sidecar or embedded captions were found.")
    elif transcript_mode == "whisper":
        warnings.append("Speech transcription is deferred to the optional Whisper pipeline.")

    identity_before = path.stat()
    content_sha256 = _file_sha256(path)
    identity_after = path.stat()
    if (
        identity_before.st_size != identity_after.st_size
        or identity_before.st_mtime_ns != identity_after.st_mtime_ns
    ):
        raise SourceError(f"Local video changed while it was being inspected: {path}")
    metadata = SourceMetadata(
        source=str(path),
        kind=SourceKind.LOCAL,
        video_id=f"local-{content_sha256[:16]}",
        title=title,
        duration_s=duration,
        provider="local",
        file_size_bytes=identity_after.st_size,
        width=width,
        height=height,
        fps=_parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
        content_sha256=content_sha256,
        content_mtime_ns=identity_after.st_mtime_ns,
        availability="local",
    )
    return AcquiredSource(
        metadata=metadata,
        transcript=transcript,
        chapters=_chapters(probe, duration=duration),
        media_path=path,
        warnings=tuple(warnings),
        owns_media=False,
    )


class _YtDlpLogger:
    """Route yt-dlp diagnostics to logging instead of protocol stdout."""

    def debug(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        logger.info("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        logger.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logger.error("yt-dlp: %s", message)


class _ValidatedRedirectHandler(HTTPRedirectHandler):
    """Validate every subtitle redirect before urllib follows it."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        absolute_url = urljoin(req.full_url, newurl)
        validate_remote_url(
            absolute_url,
            allow_private=self._settings.allow_private_urls,
            resolve_dns=True,
        )
        return super().redirect_request(req, fp, code, msg, headers, absolute_url)


class _ValidatedSubtitleHTTPHandler(HTTPHandler):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    def http_open(self, req: Request) -> Any:
        return self.do_open(
            partial(
                _validated_http_connection,
                http.client.HTTPConnection,
                None,
                self._settings.allow_private_urls,
            ),
            req,
        )


class _ValidatedSubtitleHTTPSHandler(HTTPSHandler):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    def https_open(self, req: Request) -> Any:
        return self.do_open(
            partial(
                _validated_http_connection,
                http.client.HTTPSConnection,
                None,
                self._settings.allow_private_urls,
            ),
            req,
            context=getattr(self, "_context", None),
        )


def _validated_subtitle_opener(settings: Settings) -> Any:
    """Build a no-proxy opener whose socket connects only to validated addresses."""

    return build_opener(
        ProxyHandler({}),
        _ValidatedSubtitleHTTPHandler(settings),
        _ValidatedSubtitleHTTPSHandler(settings),
        _ValidatedRedirectHandler(settings),
    )


def _open_validated_subtitle(request: Request, settings: Settings) -> Any:
    validate_remote_url(
        request.full_url,
        allow_private=settings.allow_private_urls,
        resolve_dns=True,
    )
    opener = _validated_subtitle_opener(settings)
    return opener.open(request, timeout=30)


def _safe_create_connection(
    address: tuple[str, int],
    timeout: Any = _SOCKET_DEFAULT_TIMEOUT,
    source_address: tuple[str, int] | None = None,
    *,
    allow_private: bool = False,
) -> socket.socket:
    """Resolve and connect once, rejecting every non-public destination address."""

    host, port = address
    addresses = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not addresses:
        raise OSError(f"Hostname {host!r} resolved to no usable address.")
    if not allow_private and any(_is_disallowed_address(str(item[4][0])) for item in addresses):
        raise OSError(
            f"Hostname {host!r} resolved to a private or non-public address; request blocked."
        )
    if source_address is not None:
        family = socket.AF_INET if ":" not in source_address[0] else socket.AF_INET6
        addresses = [item for item in addresses if item[0] == family]
        if not addresses:
            raise OSError(f"No address for {host!r} matches the configured source address.")

    last_error: OSError | None = None
    for family, socktype, proto, _canonical_name, socket_address in addresses:
        connection = socket.socket(family, socktype, proto)
        try:
            if timeout is not _SOCKET_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address is not None:
                connection.bind(source_address)
            connection.connect(socket_address)
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"Could not connect to any public address for {host!r}.")


def _remove_owned_temp_dir(temp_dir: Path) -> None:
    try:
        shutil.rmtree(temp_dir)
    except OSError as exc:
        raise SourceError(
            f"Could not remove temporary downloaded media at {temp_dir}. "
            "Close processes using the file and remove that directory before retrying."
        ) from exc
    if temp_dir.exists():
        raise SourceError(f"Temporary downloaded media still exists after cleanup: {temp_dir}")


def _validated_http_connection(
    http_class: type[Any],
    source_address: str | None,
    allow_private: bool,
    *args: Any,
    **kwargs: Any,
) -> Any:
    connection = http_class(*args, **kwargs)
    connection._create_connection = partial(
        _safe_create_connection,
        allow_private=allow_private,
    )
    if source_address is not None:
        connection.source_address = (source_address, 0)
    return connection


def _get_youtube_dl(settings: Settings) -> type[Any]:
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-untyped]
        from yt_dlp.dependencies import Cryptodome  # type: ignore[import-untyped]
        from yt_dlp.downloader import determine_protocol  # type: ignore[import-untyped]
        from yt_dlp.downloader.hls import HlsFD  # type: ignore[import-untyped]
        from yt_dlp.networking import _urllib as ydl_urllib  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - packaging guarantees the dependency
        raise ConfigurationError("yt-dlp is not installed; reinstall video-context-mcp.") from exc

    class ValidatedRedirectHandler(ydl_urllib.RedirectHandler):  # type: ignore[misc]
        def redirect_request(
            self,
            req: Any,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> Any:
            absolute_url = urljoin(req.full_url, newurl)
            validate_remote_url(
                absolute_url,
                allow_private=settings.allow_private_urls,
                resolve_dns=True,
            )
            return super().redirect_request(req, fp, code, msg, headers, absolute_url)

    class ValidatedHTTPHandler(ydl_urllib.HTTPHandler):  # type: ignore[misc]
        def http_open(self, req: Any) -> Any:
            connection_class = self._make_conn_class(http.client.HTTPConnection, req)
            return self.do_open(
                partial(
                    _validated_http_connection,
                    connection_class,
                    self._source_address,
                    settings.allow_private_urls,
                ),
                req,
            )

        def https_open(self, req: Any) -> Any:
            connection_class = self._make_conn_class(http.client.HTTPSConnection, req)
            return self.do_open(
                partial(
                    _validated_http_connection,
                    connection_class,
                    self._source_address,
                    settings.allow_private_urls,
                ),
                req,
                context=self._context,
            )

    class UrllibRH(ydl_urllib.UrllibRH):  # type: ignore[misc]
        """Pinned yt-dlp urllib handler with a closed-world network boundary."""

        def _create_instance(
            self,
            proxies: Any,
            cookiejar: Any,
            legacy_ssl_support: Any = None,
        ) -> Any:
            opener = urllib.request.OpenerDirector()
            handlers = [
                ValidatedHTTPHandler(
                    debuglevel=int(bool(self.verbose)),
                    context=self._make_sslcontext(legacy_ssl_support=legacy_ssl_support),
                    source_address=self.source_address,
                ),
                ydl_urllib.HTTPCookieProcessor(cookiejar),
                ydl_urllib.DataHandler(),
                ydl_urllib.UnknownHandler(),
                ydl_urllib.HTTPDefaultErrorHandler(),
                ydl_urllib.HTTPErrorProcessor(),
                ValidatedRedirectHandler(),
            ]
            for handler in handlers:
                opener.add_handler(handler)
            opener.addheaders = []
            return opener

        def _get_proxies(self, request: Any) -> dict[str, None]:
            request_proxies = getattr(request, "proxies", None)
            if isinstance(request_proxies, Mapping) and any(
                value not in {None, ""} for value in request_proxies.values()
            ):
                raise SourceError("Per-request proxies are not supported by Keyframe.")
            return {"all": None}

        def _send(self, request: Any) -> Any:
            validate_remote_url(
                str(request.url),
                allow_private=settings.allow_private_urls,
                resolve_dns=True,
            )
            return super()._send(request)

    class ValidatedYoutubeDL(YoutubeDL):  # type: ignore[misc]
        def build_request_director(
            self,
            handlers: Any,
            preferences: Any = None,
        ) -> Any:
            del handlers
            return super().build_request_director((UrllibRH,), preferences)

        def dl(self, name: str, info: Any, subtitle: bool = False, test: bool = False) -> Any:
            selected = info.get("requested_formats") if isinstance(info, Mapping) else None
            formats = (
                [item for item in selected if isinstance(item, Mapping)]
                if isinstance(selected, list) and selected
                else [info]
            )
            for selected_format in formats:
                if not isinstance(selected_format, Mapping):
                    raise SourceError("yt-dlp selected an invalid media format document.")
                if selected_format.get("section_start") or selected_format.get("section_end"):
                    raise SourceError(
                        "Remote partial-media downloads that require FFmpeg networking are not "
                        "supported."
                    )
                try:
                    protocol = str(determine_protocol(selected_format)).lower()
                except Exception as exc:
                    raise SourceError(
                        "yt-dlp selected a media format with no safe protocol."
                    ) from exc
                protocols = set(protocol.split("+"))
                unsupported = protocols - _IN_PROCESS_YTDLP_PROTOCOLS
                if unsupported:
                    names = ", ".join(sorted(unsupported))
                    raise SourceError(
                        f"yt-dlp selected unsupported media protocol(s) {names}. Keyframe only "
                        "downloads formats handled by its validated in-process HTTP transport."
                    )
                if "m3u8_native" in protocols:
                    if not isinstance(selected_format, dict):
                        raise SourceError("yt-dlp selected an immutable HLS format document.")
                    manifest_value = selected_format.get("hls_media_playlist_data")
                    if isinstance(manifest_value, str) and manifest_value:
                        manifest = manifest_value
                    else:
                        media_url = selected_format.get("url")
                        if not isinstance(media_url, str):
                            raise SourceError("yt-dlp selected an HLS format without a URL.")
                        validate_remote_url(
                            media_url,
                            allow_private=settings.allow_private_urls,
                            resolve_dns=True,
                        )
                        hls_downloader = HlsFD(self, self.params)
                        request = hls_downloader._prepare_url(selected_format, media_url)
                        try:
                            with self.urlopen(request) as response:
                                payload = response.read(settings.max_subtitle_bytes + 1)
                                final_url = getattr(response, "url", None)
                                if not isinstance(final_url, str):
                                    final_url = response.geturl()
                        except SourceError:
                            raise
                        except Exception as exc:
                            raise SourceError(
                                f"Could not inspect the selected HLS manifest safely: {exc}"
                            ) from exc
                        if len(payload) > settings.max_subtitle_bytes:
                            raise SourceError(
                                "The selected HLS manifest exceeds the configured "
                                f"{settings.max_subtitle_bytes}-byte text limit."
                            )
                        if isinstance(final_url, str):
                            selected_format["url"] = validate_remote_url(
                                final_url,
                                allow_private=settings.allow_private_urls,
                                resolve_dns=True,
                            )
                        manifest = payload.decode("utf-8", errors="ignore")
                    if not HlsFD.can_download(manifest, selected_format, False):
                        raise SourceError(
                            "The selected HLS manifest uses DRM, live, or unsupported encryption "
                            "features. Keyframe will not delegate remote access to FFmpeg."
                        )
                    if not Cryptodome.AES and "#EXT-X-KEY:METHOD=AES-128" in manifest:
                        raise SourceError(
                            "The selected HLS manifest requires AES-128 support that is unavailable. "
                            "Reinstall video-context-mcp with yt-dlp's default extras."
                        )
                    # Pin the exact preflighted document so HlsFD cannot refetch a changed manifest
                    # before deciding whether to fall back to an external downloader.
                    selected_format["hls_media_playlist_data"] = manifest
            return super().dl(name, info, subtitle=subtitle, test=test)

    return cast(type[Any], ValidatedYoutubeDL)


def _base_ydl_options(settings: Settings, *, refresh: bool) -> dict[str, Any]:
    options: dict[str, Any] = {
        "cachedir": False if refresh else str(settings.cache_dir / "yt-dlp"),
        "cookiefile": None,
        "allow_unplayable_formats": False,
        "extract_flat": False,
        "external_downloader": "native",
        "hls_prefer_native": True,
        "ignoreerrors": False,
        "js_runtimes": {"node": {"path": settings.node_executable}},
        "logger": _YtDlpLogger(),
        "noplaylist": True,
        "no_warnings": True,
        "proxy": "",
        "quiet": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    if settings.ffmpeg_executable != "ffmpeg":
        options["ffmpeg_location"] = settings.ffmpeg_executable
    return options


def _download_size_hook(settings: Settings) -> Callable[[Mapping[str, Any]], None]:
    observed_by_stream: dict[str, int] = {}

    def enforce(status: Mapping[str, Any]) -> None:
        downloaded = _as_int(status.get("downloaded_bytes")) or 0
        total = _as_int(status.get("total_bytes")) or _as_int(status.get("total_bytes_estimate"))
        observed = max(downloaded, total or 0)
        info = status.get("info_dict")
        info_mapping = info if isinstance(info, Mapping) else {}
        stream_key = str(
            status.get("filename")
            or status.get("tmpfilename")
            or info_mapping.get("format_id")
            or info_mapping.get("url")
            or "single-stream"
        )
        observed_by_stream[stream_key] = max(observed_by_stream.get(stream_key, 0), observed)
        aggregate = sum(observed_by_stream.values())
        if aggregate > settings.max_remote_file_bytes:
            raise SourceError(
                f"Aggregate remote download exceeded the configured "
                f"{settings.max_remote_file_bytes}-byte limit."
            )

    return enforce


def _extract_remote_info(
    source: str,
    settings: Settings,
    *,
    refresh: bool,
) -> Mapping[str, Any]:
    options = _base_ydl_options(settings, refresh=refresh)
    options["skip_download"] = True
    try:
        with _get_youtube_dl(settings)(options) as ydl:
            result = ydl.extract_info(source, download=False)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        logger.warning("yt-dlp metadata extraction failed for %s: %s", source, exc)
        raise SourceError(f"yt-dlp could not inspect the remote video: {exc}") from exc
    if not isinstance(result, Mapping):
        raise SourceError("yt-dlp returned no metadata for the remote video.")
    return cast(Mapping[str, Any], result)


def _validate_remote_info(
    info: Mapping[str, Any],
    settings: Settings,
    *,
    max_duration_s: int,
) -> float:
    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries") is not None:
        raise SourceError(
            "Playlists and multi-video sources are not supported; provide one video URL."
        )
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise SourceError("Livestreams and upcoming streams are not supported.")
    age_limit = _as_int(info.get("age_limit")) or 0
    if age_limit > 0:
        raise SourceError("Age-restricted videos are not supported.")
    availability = str(info.get("availability") or "public").lower()
    if availability not in {"public", "unlisted"} or info.get("is_private"):
        raise SourceError(
            f"Video availability {availability!r} requires authentication and is not supported."
        )
    if info.get("has_drm"):
        raise SourceError("DRM-protected videos are not supported.")
    _validate_frame_dimensions(_as_int(info.get("width")), _as_int(info.get("height")))
    duration = _as_float(info.get("duration"))
    if duration is None or duration <= 0:
        raise SourceError("yt-dlp could not determine a positive video duration.")
    if duration > max_duration_s:
        raise SourceError(
            f"Video duration is {duration:.1f}s, above the configured {max_duration_s}s limit."
        )
    size = _as_int(info.get("filesize")) or _as_int(info.get("filesize_approx"))
    if size is not None and size > settings.max_remote_file_bytes:
        raise SourceError(
            f"Remote video is approximately {size} bytes, above the configured "
            f"{settings.max_remote_file_bytes}-byte limit."
        )
    return duration


def _validate_reported_direct_urls(info: Mapping[str, Any], settings: Settings) -> None:
    """Reject private-network URLs reported by a generic direct extractor."""

    for key in ("url", "manifest_url", "webpage_url", "original_url"):
        value = info.get(key)
        if not isinstance(value, str) or not value.lower().startswith(("http://", "https://")):
            continue
        validate_remote_url(
            value,
            allow_private=settings.allow_private_urls,
            resolve_dns=True,
        )


def _preferred_subtitle(
    tracks_value: object,
) -> tuple[str, Mapping[str, Any]] | None:
    if not isinstance(tracks_value, Mapping):
        return None
    tracks = {
        str(language): value
        for language, value in tracks_value.items()
        if language != "live_chat" and isinstance(value, list)
    }
    if not tracks:
        return None
    languages = sorted(
        tracks,
        key=lambda language: (
            language.lower() != "en",
            not language.lower().startswith("en-"),
            language.lower(),
        ),
    )
    for language in languages:
        variants = [item for item in tracks[language] if isinstance(item, Mapping)]
        variants.sort(
            key=lambda item: (
                str(item.get("ext", "")).lower() != "vtt",
                str(item.get("ext", "")).lower() != "srt",
            )
        )
        for variant in variants:
            if variant.get("url") and str(variant.get("ext", "")).lower() in {"vtt", "srt"}:
                return language, cast(Mapping[str, Any], variant)
    return None


def _download_subtitle(
    language: str,
    track: Mapping[str, Any],
    settings: Settings,
    *,
    origin: str,
) -> tuple[TranscriptSegment, ...]:
    url_value = track.get("url")
    if not isinstance(url_value, str):
        return ()
    subtitle_url = validate_remote_url(
        url_value,
        allow_private=settings.allow_private_urls,
        resolve_dns=True,
    )
    request = Request(subtitle_url, headers={"User-Agent": "Keyframe/0.1"})
    try:
        with _open_validated_subtitle(request, settings) as response:
            final_url = response.geturl()
            if isinstance(final_url, str):
                validate_remote_url(
                    final_url,
                    allow_private=settings.allow_private_urls,
                    resolve_dns=True,
                )
            payload = response.read(settings.max_subtitle_bytes + 1)
    except SourceError:
        raise
    except OSError as exc:
        raise SourceError(f"Could not download {language!r} subtitles: {exc}") from exc
    if len(payload) > settings.max_subtitle_bytes:
        raise SourceError(f"Remote subtitles exceed the {settings.max_subtitle_bytes}-byte limit.")
    return parse_subtitles(
        payload.decode("utf-8-sig", errors="replace"),
        language=language,
        origin=origin,
    )


def _chapters(info: Mapping[str, Any], *, duration: float) -> tuple[Chapter, ...]:
    values = info.get("chapters")
    if not isinstance(values, list):
        return ()
    chapters: list[Chapter] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        start = _as_float(item.get("start_time"))
        if start is None:
            continue
        end = _as_float(item.get("end_time"))
        tags = item.get("tags")
        tag_mapping = tags if isinstance(tags, Mapping) else {}
        title = str(
            item.get("title") or tag_mapping.get("title") or f"Chapter {len(chapters) + 1}"
        ).strip()
        normalized_end = duration if end is None else min(end, duration)
        if normalized_end >= start:
            chapters.append(Chapter(start, normalized_end, title))
    return tuple(sorted(chapters, key=lambda chapter: (chapter.start_s, chapter.end_s)))


def _remote_metadata(
    source: str,
    kind: SourceKind,
    info: Mapping[str, Any],
    *,
    duration: float,
) -> SourceMetadata:
    video_id = str(info.get("id") or hashlib.sha256(source.encode()).hexdigest()[:16])
    title = str(info.get("title") or video_id).strip()
    webpage_url = info.get("webpage_url")
    uploader = info.get("uploader") or info.get("channel")
    provider = str(info.get("extractor_key") or info.get("extractor") or kind.value).lower()
    return SourceMetadata(
        source=source,
        kind=kind,
        video_id=video_id,
        title=title,
        duration_s=duration,
        provider=provider,
        webpage_url=str(webpage_url) if webpage_url else source,
        uploader=str(uploader) if uploader else None,
        file_size_bytes=_as_int(info.get("filesize")) or _as_int(info.get("filesize_approx")),
        width=_as_int(info.get("width")),
        height=_as_int(info.get("height")),
        fps=_as_float(info.get("fps")),
        availability=cast(Availability, str(info.get("availability") or "public").lower()),
    )


def _find_downloaded_media(temp_dir: Path, info: Mapping[str, Any]) -> Path | None:
    resolved_temp_dir = temp_dir.resolve()

    def owned_file(value: object) -> Path | None:
        if not value:
            return None
        candidate = Path(str(value)).resolve()
        if candidate.is_file() and candidate.is_relative_to(resolved_temp_dir):
            return candidate
        return None

    requested = info.get("requested_downloads")
    if isinstance(requested, list):
        for item in requested:
            if not isinstance(item, Mapping):
                continue
            for key in ("filepath", "_filename"):
                candidate = owned_file(item.get(key))
                if candidate is not None:
                    return candidate
    candidate = owned_file(info.get("_filename"))
    if candidate is not None:
        return candidate
    candidates = [
        path
        for path in temp_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
        and not path.name.endswith((".part", ".ytdl"))
    ]
    return max(candidates, key=lambda path: path.stat().st_size, default=None)


def _download_remote_media(
    source: str,
    settings: Settings,
    *,
    refresh: bool,
    temp_dir: Path,
) -> tuple[Path, Mapping[str, Any]]:
    options = _base_ydl_options(settings, refresh=refresh)
    options.update(
        {
            "format": "bv*+ba/b",
            "max_filesize": settings.max_remote_file_bytes,
            "merge_output_format": "mp4",
            "outtmpl": str(temp_dir / "source.%(ext)s"),
            "overwrites": True,
            "progress_hooks": [_download_size_hook(settings)],
            "skip_download": False,
        }
    )
    try:
        with _get_youtube_dl(settings)(options) as ydl:
            result = ydl.extract_info(source, download=True)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        logger.warning("yt-dlp media download failed for %s: %s", source, exc)
        raise SourceError(f"yt-dlp could not download the remote video: {exc}") from exc
    if not isinstance(result, Mapping):
        raise SourceError("yt-dlp returned no metadata after downloading the video.")
    normalized = cast(Mapping[str, Any], result)
    media_path = _find_downloaded_media(temp_dir, normalized)
    if media_path is None:
        raise SourceError("yt-dlp completed without producing a supported video file.")
    size = media_path.stat().st_size
    if size > settings.max_remote_file_bytes:
        raise SourceError(
            f"Downloaded video is {size} bytes, above the configured "
            f"{settings.max_remote_file_bytes}-byte limit."
        )
    return media_path, normalized


def _assert_same_remote_identity(initial: Mapping[str, Any], downloaded: Mapping[str, Any]) -> None:
    initial_id = str(initial.get("id") or "")
    downloaded_id = str(downloaded.get("id") or "")
    if initial_id and downloaded_id and initial_id != downloaded_id:
        raise SourceError(
            "The remote provider changed video identity between metadata and media download; "
            "nothing was published. Retry with refresh or use a stable single-video URL."
        )
    initial_provider = str(initial.get("extractor_key") or initial.get("extractor") or "").lower()
    downloaded_provider = str(
        downloaded.get("extractor_key") or downloaded.get("extractor") or ""
    ).lower()
    if initial_provider and downloaded_provider and initial_provider != downloaded_provider:
        raise SourceError(
            "The remote provider changed extractor identity during download; nothing was published."
        )


def _probe_downloaded_media(
    media_path: Path,
    settings: Settings,
    *,
    max_duration_s: int,
) -> None:
    probe = _run_json_command(
        [
            settings.ffprobe_executable,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(media_path),
        ],
        label="ffprobe",
    )
    streams_value = probe.get("streams", [])
    streams = (
        [item for item in streams_value if isinstance(item, Mapping)]
        if isinstance(streams_value, list)
        else []
    )
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    if video_stream is None:
        raise SourceError("The downloaded remote media contains no video stream.")
    _validate_frame_dimensions(
        _as_int(video_stream.get("width")),
        _as_int(video_stream.get("height")),
    )
    format_info = cast(
        Mapping[str, Any],
        probe.get("format") if isinstance(probe.get("format"), Mapping) else {},
    )
    duration = _as_float(format_info.get("duration"))
    if duration is None:
        duration = max((_as_float(item.get("duration")) or 0.0 for item in streams), default=0.0)
    if duration <= 0:
        raise SourceError("ffprobe could not determine a positive downloaded-media duration.")
    if duration > max_duration_s + 1:
        raise SourceError(
            f"Downloaded video duration is {duration:.1f}s, above the configured "
            f"{max_duration_s}s limit."
        )


def acquire_remote(
    source: str,
    settings: Settings,
    *,
    mode: AcquisitionMode = "fast",
    transcript_mode: TranscriptMode = "auto",
    max_duration_s: int | None = None,
    refresh: bool = False,
) -> AcquiredSource:
    """Acquire public remote metadata/captions and, when needed, media.

    The first yt-dlp pass is metadata-only.  Media is downloaded only for full
    visual indexing or explicit Whisper transcription, and only after restrictions
    and the duration cap have passed.
    """

    _validate_modes(mode, transcript_mode)
    limit = _effective_duration_limit(settings, max_duration_s)
    validated_source = validate_remote_url(
        source,
        allow_private=settings.allow_private_urls,
        resolve_dns=True,
    )
    kind = classify_source(validated_source)
    info = _extract_remote_info(validated_source, settings, refresh=refresh)
    if kind is SourceKind.DIRECT:
        _validate_reported_direct_urls(info, settings)
    duration = _validate_remote_info(info, settings, max_duration_s=limit)
    warnings: list[str] = []
    transcript: tuple[TranscriptSegment, ...] = ()

    if transcript_mode in {"auto", "captions"}:
        selected = _preferred_subtitle(info.get("subtitles"))
        if selected is not None:
            language, track = selected
            try:
                transcript = _download_subtitle(
                    language,
                    track,
                    settings,
                    origin="captions",
                )
            except SourceError as exc:
                if transcript_mode == "captions":
                    raise
                warnings.append(
                    f"Manual captions could not be read; trying automatic captions: {exc}"
                )
        if not transcript and transcript_mode == "auto":
            automatic = _preferred_subtitle(info.get("automatic_captions"))
            if automatic is not None:
                language, track = automatic
                try:
                    transcript = _download_subtitle(
                        language,
                        track,
                        settings,
                        origin="automatic_captions",
                    )
                except SourceError as exc:
                    warnings.append(f"Automatic captions could not be read: {exc}")
        if not transcript:
            warnings.append("No readable captions were available from the remote source.")
    elif transcript_mode == "whisper":
        warnings.append("Speech transcription is deferred to the optional Whisper pipeline.")

    temp_dir: Path | None = None
    media_path: Path | None = None
    try:
        if mode == "full" or transcript_mode == "whisper":
            settings.ensure_directories()
            temp_dir = Path(tempfile.mkdtemp(prefix="acquire-", dir=settings.tmp_dir))
            media_path, downloaded_info = _download_remote_media(
                validated_source,
                settings,
                refresh=refresh,
                temp_dir=temp_dir,
            )
            # Defend against a provider changing the result between metadata and download.
            _assert_same_remote_identity(info, downloaded_info)
            _validate_remote_info(downloaded_info, settings, max_duration_s=limit)
            if kind is SourceKind.DIRECT:
                _validate_reported_direct_urls(downloaded_info, settings)
            _probe_downloaded_media(media_path, settings, max_duration_s=limit)

        return AcquiredSource(
            metadata=_remote_metadata(validated_source, kind, info, duration=duration),
            transcript=transcript,
            chapters=_chapters(info, duration=duration),
            media_path=media_path,
            warnings=tuple(warnings),
            owns_media=media_path is not None,
            _temp_dir=temp_dir,
        )
    except BaseException as exc:
        if temp_dir is not None:
            try:
                _remove_owned_temp_dir(temp_dir)
            except SourceError as cleanup_error:
                cleanup_error.add_note(
                    f"The original acquisition failure was {type(exc).__name__}: {exc}"
                )
                raise cleanup_error from exc
        raise
