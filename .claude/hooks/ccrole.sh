#!/usr/bin/env bash
# Give an agent session a role, from outside the session.
#
# The role has to come from the environment rather than from the model, because a
# session that reads SKILL.md and concludes it is the reviewer is indistinguishable
# from one that was told to be. That is not hypothetical: see role_guard.py's header.
#
#   ccrole.sh reviewer                 launch a session as the reviewer
#   ccrole.sh coder -- --resume        launch as the coder, passing flags to the CLI
#   ccrole.sh assign <session-id> ROLE give a role to a session already running
#   ccrole.sh list                     show every assigned session
#   ccrole.sh cli                      show which CLI each role launches
#   ccrole.sh revoke <session-id>      take a role away
#
# Add to ~/.zshrc for the short form:
#   ccrole() { "${SHARED_REPO_PATH}/.claude/hooks/ccrole.sh" "$@"; }
#
# THE FOURTH ROLE (2026-09-11)
#
# `tester` opens the app in a real browser and reports what it sees. It exists
# because on 2026-09-11 a change merged, swept clean and deployed, and the weather
# was blank in a browser: the 22 check scripts call functions, and not one of them
# opens the page. It runs against a locally started server, never the Pi, so it can
# never collide with the coder's deploy or with the household's device.
#
# THE FIFTH ROLE, WHICH IS NOT PART OF THE LOOP (2026-09-13)
#
# `aide` is for a session the owner starts to write something down — filing an
# issue while the monitor is busy with the last one. It has no tick, no queue and
# no unit of work; it files, it comments, and it may not close, merge or move an
# issue's stage.
#
# It exists because the alternative was worse. role_guard refuses a roleless
# write outright, so before #470 these sessions got through only on the
# fail-open gap for unreadable comment bodies — which is exactly the gap #470
# closed, and which is why #477's body and a comment on #473 are sitting in the
# timeline unsigned. Closing the gap without adding this role would have made
# the owner's own sessions refuse with "ask the owner which role you are".
#
#   ccrole.sh aide                     launch one
#   ccrole.sh assign <session-id> aide give the role to one already running
#
# WHICH CLI RUNS WHICH ROLE (#243, extended 2026-09-11)
#
# The coder and the reviewer run on the Antigravity CLI (`agy`); the monitor stays
# on Claude Code. The reviewer moved on the owner's word the same day it took over
# implementation, and the three capabilities that move depended on were checked
# against the binary first rather than assumed: parallel subagents, `git worktree`
# checkouts, and a `schedule` tool with one-shot timers and cron. All three exist.
#
# The monitor staying put is not inertia. It is the only role that writes files in
# this repo, it is the one whose `/loop` dynamic pacing has no exact agy analogue,
# and keeping one session on each CLI is what makes the comparison still a
# comparison.
#
# It is a trial, so reverting must not need an edit to this file:
#
#   CCROLE_CODER_CLI=claude ccrole coder       put the coder back on Claude Code
#   CCROLE_REVIEWER_CLI=claude ccrole reviewer put the reviewer back
#   CCROLE_CLI=claude ccrole coder             force one CLI for every role
#   CCROLE_AGY_MODEL="..." ccrole coder        pin a model (`agy models` lists them)
#
# If `agy` underperforms, exporting those in ~/.zshrc reverts the whole thing with
# no code change and no restart of the sessions that stayed.

set -euo pipefail

ROLE_DIR="$HOME/.claude/cc-role"
VALID="monitor reviewer coder tester aide"

cli_for() {
  case "$1" in
    coder)    printf '%s' "${CCROLE_CLI:-${CCROLE_CODER_CLI:-agy}}" ;;
    reviewer) printf '%s' "${CCROLE_CLI:-${CCROLE_REVIEWER_CLI:-agy}}" ;;
    tester)   printf '%s' "${CCROLE_CLI:-${CCROLE_TESTER_CLI:-agy}}" ;;
    monitor)  printf '%s' "${CCROLE_CLI:-${CCROLE_MONITOR_CLI:-claude}}" ;;
    aide)     printf '%s' "${CCROLE_CLI:-${CCROLE_AIDE_CLI:-agy}}" ;;
  esac
}

die() { printf '%s\n' "$*" >&2; exit 1; }

check_role() {
  for r in $VALID; do [ "$1" = "$r" ] && return 0; done
  die "unknown role '$1' — one of: $VALID"
}

# Auto-configure core.hooksPath to .githooks if .githooks exists (#334)
for dir in "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)" "$(git rev-parse --show-toplevel 2>/dev/null || true)"; do
  if [ -n "$dir" ] && [ -d "$dir/.githooks" ]; then
    if [ "$(git -C "$dir" config --get core.hooksPath 2>/dev/null || true)" != ".githooks" ]; then
      git -C "$dir" config core.hooksPath .githooks
    fi
  fi
done

case "${1:-}" in
  assign)
    [ $# -eq 3 ] || die "usage: ccrole.sh assign <session-id> <role>"
    check_role "$3"
    mkdir -p "$ROLE_DIR"
    printf '%s\n' "$3" > "$ROLE_DIR/$2"
    echo "session $2 is now the $3"
    ;;
  revoke)
    [ $# -eq 2 ] || die "usage: ccrole.sh revoke <session-id>"
    rm -f "$ROLE_DIR/$2"
    echo "session $2 has no role; its GitHub writes will be refused"
    ;;
  list)
    mkdir -p "$ROLE_DIR"
    found=0
    for f in "$ROLE_DIR"/*; do
      [ -e "$f" ] || continue
      found=1
      printf '%-40s %s\n' "$(basename "$f")" "$(cat "$f")"
    done
    [ "$found" = 1 ] || echo "no sessions have been assigned a role"
    ;;
  cli)
    for r in $VALID; do printf '%-10s %s\n' "$r" "$(cli_for "$r")"; done
    ;;
  ""|-h|--help)
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
    ;;
  *)
    check_role "$1"
    role="$1"; shift
    [ "${1:-}" = "--" ] && shift
    cli="$(cli_for "$role")"
    command -v "$cli" >/dev/null 2>&1 || die \
      "'$cli' is not on PATH. Set CCROLE_CLI or CCROLE_$(printf '%s' "$role" | tr '[:lower:]' '[:upper:]')_CLI to pick another."
    # Which CLI is running a role decides how its output should be read, and the
    # session is about to replace this shell — so say it once, on stderr, before
    # there is nothing left to say it to.
    printf '%s session on %s\n' "$role" "$cli" >&2
    # Pinned rather than left to agy's default, because the trial is of a named
    # model: "the coder got slower" has to mean the CLI, not whichever model the
    # CLI happened to pick that week. `agy models` lists the spellings, and it
    # only tells the truth once signed in.
    #
    # Per-role, because the two agy roles stopped being comparable on 2026-09-11.
    # The coder reads a diff and runs a deploy; the reviewer analyses, writes the
    # code through subagents, and signs the verdicts. Raise one without raising
    # the other:  CCROLE_AGY_MODEL_REVIEWER=<model> ccrole reviewer
    if [ "$cli" = "agy" ]; then
      role_model="CCROLE_AGY_MODEL_$(printf '%s' "$role" | tr '[:lower:]' '[:upper:]')"
      eval "model=\${$role_model:-\${CCROLE_AGY_MODEL:-gemini-3.8-flash-high}}"
      # --add-dir attaches the repository root so agy discovers and loads workspace
      # customizations (.agents/hooks.json) in headless/--print mode (#643).
      root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
      set -- --add-dir "$root" --model "$model" "$@"
    fi
    # The monitor on claude reads and commits .claude/ at the repository root,
    # which sits outside the project directory it is started from.
    if [ "$cli" = "claude" ]; then
      set -- --add-dir "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" "$@"
    fi
    # Skip the per-call approval prompts, on the owner's word (2026-09-25): a
    # tester started by hand stalled on them. role_guard is the gate, and it is
    # a hook, so it still runs. ccloop.sh passes the flag itself, so add it
    # only when it is not there already.
    case "$cli" in
      agy|claude)
        skip=1
        for a in "$@"; do [ "$a" = "--dangerously-skip-permissions" ] && skip=0; done
        [ "$skip" = 1 ] && set -- --dangerously-skip-permissions "$@"
        ;;
    esac
    # Exported, so the guard sees it and so do any subagents this session spawns.
    CC_ROLE="$role" exec "$cli" "$@"
    ;;
esac
