#!/usr/bin/env python3
"""Stop hook: the reviewer does not go back to sleep on a backed-up triage queue.

The reviewer's skill already carries the fan-out doctrine at length — a target of
ten, a refill rule, a named failure mode, and four triggers that decide when the
triage gate fires. None of it is enforced by anything. The skill says so in its
own words (`issue-looping-reviewer/SKILL.md`, *Feeding the queue*): "neither has
an alarm on it: nothing goes red when you skip them, the verify queue just keeps
looking busy."

Measured 2026-09-12, which is why this exists. Between 07:05Z and 07:30Z six
issues were filed — #446, #447, #448, #449, #450, #452, five of them unblocked
P1. At 07:30Z every one of them still had zero reviewer comments, `stage:ready`
held two P2 leftovers, and the reviewer had produced exactly one analysis (#445
at 07:16Z) and one pull request (#451 at 07:26Z) all day: a pipeline of width
one. Three of the gate's four triggers were firing the whole time.

The mechanism is not that the reviewer had not read the doctrine. Reading it in
order produces this outcome. The implementing fan-out's pick query filters on
`stage:ready`, everything newly filed lands on `stage:triage`, and implementers
are told to take slots first — so the session fills slots from an empty queue,
arrives at the triage gate with no number attached to it, and dispatches
conservatively. `TimerCondition="any"` then makes width-one self-sustaining: one
subagent out, one wake when it returns, refill from an empty `stage:ready`,
re-arm, sleep.

So the number lives on the queue that is empty and the queue that is full has no
number. That is a property of where the counting happens, and the shared skill's
own conclusion applies: **making the convention louder inherits the defect;
moving it into the harness does not.** The same file records that this exact
failure was self-diagnosed and re-documented three times on the morning of
2026-09-12. This is the fourth occurrence and the first one that is not a
paragraph.

**What this is not.** It is not a scheduler and it does not dispatch anything. It
reads the triage and ready queues at the end of a tick and refuses to let the turn
end on either of the gate's two countable triggers, with the issue numbers in the
refusal. Everything about *how* to analyse them stays in the skill.

The three it enforces, in the order it checks them:

1. **Trigger 1** — an unblocked P0 sits at `stage:triage`. One issue is enough,
   so the depth test below never sees it, and the ordering a P0 expresses is
   usually one the owner set by hand. Added 2026-09-12, when the owner ruled
   #459 ahead of the product queue and the only thing carrying that ruling was a
   paragraph.
4. **Trigger 4** — an unblocked P0/P1 has sat at `stage:triage` across three ticks,
   tracked in `sightings.json`. Catches starvation that does not reach trigger 3's
   depth threshold (#662).
3. **Trigger 3** — more than `THRESHOLD` unblocked P0/P1 sit at `stage:triage`.

Trigger 2 is comparative — it ranks triage against the startable queue — and stays
in the skill.

It fails open in every direction: no role, wrong role, no `gh`, a `gh` error, a
timeout, a bad payload, or any unhandled exception all allow the stop. A hook that
wedges the reviewer would cost far more than the gap it closes, and the failure it
guards against is one the next tick gets another chance at.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from role_guard import resolve_role, sniff_deny  # noqa: E402

# Trigger 3 in the reviewer's *Feeding the queue*: "More than three unblocked
# P0/P1 sit at `stage:triage`." The gate keeps firing "until it is back under
# three", so the hook blocks while the count is strictly greater than three and
# lets the turn end at three or fewer. Same number, same direction, so the hook
# and the prose cannot drift into disagreeing.
THRESHOLD = 3

# Belt and braces against a wedge. `stop_hook_active` already stops Claude Code
# re-entering, but it is one host's field and a hook that can loop is not worth
# the finding it reports. After this many consecutive blocks for one session the
# gate stands down and says so, leaving the backlog to the monitor — which is the
# role whose whole unit of work is the backlog anyway.
MAX_CONSECUTIVE = 3

# A tick that was genuinely blocked and did the work still ends within seconds of
# the dispatch, because dispatching is one message. Anything faster than this is
# the same turn bouncing, not a second tick.
COOLDOWN_S = 45

# Number of ticks an unblocked P0/P1 issue can sit at stage:triage before Trigger 4
# fires and blocks stop (#662).
SIGHTINGS_THRESHOLD = 3

STATE_DIR = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "rev-triage-gate"

GH_TIMEOUT_S = 20

# `[.[] | ...]` first: a bare `.[]` against this endpoint is the mistake that
# makes the filter silently return nothing on an empty page.
COUNT_Q = (
    "[.[] | select(.pull_request|not)"
    " | select(.issue_dependencies_summary.blocked_by==0)"
    ' | select([.labels[].name] | index("on hold") | not)'
    ' | select([.labels[].name] | any(test("^P[01]$")))'
    ' | {n: .number, p0: ([.labels[].name] | index("P0") != null)}]'
    " | @json"
)


def emit(decision: str | None, reason: str = "", deny_word: str = "block") -> None:
    """Answer in the host's vocabulary and exit.

    `decision=None` means *let the turn end*. Claude Code takes an empty object
    for that; agy does not — it reads `{}` as a refusal, which is how the guard
    blocked the coder's first command on 2026-09-11. On a Stop hook that same
    mistake would not refuse one command, it would refuse to ever stop. So agy
    always gets an explicit word: `stop` to allow termination, `block` to keep
    going. (Its enum is `stop` / `continue` / `block`; Claude Code's is `block`
    with everything else meaning no opinion. `block` is the one word that means
    the same thing on both, which is why the refusal needs no branch.)
    """
    if decision is None:
        print(json.dumps({"decision": "stop"} if deny_word == "deny" else {}))
    else:
        print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def app_label(cwd: pathlib.Path) -> str:
    """The label whose description names this directory, cached after the first look.

    `references/labels.md` makes the description the link between directory and
    label, so the lookup is exact rather than a guess at the naming convention.
    It costs one `gh` read, once: label descriptions do not change, and this hook
    runs at the end of every tick.

    Returns "" from the repo root, where there is no single app to filter on. The
    caller counts every `stage:triage` issue in that case, which is the honest
    answer to "is triage backed up" when the question was not asked about an app.
    """
    basename = cwd.name
    if basename == "alex-workspace":
        return ""

    cache = STATE_DIR / f"applabel-{re.sub(r'[^A-Za-z0-9_.-]', '_', basename)}"
    try:
        return cache.read_text().strip()
    except OSError:
        pass

    label = ""
    try:
        out = subprocess.run(
            ["gh", "label", "list", "--limit", "200", "--json", "name,description"],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=True,
        ).stdout
        for row in json.loads(out):
            if basename in (row.get("description") or ""):
                label = row.get("name", "")
                break
    except Exception:  # noqa: BLE001
        return ""

    if label:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(label)
        except OSError:
            pass
    return label


def unblocked_p01(stage: str, label: str) -> list[dict]:
    """The gate's own query, in the gate's own shape — see *Feeding the queue*.

    Unblocked, not `on hold`, P0 or P1, issues only. Raises on anything unexpected
    so the caller can fail open rather than act on a number it did not get.

    Each row is `{"n": <number>, "p0": <bool>}`. The priority comes back with the
    number because trigger 1 and trigger 3 read the same page and asking twice
    would double the reads for a field already in the response.
    """
    labels = f"{label},stage:{stage}" if label else f"stage:{stage}"
    out = subprocess.run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/issues?state=open&labels={labels}&per_page=100",
         "-q", COUNT_Q],
        capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=True,
    ).stdout.strip()
    return json.loads(out) if out else []


def consecutive(session: str) -> tuple[int, float]:
    try:
        n, ts = (STATE_DIR / f"blocks-{session}").read_text().split()
        return int(n), float(ts)
    except Exception:  # noqa: BLE001
        return 0, 0.0


def record(session: str, n: int) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / f"blocks-{session}").write_text(f"{n} {time.time()}")
    except OSError:
        pass


def sightings_path() -> pathlib.Path:
    return STATE_DIR / "sightings.json"


def load_sightings() -> dict[str, int]:
    try:
        return json.loads(sightings_path().read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_sightings(sightings: dict[str, int]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        sightings_path().write_text(json.dumps(sightings))
    except OSError:
        pass


def update_sightings(triage: list[dict]) -> dict[str, int]:
    """Track tick counts for unblocked P0/P1 issues at stage:triage.

    Issues that have left stage:triage are pruned from sightings.json.
    """
    sightings = load_sightings()
    current_nums = {str(r["n"]) for r in triage}
    updated = {
        num: sightings.get(num, 0) + 1
        for num in current_nums
    }
    save_sightings(updated)
    return updated


def nums(rows: list[dict]) -> list[int]:
    return [r["n"] for r in rows]


def reason_p0(p0: list[int], triage: list[dict], ready: list[dict], label: str) -> str:
    """Trigger 1: an unblocked P0 at triage goes ahead of everything.

    Separate from the depth refusal because it is a different instruction. Depth
    says *fan out across the whole queue*; this says *one issue outranks the
    queue*, and a session told both at once will do the wide thing, which is the
    cheaper of the two to feel good about.

    It also fires at a count the depth rule never reaches. A single P0 at triage
    is one issue, so `len(triage) > 3` is silent on exactly the case the skill
    ranks highest — and the ordering it enforces is usually one the owner set by
    hand, which is the worst kind to lose to a threshold.
    """
    listed = ", ".join(f"#{n}" for n in p0)
    scope = f"`{label}` " if label else ""
    return (
        f"{listed} is an unblocked **P0** at {scope}`stage:triage`. Trigger 1 in "
        "*Feeding the queue*: a P0 at triage is dispatched ahead of everything, "
        "whatever else the queue holds. Do not end the tick here.\n\n"
        f"    triage P0     {len(p0)}   {listed}\n"
        f"    triage P0/P1  {len(triage)}\n"
        f"    ready  P0/P1  {len(ready)}\n\n"
        "**Dispatch an analysis subagent for it now**, before anything else in "
        "this tick. A P0 at triage usually means something is blocking other "
        "issues — check `dependencies/blocking` and say in the analysis what is "
        "waiting on it.\n\n"
        "Analysis is read-only: no worktree, no sweep, no merge skew. The "
        "implementing cap of ten does not apply and nothing about this issue has "
        "to wait for an implementer slot to free up.\n\n"
        "It is drained when the label moves to `stage:ready`, not when a comment "
        "lands. You have already re-armed if you got this far — dispatch and let "
        "the turn end."
    )


def reason_trigger4(stalled: list[int], triage: list[dict], ready: list[dict], label: str) -> str:
    """Trigger 4: an unblocked P0/P1 has sat at stage:triage across three ticks.

    It goes out first and ahead of the verify queue. Unlike trigger 3 which fans out
    across the whole triage queue when depth exceeds threshold, trigger 4 catches
    individual issues starving across multiple ticks (#662).
    """
    listed = ", ".join(f"#{n}" for n in stalled)
    all_triage = ", ".join(f"#{n}" for n in nums(triage))
    scope = f"`{label}` " if label else ""
    return (
        f"{listed} has sat at {scope}`stage:triage` across three ticks. Trigger 4 in "
        "*Feeding the queue*: an unblocked P0/P1 at triage across three ticks goes out "
        "first and ahead of the verify queue. Do not end the tick here.\n\n"
        f"    stalled P0/P1 {len(stalled)}   {listed}\n"
        f"    triage  P0/P1 {len(triage)}   {all_triage}\n"
        f"    ready   P0/P1 {len(ready)}\n\n"
        "**Dispatch an analysis subagent for each stalled issue now**, ahead of the "
        "verify queue. An unblocked issue sitting across ticks is work starving.\n\n"
        "Analysis is read-only: no worktree, no sweep, no merge skew. The "
        "implementing cap of ten does not apply and nothing about this issue has "
        "to wait for an implementer slot to free up.\n\n"
        "It is drained when the label moves to `stage:ready`, not when a comment "
        "lands. You have already re-armed if you got this far — dispatch and let "
        "the turn end."
    )


def reason_text(triage: list[dict], ready: list[dict], label: str) -> str:
    listed = ", ".join(f"#{n}" for n in nums(triage))
    scope = f"`{label}` " if label else ""
    return (
        f"{len(triage)} unblocked P0/P1 issues are sitting at {scope}`stage:triage`, "
        f"which is over the gate's threshold of {THRESHOLD}. Do not end the tick here.\n\n"
        f"    triage P0/P1  {len(triage)}   {listed}\n"
        f"    ready  P0/P1  {len(ready)}\n\n"
        "**Dispatch one analysis subagent per issue on that list, all in one message, "
        "now.** Not three, not whatever is left over from the implementing cap — one "
        "each, in parallel. The owner's ruling of 2026-09-12 is that this queue gets as "
        "many subagents at once as it has issues.\n\n"
        "The implementing cap of ten does not bind here and the arithmetic behind it "
        "says why: ten was sized by the serial merge sweep queueing behind "
        "`/tmp/rev-sweep.lock` and by the merge skew of many worktrees branched from one "
        "`origin/main`. An analysis subagent takes no worktree, runs no sweep and creates "
        "no skew. It is read-only until it writes its verdict.\n\n"
        "What does bind is #275: each analysis finishes with a comment plus two label "
        "edits, so a wide fan-out lands a burst of content-creation writes and GitHub's "
        "*secondary* rate limit — the one `gh api rate_limit` does not report — is what "
        "answers. Tell each subagent to write its comment and its label change and stop.\n\n"
        "An issue is drained when the label moves to `stage:ready`, not when a comment "
        "lands. A tick that posts analysis and leaves `stage:triage` set has bought "
        "nothing: the implementing query still cannot see it.\n\n"
        "You have already re-armed for this tick if you got this far, so dispatch and let "
        "the turn end — do not schedule a second timer."
    )


def is_subagent(payload: dict) -> bool:
    """Anything that says this turn belongs to a child, not to the tick.

    Claude Code is safe without this — a subagent finishing raises `SubagentStop`,
    a different event. `agy` is the risk: its hook registry exposes `Stop` and no
    subagent variant, and its conversation messages carry `parentConversationId`,
    `subagentName` and `agentType`, so a child conversation ending plausibly lands
    here too.

    Getting this wrong is worse than the failure the gate exists to catch, and it
    fails at the worst possible moment: the refusal tells its reader to fan out
    analysis subagents, so a gate that caught its own children would block each of
    them, the first time a fan-out ever happened. The markers are checked by name
    and the answer on any of them is to stand aside.
    """
    keys = (
        "parent_conversation_id", "parentConversationId",
        "subagent_name", "subagentName",
        "agent_type", "agentType",
        "main_conversation_id", "mainConversationId",
    )
    return any(str(payload.get(k) or "").strip() for k in keys)


def main(raw: str, deny_word: str) -> None:
    payload = json.loads(raw) if raw.strip() else {}

    # Claude Code sets this while the session is only still running because a Stop
    # hook blocked it. Re-blocking there is how a Stop hook becomes a loop.
    if payload.get("stop_hook_active"):
        emit(None, deny_word=deny_word)

    if is_subagent(payload):
        emit(None, deny_word=deny_word)

    session = str(
        payload.get("session_id") or payload.get("conversationId") or ""
    ) or "unknown"
    session = re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:120]

    if resolve_role(session) != "reviewer":
        emit(None, deny_word=deny_word)

    n, ts = consecutive(session)
    if n >= MAX_CONSECUTIVE:
        record(session, 0)
        emit(None, deny_word=deny_word)
    if time.time() - ts < COOLDOWN_S:
        emit(None, deny_word=deny_word)

    label = app_label(pathlib.Path.cwd())
    triage = unblocked_p01("triage", label)
    ready = unblocked_p01("ready", label)

    # Update sightings history for Trigger 4 across ticks
    sightings = update_sightings(triage)

    # Trigger 1 before trigger 4 & 3: a P0 outranks the queue's depth, and at a
    # count the depth test cannot see. One P0 alone never reaches THRESHOLD.
    p0 = [r["n"] for r in triage if r.get("p0")]
    if p0:
        record(session, n + 1)
        emit("block", reason_p0(p0, triage, ready, label), deny_word=deny_word)

    # Trigger 4: an unblocked P0/P1 has sat at stage:triage across three ticks.
    stalled = [r["n"] for r in triage if sightings.get(str(r["n"]), 0) >= SIGHTINGS_THRESHOLD]
    if stalled:
        record(session, n + 1)
        emit("block", reason_trigger4(stalled, triage, ready, label), deny_word=deny_word)

    if len(triage) <= THRESHOLD:
        record(session, 0)
        emit(None, deny_word=deny_word)

    record(session, n + 1)
    emit("block", reason_text(triage, ready, label), deny_word=deny_word)


if __name__ == "__main__":
    try:
        RAW = sys.stdin.read()
    except Exception:  # noqa: BLE001
        RAW = ""
    DENY = sniff_deny(RAW)

    try:
        main(RAW, DENY)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # Fail open, loudly. A reviewer that cannot stop is worse than a reviewer
        # that stops with work outstanding, because the second one wakes up in
        # 270 seconds and gets another chance.
        print(json.dumps({"decision": "stop"} if DENY == "deny" else {}))
        print(f"triage_gate error, allowing stop: {exc}", file=sys.stderr)
