#!/usr/bin/env bash
# Run the loop over GitHub queues, one fresh agy session per batch or tick.
#
#   ccloop.sh              run coder (merge and deploy until queue stays empty)
#   ccloop.sh coder        run coder explicitly
#   ccloop.sh reviewer     run reviewer (one tick per fresh session)
#   ccloop.sh tester       run tester (one tick per fresh session)
#   ROLE=reviewer ccloop.sh
#   ROLE=tester ccloop.sh
#   MAX=3 ccloop.sh        stop after three iterations
#
# WHY THERE IS NO ITERATION CAP BY DEFAULT
#
# There used to be one, MAX=20, described as the brake on a runaway loop. It was
# the wrong instrument. Two of the three ways an iteration can be wasted are
# already caught precisely -- an empty queue by IDLE_MAX, a crashed agy by the
# `rc != 0` exit -- and the third, a coder that finds work every round and never
# finishes it, is not a matter of how many rounds have run. A count both kills
# healthy loops at twenty and lets an unhealthy one burn twenty rounds first.
#
# STUCK_MAX replaces it with the thing actually worth measuring: a productive
# iteration moves origin/main. An iteration that was not idle and moved nothing
# accomplished nothing, and STUCK_MAX of those in a row stops the loop. A healthy
# run never touches it.
#
# One known false negative, and it is the harmless direction: anyone else pushing
# to main during an iteration -- the monitor commits to .claude/ -- also moves
# origin/main and resets the counter. That delays a stop, it does not prevent it.
#
# WHY THIS EXISTS
#
# `agy` has no /loop, so a session finishes its work and stops. Measured
# 2026-09-11: launched 10:04:43, closed #246 at 10:12, sat idle after. The loop
# therefore lives in the shell, and the one design choice is whether each
# iteration continues the last conversation or starts a new one.
#
# IT STARTS A NEW ONE, AND THAT IS THE POINT
#
#   - The coder's unit of work is one batch of mergeable PRs, and nothing it
#     learned merging one batch is needed for the next.
#   - A fresh session re-reads `.claude/skills` from disk. That is #260 — a rule
#     corrected in a file does not reach a session already running, because
#     skills load at session start — and a per-batch restart closes it for the
#     coder without anyone having to remember. Both instances of #260 measured
#     on 2026-09-11 were sessions that had been up for hours.
#
# The cost is re-reading the skill every iteration. That is paid in tokens rather
# than in a session working from rules that were corrected an hour ago.
#
# RUNNING THE REVIEWER (ROLE=reviewer)
#
# Until #571, the reviewer stayed one long session, because its triage gate
# counted "across three of your ticks" and relied on in-session memory. But on
# 2026-09-13 (#571) a reviewer turn wedged mid-turn after 16 hours, freezing
# the loop for nearly 3 hours without crashing.
#
# The reviewer can now run per-tick under this driver:
#   ROLE=reviewer ccloop.sh
#   ccloop.sh reviewer
#
# Each tick runs as a fresh agy session. State that must survive across ticks —
# including triage gate block counters and sighting history — is persisted to
# /tmp/rev-triage-gate by triage_gate.py.
#
# When all queues are empty, the reviewer prints LOOP-REVIEWER-IDLE and stops.
# The driver catches this sentinel -- only as a line of its own in the final
# response, see is_idle() -- and backs off (IDLE_GAP doubling up to
# IDLE_GAP_MAX, default 270s -> 1800s). While active, ticks run with GAP=20s.
# A stuck reviewer is detected when an iteration finishes without the idle sentinel
# and produces no observable GitHub activity (PRs, issue comments, stage transitions).
#
# RUNNING THE TESTER (ROLE=tester)
#
# Until #667, the tester had no driver in ccloop.sh and relied on an in-session
# timer or manual invocation, leaving open issues waiting on needs:live for up to 41 h.
#
# The tester can now run per-tick under this driver:
#   ROLE=tester ccloop.sh
#   ccloop.sh tester
#
# Each tick runs as a fresh agy session.
#
# When origin/main has not moved, regression scenarios pass, and there are no
# new findings or issues to verify, the tester prints LOOP-TESTER-IDLE and stops.
# The driver catches this sentinel and backs off (IDLE_GAP doubling up to
# IDLE_GAP_MAX, default 1200s -> 1800s). While active, ticks run with GAP=20s.
# A stuck tester is detected when an iteration finishes without the idle sentinel
# and produces no observable GitHub activity (issue comments or new triage issues).
#
# WHY AN EMPTY QUEUE IS NOT A STOP ANY MORE
#
# Until 2026-09-11 the coder picked its own issues, so an empty queue meant there
# was nothing to do and the loop exited. Now the reviewer's implementing
# subagents produce the work and they arrive asynchronously: an empty queue
# usually means the reviewer is mid-fan-out, not that the day is over.
#
# IDLE_MAX used to stop the loop after five of those in a row. It was measured on
# 2026-09-12 (#391) and it was the wrong instrument, for the same reason MAX was:
# it stopped healthy runs. Of the eleven iterations in that run, nine were empty
# and two merged everything the run produced -- because this is a merge loop and
# its work arrives from a different session on a different cadence. Being idle
# most of the time is the design, not a symptom. Exiting on it meant the reviewer
# opened PRs into a repository with nobody left to merge them, twice, each time
# needing the owner to notice and restart by hand.
#
# So the loop no longer exits on its own. An empty iteration backs off -- IDLE_GAP
# doubling up to IDLE_GAP_MAX -- and the backoff resets the moment something
# merges. A quiet night costs a handful of sessions instead of one every three
# minutes, and a PR opened at 03:00 waits at most IDLE_GAP_MAX rather than until
# somebody wakes up.
#
# The other two stop conditions went the same way, and for the same reason: every
# one of them ends with a queue nobody is draining.
#
#   - A non-zero rc used to exit "rather than retrying blind". One timed-out
#     batch should not end the night; it backs off and retries, loudly.
#   - STUCK_MAX still detects a coder spinning on work it cannot finish, and
#     still says so in as many words -- but it backs off instead of exiting,
#     because the condition clears by itself when the reviewer fixes the PR.
#
# What is left is a loop that stops on ^C, or on MAX if you asked for one. If you
# want the old behaviour for a scripted run, set MAX.
#
# WHY THE CONSOLE SHOWS THE STEPS
#
# `--print` emits one summary after the batch is over and nothing at all while it
# runs. Measured 2026-09-12: a productive iteration's entire log was 19 lines,
# every one of them written after the merge had already happened. From outside,
# a coder merging three PRs and a coder wedged on a prompt look identical for
# thirty minutes. `--output-format stream-json` carries the intermediate steps;
# ccloop_render.py turns them into one timestamped line each, and the raw stream
# is kept beside it. Set STREAM=0 to go back to plain text.
#
# Logs: three files per iteration under $LOGDIR -- NNN.log (what you watched),
# NNN.jsonl (the raw stream, for a trajectory audit after the terminal is gone)
# and NNN.err (stderr).

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# overridable so the loop's own stop conditions can be exercised with a stub
CCROLE="${CCROLE:-$HERE/ccrole.sh}"

ROLE="${ROLE:-coder}"
if [ -n "${1:-}" ]; then
  ROLE="$1"
  shift
fi

if [ "$ROLE" != "coder" ] && [ "$ROLE" != "reviewer" ] && [ "$ROLE" != "tester" ]; then
  echo "ccloop.sh: unknown role '$ROLE' (must be 'coder', 'reviewer', or 'tester')" >&2
  exit 1
fi

if [ "$ROLE" = "reviewer" ]; then
  LOGDIR="${LOGDIR:-/tmp/rev-loop}"
  IDLE_GAP="${IDLE_GAP:-270}"         # prompt cache TTL is 300s; 270s keeps cache warm
  IDLE_GAP_MAX="${IDLE_GAP_MAX:-1800}" # backoff ceiling when queues remain quiet
  IDLE_SENTINEL="LOOP-REVIEWER-IDLE"
  IDLE_REASON="reviewer idle"
elif [ "$ROLE" = "tester" ]; then
  LOGDIR="${LOGDIR:-/tmp/tst-loop}"
  IDLE_GAP="${IDLE_GAP:-1200}"        # 20 min between runs when quiet
  IDLE_GAP_MAX="${IDLE_GAP_MAX:-1800}" # backoff ceiling (30 min)
  IDLE_SENTINEL="LOOP-TESTER-IDLE"
  IDLE_REASON="tester idle"
else
  LOGDIR="${LOGDIR:-/tmp/cod-loop}"
  IDLE_GAP="${IDLE_GAP:-180}"         # first wait after an empty queue — a PR takes minutes to appear
  IDLE_GAP_MAX="${IDLE_GAP_MAX:-900}"  # ceiling for the backoff, so a PR never waits longer
  IDLE_SENTINEL="LOOP-NOTHING-TO-MERGE"
  IDLE_REASON="nothing to merge"
fi

MAX="${MAX:-0}"             # 0 = no cap; see WHY THERE IS NO ITERATION CAP above
GAP="${GAP:-20}"            # seconds after a batch, so a ^C has somewhere to land
STUCK_MAX="${STUCK_MAX:-3}" # consecutive busy iterations that move origin/main not at all (coder) or produce no activity (reviewer/tester)
TIMEOUT="${TIMEOUT:-30m}"   # agy's default is 5m and a batch with a deploy takes longer
STREAM="${STREAM:-1}"       # 1 = render agy's step-by-step stream; 0 = plain --print text
START_FAIL_MAX="${START_FAIL_MAX:-3}"       # max consecutive startup/auth failures before backing off
START_RETRY_GAP="${START_RETRY_GAP:-10}"    # seconds between fast retries on startup failure
COUNTDOWN_SLEEP="${COUNTDOWN_SLEEP:-1}"     # 1 = sleep in countdown; 0 = return immediately (for tests)


mkdir -p "$LOGDIR"

RENDER="${RENDER:-$HERE/ccloop_render.py}"
[ -x "$RENDER" ] || RENDER="python3 $RENDER"

# A wait with no output is indistinguishable from a hang, and these waits now go
# up to fifteen minutes. One line a minute is enough to show a pulse without
# burying the iteration headers.
#
# The wait checks $LOGDIR/wake every second and ends early when ccwake.sh has
# written it (#698): new work for this role appeared on GitHub, so the backoff
# has nothing left to wait for, and it resets.
countdown() {
  local left="$1" why="$2" tick=0
  woke=0
  printf '    %s  %s — next try at %s\n' \
    "$(date '+%H:%M:%S')" "$why" "$(date -v "+${left}S" '+%H:%M:%S' 2>/dev/null || date -d "+${left} seconds" '+%H:%M:%S')"
  if [ -e "$LOGDIR/wake" ]; then
    rm -f "$LOGDIR/wake"
    printf '    %s  woken by ccwake — starting early\n' "$(date '+%H:%M:%S')"
    woke=1
    return 0
  fi
  if [ "${COUNTDOWN_SLEEP:-1}" = "0" ]; then
    return 0
  fi
  while [ "$left" -gt 0 ]; do
    if [ -e "$LOGDIR/wake" ]; then
      rm -f "$LOGDIR/wake"
      printf '    %s  woken by ccwake — starting early\n' "$(date '+%H:%M:%S')"
      woke=1
      return 0
    fi
    sleep 1
    left=$((left - 1))
    tick=$((tick + 1))
    if [ $((tick % 60)) -eq 0 ] && [ "$left" -gt 0 ]; then
      printf '    %s  still waiting, %dm left\n' "$(date '+%H:%M:%S')" "$(( (left + 59) / 60 ))"
    fi
  done
}

# Did the agent end the tick idle? Only a line that is the sentinel and nothing
# else, in the agent's final response, counts (#696). The raw stream also holds
# every tool result, and the roles read their own skills, which quote the
# sentinel -- so grepping the whole stream read busy ticks as idle, and so did a
# final message that named the sentinel to say it was not printing it.
is_idle() {
  python3 - "$1" "$IDLE_SENTINEL" <<'PY'
import json, sys
path, sentinel = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8", errors="replace").read()
final = None
for line in text.splitlines():
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if isinstance(ev, dict) and ev.get("event") == "result":
        final = (ev.get("result") or {}).get("response") or ""
if final is None:
    final = text  # STREAM=0: the file is the agent's plain output
sys.exit(0 if any(l.strip() == sentinel for l in final.splitlines()) else 1)
PY
}

# The role's work signature, written at the end of every tick so that ccwake.sh
# measures change from here -- which is what stops a role's own writes from
# waking it (#698). A failed read leaves the old record in place.
SIGNATURE_CMD="${SIGNATURE_CMD:-python3 $HERE/ccwake_sig.py --role $ROLE}"
compute_signature() {
  local sig
  if sig=$($SIGNATURE_CMD 2>/dev/null) && [ -n "$sig" ]; then
    printf '%s\n' "$sig"
  fi
}
record_signature() {
  local sig="${1:-}"
  [ -z "$sig" ] && sig=$(compute_signature)
  if [ -n "$sig" ]; then
    printf '%s\n' "$sig" > "$LOGDIR/signature"
  fi
  rm -f "$LOGDIR/wake"
}

timeout_to_seconds() {
  local t="$1"
  if [[ "$t" =~ ^([0-9]+)m$ ]]; then
    echo $(( ${BASH_REMATCH[1]} * 60 ))
  elif [[ "$t" =~ ^([0-9]+)s$ ]]; then
    echo "${BASH_REMATCH[1]}"
  elif [[ "$t" =~ ^([0-9]+)h$ ]]; then
    echo $(( ${BASH_REMATCH[1]} * 3600 ))
  elif [[ "$t" =~ ^[0-9]+$ ]]; then
    echo "$t"
  else
    echo 1800
  fi
}

REPO="${REPO:-$(git -C "$HERE" rev-parse --show-toplevel)}"
head_sha() {
  if [ -z "${NO_FETCH:-}" ]; then
    git -C "$REPO" fetch -q origin main 2>/dev/null || true
  fi
  git -C "$REPO" rev-parse origin/main 2>/dev/null || echo "0000000"
}

progress_snapshot() {
  if [ -n "${PROGRESS_SNAPSHOT:-}" ]; then
    echo "$PROGRESS_SNAPSHOT"
    return
  fi
  if [ "$ROLE" = "reviewer" ]; then
    local pr comment event
    pr=$(gh api "repos/{owner}/{repo}/pulls?state=all&sort=created&direction=desc&per_page=1" --jq '.[0].number' 2>/dev/null || true)
    comment=$(gh api "repos/{owner}/{repo}/issues/comments?sort=created&direction=desc&per_page=1" --jq '.[0].id' 2>/dev/null || true)
    event=$(gh api "repos/{owner}/{repo}/issues/events?per_page=1" --jq '.[0].id' 2>/dev/null || true)
    printf 'pr:%s cmt:%s evt:%s' "$pr" "$comment" "$event"
  elif [ "$ROLE" = "tester" ]; then
    local comment event
    comment=$(gh api "repos/{owner}/{repo}/issues/comments?sort=created&direction=desc&per_page=1" --jq '.[0].id' 2>/dev/null || true)
    event=$(gh api "repos/{owner}/{repo}/issues/events?per_page=1" --jq '.[0].id' 2>/dev/null || true)
    printf 'cmt:%s evt:%s' "$comment" "$event"
  else
    head_sha
  fi
}

# The sentinel is the whole stop condition, so it is stated twice: once as the
# thing to print, once as the thing not to print for any other reason.
read -r -d '' PROMPT_CODER <<'EOF'
Use the issue-looping-shared skill and work exactly one batch, then stop.

Read the open pull requests. For each mergeable one, read the diff, merge it with
`gh pr merge --squash --delete-branch`, clear stage:pr in the very next command,
and post the signed merge comment. Merge them one at a time, then deploy the
whole batch to the Pi in ONE process, scoped by the union of what the batch
touched. You run no check suite.

Three rules for this run, on top of the skill:

1. One batch. Do not go back for PRs that appeared while you were working — a
   wrapper will start you again with a fresh session, which is how you get the
   current version of these rules.
2. A conflicting PR is work, not an empty queue. Rebase it onto origin/main and
   merge it, as section 3 of your skill says. Only a hunk that needs a decision
   about behaviour goes back to the reviewer, and it goes back once: if you have
   already said so on that PR and nothing has changed since, say nothing again.
3. If there is genuinely nothing to merge — no open PRs, or the only ones left
   are not yet green, or are already reported as needing a reviewer decision —
   print the line LOOP-NOTHING-TO-MERGE on its own and stop. Print that line for
   no other reason: it is what paces the loop.
EOF

read -r -d '' PROMPT_REVIEWER <<EOF
Use the issue-looping-reviewer skill and work exactly one tick, then stop.

Check the three queues in order and give every queue that fires its subagents:
1. Implementing fan-out: if stage:ready has unblocked issues and worker capacity (<10 live worktrees), verify target worktrees exist and dispatch implementing subagents. Do not dispatch an implementer with less than 15 minutes left in the tick.
2. Reopen sweep: if any open issue has stage:verified, clear it.
3. Verify queue: audit any closed issues awaiting verification. Collect completed PRs and publish verdicts.
4. Triage gate: check the four triggers in Feeding the queue (1: unblocked P0, 2: triage ranks at/above startable queue, 3: >3 unblocked P0/P1, 4: unblocked P0/P1 across 3 ticks). If any trigger fires, dispatch analysis subagents.

Rules for this run, on top of the skill:
1. Work exactly one tick. Do not re-arm or schedule a timer in-session — an external driver runs you per-tick with fresh sessions.
2. Publish observable progress on GitHub for any action taken (issue comments, label changes, PRs created, or heartbeat timestamps).
3. If there was genuinely nothing to do across all three queues — verify queue empty, triage gate quiet, and ready queue empty or cap reached — print the line LOOP-REVIEWER-IDLE on its own and stop. Print that line for no other reason: it is what paces the loop. The idle sentinel LOOP-REVIEWER-IDLE cannot print while the triage drain query is non-empty.
4. Budget: You have a hard wall-clock timeout of $TIMEOUT for this tick. Manage your time and commit/push completed work before you are killed.
EOF

read -r -d '' PROMPT_TESTER <<EOF
Use the issue-looping-tester skill and work exactly one tick, then stop.

A run, in order:
1. Read the closures since your last run and sort them by the regression table.
2. Run the scenario suite against a local test server on your own port (never 8000, SESSIONS_DB in /tmp).
3. Read every screenshot produced, green ones included, and judge UX friction.
4. Drain the stage:live queue: for each issue, verify on raspberrypi.local or against a live session and post a live-verified: line, or say on the thread that the rig cannot reach it.
5. Sweep the next five closed issues off the backlog for missing assertions.
6. Record metrics and rewrite current_test_results.md at the project root.
7. Stop the test server.

Rules for this run, on top of the skill:
1. Work exactly one tick. Do not re-arm or schedule a timer in-session — an external driver runs you per-tick with fresh sessions.
2. Publish observable progress on GitHub for any action taken (issue comments on verified/failed closures or stage:live issues, or new issues filed at stage:triage).
3. If origin/main has not moved since last run, regression scenarios pass, and there are no new findings to report or issues to verify, print the line LOOP-TESTER-IDLE on its own and stop. Print that line for no other reason: it is what paces the loop. The idle sentinel LOOP-TESTER-IDLE cannot print while any stage:live issue lacks a tester comment since its transition.
4. Budget: You have a hard wall-clock timeout of $TIMEOUT for this tick. Manage your time and commit/push completed work before you are killed.
EOF

if [ "$ROLE" = "reviewer" ]; then
  PROMPT="$PROMPT_REVIEWER"
elif [ "$ROLE" = "tester" ]; then
  PROMPT="$PROMPT_TESTER"
else
  PROMPT="$PROMPT_CODER"
fi

idle=0
stuck=0
i=0
progress_count=0
backoff="$IDLE_GAP"
start_fails=0
woke=0


# Every path out of an iteration comes back through here. Nothing below calls
# exit, which is the whole point: see WHY AN EMPTY QUEUE IS NOT A STOP ANY MORE.
back_off() {
  countdown "$backoff" "$1"
  if [ "$woke" = "1" ]; then
    backoff="$IDLE_GAP"
    return 0
  fi
  backoff=$((backoff * 2))
  [ "$backoff" -gt "$IDLE_GAP_MAX" ] && backoff="$IDLE_GAP_MAX"
  return 0
}

while [ "$MAX" -eq 0 ] || [ "$i" -lt "$MAX" ]; do
  i=$((i + 1))
  n=$(printf '%03d' "$i")
  log="$LOGDIR/$n.log"
  raw="$LOGDIR/$n.jsonl"
  err="$LOGDIR/$n.err"
  before="$(progress_snapshot)"
  local_role_label="merged"
  [ "$ROLE" = "reviewer" ] && local_role_label="active ticks"
  [ "$ROLE" = "tester" ] && local_role_label="test runs"
  if [ "$MAX" -eq 0 ]; then
    printf '\n=== %s  iteration %s  (%s: %s so far)  -> %s\n' \
      "$(date '+%H:%M:%S')" "$i" "$local_role_label" "$progress_count" "$log"
  else
    printf '\n=== %s  iteration %s/%s  (%s: %s so far)  -> %s\n' \
      "$(date '+%H:%M:%S')" "$i" "$MAX" "$local_role_label" "$progress_count" "$log"
  fi

  # stderr goes to its own file, plainly, with no process substitution.
  #
  # It has to leave stdout because ccrole.sh writes a role banner there
  # and non-JSON in the middle of the stream is the renderer's problem.
  pre_sig=$(compute_signature)
  [ -z "$pre_sig" ] && [ -f "$LOGDIR/signature" ] && pre_sig=$(cat "$LOGDIR/signature" 2>/dev/null || true)
  start_sec=$(date +%s)
  if [ "$STREAM" = "1" ]; then
    "$CCROLE" "$ROLE" -- \
      --print "$PROMPT" \
      --print-timeout "$TIMEOUT" \
      --output-format stream-json \
      --dangerously-skip-permissions \
      2>"$err" \
      | tee "$raw" | $RENDER | tee "$log"
    rc=${PIPESTATUS[0]}
  else
    raw="$log"
    "$CCROLE" "$ROLE" -- \
      --print "$PROMPT" \
      --print-timeout "$TIMEOUT" \
      --dangerously-skip-permissions \
      2>"$err" \
      | tee "$log"
    rc=${PIPESTATUS[0]}
  fi
  end_sec=$(date +%s)
  elapsed=$((end_sec - start_sec))
  timeout_sec=$(timeout_to_seconds "$TIMEOUT")

  if grep -q "print timeout" "$err" 2>/dev/null || [ "$rc" -eq 124 ] || [ "$elapsed" -ge "$timeout_sec" ]; then
    echo "!!! tick timed out after $TIMEOUT" >&2
    echo "!!! tick timed out after $TIMEOUT" >> "$log"
    if [ "$ROLE" = "reviewer" ]; then
      for wt in /tmp/rev-*-wt; do
        if [ -d "$wt" ]; then
          git -C "$wt" add -N . 2>/dev/null || true
          diff_out=$(git -C "$wt" diff HEAD 2>/dev/null || true)
          status_out=$(git -C "$wt" status --porcelain 2>/dev/null || true)
          if [ -n "$diff_out" ] || [ -n "$status_out" ]; then
            patch_file="/tmp/$(basename "$wt")-rescued.diff"
            git -C "$wt" diff HEAD > "$patch_file" 2>/dev/null || true
            echo "!!! Rescued stranded work from $wt into $patch_file" >&2
            echo "!!! Rescued stranded work from $wt into $patch_file" >> "$log"
            wt_base=$(basename "$wt")
            if [[ "$wt_base" =~ ^rev-([0-9]+)-wt$ ]]; then
              issue_num="${BASH_REMATCH[1]}"
              gh issue comment "$issue_num" --body "<!-- reviewer -->
Tick timed out after $TIMEOUT with uncommitted work. Rescued patch saved to \`$patch_file\` on loop host." 2>/dev/null || true
            fi
          fi
        fi
      done
    fi
    record_signature
    back_off "agy timed out ($TIMEOUT)"
    continue
  fi

  if [ "$rc" -ne 0 ]; then
    if [ "$start_fails" -lt "$START_FAIL_MAX" ]; then
      start_fails=$((start_fails + 1))
      echo "!!! agy exited $rc on iteration $i — fast retry $start_fails/$START_FAIL_MAX" | tee -a "$log"
      echo "    stderr: $err" | tee -a "$log"
      if [ -s "$err" ]; then
        tail -n 20 "$err" 2>/dev/null | sed 's/^/    | /' | tee -a "$log"
      fi
      countdown "$START_RETRY_GAP" "fast retry ($start_fails/$START_FAIL_MAX)"
      continue
    fi

    echo "error: agy exited $rc (persistent: $start_fails failures)" > "$LOGDIR/status"
    echo "!!! agy exited $rc on iteration $i — persistent startup failure ($start_fails failures)" | tee -a "$log"
    echo "    stderr: $err" | tee -a "$log"
    if [ -s "$err" ]; then
      tail -n 20 "$err" 2>/dev/null | sed 's/^/    | /' | tee -a "$log"
    fi
    backoff="$IDLE_GAP"
    countdown "$IDLE_GAP" "agy exited $rc (persistent: $start_fails failures)"
    continue
  fi

  start_fails=0
  rm -f "$LOGDIR/status"

  post_sig=$(compute_signature)

  if is_idle "$raw"; then
    if [ -n "$pre_sig" ] && [ -n "$post_sig" ] && [ "$post_sig" != "$pre_sig" ]; then
      printf '%s\n' "$pre_sig" > "$LOGDIR/signature"
      touch "$LOGDIR/wake"
    else
      record_signature "$post_sig"
    fi
    idle=$((idle + 1))
    back_off "$IDLE_REASON ($idle in a row)"
    continue
  fi

  record_signature "$post_sig"

  idle=0
  after="$(progress_snapshot)"

  # The iteration claimed there was work. If no forward progress was made,
  # whatever it tried is still there for next round.
  if [ "$after" = "$before" ]; then
    stuck=$((stuck + 1))
    if [ "$stuck" -ge "$STUCK_MAX" ]; then
      if [ "$ROLE" = "reviewer" ]; then
        echo "!!! busy but produced no GitHub activity $stuck times running"
        echo "!!! the reviewer is spinning on work it cannot finish — someone should look."
      elif [ "$ROLE" = "tester" ]; then
        echo "!!! busy but produced no GitHub activity $stuck times running"
        echo "!!! the tester is spinning on work it cannot finish — someone should look."
      else
        echo "!!! busy but merged nothing $stuck times running (origin/main still $before)"
        echo "!!! the coder is spinning on work it cannot finish — someone should look."
      fi
      echo "    It keeps going: the condition clears by itself when the state changes."
      back_off "spinning on work it cannot finish"
      continue
    fi
    echo "busy but produced no activity ($stuck/$STUCK_MAX)"
    sleep "$GAP"
    continue
  fi

  stuck=0
  progress_count=$((progress_count + 1))
  backoff="$IDLE_GAP"
  if [ "$ROLE" = "reviewer" ]; then
    printf '*** %s  reviewer activity: %s -> %s\n' \
      "$(date '+%H:%M:%S')" "$before" "$after"
  elif [ "$ROLE" = "tester" ]; then
    printf '*** %s  tester activity: %s -> %s\n' \
      "$(date '+%H:%M:%S')" "$before" "$after"
  else
    printf '*** %s  origin/main %s -> %s\n' \
      "$(date '+%H:%M:%S')" "${before:0:7}" "$(git -C "$REPO" rev-parse --short origin/main)"
  fi
  sleep "$GAP"
done

echo "hit MAX=$MAX iterations — stopping. Re-run to continue."
