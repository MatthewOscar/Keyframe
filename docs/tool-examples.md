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
user for a shorter excerpt when the four-hour hard maximum is exceeded.

Successful ingests include request-local `timings`. `total_ms` is authoritative;
transcription and visual processing may overlap, so component values must not
be summed. A null component means that stage was skipped or reused.

Repeat with `"mode": "full"` for code or terminal sequences, diagram topology,
UI state changes, probe gaps, uncertain or contradictory OCR, and negative
visual claims. A single relevant probe image can settle one targeted visual
fact. A completed full index reports `visual_coverage="full"` and also satisfies
later fast requests.

Local animated GIFs follow the same calls. They report `has_audio=false`, skip
Whisper under `auto`, and use denser but bounded one-loop sampling in full mode.
Static GIFs should be supplied as ordinary images.

## Read a bounded transcript page

Transcript pages default to the maximum 200 segments so whole-video retrieval
uses the fewest calls. Set a smaller explicit limit for narrow time-bounded
reads, as in this example:

```json
{
  "video_id": "youtube-VIDEO_ID",
  "start_s": 120,
  "end_s": 180,
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

Use one fast ingest, one `video_list_moments` request with `kind="any"` and
`limit=12`, then `video_get_transcript` with `limit=200` and no time bounds.
Follow `next_cursor` only while `has_more=true`, and inspect at most four frames
for consequential visual claims. Skip generic searches; reserve `video_search`
for targeted questions. If a full upgrade is necessary, list moments once for
that new index generation because prior moment IDs and cursors are invalid.

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
code metadata plus one MCP image block. When `parses` is false/null or
confidence is low, inspect the image and preserve uncertainty. Moment IDs are
opaque and generation-scoped; retrieve fresh IDs after upgrading probe coverage
to full or performing any other refresh/re-ingestion.

## Retrieve one exact retained frame

```json
{
  "video_id": "youtube-VIDEO_ID",
  "moment_id": "MOMENT_ID_FROM_BOUNDED_SHOWN_SEARCH",
  "region": "full"
}
```

Provide exactly one of `moment_id` or `t`, never both. Prefer the unchanged
generation-scoped `moment_id` from bounded shown search or moment listing. Use a
timestamp only when no moment ID is available:

```json
{
  "video_id": "youtube-VIDEO_ID",
  "t": 137.5,
  "region": "auto_crop"
}
```

The structured result includes `requested_moment_id` or `requested_t`, retained
`start_s`/`end_s`, `requested_t_covered`, `actual_t`, `ocr_text`,
`ocr_confidence`, and `visual_coverage`; the MCP result also normally includes
an image block. Cite `actual_t`, and treat `requested_t_covered=false` under
probe coverage as a gap requiring full coverage. If the host says image content was omitted
because the model lacks image input, do not say the frame was seen or visually
confirmed. Label the result OCR-derived and corroborate an exact identity with
same-window full-index OCR from an adjacent moment, or preserve uncertainty.

## Expected errors

Invalid sources, unsupported protected videos, missing native tools, malformed
or stale cursors, missing retained frames, and invalid selectors are returned
as actionable MCP tool errors. Do not retry authentication, DRM, playlist, or
livestream failures with cookies; those flows are deliberately out of scope.
Do not claim a Keyframe result unless ingest returned `status="ready"` and a
`video_id`; a tool error followed by native media analysis is not Keyframe evidence.
