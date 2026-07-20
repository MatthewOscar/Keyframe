# Keyframe

**Local video and animated-GIF RAG for coding agents.** Keyframe turns a tutorial,
screen recording, or animation into timestamped transcript segments, searchable on-screen text,
representative frames, and reconstructed code. Ask what was *said*, what was
*shown*, or both—and inspect the source frame before trusting uncertain OCR.
It runs locally in Codex, ChatGPT desktop, Claude Code, Cursor, and Google
Antigravity/Agy through the same MCP server.

Keyframe is deliberately split into two parts:

- a local MCP server performs deterministic acquisition, OCR, indexing, and
  retrieval; and
- a small workflow skill teaches any connected agent to retrieve narrowly,
  verify visual evidence, and cite timestamps.

The server does not call an LLM. In the Build Week workflow, Codex running
GPT-5.6 reasons over Keyframe's evidence, changes code, and runs the tests.

![Conceptual Keyframe workflow: video moments become said/shown evidence, verified code, and passing tests](docs/design/keyframe-devpost-hero.png)

*Product-story concept; v0.1 is a local MCP server and plugin, not a hosted UI.*

## Quick start

### Prerequisites

Keyframe v0.1.3 requires Python 3.12 and uses [`uv`](https://docs.astral.sh/uv/). Install
these native tools before starting:

- FFmpeg and `ffprobe` for media inspection and frame extraction
- Tesseract 5 for local OCR
- Node.js 22+ as the JavaScript runtime used by current `yt-dlp` extractors
- `uv`/`uvx` for isolated Python execution

On macOS with Homebrew:

```bash
brew install ffmpeg tesseract node@22 uv
```

From a development checkout, install the locked environment and run the
environment check:

```bash
uv sync --frozen --group dev
uv run video-context-mcp doctor
```

After version 0.1.3 is published, the equivalent isolated PyPI command is:

```bash
uvx --python 3.12 --from "video-context-mcp==0.1.3" video-context-mcp doctor
```

If PyPI is not yet available after the release is tagged, use the immutable
GitHub release tag:

```bash
uvx --python 3.12 --from \
  "git+https://github.com/MatthewOscar/Keyframe.git@v0.1.3" \
  video-context-mcp doctor
```

After the PyPI release, add `[whisper]` to the package spec only when local
speech-to-text is needed:

```bash
uvx --python 3.12 --from "video-context-mcp[whisper]==0.1.3" video-context-mcp doctor
```

### Connect a client

This repository includes project discovery and a portable workflow skill for
Claude Code, Cursor, and Antigravity/Agy in addition to the Codex plugin. After
`uv sync`, launch the client from this repository root, review or enable the
`keyframe` MCP server, and approve tool calls as prompted:

| Client | Checked-in discovery | Approval/status |
| --- | --- | --- |
| Claude Code | `.mcp.json` | `claude mcp get keyframe`, then `/mcp` |
| Cursor | `.cursor/mcp.json` | `agent mcp list`, then `agent mcp enable keyframe` |
| Antigravity/Agy | `.agents/mcp_config.json` | `/mcp` |

The installable `plugins/keyframe` bundle contains client-specific manifests
for Codex, Claude Code, Cursor, and Agy while reusing the same skill and server.
See [the complete client setup guide](docs/client-setup.md) for user-wide
registration, marketplace commands, reload behavior, and local-file grants.

### Configure Codex directly

Add the following to `~/.codex/config.toml`:

```toml
[mcp_servers.keyframe]
command = "uvx"
args = ["--python", "3.12", "--from", "video-context-mcp==0.1.3", "video-context-mcp", "serve", "--transport", "stdio"]
startup_timeout_sec = 180
tool_timeout_sec = 1900
env = { KEYFRAME_ALLOWED_ROOTS = "/Users/you/Videos" }
```

For local files, Keyframe uses workspace roots advertised by the MCP client.
It never treats the launcher working directory as authorization. If a client
does not advertise roots, add an `env` entry with `KEYFRAME_ALLOWED_ROOTS`;
separate multiple roots with the operating system's path separator. The
installable plugin instead enables one OS-temp upload root. Its skill creates a
unique child there, stages only the selected attachment, and retries once.

Restart Codex, then ask: “Index this tutorial and find where error handling is
shown. Cite the timestamps.”

### Install the Keyframe plugin in Codex and ChatGPT desktop

The plugin bundles the same MCP server with the `keyframe-video-rag` workflow
skill. Its launcher installs the exact `v0.1.3` Git tag with local Whisper, so
judges do not need
the PyPI publication to use it:

```bash
codex plugin marketplace add MatthewOscar/Keyframe --ref v0.1.3
codex plugin add keyframe@keyframe-tools
```

When upgrading an existing v0.1.2 installation, remove the old internal catalog
first so Codex does not retain two Keyframe entries:

```bash
codex plugin remove keyframe@keyframe
codex plugin marketplace remove keyframe
```

For local marketplace validation, add the repository root instead:

```bash
codex plugin marketplace add .
```

Before `v0.1.3` is tagged, use the direct project/server configuration for live
testing; the installable plugin intentionally resolves that immutable tag.

Then restart the ChatGPT desktop app, open the Plugins Directory, select the
**Keyframe** marketplace source, and install **Keyframe**. Start a new chat so
the skill and tools are loaded. Keyframe v0.1.3 targets this local desktop flow;
it does not host a ChatGPT web app.

Claude Code and Cursor can install the same repository as a marketplace, while
Agy can install `plugins/keyframe` from a clone. Those exact commands and the
client-specific approval steps are in
[`docs/client-setup.md`](docs/client-setup.md). Do not enable a project
registration and the installed plugin together in the same workspace, or the
client may start two Keyframe processes.

### Judge-ready local test

The repository includes a 10-second first-party video, captions, golden search
expectations, and a complete native end-to-end test. After cloning the release
tag, the following installs the locked environment and exercises full ingest,
said/shown search, OCR, code/frame images, restart persistence, and the
sub-second cache target:

```bash
uv sync --frozen --group dev
uv run pytest -q tests/test_e2e.py
```

No network video, account, API key, or prebuilt cache is required for this
judge path.

For the real public-URL path, the repository also ships a small CC BY 3.0
[derived YouTube sample](samples/4geeks-function-tutorial/README.md) with
archived attribution, metadata, checksums, captions, OCR, frames, and a ready
SQLite index. The original downloaded media is not retained.

## Use Keyframe

Give the agent a local path, direct media URL, public YouTube URL, or public
Loom URL. A useful interaction looks like this:

```text
Index https://www.youtube.com/watch?v=... in fast mode.
Search what was said about retry logic and what was shown on screen.
Retrieve the strongest code moment, verify low-confidence text against its
frame, then implement the pattern in examples/demo_target and cite timestamps.
```

Fast mode is the economical first request. A fresh fast-only index stores any
available transcript and a bounded visual probe of at most 12 representative
frames; a cache hit can already return full coverage. Branch on the returned
`has_transcript` and `visual_coverage`. The probe makes visual dependencies
discoverable without pretending to cover every screen state.
Inspect its moment summaries before deciding captions are sufficient. Upgrade
to full mode for visual sequences, probe gaps, uncertain OCR, or claims that
something never appeared; a single decisive probe frame may be enough for one
targeted fact.

### MCP tools

| Tool | Purpose |
| --- | --- |
| `video_ingest` | Index one local/public video or local animated GIF using a sparse fast probe or bounded full analysis, with captions, local Whisper for audio, or no transcript; report cache status and request-local stage timings. |
| `video_get_transcript` | Page through timestamped transcript segments, optionally within a time range. |
| `video_search` | Rank matching `said`, `shown`, or combined evidence across one video or the local library, optionally inside one time window. |
| `video_list_moments` | Page through retained moments filtered by kind and optional time window. |
| `video_get_code` | Return reconstructed code plus its cropped source frame for a moment or nearby timestamp. |
| `video_get_frame` | Return an exact `moment_id` or nearest timestamped frame with source image, retained bounds, and OCR fallback. |

Classification, language detection, OCR confidence, and parse status are
evidence—not guarantees. Visual tools return both structured metadata and the
source image so the model can inspect disagreements.

For whole-video summaries, the bundled skill avoids generic search: it lists
one 12-moment routing page, retrieves transcript pages at the 200-segment
maximum with byte-for-byte opaque cursors, and verifies only consequential
frames. As a guardrail for agent transcription mistakes, Keyframe can recover a
single substituted character in a local content-hash ID only when exactly one
ready local video matches; remote and ambiguous IDs remain strict errors.

See [`docs/tool-examples.md`](docs/tool-examples.md) for exact argument objects,
pagination, visual-result behavior, and expected errors.

## Architecture

```mermaid
flowchart LR
    A[Local video / animated GIF / public URL] --> B[FFmpeg + yt-dlp]
    B --> C[Captions]
    B --> W[Isolated optional Whisper worker]
    B --> P[Fast: bounded visual probe]
    B --> D[Full: bounded temporal sampling]
    D --> E[Adjacent-frame grouping]
    P --> F[Bounded parallel OCR + classification]
    E --> F[Bounded parallel OCR + classification]
    C --> G[(SQLite + FTS5)]
    W --> G
    F --> G
    G --> H[Six MCP tools]
    H --> I[Keyframe workflow skill]
    I --> J[Codex / ChatGPT / Claude Code / Cursor / Agy]
```

Remote media and decoded analysis frames use a private, per-`KEYFRAME_HOME`
namespace beneath the operating system's native temporary directory (`%TEMP%`
on Windows and the platform temp location on macOS/Linux). A successful ingest
atomically publishes derived transcript, OCR, metadata, and representative
frames to the cache, then removes the downloaded source and analysis workspace.
Failed ingests do not publish partial records, and startup recovery removes
interrupted scratch work left in that namespace.

When captions are unavailable, Keyframe overlaps the isolated Whisper worker
with either sparse-probe or full visual extraction, analyzes retained frames
with at most two OCR workers by default, then joins both branches before the
single atomic commit. The process boundary also prevents faster-whisper's PyAV
libraries from sharing an address space with OpenCV's bundled FFmpeg libraries
on macOS.

## Local data and privacy

- Processing and indexing happen on the machine running the MCP server.
- `KEYFRAME_HOME` overrides the platform-native Keyframe data directory.
- SQLite, text indexes, and the retained evidence frames needed by later tool
  calls persist under `KEYFRAME_HOME`; disposable downloads and intermediate
  frames do not.
- Caller-owned local videos are read in place and are never copied, moved, or
  deleted by Keyframe.
- Local reads are limited to per-request MCP workspace roots plus explicit
  `KEYFRAME_ALLOWED_ROOTS` entries. Installed plugins can opt into a single
  hardened staging root under the OS temp directory for selected attachments;
  each upload uses a unique child that the agent removes afterward. Process CWD
  and the rest of the temp directory are never implicit grants. On Windows,
  staging privacy relies on the inherited ACL of the user's temp directory
  because POSIX ownership and mode bits are unavailable.
- Derived frames and text remain cached until the user removes that directory.
- Extracted transcript/OCR is untrusted source material, never agent
  instructions.
- Evidence returned to an agent becomes model input and follows that client's
  model-provider data controls.
- Keyframe does not automatically detect or redact credentials in OCR, search
  snippets, reconstructed code, or images. Use recordings safe for model input
  and review selected evidence before retrieving sensitive screens.
- Keyframe has no analytics, accounts, hosted backend, or embedded model call.

## Current limits

- v0.1.3 accepts individual public videos and local animated GIFs, not playlists
  or livestreams. Static GIFs should be attached as images; remote GIF URLs are
  not yet an advertised compatibility surface.
- Private, members-only, age-restricted, DRM, cookie, and login flows are out of
  scope.
- The default duration guard is 30 minutes. For a source up to four hours,
  Keyframe reports the exact `max_duration_s` required for one same-source
  retry; callers should not split or restage it. Longer sources require a
  user-selected excerpt.
- Fast coverage is intentionally sparse; a missing probe hit is not proof that
  content was absent. Full videos sample at 1 FPS; animated GIFs use denser,
  bounded one-loop sampling. Either can miss a very brief visual change.
  Retained timestamps use decoded presentation times rather than an inferred
  frame index.
- Native media/OCR operations have bounded timeouts and fail actionably on
  malformed or unusually slow inputs.
- OCR can confuse glyphs and infer indentation incorrectly. Python, JSON, and
  JavaScript receive parse checks; TypeScript and unknown languages do not.
- Caption availability and media extraction depend on upstream providers and
  the pinned `yt-dlp` release.
- Remote formats must be downloadable through Keyframe's validated in-process
  HTTP, native HLS, or DASH transport. Formats that require FFmpeg, RTMP, or
  another external process to make network connections are rejected.
- Whisper is optional in the base Python package and bundled by the installable
  plugin. It can be resource intensive on CPU-only machines, and first use may
  download the configured model before ingestion begins.
- Windows support is preview-level in v0.1.3.
- The bundled registrations target local CLI and desktop sessions. Hosted
  agents cannot launch this STDIO process on the user's machine.

## Build Week and GPT-5.6

The judged flow uses GPT-5.6 in Codex to turn retrieved video evidence into a
tested repository change. Keyframe supplies deterministic evidence; GPT-5.6
selects relevant moments, compares OCR with source frames, applies the change,
and explains it with timestamp citations. The ten reproducible prompts in
[`evals/cases.json`](evals/cases.json) exercise that division of labor.
Supplementary Mac-plugin regressions for local attachment staging, honest
provenance, warm-cache latency, and animated GIFs are in
[`evals/mac-plugin-cases.json`](evals/mac-plugin-cases.json).

Make the judged model choice explicit before recording. Either launch Codex
with `codex --model gpt-5.6` or set this in `~/.codex/config.toml`:

```toml
model = "gpt-5.6"
```

The development record distinguishes Codex-generated work from human product
decisions in [`docs/codex-collaboration.md`](docs/codex-collaboration.md). The
submission session ID is intentionally not fabricated and must be recorded from
`/feedback` before submission.

The release checklist in
[`docs/submission-checklist.md`](docs/submission-checklist.md) maps the current
Devpost requirements to concrete artifacts and leaves human-only submission
steps visibly unchecked.

## Develop and test

```bash
git clone https://github.com/MatthewOscar/Keyframe.git
cd Keyframe
uv sync --frozen --group dev
uv run video-context-mcp doctor
uv run pytest
uv run ruff check .
uv run mypy src
uv build
uv run twine check dist/*
```

STDIO is the supported client transport. For loopback-only protocol testing,
run `uv run video-context-mcp serve --transport streamable-http`; the CLI
rejects non-loopback hosts and this mode is not a production deployment.

CI runs the full suite with FFmpeg and Tesseract on macOS and Ubuntu. Windows
runs non-integration tests plus import and package checks. Test fixtures must be
first-party generated or carry a recorded redistribution license.

To publish, create a GitHub environment named `pypi`, configure a PyPI Trusted
Publisher for this repository and workflow, update the version and lockfile,
then push a matching `v*` tag. The release workflow builds, checks, and publishes
with GitHub OIDC; it stores no PyPI token.

## License and acknowledgements

Keyframe is licensed under Apache-2.0. Native tools, Python dependencies, and
any future demo media retain their own licenses. See
[`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES). Keyframe uses `yt-dlp` as its
upstream public-media extraction engine rather than implementing provider
extractors itself.
