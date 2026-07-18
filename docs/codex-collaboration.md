# Codex collaboration record

This document records actual product decisions and the division of work for the
OpenAI Build Week submission. It is not a generated session transcript.

## Human-owned decisions

Matthew Oscar selected and approved these decisions:

- Build **Keyframe**, a local video-RAG developer tool, in
  `MatthewOscar/Keyframe`.
- Keep a typed MCP server as the capability layer and add a thin skill for the
  retrieval workflow, rather than hiding the pipeline in shell instructions.
- Make public YouTube tutorials a P0 source and use `yt-dlp` as the upstream
  extraction engine instead of implementing a provider extractor.
- Support local files, public YouTube, public Loom, and direct media URLs in the
  first release; defer authenticated and protected sources.
- Distribute through PyPI and a repository marketplace plugin.
- Target Codex and ChatGPT desktop locally through STDIO; defer a hosted
  ChatGPT web app.
- Prefer an honest source-frame fallback over claiming incorrect OCR or parse
  success.
- Prioritize the six MCP tools and cache ahead of optional Whisper, a gallery,
  or hosted infrastructure.

## Codex-led work

Codex was asked to turn the approved product specification into an executable
plan and implementation. Work was split across focused agents while the primary
session retained ownership of integration:

- audit the Build Week rules and current MCP/plugin surface;
- inspect the empty starter repository and define architecture and contracts;
- implement acquisition, frame/OCR processing, storage, MCP tools, and tests;
- package the MCP server with a Keyframe workflow skill and marketplace entry;
- create documentation, eval prompts, CI, release automation, and the demo
  script; and
- reconcile agent outputs, run the final verification suite, and fix failures.

Codex proposed the fast-first retrieval pattern, structured-plus-image visual
responses, versioned atomic caching, parse-aware OCR fallback, and the split
between deterministic evidence and model reasoning. Matthew explicitly approved
the complete plan before implementation began.

## GPT-5.6 integration

Keyframe itself does not call an OpenAI model. The judged developer workflow
runs in Codex with GPT-5.6. The model must:

1. choose the appropriate Keyframe tools and search channels;
2. synthesize transcript, OCR, and attached frame evidence;
3. inspect the target repository and implement the demonstrated change;
4. run and interpret tests; and
5. report the result with timestamp citations and uncertainty.

This is the headline eval in `evals/cases.json` and the central flow in the demo
script. GPT-5.6 is therefore responsible for the reasoning and code change, not
for decorative summarization.

## Human checkpoints before submission

- Select and audit the final demo video's redistribution rights.
- Run all evals on the release artifact and record observed results.
- Review the final diff, README claims, package metadata, and public repository.
- Record the primary Codex session ID using `/feedback`.
- Confirm the demo is public, narrated, at most 2:59, and shows the real product.
- Re-read the live Build Week rules immediately before submitting.

## Primary session ID

Not recorded yet. This field must be populated from the real `/feedback` output;
no placeholder value should be submitted as if it were a session ID.
