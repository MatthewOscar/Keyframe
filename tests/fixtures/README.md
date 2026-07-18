# Keyframe synthetic fixture

`keyframe-synthetic.mp4` is a first-party, generated test video. It contains no
downloaded footage, voices, logos, or third-party media. Its three static scenes
exercise slide, Python-code, and terminal retrieval. The adjacent WebVTT file is
the fixture's deterministic transcript.

Regenerate the MP4 with:

```bash
python tests/fixtures/generate_fixture.py --force
```

The generator uses Pillow (a runtime dependency) to rasterize the synthetic
screens and FFmpeg to encode them. Font rendering and encoder bytes may vary by
platform, so a regenerated file can have a different SHA-256 identity while
retaining the semantic expectations in `golden.json`.

The fixture source, generated media, captions, and metadata are licensed under
the repository's Apache-2.0 license.
