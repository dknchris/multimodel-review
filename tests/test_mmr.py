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


def picker_tests(tmp):
    """mmr picker: a small CDN loader only for a pushed, unchanged picker.js on GitHub; inline otherwise."""
    import base64
    import hashlib
    import shutil
    root = Path(MMR).parent.parent
    js = (root / "assets" / "picker.js").read_bytes()

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
    start_gate_tests(tmp, env, intent)
    location_tests(tmp, env, intent)

    sh("git", "init", "-q", "-b", "main")
    commit("one\n", "one")
    sh("git", "checkout", "-qb", "feature")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent))
    check(code == 1 and "Nothing to review" in out, "refuses empty diff")
    commit("two\n", "two")
    check(mmr("init", "--base", "main")[0] == 1, "refuses missing intent")
    check(mmr("init", "--base", "main", "--intent-file", str(intent))[0] == 0, "init")
    check(mmr("init", "--base", "main", "--intent-file", str(intent))[0] == 1, "refuses second run")
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
    check('service_tier="priority"' in flags, "fast mode is on by default")
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

    # Final: an accepted finding, then a delta round that still fails.
    check(mmr("next", "--phase", "final")[0] == 0, "final round")
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
        "terra:max:fast opus:xhigh rounds:26": "Reviewers terra (max, fast), opus (xhigh); round limit 26",
        "terra:fast opus": "Reviewers terra (max, fast), opus (xhigh); round limit 26",
        "terra:high astra:max": "Reviewers terra (high, standard), astra (max, standard); round limit 26",
        "": "Reviewers terra (max, fast), opus (xhigh); round limit 26",
        "rounds:6": "Reviewers terra (max, fast), opus (xhigh); round limit 6",
        "Terra:MAX:Fast, Opus": "Reviewers terra (max, fast), opus (xhigh); round limit 26",
        "terra:max opus:xhigh luna:low:fast rounds:6.": "luna (low, fast); round limit 6",
        "luna:fast:medium claude:low": "Reviewers luna (medium, fast), opus (low); round limit 26",
        "terra fable:high sonnet haiku:low": "Reviewers terra (max, standard), fable (high), sonnet (xhigh), "
                                             "haiku (low); round limit 26",
    }
    for spec, want in cases.items():
        code, out = mmr("options", spec)
        check(code == 0 and want in out, f"options parse: {spec!r}")
    for spec in ("terra:maxx", "opus:fast", "fable:fast", "opus claude", "terra terra", "rounds:0", "rounds:27", "rounds", "venus",
                 "terra:max:high", "terra:fast:fast", "terra:", "pick terra"):
        code, out = mmr("options", spec)
        check(code == 1 and "Init:" not in out, f"options refused: {spec!r}")
    code, out = mmr("options", "terra:fast opus")
    check('Init: --spec "terra:max:fast opus:xhigh rounds:26"' in out, "options print the init spec")
    mmr("stop", "--reason", "Next scenario.")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new", "--spec", "astra:high sol:fast")
    check(code == 0 and "Reviewers astra (high, standard), sol (max, fast); round limit 26" in out, "init takes --spec")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new", "--spec", "opus:fast")
    check(code == 1 and "Codex models only" in out, "init refuses a bad --spec")
    mmr("stop", "--reason", "Next scenario.")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new")
    check(code == 0 and "Reviewers terra (max, fast), opus (xhigh); round limit 26" in out,
          "plain init uses the new defaults")

    # Several Claude reviewers, each its own Agent model.
    mmr("stop", "--reason", "Next scenario.")
    code, out = mmr("init", "--base", "main", "--intent-file", str(intent), "--new",
                    "--spec", "opus:high fable sonnet:low rounds:2")
    check(code == 0 and "Reviewers opus (high), fable (xhigh), sonnet (low); round limit 2" in out,
          "Claude-only panel saved")
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
