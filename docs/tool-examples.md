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

A fresh fast-only index returns `visual_coverage="probe"` and at most 12 sparse
visual moments; an existing full index can satisfy a later fast request. Branch
on the returned coverage and transcript availability. Immediately list at most
12 moment summaries; this does not load images. A probe miss is not evidence
that something was absent.

Repeat with `"mode": "full"` for code or terminal sequences, diagram topology,
UI state changes, probe gaps, uncertain or contradictory OCR, and negative
visual claims. A single relevant probe image can settle one targeted visual
fact. A completed full index reports `visual_coverage="full"` and also satisfies
later fast requests.

## Read a bounded transcript page

```json
{
  "video_id": "youtube-VIDEO_ID",
  "start_s": 120,
  "end_s": 180,
  "limit": 40
}
```

If `has_more` is true, send the returned `next_cursor` with the same video and
time bounds. Cursors are valid only while that cached index is unchanged; after
any refresh or re-ingestion, discard outstanding cursors and start again from
the first page.

## Search spoken and visual evidence separately

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

## Browse retained moments

```json
{
  "video_id": "youtube-VIDEO_ID",
  "kind": "any",
  "limit": 12
}
```

Kind, stability, OCR confidence, language, and parse status are heuristic
evidence. They are not a guarantee that reconstruction is correct. Probe
moments have `stable_seconds=0`; only full analysis measures scene stability.

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

## Retrieve the nearest retained frame

```json
{
  "video_id": "youtube-VIDEO_ID",
  "t": 137.5,
  "region": "full"
}
```

Use `"auto_crop"` for the OCR text region. Always cite `actual_t` from the
response, because it can differ from the requested timestamp.

## Expected errors

Invalid sources, unsupported protected videos, missing native tools, malformed
or stale cursors, missing retained frames, and invalid selectors are returned
as actionable MCP tool errors. Do not retry authentication, DRM, playlist, or
livestream failures with cookies; those flows are deliberately out of scope.
