#!/usr/bin/env python3
"""What the planner was handed, over conversations that really happened.

Every number that decides what the planner sees is a guess written down in a
comment. `RECENT` and `RECENT_CHARS` (`plan.py:179-180`) bound the conversation
it is told about; `PAD`, `PAD_CHARS` and `PAD_LINE` (`_plan_run.py:138-140`)
bound the findings it carries between plans, and that comment says outright
that it is estimated and "wants a week of real pads before either moves". There
was nowhere to put such a week. `gws_bench.py` counts what one Workspace
question costs and `latency_report.py` counts where the seconds go; nothing at
all looked at the planner, so an argument about those five numbers was an
argument about nobody's data.

This is the store the server has been writing all along. `SESSIONS_DB` holds
every conversation this terminal has had, events and all, and `_recent` is a
pure function of an event list — so what the planner *would* have been handed,
at every moment a tool actually went out, can be worked out after the fact
without asking anybody anything.

**Read what this can and cannot conclude before you act on it.** The replay
measures the planner's input. It cannot tell a better plan from a worse one: no
model runs, nothing is scored, and a paragraph twice as long is reported as
twice as long and not as twice as good. What it settles is the other half of
the question, which is the half that is cheap and was still unanswered — how
often each cap is the one doing the cutting, how much of the conversation goes
over the side when it does, and how much of the *other* budget is left unspent
at that moment. A cap that never binds cannot be worth moving, and that is a
decision this makes on its own. A cap that binds at four dispatch points in
five is a *hypothesis* that the planner is being starved, and it takes `--live`
and somebody's credentials to test it.

    uv run python tools/plan_bench.py                  # the store, at today's caps
    uv run python tools/plan_bench.py --recent 30      # ... and what 30 would change
    uv run python tools/plan_bench.py --db /tmp/x.db   # some other store
    uv run python tools/plan_bench.py --live -n 3      # dispatch real plans

**Nothing in replay mode prints anything anybody said.** The store is the
household's own conversations — the whole of `sessions.db` is one line in
`.gitignore` for that reason — and a bench whose output cannot be pasted into a
comment is a bench nobody pastes. So every figure here is a count or a length,
there is no verbose mode on this half, and `replay_check.py` pins that by
running this over a store full of a marker string and looking for the marker in
the output. It also means the answer to "can a fixture live in the repo" is no,
and does not need to be yes: the store on the machine is the corpus.

`--live` is the other half and it is not this one. It dispatches real plans
through `build_plan_agent()` on a plain `Runner`, the shape `gws_bench.py`
drives the Workspace sub-agent in, and reports four things that can be counted
rather than judged: turns off the budget the enforcement itself uses
(`_plan_run.budget`), how many tools were called, how many of those calls asked
for something an earlier call in the same run already asked for, and which of
the five endings `plan._kind` already names. There is no rubric and no model
judging another model. The signal is the repeat count: `context` and `found`
exist so the planner does not run a lookup it has already run
(`plan.py:266-269`), so a cap that goes up and a repeat count that goes down is
a change that helped, and nothing had to have an opinion about prose to see it.

The tasks it runs are grounding-only on purpose. `gws` is one of the planner's
tools and it reads the owner's real calendar and mail, so a task phrased as
"my" would reach the real account; none of these are. A run still costs real
model time and real grounding calls.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
load_dotenv(REPO_ROOT / ".env", override=True)

from google.adk.events import Event  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from ai_terminal.live.tools import _plan_run  # noqa: E402
from ai_terminal.live.tools import plan as plan_mod  # noqa: E402

APP = "plan_bench"
USER = "bench"

# The store, defaulting the way `main.SESSIONS_DB` does but resolved against
# the repo root rather than the working directory. The server's own default is
# relative because it is started from the directory it should write into; a
# bench is run from wherever, and reading an empty store because of a `cd` is
# the one failure here that looks exactly like a clean result.
STORE = Path(os.getenv("SESSIONS_DB") or REPO_ROOT / "sessions.db")

# A cap high enough to be no cap. Both of `_recent`'s are compared against
# counts of turns and characters in one conversation, so anything past a
# billion is off the end of every conversation this terminal will ever hold,
# and passing it is how the uncapped paragraph is obtained without a second
# copy of `_recent` that has the caps taken out.
UNCAPPED = 1 << 30


class Talk(NamedTuple):
    """One recorded conversation, as the planner's inputs are built from it."""

    ident: str
    state: dict
    events: list


class Seen(NamedTuple):
    """What one dispatch would have handed the planner as `context`.

    `bound` is the cap you would have to raise to see one more turn — `turns`,
    `chars`, or `none` when the whole conversation already fits. It is the
    actionable one and it is not the same as "which caps are exceeded": late in
    a long conversation both are exceeded, and only one of them is the one
    doing the cutting.
    """

    lines: int
    chars: int
    all_lines: int
    all_chars: int
    bound: str


class Run(NamedTuple):
    """What one dispatched plan cost, in things that can be counted."""

    turns: int
    tools: int
    repeats: int
    seconds: float
    ending: str
    answer: str


# ------------------------------------------------------------------ the store

def opened(db: Path) -> sqlite3.Connection:
    """The store, read-only.

    `mode=ro` and plain `sqlite3` rather than `SqliteSessionService`, for two
    reasons that both matter when the file is the household's live one. The
    service opens the database read-write and migrates an old schema on the way
    in (`sqlite_session_service.py:644-652`), and a bench that can write to
    what it is measuring is one bad afternoon from being the reason a
    conversation went missing. Read-only also means this can run against a
    store the server has open.
    """
    if not db.exists():
        raise SystemExit(f"no session store at {db} — set SESSIONS_DB or --db")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _unreadable(ident: str, what: str) -> "SystemExit":
    """Stop, naming the conversation and quoting none of it.

    Every parser reached below reports what it rejected: a pydantic
    `ValidationError` carries `input_value=` and pydantic's JSON errors carry
    the head of the document. Here the document is somebody's speech, and this
    file's promise is that its output can be pasted into a comment. So no
    parser's message is ever allowed out — the row is identified by the id the
    service generated and the reader goes and looks.

    A row this release cannot read is not hypothetical. `_plan_run.held`
    already tolerates a pad written by an earlier one, and `replay_check`
    builds its stores through the real service so that the reading does not go
    stale when ADK changes the schema. The day it changes is the day this
    raises.
    """
    return SystemExit(
        f"conversation {ident[:12]}: its {what} is not readable by this "
        f"release, so the store cannot be replayed. Nothing of it is printed "
        f"— the rows are conversations.")


def stored(conn: sqlite3.Connection) -> list[Talk]:
    """Every conversation in the store, oldest event first.

    `ORDER BY timestamp, id` is the service's own order read forwards: it
    selects `timestamp DESC, id DESC` and reverses, and the tie-break on `id`
    is deliberate there — without it a replayed conversation shuffles wherever
    two events share a timestamp (`sqlite_session_service.py:308-311`), and
    two events sharing a timestamp is what a fanned-out turn looks like.

    Only the session row's own state is read, not the app and user rows the
    service merges in. The pad is written unprefixed on purpose so that one
    conversation's findings are its own (`_plan_run.PAD_KEY`, #64), so the
    session row is where all of it is.
    """
    talks = []
    for row in conn.execute(
        "SELECT app_name, user_id, id, state FROM sessions "
        "ORDER BY create_time, id"
    ).fetchall():
        events = conn.execute(
            "SELECT event_data FROM events WHERE app_name=? AND user_id=? "
            "AND session_id=? ORDER BY timestamp, id",
            (row["app_name"], row["user_id"], row["id"]),
        ).fetchall()
        try:
            state = json.loads(row["state"] or "{}")
        except json.JSONDecodeError:
            raise _unreadable(row["id"], "state") from None
        try:
            replay = [Event.model_validate_json(e["event_data"])
                      for e in events]
        except Exception:
            raise _unreadable(row["id"], "event history") from None
        talks.append(Talk(ident=row["id"], state=state, events=replay))
    return talks


# ----------------------------------------------------------------- the replay

def dispatches(events) -> list[int]:
    """The indexes at which a tool went out, which is where a plan could have.

    Every event carrying a `function_call`, and nothing else. "After each
    person turn" was the obvious alternative and it invents dispatch points
    that never happened: the live model answers most turns out of its own head,
    and in a real store a good half of the user events carry no transcription
    to read at all. These are the moments it did decide a tool was needed,
    which is the population `plan` is drawn from.

    The context is `events[:i]`, the conversation *before* the call. Whether
    ADK has appended the call's own event by the time the tool body runs is not
    something this can see from a store, and the difference is one line reading
    `[called plan: task=...]` — the task the planner is already being handed in
    its own field. Excluded, as the reading that does not double it.
    """
    return [i for i, event in enumerate(events)
            if event.content and any(part.function_call
                                     for part in (event.content.parts or []))]


def turns_in(paragraph: str) -> int:
    """How many conversational turns a rendered `context` holds."""
    return len(paragraph.splitlines())


def seen(events, limit: int = plan_mod.RECENT,
         chars: int = plan_mod.RECENT_CHARS) -> Seen:
    """What `_recent` would hand the planner here, and which cap cut it.

    Four calls to the real `_recent` rather than one reimplementation of it
    with the caps parameterised out. The caps are already parameters, and a
    second copy of the merging and the soft last line is a second copy that
    drifts — at which point the bench reports on a function the server does not
    run.

    `bound` is found by raising the turn cap by exactly one. If that shows one
    more turn, turns are what is cutting; if it shows nothing new and there was
    something to show, the character budget is spent and raising the turn cap
    alone buys nothing. Both exhausted at once reads as `chars`, which is
    right — you would have to raise both.
    """
    got = plan_mod._recent(events, limit=limit, chars=chars)
    whole = plan_mod._recent(events, limit=UNCAPPED, chars=UNCAPPED)
    if got == whole:
        bound = "none"
    else:
        bound = ("turns"
                 if plan_mod._recent(events, limit=limit + 1, chars=chars) != got
                 else "chars")
    return Seen(lines=turns_in(got), chars=len(got),
                all_lines=turns_in(whole), all_chars=len(whole), bound=bound)


def replayed(talk: Talk, limit: int, chars: int) -> list[Seen]:
    """Every dispatch in one conversation, as the planner would have seen it."""
    return [seen(talk.events[:i], limit, chars) for i in dispatches(talk.events)]


def tally(rows: list[Seen]) -> dict:
    """The distribution worth reading off a replay.

    `spare` is the point of the whole exercise: the characters left in the
    other budget at a dispatch the turn cap cut. `plan.py:171-174` says the two
    caps are there because they fail differently, and the one that fails is
    supposed to be whichever runs out first. If the turn cap keeps running out
    with most of the character budget unspent, the paragraph the planner reads
    is short for a reason nobody chose.
    """
    if not rows:
        return {"points": 0}
    tight = [r for r in rows if r.bound == "turns"]
    return {
        "points": len(rows),
        "none": sum(1 for r in rows if r.bound == "none"),
        "turns": len(tight),
        "chars": sum(1 for r in rows if r.bound == "chars"),
        "empty": sum(1 for r in rows if not r.lines),
        "median_lines": statistics.median(r.lines for r in rows),
        "median_chars": statistics.median(r.chars for r in rows),
        "worst_lost": max(r.all_lines - r.lines for r in rows),
        "spare": (statistics.median(plan_mod.RECENT_CHARS - r.chars
                                    for r in tight) if tight else None),
    }


def moved(before: list[Seen], after: list[Seen]) -> dict:
    """How much a different pair of caps would change, dispatch by dispatch.

    Zero changed is a result and not a null one: it says the caps under test
    are not what is bounding this corpus, and that raising them is work with a
    measured effect of nothing.
    """
    pairs = [(a, b) for a, b in zip(before, after)
             if (a.lines, a.chars) != (b.lines, b.chars)]
    return {
        "changed": len(pairs),
        "of": len(before),
        "lines": sum(b.lines - a.lines for a, b in pairs),
        "chars": sum(b.chars - a.chars for a, b in pairs),
    }


# ----------------------------------------------------------------- a live run

def ending(answer: str) -> str:
    """What a plan ended as: `answer`, or one of the four ways it did not.

    `plan._kind` already names them and the card already draws by them, so the
    bench classifies an outcome with the same function the product does rather
    than with a rubric of its own. It is the one piece of "was this any good"
    available without a judge, and it is only the floor of it: it separates an
    answer from a non-answer and says nothing about whether the answer was
    right.
    """
    return plan_mod._kind(answer) or "answer"


def asked(events) -> list[str]:
    """Every tool call in a run, as a string two identical calls share.

    `lang` is dropped because every call carries the same one, and the
    arguments are sorted so that two calls the model wrote in a different order
    are one repeat and not two calls.
    """
    out = []
    for event in events:
        for part in (event.content.parts if event.content else None) or []:
            if not part.function_call:
                continue
            args = sorted((part.function_call.args or {}).items())
            body = ", ".join(f"{k}={v!r}" for k, v in args if k != "lang")
            out.append(f"{part.function_call.name}({body})")
    return out


def repeated(calls: list[str]) -> int:
    """Calls that ask for what an earlier call in the same run already asked.

    The mechanical half of "was this plan any good". A planner that has been
    told what it already found does not go and find it again, so this is what
    `found` and `context` are for, counted — and it is counted rather than
    judged, so a cap that goes up and this that goes down is an argument that
    does not need anybody to read the prose.
    """
    already: set[str] = set()
    again = 0
    for call in calls:
        if call in already:
            again += 1
        else:
            already.add(call)
    return again


# Three tasks, none of which says "my". Each needs more than one lookup, which
# is the whole precondition for `plan` (`plan.py:546-556`), and each carries a
# `found` a lookup is already answered by — so a run that spends a turn on it
# anyway is the failure `found` exists to stop, showing up in `repeats`.
TASKS = {
    "chain": {
        "task": "somewhere near Ebisu for dinner tonight, and whether they "
                "need an umbrella getting there",
        "context": "",
        "found": "",
    },
    "follow": {
        # The follow-up in `note_finding`'s own example (`_plan_run.py:310`).
        # The three places are on the pad, so the shortlist is a search the
        # planner does not have to run — if it runs it, `found` is not working.
        "task": "what about the second one?",
        "context": "person: somewhere in Kyoto for a ryokan next month\n"
                   "assistant: I found three.",
        "found": "- shortlist: Hiiragiya, Tawaraya, Yoshida-sanso",
    },
    "wide": {
        # Three lookups that need nothing from each other, which the
        # instruction asks for in one turn (`plan.py:202-206`). A run that
        # takes three turns over it is the fan-out rule not landing.
        "task": "the weather in Kyoto, Osaka and Nara for the weekend",
        "context": "",
        "found": "",
    },
}


async def dispatch(agent, task: dict) -> Run:
    """Run one plan and report what it cost.

    The only function in this file that needs credentials, and the only one
    that costs anything. Same shape as `gws_bench.ask`: the sub-agent on a
    plain `Runner`, no live agent and no audio in front of it, because those
    add a minute of setup and a lot of variance to numbers that do not depend
    on them.

    The turn count is read off the budget the enforcement itself uses rather
    than counted from the log, for the reason `_plan_run.budget` gives — the
    two cannot disagree that way. `budget()` is entered around the run because
    `on_model` is the agent's `before_model_callback` and looks the tally up in
    the context it is called in.
    """
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name=APP, user_id=USER)
    runner = Runner(app_name=APP, agent=agent, session_service=sessions)
    message = types.Content(role="user", parts=[types.Part.from_text(
        text=plan_mod.PlanRequest(
            task=task["task"], context=task["context"],
            found=task["found"], lang="en").model_dump_json())])

    answer, events = "", []
    started = time.monotonic()
    with _plan_run.budget() as spent:
        async for event in runner.run_async(
            user_id=USER, session_id=session.id, new_message=message
        ):
            events.append(event)
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text or "" for p in event.content.parts)
    calls = asked(events)
    return Run(turns=spent.turns, tools=len(calls), repeats=repeated(calls),
               seconds=time.monotonic() - started, ending=ending(answer),
               answer=answer.strip())


# ------------------------------------------------------------------ reporting

def report(talks: list[Talk], limit: int, chars: int) -> list[Seen]:
    """Print one line per conversation and return every dispatch in the store."""
    every: list[Seen] = []
    print(f"{'conversation':<14}{'events':>7}{'plans':>7}{'turns':>7}"
          f"{'chars':>7}{'of':>7}{'pad':>5}")
    for talk in talks:
        rows = replayed(talk, limit, chars)
        every.extend(rows)
        pad = len(_plan_run.held(talk.state))
        print(f"{talk.ident[:12]:<14}{len(talk.events):>7}{len(rows):>7}"
              f"{max((r.lines for r in rows), default=0):>7}"
              f"{max((r.chars for r in rows), default=0):>7}"
              f"{max((r.all_chars for r in rows), default=0):>7}{pad:>5}")
    return every


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--db", type=Path, default=STORE,
                        help=f"session store to replay (default {STORE})")
    parser.add_argument("--recent", type=int,
                        help="replay again at this turn cap and diff the two")
    parser.add_argument("--chars", type=int,
                        help="replay again at this character cap")
    parser.add_argument("--live", action="store_true",
                        help="dispatch real plans; needs credentials")
    parser.add_argument("-n", "--runs", type=int, default=1,
                        help="runs per task under --live (default 1)")
    parser.add_argument("-t", "--task", action="append", choices=TASKS,
                        help="only this task under --live; repeatable")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print each answer; --live only, see the module "
                             "docstring")
    args = parser.parse_args(argv)
    if args.verbose and not args.live:
        parser.error("nothing to print: a replay reports counts, never text")
    if args.live:
        return asyncio.run(live(args))

    conn = opened(args.db)
    try:
        talks = stored(conn)
    finally:
        conn.close()
    print(f"{args.db}, {len(talks)} conversation(s), "
          f"caps {plan_mod.RECENT} turns / {plan_mod.RECENT_CHARS} chars\n")
    rows = report(talks, plan_mod.RECENT, plan_mod.RECENT_CHARS)
    counted = tally(rows)
    if not counted["points"]:
        print("\nno tool call anywhere in this store, so no dispatch to replay")
        return 0

    print(f"\n{counted['points']} dispatch point(s): "
          f"{counted['none']} saw the whole conversation, "
          f"{counted['turns']} were cut by the turn cap, "
          f"{counted['chars']} by the character cap, "
          f"{counted['empty']} arrived with nothing")
    print(f"median {counted['median_lines']:g} turns / "
          f"{counted['median_chars']:g} chars; "
          f"the worst dropped {counted['worst_lost']} turns")
    if counted["spare"] is not None:
        print(f"where the turn cap did the cutting, a median of "
              f"{counted['spare']:g} of {plan_mod.RECENT_CHARS} characters "
              f"went unspent")

    if args.recent or args.chars:
        limit = args.recent or plan_mod.RECENT
        cap = args.chars or plan_mod.RECENT_CHARS
        other: list[Seen] = []
        for talk in talks:
            other.extend(replayed(talk, limit, cap))
        shift = moved(rows, other)
        print(f"\nat {limit} turns / {cap} chars: "
              f"{shift['changed']} of {shift['of']} dispatch points change, "
              f"by {shift['lines']:+d} turns and {shift['chars']:+d} characters "
              "in total")
        if not shift["changed"]:
            print("nothing moves — these caps are not what is bounding "
                  "this store")
    return 0


async def live(args) -> int:
    """Dispatch the tasks and print what each one cost."""
    agent = plan_mod.build_plan_agent()
    print(f"model {os.getenv('PLAN_MODEL', 'gemini-3.8-flash')}, "
          f"{args.runs} run(s) per task\n")
    for name in args.task or TASKS:
        print(f"{name}: {TASKS[name]['task']}")
        for run in range(1, args.runs + 1):
            got = await dispatch(agent, TASKS[name])
            print(f"  run {run}: {got.turns:>2} turns  {got.tools:>2} tools  "
                  f"{got.repeats:>2} repeated  {got.seconds:>5.1f}s  "
                  f"{got.ending}")
            if args.verbose:
                print(f"    {got.answer}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
