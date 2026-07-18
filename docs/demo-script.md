# Build Week demo script (2:55)

This script intentionally ends four seconds before the 2:59 limit. Record one
continuous narrated product story; trim waits, not evidence. Use a self-recorded
video or media whose redistribution and demo rights are documented in
`THIRD_PARTY_NOTICES`. Do not add unlicensed music.

## 0:00–0:12 — Hook

**On screen:** A tutorial playing beside a repository with a failing demo test.

**Voiceover:** “The answer is in this video—but not only in its transcript.
Keyframe lets Codex search what a tutorial says and what it actually shows.”

## 0:12–0:30 — Product and architecture

**On screen:** Keyframe logo, then the six MCP tools in Codex.

**Voiceover:** “Keyframe is a local MCP server plus a retrieval skill. FFmpeg,
Tesseract, and yt-dlp extract deterministic evidence into a local SQLite index.
There is no model call inside the server.”

## 0:30–0:52 — Install and ingest

**On screen:** Run `video-context-mcp doctor`, then ask Codex to ingest the
first-party synthetic fixture from its prepared public YouTube upload in fast
mode. Keep the URL and returned metadata visible.

**Voiceover:** “One isolated `uvx` command checks the native tools. I give
Keyframe a public tutorial URL; fast mode captures metadata and captions first.
A repeated ingest reuses the cache.”

## 0:52–1:18 — Search “said” versus “shown”

**On screen:** Search the same concept first with `channel="said"`, then
`channel="shown"`. Highlight different timestamped hits.

**Voiceover:** “The spoken explanation and the on-screen implementation are
separate search channels. When the visual index is needed, the skill escalates
to full ingestion, samples frames, groups stable scenes, and indexes OCR.”

## 1:18–1:44 — Verify code visually

**On screen:** Open the best `video_get_code` result. Show structured OCR,
confidence/parse status, timestamp, and attached crop together.

**Voiceover:** “OCR is never presented as ground truth. Keyframe returns the
reconstructed code and its source crop, so Codex can inspect low confidence or
a failed parse instead of inventing missing characters.”

## 1:44–2:10 — GPT-5.6 changes the repository

**On screen:** Ask Codex, configured with GPT-5.6, to apply the demonstrated
pattern to `examples/demo_target`. Show the concise diff.

**Voiceover:** “This is where GPT-5.6 is essential. It combines the spoken
rationale, visual source, and repository context, then implements the smallest
supported change. Keyframe remains the evidence layer.”

## 2:10–2:38 — Prove the result

**On screen:** Run the demo-target tests, show them passing, then show Codex's
answer with exact video timestamps.

**Voiceover:** “The repository tests prove the change, and the answer cites the
moments that justified it. A reviewer can reproduce the same flow with the
included eval case rather than trusting a hand-picked screenshot.”

## 2:38–2:55 — Close

**On screen:** Keyframe wordmark and the flow: video → evidence → tested change.

**Voiceover:** “Keyframe turns hours of developer video into inspectable,
timestamped context for Codex and ChatGPT desktop—search what was said, verify
what was shown, and ship what was taught.”

## Capture checklist

- Configure Codex to use GPT-5.6 and make the model name visible once.
- Warm only dependency downloads; do not pre-populate the video cache for the
  first-ingest shot.
- Upload `tests/fixtures/keyframe-synthetic.mp4` as a public source video and
  attach `keyframe-synthetic.en.vtt` as its English captions. Keep the local
  fixture as the identical fallback.
- Zoom the terminal and crop notifications that expose usernames or paths.
- Record a clean run before editing the script around observed timings.
- Keep the final upload public, narrated, and no longer than 2:59.
