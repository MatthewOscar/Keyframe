# Build Week submission checklist

Last verified against the [official rules](https://openai.devpost.com/rules) on
2026-07-18. The official page remains the source of truth.

## Implemented in the repository

- [x] Developer Tools project built with Codex and intended for GPT-5.6.
- [x] Installable local project with an exact Python 3.12 dependency lock.
- [x] Public-repository licensing (`Apache-2.0`) and third-party notices.
- [x] Plugin installation instructions and supported-platform notes.
- [x] A network-free, first-party fixture that judges can test immediately.
- [x] README explanation of Codex collaboration and human decisions.
- [x] Reproducible eval prompts, including video evidence to a tested change.
- [x] A narrated demo script designed to remain below three minutes.

## Human release steps

- [ ] Review the complete diff and merge the verified release branch.
- [ ] Create and push immutable tag `v0.1.0`.
- [ ] Create the free PyPI project/Trusted Publisher and confirm the workflow,
      or retain the exact Git-tag launcher already bundled in the plugin.
- [ ] Install the tagged plugin in Codex and ChatGPT desktop and run the
      headline flow with Codex visibly configured to `gpt-5.6`.
- [ ] Upload the first-party synthetic source video and its WebVTT captions as
      a public YouTube tutorial for the remote-ingest demo shot.
- [ ] Run all ten prompts in `evals/cases.json` and record observed outcomes.
- [ ] Record a public narrated YouTube demo no longer than 2:59, using only
      first-party or separately audited media and no unlicensed music.
- [ ] Add the public video URL and public repository URL to Devpost.
- [ ] Run `/feedback` in the primary Codex thread and add its real Session ID
      to the submission and collaboration record.
- [ ] Re-read the live rules immediately before submission.
- [ ] Submit before **July 21, 2026 at 5:00 PM Pacific Time**.

## Submission copy must cover

- the problem, intended developer audience, and six-tool product surface;
- how Codex and GPT-5.6 were used to build and run the judged workflow;
- supported platforms and exact installation/testing instructions;
- the local privacy boundary and when evidence becomes OpenAI model input;
- limitations, heuristic OCR behavior, and the deterministic/LLM split; and
- the repository and publicly visible narrated YouTube demo links.
