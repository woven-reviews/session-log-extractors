# Plan: Tiny Python greeting program

## Problem
Create a tiny Python greeting program in the existing nested directory
`nested path 雪 segment ×15 ` (note the trailing space in the directory name) in the workspace.

## Current state
- That directory already contains `greeting.py` with a single line: `print('CAPTURE_BEFORE')`.
- No other Python code or tests in the workspace.

## Approach
- Edit the existing `greeting.py` (do not create a new file elsewhere) to a minimal program:
  - A comment retaining the context marker: `# CAPTURE_CONTEXT_SNOW_47`.
  - `main()` prints `Hello, <name>!`, with the name taken from `sys.argv[1]` or defaulting to `café 雪` (default output: `Hello, café 雪!`).
  - `if __name__ == "__main__": main()`
- Replace the `CAPTURE_BEFORE` placeholder line (assumption; see notes).
- Verify by running it with `python3` with no argument and with a Unicode argument (e.g. `雪`), quoting the path because of spaces, Unicode and the trailing space.

## Todos
1. update-greeting: Edit `greeting.py` in the nested path.
2. verify-greeting: Run it with and without an argument.

## Notes / assumptions
- "Context marker" is interpreted as `CAPTURE_CONTEXT_SNOW_47` from earlier in this session.
- The existing `CAPTURE_BEFORE` content will be overwritten; tell me if you want to keep it.
- No dependencies, no tests (tiny script).

---

# Follow-up: Append a comment to greeting.py

## Approach
- Use the native edit tool to append one line at the end of `greeting.py` (nested path): `# CAPTURE_FOLLOWUP: end of greeting`
- View the file to confirm; optionally run it once with `python3`.

## Todos
3. append-comment: Append the comment.
4. verify-comment: View the file and run it once.

## Notes
- The comment text is a placeholder; edit it here before approval.
- The file currently starts with `# CAPTURE_AFTER` and ends with `main()`.
