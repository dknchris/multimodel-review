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
PICKER_TITLE = "Options for a new multi-model review: Reviewers with effort and fast mode for each, and review length."
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
# Fast mode (priority tier) is off unless asked for: It draws more Codex usage per pass.
DEFAULT_SPEC = "terra:max opus:xhigh"
CODEX = os.environ.get("MMR_CODEX_BIN", "codex")
CAP_SECONDS = int(os.environ.get("MMR_CAP_SECONDS", "3600"))
SILENCE_SECONDS = int(os.environ.get("MMR_SILENCE_SECONDS", "600"))
TICK_SECONDS = int(os.environ.get("MMR_TICK_SECONDS", "60"))
QUIET_SECONDS = int(os.environ.get("MMR_QUIET_SECONDS", "240"))
MONITOR_CAP_SECONDS = int(os.environ.get("MMR_MONITOR_CAP_SECONDS", str(28 * 60)))
MAX_ATTEMPTS = 2
CAPS = {"code": 20, "docs": 2, "final": 2}
SPARE = 2  # Rounds on top of the phase caps, for evidence rounds after a device test.
HARD_STOP = 26  # Default limit: 20 Code + 2 Docs + 2 Final + 2 spare.
CODE_RANGE = (20, 50)  # A run's Code cap, set with code:<n>.
SPARE_RANGE = (2, 6)  # A run's spare rounds, set with spare:<n>.
MAX_LIMIT = CODE_RANGE[1] + CAPS["docs"] + CAPS["final"] + SPARE_RANGE[1]
OLD_CAPS = {"code": 12, "docs": 2, "final": 2}  # Runs saved before the caps were stored keep these.
OLD_HARD_STOP = 18
# A full-scope diff over SLICE_OVER bytes is split by folder into slices of at most SLICE_MAX bytes,
# each with its own reviewer pass, plus one cross-file pass. Runs saved without these never split.
# In runs saved with slices.in_slice, a slice pass reports only what its own slice can trigger, to cut
# rejected findings; doubts about other slices go to the cross-file pass.
SLICE_OVER = int(os.environ.get("MMR_SLICE_OVER", str(60 * 1024)))
SLICE_MAX = int(os.environ.get("MMR_SLICE_MAX", str(40 * 1024)))
CROSS = "cross"
# In a split round, Codex reviewers take the slices and the cross-file pass, and each Claude reviewer
# takes one pass over the whole diff: A Claude pass costs about as much on a slice as on the whole
# diff. MMR_CLAUDE_SLICES=1 gives every reviewer every slice again.
WHOLE = "whole"
CLAUDE_SLICES = os.environ.get("MMR_CLAUDE_SLICES") == "1"
# Code sweeps the full diff again after each fix, at most FULL_SWEEPS times; then delta rounds check the fixes.
# Sweep 2 always follows a non-clean sweep 1. After that, another sweep runs only if the last one accepted at
# least SWEEP_MIN_OLD P0/P1 on old code: code an earlier sweep reviewed (seen-in-full) or older than the branch
# (before-branch). Runs saved without "sweep_min_old" sweep again on 1 seen-in-full finding, and runs saved
# without "full_sweeps" keep rounds 1-2 full.
FULL_SWEEPS = 4
SWEEP_MIN_OLD = 2
OLD_CODE = ("seen-in-full", "before-branch")
# From this sweep on, only Codex reviewers sweep when the panel has one: After sweep 1, a Claude pass costs
# about $3.80 and found few bugs alone. Runs saved without "codex_sweeps_from" keep every reviewer.
CODEX_SWEEPS_FROM = 2
# The session's context is most of a review's cost, and the run's state is on disk, so a fresh session can take
# over between rounds. close asks for a hand-off once this many rounds ran since the last one (about 200k tokens
# of context). Runs saved without "handoff_rounds" never ask.
HANDOFF_ROUNDS = 8
# How old a finding's code is, from git blame. Oldest first; "unknown" when blame can't place it.
AGES = ["before-branch", "seen-in-full", "newer"]
ALLOWED = {
    "code": {"P0", "P1"},
    "docs": {"P2"},
    "final": {"P0", "P1"},
    "evidence": {"P0", "P1", "P2"},
}
TRIAGE_HEADER = ["reviewers", "sev", "loc", "status", "summary", "note"]
FINDING_COLS = ["id", "round", "phase", "reviewers", "sev", "loc", "status", "summary", "note", "age", "tag"]
ROUND_COLS = ["round", "phase", "phase_round", "scope", "commit", "fix_commit",
              "clean", "accepted", "rejected", "open", "tests"]
# Reviewers are never held to a format. These only count findings, on a best-effort basis.
SEVERITY_RE = re.compile(r"^\W*(?:\d+[.)]\W*)?(P[012])\b", re.I)
LOCATION_RE = re.compile(r"[\w./()@+~-]+:\d+(?:-\d+)?")
LOC_PARTS_RE = re.compile(r"([\w./()@+~-]+):(\d+)(?:-(\d+))?")
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
    c = caps(s)
    spare = max_rounds(s) - sum(c.values())
    return (f"Settings: Reviewers {', '.join(names)}; round limit {max_rounds(s)} "
            f"(Code {c['code']}, Docs {c['docs']}, Final {c['final']}" + (f", Spare {spare})" if spare >= 0 else ")"))


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


def part_names(rd):
    """A split round's slices plus its cross-file pass, in order; empty for a round in one piece."""
    meta = rd / "round.json"
    return read_json(meta).get("parts", []) if meta.exists() else []


def part_dirs(rd):
    """Where each pass keeps its prompt, diff and reviewer files: the round folder, or one folder per slice."""
    return [rd / name for name in part_names(rd)] or [rd]


def pass_dirs(rd, who):
    """The folders where a reviewer has a pass. A split round assigns its parts to reviewers;
    rounds split before that give every reviewer every part."""
    meta = rd / "round.json"
    assign = read_json(meta).get("assign", {}) if meta.exists() else {}
    return [rd / name for name in assign[who]] if who in assign else part_dirs(rd)


def not_assigned(rd, who, pd):
    die(f"{who} has no pass on {pd.name} in this round. Its passes: {', '.join(p.name for p in pass_dirs(rd, who))}.")


def part_dir(rd, name):
    parts = part_names(rd)
    if not name:
        if parts:
            die(f"This round is split. Name the slice with --slice: {', '.join(parts)}")
        return rd
    if name not in parts:
        die(f"No slice {name} in this round." + (f" Use {', '.join(parts)}." if parts else " It isn't split."))
    return rd / name


def label(who, pd):
    """A reviewer's name, plus the slice when the round is split."""
    return f"{who} {pd.name}" if (pd.parent / "round.json").exists() and pd.name in part_names(pd.parent) else who


# ---------- slices ----------

def nbytes(text):
    return len(text.encode("utf-8", "replace"))


def kb(n):
    return max(1, round(n / 1024))


def diff_files(diff):
    """The diff as (path, text) per file, in diff order."""
    files = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            m = re.match(r'diff --git "?a/(.*?)"? "?b/(.*?)"?\s*$', line)
            files.append([m.group(2) if m else line.split()[-1], [line]])
        elif files:
            files[-1][1].append(line)
    return [(path, "".join(lines)) for path, lines in files]


def split_hunks(text, limit):
    """One file's diff in pieces of at most `limit` bytes, cut between hunks; each piece keeps the file header."""
    lines = text.splitlines(keepends=True)
    first = next((i for i, ln in enumerate(lines) if ln.startswith("@@")), None)
    if first is None:
        return [text]
    head, hunks = "".join(lines[:first]), []
    for ln in lines[first:]:
        if ln.startswith("@@") or not hunks:
            hunks.append("")
        hunks[-1] += ln
    pieces, cur = [], ""
    for hunk in hunks:
        if cur and nbytes(head + cur + hunk) > limit:
            pieces.append(head + cur)
            cur = ""
        cur += hunk
    return pieces + [head + cur]


def plan_slices(diff, limit):
    """Split a diff by folder into slices of at most `limit` bytes. Neighbouring folders share a slice
    when they fit, and a file over the limit is cut between hunks. Returns lists of (path, text)."""
    items = []
    for path, text in diff_files(diff):
        items += [(path, piece) for piece in (split_hunks(text, limit) if nbytes(text) > limit else [text])]
    items.sort(key=lambda item: item[0].split("/"))  # Stable, so a cut file's pieces stay in order.

    def size(group):
        return sum(nbytes(text) for _, text in group)

    def pack(groups):
        out = []
        for g in groups:
            if out and size(out[-1]) + size(g) <= limit:
                out[-1] = out[-1] + g
            else:
                out.append(list(g))
        return out

    def split(group, depth):
        if size(group) <= limit:
            return [group]
        children = {}
        for item in group:
            parts = item[0].split("/")
            key = parts[depth] if depth < len(parts) - 1 else "\0" + item[0]  # A file in this folder.
            children.setdefault(key, []).append(item)
        groups = []
        for key, sub in children.items():
            groups += [[item] for item in sub] if key.startswith("\0") else split(sub, depth + 1)
        return pack(groups)

    return split(items, 0) if items else []


def slice_files(items):
    return list(dict.fromkeys(path for path, _ in items))


def slice_folder(items):
    dirs = {os.path.dirname(p) for p in slice_files(items)}
    common = os.path.commonpath(sorted(dirs)) if dirs else ""
    return common or ", ".join(sorted({d.split("/")[0] or "(top)" for d in dirs})[:3])


def slice_outline(names, slices):
    """Each slice's files with their change counts and hunk headers, for the cross-file pass."""
    out = []
    for name, items in zip(names, slices):
        out += [f"# {name}: {slice_folder(items)}", ""]
        for path, text in items:
            lines = text.splitlines()
            adds = sum(ln.startswith("+") and not ln.startswith("+++") for ln in lines)
            dels = sum(ln.startswith("-") and not ln.startswith("---") for ln in lines)
            out.append(f"{path} (+{adds} -{dels})")
            out += ["  " + ln for ln in lines if ln.startswith(("@@", "new file", "deleted file", "rename ",
                                                                   "similarity", "Binary files"))]
        out.append("")
    return "\n".join(out)


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
    print(f"{label(who, rd)} attempt {n}: {status}; {len(findings)} finding(s).", flush=True)
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
    """Read typed options like "terra:max:fast opus:xhigh code:20 spare:2". Fast only when :fast is given.
    Returns the reviewers, their effort, the fast ones, the phase caps and the round limit."""
    reviewers, effort, fast, size = [], {}, [], {"code": CAPS["code"], "spare": SPARE}
    for tok in re.split(r"[\s,]+", (text or "").strip().rstrip(".").lower()):
        if not tok:
            continue
        name, *parts = tok.split(":")
        if name in ("pick", "picker"):
            raise ValueError(f"{tok}: Use it alone to open the picker, like /multimodel-review pick.")
        if name == "rounds":
            raise ValueError(f"{tok}: Use code:<{CODE_RANGE[0]}-{CODE_RANGE[1]}> and "
                             f"spare:<{SPARE_RANGE[0]}-{SPARE_RANGE[1]}> instead, like code:35 spare:2.")
        if name in size:
            lo, hi = CODE_RANGE if name == "code" else SPARE_RANGE
            if len(parts) != 1 or not parts[0].isdigit() or not lo <= int(parts[0]) <= hi:
                raise ValueError(f"{tok}: Use {name}:<{lo}-{hi}>, like {name}:{lo}.")
            size[name] = int(parts[0])
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
        reviewers, effort, fast, _, _ = parse_spec(DEFAULT_SPEC)
    run_caps = {**CAPS, "code": size["code"]}
    return reviewers, effort, fast, run_caps, sum(run_caps.values()) + size["spare"]


def spec_text(reviewers, effort, fast, run_caps, limit):
    return " ".join(r + f":{effort[r]}" + (":fast" if r in fast else "") for r in reviewers) + \
        f" code:{run_caps['code']} spare:{limit - sum(run_caps.values())}"


def cmd_options(a):
    try:
        reviewers, effort, fast, run_caps, limit = parse_spec(a.spec)
    except ValueError as e:
        die(str(e))
    print(settings_line({"reviewers": reviewers, "effort": effort, "fast": fast,
                         "caps": run_caps, "max_rounds": limit}))
    print(f'Init: --spec "{spec_text(reviewers, effort, fast, run_caps, limit)}"')


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
    if not 1 <= a.max_rounds <= MAX_LIMIT:
        die(f"--max-rounds must be 1 to {MAX_LIMIT}.")
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
    return reviewers, effort, fast, CAPS, a.max_rounds


def cmd_init(a):
    top = repo_root(a.repo)
    root = runs_dir(top)
    current = current_run(top)
    if current and not a.new and not read_json(current / "run.json").get("finished"):
        die("An unfinished run exists. Continue it with `mmr.py status`, or pass --new.")
    if a.spec is not None:
        try:
            reviewers, effort, fast, run_caps, rounds = parse_spec(a.spec)
        except ValueError as e:
            die(str(e))
    else:
        reviewers, effort, fast, run_caps, rounds = init_flags(a)
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
        "reviewers": reviewers, "fast": fast, "effort": effort, "max_rounds": rounds, "caps": run_caps,
        "start_head": head, "started_at": now(), "rounds": 0,
        "created_branch": created, "snapshot_commit": snapshot, "left_out": left_out,
        "slices": {"over": SLICE_OVER, "max": SLICE_MAX, "in_slice": True},
        "full_sweeps": min(FULL_SWEEPS, run_caps["code"]),
        "sweep_min_old": SWEEP_MIN_OLD, "codex_sweeps_from": CODEX_SWEEPS_FROM, "final_check": True,
        "handoff_rounds": HANDOFF_ROUNDS, "handoffs": [],
        "phase_rounds": {"code": 0, "docs": 0, "final": 0, "evidence": 0},
        "phases": {"code": "pending", "docs": "pending", "final": "pending"},
        "open_round": None, "last_reviewed": None, "stopped": None, "finished": False,
    })
    (root / "latest").write_text(name + "\n")
    print(f"Run: {run}\nBranch: {branch}\nBase: {base} at {merge_base[:12]}\n"
          f"{settings_line(read_json(run / 'run.json'))}\n"
          "Hand-off: Unless this review is this session's first task, run mmr.py handoff and offer a fresh "
          "session now (SKILL.md, Hand-off).\nNext: mmr.py next --phase code")


def read_tsv(path):
    lines = Path(path).read_text().splitlines()
    if not lines:
        return []
    cols = lines[0].split("\t")
    return [dict(zip(cols, ln.split("\t"))) for ln in lines[1:] if ln.strip()]


def write_tsv_rows(path, cols, rows):
    """Append rows (dicts) in the file's column order. An older file gets the missing columns first,
    so its old rows read as empty there."""
    lines = Path(path).read_text().splitlines()
    header = lines[0].split("\t") if lines else []
    missing = [c for c in cols if c not in header]
    if missing:
        old = [ln for ln in lines[1:] if ln.strip()]
        header += missing
        tmp = Path(str(path) + ".tmp")
        tmp.write_text("".join(ln + "\n" for ln in ["\t".join(header)] +
                               [ln + "\t" * (len(header) - 1 - ln.count("\t")) for ln in old]))
        os.replace(tmp, path)
    with open(path, "a") as fh:
        for row in rows:
            fh.write("\t".join(str(row.get(c, "")) for c in header) + "\n")


def git_history(repo):
    """Where to read a run's history: its checkout, or the main repo once a worktree is gone."""
    for path in dict.fromkeys((str(repo), str(repo).split("/.claude/worktrees/")[0])):
        if Path(path).is_dir() and subprocess.run(
                ["git", "-C", path, "rev-parse", "--git-dir"], capture_output=True,
                timeout=30, stdin=subprocess.DEVNULL).returncode == 0:
            return path
    return None


class Dater:
    """How old a finding's code is: before-branch (an ancestor of the base), seen-in-full (an ancestor
    of the last full Code sweep's commit, so a full sweep reviewed it) or newer. From
    `git blame --porcelain -L n-1,n+1 <round commit> -- <path>`; the newest line in the window decides."""

    def __init__(self, repo, base):
        self.repo, self.base, self.ancestor, self.trees = git_history(repo), base, {}, {}
        # A finding may give an absolute path in the run's checkout or in the main repo.
        self.roots = [str(r).rstrip("/") + "/" for r in (repo, self.repo) if r]

    def git(self, *args):
        try:
            return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True,
                                  timeout=60, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return None

    def is_ancestor(self, sha, target):
        if (sha, target) not in self.ancestor:
            r = self.git("merge-base", "--is-ancestor", sha, target)
            self.ancestor[(sha, target)] = bool(r) and r.returncode == 0
        return self.ancestor[(sha, target)]

    def path_at(self, commit, path):
        """The path as the commit has it, also when a finding names only its end, like plan.rs."""
        path = path.removeprefix("./")
        for root in self.roots:
            path = path.removeprefix(root)
        r = self.git("cat-file", "-e", f"{commit}:{path}")
        if r and r.returncode == 0:
            return path
        if commit not in self.trees:
            r = self.git("ls-tree", "-r", "--name-only", "-z", commit)
            self.trees[commit] = r.stdout.split("\0") if r and r.returncode == 0 else []
        ends = [f for f in self.trees[commit] if f.endswith("/" + path)]
        return ends[0] if len(ends) == 1 else None

    def blame(self, commit, path, lo, hi):
        r = self.git("blame", "--porcelain", "-L", f"{lo},{hi}", commit, "--", path)
        short = r and r.returncode and re.search(r"has only (\d+) lines?", r.stderr)
        if short:  # The window runs past the end of the file.
            last = int(short.group(1))
            r = self.git("blame", "--porcelain", "-L", f"{min(lo, last)},{last}", commit, "--", path) if last else None
        if not r or r.returncode:
            return []
        return re.findall(r"^([0-9a-f]{40,64}) \d+ \d+", r.stdout, re.M)

    def age(self, loc, commit, sweep):
        m = LOC_PARTS_RE.search(loc or "")
        path = self.repo and m and commit and self.path_at(commit, m.group(1))
        if not path:
            return "unknown"
        first, last = sorted((int(m.group(2)), int(m.group(3) or m.group(2))))
        shas = self.blame(commit, path, max(1, first - 1), min(last, first + 40) + 1)
        if not shas:
            return "unknown"
        return AGES[max(0 if self.is_ancestor(sha, self.base) else
                        1 if sweep and self.is_ancestor(sha, sweep) else 2 for sha in set(shas))]


def sweeps_before(run, n=None):
    """Closed full-scope Code rounds before round n. Docs and Final rounds don't count."""
    return [r for r in read_tsv(run / "rounds.tsv")
            if r["phase"] == "code" and r["scope"] == "full" and (n is None or int(r["round"]) < n)]


def missed(run, n, s=None):
    """Accepted P0/P1 of round n on old code: code an earlier full sweep reviewed, plus code older than
    the branch in runs saved with sweep_min_old."""
    ages = OLD_CODE if s and s.get("sweep_min_old") else ("seen-in-full",)
    return [f for f in read_tsv(run / "findings.tsv") if f["round"] == str(n) and f["status"] == "accepted"
            and f["sev"] in ("P0", "P1") and f.get("age") in ages]


def resting(s, sweep):
    """Claude reviewers that sit out this full Code sweep: From codex_sweeps_from on, when the panel has Codex."""
    start, codex = s.get("codex_sweeps_from"), [w for w in panel(s) if w in MODELS]
    return [w for w in panel(s) if w in AGENT_MODEL] if start and sweep >= start and codex else []


def code_scope(run, s):
    """Full or delta for the next Code round, and why. Runs saved before sweeps keep rounds 1-2 full."""
    if s["phases"]["code"] == "reopened":
        return "delta", ""
    cap = s.get("full_sweeps")
    if cap is None:
        return ("full" if s["phase_rounds"]["code"] < 2 else "delta"), ""
    code = [r for r in read_tsv(run / "rounds.tsv") if r["phase"] == "code"]
    done = len(sweeps_before(run))
    if not code:
        return "full", "Full sweep 1"
    if code[-1]["scope"] != "full":
        return "delta", ""
    if done >= cap:
        return "delta", f"{done} full sweeps is the cap, so delta rounds check the fixes"
    if done == 1:
        return "full", "Full sweep 2, after sweep 1's fixes"
    found, need = len(missed(run, code[-1]["round"], s)), s.get("sweep_min_old")
    if need:
        if found >= need:
            return "full", (f"Full sweep {done + 1}: Sweep {done} found {found} accepted P0/P1 on old code "
                            "(reviewed by an earlier sweep or older than the branch)")
        return "delta", (f"Sweep {done} found {found} accepted P0/P1 on old code, under {need}, "
                         "so delta rounds check the fixes")
    if found:
        return "full", f"Full sweep {done + 1}: Sweep {done} found {found} accepted P0/P1 on code an earlier sweep reviewed"
    return "delta", f"Sweep {done} found nothing new on code an earlier sweep reviewed, so delta rounds check the fixes"


def scope_text(phase, scope, sev_label, sweep=0):
    if scope == "since-sweep":
        return ("Check the code changed since the last full Code sweep: The diff file holds it, and full sweeps "
                "already reviewed the rest of the branch. Review this range plus its effects on callers and on "
                f"other code that uses what it changed. Report {sev_label} only. You may open any file for "
                "context. Outside this range, report only P0, unless this range causes the failure: then anchor "
                "the finding to the changed line and give it its real severity.")
    if scope == "full" and sweep > 1:
        return (f"Review the full diff again: This is full sweep {sweep}. Earlier sweeps reviewed this code and "
                f"still missed bugs, so read every change, not only the fixes. Report {sev_label} only.")
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
    why = ""
    if phase == "code":
        scope, why = code_scope(run, s)
    elif phase in ("docs", "final"):
        scope = "full" if pr == 1 else "delta"
    else:
        scope = "delta"
    start = s["base_commit"] if scope == "full" else s["last_reviewed"]
    if phase == "final" and pr == 1 and s.get("final_check") and sweeps_before(run):
        # Final is a check: The code written since the last full sweep, plus its effects on callers.
        scope, start = "since-sweep", sweeps_before(run)[-1]["commit"]
        if start == head:
            phases["final"] = "converged"
            save(run, s)
            print("Final converged: Nothing changed since the last full sweep, so there is nothing to check. "
                  "Next: Done steps.")
            return
    if start == head:
        die("Empty delta: nothing changed since the last reviewed commit.")
    rd = round_dir(run, n)
    rd.mkdir(parents=True)
    diff = subprocess.run(["git", "-C", str(top), "diff", "--no-color", "--no-ext-diff", "--src-prefix=a/",
                           "--dst-prefix=b/", f"{start}..{head}"], capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL).stdout
    (rd / "diff.patch").write_text(diff)
    findings = read_tsv(run / "findings.tsv")
    fixes = [f"- {f['id']} {f['loc']}: {f['note']}" for f in findings
             if f["status"] == "accepted" and int(f["round"]) == n - 1]
    ledger = [f"- {f['id']} {f['loc']}: {f['note']}" for f in findings if f["status"] == "rejected"]
    sev_label = "/".join(sorted(ALLOWED[phase]))
    cap = f" of {caps(s)[phase]}" if phase in caps(s) else ""

    def prompt(diff_file, this_round, extra=()):
        parts = [
            "# Multi-model review round", "",
            f"Phase: {phase.capitalize()}",
            f"Round: {n} (phase round {pr}{cap}; hard stop {max_rounds(s)})", *extra,
            f"Base commit: {s['base_commit']}",
            f"Reviewed commit: {head}",
            f"Review range: {scope.capitalize()}: git diff {start}..{head}",
            f"Diff file: {diff_file}", "",
            "## Intent", "", (run / "intent.md").read_text().strip(), "",
            "## This round", "", this_round, "",
        ]
        if a.note:
            parts += ["## New evidence", "", a.note, ""]
        parts += [
            RULES_FILE.read_text().strip(), "",
            "## Fixes since the last round", "", "\n".join(fixes) or "None.", "",
            "## Rejection ledger", "", "\n".join(ledger) or "None.", "",
        ]
        return "\n".join(parts)

    sweep = len(sweeps_before(run)) + 1 if phase == "code" and scope == "full" and s.get("full_sweeps") else 0
    again = (f"This is full sweep {sweep}: Earlier sweeps reviewed this code and still missed bugs, so read "
             "every change, not only the fixes. ") if sweep > 1 else ""
    codex = [w for w in panel(s) if w in MODELS]
    claude = [w for w in panel(s) if w in AGENT_MODEL]
    rest = resting(s, sweep)
    split = s.get("slices")
    slices = plan_slices(diff, split["max"]) if scope == "full" and split and nbytes(diff) > split["over"] else []
    names = [f"s{i}" for i in range(1, len(slices) + 1)] if len(slices) > 1 else []
    meta = {"round": n, "phase": phase, "phase_round": pr, "scope": scope,
            "commit": head, "range_start": start, "started_at": now()}
    lines = []
    # Runs saved with slices.in_slice: A slice pass reports only what its own slice can trigger, and leaves
    # doubts about other slices to the cross-file pass.
    in_slice = (f"Report only {sev_label} problems in the text of these files. " if phase == "docs" else
                f"Report only {sev_label} bugs that something in this slice can trigger: an input, call or state "
                "change in these files that leads to the failure, wherever the failure shows. ") + (
        "A doubt that depends on how code in another slice behaves, such as a caller, a contract, a schema or "
        "a config, belongs to the cross-file pass, so leave it out. You may open any file for context.")
    if names:
        size = kb(nbytes(diff))
        for name, items in zip(names, slices):
            pd = rd / name
            pd.mkdir()
            (pd / "diff.patch").write_text("".join(text for _, text in items))
            files = slice_files(items)
            listing = "\n".join(f"- {p} ({kb(sum(nbytes(t) for q, t in items if q == p))} KB)" for p in files)
            (pd / "prompt.md").write_text(prompt(pd / "diff.patch", (
                f"{again}Review the full diff of the files in this slice. The round's diff is {size} KB, so it is split "
                f"by folder into {len(names)} slices, each with its own pass, plus a cross-file pass. "
                + (in_slice if split.get("in_slice") else
                   "Focus on these files, and report a problem elsewhere only if this slice's code causes it. You may "
                   f"open any file for context. Report {sev_label} only.")
                + f"\n\nFiles in this slice:\n{listing}"),
                [f"Slice: {name} of {len(names)}, plus a cross-file pass"]))
            lines.append(f"Slice {name}: {len(files)} file(s), {kb(nbytes((pd / 'diff.patch').read_text()))} KB, "
                         f"{slice_folder(items)}; prompt {pd / 'prompt.md'}")
        pd = rd / CROSS
        pd.mkdir()
        (pd / "diff.patch").write_text(slice_outline(names, slices))
        across = ("A doc or comment in one slice that is false about code in another slice" if phase == "docs" else
                  "A caller and callee changed in different slices, a changed contract, type, schema, config or "
                  "file format that code in another slice relies on, removed or renamed code still used elsewhere, "
                  "and ordering or shared state across modules")
        (pd / "prompt.md").write_text(prompt(pd / "diff.patch", (
            f"Cross-file pass. The round's diff is {size} KB, so it is split by folder into {len(names)} slices, "
            "and other passes review each slice line by line"
            + (", reporting only what their own slice can trigger. They leave you every doubt that depends on "
               "another slice" if split.get("in_slice") else "")
            + f". Look only for problems that cross slices: {across}. "
            "The diff file lists each slice's files with their hunk headers. "
            f"Read the whole diff ({rd / 'diff.patch'}) in parts as needed, and open any file for context. "
            f"Report {sev_label} only."), ["Slice: cross-file pass over every slice"]))
        lines.append(f"Slice {CROSS}: Every file's hunk headers, {kb(nbytes((pd / 'diff.patch').read_text()))} KB; "
                     f"prompt {pd / 'prompt.md'}")
        whole = bool(codex and claude) and not CLAUDE_SLICES and not rest
        if whole:
            pd = rd / WHOLE
            pd.mkdir()
            shutil.copyfile(rd / "diff.patch", pd / "diff.patch")
            (pd / "prompt.md").write_text(prompt(pd / "diff.patch", (
                f"{scope_text(phase, scope, sev_label, sweep)} The round's diff is {size} KB. {' and '.join(codex)} "
                f"review it in {len(names)} slices plus a cross-file pass, and you review the whole diff in one "
                "pass. Read it in parts as needed, and open any file for context."), ["Slice: whole diff, one pass"]))
            lines.append(f"Slice {WHOLE}: The whole diff, {size} KB, for {', '.join(claude)} only; "
                         f"prompt {pd / 'prompt.md'}")
            meta["assign"] = {**{w: names + [CROSS] for w in codex}, **{w: [WHOLE] for w in claude}}
        meta["parts"] = names + [CROSS] + ([WHOLE] if whole else [])
        (rd / "prompt.md").write_text("\n".join([
            "# Multi-model review round (split)", "",
            f"Round {n} is split into {len(names)} slices plus a cross-file pass"
            + (", and Claude reviewers get one pass over the whole diff" if whole else "") + ". Each has its own prompt:", "",
            *[f"- {name}: {rd / name / 'prompt.md'}" for name in meta["parts"]], ""]))
    else:
        (rd / "prompt.md").write_text(prompt(rd / "diff.patch", scope_text(phase, scope, sev_label, sweep)))
    if rest:  # Reviewers with no pass this round; the others keep every part.
        meta["assign"] = {w: [] for w in rest}
    if sweep:
        meta["sweep"] = sweep
    write_json(rd / "round.json", meta)
    s["rounds"] = n
    s["phase_rounds"][phase] = pr
    s["open_round"] = n
    if phase in phases and phases[phase] == "pending":
        phases[phase] = "open"
    save(run, s)
    print(f"Round {n}: {phase} phase round {pr}, {scope} range {start[:12]}..{head[:12]}"
          + (f". {why}." if why else ""))
    if names:
        print(f"Split: The {kb(nbytes(diff))} KB diff is {len(names)} slices of at most {kb(split['max'])} KB, "
              "plus a cross-file pass. " + (
                  f"{', '.join(codex)} review every slice and the cross-file pass; {', '.join(claude)} review the "
                  "whole diff once. All passes run in parallel." if whole else
                  f"{', '.join(codex)} review every slice and the cross-file pass, in parallel." if rest else
                  "Every reviewer reviews every slice and the cross-file pass, in parallel."))
        print("\n".join(lines))
    else:
        print(f"Prompt: {rd / 'prompt.md'}")
    print(settings_line(s))
    for who in panel(s):
        if who in rest:
            print(f"{who}: No pass this round. From full sweep {s['codex_sweeps_from']} on, only "
                  f"{', '.join(codex)} sweep{'s' if len(codex) == 1 else ''}.")
            continue
        mine = [pd.name for pd in pass_dirs(rd, who)] if names else []
        if who in MODELS and names:
            print(f"Codex {who}: `mmr.py codex --reviewer {who}` runs all {len(mine)} passes at once, "
                  f"and `mmr.py monitor --reviewer {who}` watches them all.")
        if who in AGENT_MODEL:
            for name in mine or [None]:
                if name:
                    print(f"Claude Agent {who} {name}: model {AGENT_MODEL[who]}, effort {effort_for(s, who)}, "
                          f"prompt {rd / name / 'prompt.md'}, answer file {rd / name / (who + '.answer.md')}")
                else:
                    print(f"Claude Agent {who}: model {AGENT_MODEL[who]}, effort {effort_for(s, who)}, "
                          f"answer file {rd / (who + '.answer.md')}")
    old_location_note(repo_root(a.repo), run)


def cmd_codex(a):
    run, s = load(a.repo)
    n = s["open_round"] or die("No open round.")
    who = a.reviewer
    if who not in panel(s):
        die(f"{who} isn't a reviewer on this run.")
    if part_names(round_dir(run, n)) and not a.slice:
        codex_all_slices(a, round_dir(run, n), who)
    rd = part_dir(round_dir(run, n), a.slice)
    if a.slice and rd not in pass_dirs(round_dir(run, n), who):
        not_assigned(round_dir(run, n), who, rd)
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


def codex_all_slices(a, rd, who):
    """Run one Codex pass per slice of a split round, all at once. Each pass is a normal runner with
    its own PID, logs and limits; reruns name their slice."""
    todo = [pd.name for pd in pass_dirs(rd, who) if reviewer_status(pd, who) == "not started"]
    if not todo:
        die("Every slice has a run already. Rerun one slice with --slice <name>.")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--repo", a.repo, "codex", "--reviewer", who,
           *(["--standard-tier"] if a.standard_tier else [])]
    procs = [subprocess.Popen(cmd + ["--slice", name], stdin=subprocess.DEVNULL) for name in todo]

    def stop_children(*_):
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()

    signal.signal(signal.SIGTERM, stop_children)
    codes = [proc.wait() for proc in procs]
    print(f"{who}: {sum(c == 0 for c in codes)} of {len(todo)} slice passes ok.")
    sys.exit(0 if not any(codes) else 1)


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
    # A split round's monitor watches every slice unless one is named, one line per event.
    dirs = [part_dir(rd, a.slice)] if a.slice or not part_names(rd) else pass_dirs(rd, who)
    watch, failed = [], False
    deadline = time.time() + 30
    while True:
        found = {}
        for pd in dirs:
            pids = sorted(pd.glob(f"{who}.a*.pid"), key=lambda p: p.stat().st_mtime)
            if pids:
                found[pd] = pids[-1]
        if len(found) == len(dirs) or time.time() >= deadline:
            break
        time.sleep(1)
    for pd in dirs:
        if pd not in found:
            print(f"FAILED {label(who, pd)}: Runner did not start within 30 seconds.", flush=True)
            failed = True
            continue
        attempt = re.search(r"\.a(\d+)\.pid$", found[pd].name).group(1)
        watch.append({"label": label(who, pd), "slice": pd.name if pd != rd else None,
                      "pid": int(found[pd].read_text()),
                      "logs": (pd / f"{who}.a{attempt}.jsonl", pd / f"{who}.a{attempt}.err"),
                      "meta": pd / f"{who}.a{attempt}.meta.json",
                      "monitor_log": pd / f"{who}.a{attempt}.monitor.log",
                      "size": -1, "change": time.time(), "warned": False})
    if not watch:
        sys.exit(1)
    started = time.time()
    time.sleep(min(5, TICK_SECONDS))
    while True:
        for w in list(watch):
            alive = pid_alive(w["pid"])
            size = sum(p.stat().st_size for p in w["logs"] if p.exists())
            if size != w["size"]:
                w["size"], w["change"], w["warned"] = size, time.time(), False
            quiet = time.time() - w["change"]
            with open(w["monitor_log"], "a") as fh:
                fh.write(f"{now()} alive={alive} bytes={size} quiet={int(quiet)}s "
                         f"elapsed={int(time.time() - started)}s\n")
            if not alive:
                watch.remove(w)
                if not w["meta"].exists():
                    print(f"FAILED {w['label']}: Runner exited without a result.", flush=True)
                    failed = True
                    continue
                meta = read_json(w["meta"])
                word = "FINISHED" if meta["status"] == "ok" else "FAILED"
                failed = failed or meta["status"] != "ok"
                print(f"{word} {w['label']}: {meta['status']}, {meta['findings']} finding(s), "
                      f"{meta.get('seconds', 0) / 60:.1f} min.", flush=True)
            elif quiet >= QUIET_SECONDS and not w["warned"]:
                print(f"QUIET {w['label']}: No output for {int(quiet / 60)} min; process alive. "
                      f"Normal at max effort; it stops at {SILENCE_SECONDS // 60} silent minutes.", flush=True)
                w["warned"] = True
        if not watch:
            sys.exit(1 if failed else 0)
        if time.time() - started >= MONITOR_CAP_SECONDS:
            for w in watch:
                again = f" with --slice {w['slice']}" if w["slice"] else ""
                print(f"RE-ARM {w['label']}: Still running at the monitor cap; start the monitor again{again}.",
                      flush=True)
            sys.exit(0)
        time.sleep(TICK_SECONDS)


def cmd_claude_result(a):
    run, s = load(a.repo)
    n = s["open_round"] or die("No open round.")
    claude = [r for r in panel(s) if r in AGENT_MODEL]
    if not claude:
        die("This run has no Claude reviewer.")
    who = a.reviewer or (claude[0] if len(claude) == 1 else
                         die(f"Name the reviewer with --reviewer: {', '.join(claude)}"))
    if who not in claude:
        die(f"{who} isn't a Claude reviewer on this run. Use {', '.join(claude)}.")
    # A reviewer with a single pass in a split round (the whole-diff pass) needs no --slice.
    top_rd, mine = round_dir(run, n), pass_dirs(round_dir(run, n), who)
    if not mine:
        die(f"{who} has no pass in this round: Only Codex reviewers review it.")
    rd = part_dir(top_rd, a.slice or (mine[0].name if part_names(top_rd) and len(mine) == 1 else None))
    if part_names(top_rd) and rd not in mine:
        not_assigned(top_rd, who, rd)
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
    rd = part_dir(round_dir(run, n), a.slice)
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
    statuses = {label(who, pd): reviewer_status(pd, who) for who in panel(s) for pd in pass_dirs(rd, who)}
    for who, status in statuses.items():
        print(f"{who}: {status}")
    if all(status == "ok" for status in statuses.values()):
        if part_names(rd):
            write_answers(rd, panel(s))
            print(f"Answers: {rd / 'answers.md'} has every slice's answers, with findings at nearby lines "
                  "grouped as likely duplicates.")
        print("Barrier passed: triage may start.")
        return
    print("Barrier not passed: edit nothing yet.")
    sys.exit(1)



def write_answers(rd, reviewers):
    """One file with every slice's answers, so triage can merge findings across slices."""
    found, body = [], []
    for pd in part_dirs(rd):
        body += [f"## {pd.name}", ""]
        for who in reviewers:
            if pd not in pass_dirs(rd, who):
                continue
            text = (pd / f"{who}.final.md").read_text().strip()
            body += [f"### {who}", "", text, ""]
            for sev, loc in parse_final(text)[1]:
                m = LOC_PARTS_RE.fullmatch(loc)
                if m:
                    found.append((m.group(1), int(m.group(2)), f"{who} {pd.name} {sev}"))
    groups = []
    for path, line, who in sorted(found, key=lambda f: f[:2]):
        if groups and groups[-1][0] == path and line - groups[-1][2] <= 5:
            groups[-1][2] = line
            groups[-1][3].append(who)
        else:
            groups.append([path, line, line, [who]])
    dups = [f"- {p}:{lo}" + (f"-{hi}" if hi != lo else "") + ": " + ", ".join(names)
            for p, lo, hi, names in groups if len(set(names)) > 1]
    (rd / "answers.md").write_text("\n".join([
        f"# Round {int(rd.name)} answers, every slice", "",
        "Write one triage.tsv row per merged finding, listing every reviewer who raised it.", "",
        "## Likely duplicates (same file, within 5 lines)", "", "\n".join(dups) or "None found.", "",
        *body]))


def merge_duplicates(rows):
    """Rows with the same location and summary are one finding: Keep one row with every reviewer."""
    merged, seen = [], {}
    for row in rows:
        key = (row["loc"].lower(), " ".join(re.findall(r"\w+", row["summary"].lower())))
        first = seen.get(key)
        if not first:
            seen[key] = row
            merged.append(row)
            continue
        if (first["sev"], first["status"]) != (row["sev"], row["status"]):
            die(f"Two triage rows for {row['loc']} have the same summary but a different severity or status. "
                "Merge them into one row.")
        first["reviewers"] += [r for r in row["reviewers"] if r not in first["reviewers"]]
    return merged


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
        if any(reviewer_status(pd, who) != "ok" for pd in pass_dirs(rd, who)):
            die("Barrier not passed. Run `mmr.py barrier`.")
    meta = read_json(rd / "round.json")
    phase = meta["phase"]
    raised = {who: sum(len(parse_final((pd / f"{who}.final.md").read_text())[1]) for pd in pass_dirs(rd, who))
              for who in panel(s) if pass_dirs(rd, who)}
    rows = merge_duplicates(read_triage(rd, raised))
    for row in rows:
        if row["status"] != "rejected" and row["sev"] not in ALLOWED[phase]:
            die(f"{row['sev']} is outside the {phase} phase: reject it as out of phase.")
    sweeps = sweeps_before(run, n)
    dater = Dater(s["repo"], s["base_commit"])
    for row in rows:
        row["age"] = dater.age(row["loc"], meta["commit"], sweeps[-1]["commit"] if sweeps else None)
        # A Final finding on code a full sweep already reviewed is late.
        row["tag"] = "late" if phase == "final" and row["age"] == "seen-in-full" else ""
    listed = []
    if phase == "final" and s.get("final_check"):
        for row in rows:
            if row["tag"] == "late" and row["sev"] == "P1" and row["status"] == "accepted":
                die(f"{row['loc']}: This P1 is late, on code a full sweep already reviewed. Late P1s are listed, "
                    "not looped on: Mark it open (the report lists it) or reject it, and leave its fix out.")
        listed = [r for r in rows if r["tag"] == "late" and r["sev"] == "P1" and r["status"] == "open"]
    accepted = [r for r in rows if r["status"] == "accepted"]
    rejected = [r for r in rows if r["status"] == "rejected"]
    held = [r for r in rows if r["status"] == "open" and all(r is not x for x in listed)]
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
    write_tsv_rows(run / "findings.tsv", FINDING_COLS, [
        {"id": f"R{n}-{i}", "round": n, "phase": phase, "reviewers": ",".join(row["reviewers"]),
         **{k: row[k] for k in ("sev", "loc", "status", "summary", "note", "age", "tag")}}
        for i, row in enumerate(rows, 1)])
    clean = not accepted and not held
    with open(run / "rounds.tsv", "a") as fh:
        values = [n, phase, meta["phase_round"], meta["scope"], meta["commit"], head,
                  "yes" if clean else "no", len(accepted), len(rejected), len(held) + len(listed), a.tests or ""]
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
        elif s.get("full_sweeps"):
            _, why = code_scope(run, s)
            message = (f"{why}. " if why else "") + message
    elif phase == "docs":
        if clean or last:
            phases["docs"] = "converged" if clean else "ended"
            message = "Docs finished. Next: mmr.py next --phase final"
    elif phase == "final":
        if listed:
            message = (f"{len(listed)} late P1(s) listed for the report, not looped on. " + message)
        if clean:
            phases["final"] = "converged"
            message = (f"{len(listed)} late P1(s) listed for the report. " if listed else "") + \
                "Final converged. Next: Done steps."
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
    due = handoff_due(s)
    if due:
        message = (f"Hand-off due: {due} rounds ran since the last hand-off. Run mmr.py handoff and offer a fresh "
                   "session before the next step (SKILL.md, Hand-off). " + message)
    save(run, s)
    ages = [row["age"] for row in rows]
    dated = ", ".join(f"{ages.count(age)} {age}" for age in AGES + ["unknown"] if age in ages)
    print(f"Round {n} closed: {'clean' if clean else 'not clean'}; "
          f"{len(accepted)} accepted, {len(rejected)} rejected, {len(held) + len(listed)} open."
          + (f" Code dated by git blame: {dated}." if dated else "") + f"\n{message}")


def next_step(run, s):
    """What the run needs next, from its saved state, so a fresh or compacted session can pick it up."""
    if s.get("finished"):
        return "Nothing: The run is finished."
    if s["stopped"]:
        return "Done steps."
    if s["open_round"]:
        return (f"Finish round {s['open_round']}: Start any reviewer that hasn't started, wait for the rest, "
                "then mmr.py barrier, triage, fix and close.")
    phases = s["phases"]
    if phases["code"] in ("pending", "open", "reopened"):
        return "mmr.py next --phase code"
    if not (run / "cleanup.md").exists():
        return f"Cleanup, then write {run / 'cleanup.md'}"
    if phases["docs"] in ("pending", "open"):
        return "mmr.py next --phase docs"
    if phases["final"] != "converged":
        return "mmr.py next --phase final"
    return "Done steps."


def handoff_due(s):
    """Rounds since the last hand-off, once they reach the run's handoff_rounds and work is left; else 0."""
    every = s.get("handoff_rounds")
    if not every or s["stopped"] or s.get("finished") or s["phases"]["final"] == "converged":
        return 0
    since = s["rounds"] - max((h["after_round"] for h in s.get("handoffs", [])), default=0)
    return since if since >= every else 0


def cmd_handoff(a):
    """Write a brief that lets a fresh session resume this run, and count the hand-off. Between rounds only."""
    run, s = load(a.repo)
    if s.get("finished"):
        die("The run is finished: Nothing to hand off.")
    if s["open_round"]:
        die(f"Round {s['open_round']} is open. Hand off only between rounds: Its reviewers report to this session.")
    notes = Path(a.notes).read_text().strip() if a.notes else ""
    tests = [r["tests"] for r in read_tsv(run / "rounds.tsv") if r.get("tests")]
    status = f"python3 {Path(__file__).resolve()} --repo {s['repo']} status"
    brief = "\n".join([
        "Continue a multimodel-review run in this fresh session. The owner started it with /multimodel-review "
        "and accepted this hand-off, and its state is on disk, so the skill applies here.", "",
        f"1. Read {SKILL_DIR / 'SKILL.md'} in full and follow it.",
        f"2. Run `{status}` and continue from its Next line. Don't run init or start a new run.",
        f"3. Work in {s['repo']} on branch {s['branch']}, and don't switch branches.",
        f"4. First check that your working folder is {s['repo']}. If it isn't, stop before any reviewer runs: "
        "A hook blocks edits outside your own folder, and a session in its own worktree can't move. Tell the "
        "owner in one line that the session that handed off continues the run.", "",
        f"Run folder: {run}",
        f"Intent: {run / 'intent.md'}",
        settings_line(s),
        f"Rounds used: {s['rounds']} of {max_rounds(s)}. Phases: "
        + ", ".join(f"{k} {v}" for k, v in s["phases"].items()),
        f"Last test command: {tests[-1] if tests else 'None yet'}",
        f"Next: {next_step(run, s)}",
        *(["", "Notes from the last session:", "", notes] if notes else []),
    ])
    (run / "handoff.md").write_text(brief + "\n")
    s.setdefault("handoffs", []).append({"after_round": s["rounds"], "at": now()})
    save(run, s)
    print(f"Brief saved: {run / 'handoff.md'}\n\n{brief}")


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
        parts = part_names(rd)
        print(f"Open round: {s['open_round']} ({read_json(rd / 'round.json')['phase']})"
              + (f", split into {', '.join(parts)}" if parts else ""))
        for who in panel(s):
            if not pass_dirs(rd, who):
                print(f"  {who}: no pass this round")
            for pd in pass_dirs(rd, who):
                print(f"  {label(who, pd)}: {reviewer_status(pd, who)}")
    if (run / "handoff.md").exists():
        print(f"Last hand-off brief: {run / 'handoff.md'}")
    if handoff_due(s) and not s["open_round"]:
        print(f"Hand-off due: {handoff_due(s)} rounds ran since the last hand-off (SKILL.md, Hand-off).")
    print(f"Next: {next_step(run, s)}")


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
    busy = [p.name for p in [*src.glob("rounds/*/*.pid"), *src.glob("rounds/*/*/*.pid")]
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
        for prompt in [*tmp.glob("rounds/*/prompt.md"), *tmp.glob("rounds/*/*/prompt.md")]:
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
            finals = [pd / f"{who}.final.md" for pd in pass_dirs(round_dir(run, n), who)]
            finals = [f for f in finals if f.exists()]
            found = [item for f in finals for item in parse_final(f.read_text())[1]]
            st["raised"] += len(found)
            st["p01"] += sum(sev in ("P0", "P1") for sev, _ in found)
            st["p2"] += sum(sev == "P2" for sev, _ in found)
            st["clean_rounds"] += bool(finals) and not found
            # A split round's passes run in parallel, so its time is its longest pass.
            seconds = [float(good[-1]["seconds"]) for pd in pass_dirs(round_dir(run, n), who)
                       for good in [[m for m in attempts(pd, who) if m["status"] == "ok"]]
                       if good and good[-1].get("seconds")]
            if seconds:
                st["minutes"].append(max(seconds) / 60)
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


def sweep_lines(run, s):
    sweeps = sweeps_before(run)
    if not s.get("full_sweeps") or not sweeps:
        return []
    found = [f"round {r['round']}: {len(missed(run, r['round'], s))}" for r in sweeps[1:]]
    old = "old code" if s.get("sweep_min_old") else "code an earlier sweep reviewed"
    return [f"Full Code sweeps: {len(sweeps)} (rounds {', '.join(r['round'] for r in sweeps)}"
            + (f"). Accepted P0/P1 on {old}: {', '.join(found)}" if found else ")")]


def late_lines(run):
    late = [f for f in read_tsv(run / "findings.tsv") if f.get("tag") == "late" and f["status"] != "rejected"]
    if not late:
        return []
    fixed = sum(f["status"] == "accepted" for f in late)
    return [f"Late Final findings (on code a full sweep already reviewed): {len(late)}; {fixed} fixed, "
            f"{len(late) - fixed} listed"]


def cmd_report(a):
    run, s = load(a.repo)
    stats, findings = reviewer_stats(run, s)
    out = ["# Multi-model review report", "",
           f"Rounds: {s['rounds']} total; Code {s['phase_rounds']['code']}, "
           f"Docs {s['phase_rounds']['docs']}, Final {s['phase_rounds']['final']}, "
           f"Evidence {s['phase_rounds']['evidence']}",
           f"Result: {s['stopped'] or 'Converged'}",
           *sweep_lines(run, s), *late_lines(run),
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
    listed = [f for f in held if f.get("tag") == "late" and f["sev"] == "P1"] if s.get("final_check") else []
    held = [f for f in held if f not in listed]
    out += ["## Open items", ""] + ([f"- {f['id']} [{f['sev']}] {f['loc']}: {f['summary']}" for f in held] or ["None."])
    if listed:
        out += ["", "## Late P1s (listed, not looped on)", ""]
        out += [f"- {f['id']} [{f['sev']}] {f['loc']}: {f['summary']}. {f['note']}" for f in listed]
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
            ids += [m["thread_id"] for pd in part_dirs(round_dir(run, n))
                    for m in attempts(pd, who) if m.get("thread_id")]
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


def cmd_ages(a):
    """Date findings with git blame. Reads only, and writes nothing: With --run, every finding of that
    run folder; otherwise the current run's open round (its triage.tsv, or else the reviewers' answers)
    or, with no open round, all its findings."""
    if a.run:
        run = Path(a.run).expanduser().resolve()
        s = read_json(run / "run.json")
    else:
        run, s = load(a.repo)
    dater = Dater(a.git or s["repo"], s["base_commit"])
    if not dater.repo:
        die(f"No git history at {a.git or s['repo']} or its main repo.")
    print(f"Run: {run}\nHistory: {dater.repo}")
    sweeps = sweeps_before(run)
    print("Full Code sweeps: " + (", ".join(f"round {r['round']} at {r['commit'][:12]}" for r in sweeps) or "None"))

    def last_sweep(n):
        before = [r for r in sweeps if int(r["round"]) < int(n)]
        return before[-1]["commit"] if before else None

    n = s.get("open_round")
    if n and not a.run:
        rd = round_dir(run, n)
        meta = read_json(rd / "round.json")
        print(f"Open round {n} ({meta['phase']}), dated at {meta['commit'][:12]}:")
        if (rd / "triage.tsv").exists():
            for row in read_tsv(rd / "triage.tsv"):
                age = dater.age(row.get("loc", ""), meta["commit"], last_sweep(n))
                late = meta["phase"] == "final" and age == "seen-in-full"
                todo = ("" if not late or not s.get("final_check") else "\tlate P0: Fix it" if row.get("sev") == "P0"
                        else "\tlate P1: List it as open; don't fix it" if row.get("status") != "rejected" else "")
                print(f"{row.get('sev')}\t{row.get('status')}\t{age}{' late' if late else ''}\t{row.get('loc')}"
                      f"\t{row.get('summary')}{todo}")
        else:
            for pd in part_dirs(rd):
                for who in panel(s):
                    final = pd / f"{who}.final.md"
                    for sev, loc in parse_final(final.read_text())[1] if final.exists() else []:
                        age = dater.age(loc, meta["commit"], last_sweep(n))
                        late = " late" if meta["phase"] == "final" and age == "seen-in-full" else ""
                        print(f"{label(who, pd)}\t{sev}\t{age}{late}\t{loc}")
        return
    rounds = {r["round"]: r for r in read_tsv(run / "rounds.tsv")}
    by_round = {}
    print("id\tround\tphase\tsev\tstatus\tage\treviewers\tloc")
    for f in read_tsv(run / "findings.tsv"):
        if f["round"] not in rounds:
            continue
        age = dater.age(f["loc"], rounds[f["round"]]["commit"], last_sweep(f["round"]))
        stored = f.get("age")
        print("\t".join([f["id"], f["round"], f["phase"], f["sev"], f["status"], age, f["reviewers"], f["loc"]])
              + (f"\t(stored: {stored})" if stored and stored != age else ""))
        by_round.setdefault(f["round"], []).append((f, age))
    print("\nBy round:")
    for rn, items in by_round.items():
        r = rounds[rn]
        ages = [age for _, age in items]
        line = ", ".join(f"{ages.count(x)} {x}" for x in AGES + ["unknown"] if x in ages)
        who = [f"{w} {sum(w in f['reviewers'].split(',') for f, _ in items)} "
               f"({sum(w in f['reviewers'].split(',') and age == 'seen-in-full' for f, age in items)} seen-in-full)"
               for w in panel(s) if any(w in f["reviewers"].split(",") for f, _ in items)]
        late = sum(age == "seen-in-full" for _, age in items) if r["phase"] == "final" else 0
        print(f"Round {rn} {r['phase']} ({r['scope']}): {line}" + (f"; {late} late" if late else "")
              + f". By reviewer: {', '.join(who)}.")


def cmd_slices(a):
    """Show how a diff file would be split. Reads only that file."""
    diff = Path(a.diff).read_text()
    size, slices = nbytes(diff), plan_slices(diff, a.max)
    names = [f"s{i}" for i in range(1, len(slices) + 1)]
    split = size > a.over and len(slices) > 1
    print(f"Diff: {kb(size)} KB in {len(diff_files(diff))} file(s). Split (over {kb(a.over)} KB): "
          f"{'Yes' if split else 'No'}")
    for name, items in zip(names, slices):
        print(f"{name}: {kb(sum(nbytes(t) for _, t in items))} KB, {len(slice_files(items))} file(s), "
              f"{slice_folder(items)}")
        if a.files:
            print("\n".join(f"  {p}" for p in slice_files(items)))
    print(f"{CROSS}: {kb(nbytes(slice_outline(names, slices)))} KB of file names and hunk headers")


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
    c.add_argument("--fast", default="off",
                   help="Codex priority (fast) tier: on, off, or the Codex reviewers that run fast (terra,luna)")
    c.add_argument("--effort", help="Per-reviewer effort, such as terra=max,opus=high")
    c.add_argument("--reviewers", default=",".join(REVIEWERS))
    c.add_argument("--max-rounds", type=int, default=HARD_STOP)
    c.add_argument("--spec", help='Typed options, such as "terra:max:fast opus:xhigh code:20 spare:2"; replaces the flags above')
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
    slice_help = "In a split round: the part (s1, s2, ..., cross or whole)"
    for name, fn in (("codex", cmd_codex), ("monitor", cmd_monitor)):
        c = sub.add_parser(name)
        c.add_argument("--reviewer", required=True, choices=list(MODELS))
        c.add_argument("--slice", help=slice_help + "; without it, every slice")
        if name == "codex":
            c.add_argument("--standard-tier", action="store_true", help="Leave out the priority (fast) service tier")
        c.set_defaults(fn=fn)
    c = sub.add_parser("claude-result")
    c.add_argument("--reviewer", choices=list(AGENT_MODEL), help="Needed when the run has 2+ Claude reviewers")
    c.add_argument("--slice", help=slice_help)
    c.add_argument("--seconds", type=float, required=True)
    c.add_argument("--file")
    c.set_defaults(fn=cmd_claude_result)
    c = sub.add_parser("reread")
    c.add_argument("--reviewer", required=True, choices=ALL_REVIEWERS + ["claude"])
    c.add_argument("--slice", help=slice_help)
    c.set_defaults(fn=cmd_reread)
    c = sub.add_parser("ages", help="Date findings with git blame; writes nothing")
    c.add_argument("--run", help="A run folder to read instead of this repo's current run")
    c.add_argument("--git", help="Read history from this repo instead of the run's checkout")
    c.set_defaults(fn=cmd_ages)
    c = sub.add_parser("slices", help="Show how a diff file would be split; starts nothing")
    c.add_argument("--diff", required=True)
    c.add_argument("--over", type=int, default=SLICE_OVER)
    c.add_argument("--max", type=int, default=SLICE_MAX)
    c.add_argument("--files", action="store_true", help="List each slice's files")
    c.set_defaults(fn=cmd_slices)
    c = sub.add_parser("close")
    c.add_argument("--tests")
    c.set_defaults(fn=cmd_close)
    c = sub.add_parser("stop")
    c.add_argument("--reason", required=True)
    c.set_defaults(fn=cmd_stop)
    c = sub.add_parser("handoff", help="Write a brief for a fresh session that resumes the run; between rounds only")
    c.add_argument("--notes", help="A file with what the owner decided in this session that the run folder lacks")
    c.set_defaults(fn=cmd_handoff)
    for name, fn in (("barrier", cmd_barrier), ("status", cmd_status), ("migrate", cmd_migrate),
                     ("report", cmd_report), ("archive", cmd_archive)):
        sub.add_parser(name).set_defaults(fn=fn)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
