#!/usr/bin/env bash
# Wake a ccloop.sh role early when its GitHub work changes (#698).
#
#   ccwake.sh              poll every WAKE_POLL seconds until ^C
#   WAKE_ONCE=1 ccwake.sh  poll once and exit (for tests)
#
# WHY THIS EXISTS
#
# Every check for new work used to be a full LLM tick, so ccloop.sh backs off to
# IDLE_GAP_MAX after idle ticks, and new work waited out that backoff. Measured
# 2026-09-26: the coder refused PR #697 at 06:28 JST and the reviewer, in backoff
# since 06:36, had not picked it up at 06:56. This watcher is not an LLM. It
# polls GitHub with one GraphQL request and touches $LOGDIR/wake for any role
# whose work signature moved; ccloop.sh's countdown sees the file within a
# second and starts the next tick.
#
# The wake carries no content. The woken session reads GitHub exactly as before,
# so the protocol is still GitHub only, with no messages between sessions.
#
# WAKE ON STATE, NOT ON EVENTS
#
# ccloop.sh writes $LOGDIR/signature at the end of every tick, which absorbs that
# role's own writes. This script compares the live signature with that record,
# and wakes only on a difference. A role that has not finished a tick since
# ccloop.sh started has no record and is left alone.
#
# The owner starts it, alongside the three ccloop.sh roles. It does not start
# itself, and the loop works without it -- only slower.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WAKE_POLL="${WAKE_POLL:-45}"        # seconds between polls; one GraphQL request each
WAKE_ONCE="${WAKE_ONCE:-0}"
# role:logdir pairs; the logdirs are ccloop.sh's defaults
WAKE_ROLES="${WAKE_ROLES:-coder:/tmp/cod-loop reviewer:/tmp/rev-loop tester:/tmp/tst-loop}"
SIGNATURE_ALL_CMD="${SIGNATURE_ALL_CMD:-python3 $HERE/ccwake_sig.py --all}"

poll() {
  local sigs role dir live recorded
  if ! sigs=$($SIGNATURE_ALL_CMD); then
    printf '    %s  signature poll failed; no wake\n' "$(date '+%H:%M:%S')"
    return
  fi
  for pair in $WAKE_ROLES; do
    role="${pair%%:*}"
    dir="${pair#*:}"
    live=$(awk -v r="$role" '$1 == r { print $2 }' <<<"$sigs")
    [ -n "$live" ] || continue
    [ -f "$dir/signature" ] || continue
    recorded=$(cat "$dir/signature")
    if [ "$live" != "$recorded" ] && [ ! -e "$dir/wake" ]; then
      date '+%s' > "$dir/wake"
      printf '*** %s  woke %s (%s -> %s)\n' "$(date '+%H:%M:%S')" "$role" "$recorded" "$live"
    fi
  done
}

while :; do
  poll
  [ "$WAKE_ONCE" = "1" ] && exit 0
  sleep "$WAKE_POLL"
done
