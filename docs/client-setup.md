# Connect Keyframe to coding agents

Keyframe uses local STDIO MCP. The same six tools run in Codex, ChatGPT
desktop, Claude Code, Cursor, and Google Antigravity/Agy; discovery, approval,
and plugin packaging are client responsibilities.

These instructions target local IDE and CLI sessions. A hosted or cloud agent
cannot start a process on your computer from these files.

## Development checkout

After cloning this repository, prepare the locked environment once:

```bash
uv sync --frozen --group dev
```

Open or launch the client from the repository root. The checkout contains all
three non-Codex project registrations:

| Client | MCP discovery | Workflow discovery |
| --- | --- | --- |
| Claude Code | `.mcp.json` | `.claude/skills/keyframe-video-rag/SKILL.md` |
| Cursor | `.cursor/mcp.json` | `.agents/skills/keyframe-video-rag/SKILL.md` |
| Antigravity/Agy | `.agents/mcp_config.json` | `.agents/skills/keyframe-video-rag/SKILL.md` |

These project registrations run the current checkout with `uv run`. The
installable plugin registrations use `uvx` and the immutable `v0.1.0` release
tag. Do not enable both the project registration and an installed Keyframe
plugin in the same workspace: that starts two local servers backed by the same
cache.

## Claude Code

Claude discovers the checked-in `.mcp.json` as a project server. Confirm the
definition, then start a session and approve it from `/mcp`:

```bash
claude mcp get keyframe
claude
```

A project MCP remains pending until approved. Start a new session after adding
the file if an existing session does not see it.

To register the tagged release for every Claude workspace instead, run:

```bash
claude mcp add --transport stdio --scope user keyframe -- \
  uvx --python 3.12 --from \
  "git+https://github.com/MatthewOscar/Keyframe.git@v0.1.0" \
  video-context-mcp serve --transport stdio
```

The Claude plugin includes both the server and the video-RAG skill:

```bash
claude plugin marketplace add MatthewOscar/Keyframe
claude plugin install keyframe@keyframe
```

Before the release tag exists, use the checked-in project MCP for live testing
and run `claude plugin validate --strict plugins/keyframe` for packaging checks.
For checkout-only plugin discovery, launch
`claude --plugin-dir ./plugins/keyframe`; its release-pinned server still cannot
start until the tag exists. Marketplace installs are copied into Claude's
plugin cache, so source edits require a version bump plus marketplace/plugin
update (or reinstall) before `/reload-plugins` activates the updated copy.
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
        "git+https://github.com/MatthewOscar/Keyframe.git@v0.1.0",
        "video-context-mcp",
        "serve",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

To index the repository as a Cursor plugin marketplace, run:

```bash
agent plugin marketplace add --git-ref v0.1.0 \
  https://github.com/MatthewOscar/Keyframe.git
```

Then run `/add-plugin` in Cursor and select **Keyframe**. During local
development, use the checked-in project config; the plugin's `uvx` launcher is
release-pinned. `agent --plugin-dir ./plugins/keyframe` is still useful for
checking plugin discovery, but the server cannot start through that path until
the tag exists. The release tag must contain the Cursor manifests before using
the pinned marketplace command.

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
        "git+https://github.com/MatthewOscar/Keyframe.git@v0.1.0",
        "video-context-mcp",
        "serve",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

The multi-client plugin directory also follows Agy's plugin layout. Validate
and install it from a clone:

```bash
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

Remote URLs need no filesystem grant. For local videos, Keyframe accepts only
paths under Roots advertised by the client or explicit
`KEYFRAME_ALLOWED_ROOTS`. The launcher's working directory is never an implicit
grant.

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

## Client-specific limits

- The Codex plugin retains its documented 180-second startup and 1,900-second
  tool timeouts. Claude uses its millisecond timeout field. Cursor and Agy do
  not document equivalent per-server fields, so their configs intentionally
  omit Codex-only keys.
- Full ingestion is synchronous and can take several minutes. Start in fast
  mode; if a client cancels a full run, retry safely because ingest is locked,
  staged, and atomically published.
- Project configs run the checkout; plugin and global examples require the
  `v0.1.0` tag. Until that human release step is complete, use project mode or
  a local plugin checkout.
- Windows remains preview-level for Keyframe v0.1.0.

The relevant client specifications are maintained by
[Claude Code](https://code.claude.com/docs/en/mcp),
[Cursor](https://cursor.com/docs/mcp), and
[Google Antigravity](https://antigravity.google/docs/mcp).
