# Connect Keyframe to coding agents

Keyframe uses local STDIO MCP. The same six tools run in Codex, ChatGPT
desktop, Claude Code, Cursor, and Google Antigravity/Agy; discovery, approval,
and plugin packaging are client responsibilities.

These instructions target local IDE and CLI sessions. A hosted or cloud agent
cannot start a process on your computer from these files.

## Topic discovery

Tagging or selecting Keyframe chooses the analysis capability; it does not make
Keyframe the topic. With no supplied file or URL, a connected client may use
its own web search to find up to three individual public videos. Keyframe then
ingests only the strongest direct match and supplies timestamped evidence. Its
`video_search` tool searches previously indexed media, not the public web. When
the host has no web-search capability, supply a direct video URL instead.

## Direct frame rendering

`video_get_frame` and `video_get_code` each return one MCP image block plus
`render_path`, ready-to-copy `render_markdown`, and `render_expires_at`. The path
contains the exact encoded image bytes in a private platform-native temp
namespace. For a show/share request, the agent should paste `render_markdown`
byte-for-byte, including its `<` and `>` destination delimiters; no browser,
terminal, redownload, screenshot, or permission step is needed.

Rendered images expire after seven days and share a 256 MiB quota, so quota
pressure may evict one earlier. This path is intended for the local STDIO and
localhost desktop flows documented here; a hosted client cannot open a path on
the user's machine. For a sole-image show/share request, a model without image
input must choose from text evidence before making exactly one frame call. It
may still render the image, but must make the returned `render_markdown` its
entire final response. The Markdown alt text includes the decoded timestamp;
the model adds no metadata, OCR, or visual description. Pre-retrieval progress
may state the requested retrieval goal but must not claim that a candidate has
already been visually verified. Multi-evidence analysis can continue with the
structured timestamp, provenance, and explicitly labeled OCR fields.

## Install the latest command-line release

For a normal standalone installation with local Whisper transcription, use:

```bash
uv tool install --python 3.14 'video-context-mcp[whisper]'
video-context-mcp doctor
```

Or use `pip` inside a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install 'video-context-mcp[whisper]'
video-context-mcp doctor
```

These commands select the latest compatible release. Quotes are recommended
because zsh and other shells can interpret `[whisper]` as a wildcard pattern;
the quotes are unrelated to version pinning. A `requirements.txt` entry needs
no quotes. Use `video-context-mcp[whisper]==0.2.7` only when reproducing the
tested Build Week release.

The client and plugin examples below intentionally keep the exact `0.2.7` pin
so their behavior cannot change unexpectedly during evaluation. Remove the
pin from a manual `uvx --from` value only if you prefer to track future PyPI
releases automatically.

## Development checkout

After cloning this repository, prepare the locked environment once:

```bash
uv sync --frozen --all-extras --group dev
```

Open or launch the client from the repository root. The checkout contains all
three non-Codex project registrations:

| Client | MCP discovery | Workflow discovery |
| --- | --- | --- |
| Claude Code | `.mcp.json` | `.claude/skills/keyframe-video-rag/SKILL.md` |
| Cursor | `.cursor/mcp.json` | `.agents/skills/keyframe-video-rag/SKILL.md` |
| Antigravity/Agy | `.agents/mcp_config.json` | `.agents/skills/keyframe-video-rag/SKILL.md` |

These project registrations run the current checkout with `uv run`. The
installable plugin registrations use `uvx`, an isolated Python 3.12 runtime,
and the exact `video-context-mcp[whisper]==0.2.7` PyPI release. This does not
depend on the user's system Python; the PyPI package separately supports Python
3.12-3.14. Do not enable both the project registration and an installed
Keyframe plugin in the same workspace: that starts two local servers backed by
the same cache.

## Codex and ChatGPT desktop

Install the release-pinned marketplace and plugin from a terminal:

```bash
codex plugin marketplace add MatthewOscar/Keyframe --ref v0.2.7
codex plugin add keyframe@keyframe-tools
```

To upgrade an older pinned release, replace both its marketplace snapshot and
installed plugin:

```bash
codex plugin remove keyframe@keyframe-tools
codex plugin marketplace remove keyframe-tools
codex plugin marketplace add MatthewOscar/Keyframe --ref v0.2.7
codex plugin add keyframe@keyframe-tools
```

Restart Codex or ChatGPT desktop, open **Plugins**, and select **Keyframe** from
the `keyframe-tools` marketplace. Start a new chat so the updated skill and MCP
server are loaded. The plugin needs neither a separate `pip` installation nor
the user's system Python.

The OpenAI workflow skill intentionally disables implicit invocation. A
request whose sole deliverable is one photo, screenshot, still, or frame goes
straight to the MCP tool contracts with no skill-file or shell lookup. Select
`keyframe-video-rag` explicitly for multi-evidence analysis that combines
transcript, OCR, code, and multiple visual moments.

## Claude Code

Claude discovers the checked-in `.mcp.json` as a project server. Confirm the
definition, then start a session and approve it from `/mcp`:

```bash
claude mcp get keyframe
claude
```

A project MCP remains pending until approved. Start a new session after adding
the file if an existing session does not see it.

To register the released PyPI package for every Claude workspace instead, run:

```bash
claude mcp add --transport stdio --scope user keyframe -- \
  uvx --python 3.12 --from \
  "video-context-mcp[whisper]==0.2.7" \
  video-context-mcp serve --transport stdio
```

The Claude plugin includes both the server and the video-RAG skill:

```bash
claude plugin marketplace add MatthewOscar/Keyframe
claude plugin install keyframe@keyframe-tools
```

For unreleased development changes, use the checked-in project MCP and run
`claude plugin validate --strict plugins/keyframe` for packaging checks.
Marketplace installs are copied into Claude's plugin cache, so released source
edits require a version bump plus marketplace/plugin update (or reinstall)
before `/reload-plugins` activates the updated copy.
Start a new conversation afterward. For local videos outside the open project,
launch with
`claude --add-dir /absolute/path/to/Videos` or configure
`KEYFRAME_ALLOWED_ROOTS` on the server.

If the first `uvx` install exceeds Claude's MCP startup budget, start that
session with `MCP_TIMEOUT=180000 claude`; subsequent launches reuse uv's cache.
The bundled Claude config sets a 1,900,000 ms tool budget for synchronous full
ingestion.

## Cursor

Cursor discovers `.cursor/mcp.json`. Current Cursor clients support MCP Roots,
but configure `KEYFRAME_ALLOWED_ROOTS` if `/mcp` does not expose the local video
folder. The CLI shows a new project server as requiring approval:

```bash
agent mcp list
agent mcp enable keyframe
agent mcp list-tools keyframe
```

In the IDE, enable Keyframe under **Settings → Tools & MCP**. Start a new chat;
if a newly added config or local plugin is still absent, run **Developer:
Reload Window**.

For a user-wide server, place this entry under `mcpServers` in
`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "keyframe": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--python",
        "3.12",
        "--from",
        "video-context-mcp[whisper]==0.2.7",
        "video-context-mcp",
        "serve",
        "--transport",
        "stdio"
      ],
      "env": {
        "KEYFRAME_ALLOW_TEMP_UPLOADS": "true"
      }
    }
  }
}
```

To index the repository as a Cursor plugin marketplace, run:

```bash
agent plugin marketplace add --git-ref v0.2.7 \
  https://github.com/MatthewOscar/Keyframe.git
```

Then run `/add-plugin` in Cursor and select **Keyframe**. During local
development, use the checked-in project config; the plugin's `uvx` launcher is
PyPI-pinned. `agent --plugin-dir ./plugins/keyframe` can check plugin discovery
before release, but its server starts only after the referenced PyPI version is
published. The Git tag must contain the Cursor manifests before using the
pinned marketplace command.

## Google Antigravity and Agy

Antigravity IDE and Agy discover the workspace server in
`.agents/mcp_config.json`. Open `/mcp` to inspect status, reload the config, and
approve tools. Unconfigured MCP calls begin in Ask mode.

For a user-wide server, put this in `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "keyframe": {
      "command": "uvx",
      "args": [
        "--python",
        "3.12",
        "--from",
        "video-context-mcp[whisper]==0.2.7",
        "video-context-mcp",
        "serve",
        "--transport",
        "stdio"
      ],
      "env": {
        "KEYFRAME_ALLOW_TEMP_UPLOADS": "true"
      }
    }
  }
}
```

The multi-client plugin directory also follows Agy's plugin layout. Clone the
exact release, then validate and install it:

```bash
git clone --branch v0.2.7 --depth 1 \
  https://github.com/MatthewOscar/Keyframe.git
cd Keyframe
agy plugin validate ./plugins/keyframe
agy plugin install ./plugins/keyframe
```

Agy does not currently document installation from a nested directory within a
Git URL, so clone the release and install the local `plugins/keyframe` path
instead of relying on an unverified subdirectory URL.

For local videos, add `KEYFRAME_ALLOWED_ROOTS` to Agy's server `env` unless
`/mcp` confirms that the video directory is available as an MCP Root. Neither
the workspace config nor its `cwd` value grants file access by itself.

## Local video authorization

Remote URLs need no filesystem grant. For local videos and animated GIFs,
Keyframe accepts only paths under Roots advertised by the client, explicit
`KEYFRAME_ALLOWED_ROOTS`, or the private upload staging directory when
`KEYFRAME_ALLOW_TEMP_UPLOADS=true`. The bundled plugin enables only that staging
root; its skill creates a unique child with the OS `mktemp` or random-UUID
equivalent, copies the selected attachment into that child once, keeps it through
any full upgrade, and then removes that exact child. The launcher's working
directory and the rest of the OS temp tree are never implicit grants.

Add an `env` object to the relevant server entry when a client does not expose
the video folder as a Root:

```json
{
  "env": {
    "KEYFRAME_ALLOWED_ROOTS": "/absolute/path/to/Videos"
  }
}
```

Use the operating system path separator for multiple roots (`:` on macOS and
Linux, `;` on Windows). Grant only the smallest directories needed.

Do not guess the upload staging path or copy directly into the shared root. On
an authorization error, use the exact cross-platform upload root returned by
Keyframe, create a unique child beneath it, and remove only that child after the
last possible full upgrade. If you create a custom registration and want the
same attachment workflow, add:

```json
{
  "env": {
    "KEYFRAME_ALLOW_TEMP_UPLOADS": "true"
  }
}
```

On POSIX, Keyframe verifies ownership and restricts its temp namespace and
upload root to mode `0700`. On Windows, directory privacy relies on the inherited
ACL of the current user's temp folder because POSIX ownership and mode bits are
not available.

## Client-specific limits

- The Codex plugin retains its documented 180-second startup and 1,900-second
  tool timeouts. Claude uses its millisecond timeout field. Cursor and Agy do
  not document equivalent per-server fields, so their configs intentionally
  omit Codex-only keys.
- Full ingestion is synchronous and can take several minutes. Start in fast
  mode; if a client cancels a full run, retry safely because ingest is locked,
  staged, and atomically published.
- Project configs run the checkout; global server examples pin the v0.2.7 PyPI
  package, while plugin marketplace commands pin the immutable `v0.2.7` tag.
- Windows remains preview-level for Keyframe v0.2.7.

The relevant client specifications are maintained by
[Claude Code](https://code.claude.com/docs/en/mcp),
[Cursor](https://cursor.com/docs/mcp), and
[Google Antigravity](https://antigravity.google/docs/mcp).
