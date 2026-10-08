#!/usr/bin/env python3
"""Read-only. Counts the same things .claude/skills/issue-looping-monitor/references/agy-trajectory.py counts, but from a
Claude Code transcript, so the agy coder has something to be compared against.

Identifies each session's own role from the commands it ran rather than from the
markers it read, because every session reads all three.

Usage: uv run --no-project python .claude/skills/issue-looping-monitor/references/cc-baseline.py [dir]
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import Counter
from datetime import datetime

SECRET = re.compile(
    r"AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{16,}|ya29\.[0-9A-Za-z_-]+"
    r"|gh[pousr]_[A-Za-z0-9]{16,}|-----BEGIN"
)

OWN_MARKER = re.compile(r"gh\s+issue\s+comment[^\n]*<!--\s*(monitor|reviewer|coder)\s*-->")
BODY_FILE = re.compile(r"gh\s+issue\s+(?:comment|create)[^\n]*--body-file\s+\S*?(mon|rev|cod)-")
CLOSES = re.compile(r"\bCloses\s+#(\d+)", re.I)
GH_CLOSE = re.compile(r"\bgh\s+issue\s+close\s+(\d+)")
GUARD_REFUSAL = re.compile(r'"decision"\s*:\s*"(?:block|deny)"|This session has no role')
CONFIG_LEAK = re.compile(r"/api/config")


def blocks(msg):
    content = (msg or {}).get("content")
    if isinstance(content, list):
        return content
    return []


def scan(path: pathlib.Path) -> dict | None:
    c = Counter()
    role_votes = Counter()
    times: list[str] = []
    closed: set[str] = set()
    # Wall-clock from first to last timestamp counts the hours the session spent
    # asleep, which for a five-day session is most of them. Count 10-minute buckets
    # that contain at least one event instead: that is the time it was working, and
    # it is the only figure comparable across two hosts run on different schedules.
    active: set[str] = set()

    with path.open(errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                c["unparsable_lines"] += 1
                continue

            ts = rec.get("timestamp")
            if ts:
                times.append(ts)
                active.add(ts[:15])   # YYYY-MM-DDTHH:M -> a 10-minute bucket

            kind = rec.get("type")
            if kind == "summary" or rec.get("isCompactSummary"):
                c["compactions"] += 1

            if kind == "assistant":
                for b in blocks(rec.get("message")):
                    if b.get("type") != "tool_use":
                        continue
                    c["tool_calls"] += 1
                    name = b.get("name", "")
                    inp = b.get("input") or {}
                    if name != "Bash":
                        continue
                    c["bash_calls"] += 1
                    cmd = inp.get("command", "") or ""
                    m = OWN_MARKER.search(cmd)
                    if m:
                        role_votes[m.group(1)] += 1
                    m = BODY_FILE.search(cmd)
                    if m:
                        role_votes[{"mon": "monitor", "rev": "reviewer", "cod": "coder"}[m.group(1)]] += 1
                    for n in CLOSES.findall(cmd) + GH_CLOSE.findall(cmd):
                        closed.add(n)
                    if CONFIG_LEAK.search(cmd):
                        c["api_config_touches"] += 1
                    if SECRET.search(cmd):
                        c["secret_shaped_in_command"] += 1

            if kind == "user":
                for b in blocks(rec.get("message")):
                    if b.get("type") != "tool_result":
                        continue
                    c["tool_results"] += 1
                    if b.get("is_error"):
                        c["tool_errors"] += 1
                    text = b.get("content")
                    if isinstance(text, list):
                        text = " ".join(
                            p.get("text", "") for p in text if isinstance(p, dict)
                        )
                    text = text if isinstance(text, str) else ""
                    if GUARD_REFUSAL.search(text):
                        c["guard_refusals"] += 1
                    if SECRET.search(text):
                        c["secret_shaped_in_output"] += 1

    if not times:
        return None
    times.sort()
    try:
        t0 = datetime.fromisoformat(times[0].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(times[-1].replace("Z", "+00:00"))
        hours = (t1 - t0).total_seconds() / 3600
    except ValueError:
        hours = 0.0
    act_h = len(active) / 6

    role = role_votes.most_common(1)[0][0] if role_votes else "?"
    return {
        "file": path.name.split("-")[0],
        "role": role,
        "role_votes": dict(role_votes),
        "start": times[0][:16].replace("T", " "),
        "hours": round(hours, 2),
        "act_h": round(act_h, 2),
        "closed": len(closed),
        "min_per_close": round(act_h * 60 / len(closed), 1) if closed else None,
        "bash": c["bash_calls"],
        "errors": c["tool_errors"],
        "shell_fail_pct": round(100 * c["tool_errors"] / c["tool_results"]) if c["tool_results"] else 0,
        "compactions": c["compactions"],
        "guard_refusals": c["guard_refusals"],
        "api_config_touches": c["api_config_touches"],
        "secret_in_cmd": c["secret_shaped_in_command"],
        "secret_in_out": c["secret_shaped_in_output"],
    }


def main() -> int:
    d = pathlib.Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else pathlib.Path.home() / ".claude" / "projects" / str(pathlib.Path.cwd()).replace("/", "-")
    )
    rows = []
    for p in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime):
        r = scan(p)
        if r:
            rows.append(r)

    hdr = (
        f"{'file':9} {'role':9} {'start':17} {'hrs':>5} {'act':>5} {'closed':>6} {'min/cl':>7} "
        f"{'bash':>5} {'fail%':>5} {'cmpct':>5} {'guard':>5} {'cfg':>4} {'sec!':>4}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['file']:9} {r['role']:9} {r['start']:17} {r['hours']:5.1f} {r['act_h']:5.1f} "
            f"{r['closed']:6} {str(r['min_per_close'] or '-'):>7} {r['bash']:5} "
            f"{r['shell_fail_pct']:5} {r['compactions']:5} {r['guard_refusals']:5} "
            f"{r['api_config_touches']:4} {r['secret_in_cmd'] + r['secret_in_out']:4}"
        )
    print("\nrole_votes (how the role was inferred — commands the session itself ran):")
    for r in rows:
        print(f"  {r['file']}  {r['role_votes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
