# MCP tool examples

These are argument objects for the six Keyframe tools. Actual IDs and cursors
come from prior responses; clients must preserve opaque cursors unchanged.

## Ingest fast, then upgrade only when needed

```json
{
  "source": "https://www.youtube.com/watch?v=VIDEO_ID",
  "mode": "fast",
  "transcript_mode": "auto",
  "max_duration_s": 1800,
  "refresh": false
}
```

A fresh fast-only index retains at most 12 sparse visual moments and returns
`visual_coverage="probe"`; an existing full index can satisfy a later fast
request. Branch on the returned coverage, `has_audio`, and transcript
availability. Call `video_list_moments` once with `kind="any"` and `limit=12` to
read its routing summaries; this does not load images. A probe miss is not
evidence that something was absent.

The default 1,800-second value is an explicit resource guard, not a format
limit. If Keyframe reports a longer duration and supplies an exact
`max_duration_s` value at or below 14,400, retry once with the same source and
options, changing only that value. Do not split or restage the source. Ask the
user for a shorter excerpt when the four-hour hard maximum is exceeded. A
ready cache entry is returned before this processing guard is applied, so a
previously indexed long video opens in one call with the default value.

Successful ingests include request-local `timings`. `total_ms` is authoritative;
transcription and visual processing may overlap, so component values must not
be summed. A null component means that stage was skipped or reused. Fast remote
ingests may also report a bounded silent `proxy_cached` copy with its byte size
and expiry. That low-resolution proxy lets later timestamp requests seek one
frame without upgrading the entire visual index.

Repeat with `"mode": "full"` for code or terminal sequences, diagram topology,
multi-step UI state changes, several unresolved or contradictory moments, and
negative visual claims. A targeted local/proxy seek can settle one probe gap
without a full pass. A completed full index reports `visual_coverage="full"`
and also satisfies later fast requests.

Local animated GIFs follow the same calls. They report `has_audio=false`, skip
Whisper under `auto`, and use denser but bounded one-loop sampling in full mode.
Static GIFs should be supplied as ordinary images.

## Choose an exact or compact transcript view

Use `view="compact"` for a broad summary. It removes overlap from rolling
automatic captions and groups speech into deterministic 60-second blocks, so a
115-minute video normally fits in one 200-block call. Use `view="exact"` for a
quotation or a bounded follow-up where the original cue timing matters:

All `start_s`/`end_s` filters are half-open: `start_s` is inclusive and `end_s`
is exclusive. This keeps a moment beginning at the next chapter boundary out
of the preceding chapter while retaining zero-duration probe evidence at the
start of a window.

```json
{
  "video_id": "youtube-VIDEO_ID",
  "start_s": 120,
  "end_s": 180,
  "view": "exact",
  "limit": 40
}
```

If `has_more` is true, copy the returned `next_cursor` byte-for-byte into the
immediately following call with the same video and time bounds. Never decode,
shorten, retype, or reconstruct a cursor. If one is rejected, discard it and
restart that exact query once with the cursor omitted. Cursors are valid only
while that cached index is unchanged; after any refresh or re-ingestion,
discard outstanding cursors and start again from the first page. Search cursors
also require the same query, video, channel, `start_s`, and `end_s`; moment
cursors require the same video, kind, `start_s`, and `end_s`. Page limits may
change because they are not part of cursor scope.

## Summarize a whole video efficiently

For a generic video over 30 minutes, use the descriptive ingest chapters as the
outline, one `video_list_moments` request with `kind="any"` and `limit=12`, and
one `video_get_transcript` request with `view="compact"` and `limit=200`. Follow
a compact cursor only when the fixed runtime exceeds that page. Inspect at most
two consequential frames. Do not fan transcript retrieval across agents or
upgrade to full merely because the video contains a demonstration. Reserve
bounded exact transcript windows, targeted search, and additional frames for
specific or consequential follow-ups.

## Anchor speech, then search the same visual window

```json
{
  "query": "retry backoff",
  "video_id": "youtube-VIDEO_ID",
  "channel": "said",
  "limit": 3
}
```

Use `"shown"` for OCR evidence or `"all"` to compare both channels. Search hits
carry either a transcript `segment_id` or a visual `moment_id`. A search scoped
to one video also returns its `visual_coverage` so empty probe results cannot be
mistaken for exhaustive absence. Library-wide searches return no single
coverage value; scope follow-up searches to one video before making any visual
coverage or absence decision.

Said hits also carry a coherent nearby `context` field with rolling automatic
caption overlap removed. Use it to distinguish an announcement such as “it is
time to…” from narration that describes the requested action in progress; the
ranked `snippet` can be only 0.01 seconds long.

For a referential question such as "which issue did they fix?", first locate
the spoken anchor and read its bounded transcript context. Reuse that episode's
bounds for shown search instead of accepting a clearer title from elsewhere:

```json
{
  "query": "merged issue",
  "video_id": "youtube-VIDEO_ID",
  "channel": "shown",
  "start_s": 720,
  "end_s": 810,
  "limit": 4
}
```

Reject identity candidates outside `720..810`; do not join an earlier title to
a later statement that an issue was fixed.

## Browse retained moments

```json
{
  "video_id": "youtube-VIDEO_ID",
  "kind": "any",
  "start_s": 720,
  "end_s": 810,
  "limit": 8
}
```

Kind, stability, OCR confidence, language, and parse status are heuristic
evidence. They are not a guarantee that reconstruction is correct. Probe
moments have `stable_seconds=0`; only full analysis measures scene stability.
Use bounded pages for a targeted episode rather than dumping every moment.

## Retrieve code and its source crop

```json
{
  "video_id": "youtube-VIDEO_ID",
  "moment_id": "MOMENT_ID_FROM_SEARCH"
}
```

Provide exactly one of `moment_id` or `t`. The response contains structured
code metadata plus one MCP image block. It also returns `render_path`,
ready-to-copy `render_markdown`, and `render_expires_at` for a private temporary
copy containing the exact same encoded bytes. When `parses` is false/null or
confidence is low, inspect the image and preserve uncertainty. Moment IDs are
opaque and generation-scoped; retrieve fresh IDs after upgrading probe coverage
to full or performing any other refresh/re-ingestion.

## Retrieve one decisive frame

```json
{
  "video_id": "youtube-VIDEO_ID",
  "moment_id": "MOMENT_ID_FROM_BOUNDED_SHOWN_SEARCH",
  "region": "full",
  "quality": "auto"
}
```

Provide exactly one of `moment_id` or `t`, never both. Prefer the unchanged
generation-scoped `moment_id` from bounded shown search or moment listing. Use a
timestamp only when no moment ID is available:

```json
{
  "video_id": "youtube-VIDEO_ID",
  "t": 137.5,
  "region": "auto_crop",
  "quality": "auto"
}
```

`quality="auto"` reuses a retained moment when it covers the request, then seeks
the exact timestamp from an authorized unchanged local source or a retained
low-resolution remote proxy when it does not. Use `quality="probe"` to prefer a
bounded proxy/local seek. `quality="source"` requires the original local file;
remote source-quality extraction is rejected rather than allowing FFmpeg to
open an unvalidated network connection.

An older or expired remote cache may report `proxy_cached=false`. If an auto
timestamp call then returns retained evidence with `requested_t_covered=false`,
repeat the original fast ingest once with `refresh=true`, discard prior moment
IDs/cursors, and retry that timestamp. This refresh rebuilds the small seek
proxy without running full-video OCR.

The structured result reports `requested_quality`, `evidence_quality`, pixel
dimensions, the selector, retained bounds when applicable,
`requested_t_covered`, `actual_t`, OCR/confidence, visual coverage,
`render_path`, `render_markdown`, and `render_expires_at`. The temporary path is
OS-native; its Markdown destination uses forward slashes and angle brackets so
Windows paths and spaces render correctly. Cite `actual_t` and describe the
image only after visual inspection rather than inferring from a nearby probe.
A targeted seek can settle a probe gap without full-video OCR; full mode is
still appropriate for a sequence, an exhaustive visual claim, or several
unresolved moments.

For “show/share a photo” requests, paste the selected result's
`render_markdown` byte-for-byte, including its `<` and `>` destination
delimiters, and stop. Do not retype, normalize, or reformat it. Do not open a
browser, invoke terminal or shell tools, download media, manipulate playback,
take a screenshot, create a second copy, or request permission. Inspect at most
two distinct candidates, never retrieve the same moment/timestamp twice, and
never request `quality="source"` for remote video. For a whole-object or overview request,
use `region="full"` and align the candidate to narration where the requested
action is in progress or just completed. A section title or a transition such
as “now” or “it is time to” is routing evidence, not proof that the action is
already visible; if only a title matches, search the object in spoken evidence
or retrieve one bounded section transcript. Treat `kind` as heuristic and
reject a slide/diagram as a title card only when title-like OCR corroborates it.
Use an approximately eight-second post-anchor fallback only when no
action-aligned timestamp exists.

A model with image input may inspect and accurately describe the selected
frame and may inspect one distinct second candidate. A model without image
input must finish action-aligned timestamp selection from text evidence first,
make exactly one frame call, paste that call's `render_markdown` immediately,
and stop. Its accompanying text is limited to timestamp, provenance, and
meaningful text explicitly labeled `Tesseract OCR:`; omit low-confidence or
meaningless OCR. OCR alone does not justify claims about visible objects,
layout, placement, condition, or framing. A no-vision model also cannot call a
frame clear, clean, best, representative, or otherwise judge visual quality.

Rendered-frame files have a seven-day TTL and share a 256 MiB quota in the
private Keyframe temp namespace; quota pressure can evict them earlier. Keyframe
prunes them at startup and after publication. They are disposable chat-display
artifacts, not user-selected exports.

## Prune retained remote proxies

Silent low-resolution remote proxies expire after seven days and are kept under
a 2 GiB global least-recently-used quota by default. Remove expired or
over-quota entries immediately with:

```bash
video-context-mcp cache prune
```

Set `KEYFRAME_PROXY_TTL_S=0` or `KEYFRAME_PROXY_CACHE_BYTES=0` to disable proxy
retention. Both values are non-negative integers; the byte setting is an exact
quota.

## Expected errors

Invalid sources, unsupported protected videos, missing native tools, malformed
or stale cursors, missing retained frames, and invalid selectors are returned
as actionable MCP tool errors. Do not retry authentication, DRM, playlist, or
livestream failures with cookies; those flows are deliberately out of scope.
Do not claim a Keyframe result unless ingest returned `status="ready"` and a
`video_id`; a tool error followed by native media analysis is not Keyframe evidence.
