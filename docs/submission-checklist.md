# Build Week submission checklist

Last verified against the [official rules](https://openai.devpost.com/rules) on
2026-07-20. The official page remains the source of truth.

## Implemented in the repository

- [x] Developer Tools project built with Codex and intended for GPT-5.6.
- [x] Installable local project with one locked dependency graph for Python
      3.12-3.14 and CI coverage for every supported minor version.
- [x] Public-repository licensing (`Apache-2.0`) and third-party notices.
- [x] Plugin and direct-MCP instructions for Codex, Claude Code, Cursor, and
      Antigravity/Agy, with supported-platform notes.
- [x] Release-pinned PyPI/plugin installation and a first-party judge test that
      do not require rebuilding the project from source.
- [x] A network-free, first-party fixture that judges can test immediately.
- [x] README explanation of Codex collaboration and human decisions.
- [x] Reproducible eval prompts, including video evidence to a tested change.
- [x] A narrated demo script designed to remain below three minutes.
- [x] Internal factual Devpost worksheet with no-build test instructions and
      explicit placeholders for the demo URL and real Session ID; Matthew must
      independently write the submitted prose.

## Human release steps

- [ ] Matthew independently writes every Devpost submission field; do not paste
      AI-generated prose from `docs/devpost-submission.md`.
- [x] Review the complete v0.2.2 diff, commit it to `main`, and confirm CI.
- [x] Review the complete v0.2.3 corrective diff and pass the local release
      gate.
- [x] Commit and push v0.2.3 to `main`, then confirm the full CI matrix.
- [x] Review the complete v0.2.4 frame-routing diff and pass the local release
      gate.
- [x] Commit and push v0.2.4 to `main`, then confirm the full CI matrix.
- [x] Review the v0.2.5 single-frame routing correction and pass the local
      release gate.
- [ ] Commit and push v0.2.5 to `main`, then confirm the full CI matrix.
- [x] Create and push immutable tag `v0.1.0`.
- [x] Create and push the verified agent-efficiency release tag `v0.1.3`.
- [x] Create the PyPI project, configure Trusted Publishing, and publish the
      verified `video-context-mcp` v0.1.3 wheel and source distribution.
- [x] Create and push the verified Python 3.14/setup release tag `v0.1.4`, then
      confirm its PyPI and GitHub releases complete successfully.
- [x] Create and push the verified smart-retrieval release tag `v0.2.0`, then
      confirm its PyPI and GitHub releases complete successfully.
- [x] Create and push the Windows/frame-sharing patch release tag `v0.2.1`,
      then confirm its PyPI and GitHub releases complete successfully.
- [x] Create and push the reliable inline-rendering release tag `v0.2.2`,
      then confirm its PyPI and GitHub releases complete successfully.
- [x] Create and push the corrective inline-frame-selection release tag
      `v0.2.3`, then confirm its PyPI and GitHub releases complete successfully.
- [x] Create and push the deferred-routing correction release tag `v0.2.4`,
      then confirm its PyPI and GitHub releases complete successfully.
- [ ] Create and push the single-frame routing correction release tag `v0.2.5`,
      then confirm its PyPI and GitHub releases complete successfully.
- [ ] Install the tagged plugin in Codex and ChatGPT desktop and run the
      headline flow with Codex visibly configured to `gpt-5.6`.
- [ ] Smoke-install the tagged release in Claude Code, Cursor, and Agy; approve
      Keyframe, list all six tools, and run one cached query in each client.
- [ ] Upload the first-party synthetic source video and its WebVTT captions as
      a public YouTube tutorial for the remote-ingest demo shot.
- [ ] Run all ten prompts in `evals/cases.json` and record observed outcomes.
- [ ] Run the Mac plugin regressions in `evals/mac-plugin-cases.json` and record
      attachment staging, provenance, warm-cache timing, no-vision inline frame
      rendering, forward visual accuracy, and animated-GIF results.
- [ ] Record a clear, English-language narrated demo no longer than 2:59 that
      shows the working product and explicitly explains how Codex and GPT-5.6
      were used.
- [ ] Complete a final trademark, copyright, privacy, and credential review of
      every demo frame and submission asset; use no unlicensed music or media.
- [ ] Select **Developer Tools** on Devpost and finalize the English project
      description, supported platforms, and no-build testing instructions.
- [ ] Add the public demo-video URL and public repository URL to Devpost.
- [ ] Run `/feedback` in the primary Codex thread and enter its real Session ID
      in the Devpost submission.
- [ ] Confirm every submitted text, video, and testing instruction is in English.
- [ ] Keep the repository, package, and judge test free and unrestricted through
      the end of judging on **August 5, 2026 at 5:00 PM Pacific Time**.
- [ ] Re-read the live rules immediately before submission.
- [ ] Submit before **July 21, 2026 at 5:00 PM Pacific Time / 7:00 PM Central
      Time**.

## Submission copy must cover

- the problem, intended developer audience, and six-tool product surface;
- how Codex and GPT-5.6 were used to build and run the judged workflow;
- supported platforms and exact installation/testing instructions;
- the local privacy boundary and when evidence becomes OpenAI model input;
- limitations, heuristic OCR behavior, and the deterministic/LLM split; and
- the repository and publicly visible narrated YouTube demo links.
