---
name: multimodel-review
description: "Run the multi-model review loop on the current branch: Codex reviewers (Terra by default; Astra, Sol and Luna optional) and Claude reviewers (Opus by default; Fable, Sonnet and Haiku optional), with this session as the only fixer, through Code, Cleanup, Docs and Final phases. With no options it starts with terra:max:fast opus:xhigh code:20 spare:2. Typed options change the reviewers, each one's effort and Codex fast mode, and the review length, and `pick` opens a picker for them. Runs only when the user invokes /multimodel-review, because one run can use many max-effort reviewer runs."
disable-model-invocation: true
argument-hint: "[pick | terra:max:fast opus:xhigh code:20 spare:2]"
---

# Multi-model review (Codex + Claude)

Independent reviewers read the same prompt at the same commit. This session is the only fixer. Run it from a session on Opus 5.5 at high effort.

**Options:** $ARGUMENTS

## Settings

- Round caps: Code 20 to 50 (default 20; the picker's Standard, Long and Max are 20, 35 and 50), Docs 2, Final 2, plus 2 to 6 spare rounds for evidence rounds (default 2). Hard stop: Their sum, 26 by default. Each phase ends at its first clean round, so the caps are ceilings. Up to 4 Code rounds are full sweeps, and they count toward the Code cap. Older runs keep their own caps and limit.
- Reviewers: Terra (gpt-6.1-terra) and Opus (Opus 5.5) by default. Optional: Fable (Fable 5.2), Astra (gpt-6-astra), Sol (gpt-6.1-sol), Sonnet (Sonnet 5.5), Haiku (Haiku 5.5) and Luna (gpt-6-luna). Runs started before this change call their Opus reviewer `claude`.
- Effort is set for each reviewer, per run: low, medium, high, xhigh or max. Defaults: Codex max, Claude models xhigh.
- Default panel: `terra:max:fast opus:xhigh code:20 spare:2`.
- Codex fast mode (priority tier) is set for each Codex reviewer, per run. It never applies to Claude models.
- Each Codex run is stopped after 10 minutes of silence or 60 minutes in total. Crashes and empty answers get 1 rerun. An answer is never rerun because of its format.

The helper below enforces these limits and keeps all state in `~/.claude/multimodel-review/`, outside every repo, because a long run gets compacted and the report can't be rebuilt from memory. Write its path out in full each time, and add `--repo <path>` before the subcommand if the shell isn't inside the repo:

```bash
python3 ~/.claude/skills/multimodel-review/scripts/mmr.py status
```

Below, `mmr <command>` is short for that full call.

## Start or resume

1. Run `mmr status`. If an unfinished run exists, continue from what it shows, with the reviewers it lists. If it says "Old location", run `mmr migrate` once every reviewer has finished, then use only the new path.
2. Choose options, before any other step, so the owner sees them right away:
   - If the Options line above is only `pick` (or `picker`): Show the picker (next two bullets).
   - Otherwise, whether it's empty or not: Run `mmr options "<options>"` and show no picker. Empty means the defaults, `terra:max:fast opus:xhigh code:20 spare:2`. If it fails, show the owner its one-line error and stop. Each reviewer is `name[:effort][:fast]`: Effort defaults to max for Codex and xhigh for Claude models, and a Codex model runs at standard speed unless `:fast` is given. `code:<20-50>` and `spare:<2-6>` are optional (defaults 20 and 2).
   - Picker, if `show_widget` is available: Call `read_me` once with modules `["interactive"]`. Then run `mmr picker` and pass everything after its `Widget code:` line, unchanged, as `widget_code`, with title `multimodel_review_options`. End the turn and wait. Its button sends "Start the review with <options>.", in the same typed form, for example "terra:max:fast opus:xhigh code:20 spare:2". Run `mmr options "<options>"` on them. If the owner's message says the picker didn't load, run `mmr picker --offline` and show that instead.
   - Picker in a terminal (no `show_widget`): One AskUserQuestion call with four questions: Reviewers (multi-select: Terra, Opus, Fable, Astra; Sol, Sonnet, Haiku and Luna through Other), Codex models in fast mode (multi-select: Terra, Astra, Sol, None; Luna through Other), effort (Codex max and Claude xhigh, everything high, everything medium), and review length (Standard `code:20`, Long `code:35`, Max `code:50`). Ignore a fast pick that isn't a chosen reviewer. Write the answers in the typed form and run `mmr options "<options>"` on them.
3. Write a 3-5 line intent to a scratchpad file with the Write tool: what the change should do and for whom. Take it from the request, this conversation, PR text, commits or the uncommitted diff. Reviewers can't judge "wrong behavior" without it. If it's unclear, ask the owner one question with buttons.
4. Read `git status --short`. Everything unignored gets committed into the snapshot, so pick out anything unrelated to this change (a stray folder, notes) and pass it as `--leave-out <path>`. If you can't tell, ask with buttons.
5. Run `mmr init --intent-file <file> --trailer "<attribution line>" --spec "<options>"`, with the options exactly as the `Init:` line of `mmr options` printed them, adding `--base <base>` only if the owner named one and `--leave-out <path>` as needed. The base defaults to the remote's default branch, and the review starts from the merge-base. Invoking this skill is the owner's OK for what init does:
   - On the base branch or a detached HEAD, it creates `multimodel-review/<time>` first, so WIP commits never land on the base.
   - Uncommitted work becomes one WIP snapshot commit, so all reviewers read the same commit. Nothing is pushed.
   - Exit code 5 means some files look like secrets, are over 20 MB, or are embedded repositories. Nothing has changed yet. Ask the owner with buttons: Leave them out (`--leave-out`) or include them (`--allow-flagged`).
   - "Nothing to review" means no commits and no uncommitted work. Ask the owner which commits to review, then pass `--base <commit>`.
6. Tell the owner in one line if init created a branch or a snapshot.

## One round

1. Run `mmr next --phase <code|docs|final|evidence>`. It builds the prompt file and prints its path, the run's settings and one `Claude Agent` line per Claude reviewer, with its model, effort and answer file. Exit code 3 means a cap was hit: Go to Done.
   - Split rounds: A full-scope diff over 60 KB is split by folder into slices of at most 40 KB, plus a cross-file pass that gets every file's hunk headers. Then `next` prints a `Split:` line, one prompt per slice, and one `Claude Agent <name> <slice>` line per Claude reviewer and slice. Every reviewer reviews every slice and the cross-file pass, all in parallel. Runs started before this change never split.
2. Start every reviewer `mmr status` lists, in one message:
   - Each Codex reviewer (terra, astra, sol, luna): Bash with `run_in_background`: `mmr codex --reviewer <name>`. In a split round, this one call runs all of that reviewer's slices at once. The helper runs Codex with `-s read-only`, apps, plugins and subagents off, web search on, its own logs, and the time limits. Never use workspace-write or `--dangerously-bypass-approvals-and-sandbox`, the companion script, or the rescue subagent.
   - One Monitor per Codex reviewer: `mmr monitor --reviewer <name>`, with `timeout_ms: 1800000`. In a split round it watches every slice and prints one line per slice event. It checks every 60 seconds and decides "finished" from the PID exiting, never from output text.
   - Each Claude reviewer (opus, fable, sonnet, haiku; `claude` in older runs): A fresh Agent with `subagent_type: "multimodel-reviewer"`, `model` and `effort` from its `Claude Agent` line, and the prompt "Review round file: <prompt path>". In a split round, start one Agent per `Claude Agent` line, each with its own slice's prompt. That agent has read and web tools, but no shell or edit tools. If the type isn't available, use `Plan` with the same model and effort and tell it to use only Read, Grep and web search.
3. Handle Monitor events:
   - `FINISHED`: Nothing to do.
   - `FAILED`: Rerun as in step 5, and tell the owner in one line only if the rerun also fails.
   - `QUIET`: Not a failure, since high effort can think silently for minutes. Take no action; the helper stops a reviewer at 10 silent minutes.
   - `RE-ARM`: Start that monitor again, with the `--slice` the line names.
4. When a Claude Agent returns, Write its final message unchanged to its answer file from step 1, then run `mmr claude-result --reviewer <name> --seconds <duration_ms / 1000>`, adding `--slice <slice>` in a split round.
5. Run `mmr barrier`. Edit nothing until it passes. If a result is empty or missing, rerun only that reviewer, at the same commit; the round doesn't count again. In a split round, rerun only that reviewer's failed slice: `mmr codex --reviewer <name> --slice <slice>` with its own `mmr monitor --reviewer <name> --slice <slice>`. For a Claude reviewer, spawn a fresh Agent and repeat step 4. The helper refuses a third attempt and any rerun after a stall or timeout: Then run `mmr stop --reason "<why>"` and go to Done. Exception: A run from before the format rules were dropped may show `malformed` on a real answer. Run `mmr reread --reviewer <who>`, which reads the saved answer again with no new reviewer run.
6. Triage (see Fixer rules), and write `triage.tsv` in the round folder with the Write tool. In a split round, the barrier writes `answers.md` in the round folder: Every slice's answers, with findings at nearby lines listed as likely duplicates. Triage from it, one row per merged finding across slices; `close` also merges rows with the same `loc` and summary.
7. Fix accepted findings, run the affected tests, and make one WIP commit. A failing test blocks the round, so a bad fix can't reach the next delta.
8. Run `mmr close --tests "<exact test command>"`, or `--tests "none: docs only"`. With nothing accepted, omit `--tests`. Follow the `Next:` line it prints.

`triage.tsv` is tab-separated, one row per merged finding, and can be omitted only if every reviewer answered "NO BLOCKING ISSUES.":

```
reviewers	sev	loc	status	summary	note
terra,opus	P1	src/a.py:42	accepted	Retry drops the last item	src/a.py: Keep the last item on retry
terra	P1	src/b.py:9	rejected	Null token crashes	Token is non-null: src/auth.py:30 returns early
```

- `loc` is `path:line` from the repo root. `close` dates each row with git blame on the lines around it: `before-branch`, `seen-in-full` (code a full Code sweep already reviewed) or `newer`. `mmr ages` shows these dates and changes nothing.
- `status` is `accepted`, `rejected`, or `open`. `open` is only for the last allowed round, where a fix would go unreviewed.
- `note` is what changed (accepted), the code-cited reason (rejected), or why it's left (open). Accepted and rejected notes feed the next prompt's fix list and ledger.
- Reject out-of-phase findings as "Out of phase".
- Reviewers can answer in any format, so read each answer yourself. Finding counts are a best guess. An answer with no P0/P1/P2 tags counts as one finding: Add a row for what it raises, or one `rejected` row if it raises nothing.

WIP commits: Message `WIP multimodel-review round <N>`, written to a file and passed with `git commit -F`, plus the session's attribution lines. Never push.

## Phases

1. **Code:** Round 1 is a full sweep of the diff vs the base. After each fix, another full sweep runs, until a sweep finds no accepted P0/P1 on code an earlier sweep reviewed (dated `seen-in-full`), with at most 4 sweeps. Then delta rounds check the fixes: Only the delta plus the fix list. `close` prints which comes next and why. Reviewers may open any file for context. In a delta round, outside the delta they report only P0, unless the delta causes the failure: Then the finding anchors to the delta line at its real severity. Runs started before this change keep rounds 1-2 full and then delta rounds.
2. **Cleanup (fixer only):** Runs before Docs so that any text written here gets a P2 review.
   - Docs: Update only docs made stale by this change. Trim, don't expand.
   - Tests: Delete only debugging or scaffolding tests and tests for removed code. Keep regression tests.
   - Run the full test suite. If it fails, fix it; if you can't with a small fix, stop and report.
   - Commit, then Write `cleanup.md` in the run folder: Each deletion with its reason, and the exact test command and result. `next --phase docs` refuses without it.
3. **Docs:** One full-diff round at P2, fix, then one delta round. Then the phase ends; anything left is recorded as open.
4. **Final:** One full-diff round with all reviewers, P0/P1 only. If a finding is accepted, fix it and run one delta round. If that still fails, mark it open; the helper stops the run.

## Convergence

- A round is clean when the fixer accepts no finding at the phase's severity. Findings rejected with a code-cited reason don't block.
- A phase converges at its first clean round.
- New evidence after Code converges (device or emulator run): Update the affected code or text once, commit, then run `mmr next --phase evidence --note "<what the run showed and what changed>"`, one delta round at P0/P1/P2. Code reopens only if a P0/P1 is accepted, and `close` says so.
- At a phase cap or the hard stop: Stop and report what's left.

## Fixer rules

- Merge and dedupe, keeping every reviewer who raised a finding, so the stats stay honest. Verify each finding against the code; reject unverifiable ones with a one-line reason.
- Judge findings on the code alone, whoever raised it. You are a Claude model too, so hold Claude reviewers' findings to the same bar. Rejecting a finding raised by 2+ reviewers, or any P0, needs a specific code citation; the report lists these as disputed for the owner.
- Smallest fix. No refactors, no new features. Add a regression test only for real P0/P1 bugs.
- Write less prose: Prefer deleting or narrowing a wrong sentence over adding one. New comments are at most one line. Add no new doc sections during review.
- If a hook refuses a path, stop and report it to the owner. Never work around it, with a shell copy or any other way.
- Keep shell commands dcg-safe: Write files with the Write tool, and put no variables, globs or redirects to variable paths in commands.

## Done

Run this on convergence and on any stop.

1. Run `mmr archive` to archive every Codex chat, reruns included.
2. Run `mmr report`. It prints rounds per phase, the reviewer table, open items, disputed findings, test commands, and the squash command, and saves `report.md`.
3. Report to the owner, short and answer first: Rounds per phase, open items, exact validation commands and results.
4. Show a simple visual per reviewer, inline: Findings raised, accepted, rejected, unique, P0-P1 vs P2, clean rounds, average minutes per round. Name your winner in one line. The default rule is most accepted unique P0/P1, and you should say if you overrule it, since you're judging your own model too.
5. Leave the WIP commits in place, because evidence rounds need the delta range. Ask with a button whether to squash now, and run the squash from the report only on a yes.

## Changing this skill

This folder is the public repo github.com/dknchris/multimodel-review. Commit each change, and push only when the owner says so. The picker's code is `assets/picker.js`: `mmr picker` loads it from the CDN only once its latest commit is pushed, and sends it inline, which is slower, until then. Run `python3 tests/test_mmr.py` after every change.
