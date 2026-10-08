#!/usr/bin/env bash
# agyview.sh — put another role's agy console on this terminal.
#
# Every loop session already runs inside the one herdr session
# (`herdr --session 2026-0906-raspi-ai-terminal server`, pid 3304 on
# 2026-09-14), one pane each. So the consoles are not hidden — they are panes
# you have to find, in a session with six of them, three of which carry no
# label at all. This script answers "which pane is the reviewer" and then puts
# that pane's output somewhere you are already looking.
#
#   agyview.sh map                 role -> pane -> pid, one line each
#   agyview.sh dump <role> [n]     last n lines of that role's console
#   agyview.sh follow <role> [n]   redraw it on THIS terminal until Ctrl-C
#   agyview.sh mirror [secs]       every role -> /tmp/agy-console/<role>.txt
#
# <role> is coder | reviewer | tester | monitor, or a pane id (w1:p4).
#
# WHY IT READS THE PANE AND NOT THE PROCESS
#
# The reviewer and the tester run agy directly, and agy puts its own stdout and
# stderr on a log file, not on the terminal:
#
#   pid 56028 (reviewer)  fd0 /dev/ttys037   fd1,fd2 -> cli-20260914_061748.log
#                         fd5,fd6 /dev/ttys037     <- the TUI, on a separate fd
#
# That log is the wrong thing to tail. It is glog — internal debug lines and Go
# stack traces from detached-context warnings — and it carries no tool call, no
# model text, nothing a person wants. The console the TUI draws on fd5/fd6 is
# the readable one, and `herdr pane read` is how you get at it without touching
# the process.
#
# Not touching the process is the whole design constraint. See ccloop.sh's note
# on `2> >(tee -a "$err" >&2)`: adding a live console tail to the coder's
# pipeline wedged the loop for good on 2026-09-12, because the process
# substitution inherited the write end of the pipe and it never reached EOF.
# Anything that rewires a running role's fds can do that again. This script
# opens nothing on the role; it asks herdr for a snapshot over a socket, and
# the worst it can do on failure is print nothing.
#
# WHAT IT DOES NOT GIVE YOU
#
# A snapshot, overwritten — not an append-only transcript. A TUI redraws, so
# the tail of the scrollback mutates between polls and there is no stable
# marker to append after; the owner's own tmux version of this in gcp-eoa
# (`tmux capture-pane -p ... > /tmp/agy-live.txt` on a 2s loop) overwrites for
# the same reason. History is therefore herdr's scrollback depth, which on
# 2026-09-14 was 8665 lines for a reviewer 1h11m old — a couple of hours, not
# the whole session. The coder is the exception and needs none of this: ccloop
# already writes /tmp/cod-loop/NNN.{log,jsonl,err} per iteration.

set -uo pipefail

OUTDIR="${AGYVIEW_OUTDIR:-/tmp/agy-console}"
LINES="${AGYVIEW_LINES:-200}"
INTERVAL="${AGYVIEW_INTERVAL:-3}"

# role -> pane, printed as "<role> <pane_id> <pid>".
#
# CC_ROLE is read from the foreground process's environment, one variable at a
# time. Never `ps -E ... | grep CC_ROLE`: -E prints the whole environment on one
# line, so that pipeline puts every API key in the env onto your screen.
#
# The coder is the exception and has no CC_ROLE of its own. ccloop.sh is a
# driver; it sets the role per iteration by invoking `ccrole.sh coder`, so its
# own env has nothing to read and it is matched on the command line instead.
panemap() {
  herdr pane list 2>/dev/null | python3 -c '
import json, subprocess, sys

try:
    panes = json.load(sys.stdin)["result"]["panes"]
except Exception:
    sys.exit(1)

for pane in panes:
    pid = pane.get("pane_id")
    info = subprocess.run(
        ["herdr", "pane", "process-info", "--pane", pid],
        capture_output=True, text=True,
    )
    if info.returncode != 0:
        continue
    try:
        pinfo = json.loads(info.stdout)["result"]["process_info"]
    except Exception:
        continue
    procs = pinfo.get("foreground_processes", [])
    # The process group leader, not procs[0]. procs[0] is the innermost child —
    # a `sleep 60` while ccloop waits out its GAP, or an agy helper — and
    # it changes between polls, so it is useless for "is the reviewer still the
    # process it was an hour ago".
    leader = pinfo.get("foreground_process_group_id", "-")

    role = None
    for proc in procs:
        cmd = proc.get("cmdline", "")
        if "ccloop.sh" in cmd:
            role = "coder"
            break
        env = subprocess.run(
            ["ps", "-E", "-p", str(proc["pid"]), "-o", "command="],
            capture_output=True, text=True,
        ).stdout
        for word in env.split():
            if word.startswith("CC_ROLE="):
                role = word.split("=", 1)[1]
                break
        if role:
            break

    if not role and pane.get("agent") == "claude":
        role = "monitor"
    if role:
        print(role, pid, leader)
'
}

# A role name, or a pane id passed straight through.
resolve() {
  case "$1" in
    w[0-9]*:p*) printf '%s\n' "$1"; return 0 ;;
  esac
  panemap | awk -v r="$1" '$1 == r { print $2; found = 1 } END { exit !found }'
}

read_pane() {
  herdr pane read "$1" --source recent-unwrapped --lines "${2:-$LINES}" --format text 2>/dev/null
}

case "${1:-map}" in
  map)
    printf '%-9s %-7s %s\n' ROLE PANE PID
    panemap | sort | while read -r role pane pid; do
      printf '%-9s %-7s %s\n' "$role" "$pane" "$pid"
    done
    ;;

  dump)
    pane=$(resolve "${2:?usage: agyview.sh dump <role>}") || {
      echo "agyview: no pane for role '$2' — try: agyview.sh map" >&2; exit 1; }
    read_pane "$pane" "${3:-$LINES}"
    ;;

  follow)
    pane=$(resolve "${2:?usage: agyview.sh follow <role>}") || {
      echo "agyview: no pane for role '$2' — try: agyview.sh map" >&2; exit 1; }
    n="${3:-$LINES}"
    # Redraw in place. Resolving the pane once is deliberate: a role that dies
    # and is relaunched gets a new pane, and silently following the new one
    # would hide the restart, which is the event you most want to see.
    trap 'printf "\033[?25h\n"; exit 0' INT TERM
    printf '\033[?25l'
    while :; do
      out=$(read_pane "$pane" "$n")
      printf '\033[H\033[2J'
      printf '=== %s (%s) — %s ===\n' "$2" "$pane" "$(date '+%H:%M:%S')"
      if [ -n "$out" ]; then printf '%s\n' "$out"; else echo "(pane gone)"; fi
      sleep "$INTERVAL"
    done
    ;;

  mirror)
    secs="${2:-$INTERVAL}"
    mkdir -p "$OUTDIR"
    echo "agyview: mirroring every ${secs}s into $OUTDIR (Ctrl-C to stop)" >&2
    while :; do
      panemap | while read -r role pane _pid; do
        read_pane "$pane" "$LINES" > "$OUTDIR/$role.txt.tmp" 2>/dev/null &&
          mv "$OUTDIR/$role.txt.tmp" "$OUTDIR/$role.txt"
      done
      sleep "$secs"
    done
    ;;

  *)
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
