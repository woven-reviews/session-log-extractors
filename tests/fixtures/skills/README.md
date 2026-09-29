# Vendored skill fixtures

`ponytail/SKILL.md` is a verbatim, frozen copy of `skills/ponytail/SKILL.md`
from the public, MIT-licensed [ponytail](https://github.com/DietrichGebert/ponytail)
Claude Code plugin (installed locally as a marketplace plugin; the source
repo carries its own `LICENSE` file). Copied here — rather than read from the
installed plugin's path on a given machine — so `test_skill_metadata.py` has
a real, non-synthetic `SKILL.md` to parse that (a) exists in every checkout,
independent of what plugins a given machine has installed, and (b) won't
silently change out from under the tests if the plugin is later updated.

Used to exercise `skill_metadata.py`'s YAML-folded (`description: >`)
frontmatter handling, `##`-section extraction, and the `argument-hint`
metadata field against real-world formatting, rather than a hand-written
specimen.

`ponytail-command-root/.claude/commands/ponytail-review.md` is a verbatim
copy of the same plugin's OpenCode command variant,
`.opencode/command/ponytail-review.md` -- a real single-paragraph,
heading-less command body (no real command in this repo's own
`.claude/commands/` happens to be that short). Nested under its own
`.claude/commands/` so `load_command_details` can resolve it directly by
pointing `project_root` at `ponytail-command-root/`.

