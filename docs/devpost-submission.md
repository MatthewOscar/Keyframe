# Keyframe — Devpost submission copy

This is an internal factual worksheet, not submission copy. Matthew must write
the final Devpost text himself and must not paste AI-generated prose from this
file. Use it only to verify facts, links, limits, and missing placeholders after
the final v0.2.0 release and demo are public.

## Submission fields

- **Project name:** Keyframe
- **Category:** Developer Tools
- **Tagline:** Search what developer videos say, verify what they show, and ship
  what they teach.
- **Repository:** https://github.com/MatthewOscar/Keyframe
- **Public demo video:** `[ADD_PUBLIC_YOUTUBE_DEMO_URL]`
- **Codex `/feedback` Session ID:** `[ADD_REAL_SESSION_ID]`

## Inspiration

Developer knowledge is often trapped in tutorials, screen recordings, office
hours, and animated demos. Captions capture the narration but miss the exact
code, terminal output, diagrams, and UI state visible on screen. Scrubbing a
long recording manually is slow, while asking a model to watch the entire file
can be expensive and can make its evidence difficult to inspect.

Keyframe gives coding agents a local, searchable evidence layer for video. It
lets the agent retrieve only the relevant transcript, OCR, and source frames,
then use that evidence to answer a question or implement a demonstrated change.

## What it does

Keyframe is a local MCP server plus a small video-research skill. It accepts an
individual local video, animated GIF, public YouTube or Loom video, or supported
direct media URL and exposes six tools:

1. ingest a sparse fast index or a full visual index;
2. page through timestamped transcript segments;
3. search what was **said**, what was **shown**, or both;
4. list stable visual moments such as code, terminal, slide, or diagram scenes;
5. reconstruct code while returning its cropped source frame; and
6. retrieve an exact retained moment or nearest timestamped frame.

Fast ingestion captures available captions plus a bounded visual scout. The
skill inspects that coverage and escalates to full ingestion when the question
depends on visuals, a probe has a gap, or OCR is uncertain. Results persist in
a versioned SQLite/FTS5 cache, so repeated ingestion and cached queries are
fast. Local files are processed on the user's machine, and the server itself
makes no model call.

## How we built it

The Python 3.12-3.14 server uses the MCP Python SDK and Pydantic for typed tool
contracts. `yt-dlp` handles supported public-media extraction; FFmpeg and
ffprobe inspect media and sample frames; perceptual hashing groups adjacent
similar frames; Tesseract TSV provides local OCR and geometry; SQLite FTS5
indexes transcript and on-screen text; and optional faster-whisper transcribes
local videos without captions.

In full mode, Keyframe samples video at 1 FPS, groups stable scenes, retains
representative evidence, classifies it heuristically, reconstructs indentation
from OCR boxes, and parse-checks Python, JSON, and JavaScript. Each ingest is
locked, staged, and published atomically. Downloaded remote media and
intermediate frames are removed after derived evidence is committed.

The same release is distributed as the `video-context-mcp` PyPI package and a
Keyframe marketplace plugin for Codex and ChatGPT desktop, with compatible
bundles for Claude Code, Cursor, and Google Antigravity/Agy.

## How Codex and GPT-5.6 were used

Codex accelerated the build by converting an approved product specification
into the MCP contracts, acquisition/OCR/cache pipeline, automated tests, plugin
manifests, evals, cross-platform CI, release workflow, and documentation.
Focused Codex agents audited independent surfaces in parallel, while the
primary thread integrated the changes and diagnosed failures from real local
and public-video runs.

Matthew Wyatt made the key product and engineering decisions: use an MCP server
plus a retrieval skill; keep evidence extraction deterministic and local; use
`yt-dlp` rather than build a provider extractor; include limited visual
ingestion in the fast path; require source-frame verification for uncertain
OCR; and prioritize a reliable desktop plugin over a hosted web app. Real agent
tests exposed weak-model hallucinations and local-upload problems, and Matthew
used those results to approve or reject each iteration.

Keyframe does not hide an OpenAI call inside its server. In the judged workflow,
Codex running GPT-5.6 is the reasoning layer: it selects Keyframe evidence,
compares OCR with source frames, edits the target repository, runs tests, and
cites the timestamps that support the change.

## Challenges

- Captions and visuals are complementary. A fast transcript-only path was
  quick, but it could miss the exact item shown on screen. The bounded visual
  scout makes that dependency visible without claiming full coverage.
- OCR is heuristic. Keyframe reports confidence and parse status honestly and
  returns the original crop instead of silently treating reconstructed text as
  truth.
- Local desktop clients expose different plugin, timeout, and filesystem-root
  behavior. The release uses a hardened OS-temp staging root for selected
  attachments and client-specific manifests around one protocol-clean server.
- Video ingestion is expensive relative to text retrieval. Parallel Whisper
  and visual branches, bounded workers, persistent caches, and narrow retrieval
  keep the workflow responsive without sacrificing evidence.

## Accomplishments

- Six typed MCP tools with structured text results and bounded image evidence.
- Fast visual scouting, full video analysis, animated-GIF support, and optional
  local Whisper transcription.
- Atomic, persistent caching with stable opaque pagination and actionable
  failures.
- A first-party 10-second fixture, complete native end-to-end test, reproducible
  eval prompts, and a no-build judge path through the published plugin.
- Verified Python 3.12-3.14 support on the primary macOS target, full CI on
  macOS and Ubuntu, and Windows preview smoke coverage.
- Release-pinned PyPI and marketplace distribution with no hosted backend,
  account, analytics, or API key.

## What we learned

Retrieval quality is not only a ranking problem. Agents need an explicit policy
for when transcript evidence is sufficient, when a visual probe is only a
scout, and when an exact source image must be inspected. We also learned that
typed IDs and opaque cursors need workflow-level guardrails because a model can
mistype otherwise valid retrieval state. Finally, deterministic evidence makes
model differences measurable: stronger reasoning models use the same frames
and timestamps more reliably, while lighter models can still mis-synthesize
similar visual steps.

## What's next

- Continue improving retrieval policies and measured light-model reliability.
- Add richer code reconstruction and optional OCR backends without weakening
  the honest source-frame fallback.
- Expand verified Windows and Intel-macOS support where native Whisper wheels
  permit it.
- Explore a hosted, user-authorized transport for ChatGPT web while preserving
  the current local privacy boundary.

## Supported platforms

- **Primary:** Apple Silicon macOS, with Python 3.12-3.14. The Whisper/plugin
  path requires macOS 14 or newer.
- **Supported:** Ubuntu Linux x64, with Python 3.12-3.14.
- **Preview:** Windows x64, with unit, import, Whisper-dependency, and package
  smoke coverage.

Intel macOS is not a supported Whisper/plugin target in v0.2.0. Keyframe uses
local STDIO MCP and therefore targets desktop and local coding-agent sessions,
not ChatGPT web.

## No-build testing instructions

Install FFmpeg/ffprobe, Tesseract 5, Node.js 22+, and `uv`. Then install the
release plugin:

```bash
codex plugin marketplace add MatthewOscar/Keyframe --ref v0.2.0
codex plugin add keyframe@keyframe-tools
```

Clone the release only to obtain its first-party fixture, then launch Codex;
the MCP server runs from the published wheel and is not rebuilt from source:

```bash
git clone --branch v0.2.0 --depth 1 \
  https://github.com/MatthewOscar/Keyframe.git
cd Keyframe
codex --model gpt-5.6
```

Ask:

```text
Index tests/fixtures/keyframe-synthetic.mp4 in full mode. Search what was said
about normalizing non-alphanumeric separators and what was shown for slugify.
Inspect the decisive source frame, report whether the reconstructed Python
parses, and cite timestamps.
```

Expected evidence: a spoken hit beginning at `00:03`, a `slugify_title` code
moment around `00:03-00:07`, and either parse-valid reconstructed Python or an
explicit OCR fallback accompanied by its source image.

## Built with

Codex, GPT-5.6, Python, MCP, Pydantic, SQLite FTS5, FFmpeg, Tesseract,
`yt-dlp`, faster-whisper, OpenCV, Pillow, and `uv`.
