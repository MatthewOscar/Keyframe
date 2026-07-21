from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from video_context_mcp.constants import PIPELINE_VERSION


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class IngestMode(StrEnum):
    FAST = "fast"
    FULL = "full"


class VisualCoverage(StrEnum):
    NONE = "none"
    PROBE = "probe"
    FULL = "full"


class TranscriptMode(StrEnum):
    AUTO = "auto"
    CAPTIONS = "captions"
    WHISPER = "whisper"
    NONE = "none"


class TranscriptView(StrEnum):
    EXACT = "exact"
    COMPACT = "compact"


class MomentKind(StrEnum):
    CODE = "code"
    TERMINAL = "terminal"
    SLIDE = "slide"
    DIAGRAM = "diagram"
    OTHER = "other"
    ANY = "any"


class SearchChannel(StrEnum):
    SAID = "said"
    SHOWN = "shown"
    ALL = "all"


class FrameRegion(StrEnum):
    FULL = "full"
    AUTO_CROP = "auto_crop"


class FrameQuality(StrEnum):
    AUTO = "auto"
    PROBE = "probe"
    SOURCE = "source"


class FrameEvidenceQuality(StrEnum):
    RETAINED = "retained"
    PROBE = "probe"
    SOURCE = "source"


class Chapter(StrictModel):
    start_s: Annotated[float, Field(ge=0)]
    end_s: Annotated[float | None, Field(default=None, ge=0)]
    title: str = Field(min_length=1, max_length=500)


class TranscriptSegment(StrictModel):
    segment_id: str
    start_s: Annotated[float, Field(ge=0)]
    end_s: Annotated[float, Field(ge=0)]
    text: str
    source: str = "captions"


class VisualMoment(StrictModel):
    moment_id: str
    video_id: str
    actual_t: Annotated[float, Field(ge=0)]
    start_s: Annotated[float, Field(ge=0)]
    end_s: Annotated[float, Field(ge=0)]
    kind: MomentKind
    classification_confidence: Annotated[float, Field(ge=0, le=1)] = 0.0
    stable_seconds: Annotated[float, Field(ge=0)]
    ocr_text: str = ""
    ocr_confidence: Annotated[float, Field(ge=0, le=1)] = 0.0
    language_guess: str | None = None
    code: str = ""
    parses: bool | None = None
    notes: tuple[str, ...] = ()
    frame_path: str
    crop_path: str | None = None


class VideoRecord(StrictModel):
    video_id: str
    source: str
    source_kind: str
    availability: Literal["local", "public", "unlisted"] = "public"
    source_fingerprint: str
    title: str
    duration_s: Annotated[float, Field(ge=0)]
    chapters: tuple[Chapter, ...] = ()
    has_transcript: bool
    has_audio: bool = True
    transcript_mode: TranscriptMode = TranscriptMode.AUTO
    indexed_mode: IngestMode
    visual_coverage: VisualCoverage
    keyframe_count: Annotated[int, Field(ge=0)] = 0
    status: str = "ready"
    warnings: tuple[str, ...] = ()
    local_source_path: str | None = None
    local_source_size: int | None = None
    local_source_mtime_ns: int | None = None
    pipeline_version: str


class IngestTimings(StrictModel):
    """Request-local ingest timings; concurrent stages must not be summed."""

    total_ms: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Total service wall time. This is authoritative because component stages may "
                "overlap and do not partition the total."
            ),
        ),
    ]
    cache_lookup_ms: Annotated[
        int,
        Field(ge=0, description="Cumulative cache lookup and reuse-validation wall time."),
    ]
    acquisition_ms: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description="Source acquisition, hashing, metadata, and caption wall time; null when skipped.",
        ),
    ]
    transcription_ms: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description="Local Whisper wall time; null when captions were used or speech was skipped.",
        ),
    ]
    visual_ms: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description="Visual sampling, OCR, encoding, and artifact-publication wall time; null when reused.",
        ),
    ]
    index_commit_ms: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description="SQLite index commit wall time; null when no index write occurred.",
        ),
    ]


class IngestResult(StrictModel):
    video_id: str = Field(
        description=(
            "Authoritative opaque ID for this successful ingest. Copy it byte-for-byte into "
            "follow-up Keyframe calls; never derive or retype it from the source, title, or "
            "provider ID."
        )
    )
    title: str
    duration_s: float
    source_type: str
    availability: Literal["local", "public", "unlisted"] = "public"
    chapters: tuple[Chapter, ...] = ()
    has_transcript: bool
    has_audio: bool = True
    transcript_mode: TranscriptMode = TranscriptMode.AUTO
    keyframe_count: int
    indexed_mode: IngestMode
    visual_coverage: VisualCoverage = VisualCoverage.NONE
    status: str
    warnings: tuple[str, ...] = ()
    cache_hit: bool
    proxy_cached: bool = Field(
        default=False,
        description=(
            "Whether a silent bounded remote-video proxy is currently retained for targeted "
            "timestamp seeks. False for local sources and when proxy retention is unavailable."
        ),
    )
    proxy_size_bytes: Annotated[int | None, Field(default=None, ge=0)] = None
    proxy_expires_at: str | None = Field(
        default=None,
        description="UTC expiry for the retained proxy; access can extend this LRU/TTL value.",
    )
    pipeline_version: str = PIPELINE_VERSION
    timings: IngestTimings | None = Field(
        default=None,
        description="Request-local timings; absent only for backward-compatible constructed results.",
    )


class TranscriptPage(StrictModel):
    video_id: str = Field(
        description="Canonical Keyframe video ID copied from the successful ingest receipt."
    )
    segments: tuple[TranscriptSegment, ...]
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque continuation token. Copy it byte-for-byte into the immediately following "
            "request and keep that query's scope-defining arguments unchanged."
        ),
    )
    has_more: bool = False
    view: TranscriptView = TranscriptView.EXACT


class MomentSummary(StrictModel):
    moment_id: str = Field(
        description="Opaque retained-moment ID; copy unchanged into video_get_frame."
    )
    start_s: float
    end_s: float
    kind: MomentKind
    classification_confidence: float = 0.0
    stable_seconds: float
    ocr_preview: str
    ocr_confidence: float
    language_guess: str | None = None
    parses: bool | None = None


class MomentPage(StrictModel):
    video_id: str
    moments: tuple[MomentSummary, ...]
    visual_coverage: VisualCoverage = VisualCoverage.NONE
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque continuation token. Copy it byte-for-byte into the immediately following "
            "request and keep that query's scope-defining arguments unchanged."
        ),
    )
    has_more: bool = False


class SearchHit(StrictModel):
    video_id: str
    start_s: float
    end_s: float
    channel: SearchChannel
    snippet: str
    context: str | None = Field(
        default=None,
        description=(
            "Coherent nearby speech with rolling-caption overlap removed for said hits; null "
            "for shown OCR hits. Use this to distinguish an action announcement from action in "
            "progress."
        ),
    )
    score: float
    segment_id: str | None = None
    moment_id: str | None = Field(
        default=None,
        description=(
            "Opaque visual moment ID for shown evidence; copy unchanged into video_get_frame."
        ),
    )


class SearchPage(StrictModel):
    query: str
    hits: tuple[SearchHit, ...]
    visual_coverage: VisualCoverage | None = None
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque continuation token. Copy it byte-for-byte into the immediately following "
            "request and keep that query's scope-defining arguments unchanged."
        ),
    )
    has_more: bool = False


class CodeResult(StrictModel):
    video_id: str
    moment_id: str
    requested_t: float | None = None
    actual_t: float
    language_guess: str | None = None
    code: str
    parses: bool | None = None
    confidence: float
    classification_confidence: float = 0.0
    kind: MomentKind
    visual_coverage: VisualCoverage = VisualCoverage.NONE
    notes: tuple[str, ...] = ()
    render_path: str = Field(
        description=(
            "Absolute OS-native path to a private, temporary copy of the exact attached image."
        )
    )
    render_markdown: str = Field(
        description=(
            "Ready-to-copy Markdown that renders the exact attached image without browser, "
            "shell, download, or permission steps."
        )
    )
    render_expires_at: str = Field(
        description=(
            "UTC ISO-8601 TTL expiry for render_path; global quota pressure may evict it earlier."
        )
    )


class FrameResult(StrictModel):
    video_id: str
    moment_id: str | None = Field(
        description=(
            "Opaque retained-moment ID when the attached image came from the visual index; "
            "null when Keyframe performed a transient exact seek instead."
        )
    )
    start_s: float = Field(
        description=(
            "Start of the retained moment's represented interval, or actual_t for a transient seek."
        )
    )
    end_s: float = Field(
        description=(
            "End of the retained moment's represented interval, or actual_t for a transient seek."
        )
    )
    actual_t: float = Field(description="Decoded timestamp of the attached frame.")
    kind: MomentKind
    region: FrameRegion
    requested_quality: FrameQuality = FrameQuality.AUTO
    evidence_quality: FrameEvidenceQuality = Field(
        default=FrameEvidenceQuality.RETAINED,
        description=(
            "Actual attached-image provenance: retained visual-index artifact, bounded "
            "low-resolution probe seek, or source-backed seek."
        ),
    )
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]
    classification_confidence: float = 0.0
    ocr_text: str = Field(
        default="",
        description=(
            "Untrusted heuristic OCR for this exact attached frame. Use as a fallback when the "
            "host omits the image; do not describe OCR-only evidence as visual inspection."
        ),
    )
    ocr_confidence: float = 0.0
    requested_moment_id: str | None = Field(
        default=None,
        description="The opaque moment_id selector supplied by the caller, when used.",
    )
    requested_t: float | None = Field(
        default=None,
        description="The timestamp selector supplied by the caller, when used.",
    )
    requested_t_covered: bool | None = Field(
        default=None,
        description=(
            "For a retained image, whether requested_t falls inside that moment's start_s/end_s "
            "interval. True for a successful targeted seek; false means the returned retained "
            "image does not cover the requested timestamp."
        ),
    )
    visual_coverage: VisualCoverage = VisualCoverage.NONE
    render_path: str = Field(
        description=(
            "Absolute OS-native path to a private, temporary copy of the exact attached image."
        )
    )
    render_markdown: str = Field(
        description=(
            "Ready-to-copy Markdown that renders the exact attached image without browser, "
            "shell, download, or permission steps."
        )
    )
    render_expires_at: str = Field(
        description=(
            "UTC ISO-8601 TTL expiry for render_path; global quota pressure may evict it earlier."
        )
    )


class CodeSelector(StrictModel):
    moment_id: str | None = None
    t: Annotated[float | None, Field(default=None, ge=0)]

    @model_validator(mode="after")
    def exactly_one_selector(self) -> CodeSelector:
        if (self.moment_id is None) == (self.t is None):
            raise ValueError("Provide exactly one of moment_id or t.")
        return self
