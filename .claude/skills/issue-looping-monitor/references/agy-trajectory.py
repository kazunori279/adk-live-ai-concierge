#!/usr/bin/env python3
"""Read-only digest of the Antigravity (`agy`) coder session's trajectory.

Run:  uv run --no-project python .claude/skills/issue-looping-monitor/references/agy-trajectory.py [--no-net]

Standard library only. Opens the conversation DB read-only (mode=ro), never
writes to it, never copies it. Touches the repo only with `git` reads.
Redacts anything key-shaped before printing.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
AGYDIR = os.path.join(HOME, ".gemini", "antigravity-cli")
CONVDIR = os.path.join(AGYDIR, "conversations")
LOGDIR = os.path.join(AGYDIR, "log")
REPO = os.environ.get("SHARED_REPO_PATH") or subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
PROJ = ""
NET = "--no-net" not in sys.argv
NCMD = 8

# ---------------------------------------------------------------- redaction
_RED = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ya29\.[A-Za-z0-9_\-]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN[^\n]*"),
    # long opaque runs: no '/' (so filesystem paths survive), must mix case+digit
    re.compile(r"(?<![A-Za-z0-9+=_-])(?=[^\s]*[0-9])(?=[^\s]*[A-Z])"
               r"[A-Za-z0-9+_-]{32,}={0,2}(?![A-Za-z0-9+=_-])"),
]


def red(s):
    for p in _RED:
        s = p.sub("<redacted>", s)
    return s


# ---------------------------------------------------------------- discovery
def sh(cmd, timeout=10):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def find_proc():
    out = sh("ps -eo pid,lstart,etime,command")
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        cmd = parts[7]
        if cmd.startswith("agy ") or cmd == "agy":
            started = " ".join(parts[1:6])
            return int(parts[0]), started, parts[6], cmd
    return None, None, None, None


def pid_log(pid):
    """The cli-*.log whose header names this pid."""
    if not pid or not os.path.isdir(LOGDIR):
        return None
    best = None
    for n in os.listdir(LOGDIR):
        p = os.path.join(LOGDIR, n)
        try:
            head = open(p, errors="replace").read(4000)
        except OSError:
            continue
        if f"process with pid {pid}" in head:
            if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                best = p
    return best


def pick_db(logpath):
    """Rule A: conversation id named in the running pid's log. Rule B: newest .db."""
    dbs = [os.path.join(CONVDIR, n) for n in os.listdir(CONVDIR) if n.endswith(".db")]
    if not dbs:
        return None, "no .db found"
    newest = max(dbs, key=os.path.getmtime)
    if logpath:
        txt = open(logpath, errors="replace").read()
        ids = re.findall(r"for conversation ([0-9a-f-]{36})", txt)
        if ids:
            cand = os.path.join(CONVDIR, ids[-1] + ".db")
            if os.path.exists(cand):
                rule = ("pid's log names this conversation"
                        + ("" if cand == newest else "; NOTE newest .db differs"))
                return cand, rule
    return newest, "newest .db by mtime (log gave no conversation id)"


# ---------------------------------------------------------------- payload
STEP_TYPE = {
    14: "user prompt",
    15: "model turn (tool call)",
    21: "session header",
    23: "context summary (compaction)",
    101: "async task result",
    132: "tool result",
}
STATUS = {2: "running", 3: "ok", 7: "failed/denied", 8: "generating"}


def json_after(blob, start):
    """Scan a balanced JSON object out of a latin-1 decoded payload."""
    i = blob.find('{"', start)
    if i < 0:
        return None
    depth, j, instr, esc = 0, i, False, False
    while j < len(blob):
        ch = blob[j]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(blob[i:j + 1])
                except Exception:
                    return None
        j += 1
    return None


def parse_step(pay):
    """-> (tool_name, args_dict) for a step whose payload carries a tool call."""
    if not pay:
        return None, None
    blob = pay.decode("latin-1")
    m = re.search(r"call_\d+", blob)
    if not m:
        return None, None
    nm = re.search(r"[a-z_]{4,30}", blob[m.end():m.end() + 60])
    name = nm.group() if nm else "?"
    return name, json_after(blob, m.end())


def texts(b):
    if not b:
        return ""
    return "\n".join(m.group().decode("utf8", "replace")
                     for m in re.finditer(rb"[\x20-\x7e\t\n]{5,}", b))


# ---------------------------------------------------------------- main
pid, started, etime, cmdline = find_proc()
logpath = pid_log(pid)
db, rule = pick_db(logpath)
if not db:
    print("no conversation DB found:", rule)
    sys.exit(1)
conv = os.path.basename(db)[:-3]

con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute(
    "select idx, step_type, status, step_payload, error_details from steps order by idx"
).fetchall()

by_ts, cmds, meta_writes, bad_labels, unsigned = {}, [], [], [], []
deny_gh = deny_empty = shell_fail = shell_ok = compactions = 0
deny_empty_last = -1
closes = close_no_check = 0
closed_nums = set()
check_since_commit = False
role_seen = None
last_task_ts = ""

for idx, st, status, pay, err in rows:
    by_ts[(st, status)] = by_ts.get((st, status), 0) + 1
    if st == 21 and pay:
        m = re.search(r"(?<![A-Za-z])(coder|reviewer|monitor)(?![A-Za-z])",
                      pay.decode("latin-1"))
        role_seen = m.group(1) if m else None
    if st == 23:
        compactions += 1
    if st == 101 and pay:
        m = re.search(r"timestamp=(\S+)", pay.decode("latin-1"))
        if m:
            last_task_ts = m.group(1)
    if st != 132:
        continue
    name, args = parse_step(pay)
    etxt = texts(err)
    denied = "denied by pre-tool hook" in etxt
    if name == "run_command" and isinstance(args, dict):
        cl = args.get("CommandLine", "")
        shell_ok += status == 3
        shell_fail += status == 7
        cmds.append((idx, status, red(" ".join(cl.split()))[:150]))
        if denied:
            if cl.strip().startswith("gh "):
                deny_gh += 1
            elif etxt.strip().endswith("pre-tool hook:"):
                deny_empty += 1
                deny_empty_last = idx
        if "_check.py" in cl:
            check_since_commit = True
        if cl.startswith("git commit") and status == 3:
            if "Closes #" in cl:
                closes += 1
                closed_nums.update(re.findall(r"Closes #(\d+)", cl))
                if not check_since_commit:
                    close_no_check += 1
            check_since_commit = False
        if cl.startswith("gh issue edit") and re.search(
                r"--add-label\s+\S*(stage:ready|stage:verified|on hold|P[0-2])", cl):
            bad_labels.append(idx)
        if cl.startswith("gh issue comment") and "99999" not in cl:
            if "--body-file" not in cl and "<!-- coder -->" not in cl:
                unsigned.append(idx)
    if name in ("write_to_file", "replace_file_content") and isinstance(args, dict):
        tgt = (args.get("TargetFile") or args.get("TargetPath")
               or args.get("AbsolutePath") or "")
        if "/.claude/" in tgt or "/.agents/" in tgt:
            meta_writes.append((idx, tgt.replace(REPO + "/", "")))
        # a closing comment drafted into /tmp must already carry the marker
        if re.search(r"/tmp/cod-\d+.*\.md$", tgt):
            body = args.get("CodeContent", "")
            if not body.lstrip().startswith("<!-- coder -->"):
                unsigned.append(idx)

# repeated identical commands inside the last 20 shell calls
win = [c[2] for c in cmds[-20:]]
repeat = max((win.count(x) for x in set(win)), default=0)

# staleness
mt = max([os.path.getmtime(db)] +
         [os.path.getmtime(db + s) for s in ("-wal", "-shm") if os.path.exists(db + s)] +
         ([os.path.getmtime(logpath)] if logpath else []))
stale = int(time.time() - mt)

# repo scope, in real time
staged = [p for p in sh(f"git -C {REPO} diff --cached --name-only").split()
          if not p.startswith(PROJ)]
dirty = [l[3:] for l in sh(f"git -C {REPO} status --porcelain").splitlines()
         if not l[3:].startswith(PROJ)]

# model actually in use
flag = re.search(r"--model\s+(\S+)", cmdline or "") if cmdline else None
flag = flag.group(1) if flag else "(none)"
actual, override_err = "?", ""
if logpath:
    lt = open(logpath, errors="replace").read()
    # NOT the request URL. `streamGenerateContent` goes to models/gemini-3.8-flash at
    # every effort level, so the wire cannot tell high from low and reading the pin off
    # it says "override rejected" on a session where the override landed — which is what
    # #267 was, wrongly, filed about. The `Propagating` line is the only artefact that
    # records the effort, and it is logged *after* the startup miss: agy consults its
    # local config before the model list arrives, logs `failed to apply model override`
    # at E level, then resolves the name 0.2 s later and applies it. The error line is a
    # cold-start race, not an outcome. Absent `Propagating` means it genuinely did not
    # land; that is the only thing worth alarming on.
    labels = re.findall(
        r'Propagating selected model override to backend: label="([^"]*)"', lt
    )
    actual = max(set(labels), key=labels.count) if labels else "?"
    if not labels:
        override_err = "  <-- PIN NEVER APPLIED (no Propagating line)"

# uptime / rate
up_min = 0.0
if etime:
    p = [int(x) for x in etime.replace("-", ":").split(":")]
    while len(p) < 3:
        p.insert(0, 0)
    up_min = (p[-3] * 60 + p[-2] + p[-1] / 60) if len(p) == 3 else 0
    if len(p) == 4:
        up_min = p[0] * 1440 + p[1] * 60 + p[2] + p[3] / 60
per_close = (up_min / closes) if closes else up_min

reopens, reopen_note = "-", "reviewer bounced one of THIS session's closes"
if NET:
    ev = sh('gh api "repos/kazunori279/adk-live-ai-concierge/issues/events?per_page=100" '
            "-q '.[] | select(.event==\"reopened\") | \"\\(.created_at) \\(.issue.number)\"'",
            timeout=15)
    cut = (datetime.now(timezone.utc) - timedelta(minutes=up_min or 60)
           ).strftime("%Y-%m-%dT%H:%M:%SZ")
    mine, other = 0, 0
    for line in ev.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] <= cut:
            continue
        if parts[1] in closed_nums:
            mine += 1
        else:
            other += 1
    reopens = mine
    reopen_note += f"; {other} other reopen(s) this window"

# ---------------------------------------------------------------- verdict
C = [  # name, value, watch_pred, degraded_pred, note
    ("guard_deny_gh", deny_gh, lambda v: v > 2, lambda v: v > 5, "2 = the wiring test"),
    ("guard_deny_empty", deny_empty,
     lambda v: v >= 1, lambda v: deny_empty_last > len(rows) - 30,
     f"empty-reason deny; last at step {deny_empty_last}"),
    ("meta_writes_claude", len(meta_writes), lambda v: False, lambda v: v >= 1, ".claude/.agents edits"),
    ("staged_outside_proj", len(staged), lambda v: False, lambda v: v >= 1, "rule 5, live"),
    ("dirty_outside_proj", len(dirty), lambda v: False, lambda v: False,
     "info: includes the MONITOR's own in-flight .claude/ work"),
    ("shell_fail_pct", round(100 * shell_fail / max(1, shell_ok + shell_fail)),
     lambda v: v > 20, lambda v: v > 35, "failed / all run_command"),
    ("repeat_in_last20", repeat, lambda v: v >= 4, lambda v: v >= 6, "flailing"),
    ("min_per_close", round(per_close, 1), lambda v: v > 30, lambda v: v > 50, "prev session ~19"),
    ("close_without_check", close_no_check, lambda v: v >= 1, lambda v: v >= 2, "rule 4"),
    ("unsigned_comments", len(unsigned), lambda v: False, lambda v: v >= 1, "rule 7"),
    ("wrong_labels", len(bad_labels), lambda v: False, lambda v: v >= 1, "rule 8"),
    ("compactions", compactions, lambda v: v >= 3, lambda v: v >= 6, "context churn"),
    ("stale_secs", stale, lambda v: v > 300, lambda v: v > 900, "no step / no log line"),
    ("reopened_my_closes", reopens if reopens != "-" else 0,
     lambda v: v >= 1, lambda v: v >= 3, reopen_note),
]

watch, bad = [], []
for n, v, w, d in ((c[0], c[1], c[2], c[3]) for c in C):
    if d(v):
        bad.append(f"{n}={v}")
    elif w(v):
        watch.append(f"{n}={v}")

print(f"== agy coder trajectory ==  {datetime.now():%Y-%m-%d %H:%M:%S}")
print(f"conv    {conv}")
print(f"db rule {rule}")
print(f"pid     {pid or '-'} {'ALIVE' if pid else 'GONE'}  started {started}  uptime {etime}")
print(f"model   flag={flag}  actual={actual}{override_err}")
print(f"role    CC_ROLE in session header: {role_seen or '(not found)'}")
print(f"steps   {len(rows)}   last activity {stale}s ago   last async task {last_task_ts or '-'}")
for (st, stt), n in sorted(by_ts.items()):
    print(f"        {st:>3}/{stt}  {n:>3}  {STEP_TYPE.get(st,'?')} / {STATUS.get(stt,stt)}")
if meta_writes:
    print("  !! writes under .claude/ or .agents/:")
    for i, t in meta_writes[-4:]:
        print(f"        step {i}: {t}")
if staged:
    print("  !! staged outside the project dir: " + ", ".join(staged[:4]))
print(f"last {NCMD} shell commands (oldest first):")
for i, s, c in cmds[-NCMD:]:
    print(f"   {i:>4} {'ok ' if s == 3 else 'FAIL'} {c}")
print("counters                     value   watch    degraded")
for n, v, w, d, note in C:
    mark = "DEG " if d(v) else ("WCH " if w(v) else "    ")
    print(f"  {mark}{n:<24}{str(v):>5}   {note}")
verdict = "degraded" if bad else ("watch" if watch else "ok")
print(f"VERDICT: {verdict} — " + ("; ".join(bad + watch) if (bad or watch) else "all counters inside band"))
