#!/usr/bin/env python3
"""Cases for triage_gate.py. Run it: `uv run --no-project python triage_gate_test.py`.

A `Stop` hook that refuses has one failure mode the `PreToolUse` guard does not: if
it refuses when it should not, or if it cannot be made to stop refusing, the session
never ends its turn. `role_guard.py` shipped exactly that class of defect on its
first day — `{}` meant *no opinion* to Claude Code and *refusal* to agy, which
blocked the coder on its own first command (#268). On a `Stop` hook the same
mistake is a wedge rather than a blocked command, so the allow path is asserted for
both hosts before anything else.

Like the guard's suite this drives the hook as a subprocess with a payload on
stdin, so it tests the wire contract rather than the functions. `gh` is faked by a
script on `PATH`, which keeps the real subprocess, argument and JSON-parsing path
under test — the parts most likely to be wrong — without touching GitHub.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

HOOK = pathlib.Path(__file__).with_name("triage_gate.py")

CLAUDE, AGY = "claude", "agy"

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


def rows(numbers: list[int], p0: set[int] | None = None) -> list[dict]:
    """The shape the hook's jq produces: a number and whether it carries P0."""
    p0 = p0 or set()
    return [{"n": n, "p0": n in p0} for n in numbers]


def fake_gh(
    tmp: pathlib.Path,
    triage: list[dict],
    ready: list[dict],
    fail: bool = False,
) -> None:
    """A `gh` that answers the gate's two queries and nothing else.

    It branches on `stage:triage` / `stage:ready` appearing in the URL, which is how
    the real call is distinguished, so an argument-order change in the hook shows up
    here as an empty answer rather than passing quietly.
    """
    body = (
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  label) echo '[{\"name\":\"ai-terminal\",\"description\":\"the app (2026-0906-raspi-ai-terminal)\"}]' ;;\n"
        "  api)\n"
        "    case \"$*\" in\n"
        f"      *stage:triage*) echo '{json.dumps(triage)}' ;;\n"
        f"      *stage:ready*) echo '{json.dumps(ready)}' ;;\n"
        "      *) echo '[]' ;;\n"
        "    esac ;;\n"
        "esac\n"
    )
    if fail:
        body = "#!/bin/sh\necho 'gh: something broke' >&2\nexit 1\n"
    gh = tmp / "gh"
    gh.write_text(body)
    gh.chmod(0o755)


def run(
    role: str = "reviewer",
    host: str = CLAUDE,
    triage: list[int] | None = None,
    ready: list[int] | None = None,
    p0: set[int] | None = None,
    extra: dict | None = None,
    session: str = "s1",
    state: pathlib.Path | None = None,
    gh_fails: bool = False,
) -> tuple[str, str]:
    """Returns (verdict, reason), verdict in {"allow", "refuse"}."""
    payload: dict = {"session_id": session} if host == CLAUDE else {"conversationId": session}
    payload.update(extra or {})

    with tempfile.TemporaryDirectory() as bindir:
        binpath = pathlib.Path(bindir)
        fake_gh(binpath, rows(triage or [], p0), rows(ready or []), fail=gh_fails)

        env = dict(os.environ)
        env["PATH"] = f"{binpath}{os.pathsep}{env['PATH']}"
        env["TMPDIR"] = str(state) if state else bindir
        if role:
            env["CC_ROLE"] = role
        else:
            env.pop("CC_ROLE", None)

        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True, text=True, env=env, check=True,
            cwd=str(pathlib.Path(__file__).resolve().parents[2]),
        )

    out = json.loads(proc.stdout or "{}")
    return ("refuse" if out.get("decision") == "block" else "allow"), out.get("reason", "")


# --------------------------------------------------------------------------- #
# The allow path, on both hosts.
#
# Claude Code reads `{}` as no opinion and agy reads it as a refusal, so the two
# hosts need different words for the same verdict. Getting that backwards on a Stop
# hook means a session that cannot end its turn, which is strictly worse than the
# failure the hook exists to catch.
# --------------------------------------------------------------------------- #


def test_host_shapes() -> None:
    with tempfile.TemporaryDirectory() as bindir:
        binpath = pathlib.Path(bindir)
        fake_gh(binpath, [], [])
        env = dict(os.environ)
        env["PATH"] = f"{binpath}{os.pathsep}{env['PATH']}"
        env["TMPDIR"] = bindir
        env["CC_ROLE"] = "coder"

        for host, payload, want in (
            (CLAUDE, {"session_id": "h"}, {}),
            (AGY, {"conversationId": "h"}, {"decision": "stop"}),
        ):
            proc = subprocess.run(
                [sys.executable, str(HOOK)], input=json.dumps(payload),
                capture_output=True, text=True, env=env, check=True,
            )
            check(f"{host}: allowing a stop", json.loads(proc.stdout or "null"), want)


def test_only_the_reviewer() -> None:
    deep = list(range(1, 9))
    for role in ("coder", "monitor", "tester", ""):
        verdict, _ = run(role=role, triage=deep)
        check(f"role {role or '<unset>'} is not held by the gate", verdict, "allow")
    verdict, _ = run(role="reviewer", triage=deep)
    check("the reviewer is", verdict, "refuse")


# --------------------------------------------------------------------------- #
# The threshold is trigger 3's: "More than three unblocked P0/P1 sit at
# stage:triage", and the gate fires "until it is back under three". Off by one in
# either direction and the hook stops agreeing with the prose it enforces.
# --------------------------------------------------------------------------- #


def test_threshold() -> None:
    for n, want in ((0, "allow"), (3, "allow"), (4, "refuse"), (9, "refuse")):
        verdict, _ = run(triage=list(range(1, n + 1)))
        check(f"{n} unblocked P0/P1 at stage:triage", verdict, want)


def test_p0_outranks_the_threshold() -> None:
    """Trigger 1 fires at a count trigger 3 cannot reach.

    A single P0 at triage is one issue, so the depth test is silent on exactly the
    case *Feeding the queue* ranks highest — and a P0 there is usually an ordering
    the owner set by hand, which is the worst kind to lose to a threshold. #459 on
    2026-09-12 is the case this was written for.
    """
    verdict, reason = run(triage=[459], p0={459})
    check("one unblocked P0 at triage refuses", verdict, "refuse")
    check("the refusal names it", "#459" in reason, True)
    check("and says which trigger", "Trigger 1" in reason, True)
    check("and does not tell it to fan out wide", "one message" in reason, False)

    verdict, _ = run(triage=[459], p0=set())
    check("the same issue at P1 does not", verdict, "allow")

    # Depth and a P0 together: the P0 instruction wins, because a session told to
    # both fan out and prioritise will do the wide thing.
    _, reason = run(triage=[449, 452, 454, 459], p0={459})
    check("a P0 inside a deep queue still gets trigger 1", "Trigger 1" in reason, True)
    check("and the depth is still reported", "triage P0/P1  4" in reason, True)


def test_reason_names_the_issues() -> None:
    _, reason = run(triage=[446, 449, 452, 453])
    for want in ("#446", "#449", "#452", "#453", "triage P0/P1  4"):
        check(f"refusal carries {want}", want in reason, True)
    check("refusal asks for one subagent each", "one message" in reason, True)
    check("refusal names the real bound", "#275" in reason, True)


def test_ready_count_is_reported_not_gated() -> None:
    """A full stage:ready does not excuse a full stage:triage, and vice versa.

    The whole finding behind this hook is that the reviewer fills implementer slots
    from `stage:ready` and never reaches the triage gate. A gate that went quiet
    because `stage:ready` had work in it would reproduce that exactly.
    """
    verdict, reason = run(triage=list(range(1, 9)), ready=list(range(100, 130)))
    check("a deep ready queue does not silence the gate", verdict, "refuse")
    check("ready is reported", "ready  P0/P1  30" in reason, True)


# --------------------------------------------------------------------------- #
# Everything below is about not wedging the session.
# --------------------------------------------------------------------------- #


def test_stop_hook_active() -> None:
    """Claude Code sets this while the turn is only still alive because of a block.

    Honouring it is the difference between one refusal and a loop.
    """
    verdict, _ = run(triage=list(range(1, 9)), extra={"stop_hook_active": True})
    check("stop_hook_active is honoured", verdict, "allow")


def test_subagents_pass_through() -> None:
    """The refusal tells its reader to fan out subagents, so it must not catch them.

    Claude Code raises `SubagentStop` for a child and is safe by construction; agy
    registers `Stop` and nothing else, and its conversations carry these fields.
    """
    deep = list(range(1, 9))
    for marker in (
        {"parentConversationId": "p1"},
        {"parent_conversation_id": "p1"},
        {"subagentName": "analysis"},
        {"subagent_name": "analysis"},
        {"agentType": "subagent"},
        {"mainConversationId": "m1"},
    ):
        verdict, _ = run(host=AGY, triage=deep, extra=marker)
        check(f"a child with {next(iter(marker))} is not held", verdict, "allow")

    check("an empty marker is not a child", run(host=AGY, triage=deep,
                                                extra={"parentConversationId": ""})[0], "refuse")


def test_trigger4_sighting_history() -> None:
    with tempfile.TemporaryDirectory() as state:
        statedir = pathlib.Path(state)
        # Tick 1 & 2: sighting count builds up, allow stop
        for tick in (1, 2):
            verdict, _ = run(triage=[660], session=f"tick-{tick}", state=statedir)
            check(f"tick {tick} allows stop", verdict, "allow")
            age_blocks(statedir, f"tick-{tick}")

        # Tick 3: count reaches 3, Trigger 4 blocks stop naming the issue
        verdict, reason = run(triage=[660], session="tick-3", state=statedir)
        check("tick 3 blocks stop", verdict, "refuse")
        check("tick 3 reason mentions Trigger 4", "Trigger 4" in reason, True)
        check("tick 3 reason names issue #660", "#660" in reason, True)

        age_blocks(statedir, "tick-3")
        # Issue drained from stage:triage; stop is allowed again
        verdict, _ = run(triage=[], session="tick-4", state=statedir)
        check("tick 4 allows stop after issue drained", verdict, "allow")


def test_stands_down_after_three() -> None:
    deep = list(range(1, 9))
    with tempfile.TemporaryDirectory() as state:
        statedir = pathlib.Path(state)
        seen = []
        for _ in range(5):
            verdict, _ = run(triage=deep, session="stand-down", state=statedir)
            seen.append(verdict)
            age_blocks(statedir, "stand-down")
        check(
            "three refusals then stand down, then the count resets",
            seen,
            ["refuse", "refuse", "refuse", "allow", "refuse"],
        )


def test_cooldown_suppresses_a_bouncing_turn() -> None:
    """Two Stops within 45 s are one turn bouncing, not two ticks."""
    deep = list(range(1, 9))
    with tempfile.TemporaryDirectory() as state:
        statedir = pathlib.Path(state)
        first, _ = run(triage=deep, session="bounce", state=statedir)
        second, _ = run(triage=deep, session="bounce", state=statedir)
        check("first Stop refuses", first, "refuse")
        check("an immediate second Stop does not", second, "allow")


def test_fails_open() -> None:
    verdict, _ = run(triage=list(range(1, 9)), gh_fails=True)
    check("a broken gh allows the stop", verdict, "allow")

    with tempfile.TemporaryDirectory() as bindir:
        binpath = pathlib.Path(bindir)
        fake_gh(binpath, [], [])
        env = dict(os.environ)
        env["PATH"] = f"{binpath}{os.pathsep}{env['PATH']}"
        env["TMPDIR"] = bindir
        env["CC_ROLE"] = "reviewer"
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input="not json at all",
            capture_output=True, text=True, env=env, check=True,
        )
        check("a bad payload allows the stop", json.loads(proc.stdout or "null"), {})


def age_blocks(state: pathlib.Path, session: str) -> None:
    """Push the cooldown timestamp into the past without pushing the count back."""
    f = state / "rev-triage-gate" / f"blocks-{session}"
    try:
        n, ts = f.read_text().split()
        f.write_text(f"{n} {float(ts) - 100}")
    except OSError:
        pass


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all cases pass ({len(tests)} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
