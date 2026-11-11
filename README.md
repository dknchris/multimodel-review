# multimodel-review

A Claude Code skill that reviews the current branch with several models. Codex reviewers and fresh Claude reviewers read the same commit, and your Claude Code session is the only fixer. A run goes through Code, Cleanup, Docs and Final phases, with round caps, and ends with a report.

## Install

```bash
git clone https://github.com/dknchris/multimodel-review ~/.claude/skills/multimodel-review
cp ~/.claude/skills/multimodel-review/agents/multimodel-reviewer.md ~/.claude/agents/
```

It needs Python 3, git and the Codex CLI.

## Use

- `/multimodel-review` starts the default panel: `terra:max:fast opus:xhigh code:20 spare:2`.
- `/multimodel-review astra:high fable sol:fast code:35` starts your own panel. Each reviewer is `name[:effort][:fast]`, and effort is low, medium, high, xhigh or max.
- Reviewers: Codex terra, astra, sol and luna, and Claude opus, fable, sonnet and haiku. Fast mode is for the Codex models only.
- `code:<20-50>` caps the Code phase (default 20), and `spare:<2-6>` adds rounds for re-checks after a device test (default 2). Docs and Final get 2 rounds each. Each phase ends at its first clean round.
- `/multimodel-review pick` opens a picker.
- Code sweeps the full diff again after each fix, until a sweep finds no new bug on code an earlier sweep reviewed (at most 4 sweeps). Then delta rounds check the fixes. Each finding is dated with git blame.
- Final checks the code written since the last full sweep. A Final finding on code a sweep already reviewed is tagged late: Late P0s are fixed, and late P1s are listed in the report.
- A full-scope round whose diff is over 60 KB is split by folder into slices of at most 40 KB. Each slice gets its own pass, all in parallel, plus a cross-file pass.

## Test

```bash
python3 tests/test_mmr.py
```

The tests use a fake Codex, so they cost no model usage.

## License

MIT. See [LICENSE](LICENSE).
