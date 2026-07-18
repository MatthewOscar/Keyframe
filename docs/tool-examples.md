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

Repeat with `"mode": "full"` when the question depends on code, terminal
output, slides, diagrams, or exact frames. A completed full index also satisfies
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
time bounds.

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
carry either a transcript `segment_id` or a visual `moment_id`.

## Browse retained moments

```json
{
  "video_id": "youtube-VIDEO_ID",
  "kind": "code",
  "limit": 20
}
```

Kind, stability, OCR confidence, language, and parse status are heuristic
evidence. They are not a guarantee that reconstruction is correct.

## Retrieve code and its source crop

```json
{
  "video_id": "youtube-VIDEO_ID",
  "moment_id": "youtube-VIDEO_ID:m:4"
}
```

Provide exactly one of `moment_id` or `t`. The response contains structured
code metadata plus one MCP image block. When `parses` is false/null or
confidence is low, inspect the image and preserve uncertainty.

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
cursors, unavailable fast-mode frames, and invalid selectors are returned as
actionable MCP tool errors. Do not retry authentication, DRM, playlist, or
livestream failures with cookies; those flows are deliberately out of scope.
