---
name: multimodel-review
description: "Run the multi-model review loop on the current branch: Codex reviewers (Terra by default; Astra, Sol and Luna optional) and Claude reviewers (Opus by default; Fable, Sonnet and Haiku optional), with this session as the only fixer, through Code, Cleanup, Docs and Final phases. With no options it starts with terra:max opus:xhigh code:20 spare:2. Typed options change the reviewers, each one's effort and Codex fast mode, and the review length, and `pick` opens a picker for them. Runs only when the user invokes /multimodel-review, because one run can use many max-effort reviewer runs."
disable-model-invocation: true
argument-hint: "[pick | terra:max opus:xhigh code:20 spare:2]"
---

# Multi-model review (Codex + Claude)

Independent reviewers read the same prompt at the same commit. This session is the only fixer. Run it from a session on Opus 5.5 at high effort.

**Options:** $ARGUMENTS

## Settings

- Round caps: Code 20 to 50 (default 20; the picker's Standard, Long and Max are 20, 35 and 50), Docs 2, Final 2, plus 2 to 6 spare rounds for evidence rounds (default 2). Hard stop: Their sum, 26 by default. Each phase ends at its first clean round, so the caps are ceilings. Up to 4 Code rounds are full sweeps, and they count toward the Code cap. Older runs keep their own caps and limit.
- Reviewers: Terra (gpt-6.1-terra) and Opus (Opus 5.5) by default. Optional: Fable (Fable 5.2), Astra (gpt-6-astra), Sol (gpt-6.1-sol), Sonnet (Sonnet 5.5), Haiku (Haiku 5.5) and Luna (gpt-6-luna). Runs started before this change call their Opus reviewer `claude`.
- Effort is set for each reviewer, per run: low, medium, high, xhigh or max. Defaults: Codex max, Claude models xhigh.
- Default panel: `terra:max opus:xhigh code:20 spare:2`.
- Codex fast mode (priority tier) is set for each Codex reviewer, per run, and is off unless `:fast` is given, because it draws more Codex usage. It never applies to Claude models.
- Each Codex run is stopped after 10 minutes of silence or 60 minutes in total. Crashes and empty answers get 1 rerun. An answer is never rerun because of its format.

The helper below enforces these limits and keeps all state in `~/.claude/multimodel-review/`, outside every repo, because a long run gets compacted and the report can't be rebuilt from memory. Write its path out in full each time, and add `--repo <path>` before the subcommand if the shell isn't inside the repo:

```bash
python3 ~/.claude/skills/multimodel-review/scripts/mmr.py status
```

Below, `mmr <command>` is short for that full call.

## Start or resume

1. Run `mmr status`. If an unfinished run exists, continue from its `Next:` line, with the reviewers it lists. If it names a hand-off brief, read it: It holds the last session's notes. If this session did other work before /multimodel-review and no round is open, do the Hand-off first. If it says "Old location", run `mmr migrate` once every reviewer has finished, then use only the new path.
2. Choose options, before any other step, so the owner sees them right away:
   - If the Options line above is only `pick` (or `picker`): Show the picker (next two bullets).
   - Otherwise, whether it's empty or not: Run `mmr options "<options>"` and show no picker. Empty means the defaults, `terra:max opus:xhigh code:20 spare:2`. If it fails, show the owner its one-line error and stop. Each reviewer is `name[:effort][:fast]`: Effort defaults to max for Codex and xhigh for Claude models, and a Codex model runs at standard speed unless `:fast` is given. `code:<20-50>` and `spare:<2-6>` are optional (defaults 20 and 2).
   - Picker, if `show_widget` is available: Call `read_me` once with modules `["interactive"]`. Then run `mmr picker` and pass everything after its `Widget code:` line, unchanged, as `widget_code`, with title `multimodel_review_options`. End the turn and wait. Its button sends "Start the review with <options>.", in the same typed form, for example "terra:max opus:xhigh code:20 spare:2". Run `mmr options "<options>"` on them. If the owner's message says the picker didn't load, run `mmr picker --offline` and show that instead.
   - Picker in a terminal (no `show_widget`): One AskUserQuestion call with four questions: Reviewers (multi-select: Terra, Opus, Fable, Astra; Sol, Sonnet, Haiku and Luna through Other), Codex models in fast mode (multi-select: None first as the default, then Terra, Astra, Sol; Luna through Other), effort (Codex max and Claude xhigh, everything high, everything medium), and review length (Standard `code:20`, Long `code:35`, Max `code:50`). Ignore a fast pick that isn't a chosen reviewer. Write the answers in the typed form and run `mmr options "<options>"` on them.
3. Write a 3-5 line intent to a scratchpad file with the Write tool: what the change should do and for whom. Take it from the request, this conversation, PR text, commits or the uncommitted diff. Reviewers can't judge "wrong behavior" without it. If it's unclear, ask the owner one question with buttons.
4. Read `git status --short`. Everything unignored gets committed into the snapshot, so pick out anything unrelated to this change (a stray folder, notes) and pass it as `--leave-out <path>`. If you can't tell, ask with buttons.
5. Run `mmr init --intent-file <file> --trailer "<attribution line>" --spec "<options>"`, with the options exactly as the `Init:` line of `mmr options` printed them, adding `--base <base>` only if the owner named one and `--leave-out <path>` as needed. The base defaults to the remote's default branch, and the review starts from the merge-base. Invoking this skill is the owner's OK for what init does:
   - On the base branch or a detached HEAD, it creates `multimodel-review/<time>` first, so WIP commits never land on the base.
   - Uncommitted work becomes one WIP snapshot commit, so all reviewers read the same commit. Nothing is pushed.
   - Exit code 5 means some files look like secrets, are over 20 MB, or are embedded repositories. Nothing has changed yet. Ask the owner with buttons: Leave them out (`--leave-out`) or include them (`--allow-flagged`).
   - "Nothing to review" means no commits and no uncommitted work. Ask the owner which commits to review, then pass `--base <commit>`.
6. Tell the owner in one line if init created a branch or a snapshot.
7. Unless this review is this session's first task, do the Hand-off now, before round 1.

## One round

1. Run `mmr next --phase <code|docs|final|evidence>`. It builds the prompt file and prints its path, the run's settings and one `Claude Agent` line per Claude reviewer, with its model, effort and answer file. Exit code 3 means a cap was hit: Go to Done.
   - Split rounds: A full-scope diff over 60 KB is split by folder into slices of at most 40 KB, plus a cross-file pass that gets every file's hunk headers. A slice pass reports only bugs that its own slice can trigger, and leaves doubts that depend on another slice to the cross-file pass; runs started before this change use the older, looser slice prompt. Then `next` prints a `Split:` line and one prompt per part. Codex reviewers review every slice and the cross-file pass. Each Claude reviewer reviews the whole diff once, in a `whole` part, so `next` prints one `Claude Agent <name> whole` line per Claude reviewer. All passes run in parallel. When the panel is only Codex or only Claude, every reviewer reviews every slice and the cross-file pass. `MMR_CLAUDE_SLICES=1` puts Claude reviewers back on every slice. Runs started before slices existed never split.
   - Sweeps 2 on: From full Code sweep 2 on, only Codex reviewers review, when the panel has one. `next` prints `<name>: No pass this round` for each Claude reviewer instead of a `Claude Agent` line, so start no Agent for it. Delta, Docs and Final rounds have every reviewer. Runs started before this change keep every reviewer.
2. Start every reviewer `mmr status` lists, in one message:
   - Each Codex reviewer (terra, astra, sol, luna): Bash with `run_in_background`: `mmr codex --reviewer <name>`. In a split round, this one call runs all of that reviewer's slices at once. The helper runs Codex with `-s read-only`, apps, plugins and subagents off, web search on, its own logs, and the time limits. Never use workspace-write or `--dangerously-bypass-approvals-and-sandbox`, the companion script, or the rescue subagent.
   - One Monitor per Codex reviewer: `mmr monitor --reviewer <name>`, with `timeout_ms: 1800000`. In a split round it watches every slice and prints one line per slice event. It checks every 60 seconds and decides "finished" from the PID exiting, never from output text.
   - Each Claude reviewer (opus, fable, sonnet, haiku; `claude` in older runs): A fresh Agent with `subagent_type: "multimodel-reviewer"`, `model` and `effort` from its `Claude Agent` line, and the prompt "Review round file: <prompt path>". In a split round, start one Agent per `Claude Agent` line, each with its own part's prompt. That agent has read and web tools, but no shell or edit tools. If the type isn't available, use `Plan` with the same model and effort and tell it to use only Read, Grep and web search.
3. Handle Monitor events:
   - `FINISHED`: Nothing to do.
   - `FAILED`: Rerun as in step 5, and tell the owner in one line only if the rerun also fails.
   - `QUIET`: Not a failure, since high effort can think silently for minutes. Take no action; the helper stops a reviewer at 10 silent minutes.
   - `RE-ARM`: Start that monitor again, with the `--slice` the line names.
4. When a Claude Agent returns, Write its final message unchanged to its answer file from step 1, then run `mmr claude-result --reviewer <name> --seconds <duration_ms / 1000>`. In a split round, add `--slice <part>` when the reviewer has more than one pass; a single `whole` pass needs none.
5. Run `mmr barrier`. Edit nothing until it passes. If a result is empty or missing, rerun only that reviewer, at the same commit; the round doesn't count again. In a split round, rerun only that reviewer's failed slice: `mmr codex --reviewer <name> --slice <slice>` with its own `mmr monitor --reviewer <name> --slice <slice>`. For a Claude reviewer, spawn a fresh Agent and repeat step 4. The helper refuses a third attempt and any rerun after a stall or timeout: Then run `mmr stop --reason "<why>"` and go to Done. Exception: A run from before the format rules were dropped may show `malformed` on a real answer. Run `mmr reread --reviewer <who>`, which reads the saved answer again with no new reviewer run.
6. Triage (see Fixer rules), and write `triage.tsv` in the round folder with the Write tool. In a split round, the barrier writes `answers.md` in the round folder: Every part's answers, the `whole` pass included, with findings at nearby lines listed as likely duplicates. Triage from it, one row per merged finding across slices; `close` also merges rows with the same `loc` and summary. In Final, run `mmr ages` after writing `triage.tsv` and before fixing: It marks late rows and says which to fix (late P0) and which to list as `open` (late P1).
7. Fix accepted findings, run the affected tests, and make one WIP commit. A failing test blocks the round, so a bad fix can't reach the next delta.
8. Run `mmr close --tests "<exact test command>"`, or `--tests "none: docs only"`. With nothing accepted, omit `--tests`. Follow the `Next:` line it prints. If it says `Hand-off due` (every 8 rounds), do the Hand-off first.

`triage.tsv` is tab-separated, one row per merged finding, and can be omitted only if every reviewer answered "NO BLOCKING ISSUES.":

```
reviewers	sev	loc	status	summary	note
terra,opus	P1	src/a.py:42	accepted	Retry drops the last item	src/a.py: Keep the last item on retry
terra	P1	src/b.py:9	rejected	Null token crashes	Token is non-null: src/auth.py:30 returns early
```

- `loc` is `path:line` from the repo root. `close` dates each row with git blame on the lines around it: `before-branch`, `seen-in-full` (code a full Code sweep already reviewed) or `newer`. `mmr ages` shows these dates and changes nothing.
- `status` is `accepted`, `rejected`, or `open`. `open` is only for the last allowed round, where a fix would go unreviewed, and for a late P1 in Final.
- `note` is what changed (accepted), the code-cited reason (rejected), or why it's left (open). Accepted and rejected notes feed the next prompt's fix list and ledger.
- Reject out-of-phase findings as "Out of phase".
- Reviewers can answer in any format, so read each answer yourself. Finding counts are a best guess. An answer with no P0/P1/P2 tags counts as one finding: Add a row for what it raises, or one `rejected` row if it raises nothing.

WIP commits: Message `WIP multimodel-review round <N>`, written to a file and passed with `git commit -F`, plus the session's attribution lines. Never push.

## Phases

1. **Code:** Round 1 is a full sweep of the diff vs the base. Sweep 2 follows a non-clean sweep 1. After that, another full sweep runs only if the last one accepted 2 or more P0/P1 on old code: code an earlier sweep reviewed (dated `seen-in-full`) or older than the branch (`before-branch`). There are at most 4 sweeps. From sweep 2 on, only Codex reviewers sweep. Then delta rounds check the fixes: Only the delta plus the fix list. `close` prints which comes next and why. Reviewers may open any file for context. In a delta round, outside the delta they report only P0, unless the delta causes the failure: Then the finding anchors to the delta line at its real severity. Runs started before this rule sweep again on 1 `seen-in-full` finding, with every reviewer, and runs started before sweeps keep rounds 1-2 full and then delta rounds.
2. **Cleanup (fixer only):** Runs before Docs so that any text written here gets a P2 review.
   - Docs: Update only docs made stale by this change. Trim, don't expand.
   - Tests: Delete only debugging or scaffolding tests and tests for removed code. Keep regression tests.
   - Run the full test suite. If it fails, fix it; if you can't with a small fix, stop and report.
   - Commit, then Write `cleanup.md` in the run folder: Each deletion with its reason, and the exact test command and result. `next --phase docs` refuses without it.
3. **Docs:** One full-diff round at P2, fix, then one delta round. Then the phase ends; anything left is recorded as open.
4. **Final:** A check, not a fresh search. Round 1 reviews the code written since the last full Code sweep, plus its effects on callers, with all reviewers, P0/P1 only. If nothing changed since that sweep, `next` marks Final converged. A Final finding on code a full sweep already reviewed is tagged `late`, and the report counts them. Late P0s are always fixed. Late P1s get status `open` with a note: The report lists them, and they neither block Final nor loop. If another finding is accepted, fix it and run one delta round. If that still fails, mark it open; the helper stops the run. Runs started before this change keep a full Final round 1.

## Convergence

- A round is clean when the fixer accepts no finding at the phase's severity. Findings rejected with a code-cited reason don't block.
- A phase converges at its first clean round.
- New evidence after Code converges (device or emulator run): Update the affected code or text once, commit, then run `mmr next --phase evidence --note "<what the run showed and what changed>"`, one delta round at P0/P1/P2. Code reopens only if a P0/P1 is accepted, and `close` says so.
- At a phase cap or the hard stop: Stop and report what's left.

## Hand-off

This session's context is most of a review's cost, because every step reads it again. The run's state is on disk, so a fresh session can take over between rounds. Hand off right after init unless the review is this session's first task, and when `close` or `status` says `Hand-off due`.

1. Hand off only between rounds: An open round's reviewers report to the session that started them, so `mmr handoff` refuses one.
2. If the owner decided something in this session that the run folder doesn't hold, such as a path to leave alone or a pending device test, Write it to a scratchpad notes file.
3. Run `mmr handoff`, adding `--notes <file>` if you wrote one. It saves a brief as `handoff.md` in the run folder, prints it, and counts the hand-off, so the next one is due 8 rounds later even if the owner stays here.
4. Offer the fresh session:
   - With a start_session tool (desktop app): Offer a session with title "Continue multimodel-review", fresh context, on Opus 5.5 at high effort, in this same checkout (no new worktree: The run's commits are on this branch), with the brief, unchanged, as its prompt. The owner picks the new session or this one.
   - In a terminal: Ask with buttons: "Fresh session (Recommended)" or "Continue here". For a fresh session, tell the owner to type `/clear`, then `/multimodel-review`; the run resumes from disk.
5. If the owner takes the fresh session, stop: This session does nothing more on the run, even when that session's notes arrive here. Otherwise continue from the `Next:` line.

Runs started before this change print no `Hand-off due`, but `mmr handoff` works on them too.

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
2. Run `mmr report`. It prints rounds per phase, the full sweeps, the late count, the reviewer table, open items, listed late P1s, disputed findings, test commands, and the squash command, and saves `report.md`.
3. Report to the owner, short and answer first: Rounds per phase, open items, late count, exact validation commands and results.
4. Show a simple visual per reviewer, inline: Findings raised, accepted, rejected, unique, P0-P1 vs P2, clean rounds, average minutes per round. Name your winner in one line. The default rule is most accepted unique P0/P1, and you should say if you overrule it, since you're judging your own model too.
5. Leave the WIP commits in place, because evidence rounds need the delta range. Ask with a button whether to squash now, and run the squash from the report only on a yes.

## Changing this skill

This folder is the public repo github.com/dknchris/multimodel-review. Commit each change, and push only when the owner says so. The picker's code is `assets/picker.js`: `mmr picker` loads it from the CDN only once its latest commit is pushed, and sends it inline, which is slower, until then. Run `python3 tests/test_mmr.py` after every change.
