#!/usr/bin/env python3
"""End-to-end test of mmr.py with a fake Codex. Costs no model usage."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MMR = str(Path(__file__).resolve().parent.parent / "scripts" / "mmr.py")
HEADER = "reviewers\tsev\tloc\tstatus\tsummary\tnote\n"
GIT_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.t",
          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.t"}
FAKE = """#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
if args[0] == "archive":
    print("Archived session", args[1]); sys.exit(0)
out = args[args.index("-o") + 1]
open(os.environ["FAKE_MODE_FILE"] + ".args", "w").write(" ".join(args))
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "t-" + str(os.getpid())}), flush=True)
mode_file = os.environ["FAKE_MODE_FILE"]
modes = open(mode_file).read().split()
mode = modes.pop(0) if modes else "clean"
open(mode_file, "w").write(" ".join(modes))
if mode == "hang":
    time.sleep(600)
if mode == "crash":
    sys.exit(7)
answer = {"clean": "NO BLOCKING ISSUES.",
          "finding": "[P1] a.txt:1 | Input x -> wrong y | Fix the thing.",
          "late-crash": "[P1] a.txt:1 | x -> y | Fix.",
          "bad": ""}[mode]
open(out, "w").write(answer)
if mode == "late-crash":
    sys.exit(7)
"""
passed = 0


def check(cond, label):
    global passed
    if not cond:
        print(f"FAIL: {label}")
        sys.exit(1)
    passed += 1


def latest_run(repo, env):
    r = subprocess.run([sys.executable, MMR, "--repo", str(repo), "status"], capture_output=True,
                       text=True, env=env, timeout=60)
    return Path(r.stdout.splitlines()[0].removeprefix("Run: "))


def location_tests(tmp, env, intent):
    """Runs live outside every repo, per repo and worktree. Old runs keep working, then move once."""
    runs_home = Path(env["MMR_RUNS_HOME"])

    def g(repo, *args):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.t", *args], cwd=repo,
                           capture_output=True, text=True)
        return r.stdout.strip()

    def mmr(repo, *args):
        r = subprocess.run([sys.executable, MMR, "--repo", str(repo), *args], capture_output=True,
                           text=True, env={**env, **GIT_ID}, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def project(name):
        repo = tmp / name
        repo.mkdir()
        g(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("one\n")
        g(repo, "add", "-A")
        g(repo, "commit", "-qm", "one")
        wt = tmp / f"{name}-wt" / "same-name"
        g(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
        (wt / "a.txt").write_text("two\n")
        g(wt, "commit", "-qam", "two")
        return repo, wt

    # Same worktree name in two projects, plus the main checkout: three separate folders.
    repo1, wt1 = project("loc1")
    repo2, wt2 = project("loc2")
    g(repo1, "checkout", "-qb", "mainwork")
    (repo1 / "a.txt").write_text("three\n")
    g(repo1, "commit", "-qam", "three")
    for repo in (wt1, wt2, repo1):
        check(mmr(repo, "init", "--base", "main", "--intent-file", str(intent))[0] == 0, "init in place")
    runs = [latest_run(r, env) for r in (wt1, wt2, repo1)]
    check(len({r.parent for r in runs}) == 3, "one folder per repo and worktree")
    check(all(r.parent.parent == runs_home for r in runs), "runs are under the runs home")
    check(not any("multimod" in p.name for p in (repo1 / ".git").rglob("*")), "nothing in the git dir")

    # A run in the old place: every command reads it, and init won't start over it.
    old_root = Path(g(wt2, "rev-parse", "--absolute-git-dir")) / "multimodal-review"
    check(mmr(wt2, "next", "--phase", "code")[0] == 0, "open a round")
    new_run = latest_run(wt2, env)
    old_run = old_root / new_run.name
    old_root.mkdir()
    new_run.rename(old_run)
    (new_run.parent / "latest").rename(old_root / "latest")
    prompt = old_run / "rounds" / "01" / "prompt.md"
    prompt.write_text(prompt.read_text().replace(str(new_run), str(old_run)))
    code, out = mmr(wt2, "status")
    check(code == 0 and str(old_run) in out and "Old location" in out, "old run still reads")
    code, out = mmr(wt2, "init", "--base", "main", "--intent-file", str(intent))
    check(code == 1 and "unfinished" in out, "init sees the old unfinished run")
    pid = old_run / "rounds" / "01" / "terra.a1.pid"
    pid.write_text(str(os.getpid()))
    code, out = mmr(wt2, "migrate")
    check(code == 1 and "still running" in out and old_run.exists(), "migrate waits for a live reviewer")
    pid.unlink()
    (old_run / "marker").write_text("x\n")

    code, out = mmr(wt2, "migrate")
    check(code == 0 and str(new_run) in out, "migrate moves the run")
    check(not old_root.joinpath("latest").exists() and not old_run.exists(), "old copy gone")
    check((new_run / "marker").exists(), "files moved")
    moved = (new_run / "rounds" / "01" / "prompt.md").read_text()
    check(str(new_run) in moved and str(old_run) not in moved, "prompt names the new diff path")
    code, out = mmr(wt2, "status")
    check(str(new_run) in out and "Old location" not in out and "Open round: 1" in out, "run continues")
    code, out = mmr(wt2, "migrate")
    check(code == 0 and "Already" in out, "second migrate does nothing")


def parser_tests():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("mmr", MMR)
    mmr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mmr)
    linked = ("[P1] [src/a(b).mjs:55](/abs/src/a(b).mjs:55) | Trigger -> [a doc](https://x.y/z) wrong "
              "| Fix it.\n\n- [P0] [b.md:4-9](/abs/b.md:4) | x -> y | Fix.")
    check(mmr.parse_final(linked) == (True, [("P1", "src/a(b).mjs:55"), ("P0", "b.md:4-9")], []),
          "linked location is stored as plain file:line")
    check(mmr.parse_final("[P1] a.txt:1 | x -> y | Fix.")[1] == [("P1", "a.txt:1")], "plain location still parses")
    for odd in ("**[P1]** `a.txt:1`: Bad thing", "### P1: a.txt:1", "1. P1 - a.txt:1 breaks", "| P1 | a.txt:1 | x |",
                "- [p1] a.txt:1", "Here is what I found:\n\n[P1] a.txt:1 - it breaks\nMore detail here."):
        check(mmr.parse_final(odd) == (True, [("P1", "a.txt:1")], []), f"accepts: {odd[:30]}")
    check(mmr.parse_final("[P1] Retry drops the last item") == (True, [("P1", "")], []), "no location is fine")
    for clean in ("NO BLOCKING ISSUES.", "No blocking issues", "**No issues found.**", "No findings in this delta."):
        check(mmr.parse_final(clean) == (True, [], []), f"clean: {clean}")
    long_text = "No issues in auth.\n" + "But the retry loop looks wrong to me in several ways. " * 8
    for free in ("The retry loop drops the last item.", long_text):
        check(mmr.parse_final(free) == (True, [("P?", "")], []), "free-form answer goes to the fixer to read")
    check(not mmr.parse_final("  \n")[0], "only an empty answer is invalid")


def start_gate_tests(tmp, env, intent):
    """Uncommitted work and the base branch no longer block a run."""
    count = [0]

    def fresh():
        count[0] += 1
        repo = tmp / f"gate{count[0]}"
        repo.mkdir()
        g(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("one\n")
        g(repo, "add", "-A")
        g(repo, "commit", "-qm", "one")
        return repo

    def g(repo, *args):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.t", *args], cwd=repo,
                           capture_output=True, text=True)
        return r.stdout.strip()

    def init(repo, *args):
        r = subprocess.run([sys.executable, MMR, "--repo", str(repo), "init", "--intent-file", str(intent),
                            *args], capture_output=True, text=True, env={**env, **GIT_ID}, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def state(repo):
        return json.loads((latest_run(repo, env) / "run.json").read_text())

    # Dirty on main: branch first, snapshot, main untouched, diff is the uncommitted work.
    repo = fresh()
    main_head = g(repo, "rev-parse", "main")
    (repo / "a.txt").write_text("two\n")
    (repo / "new.txt").write_text("new\n")
    code, out = init(repo, "--trailer", "Co-Authored-By: Test <t@t.t>")
    check(code == 0 and "Created branch" in out and "WIP snapshot" in out, "dirty main starts")
    check(g(repo, "rev-parse", "main") == main_head, "main untouched")
    check(g(repo, "symbolic-ref", "--short", "HEAD").startswith("multimodel-review/"), "on new branch")
    check(g(repo, "status", "--porcelain") == "", "snapshot leaves a clean tree")
    check("Co-Authored-By: Test" in g(repo, "log", "-1", "--format=%B"), "snapshot has the trailer")
    s = state(repo)
    check(s["start_head"] == main_head and s["base_commit"] == main_head, "squash point is before the snapshot")
    check("new.txt" in g(repo, "diff", "--name-only", f"{s['base_commit']}..HEAD"), "untracked work in the diff")

    # Dirty on a feature branch: snapshot, no new branch.
    repo = fresh()
    g(repo, "checkout", "-qb", "feature")
    (repo / "a.txt").write_text("two\n")
    code, out = init(repo, "--base", "main")
    check(code == 0 and "Created branch" not in out and "WIP snapshot" in out, "dirty feature starts")
    check(g(repo, "symbolic-ref", "--short", "HEAD") == "feature", "stays on feature")

    # Unrelated folder left out: stays untracked, and later rounds don't count it as dirty.
    repo = fresh()
    g(repo, "checkout", "-qb", "feature")
    (repo / "a.txt").write_text("two\n")
    (repo / "stray").mkdir()
    (repo / "stray" / "notes.md").write_text("x\n")
    code, out = init(repo, "--base", "main", "--leave-out", "stray/")
    check(code == 0 and "Left out" in out, "leave-out starts")
    check("stray" not in g(repo, "show", "--name-only", "--format=", "HEAD"), "left-out folder not committed")
    check((repo / "stray" / "notes.md").exists(), "left-out folder kept")
    r = subprocess.run([sys.executable, MMR, "--repo", str(repo), "next", "--phase", "code"],
                       capture_output=True, text=True, env=env, timeout=60)
    check(r.returncode == 0, "left-out folder doesn't block a round")

    # Flagged file: exit 5 and nothing changes until the owner decides.
    repo = fresh()
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "a.txt").write_text("two\n")
    code, out = init(repo)
    check(code == 5 and ".env" in out, "flags sensitive file")
    check(g(repo, "symbolic-ref", "--short", "HEAD") == "main", "no branch after flag")
    check(g(repo, "log", "--oneline").count("\n") == 0, "no commit after flag")
    code, out = init(repo, "--leave-out", ".env")
    check(code == 0 and ".env" not in g(repo, "show", "--name-only", "--format=", "HEAD"), "leave out flagged file")

    repo = fresh()
    (repo / ".env.example").write_text("SECRET=\n")
    check(init(repo)[0] == 0, ".env.example isn't flagged")

    repo = fresh()
    (repo / "key.pem").write_text("x\n")
    check(init(repo, "--allow-flagged")[0] == 0, "allow-flagged includes it")

    # Clean main with nothing ahead: nothing created.
    repo = fresh()
    code, out = init(repo)
    check(code == 1 and "Nothing to review" in out, "clean main refuses")
    check(g(repo, "branch", "--list").count("\n") == 0, "no branch created")

    # Clean main with unpushed commits: branches off and reviews them against origin/main.
    repo = fresh()
    pushed = g(repo, "rev-parse", "HEAD")
    g(repo, "update-ref", "refs/remotes/origin/main", pushed)
    g(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    (repo / "a.txt").write_text("two\n")
    g(repo, "commit", "-qam", "two")
    code, out = init(repo)
    check(code == 0 and "Created branch" in out and "WIP snapshot" not in out, "unpushed main starts")
    check(state(repo)["base_commit"] == pushed, "base is origin/main")

    # Detached HEAD with commits ahead of main.
    repo = fresh()
    g(repo, "checkout", "-qb", "feature")
    (repo / "a.txt").write_text("two\n")
    g(repo, "commit", "-qam", "two")
    g(repo, "checkout", "-q", "--detach")
    code, out = init(repo, "--base", "main")
    check(code == 0 and "detached HEAD" in out, "detached HEAD branches first")


def load_module():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("mmr", MMR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_diff(files):
    """A git-style diff: {path: [hunk sizes in lines]}, 40-byte lines."""
    out = []
    for path, hunks in files.items():
        out += [f"diff --git a/{path} b/{path}", "index 1111111..2222222 100644", f"--- a/{path}", f"+++ b/{path}"]
        for i, lines in enumerate(hunks):
            out.append(f"@@ -{i * 100 + 1},{lines} +{i * 100 + 1},{lines} @@ def f{i}():")
            out += [f"+{path[:20]:<20} hunk {i:02d} line {j:04d}"[:39] for j in range(lines)]
    return "\n".join(out) + "\n"


def slice_unit_tests():
    """plan_slices: by folder, at most the limit, nothing lost, big files cut between hunks."""
    mmr = load_module()
    files = {"apps/web/a.ts": [10], "apps/web/b.ts": [10], "apps/cli/c.ts": [12],
             "crates/core/src/plan.rs": [30, 30, 30, 30], "crates/core/src/run.rs": [8],
             "crates/adb/x.rs": [5], "docs/guide.md": [4], "README.md": [2]}
    diff = fake_diff(files)
    limit = 2000
    slices = mmr.plan_slices(diff, limit)
    check(len(slices) > 2 and all(mmr.nbytes("".join(t for _, t in s)) <= limit for s in slices),
          "every slice is at most the limit")
    lines = lambda text: sorted(ln for ln in text.splitlines() if ln.startswith(("+", "@@")) and not ln.startswith("+++"))
    check(lines("".join(t for s in slices for _, t in s)) == lines(diff), "slices keep every hunk line once")
    order = [mmr.slice_files(s) for s in slices]
    flat = [p for files_ in order for p in files_]
    check(set(flat) == set(files), "every file is in a slice")
    plan_pieces = [t for s in slices for p, t in s if p == "crates/core/src/plan.rs"]
    check(len(plan_pieces) > 1 and all(t.startswith("diff --git a/crates/core/src/plan.rs") and "\n@@ " in t
                                       for t in plan_pieces), "a big file is cut between hunks, header kept")
    where = {}
    for i, files_ in enumerate(order):
        for p in files_:
            where.setdefault(p.rsplit("/", 1)[0] if "/" in p else "", set()).add(i)
    check(all(max(v) - min(v) <= len(v) - 1 for v in where.values()), "a folder's files sit in neighbouring slices")
    names = [f"s{i}" for i in range(1, len(slices) + 1)]
    outline = mmr.slice_outline(names, slices)
    check(all(p in outline for p in files) and "@@ -301,30 +301,30 @@ def f3():" in outline
          and "+10 -0" in outline and "+" * 3 not in outline, "the cross outline lists files and hunk headers only")
    check(mmr.plan_slices(fake_diff({"one.txt": [3]}), limit) == [[("one.txt", fake_diff({"one.txt": [3]}))]],
          "a small diff is one slice")
    quoted = 'diff --git "a/my file.txt" "b/my file.txt"\n@@ -1 +1 @@\n+x\n'
    check(mmr.diff_files(quoted)[0][0] == "my file.txt", "quoted paths parse")


def slice_tests(tmp, env, intent):
    """A full-scope round over the size limit runs one pass per slice plus a cross-file pass."""
    repo = tmp / "sliced"
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "src" / "lib").mkdir()
    (repo / "docs").mkdir()
    modes = Path(env["FAKE_MODE_FILE"])
    small = {**env, "MMR_SLICE_OVER": "2000", "MMR_SLICE_MAX": "1500"}

    def g(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.t", *args], cwd=repo,
                              capture_output=True, text=True).stdout.strip()

    def mmr(*args, e=None):
        r = subprocess.run([sys.executable, MMR, "--repo", str(repo), *args], capture_output=True,
                           text=True, env={**(e or env), **GIT_ID}, timeout=90)
        return r.returncode, r.stdout + r.stderr

    paths = ["src/app/one.py", "src/app/two.py", "src/lib/three.py", "docs/guide.md", "a.txt"]
    for p in paths:
        (repo / p).write_text("start\n")
    g("init", "-q", "-b", "main")
    g("add", "-A")
    g("commit", "-qm", "base")
    g("checkout", "-qb", "feature")
    for p in paths:
        (repo / p).write_text("".join(f"{p} changed line {i}\n" for i in range(30)))
    g("commit", "-qam", "change")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), e=small)
    check(code == 0, "split run starts")
    run = latest_run(repo, env)
    check(json.loads((run / "run.json").read_text())["slices"] == {"over": 2000, "max": 1500, "in_slice": True},
          "the run keeps its split sizes and slice prompt")
    # MMR_CLAUDE_SLICES=1: Every reviewer reviews every slice, as before the whole-diff pass.
    code, out = mmr("next", "--phase", "code", e={**env, "MMR_CLAUDE_SLICES": "1"})
    rd = run / "rounds" / "01"
    parts = json.loads((rd / "round.json").read_text()).get("parts", [])
    check(code == 0 and "Split:" in out and len(parts) >= 3 and parts[-1] == "cross", "big full round is split")
    check(all((rd / p / "prompt.md").exists() and (rd / p / "diff.patch").exists() for p in parts),
          "each slice has its own prompt and diff")
    s1 = (rd / "s1" / "prompt.md").read_text()
    check(f"Diff file: {rd / 's1' / 'diff.patch'}" in s1 and "Slice: s1 of" in s1 and "Files in this slice" in s1,
          "a slice prompt names its own diff and files")
    check("Report only P0/P1 bugs that something in this slice can trigger" in s1
          and "belongs to the cross-file pass, so leave it out" in s1 and "Focus on these files" not in s1,
          "a slice reports only what its own slice can trigger")
    cross = (rd / "cross" / "prompt.md").read_text()
    check("Cross-file pass" in cross and str(rd / "diff.patch") in cross
          and "@@" in (rd / "cross" / "diff.patch").read_text()
          and "+src/app" not in (rd / "cross" / "diff.patch").read_text(), "the cross pass gets hunk headers only")
    check("They leave you every doubt that depends on another slice" in cross, "the cross pass takes the doubts")
    check(all(f"Claude Agent opus {p}: model opus" in out for p in parts) and "Codex terra:" in out,
          "next lists a Claude Agent per slice")
    modes.write_text("finding")
    code, out = mmr("codex", "--reviewer", "terra", "--slice", "s1")
    check(code == 0 and "terra s1 attempt 1: ok; 1 finding" in out, "one slice runs alone")
    modes.write_text("")
    code, out = mmr("codex", "--reviewer", "terra")
    check(code == 0 and f"{len(parts) - 1} of {len(parts) - 1} slice passes ok" in out, "the rest run at once")
    code, out = mmr("codex", "--reviewer", "terra")
    check(code == 1 and "--slice" in out, "a second fan-out is refused")
    code, out = mmr("monitor", "--reviewer", "terra")
    check(code == 0 and all(f"FINISHED terra {p}: ok" in out for p in parts), "one monitor covers every slice")
    code, out = mmr("codex", "--reviewer", "terra", "--slice", "s9")
    check(code == 1 and "No slice s9" in out, "unknown slice refused")
    for p in parts:
        answer = "[P1] a.txt:1 | x -> y | Fix." if p == "s1" else "NO BLOCKING ISSUES."
        (rd / p / "opus.answer.md").write_text(answer)
    code, out = mmr("claude-result", "--seconds", "30")
    check(code == 1 and "--slice" in out, "a split round's Claude result names its slice")
    check(mmr("barrier")[0] == 1, "barrier waits for every slice")
    for p in parts:
        check(mmr("claude-result", "--slice", p, "--seconds", "30")[0] == 0, f"opus {p} saved")
    code, out = mmr("barrier")
    check(code == 0 and "terra s1: ok" in out and "opus cross: ok" in out and "answers.md" in out,
          "barrier passes on every slice")
    answers = (rd / "answers.md").read_text()
    check("a.txt:1: terra s1 P1, opus s1 P1" in answers and "## cross" in answers, "answers group likely duplicates")
    code, out = mmr("status")
    check("split into s1" in out and "opus s2: ok" in out, "status lists each slice")
    (rd / "triage.tsv").write_text(HEADER + "terra\tP1\ta.txt:1\trejected\tNot real\tGuarded: a.txt:1\n"
                                   + "opus\tP1\ta.txt:1\trejected\tNot real.\tGuarded: a.txt:1\n")
    check(mmr("close")[0] == 0, "split round closes")
    rows = [ln.split("\t") for ln in (run / "findings.tsv").read_text().splitlines()[1:]]
    check(len(rows) == 1 and rows[0][3] == "terra,opus", "duplicate rows merge into one finding")
    report = mmr("report")[1]
    check("| terra | 1 |" in report and "| opus | 1 |" in report, "report counts findings across slices")

    # By default a split round gives Codex the slices and each Claude reviewer one whole-diff pass.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new", e=small)[0] == 0, "whole-pass run")
    run = latest_run(repo, env)
    code, out = mmr("next", "--phase", "code")
    rd = run / "rounds" / "01"
    meta = json.loads((rd / "round.json").read_text())
    mine = [p for p in meta.get("parts", []) if p != "whole"]
    check(code == 0 and meta.get("parts", [])[-1:] == ["whole"] and mine[-1:] == ["cross"]
          and meta.get("assign") == {"terra": mine, "opus": ["whole"]}, "Codex gets the slices, Claude the whole diff")
    whole = (rd / "whole" / "prompt.md").read_text()
    check((rd / "whole" / "diff.patch").read_text() == (rd / "diff.patch").read_text()
          and "Slice: whole diff, one pass" in whole and f"terra review it in {len(mine) - 1} slices" in whole,
          "the whole pass gets the whole diff")
    check("Claude Agent opus whole: model opus" in out and "Claude Agent opus s1" not in out
          and f"runs all {len(mine)} passes" in out, "next lists one Claude Agent")
    code, out = mmr("codex", "--reviewer", "terra", "--slice", "whole")
    check(code == 1 and "terra has no pass on whole" in out, "Codex can't take the whole pass")
    code, out = mmr("codex", "--reviewer", "terra")
    check(code == 0 and f"{len(mine)} of {len(mine)} slice passes ok" in out, "Codex runs only its own passes")
    code, out = mmr("monitor", "--reviewer", "terra")
    check(code == 0 and "FINISHED terra cross: ok" in out and "terra whole" not in out,
          "the monitor watches only terra's passes")
    (rd / "whole" / "opus.answer.md").write_text("[P1] a.txt:1 | x -> y | Fix.")
    (rd / "s1" / "opus.answer.md").write_text("NO BLOCKING ISSUES.")
    code, out = mmr("claude-result", "--slice", "s1", "--seconds", "30")
    check(code == 1 and "opus has no pass on s1" in out, "Claude can't answer for a slice")
    check(mmr("barrier")[0] == 1, "barrier waits for the whole pass")
    check(mmr("claude-result", "--seconds", "30")[0] == 0, "the whole pass needs no --slice")
    code, out = mmr("barrier")
    check(code == 0 and "opus whole: ok" in out and "opus s1" not in out and "terra whole" not in out,
          "barrier checks only assigned passes")
    before_whole, _, after_whole = (rd / "answers.md").read_text().partition("## whole")
    check("### opus" in after_whole and "### opus" not in before_whole and "### terra" in before_whole,
          "answers list each pass under its part")
    code, out = mmr("status")
    check("opus whole: ok" in out and "opus s1" not in out, "status lists only assigned passes")
    (rd / "triage.tsv").write_text(HEADER + "opus\tP1\ta.txt:1\trejected\tNot real\tGuarded: a.txt:1\n")
    check(mmr("close")[0] == 0, "whole-pass round closes")
    check("| opus | 1 |" in mmr("report")[1], "report counts the whole pass")

    # A panel with only Codex reviewers has no whole pass.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new", "--spec", "terra", e=small)[0] == 0,
          "Codex-only split run")
    run = latest_run(repo, env)
    code, out = mmr("next", "--phase", "code")
    meta = json.loads((run / "rounds" / "01" / "round.json").read_text())
    check(code == 0 and meta.get("parts", [])[-1:] == ["cross"] and "assign" not in meta,
          "no whole pass without a Claude reviewer")

    # From full sweep 2 on, Codex reviewers take every slice and Claude reviewers sit out: No whole pass.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new", e=small)[0] == 0, "split sweep run")
    run = latest_run(repo, env)
    check(mmr("next", "--phase", "code")[0] == 0, "split sweep 1")
    rd = run / "rounds" / "01"
    modes.write_text("finding")
    mmr("codex", "--reviewer", "terra", "--slice", "s1")
    modes.write_text("")
    mmr("codex", "--reviewer", "terra")
    (rd / "whole" / "opus.answer.md").write_text("NO BLOCKING ISSUES.")
    mmr("claude-result", "--seconds", "1")
    (rd / "triage.tsv").write_text(HEADER + "terra\tP1\ta.txt:1\taccepted\tBug\ta.txt: Fix 1\n")
    (repo / "a.txt").write_text("fixed\n" + (repo / "a.txt").read_text())
    g("commit", "-qam", "WIP round 1")
    code, out = mmr("close", "--tests", "make test")
    check(code == 0 and "Full sweep 2" in out, "split sweep 1 closes")
    code, out = mmr("next", "--phase", "code")
    rd = run / "rounds" / "02"
    meta = json.loads((rd / "round.json").read_text())
    check(code == 0 and "whole" not in meta.get("parts", []) and meta.get("assign") == {"opus": []}
          and "terra review every slice and the cross-file pass, in parallel." in out
          and "opus: No pass this round" in out, "split sweep 2 gives Codex every slice and opus no pass")
    code, out = mmr("codex", "--reviewer", "terra")
    n_parts = len(meta["parts"])
    check(code == 0 and f"{n_parts} of {n_parts} slice passes ok" in out, "terra runs every part of sweep 2")
    code, out = mmr("barrier")
    check(code == 0 and "opus" not in out, "split sweep 2's barrier needs only terra")

    # A failed slice: the monitor reports it and the rest, and only that slice reruns.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new", e=small)[0] == 0, "failing split run")
    check(mmr("next", "--phase", "code")[0] == 0, "failing split round")
    modes.write_text("crash")
    check(mmr("codex", "--reviewer", "terra", "--slice", "s1")[0] == 1, "s1 crashes")
    modes.write_text("")
    check(mmr("codex", "--reviewer", "terra")[0] == 0, "the other slices still run")
    code, out = mmr("monitor", "--reviewer", "terra")
    check(code == 1 and "FAILED terra s1: crash" in out and "FINISHED terra s2: ok" in out,
          "the monitor reports the failed slice and the finished ones")
    code, out = mmr("barrier")
    check(code == 1 and "terra s1: crash" in out, "barrier names the failed slice")
    code, out = mmr("codex", "--reviewer", "terra", "--slice", "s1")
    check(code == 0 and "terra s1 attempt 2: ok" in out, "only the failed slice reruns")

    # A run saved before slices never splits.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new", e=small)[0] == 0, "second split run")
    run = latest_run(repo, env)
    state = json.loads((run / "run.json").read_text())
    state.pop("slices")
    (run / "run.json").write_text(json.dumps(state))
    code, out = mmr("next", "--phase", "code", e=small)
    check(code == 0 and "Split" not in out and "Prompt:" in out, "older runs never split")

    # A run saved before in_slice keeps the older slice prompt.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new", e=small)[0] == 0, "older slice run")
    run = latest_run(repo, env)
    state = json.loads((run / "run.json").read_text())
    state["slices"].pop("in_slice")
    (run / "run.json").write_text(json.dumps(state))
    check(mmr("next", "--phase", "code")[0] == 0, "older slice round")
    rd = run / "rounds" / "01"
    s1, cross = (rd / "s1" / "prompt.md").read_text(), (rd / "cross" / "prompt.md").read_text()
    check("Focus on these files, and report a problem elsewhere only if this slice's code causes it" in s1
          and "can trigger" not in s1 and "every doubt" not in cross, "an older run keeps the older slice prompt")

    # A split Docs round asks each slice for false text in its own files.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new", e=small)[0] == 0, "split docs run")
    run = latest_run(repo, env)
    state = json.loads((run / "run.json").read_text())
    state["phases"]["code"] = "converged"
    (run / "run.json").write_text(json.dumps(state))
    (run / "cleanup.md").write_text("Nothing.\n")
    check(mmr("next", "--phase", "docs")[0] == 0, "split docs round")
    s1 = (run / "rounds" / "01" / "s1" / "prompt.md").read_text()
    check("Report only P2 problems in the text of these files." in s1 and "can trigger" not in s1,
          "a docs slice asks for false text in its own files")


def sweep_tests(tmp, env, intent):
    """Code sweeps the full diff again while sweeps find accepted bugs on code an earlier sweep reviewed.
    git blame dates each finding: before-branch, seen-in-full, newer or unknown."""
    repo = tmp / "sweeps"
    (repo / "src" / "deep").mkdir(parents=True)
    f = repo / "src" / "deep" / "f.txt"
    modes = Path(env["FAKE_MODE_FILE"])

    def g(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.t", *args], cwd=repo,
                              capture_output=True, text=True).stdout.strip()

    def mmr(*args):
        r = subprocess.run([sys.executable, MMR, "--repo", str(repo), *args], capture_output=True,
                           text=True, env={**env, **GIT_ID}, timeout=90)
        return r.returncode, r.stdout + r.stderr

    def edit(changes, msg):
        lines = f.read_text().splitlines()
        for i, text in changes.items():
            lines[i - 1] = text
        f.write_text("\n".join(lines) + "\n")
        g("commit", "-qam", msg)

    def review(n, rows):
        modes.write_text("finding")
        mmr("codex", "--reviewer", "terra")
        rd = latest_run(repo, env) / "rounds" / f"{n:02d}"
        (rd / "opus.answer.md").write_text("NO BLOCKING ISSUES.")
        mmr("claude-result", "--seconds", "1")
        (rd / "triage.tsv").write_text(HEADER + "".join(f"terra\t{r}\n" for r in rows))

    f.write_text("".join(f"base {i}\n" for i in range(1, 11)))
    g("init", "-q", "-b", "main")
    g("add", "-A")
    g("commit", "-qm", "base")
    g("checkout", "-qb", "feature")
    edit({i: f"c1 {i}" for i in range(3, 11)}, "c1")
    check(mmr("init", "--base", "main", "--intent-file", str(intent))[0] == 0, "sweep run starts")
    run = latest_run(repo, env)
    state = json.loads((run / "run.json").read_text())
    check(state["full_sweeps"] == 4 and state["sweep_min_old"] == 2 and state["codex_sweeps_from"] == 2,
          "new runs store the sweep cap and rule")
    # This scenario is a run saved before the sweep rule: It sweeps again on 1 seen-in-full bug, with every reviewer.
    for key in ("sweep_min_old", "codex_sweeps_from"):
        state.pop(key)
    (run / "run.json").write_text(json.dumps(state))

    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "full" in out and "Full sweep 1" in out, "round 1 is sweep 1")
    review(1, ["P1\tsrc/deep/f.txt:5\taccepted\tBug one\tf.txt: Fix 9"])
    edit({9: "c2 9"}, "WIP round 1")
    code, out = mmr("close", "--tests", "make test")
    check(code == 0 and "Full sweep 2" in out and "1 newer" in out, "sweep 1's fixes lead to sweep 2")

    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "full range" in out and "Claude Agent opus:" in out and "No pass" not in out,
          "round 2 is full, with every reviewer in an older run")
    check("This is full sweep 2" in (run / "rounds" / "02" / "prompt.md").read_text(), "sweep 2 says so")
    review(2, ["P1\tsrc/deep/f.txt:5\taccepted\tMissed by sweep 1\tf.txt: Fix 7"])
    edit({7: "c3 7"}, "WIP round 2")
    code, out = mmr("close", "--tests", "make test")
    check(code == 0 and "Full sweep 3: Sweep 2 found 1 accepted P0/P1 on code an earlier sweep reviewed" in out,
          "a sweep that finds bugs on reviewed code leads to another sweep")

    check(mmr("next", "--phase", "code")[0] == 0, "round 3 opens")
    check(json.loads((run / "rounds" / "03" / "round.json").read_text())["scope"] == "full", "round 3 is full")
    review(3, ["P1\tsrc/deep/f.txt:8\taccepted\tOnly in new code\tf.txt: Fix 8",
               "P1\tsrc/deep/f.txt:1\trejected\tOld line\tPre-existing: f.txt:1",
               "P1\tf.txt:5\trejected\tShort path\tGuarded: f.txt:5",
               "P1\tnope.txt:3\trejected\tNo such file\tNo such file"])
    edit({8: "c4 8"}, "WIP round 3")
    code, out = mmr("close", "--tests", "make test")
    check(code == 0 and "Sweep 3 found nothing new on code an earlier sweep reviewed, so delta rounds" in out,
          "a sweep with bugs only in fix code goes back to delta rounds")
    ages = {r["id"]: r["age"] for r in mmr_tsv(run / "findings.tsv")}
    check(ages == {"R1-1": "newer", "R2-1": "seen-in-full", "R3-1": "newer", "R3-2": "before-branch",
                   "R3-3": "seen-in-full", "R3-4": "unknown"}, f"findings are dated: {ages}")
    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "delta range" in out, "round 4 is a delta")
    review(4, ["P1\tsrc/deep/f.txt:8\trejected\tFine\tGuarded: f.txt:8"])
    check(mmr("close")[0] == 0, "code converges")
    code, out = mmr("ages")
    check(code == 0 and "Round 2 code (full): 1 seen-in-full" in out and "R3-4" in out, "ages dates a finished run")
    report = mmr("report")[1]
    check("Full Code sweeps: 3 (rounds 1, 2, 3). Accepted P0/P1 on code an earlier sweep reviewed: "
          "round 2: 1, round 3: 0" in report, "report shows the sweeps")

    # The sweep cap, runs saved before sweeps, and the main repo once a worktree is gone.
    mod = load_module()
    s = json.loads((run / "run.json").read_text())
    s["phases"]["code"] = "open"
    saved = (run / "rounds.tsv").read_text()
    (run / "rounds.tsv").write_text("\n".join(saved.splitlines()[:3]) + "\n")  # As if round 2 just closed.
    check(mod.code_scope(run, s)[0] == "full", "sweep 2's bug on reviewed code asks for sweep 3")
    check(mod.code_scope(run, {**s, "full_sweeps": 2}) == ("delta", "2 full sweeps is the cap, so delta rounds "
                                                           "check the fixes"), "the sweep cap goes back to delta")
    old = {k: v for k, v in s.items() if k != "full_sweeps"}
    check([mod.code_scope(run, {**old, "phase_rounds": {**old["phase_rounds"], "code": c}})[0] for c in (0, 1, 2, 5)]
          == ["full", "full", "delta", "delta"], "runs saved before sweeps keep rounds 1-2 full")
    gone = repo / ".claude" / "worktrees" / "gone"
    dater = mod.Dater(str(gone), s["base_commit"])
    check(dater.repo == str(repo), "a missing worktree reads the main repo")
    head = g("rev-parse", "HEAD")
    check(dater.age(f"{gone}/src/deep/f.txt:5", head, head) == "seen-in-full"
          and dater.age(f"{repo}/src/deep/f.txt:1", head, None) == "before-branch",
          "absolute paths in the checkout or the main repo are dated")
    (run / "rounds.tsv").write_text(saved)

    # Final checks the code since the last full sweep. Late P0s are fixed; late P1s are listed, not looped on.
    sweep3 = mmr_tsv(run / "rounds.tsv")[2]["commit"]
    (run / "cleanup.md").write_text("Deleted nothing. Tests: make test passed.\n")
    check(mmr("next", "--phase", "docs")[0] == 0, "docs round")
    modes.write_text("clean")
    mmr("codex", "--reviewer", "terra")
    (run / "rounds" / "05" / "opus.answer.md").write_text("NO BLOCKING ISSUES.")
    mmr("claude-result", "--seconds", "1")
    check(mmr("close")[0] == 0, "docs clean")
    code, out = mmr("next", "--phase", "final")
    check(code == 0 and f"since-sweep range {sweep3[:12]}" in out, "Final starts at the last full sweep")
    rd = run / "rounds" / "06"
    check("Check the code changed since the last full Code sweep" in (rd / "prompt.md").read_text()
          and "+c4 8" in (rd / "diff.patch").read_text() and "+c1 5" not in (rd / "diff.patch").read_text(),
          "Final's diff is only the code since the sweep")
    review(6, ["P1\tsrc/deep/f.txt:8\topen\tNew code\tLater"])
    code, out = mmr("close")
    check(code == 1 and "only for the last allowed round" in out, "a Final finding on new code can't be left open")
    (rd / "triage.tsv").write_text(HEADER + "terra\tP1\tsrc/deep/f.txt:5\taccepted\tLate one\tf.txt: Fix 5\n"
                                   + "terra\tP0\tsrc/deep/f.txt:4\taccepted\tLate P0\tf.txt: Fix 4\n")
    edit({4: "c5 4"}, "WIP round 6")
    code, out = mmr("close", "--tests", "make test")
    check(code == 1 and "This P1 is late" in out, "a late P1 isn't fixed in Final")
    code, out = mmr("ages")
    check("seen-in-full late\tsrc/deep/f.txt:5\tLate one\tlate P1: List it as open" in out
          and "late P0: Fix it" in out, "ages tells which Final rows are late before the fix")
    (rd / "triage.tsv").write_text(HEADER + "terra\tP1\tsrc/deep/f.txt:5\topen\tLate one\tListed for later\n"
                                   + "terra\tP0\tsrc/deep/f.txt:4\taccepted\tLate P0\tf.txt: Fix 4\n")
    code, out = mmr("close", "--tests", "make test")
    check(code == 0 and "not clean" in out and "1 late P1(s) listed" in out, "late P0 fixed, late P1 listed")
    check(mmr("next", "--phase", "final")[0] == 0, "Final delta round checks the P0 fix")
    review(7, ["P1\tsrc/deep/f.txt:6\topen\tLate again\tListed for later"])
    code, out = mmr("close")
    check(code == 0 and "Final converged" in out and "STOP" not in out, "late P1s alone don't stop Final")
    rows = {r["id"]: r for r in mmr_tsv(run / "findings.tsv")}
    check([rows[i]["tag"] for i in ("R3-3", "R6-1", "R6-2", "R7-1")] == ["", "late", "late", "late"],
          "only Final rows on swept code get the late tag")
    report = mmr("report")[1]
    check("Late Final findings (on code a full sweep already reviewed): 3; 1 fixed, 2 listed" in report
          and "## Late P1s (listed, not looped on)" in report and "R7-1 [P1]" in report
          and "## Open items\n\nNone." in report, "report shows the late count and the listed P1s")
    code, out = mmr("ages")
    check("Round 6 final (since-sweep): 2 seen-in-full; 2 late" in out, "ages counts late Final findings")

    # A run whose last sweep is HEAD has nothing for Final to check.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new")[0] == 0, "quiet run starts")
    run = latest_run(repo, env)
    for n, phase in ((1, "code"), (2, "docs")):
        if phase == "docs":
            (run / "cleanup.md").write_text("Nothing.\n")
        check(mmr("next", "--phase", phase)[0] == 0, f"quiet {phase} round")
        modes.write_text("clean")
        mmr("codex", "--reviewer", "terra")
        (run / "rounds" / f"{n:02d}" / "opus.answer.md").write_text("NO BLOCKING ISSUES.")
        mmr("claude-result", "--seconds", "1")
        check(mmr("close")[0] == 0, f"quiet {phase} round closes clean")
    code, out = mmr("next", "--phase", "final")
    check(code == 0 and "Nothing changed since the last full sweep" in out, "Final with nothing to check converges")
    check(json.loads((run / "run.json").read_text())["phases"]["final"] == "converged", "Final is marked converged")

    # New runs: Another sweep needs 2+ accepted P0/P1 on old code (swept or older than the branch),
    # and from sweep 2 on only Codex reviewers sweep.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new")[0] == 0, "sweep-rule run starts")
    run = latest_run(repo, env)
    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "Claude Agent opus:" in out and "No pass" not in out, "sweep 1 has every reviewer")
    review(1, ["P1\tsrc/deep/f.txt:9\taccepted\tFix bug\tf.txt: Fix 9"])
    edit({9: "d1 9"}, "WIP rule round 1")
    code, out = mmr("close", "--tests", "make test")
    check(code == 0 and "Full sweep 2, after sweep 1's fixes" in out, "sweep 2 always follows a non-clean sweep 1")
    code, out = mmr("next", "--phase", "code")
    rd = run / "rounds" / "02"
    check(code == 0 and "opus: No pass this round. From full sweep 2 on, only terra sweeps." in out
          and "Claude Agent" not in out, "sweep 2 is for Codex reviewers only")
    check(json.loads((rd / "round.json").read_text()).get("assign") == {"opus": []}, "the round gives opus no pass")
    check("  opus: no pass this round" in mmr("status")[1], "status shows who sits out")
    modes.write_text("finding")
    check(mmr("codex", "--reviewer", "terra")[0] == 0, "terra sweeps alone")
    (rd / "opus.answer.md").write_text("NO BLOCKING ISSUES.")
    code, out = mmr("claude-result", "--seconds", "1")
    check(code == 1 and "opus has no pass in this round" in out, "an answer from a reviewer who sat out is refused")
    code, out = mmr("barrier")
    check(code == 0 and "terra: ok" in out and "opus" not in out, "the barrier waits only for terra")
    (rd / "triage.tsv").write_text(HEADER + "terra,opus\tP1\tsrc/deep/f.txt:5\taccepted\tSwept line\tf.txt: Fix 5\n")
    edit({5: "d2 5", 1: "d2 1"}, "WIP rule round 2")
    code, out = mmr("close", "--tests", "make test")
    check(code == 1 and "Bad reviewers" in out, "a reviewer who sat out can't be credited")
    (rd / "triage.tsv").write_text(HEADER + "terra\tP1\tsrc/deep/f.txt:5\taccepted\tSwept line\tf.txt: Fix 5\n"
                                   + "terra\tP1\tsrc/deep/f.txt:1\taccepted\tBase line\tf.txt: Fix 1\n")
    code, out = mmr("close", "--tests", "make test")
    check(code == 0 and "Full sweep 3: Sweep 2 found 2 accepted P0/P1 on old code" in out
          and "1 before-branch, 1 seen-in-full" in out, "two bugs on old code lead to sweep 3")
    check(mmr("next", "--phase", "code")[0] == 0, "sweep 3 opens")
    review(3, ["P1\tsrc/deep/f.txt:3\taccepted\tOne more\tf.txt: Fix 3"])
    edit({3: "d3 3"}, "WIP rule round 3")
    code, out = mmr("close", "--tests", "make test")
    check(code == 0 and "Sweep 3 found 1 accepted P0/P1 on old code, under 2, so delta rounds check the fixes" in out,
          "one bug on old code ends the sweeps")
    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "delta range" in out and "Claude Agent opus:" in out, "delta rounds have every reviewer")
    modes.write_text("clean")
    mmr("codex", "--reviewer", "terra")
    (run / "rounds" / "04" / "opus.answer.md").write_text("NO BLOCKING ISSUES.")
    check(mmr("claude-result", "--seconds", "1")[0] == 0, "opus answers the delta round")
    check(mmr("close")[0] == 0, "sweep-rule run converges")
    report = mmr("report")[1]
    check("Full Code sweeps: 3 (rounds 1, 2, 3). Accepted P0/P1 on old code: round 2: 2, round 3: 1" in report
          and "| opus | 0 | 0 | 0 | 0 | 0 | 0 | 2 |" in report, "report counts old-code bugs and opus's passes")


def legacy_findings_tests(tmp, env, intent):
    """A findings.tsv saved before the age column gains it at the next close; old rows read as undated."""
    repo = tmp / "legacy"
    repo.mkdir()

    def g(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.t", *args], cwd=repo, capture_output=True)

    def mmr(*args):
        r = subprocess.run([sys.executable, MMR, "--repo", str(repo), *args], capture_output=True,
                           text=True, env={**env, **GIT_ID}, timeout=90)
        return r.returncode, r.stdout + r.stderr

    (repo / "a.txt").write_text("one\n")
    g("init", "-q", "-b", "main")
    g("add", "-A")
    g("commit", "-qm", "one")
    g("checkout", "-qb", "feature")
    (repo / "a.txt").write_text("two\n")
    g("commit", "-qam", "two")
    check(mmr("init", "--base", "main", "--intent-file", str(intent))[0] == 0, "legacy run starts")
    run = latest_run(repo, env)
    old_cols = ["id", "round", "phase", "reviewers", "sev", "loc", "status", "summary", "note"]
    (run / "findings.tsv").write_text("\t".join(old_cols) + "\n" + "R0-1\t0\tcode\tterra\tP1\ta.txt:1\trejected\tOld\tOld\n")
    state = json.loads((run / "run.json").read_text())
    for key in ("slices", "full_sweeps", "caps", "max_rounds"):
        state.pop(key, None)
    (run / "run.json").write_text(json.dumps(state))
    check(mmr("next", "--phase", "code")[0] == 0, "legacy run opens a round")
    Path(env["FAKE_MODE_FILE"]).write_text("finding")
    mmr("codex", "--reviewer", "terra")
    (run / "rounds" / "01" / "opus.answer.md").write_text("NO BLOCKING ISSUES.")
    mmr("claude-result", "--seconds", "1")
    (run / "rounds" / "01" / "triage.tsv").write_text(HEADER + "terra\tP1\ta.txt:1\taccepted\tNew\ta.txt: Three\n")
    (repo / "a.txt").write_text("three\n")
    g("commit", "-qam", "WIP round 1")
    code, out = mmr("close", "--tests", "make test")
    check(code == 0 and "sweep" not in out, "legacy run closes without sweeps")
    rows = mmr_tsv(run / "findings.tsv")
    check(len(rows) == 2 and rows[0]["age"] == "" and rows[1]["age"] == "newer", "old rows stay, new rows are dated")
    check(mmr("report")[0] == 0 and mmr("status")[0] == 0, "legacy run still reports")
    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "full range" in out and "sweep" not in out, "legacy round 2 is full, with no sweep")


def mmr_tsv(path):
    lines = Path(path).read_text().splitlines()
    cols = lines[0].split("\t")
    return [dict(zip(cols, ln.split("\t"))) for ln in lines[1:] if ln.strip()]


def picker_tests(tmp):
    """mmr picker: a small CDN loader only for a pushed, unchanged picker.js on GitHub; inline otherwise."""
    import base64
    import hashlib
    import shutil
    import re
    root = Path(MMR).parent.parent
    js = (root / "assets" / "picker.js").read_bytes()

    # The picker offers exactly the helper's reviewers, in its order, with its defaults.
    spec = importlib.util.spec_from_file_location("mmr", MMR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = re.findall(r"\['(\w+)','\w+','[\w.-]+','([gc])','(\w+)','([\w.]*)',([01]),([01])\]", js.decode())
    check([r[0] for r in rows] == mod.ALL_REVIEWERS, "picker lists every reviewer in the helper's order")
    check(all((r[1] == "g") == bool(r[3]) == (r[0] in mod.MODELS) for r in rows),
          "only Codex models get the OpenAI logo and fast mode")
    picked = " ".join(r[0] + ":" + r[2] + (":fast" if r[3] and r[5] == "1" else "") for r in rows if r[4] == "1")
    check(picked == mod.DEFAULT_SPEC, "picker starts with the default panel")
    text = js.decode()
    start = re.search(r"S=\{preset:'standard',code:(\d+),spare:(\d+)\}", text)
    check(start and (int(start[1]), int(start[2])) == (mod.CAPS["code"], mod.SPARE), "picker starts with Code 20, Spare 2")
    ranges = dict((m[0], (int(m[1]), int(m[2]))) for m in re.findall(r"\['(code|spare)','\w+','[^']+',(\d+),(\d+),", text))
    check(ranges == {"code": mod.CODE_RANGE, "spare": mod.SPARE_RANGE}, "picker steppers use the helper's ranges")
    presets = [int(c) for c in re.findall(r"\['(?:standard|long|max)','\w+',(\d+)\]", text)]
    check(len(presets) == 3 and all(mod.CODE_RANGE[0] <= c <= mod.CODE_RANGE[1] for c in presets),
          "picker presets are valid Code caps")

    def skill_at(folder):
        (folder / "scripts").mkdir(parents=True)
        (folder / "assets").mkdir()
        shutil.copy(MMR, folder / "scripts" / "mmr.py")
        (folder / "assets" / "picker.js").write_bytes(js)

    def g(cwd, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.t", *args], cwd=cwd,
                              capture_output=True, text=True).stdout.strip()

    def picker(folder, *args):
        r = subprocess.run([sys.executable, str(folder / "scripts" / "mmr.py"), "picker", *args],
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout

    skill = tmp / "picker-skill"
    skill_at(skill)
    code, out = picker(skill)
    check(code == 0 and "Picker: Inline" in out and js.decode() in out, "picker outside git is sent inline")
    g(skill, "init", "-q", "-b", "main")
    g(skill, "add", "-A")
    g(skill, "commit", "-qm", "one")
    check("Inline, because the latest picker.js commit isn't pushed" in picker(skill)[1], "unpushed picker is sent inline")
    g(skill, "remote", "add", "origin", "https://github.com/o/r.git")
    g(skill, "update-ref", "refs/remotes/origin/main", "HEAD")
    sha = g(skill, "rev-parse", "HEAD")
    sri = "sha384-" + base64.b64encode(hashlib.sha384(js).digest()).decode()
    code, out = picker(skill)
    widget = out.split("Widget code:\n", 1)[-1]
    check(code == 0 and f"cdn.jsdelivr.net/gh/o/r@{sha}/assets/picker.js'" in out and sri in out,
          "pushed picker loads from the CDN at its commit, checksum pinned")
    check(widget.startswith('<h2 class="sr-only">') and len(widget) < 1500 and "offline picker" in widget,
          "the loader is small and offers the offline picker")
    check("Inline, because --offline" in picker(skill, "--offline")[1], "--offline sends it inline")
    (skill / "assets" / "picker.js").write_bytes(js + b"\n")
    check("Inline, because picker.js has uncommitted changes" in picker(skill)[1], "a changed picker is sent inline")
    (skill / "assets" / "picker.js").write_bytes(js)
    g(skill, "remote", "set-url", "origin", "https://gitlab.com/o/r.git")
    check("Inline, because origin isn't a GitHub repo" in picker(skill)[1], "a non-GitHub origin is sent inline")
    mono = tmp / "picker-mono"
    skill_at(mono / "skills" / "mmr")
    g(mono, "init", "-q", "-b", "main")
    g(mono, "add", "-A")
    g(mono, "commit", "-qm", "one")
    g(mono, "remote", "add", "origin", "git@github.com:o/mono.git")
    g(mono, "update-ref", "refs/remotes/origin/main", "HEAD")
    check(f"gh/o/mono@{g(mono, 'rev-parse', 'HEAD')}/skills/mmr/assets/picker.js'" in picker(mono / "skills" / "mmr")[1],
          "a skill in a repo subfolder gets the right CDN path")
    installed = Path.home() / ".claude" / "agents" / "multimodel-reviewer.md"
    check(not installed.exists() or installed.read_text() == (root / "agents" / "multimodel-reviewer.md").read_text(),
          "the installed reviewer agent matches the repo copy")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="mmr-test-"))
    repo = tmp / "repo"
    repo.mkdir()
    fake = tmp / "codex"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    modes = tmp / "modes"
    modes.write_text("")
    env = {**os.environ, "MMR_CODEX_BIN": str(fake), "FAKE_MODE_FILE": str(modes),
           "MMR_TICK_SECONDS": "1", "MMR_QUIET_SECONDS": "2", "MMR_RUNS_HOME": str(tmp / "runs")}

    def sh(*cmd):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    def commit(text, msg):
        (repo / "a.txt").write_text(text)
        sh("git", "add", "-A")
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t.t", "commit", "-qm", msg)

    def mmr(*args, extra=None):
        r = subprocess.run([sys.executable, MMR, "--repo", str(repo), *args], capture_output=True,
                           text=True, env={**env, **(extra or {})}, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def run_dir():
        return latest_run(repo, env)

    def rd(n):
        return run_dir() / "rounds" / f"{n:02d}"

    def reviewers(n, terra="clean", claude="NO BLOCKING ISSUES."):
        modes.write_text(terra)
        check(mmr("codex", "--reviewer", "terra")[0] == (0 if terra != "bad" else 1), f"terra round {n}")
        # Older runs call their Opus reviewer "claude".
        who = "claude" if "claude" in json.loads((run_dir() / "run.json").read_text()).get(
            "reviewers", ["claude"]) else "opus"
        (rd(n) / f"{who}.answer.md").write_text(claude)
        check(mmr("claude-result", "--seconds", "60")[0] == 0, f"{who} round {n}")

    intent = tmp / "intent.md"
    intent.write_text("Make a.txt say two.\n")
    parser_tests()
    slice_unit_tests()
    start_gate_tests(tmp, env, intent)
    location_tests(tmp, env, intent)
    slice_tests(tmp, env, intent)
    sweep_tests(tmp, env, intent)
    legacy_findings_tests(tmp, env, intent)

    sh("git", "init", "-q", "-b", "main")
    commit("one\n", "one")
    sh("git", "checkout", "-qb", "feature")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent))
    check(code == 1 and "Nothing to review" in out, "refuses empty diff")
    commit("two\n", "two")
    check(mmr("init", "--base", "main")[0] == 1, "refuses missing intent")
    check(mmr("init", "--base", "main", "--intent-file", str(intent))[0] == 0, "init")
    check(mmr("init", "--base", "main", "--intent-file", str(intent))[0] == 1, "refuses second run")
    # Every reviewer sweeps in this run, as in runs saved before codex_sweeps_from, so round 2 can merge two.
    state = json.loads((run_dir() / "run.json").read_text())
    state.pop("codex_sweeps_from")
    (run_dir() / "run.json").write_text(json.dumps(state))
    check(subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                         text=True).stdout == "", "run folder leaves the tree clean")

    # Round 1: full diff, one finding, a malformed Codex rerun, a rejection.
    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "full" in out, "round 1 full")
    prompt = (rd(1) / "prompt.md").read_text()
    check("Make a.txt say two." in prompt and "## Reviewer rules" in prompt, "prompt has intent and rules")
    check("+two" in (rd(1) / "diff.patch").read_text(), "diff written")
    check(mmr("next", "--phase", "code")[0] == 1, "refuses second open round")
    check(mmr("barrier")[0] == 1, "barrier blocks before results")
    reviewers(1, terra="bad")
    flags = Path(str(modes) + ".args").read_text()
    check('web_search="live"' in flags and "-s read-only" in flags, "web on, sandbox read-only")
    check("--disable apps" in flags and "--disable plugins" in flags and "--disable multi_agent" in flags,
          "apps, plugins and subagents off")
    check("danger" not in flags and "workspace-write" not in flags, "no write access flags")
    check("service_tier" not in flags, "fast mode is off by default")
    check("web search" in (rd(1) / "prompt.md").read_text(), "prompt has the web rule")
    check(mmr("barrier")[0] == 1, "barrier blocks on malformed")
    modes.write_text("finding")
    check(mmr("codex", "--reviewer", "terra")[0] == 0, "rerun passes")
    code, out = mmr("codex", "--reviewer", "terra")
    check(code == 1 and "already has" in out, "refuses rerun after success")
    check(mmr("barrier")[0] == 0, "barrier passes")
    code, out = mmr("close")
    check(code == 1 and "triage.tsv" in out, "findings need triage")
    (rd(1) / "triage.tsv").write_text(HEADER + "terra\tP1\ta.txt:1\taccepted\tBad value\ta.txt: Use three\n")
    code, out = mmr("close")
    check(code == 1 and "WIP commit" in out, "accepted needs a commit")
    commit("three\n", "WIP multimodel-review round 1")
    code, out = mmr("close")
    check(code == 1 and "--tests" in out, "accepted needs tests")
    check(mmr("close", "--tests", "make test")[0] == 0, "close round 1")

    # Round 2: full, Terra and Claude raise the same finding, merged into one accepted row.
    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "full" in out, "round 2 full")
    check("R1-1 a.txt:1: a.txt: Use three" in (rd(2) / "prompt.md").read_text(), "fix list in prompt")
    reviewers(2, terra="finding", claude="[P1] a.txt:1 | x -> y | Do z.")
    (rd(2) / "triage.tsv").write_text(
        HEADER + "terra,opus\tP1\ta.txt:1\taccepted\tStill wrong\ta.txt: Use four\n")
    commit("four\n", "WIP multimodel-review round 2")
    check(mmr("close", "--tests", "make test")[0] == 0, "close round 2")

    # Round 3: delta, clean by rejection, so Code converges.
    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "delta" in out, "round 3 delta")
    delta = (rd(3) / "diff.patch").read_text()
    check("+four" in delta and "-three" in delta and "-one" not in delta, "delta covers only the last fix")
    reviewers(3, terra="finding")
    (rd(3) / "triage.tsv").write_text(
        HEADER + "terra\tP1\ta.txt:1\trejected\tNot real\tCaller guards it: a.txt:1\n")
    code, out = mmr("close")
    check(code == 0 and "Code converged" in out, "code converges on rejection")

    # Gates: Docs needs cleanup.md; Final needs Docs.
    code, out = mmr("next", "--phase", "docs")
    check(code == 1 and "cleanup.md" in out, "docs needs cleanup")
    check(mmr("next", "--phase", "final")[0] == 1, "final needs docs")
    (run_dir() / "cleanup.md").write_text("Deleted nothing. Tests: make test passed.\n")
    check(mmr("next", "--phase", "docs")[0] == 0, "docs round")
    ledger = (rd(4) / "prompt.md").read_text()
    check("Caller guards it" in ledger, "ledger in prompt")
    reviewers(4)
    check(mmr("close")[0] == 0, "docs clean")

    # Evidence round reopens Code on an accepted P1.
    commit("five\n", "WIP evidence fix")
    check(mmr("next", "--phase", "evidence", "--note", "Device run showed five")[0] == 0, "evidence round")
    reviewers(5, terra="finding")
    (rd(5) / "triage.tsv").write_text(HEADER + "terra\tP1\ta.txt:1\taccepted\tDevice bug\ta.txt: Use six\n")
    commit("six\n", "WIP round 5")
    code, out = mmr("close", "--tests", "make test")
    check(code == 0 and "reopened" in out, "evidence reopens code")
    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "delta" in out, "reopened code is delta")
    reviewers(6)
    check(mmr("close")[0] == 0, "reopened code converges")

    # Final in a run saved before Final became a check: a full round 1, then a delta round that still fails.
    state = json.loads((run_dir() / "run.json").read_text())
    state.pop("final_check")
    (run_dir() / "run.json").write_text(json.dumps(state))
    code, out = mmr("next", "--phase", "final")
    check(code == 0 and "full range" in out, "an older run's Final round 1 is full")
    reviewers(7, terra="finding")
    (rd(7) / "triage.tsv").write_text(HEADER + "terra\tP1\ta.txt:1\taccepted\tLast bug\ta.txt: Use seven\n")
    commit("seven\n", "WIP round 7")
    check(mmr("close", "--tests", "make test")[0] == 0, "final fix")
    check(mmr("next", "--phase", "final")[0] == 0, "final delta")
    reviewers(8, terra="finding")
    (rd(8) / "triage.tsv").write_text(HEADER + "terra\tP1\ta.txt:1\taccepted\tStill bad\tNope\n")
    code, out = mmr("close", "--tests", "make test")
    check(code == 1 and "unreviewed" in out, "last round refuses a fix")
    (rd(8) / "triage.tsv").write_text(HEADER + "terra\tP1\ta.txt:1\topen\tStill bad\tNeeds a redesign\n")
    code, out = mmr("close")
    check(code == 0 and "STOP" in out, "final failure stops")
    code, out = mmr("next", "--phase", "final")
    check(code == 3, "stopped run refuses rounds")

    # Report and archive.
    code, out = mmr("report")
    check(code == 0 and "| terra |" in out and "| sol |" not in out and "Still bad" in out, "report")
    check("Caller guards it" not in out.split("## Disputed")[1].split("## Tests")[0], "single rejection not disputed")
    check("make test" in out and "git reset --soft" in out, "report has tests and squash")
    code, out = mmr("archive")
    check(code == 0 and "Archived" in out, "archive")
    state = json.loads((run_dir() / "run.json").read_text())
    check(state["finished"] is True, "run finished")

    # Second run: caps, crash, time cap, rerun limit, monitor.
    check(mmr("init", "--base", "main", "--intent-file", str(intent))[0] == 0, "new run after finish")
    state = json.loads((run_dir() / "run.json").read_text())
    state.pop("reviewers")  # Sol's panel, for the two-Codex scenarios below.
    check(state["caps"]["code"] == 20 and state["max_rounds"] == 26, "new runs store Code 20 and limit 26")
    state["phase_rounds"]["code"] = 20
    (run_dir() / "run.json").write_text(json.dumps(state))
    code, out = mmr("next", "--phase", "code")
    check(code == 3 and "20 rounds" in out, "code cap")
    state["phase_rounds"]["code"] = 0
    state["rounds"] = 26
    (run_dir() / "run.json").write_text(json.dumps(state))
    check(mmr("next", "--phase", "code")[0] == 3, "hard stop")
    old = {k: v for k, v in state.items() if k not in ("caps", "max_rounds")}
    old["rounds"], old["phase_rounds"]["code"] = 0, 12
    (run_dir() / "run.json").write_text(json.dumps(old))
    code, out = mmr("next", "--phase", "code")
    check(code == 3 and "12 rounds" in out, "a run saved before keeps Code 12")
    old["phase_rounds"]["code"], old["rounds"] = 0, 18
    (run_dir() / "run.json").write_text(json.dumps(old))
    code, out = mmr("next", "--phase", "code")
    check(code == 3 and "18 rounds" in out, "a run saved before keeps the 18 limit")
    state["rounds"] = 0
    (run_dir() / "run.json").write_text(json.dumps(state))
    check(mmr("next", "--phase", "code")[0] == 0, "round after caps")

    modes.write_text("crash crash")
    check(mmr("codex", "--reviewer", "sol")[0] == 1, "crash attempt 1")
    check(mmr("codex", "--reviewer", "sol")[0] == 1, "crash attempt 2")
    code, out = mmr("codex", "--reviewer", "sol")
    check(code == 4 and "limit" in out, "rerun limit")

    modes.write_text("hang")
    proc = subprocess.Popen([sys.executable, MMR, "--repo", str(repo), "codex", "--reviewer", "terra"],
                            env={**env, "MMR_CAP_SECONDS": "6"}, stdout=subprocess.DEVNULL)
    mon = subprocess.run([sys.executable, MMR, "--repo", str(repo), "monitor", "--reviewer", "terra"],
                         env=env, capture_output=True, text=True, timeout=40)
    proc.wait(timeout=20)
    check("QUIET terra" in mon.stdout, "monitor warns on quiet")
    check("FAILED terra: timeout" in mon.stdout and mon.returncode == 1, "monitor reports timeout by PID exit")
    code, out = mmr("codex", "--reviewer", "terra")
    check(code == 4 and "hung" in out, "no rerun after timeout")
    check(list(rd(1).glob("terra.a1.monitor.log")), "monitor log written")

    # A silent reviewer is stopped at the silence limit, well before the total cap.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new")[0] == 0, "third run")
    check(mmr("next", "--phase", "code")[0] == 0, "third run round")
    code, out = mmr("codex", "--reviewer", "sol")
    check(code == 1 and "isn't a reviewer" in out, "new runs refuse Sol")
    check("sol" not in mmr("barrier")[1], "barrier doesn't wait for Sol")
    modes.write_text("hang")
    began = time.time()
    code, out = mmr("codex", "--reviewer", "terra", extra={"MMR_SILENCE_SECONDS": "3"})
    check(code == 1 and "stalled" in out and time.time() - began < 15, "silence limit stops it")
    code, out = mmr("codex", "--reviewer", "terra")
    check(code == 4 and "hung" in out, "no rerun after a stall")

    # A saved answer that only an older parser refused: re-read it, with no reviewer run.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new")[0] == 0, "fourth run")
    check(mmr("next", "--phase", "code")[0] == 0, "fourth run round")
    code, out = mmr("reread", "--reviewer", "terra")
    check(code == 1 and "no malformed" in out, "nothing to re-read yet")
    reviewers(1)
    code, out = mmr("reread", "--reviewer", "terra")
    check(code == 1 and "no malformed" in out, "valid answer isn't re-read")
    for name in ("terra.final.md", "terra.a1.meta.json"):
        (rd(1) / name).unlink()
    for attempt in (1, 2):
        (rd(1) / f"terra.a{attempt}.final.md").write_text("[P1] [a.txt:1](/abs/a.txt:1) | x -> y | Fix.\n")
        (rd(1) / f"terra.a{attempt}.meta.json").write_text(json.dumps(
            {"reviewer": "terra", "attempt": attempt, "status": "malformed", "findings": 0,
             "problems": ["Bad line"], "seconds": 90, "thread_id": f"t{attempt}"}))
    check(mmr("barrier")[0] == 1, "barrier blocks at the rerun limit")
    code, out = mmr("codex", "--reviewer", "terra")
    check(code == 4 and "limit" in out, "no third reviewer run")
    calls = Path(str(modes) + ".args").stat().st_mtime
    saved = (rd(1) / "terra.a2.final.md").read_text()
    (rd(1) / "terra.a2.final.md").write_text("")
    code, out = mmr("reread", "--reviewer", "terra")
    check(code == 1 and "malformed" in out and mmr("barrier")[0] == 1, "an empty answer stays invalid")
    (rd(1) / "terra.a2.final.md").write_text(saved)
    code, out = mmr("reread", "--reviewer", "terra")
    check(code == 0 and "attempt 2: ok; 1 finding" in out, "re-read accepts the saved answer")
    check(Path(str(modes) + ".args").stat().st_mtime == calls, "re-read runs no reviewer")
    meta = json.loads((rd(1) / "terra.a2.meta.json").read_text())
    check(meta["thread_id"] == "t2" and meta["seconds"] == 90 and meta["status"] == "ok", "re-read keeps run details")
    check((rd(1) / "terra.final.md").exists() and mmr("barrier")[0] == 0, "barrier passes after the re-read")
    (rd(1) / "triage.tsv").write_text(HEADER + "terra\tP1\ta.txt:1\trejected\tNot real\tGuarded: a.txt:1\n")
    check(mmr("close")[0] == 0, "round closes after the re-read")
    check("Any clear form is fine" in (rd(1) / "prompt.md").read_text(), "prompt doesn't demand a format")

    # A Codex exit error after a full answer keeps the paid-for answer.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new")[0] == 0, "fifth run")
    check(mmr("next", "--phase", "code")[0] == 0, "fifth run round")
    modes.write_text("late-crash")
    code, out = mmr("codex", "--reviewer", "terra")
    check(code == 0 and "attempt 1: ok; 1 finding" in out, "answer survives a late exit error")

    # A run from before Sol was dropped finishes with its three reviewers.
    mmr("stop", "--reason", "Next scenario.")
    check(mmr("init", "--base", "main", "--intent-file", str(intent), "--new")[0] == 0, "sixth run")
    state = json.loads((run_dir() / "run.json").read_text())
    state.pop("reviewers")
    (run_dir() / "run.json").write_text(json.dumps(state))
    check(mmr("next", "--phase", "code")[0] == 0, "sixth run round")
    modes.write_text("finding")
    check(mmr("codex", "--reviewer", "sol")[0] == 0, "older run still runs Sol")
    reviewers(1)
    code, out = mmr("barrier")
    check(code == 0 and "sol: ok" in out, "older run's barrier includes Sol")
    code, out = mmr("close")
    check(code == 1 and "triage.tsv" in out, "older run needs Sol's finding triaged")
    (rd(1) / "triage.tsv").write_text(HEADER + "sol\tP1\ta.txt:1\trejected\tNot real\tGuarded: a.txt:1\n")
    check(mmr("close")[0] == 0, "older run closes with Sol's finding")
    check("| sol |" in mmr("report")[1], "older run's report keeps Sol")

    # Picker options: reviewers, fast mode and round limit are saved with the run.
    mmr("stop", "--reason", "Next scenario.")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new", "--reviewers", "bogus")
    check(code == 1 and "--reviewers" in out, "unknown reviewer refused")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new", "--max-rounds", "0")
    check(code == 1 and "--max-rounds" in out, "bad round limit refused")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new",
                    "--reviewers", "astra,terra", "--fast", "off", "--max-rounds", "2")
    check(code == 0 and "Reviewers astra (max, standard), terra (max, standard); round limit 2" in out,
          "options saved")
    check(mmr("next", "--phase", "code")[0] == 0, "options run round 1")
    modes.write_text("clean")
    check(mmr("codex", "--reviewer", "astra")[0] == 0, "Astra runs")
    flags = Path(str(modes) + ".args").read_text()
    check("-m gpt-6-astra" in flags and "service_tier" not in flags, "Astra model, no fast tier")
    check(mmr("codex", "--reviewer", "terra")[0] == 0, "Terra runs")
    code, out = mmr("barrier")
    check(code == 0 and "claude" not in out, "barrier needs only the chosen reviewers")
    check(mmr("codex", "--reviewer", "sol")[1].count("isn't a reviewer") == 1, "unchosen Codex refused")
    check(mmr("close")[0] == 0, "options run closes round 1")
    commit("nine\n", "WIP evidence")
    check(mmr("next", "--phase", "evidence", "--note", "Device run")[0] == 0, "round 2 of 2")
    modes.write_text("clean clean")
    mmr("codex", "--reviewer", "astra")
    mmr("codex", "--reviewer", "terra")
    check(mmr("close")[0] == 0, "round 2 closes")
    commit("ten\n", "WIP evidence 2")
    code, out = mmr("next", "--phase", "evidence", "--note", "Again")
    check(code == 3 and "2 rounds" in out, "round limit stops the run")
    code, out = mmr("status")
    check("of 2" in out and "terra (max, standard)" in out, "status shows the options")
    check("round limit 2" in mmr("report")[1], "report shows the options")

    # Fast mode per Codex reviewer.
    mmr("stop", "--reason", "Next scenario.")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new",
                    "--reviewers", "terra,claude", "--fast", "claude")
    check(code == 1 and "--fast" in out, "fast mode refused for Claude")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new",
                    "--reviewers", "terra,claude", "--fast", "sol")
    check(code == 1 and "--fast" in out, "fast mode refused for an unchosen reviewer")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new",
                    "--reviewers", "terra,claude,astra", "--fast", "astra", "--max-rounds", "6")
    check(code == 0 and "Reviewers terra (max, standard), opus (xhigh), astra (max, fast); round limit 6" in out,
          "per-model fast mode saved")
    check(mmr("next", "--phase", "code")[0] == 0, "per-model run round 1")
    modes.write_text("clean clean")
    mmr("codex", "--reviewer", "astra")
    flags = Path(str(modes) + ".args").read_text()
    check("-m gpt-6-astra" in flags and 'service_tier="priority"' in flags, "Astra runs fast")
    mmr("codex", "--reviewer", "terra")
    flags = Path(str(modes) + ".args").read_text()
    check("-m gpt-6.1-terra" in flags and "service_tier" not in flags, "Terra runs standard")
    mmr("stop", "--reason", "Next scenario.")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new",
                    "--reviewers", "terra,claude,sol", "--fast", "on")
    check(code == 0 and "terra (max, fast), opus (xhigh), sol (max, fast)" in out,
          "fast on covers every Codex reviewer")
    check(mmr("next", "--phase", "code")[0] == 0, "fast-on run round 1")
    modes.write_text("clean clean")
    mmr("codex", "--reviewer", "sol")
    flags = Path(str(modes) + ".args").read_text()
    check("-m gpt-6.1-sol" in flags and 'service_tier="priority"' in flags, "Sol runs fast")
    mmr("codex", "--reviewer", "terra", "--standard-tier")
    flags = Path(str(modes) + ".args").read_text()
    check("-m gpt-6.1-terra" in flags and "service_tier" not in flags, "--standard-tier still overrides")

    # Effort per reviewer, and Luna.
    mmr("stop", "--reason", "Next scenario.")
    for bad in ("terra=ultra", "claude=bogus", "astra=max", "terra"):
        code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new",
                        "--reviewers", "terra,claude,luna", "--effort", bad)
        check(code == 1 and "--effort" in out, f"bad effort refused: {bad}")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new",
                    "--reviewers", "terra,claude,luna", "--effort", "terra=high, claude=xhigh",
                    "--fast", "luna")
    check(code == 0 and "terra (high, standard), opus (xhigh), luna (max, fast)" in out, "efforts saved")
    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "Claude Agent opus: model opus, effort xhigh, answer file " in out
          and "opus.answer.md" in out and "luna (max, fast)" in out,
          "next prints the settings and the Claude Agent's model and effort")
    modes.write_text("clean clean")
    mmr("codex", "--reviewer", "terra")
    flags = Path(str(modes) + ".args").read_text()
    check('model_reasoning_effort="high"' in flags and "service_tier" not in flags, "Terra runs at high")
    mmr("codex", "--reviewer", "luna")
    flags = Path(str(modes) + ".args").read_text()
    check("-m gpt-6-luna" in flags and 'model_reasoning_effort="max"' in flags
          and 'service_tier="priority"' in flags, "Luna runs at max, fast")
    meta = json.loads((rd(1) / "terra.a1.meta.json").read_text())
    check(meta["effort"] == "high", "meta records the run's effort")

    # Typed options: name[:effort][:fast], fast only when asked, rounds optional.
    cases = {
        "terra:max:fast opus:xhigh code:20 spare:2": "Reviewers terra (max, fast), opus (xhigh); round limit 26 "
                                                     "(Code 20, Docs 2, Final 2, Spare 2)",
        "terra:fast opus": "Reviewers terra (max, fast), opus (xhigh); round limit 26",
        "terra:high astra:max": "Reviewers terra (high, standard), astra (max, standard); round limit 26",
        "": "Reviewers terra (max, standard), opus (xhigh); round limit 26",
        "code:35": "Reviewers terra (max, standard), opus (xhigh); round limit 41 (Code 35, Docs 2, Final 2, Spare 2)",
        "terra code:50 spare:6": "round limit 60 (Code 50, Docs 2, Final 2, Spare 6)",
        "Terra:MAX:Fast, Opus": "Reviewers terra (max, fast), opus (xhigh); round limit 26",
        "terra:max opus:xhigh luna:low:fast spare:6.": "luna (low, fast); round limit 30",
        "luna:fast:medium claude:low": "Reviewers luna (medium, fast), opus (low); round limit 26",
        "terra fable:high sonnet haiku:low": "Reviewers terra (max, standard), fable (high), sonnet (xhigh), "
                                             "haiku (low); round limit 26",
    }
    for spec, want in cases.items():
        code, out = mmr("options", spec)
        check(code == 0 and want in out, f"options parse: {spec!r}")
    for spec in ("terra:maxx", "opus:fast", "fable:fast", "opus claude", "terra terra", "rounds:12", "code:19", "code:51", "code", "spare:1", "spare:7", "venus",
                 "terra:max:high", "terra:fast:fast", "terra:", "pick terra"):
        code, out = mmr("options", spec)
        check(code == 1 and "Init:" not in out, f"options refused: {spec!r}")
    code, out = mmr("options", "terra:fast opus")
    check('Init: --spec "terra:max:fast opus:xhigh code:20 spare:2"' in out, "options print the init spec")
    code, out = mmr("options", "rounds:12")
    check(code == 1 and "code:<20-50>" in out, "rounds: points to code: and spare:")
    mmr("stop", "--reason", "Next scenario.")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new", "--spec", "astra:high sol:fast")
    check(code == 0 and "Reviewers astra (high, standard), sol (max, fast); round limit 26" in out, "init takes --spec")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new", "--spec", "opus:fast")
    check(code == 1 and "Codex models only" in out, "init refuses a bad --spec")
    mmr("stop", "--reason", "Next scenario.")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new")
    check(code == 0 and "Reviewers terra (max, standard), opus (xhigh); round limit 26" in out,
          "plain init uses the new defaults, with fast mode off")

    # Several Claude reviewers, each its own Agent model.
    mmr("stop", "--reason", "Next scenario.")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new",
                    "--spec", "opus:high fable sonnet:low code:35 spare:4")
    check(code == 0 and "Reviewers opus (high), fable (xhigh), sonnet (low); round limit 43" in out,
          "Claude-only panel saved")
    state = json.loads((run_dir() / "run.json").read_text())
    check(state["caps"] == {"code": 35, "docs": 2, "final": 2} and state["max_rounds"] == 43,
          "the run stores its Code cap and limit")
    code, out = mmr("next", "--phase", "code")
    check(code == 0 and "Claude Agent opus: model opus, effort high" in out
          and "Claude Agent fable: model fable, effort xhigh" in out
          and "Claude Agent sonnet: model sonnet, effort low" in out, "next lists every Claude Agent")
    for who in ("opus", "fable", "sonnet"):
        (rd(1) / f"{who}.answer.md").write_text("NO BLOCKING ISSUES.")
    code, out = mmr("claude-result", "--seconds", "30")
    check(code == 1 and "--reviewer" in out, "2+ Claude reviewers need --reviewer")
    code, out = mmr("claude-result", "--reviewer", "haiku", "--seconds", "30")
    check(code == 1 and "isn't a Claude reviewer on this run" in out, "unchosen Claude reviewer refused")
    check(mmr("claude-result", "--reviewer", "fable", "--seconds", "30")[0] == 0, "Fable result saved")
    meta = json.loads((rd(1) / "fable.a1.meta.json").read_text())
    check(meta["model"] == "fable" and meta["effort"] == "xhigh", "meta records Fable's model and effort")
    code, out = mmr("barrier")
    check(code != 0 and "opus" in out and "sonnet" in out, "barrier waits for every Claude reviewer")
    mmr("claude-result", "--reviewer", "opus", "--seconds", "30")
    mmr("claude-result", "--reviewer", "sonnet", "--seconds", "30")
    check(mmr("barrier")[0] == 0, "barrier passes with every Claude answer")
    check(mmr("close")[0] == 0, "Claude-only round closes")
    check("| fable |" in mmr("report")[1], "report lists Fable")

    # Runs started in the same second get distinct folders.
    mmr("stop", "--reason", "Next scenario.")
    first = mmr("init", "--base", "main", "--intent-file", str(intent), "--new")
    mmr("stop", "--reason", "Next scenario.")
    second = mmr("init", "--base", "main", "--intent-file", str(intent), "--new")
    runs = [[l for l in out.splitlines() if l.startswith("Run:")] for _, out in (first, second)]
    check(first[0] == 0 and second[0] == 0 and runs[0] and runs[0] != runs[1], "back-to-back runs both start")
    picker_tests(tmp)
    print(f"All {passed} checks passed.")


if __name__ == "__main__":
    main()
