# Keyframe demo target

This intentionally incomplete Python module is the destination for the
“recorded demonstration → evidence → tested change” example. The prepared
first-party tutorial explains and shows the required `slugify_title` behavior.
Codex must retrieve that evidence with Keyframe, implement the function, and
cite the source timestamps.

Run only this target's tests with:

```bash
python -m unittest discover -s examples/demo_target -p "test_*.py"
```

The checked-in failing state is deliberate. Do not replace it with a finished
implementation before recording the before/after demo.
