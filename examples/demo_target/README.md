# Keyframe demo target

This intentionally incomplete Python module is the destination for the
“video → evidence → tested change” Build Week demo. The prepared first-party
tutorial should explain and show the required `slugify_title` behavior. Codex
must retrieve that evidence with Keyframe, implement the function, and cite the
source timestamps.

Run only this target's tests with:

```bash
python -m unittest discover -s examples/demo_target -p "test_*.py"
```

The checked-in failing state is deliberate. Do not replace it with a finished
implementation before recording the before/after demo.
