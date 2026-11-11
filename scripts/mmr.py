#!/usr/bin/env python3
"""State, gates and Codex runner for the multimodel-review skill.

All run state lives on disk under ~/.claude/multimodel-review/, outside every
repo, so a compacted or restarted session can continue with `mmr.py status`.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
RULES_FILE = SKILL_DIR / "assets" / "reviewer-rules.md"
PICKER_JS = "assets/picker.js"
PICKER_TITLE = "Options for a new multi-model review: Reviewers with effort and fast mode for each, and round limit."
RUNS_HOME = Path(os.environ.get("MMR_RUNS_HOME", Path.home() / ".claude" / "multimodel-review"))

MODELS = {"terra": "gpt-6.1-terra", "astra": "gpt-6-astra", "sol": "gpt-6.1-sol", "luna": "gpt-6-luna"}
# Claude reviewers run as the multimodel-reviewer Agent with this model. "claude" is the Opus reviewer of older runs.
AGENT_MODEL = {"opus": "opus", "fable": "fable", "sonnet": "sonnet", "haiku": "haiku", "claude": "opus"}
REVIEWERS = ["terra", "opus"]  # Default panel.
ALL_REVIEWERS = ["terra", "opus", "fable", "astra", "sol", "sonnet", "haiku", "luna"]  # Picker order.
EFFORTS = ["low", "medium", "high", "xhigh", "max"]  # Codex "ultra" delegates to subagents, which stay off.
OLD_REVIEWERS = ["sol", "terra", "claude"]  # Runs from before Sol was dropped keep their panel.
EFFORT = os.environ.get("MMR_EFFORT", "max")  # Default Codex effort; a run's own choice wins.
CLAUDE_EFFORT = "xhigh"  # New runs; runs saved without an effort used high for Claude.
DEFAULT_SPEC = "terra:max:fast opus:xhigh"
CODEX = os.environ.get("MMR_CODEX_BIN", "codex")
CAP_SECONDS = int(os.environ.get("MMR_CAP_SECONDS", "3600"))
SILENCE_SECONDS = int(os.environ.get("MMR_SILENCE_SECONDS", "600"))
TICK_SECONDS = int(os.environ.get("MMR_TICK_SECONDS", "60"))
QUIET_SECONDS = int(os.environ.get("MMR_QUIET_SECONDS", "240"))
MONITOR_CAP_SECONDS = int(os.environ.get("MMR_MONITOR_CAP_SECONDS", str(28 * 60)))
MAX_ATTEMPTS = 2
CAPS = {"code": 20, "docs": 2, "final": 2}
HARD_STOP = 26  # 20 Code + 2 Docs + 2 Final + 2 spare.
OLD_CAPS = {"code": 12, "docs": 2, "final": 2}  # Runs saved before the caps were stored keep these.
OLD_HARD_STOP = 18
ALLOWED = {
    "code": {"P0", "P1"},
    "docs": {"P2"},
    "final": {"P0", "P1"},
    "evidence": {"P0", "P1", "P2"},
}
TRIAGE_HEADER = ["reviewers", "sev", "loc", "status", "summary", "note"]
FINDING_COLS = ["id", "round", "phase", "reviewers", "sev", "loc", "status", "summary", "note"]
ROUND_COLS = ["round", "phase", "phase_round", "scope", "commit", "fix_commit",
              "clean", "accepted", "rejected", "open", "tests"]
# Reviewers are never held to a format. These only count findings, on a best-effort basis.
SEVERITY_RE = re.compile(r"^\W*(?:\d+[.)]\W*)?(P[012])\b", re.I)
LOCATION_RE = re.compile(r"[\w./()@+~-]+:\d+(?:-\d+)?")
CLEAN_RE = re.compile(r"\bno\s+(?:blocking\s+)?(?:issues|findings|problems)\b", re.I)
SENSITIVE_RE = re.compile(
    r"^\.env(\.(?!example$|sample$|template$).*)?$|\.(pem|key|p8|p12|pfx|keystore|jks|mobileprovision)$"
    r"|^id_(rsa|ed25519)|^credentials|^secrets?\.|^service-account.*\.json$", re.I)
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024


def die(msg, code=1):
    print(f"ERROR: {msg}")
    sys.exit(code)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(repo, *args, check=True, timeout=120):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    if check and r.returncode:
        die(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else ""


def repo_root(repo):
    top = git(repo, "rev-parse", "--show-toplevel", check=False)
    if not top:
        die("Not inside a git repository.")
    return Path(top)


def old_runs_dir(top):
    return Path(git(top, "rev-parse", "--absolute-git-dir")) / "multimodal-review"


def runs_dir(top):
    """One folder per repo and worktree. A worktree's git dir sits inside the main
    checkout, where session guards refuse the Write tool, so runs live outside it."""
    git_dir = git(top, "rev-parse", "--absolute-git-dir")
    return RUNS_HOME / f"{top.name}-{hashlib.sha256(git_dir.encode()).hexdigest()[:12]}"


def current_run(top):
    """The latest run. Runs from before the move keep working where they are."""
    for root in (runs_dir(top), old_runs_dir(top)):
        pointer = root / "latest"
        if pointer.exists():
            run = root / pointer.read_text().strip()
            if (run / "run.json").exists():
                return run
    return None


def panel(s):
    return s.get("reviewers", OLD_REVIEWERS)


def max_rounds(s):
    return s.get("max_rounds", OLD_HARD_STOP)


def caps(s):
    return s.get("caps", OLD_CAPS)


def is_fast(s, who):
    """Older runs store one bool for every Codex reviewer; newer runs list the fast ones."""
    f = s.get("fast", True)
    return f if isinstance(f, bool) else who in f


def effort_for(s, who):
    """Older runs have no per-reviewer effort: Codex used EFFORT and Claude used high."""
    return s.get("effort", {}).get(who) or (EFFORT if who in MODELS else "high")


def settings_line(s):
    names = [f"{r} ({effort_for(s, r)}" + (f", {'fast' if is_fast(s, r) else 'standard'})" if r in MODELS else ")")
             for r in panel(s)]
    return f"Settings: Reviewers {', '.join(names)}; round limit {max_rounds(s)}"


def old_location_note(top, run):
    if run.parent != runs_dir(top):
        print("Old location: Run `mmr.py migrate` once every reviewer has finished, "
              "so the Write tool can reach this folder.")


def write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def read_json(path):
    return json.loads(Path(path).read_text())


def load(repo):
    run = current_run(repo_root(repo))
    if not run:
        die("No run found. Start one with: mmr.py init")
    return run, read_json(run / "run.json")


def save(run, state):
    write_json(run / "run.json", state)


def round_dir(run, n):
    return run / "rounds" / f"{n:02d}"


def changed_paths(top, left_out=()):
    """Changed and untracked paths, minus anything the owner left out of the review."""
    raw = subprocess.run(["git", "-C", str(top), "status", "--porcelain", "-z", "--untracked-files=all"],
                         capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL).stdout
    tokens = raw.split("\0")
    paths, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if len(token) < 4:
            continue
        if token[0] in "RC" or token[1] in "RC":
            i += 1
        paths.append(token[3:])
    skip = [p.rstrip("/") for p in left_out]
    return [p for p in paths if not any(p.rstrip("/") == s or p.startswith(s + "/") for s in skip)]


def flagged_paths(top, paths):
    flagged = []
    for rel in paths:
        full = top / rel
        if rel.endswith("/") and (full / ".git").exists():
            flagged.append(f"{rel} (embedded git repository)")
        elif SENSITIVE_RE.search(full.name):
            flagged.append(f"{rel} (looks sensitive)")
        elif full.is_file() and full.stat().st_size > MAX_SNAPSHOT_BYTES:
            flagged.append(f"{rel} (over 20 MB)")
    return flagged


def require_clean(top, left_out=()):
    dirty = changed_paths(top, left_out)
    if dirty:
        die("Working tree is not clean, so reviewers would not read the commit under review:\n"
            + "\n".join(dirty[:20]))


# ---------- final answers ----------

def parse_final(text):
    """Return (ok, findings, problems). Each finding is (sev, loc).

    Only an empty answer is invalid, because a rerun costs far more than an odd format.
    An answer with no severity tags counts as one finding, so the fixer has to read it.
    """
    body = text.strip()
    if not body:
        return False, [], ["Empty answer."]
    findings = []
    for line in body.splitlines():
        m = SEVERITY_RE.match(line)
        if m:
            loc = LOCATION_RE.search(line, m.end())
            findings.append((m.group(1).upper(), loc.group(0) if loc else ""))
    if findings:
        return True, findings, []
    if len(body) <= 300 and CLEAN_RE.search(body.splitlines()[0]):
        return True, [], []
    return True, [("P?", "")], []


def attempts(rd, who):
    found = []
    for path in rd.glob(f"{who}.a*.meta.json"):
        m = re.search(r"\.a(\d+)\.meta\.json$", path.name)
        if m:
            found.append((int(m.group(1)), read_json(path)))
    return [meta for _, meta in sorted(found, key=lambda item: item[0])]


def reviewer_status(rd, who):
    done = attempts(rd, who)
    if done:
        return done[-1]["status"]
    if who in MODELS and list(rd.glob(f"{who}.a*.pid")):
        return "running"
    return "not started"


def next_attempt_allowed(rd, who):
    done = attempts(rd, who)
    if done and done[-1]["status"] == "ok":
        die(f"{who} already has a valid result for this round.")
    if done and done[-1]["status"] in ("timeout", "stalled"):
        die(f"{who} hung ({done[-1]['status']}). Do not rerun with the same wait: stop and report.", 4)
    if len(done) >= MAX_ATTEMPTS:
        die(f"{who} failed {len(done)} times. Rerun limit reached: stop and report.", 4)
    return len(done) + 1


def finish_attempt(rd, who, n, status, final_path, extra):
    text = final_path.read_text() if final_path.exists() else ""
    ok, findings, problems = parse_final(text)
    if status is None:
        status = "ok" if ok else "malformed"
    meta = {"reviewer": who, "attempt": n, "status": status,
            "findings": len(findings), "problems": problems[:5], **extra}
    write_json(rd / f"{who}.a{n}.meta.json", meta)
    if status == "ok":
        shutil.copyfile(final_path, rd / f"{who}.final.md")
    print(f"{who} attempt {n}: {status}; {len(findings)} finding(s).")
    for problem in problems[:5]:
        print(f"  {problem}")
    sys.exit(0 if status == "ok" else 1)


# ---------- commands ----------

def find_base(top):
    base = git(top, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD", check=False)
    if base:
        return base
    for name in ("origin/main", "origin/master", "main", "master"):
        if git(top, "rev-parse", "--verify", "-q", name, check=False):
            return name
    die("Could not find the base branch. Pass --base.")


def parse_spec(text):
    """Read typed options like "terra:max:fast opus:xhigh rounds:26". Fast only when :fast is given."""
    reviewers, effort, fast, rounds = [], {}, [], HARD_STOP
    for tok in re.split(r"[\s,]+", (text or "").strip().rstrip(".").lower()):
        if not tok:
            continue
        name, *parts = tok.split(":")
        if name in ("pick", "picker"):
            raise ValueError(f"{tok}: Use it alone to open the picker, like /multimodel-review pick.")
        if name == "rounds":
            if len(parts) != 1 or not parts[0].isdigit() or not 1 <= int(parts[0]) <= HARD_STOP:
                raise ValueError(f"{tok}: Use rounds:<1-{HARD_STOP}>, like rounds:12.")
            rounds = int(parts[0])
            continue
        who = "opus" if name == "claude" else name
        if who not in ALL_REVIEWERS:
            raise ValueError(f"{tok}: Unknown reviewer. Use {', '.join(ALL_REVIEWERS)}.")
        if who in reviewers:
            raise ValueError(f"{tok}: {name} is listed twice.")
        reviewers.append(who)
        effort[who] = EFFORT if who in MODELS else CLAUDE_EFFORT
        given = False
        for part in parts:
            if part == "fast" and who not in MODELS:
                raise ValueError(f"{tok}: Fast mode is for Codex models only.")
            if part == "fast" and who not in fast:
                fast.append(who)
            elif part in EFFORTS and not given:
                effort[who], given = part, True
            else:
                raise ValueError(f"{tok}: Unknown or repeated option '{part}'. "
                                 f"Effort is {', '.join(EFFORTS)}; Codex models also take fast.")
    if not reviewers:
        reviewers, effort, fast, _ = parse_spec(DEFAULT_SPEC)
    return reviewers, effort, fast, rounds


def spec_text(reviewers, effort, fast, rounds):
    return " ".join(r + f":{effort[r]}" + (":fast" if r in fast else "")
                    for r in reviewers) + f" rounds:{rounds}"


def cmd_options(a):
    try:
        reviewers, effort, fast, rounds = parse_spec(a.spec)
    except ValueError as e:
        die(str(e))
    print(settings_line({"reviewers": reviewers, "effort": effort, "fast": fast, "max_rounds": rounds}))
    print(f'Init: --spec "{spec_text(reviewers, effort, fast, rounds)}"')


def picker_url(js):
    """The CDN address of picker.js at its last pushed commit, or why it has to be sent inline."""
    def git(*args):
        r = subprocess.run(["git", "-C", str(SKILL_DIR), *args], capture_output=True)
        return r.stdout if r.returncode == 0 else b""

    sha = git("log", "-1", "--format=%H", "--", PICKER_JS).decode().strip()
    if not sha:
        return None, "picker.js isn't committed"
    if git("show", f"{sha}:./{PICKER_JS}") != js:
        return None, "picker.js has uncommitted changes"
    if not git("branch", "-r", "--contains", sha).strip():
        return None, "the latest picker.js commit isn't pushed"
    m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", git("remote", "get-url", "origin").decode().strip())
    if not m:
        return None, "origin isn't a GitHub repo"
    path = git("rev-parse", "--show-prefix").decode().strip() + PICKER_JS
    return f"https://cdn.jsdelivr.net/gh/{m[1]}/{m[2]}@{sha}/{path}", None


def cmd_picker(a):
    js = (SKILL_DIR / PICKER_JS).read_bytes()
    url, why = (None, "--offline") if a.offline else picker_url(js)
    head = (f'<h2 class="sr-only">{PICKER_TITLE}</h2>\n'
            '<div id="mmr" style="padding:1rem 0;font-size:14px;color:var(--text-secondary)">Loading the picker</div>\n')
    if url:
        sri = "sha384-" + base64.b64encode(hashlib.sha384(js).digest()).decode()
        print(f"Picker: CDN, {url}")
        body = ("<script>(function(){var s=document.createElement('script');"
                f"s.src='{url}';s.integrity='{sri}';s.crossOrigin='anonymous';"
                "s.onerror=function(){var m=document.getElementById('mmr'),b=document.createElement('button');"
                "m.textContent=\"Couldn't load the picker. \";b.textContent='Use offline picker \\u2197';"
                "b.onclick=function(){sendPrompt(\"The picker didn't load. Show the offline picker.\")};"
                "m.appendChild(b)};document.head.appendChild(s)})()</script>")
    else:
        print(f"Picker: Inline, because {why}")
        body = "<script>\n" + js.decode() + "</script>"
    print("Widget code:")
    print(head + body)


def init_flags(a):
    """The older flag form, kept for scripts and tests: --reviewers, --effort, --fast, --max-rounds."""
    reviewers = list(dict.fromkeys("opus" if r.strip() == "claude" else r.strip()
                                   for r in a.reviewers.split(",") if r.strip()))
    if not reviewers or set(reviewers) - set(ALL_REVIEWERS):
        die(f"--reviewers takes a comma-separated list from: {', '.join(ALL_REVIEWERS)}")
    if not 1 <= a.max_rounds <= HARD_STOP:
        die(f"--max-rounds must be 1 to {HARD_STOP}.")
    codex = [r for r in reviewers if r in MODELS]
    if a.fast in ("on", "off"):
        fast = codex if a.fast == "on" else []
    else:
        fast = list(dict.fromkeys(r.strip() for r in a.fast.split(",") if r.strip()))
        if set(fast) - set(codex):
            die(f"--fast takes on, off, or a comma-separated list of this run's Codex reviewers: {', '.join(codex)}")
    effort = {r: EFFORT if r in MODELS else CLAUDE_EFFORT for r in reviewers}
    for item in filter(None, (i.strip() for i in (a.effort or "").split(","))):
        who, _, level = (part.strip() for part in item.partition("="))
        who = "opus" if who == "claude" else who
        if who not in reviewers or level not in EFFORTS:
            die(f"--effort takes reviewer=level pairs for this run's reviewers ({', '.join(reviewers)}), "
                f"with levels {', '.join(EFFORTS)}. Got: {item}")
        effort[who] = level
    return reviewers, effort, fast, a.max_rounds


def cmd_init(a):
    top = repo_root(a.repo)
    root = runs_dir(top)
    current = current_run(top)
    if current and not a.new and not read_json(current / "run.json").get("finished"):
        die("An unfinished run exists. Continue it with `mmr.py status`, or pass --new.")
    if a.spec is not None:
        try:
            reviewers, effort, fast, rounds = parse_spec(a.spec)
        except ValueError as e:
            die(str(e))
    else:
        reviewers, effort, fast, rounds = init_flags(a)
    branch = git(top, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    base = a.base or find_base(top)
    if not git(top, "rev-parse", "--verify", "-q", f"{base}^{{commit}}", check=False):
        die(f"Base {base} does not exist.")
    head = git(top, "rev-parse", "HEAD")
    merge_base = git(top, "merge-base", base, "HEAD")
    left_out = [p.rstrip("/") for p in a.leave_out]
    dirty = changed_paths(top, left_out)
    if merge_base == head and not dirty:
        die(f"Nothing to review: No commits ahead of {base} and no uncommitted work.")
    intent = Path(a.intent_file).read_text().strip() if a.intent_file else ""
    if not intent:
        die("Intent is required, so reviewers can tell what counts as wrong behavior.")
    if not shutil.which(CODEX):
        die("Codex CLI not found.")
    flagged = [] if a.allow_flagged else flagged_paths(top, dirty)
    if flagged:
        die("These would go into the WIP snapshot. Nothing changed yet. Use --leave-out <path> "
            "or --allow-flagged:\n" + "\n".join(flagged), 5)
    stamp = first = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 1
    while (root / f"run-{stamp}").exists() or git(
            top, "rev-parse", "--verify", "-q", f"refs/heads/multimodel-review/{stamp}", check=False):
        n += 1  # Two runs started in the same second get distinct names.
        stamp = f"{first}-{n}"
    created = None
    protected = (not branch or branch == base or base.endswith(f"/{branch}")
                 or branch in ("main", "master"))
    if protected:
        created = a.branch or f"multimodel-review/{stamp}"
        git(top, "switch", "-q", "-c", created)
        print(f"Created branch {created} (was on {branch or 'detached HEAD'}).")
        branch = created
    snapshot = None
    if dirty:
        git(top, "add", "-A", "--", ".", *[f":(exclude){p}" for p in left_out])
        message = ["-m", "WIP multimodel-review snapshot"]
        if a.trailer:
            message += ["-m", a.trailer]
        git(top, "commit", "-q", *message, timeout=300)
        snapshot = git(top, "rev-parse", "HEAD")
        print(f"Saved {len(dirty)} uncommitted path(s) as WIP snapshot {snapshot[:12]}.")
    require_clean(top, left_out)
    if left_out:
        print("Left out of the review: " + ", ".join(left_out))
    name = "run-" + stamp
    run = root / name
    (run / "rounds").mkdir(parents=True)
    (run / "intent.md").write_text(intent + "\n")
    (run / "findings.tsv").write_text("\t".join(FINDING_COLS) + "\n")
    (run / "rounds.tsv").write_text("\t".join(ROUND_COLS) + "\n")
    save(run, {
        "repo": str(top), "branch": branch, "base": base, "base_commit": merge_base,
        "reviewers": reviewers, "fast": fast, "effort": effort, "max_rounds": rounds, "caps": CAPS,
        "start_head": head, "started_at": now(), "rounds": 0,
        "created_branch": created, "snapshot_commit": snapshot, "left_out": left_out,
        "phase_rounds": {"code": 0, "docs": 0, "final": 0, "evidence": 0},
        "phases": {"code": "pending", "docs": "pending", "final": "pending"},
        "open_round": None, "last_reviewed": None, "stopped": None, "finished": False,
    })
    (root / "latest").write_text(name + "\n")
    print(f"Run: {run}\nBranch: {branch}\nBase: {base} at {merge_base[:12]}\n"
          f"{settings_line(read_json(run / 'run.json'))}\nNext: mmr.py next --phase code")


def read_tsv(path):
    lines = Path(path).read_text().splitlines()
    if not lines:
        return []
    cols = lines[0].split("\t")
    return [dict(zip(cols, ln.split("\t"))) for ln in lines[1:] if ln.strip()]


def scope_text(phase, scope, sev_label):
    if scope == "full":
        return f"Review the full diff. Report {sev_label} only."
    return (f"Review only the delta plus the fix list. Report {sev_label} only. You may open any file "
            "for context. Outside the delta, report only P0, unless the delta causes the failure: "
            "then anchor the finding to the delta line and give it its real severity.")


def cmd_next(a):
    run, s = load(a.repo)
    top = Path(s["repo"])
    phase = a.phase
    if s["stopped"]:
        die(f"Run stopped: {s['stopped']}", 3)
    if s["open_round"]:
        die(f"Round {s['open_round']} is still open. Close it first.")
    phases = s["phases"]
    if phase == "code" and phases["code"] not in ("pending", "open", "reopened"):
        die("Code phase is already finished.")
    if phase == "docs":
        if phases["code"] != "converged":
            die("Code must converge before Docs.")
        if not (run / "cleanup.md").exists():
            die(f"Finish Cleanup first, then write {run / 'cleanup.md'}.")
        if phases["docs"] not in ("pending", "open"):
            die("Docs phase is already finished.")
    if phase == "final":
        if phases["code"] != "converged" or phases["docs"] not in ("converged", "ended"):
            die("Code and Docs must finish before Final.")
        if phases["final"] == "converged":
            die("Final phase is already finished.")
    if phase == "evidence":
        if phases["code"] != "converged":
            die("Evidence rounds run only after Code converges.")
        if not a.note:
            die("Pass --note with what the run showed and what changed.")
    if s["rounds"] >= max_rounds(s):
        die(f"Hard stop: {max_rounds(s)} rounds used. Stop and report.", 3)
    if phase in caps(s) and s["phase_rounds"][phase] >= caps(s)[phase]:
        die(f"{phase.capitalize()} cap reached ({caps(s)[phase]} rounds). Stop and report.", 3)
    require_clean(top, s.get("left_out", []))
    head = git(top, "rev-parse", "HEAD")
    n = s["rounds"] + 1
    pr = s["phase_rounds"][phase] + 1
    if phase == "code":
        scope = "full" if pr <= 2 and phases["code"] != "reopened" else "delta"
    elif phase in ("docs", "final"):
        scope = "full" if pr == 1 else "delta"
    else:
        scope = "delta"
    start = s["base_commit"] if scope == "full" else s["last_reviewed"]
    if start == head:
        die("Empty delta: nothing changed since the last reviewed commit.")
    rd = round_dir(run, n)
    rd.mkdir(parents=True)
    diff = subprocess.run(["git", "-C", str(top), "diff", f"{start}..{head}"], capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL).stdout
    (rd / "diff.patch").write_text(diff)
    findings = read_tsv(run / "findings.tsv")
    fixes = [f"- {f['id']} {f['loc']}: {f['note']}" for f in findings
             if f["status"] == "accepted" and int(f["round"]) == n - 1]
    ledger = [f"- {f['id']} {f['loc']}: {f['note']}" for f in findings if f["status"] == "rejected"]
    sev_label = "/".join(sorted(ALLOWED[phase]))
    cap = f" of {caps(s)[phase]}" if phase in caps(s) else ""
    parts = [
        "# Multi-model review round", "",
        f"Phase: {phase.capitalize()}",
        f"Round: {n} (phase round {pr}{cap}; hard stop {max_rounds(s)})",
        f"Base commit: {s['base_commit']}",
        f"Reviewed commit: {head}",
        f"Review range: {scope.capitalize()}: git diff {start}..{head}",
        f"Diff file: {rd / 'diff.patch'}", "",
        "## Intent", "", (run / "intent.md").read_text().strip(), "",
        "## This round", "", scope_text(phase, scope, sev_label), "",
    ]
    if a.note:
        parts += ["## New evidence", "", a.note, ""]
    parts += [
        RULES_FILE.read_text().strip(), "",
        "## Fixes since the last round", "", "\n".join(fixes) or "None.", "",
        "## Rejection ledger", "", "\n".join(ledger) or "None.", "",
    ]
    (rd / "prompt.md").write_text("\n".join(parts))
    write_json(rd / "round.json", {"round": n, "phase": phase, "phase_round": pr, "scope": scope,
                                   "commit": head, "range_start": start, "started_at": now()})
    s["rounds"] = n
    s["phase_rounds"][phase] = pr
    s["open_round"] = n
    if phase in phases and phases[phase] == "pending":
        phases[phase] = "open"
    save(run, s)
    print(f"Round {n}: {phase} phase round {pr}, {scope} range {start[:12]}..{head[:12]}")
    print(f"Prompt: {rd / 'prompt.md'}")
    print(settings_line(s))
    for who in panel(s):
        if who in AGENT_MODEL:
            print(f"Claude Agent {who}: model {AGENT_MODEL[who]}, effort {effort_for(s, who)}, "
                  f"answer file {rd / (who + '.answer.md')}")
    old_location_note(repo_root(a.repo), run)


def cmd_codex(a):
    run, s = load(a.repo)
    n = s["open_round"] or die("No open round.")
    who = a.reviewer
    if who not in panel(s):
        die(f"{who} isn't a reviewer on this run.")
    rd = round_dir(run, n)
    attempt = next_attempt_allowed(rd, who)
    log = rd / f"{who}.a{attempt}.jsonl"
    err = rd / f"{who}.a{attempt}.err"
    final = rd / f"{who}.a{attempt}.final.md"
    (rd / f"{who}.a{attempt}.pid").write_text(str(os.getpid()))
    cmd = [CODEX, "exec", "--ignore-user-config", "--json",
           "--disable", "apps", "--disable", "plugins", "--disable", "multi_agent",
           "-m", MODELS[who],
           "-c", f'model_reasoning_effort="{effort_for(s, who)}"',
           *(["-c", 'service_tier="priority"']
             if is_fast(s, who) and not getattr(a, "standard_tier", False) else []),
           "-c", 'web_search="live"',
           "-c", 'model_reasoning_summary="auto"',
           "-s", "read-only", "-C", s["repo"], "-o", str(final), "-"]
    began = time.monotonic()
    status = None
    with open(rd / "prompt.md") as fin, open(log, "w") as out, open(err, "w") as errf:
        proc = subprocess.Popen(cmd, stdin=fin, stdout=out, stderr=errf, start_new_session=True)

        def stop_child(*_):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGTERM, stop_child)
        # Monotonic time, so a sleeping laptop doesn't look like a silent reviewer.
        last_size, last_change = -1, time.monotonic()
        while True:
            try:
                rc = proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                size = log.stat().st_size + err.stat().st_size
                if size != last_size:
                    last_size, last_change = size, time.monotonic()
                if time.monotonic() - began >= CAP_SECONDS:
                    status = "timeout"
                elif time.monotonic() - last_change >= SILENCE_SECONDS:
                    status = "stalled"
                if status:
                    stop_child()
                    rc = proc.wait()
                    break
    if status is None and rc != 0 and not (final.exists() and final.read_text().strip()):
        status = "crash"
    thread_id = ""
    if log.exists():
        with open(log) as fh:
            first = fh.readline()
        try:
            thread_id = json.loads(first).get("thread_id", "")
        except ValueError:
            pass
    finish_attempt(rd, who, attempt, status, final, {
        "model": MODELS[who], "effort": effort_for(s, who), "exit_code": rc, "thread_id": thread_id,
        "seconds": round(time.monotonic() - began, 1), "ended_at": now(),
    })


def pid_alive(pid):
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                       timeout=10, stdin=subprocess.DEVNULL)
    stat = r.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def cmd_monitor(a):
    """One line per finish, failure or quiet stretch. Finished means the PID exited."""
    run, s = load(a.repo)
    n = s["open_round"] or die("No open round.")
    who = a.reviewer
    rd = round_dir(run, n)
    deadline = time.time() + 30
    pid_file = None
    while time.time() < deadline:
        pids = sorted(rd.glob(f"{who}.a*.pid"), key=lambda p: p.stat().st_mtime)
        if pids:
            pid_file = pids[-1]
            break
        time.sleep(1)
    if not pid_file:
        print(f"FAILED {who}: Runner did not start within 30 seconds.", flush=True)
        sys.exit(1)
    attempt = re.search(r"\.a(\d+)\.pid$", pid_file.name).group(1)
    pid = int(pid_file.read_text())
    log = rd / f"{who}.a{attempt}.jsonl"
    err = rd / f"{who}.a{attempt}.err"
    meta_path = rd / f"{who}.a{attempt}.meta.json"
    monitor_log = rd / f"{who}.a{attempt}.monitor.log"
    started = time.time()
    last_size, last_change, warned = -1, time.time(), False
    time.sleep(min(5, TICK_SECONDS))
    while True:
        alive = pid_alive(pid)
        size = sum(p.stat().st_size for p in (log, err) if p.exists())
        if size != last_size:
            last_size, last_change, warned = size, time.time(), False
        quiet = time.time() - last_change
        with open(monitor_log, "a") as fh:
            fh.write(f"{now()} alive={alive} bytes={size} quiet={int(quiet)}s "
                     f"elapsed={int(time.time() - started)}s\n")
        if not alive:
            if not meta_path.exists():
                print(f"FAILED {who}: Runner exited without a result.", flush=True)
                sys.exit(1)
            meta = read_json(meta_path)
            word = "FINISHED" if meta["status"] == "ok" else "FAILED"
            print(f"{word} {who}: {meta['status']}, {meta['findings']} finding(s), "
                  f"{meta.get('seconds', 0) / 60:.1f} min.", flush=True)
            sys.exit(0 if meta["status"] == "ok" else 1)
        if quiet >= QUIET_SECONDS and not warned:
            print(f"QUIET {who}: No output for {int(quiet / 60)} min; process alive. "
                  f"Normal at max effort; it stops at {SILENCE_SECONDS // 60} silent minutes.", flush=True)
            warned = True
        if time.time() - started >= MONITOR_CAP_SECONDS:
            print(f"RE-ARM {who}: Still running at the monitor cap; start the monitor again.", flush=True)
            sys.exit(0)
        time.sleep(TICK_SECONDS)


def cmd_claude_result(a):
    run, s = load(a.repo)
    n = s["open_round"] or die("No open round.")
    rd = round_dir(run, n)
    claude = [r for r in panel(s) if r in AGENT_MODEL]
    if not claude:
        die("This run has no Claude reviewer.")
    who = a.reviewer or (claude[0] if len(claude) == 1 else
                         die(f"Name the reviewer with --reviewer: {', '.join(claude)}"))
    if who not in claude:
        die(f"{who} isn't a Claude reviewer on this run. Use {', '.join(claude)}.")
    attempt = next_attempt_allowed(rd, who)
    answer = Path(a.file) if a.file else rd / f"{who}.answer.md"
    if not answer.exists():
        die(f"Answer file not found: {answer}")
    final = rd / f"{who}.a{attempt}.final.md"
    shutil.copyfile(answer, final)
    finish_attempt(rd, who, attempt, None, final,
                   {"model": AGENT_MODEL[who], "effort": effort_for(s, who), "seconds": a.seconds, "ended_at": now()})


def cmd_reread(a):
    """Parse the last malformed answer again after a parser fix. Runs no reviewer."""
    run, s = load(a.repo)
    n = s["open_round"] or die("No open round.")
    who = a.reviewer
    rd = round_dir(run, n)
    done = attempts(rd, who)
    if not done or done[-1]["status"] != "malformed":
        die(f"{who} has no malformed answer to re-read.")
    last = done[-1]
    kept = {k: v for k, v in last.items()
            if k not in ("reviewer", "attempt", "status", "findings", "problems")}
    finish_attempt(rd, who, last["attempt"], None, rd / f"{who}.a{last['attempt']}.final.md",
                   {**kept, "reread_at": now()})


def cmd_barrier(a):
    run, s = load(a.repo)
    n = s["open_round"] or die("No open round.")
    rd = round_dir(run, n)
    statuses = {who: reviewer_status(rd, who) for who in panel(s)}
    for who, status in statuses.items():
        print(f"{who}: {status}")
    if all(status == "ok" for status in statuses.values()):
        print("Barrier passed: triage may start.")
        return
    print("Barrier not passed: edit nothing yet.")
    sys.exit(1)


def read_triage(rd, raised):
    path = rd / "triage.tsv"
    if not path.exists():
        if any(raised.values()):
            die(f"Findings exist: write {path} first.")
        return []
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    if not lines or lines[0].split("\t") != TRIAGE_HEADER:
        die("triage.tsv must start with this tab-separated header: " + " ".join(TRIAGE_HEADER))
    rows = []
    for ln in lines[1:]:
        cells = ln.split("\t")
        if len(cells) != len(TRIAGE_HEADER):
            die(f"Bad triage row (need {len(TRIAGE_HEADER)} tab-separated cells): {ln[:120]}")
        row = dict(zip(TRIAGE_HEADER, (c.strip() for c in cells)))
        row["reviewers"] = [r for r in row["reviewers"].split(",") if r]
        if not row["reviewers"] or set(row["reviewers"]) - set(raised):
            die(f"Bad reviewers: {ln[:120]}")
        if row["sev"] not in ("P0", "P1", "P2"):
            die(f"Bad severity: {ln[:120]}")
        if row["status"] not in ("accepted", "rejected", "open"):
            die(f"Bad status: {ln[:120]}")
        if not row["summary"] or not row["note"]:
            die(f"Summary and note are required: {ln[:120]}")
        rows.append(row)
    for who, count in raised.items():
        listed = sum(who in row["reviewers"] for row in rows)
        if count and not listed:
            die(f"{who} raised {count} finding(s) that are missing from triage.tsv.")
    return rows


def cmd_close(a):
    run, s = load(a.repo)
    n = s["open_round"] or die("No open round.")
    top = Path(s["repo"])
    rd = round_dir(run, n)
    for who in panel(s):
        if reviewer_status(rd, who) != "ok":
            die("Barrier not passed. Run `mmr.py barrier`.")
    meta = read_json(rd / "round.json")
    phase = meta["phase"]
    raised = {who: len(parse_final((rd / f"{who}.final.md").read_text())[1]) for who in panel(s)}
    rows = read_triage(rd, raised)
    for row in rows:
        if row["status"] != "rejected" and row["sev"] not in ALLOWED[phase]:
            die(f"{row['sev']} is outside the {phase} phase: reject it as out of phase.")
    accepted = [r for r in rows if r["status"] == "accepted"]
    rejected = [r for r in rows if r["status"] == "rejected"]
    held = [r for r in rows if r["status"] == "open"]
    hard_last = n >= max_rounds(s)
    phase_last = phase in caps(s) and meta["phase_round"] >= caps(s)[phase]
    last = hard_last or phase_last
    if accepted and last:
        die("Last allowed round: a fix here would go unreviewed. Mark these findings open.")
    if held and not last and phase != "evidence":
        die("Status open is only for the last allowed round. Accept or reject instead.")
    head = git(top, "rev-parse", "HEAD")
    if accepted:
        require_clean(top, s.get("left_out", []))
        if head == meta["commit"]:
            die("Accepted findings need a WIP commit before close.")
        if not a.tests:
            die("Accepted findings need --tests with the passing test command, or 'none: docs only'.")
    elif head != meta["commit"]:
        die("HEAD moved, but no finding was accepted.")
    with open(run / "findings.tsv", "a") as fh:
        for i, row in enumerate(rows, 1):
            values = [f"R{n}-{i}", str(n), phase, ",".join(row["reviewers"]), row["sev"],
                      row["loc"], row["status"], row["summary"], row["note"]]
            fh.write("\t".join(values) + "\n")
    clean = not accepted and not held
    with open(run / "rounds.tsv", "a") as fh:
        values = [n, phase, meta["phase_round"], meta["scope"], meta["commit"], head,
                  "yes" if clean else "no", len(accepted), len(rejected), len(held), a.tests or ""]
        fh.write("\t".join(str(v) for v in values) + "\n")
    s["open_round"] = None
    s["last_reviewed"] = meta["commit"]
    phases = s["phases"]
    message = f"Next: mmr.py next --phase {phase}"
    if phase == "code":
        if clean:
            phases["code"] = "converged"
            if (run / "cleanup.md").exists():
                message = "Code converged. Next: mmr.py next --phase final"
            else:
                message = f"Code converged. Next: Cleanup, then write {run / 'cleanup.md'}"
        elif held:
            s["stopped"] = "Code cap reached with open findings."
    elif phase == "docs":
        if clean or last:
            phases["docs"] = "converged" if clean else "ended"
            message = "Docs finished. Next: mmr.py next --phase final"
    elif phase == "final":
        if clean:
            phases["final"] = "converged"
            message = "Final converged. Next: Done steps."
        elif held:
            s["stopped"] = "Final delta round still has open findings."
    elif phase == "evidence":
        if any(r["sev"] in ("P0", "P1") for r in accepted):
            phases["code"] = "reopened"
            phases["final"] = "pending"
            message = "P0/P1 accepted. Code reopened. Next: mmr.py next --phase code"
        else:
            message = "Evidence round closed. Code stays closed."
    if hard_last and not clean and not s["stopped"]:
        s["stopped"] = f"Hard stop at {max_rounds(s)} rounds."
    if s["stopped"]:
        message = f"STOP: {s['stopped']} Next: Done steps."
    save(run, s)
    print(f"Round {n} closed: {'clean' if clean else 'not clean'}; "
          f"{len(accepted)} accepted, {len(rejected)} rejected, {len(held)} open.\n{message}")


def cmd_status(a):
    run, s = load(a.repo)
    print(f"Run: {run}")
    old_location_note(repo_root(a.repo), run)
    print(f"Branch: {s['branch']}  Base: {s['base']} at {s['base_commit'][:12]}")
    print(settings_line(s))
    print(f"Rounds used: {s['rounds']} of {max_rounds(s)}  By phase: {s['phase_rounds']}")
    print(f"Phases: {s['phases']}")
    if s["stopped"]:
        print(f"Stopped: {s['stopped']}")
    if s["open_round"]:
        rd = round_dir(run, s["open_round"])
        print(f"Open round: {s['open_round']} ({read_json(rd / 'round.json')['phase']})")
        for who in panel(s):
            print(f"  {who}: {reviewer_status(rd, who)}")


def cmd_migrate(a):
    """Move the latest run out of the git dir, once. It copies first, so a failure
    at any step leaves a working run."""
    top = repo_root(a.repo)
    src, root = current_run(top), runs_dir(top)
    if not src:
        die("No run found.")
    if src.parent == root:
        print(f"Already in the new location: {src}")
        return
    busy = [p.name for p in src.glob("rounds/*/*.pid")
            if not p.with_name(p.name[:-len(".pid")] + ".meta.json").exists()
            and pid_alive(int(p.read_text()))]
    if busy:
        die("A reviewer is still running, so nothing moved: " + ", ".join(busy))
    dst = root / src.name
    if not dst.exists():
        tmp = root / (src.name + ".moving")
        root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp, symlinks=True)
        for prompt in tmp.glob("rounds/*/prompt.md"):
            prompt.write_text(prompt.read_text().replace(str(src), str(dst)))
        os.rename(tmp, dst)
    (root / "latest").write_text(dst.name + "\n")
    shutil.rmtree(src)
    (src.parent / "latest").unlink()
    print(f"Moved run to: {dst}\nUse this path from now on, for triage.tsv too.")


def cmd_stop(a):
    run, s = load(a.repo)
    s["stopped"] = a.reason
    s["open_round"] = None
    save(run, s)
    print(f"Stopped: {a.reason}")


def reviewer_stats(run, s):
    findings = read_tsv(run / "findings.tsv")
    stats = {}
    for who in panel(s):
        st = {"raised": 0, "p01": 0, "p2": 0, "accepted": 0, "rejected": 0, "unique": 0,
              "unique_accepted_p01": 0, "clean_rounds": 0, "minutes": []}
        for n in range(1, s["rounds"] + 1):
            rd = round_dir(run, n)
            final = rd / f"{who}.final.md"
            if final.exists():
                _, found, _ = parse_final(final.read_text())
                st["raised"] += len(found)
                st["p01"] += sum(sev in ("P0", "P1") for sev, _ in found)
                st["p2"] += sum(sev == "P2" for sev, _ in found)
                st["clean_rounds"] += not found
            good = [m for m in attempts(rd, who) if m["status"] == "ok"]
            if good and good[-1].get("seconds"):
                st["minutes"].append(float(good[-1]["seconds"]) / 60)
        for f in findings:
            names = f["reviewers"].split(",")
            if who not in names:
                continue
            st["accepted"] += f["status"] == "accepted"
            st["rejected"] += f["status"] == "rejected"
            st["unique"] += len(names) == 1
            st["unique_accepted_p01"] += (len(names) == 1 and f["status"] == "accepted"
                                          and f["sev"] in ("P0", "P1"))
        st["avg_minutes"] = sum(st["minutes"]) / len(st["minutes"]) if st["minutes"] else 0
        stats[who] = st
    return stats, findings


def cmd_report(a):
    run, s = load(a.repo)
    stats, findings = reviewer_stats(run, s)
    out = ["# Multi-model review report", "",
           f"Rounds: {s['rounds']} total; Code {s['phase_rounds']['code']}, "
           f"Docs {s['phase_rounds']['docs']}, Final {s['phase_rounds']['final']}, "
           f"Evidence {s['phase_rounds']['evidence']}",
           f"Result: {s['stopped'] or 'Converged'}",
           settings_line(s),
           f"Branch: {s['branch']}" + (" (created for this run)" if s.get("created_branch") else ""),
           f"Uncommitted work: {'Saved as WIP snapshot ' + s['snapshot_commit'][:12] if s.get('snapshot_commit') else 'None'}",
           f"Left out: {', '.join(s.get('left_out', [])) or 'Nothing'}", "",
           "| Reviewer | Raised | Accepted | Rejected | Unique | P0-P1 | P2 | Clean rounds | Avg min |",
           "|---|---|---|---|---|---|---|---|---|"]
    for who, st in stats.items():
        out.append(f"| {who} | {st['raised']} | {st['accepted']} | {st['rejected']} | {st['unique']} | "
                   f"{st['p01']} | {st['p2']} | {st['clean_rounds']} | {st['avg_minutes']:.1f} |")
    leader = sorted(stats, key=lambda w: (-stats[w]["unique_accepted_p01"], -stats[w]["accepted"],
                                          stats[w]["rejected"], stats[w]["avg_minutes"]))[0]
    out += ["", f"Leader by rule (accepted unique P0/P1, then accepted, then fewest rejected): {leader}", ""]
    held = [f for f in findings if f["status"] == "open"]
    out += ["## Open items", ""] + ([f"- {f['id']} [{f['sev']}] {f['loc']}: {f['summary']}" for f in held] or ["None."])
    disputed = [f for f in findings if f["status"] == "rejected"
                and (len(f["reviewers"].split(",")) > 1 or f["sev"] == "P0")]
    out += ["", "## Disputed (rejected, but raised by 2+ reviewers or at P0)", ""]
    out += [f"- {f['id']} [{f['sev']}] {f['loc']} ({f['reviewers']}): {f['summary']}. Rejected: {f['note']}"
            for f in disputed] or ["None."]
    tests = sorted({r["tests"] for r in read_tsv(run / "rounds.tsv") if r.get("tests")})
    out += ["", "## Tests run during rounds", ""] + ([f"- {t}" for t in tests] or ["None."])
    if (run / "cleanup.md").exists():
        out += ["", "## Cleanup", "", (run / "cleanup.md").read_text().strip()]
    out += ["", "## Squash (only on the owner's OK)", "",
            f"Folds the WIP commits on top of {s['start_head'][:12]}:",
            f"git reset --soft {s['start_head']}", "git commit"]
    text = "\n".join(out) + "\n"
    (run / "report.md").write_text(text)
    print(text)


def cmd_archive(a):
    run, s = load(a.repo)
    ids = []
    for n in range(1, s["rounds"] + 1):
        for who in MODELS:
            ids += [m["thread_id"] for m in attempts(round_dir(run, n), who) if m.get("thread_id")]
    failed = 0
    for thread_id in ids:
        r = subprocess.run([CODEX, "archive", thread_id], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        if r.returncode:
            failed += 1
            print(f"Failed {thread_id}: {(r.stderr or r.stdout).strip()[:160]}")
    s["finished"] = True
    save(run, s)
    print(f"Archived {len(ids) - failed} of {len(ids)} Codex chats.")
    sys.exit(1 if failed else 0)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=".")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("init")
    c.add_argument("--base")
    c.add_argument("--intent-file")
    c.add_argument("--new", action="store_true")
    c.add_argument("--branch")
    c.add_argument("--trailer")
    c.add_argument("--leave-out", action="append", default=[])
    c.add_argument("--allow-flagged", action="store_true")
    c.add_argument("--fast", default="on",
                   help="Codex priority (fast) tier: on, off, or the Codex reviewers that run fast (terra,luna)")
    c.add_argument("--effort", help="Per-reviewer effort, such as terra=max,opus=high")
    c.add_argument("--reviewers", default=",".join(REVIEWERS))
    c.add_argument("--max-rounds", type=int, default=HARD_STOP)
    c.add_argument("--spec", help='Typed options, such as "terra:max:fast opus:xhigh rounds:26"; replaces the flags above')
    c.set_defaults(fn=cmd_init)
    c = sub.add_parser("options")
    c.add_argument("spec", nargs="?", default="")
    c.set_defaults(fn=cmd_options)
    c = sub.add_parser("picker")
    c.add_argument("--offline", action="store_true", help="Send the picker inline instead of loading it from the CDN")
    c.set_defaults(fn=cmd_picker)
    c = sub.add_parser("next")
    c.add_argument("--phase", required=True, choices=["code", "docs", "final", "evidence"])
    c.add_argument("--note")
    c.set_defaults(fn=cmd_next)
    for name, fn in (("codex", cmd_codex), ("monitor", cmd_monitor)):
        c = sub.add_parser(name)
        c.add_argument("--reviewer", required=True, choices=list(MODELS))
        if name == "codex":
            c.add_argument("--standard-tier", action="store_true", help="Leave out the priority (fast) service tier")
        c.set_defaults(fn=fn)
    c = sub.add_parser("claude-result")
    c.add_argument("--reviewer", choices=list(AGENT_MODEL), help="Needed when the run has 2+ Claude reviewers")
    c.add_argument("--seconds", type=float, required=True)
    c.add_argument("--file")
    c.set_defaults(fn=cmd_claude_result)
    c = sub.add_parser("reread")
    c.add_argument("--reviewer", required=True, choices=ALL_REVIEWERS + ["claude"])
    c.set_defaults(fn=cmd_reread)
    c = sub.add_parser("close")
    c.add_argument("--tests")
    c.set_defaults(fn=cmd_close)
    c = sub.add_parser("stop")
    c.add_argument("--reason", required=True)
    c.set_defaults(fn=cmd_stop)
    for name, fn in (("barrier", cmd_barrier), ("status", cmd_status), ("migrate", cmd_migrate),
                     ("report", cmd_report), ("archive", cmd_archive)):
        sub.add_parser(name).set_defaults(fn=fn)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
