# 4Geeks function-tutorial sample

This is Keyframe's small, licensed public-YouTube sample. It demonstrates that
the real remote acquisition path works without committing the original video.
See `ATTRIBUTION.md` and `source-metadata.json` before redistributing any
derived file.

## Contents

- a ready-to-query schema-v3 Keyframe home containing 120 caption segments;
- three representative full frames and automatic crops;
- OCR/classification/parse evidence in the SQLite cache; and
- checksums for every redistributed binary.

The sample produced honest fallback behavior: all three broad IDE scenes were
classified as terminal-like, OCR confidence was approximately 0.62, and the
reconstructed snippets did not parse. Consumers must use the retained source
frame—not OCR—as the authority.

To use the cache from a repository checkout:

```bash
export KEYFRAME_HOME="$PWD/samples/4geeks-function-tutorial/keyframe-home"
uv run video-context-mcp doctor
```

Start Keyframe from that same shell, then query video ID
`youtube-XazswkTqKJI-186e345191`. A refreshed ingest requires network access and may
produce different OCR when native-tool versions change.

The frames visibly contain the instructor and third-party product interfaces.
The CC license covers redistribution of the video-derived material, but no
separate trademark or publicity-right conclusion is claimed. Use the
first-party synthetic fixture for a public demonstration unless a final human
review approves this sample for that recording.
