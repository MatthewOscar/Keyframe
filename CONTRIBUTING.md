# Contributing to Keyframe

Keyframe is an Apache-2.0 Python project supporting Python 3.12-3.14. Python
3.12 remains the reproducible development default, and CI verifies every
supported minor version.

## Set up

Install FFmpeg/ffprobe, Tesseract 5, Node.js 22+, and `uv`, then run:

```bash
uv sync --frozen --group dev
uv run video-context-mcp doctor
```

## Validate a change

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
uv run twine check dist/*
```

Keep MCP stdout protocol-clean; diagnostics belong on stderr. Add tests for
tool-schema changes, cache migrations, source validation, failure cleanup, and
any new extraction heuristic. Expected failures should be actionable MCP errors,
not successful responses containing an `error` string.

## Media fixtures

Prefer small first-party generated fixtures. Do not commit downloaded tutorial
videos or derived frames/transcripts without a verified redistribution and
adaptation license plus a complete entry in `THIRD_PARTY_NOTICES`. Keep binaries
small enough for a normal Git checkout.

## Pull requests

Keep changes focused, explain user-visible behavior and compatibility, and list
the exact validation commands run. Never include cookies, private URLs, cache
databases, local absolute paths, or `/feedback` session IDs in fixtures or logs.
