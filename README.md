# multimodel-review

A Claude Code skill that reviews the current branch with several models. Codex reviewers and fresh Claude reviewers read the same commit, and your Claude Code session is the only fixer. A run goes through Code, Cleanup, Docs and Final phases, with round caps, and ends with a report.

## Install

```bash
git clone https://github.com/dknchris/multimodel-review ~/.claude/skills/multimodel-review
cp ~/.claude/skills/multimodel-review/agents/multimodel-reviewer.md ~/.claude/agents/
```

It needs Python 3, git and the Codex CLI.

## Use

- `/multimodel-review` starts the default panel: `terra:max:fast opus:xhigh rounds:26`.
- `/multimodel-review astra:high fable sol:fast rounds:12` starts your own panel. Each reviewer is `name[:effort][:fast]`, and effort is low, medium, high, xhigh or max.
- Reviewers: Codex terra, astra, luna and sol, and Claude opus, fable, sonnet and haiku. Fast mode is for the Codex models only.
- `/multimodel-review pick` opens a picker.

## Test

```bash
python3 tests/test_mmr.py
```

The tests use a fake Codex, so they cost no model usage.

## License

MIT. See [LICENSE](LICENSE).
