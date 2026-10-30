# Plan: hello.py prints CAPTURE_PLAN_EDIT_VIM

## Context
`hello.py` currently prints `CAPTURE_PLAN_REVISED`. You want it to print `CAPTURE_PLAN_EDIT_VIM` instead.

## Change
In `/tmp/CAPTURE_ROOT/workspace/hello.py`, replace the single line:

```python
print("CAPTURE_PLAN_REVISED")
```

with:

```python
print("CAPTURE_PLAN_EDIT_VIM")
```

- One-line edit only. No other files, no dependencies, no subagents.
- Resubmitted for review; `hello.py` is unchanged until you approve.

## Verification
Run `python3 hello.py` in the workspace. It should print exactly `CAPTURE_PLAN_EDIT_VIM` and exit with status 0.
