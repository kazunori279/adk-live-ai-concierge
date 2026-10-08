#!/usr/bin/env python3
"""Check the replay the plan bench reads a recorded conversation with.

`tools/plan_bench.py` is the first thing here that reports a number nobody can
sanity-check by looking: `_recent` is a pile of small rules, the replay runs it
four times per dispatch with different caps and subtracts the results, and the
answer is a figure somebody is going to move a constant on the strength of.
Every way that goes wrong is quiet.

- **The wrong dispatch points.** A replay that counts moments a plan could have
  gone out is only worth anything if those are the moments a tool really went
  out. Count text turns as well and the corpus fills with dispatches that never
  happened; miss the call events with more than one part and the fanned-out
  turns — the interesting ones — drop out.
- **The wrong cap named.** Late in a long conversation both caps are exceeded
  and only one of them is doing the cutting. Report "which caps are exceeded"
  instead of "which cap you would have to raise" and the bench recommends
  moving the one that would change nothing.
- **The store read out of order.** Events come back in whatever order the
  query asks for, and `_recent` keeps the *newest* turns. Read a conversation
  backwards and it still produces a plausible paragraph of exactly the right
  length, made of the wrong end of the conversation.
- **Content in the output.** The store is the household's own conversations.
  The bench prints counts and never text, and that is a property of the code
  rather than of the habits of whoever runs it, so it is checked here: the
  whole thing is run over a store made of one marker string and the output is
  searched for the marker.

The cases run against a store written by `SqliteSessionService` itself, in a
temporary directory — never the household's own — so the reading is pinned
against the thing that does the writing rather than against a schema copied
into this file, which would go on passing after ADK changed it.

The live half's arithmetic is checked too, over a real `Runner` driven by a
scripted model with a stub tool behind it. That leaves the network call as the
only part of `plan_bench.py` these cases do not reach.

Nothing here talks to anything. It is the tool that is under check and not the
product, which is the argument #76 made for `tools/runner_check.py`: the cases
want fabricated stores and a scripted model, and both would be noise in
`plan_check.py`, whose subject is the planner the server runs.

    uv run python tools/replay_check.py

Exits non-zero on the first case that does not hold.
"""

import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "src"))

from google.adk.events import Event  # noqa: E402
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.sessions.sqlite_session_service import (  # noqa: E402
    SqliteSessionService,
)
from google.adk.tools import FunctionTool  # noqa: E402
from google.genai import types  # noqa: E402

import plan_bench  # noqa: E402

from ai_terminal.live.tools import _plan_run  # noqa: E402
from ai_terminal.live.tools import plan as plan_mod  # noqa: E402

# Counted rather than written down, so the closing line cannot drift from the
# number of cases.
RAN = 0

# The string the household's conversations stand in for. Nothing else in the
# output can contain it, so finding it in a replay's stdout means a
# transcription reached the terminal.
SAID = "quicksilver-marmalade"


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got!r}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


# ------------------------------------------------------------ events to feed

def heard(text: str) -> Event:
    """A spoken turn of the person's, the way a native-audio run stores one."""
    return Event(author="user", input_transcription=types.Transcription(text=text))


def said(text: str) -> Event:
    """A spoken turn of the assistant's."""
    return Event(author="agent",
                 output_transcription=types.Transcription(text=text))


def called(name: str, **args) -> Event:
    """A turn in which the model dispatched one tool."""
    return Event(author="agent", content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(name=name, args=args))]))


def wide(*names: str) -> Event:
    """One turn that dispatched several tools at once."""
    return Event(author="agent", content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(name=n, args={}))
        for n in names]))


async def written(path: Path, talks: dict[str, list[Event]],
                  state: dict | None = None) -> None:
    """Fabricate a store, through the service the real one is written by.

    A temporary path, always. The one thing this file must never open for
    writing is `SESSIONS_DB`.
    """
    service = SqliteSessionService(db_path=str(path))
    for ident, events in talks.items():
        session = await service.create_session(
            app_name="checks", user_id="u", session_id=ident,
            state=dict(state or {}))
        for event in events:
            await service.append_event(session, event)


# -------------------------------------------------------------------- cases

async def store_cases() -> None:
    print("== reading a store back ==")

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sessions.db"
        await written(db, {
            "one": [heard("first"), said("second"), called("weather", q="x")],
            "two": [],
        }, state={_plan_run.PAD_KEY: ["a finding"]})

        conn = plan_bench.opened(db)
        try:
            talks = plan_bench.stored(conn)
        finally:
            conn.close()

        check("every conversation comes back", [t.ident for t in talks],
              ["one", "two"])
        check("with its events", [len(t.events) for t in talks], [3, 0],
              "an empty conversation is a conversation")
        check("oldest first, which is the order `_recent` drops from",
              [plan_mod._readable(e)[1] for e in talks[0].events][:2],
              ["first", "second"])
        check("and the session's own state, pad and all",
              _plan_run.held(talks[0].state), ["a finding"])
        check("the connection is read-only",
              _writable(db), False, "a bench must not edit the store")

    # The tie-break, which is the half of the ordering nothing above can see.
    # Two events of one fanned-out turn share a timestamp, and `id` is what
    # separates them — a random string, so it is not insertion order and the
    # rowid order SQLite falls back to without it is. Written with the ids the
    # wrong way round on purpose, and read back both ways: the bench has to
    # agree with the service, not with the order the rows went in.
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sessions.db"
        together = [called("weather", q="x"), called("place_info", q="y")]
        for event, ident in zip(together, ("zzz", "aaa")):
            event.id, event.timestamp = ident, 100.0
        await written(db, {"fanned": together})

        service = SqliteSessionService(db_path=str(db))
        theirs = await service.get_session(
            app_name="checks", user_id="u", session_id="fanned")
        conn = plan_bench.opened(db)
        try:
            ours = plan_bench.stored(conn)[0].events
            check("a turn's events come back in the service's own order",
                  [e.id for e in ours], [e.id for e in theirs.events],
                  "same timestamp, so `id` is the only thing separating them")
            check("which is not the order they were written in",
                  [e.id for e in ours], ["aaa", "zzz"],
                  "or the tie-break is missing and rowid order stood in")
        finally:
            conn.close()

    missing = Path(tempfile.gettempdir()) / "replay-check-no-such-store.db"
    try:
        plan_bench.opened(missing)
        gone = "no error"
    except SystemExit as exc:
        gone = "SystemExit"
        check("and a missing store says which one", str(missing) in str(exc),
              True)
    check("a missing store is not an empty one", gone, "SystemExit",
          "sqlite would otherwise create it and report nothing to replay")


def _writable(db: Path) -> bool:
    """Whether `opened` hands back a connection that can change the store."""
    conn = plan_bench.opened(db)
    try:
        conn.execute("DELETE FROM events")
        return True
    except Exception:
        return False
    finally:
        conn.close()


def dispatch_cases() -> None:
    print("== where a plan could have gone out ==")

    events = [heard("dinner"), called("maps_search", q="a"), said("two of them"),
              heard("the second one"), wide("weather", "place_info")]
    check("every event that called a tool", plan_bench.dispatches(events),
          [1, 4])
    check("a turn that called three is still one dispatch",
          plan_bench.dispatches([wide("a", "b", "c")]), [0],
          "one turn is one moment, however wide")
    check("a spoken turn is not one", plan_bench.dispatches([heard("hello")]),
          [])
    check("nor is an event with no content at all",
          plan_bench.dispatches([Event(author="agent")]), [])
    check("nor one whose content has no parts",
          plan_bench.dispatches([Event(author="agent", content=types.Content(
              role="model", parts=[]))]), [])
    check("an empty conversation has none", plan_bench.dispatches([]), [])
    # A model turn is often a sentence and then the call, in that order, and
    # the fanned-out ones carry the most parts of all. Read only the first
    # part and exactly those drop out of the corpus.
    check("a call behind a sentence in the same turn still counts",
          plan_bench.dispatches([Event(author="agent", content=types.Content(
              role="model", parts=[
                  types.Part(text="let me look that up"),
                  types.Part(function_call=types.FunctionCall(
                      name="weather", args={}))]))]), [0])
    check("the context replayed is what came before the call",
          plan_bench.seen(events[:1]).lines, 1,
          "the person's turn, not the call echoing the task back")


def seen_cases() -> None:
    print("== which cap did the cutting ==")

    short = [heard("dinner"), said("two of them")]
    check("a conversation that fits is not cut",
          plan_bench.seen(short).bound, "none")
    check("and every turn of it is there",
          [plan_bench.seen(short).lines, plan_bench.seen(short).all_lines],
          [2, 2])
    check("its length is the paragraph's, prefixes and all",
          plan_bench.seen(short).chars,
          len("person: dinner\nassistant: two of them"))
    check("nothing at all is nothing", plan_bench.seen([])._asdict(),
          {"lines": 0, "chars": 0, "all_lines": 0, "all_chars": 0,
           "bound": "none"})

    # Ten one-word turns, alternating, so that the turn cap is reachable long
    # before the character one — the shape `plan.py:171-174` predicts and the
    # one the real store turns out to be full of.
    chatty = [heard("a") if i % 2 == 0 else said("b") for i in range(10)]
    tight = plan_bench.seen(chatty, limit=4, chars=1800)
    check("a fast exchange runs out of turns first", tight.bound, "turns")
    check("and keeps the newest of them", tight.lines, 4)
    check("with the whole conversation counted beside it", tight.all_lines, 10)
    check("and most of the character budget unspent",
          tight.chars < 100, True, tight.chars)

    # One long turn, so the character cap is the only one in play.
    wordy = [said("x" * 400), heard("y" * 400), said("z" * 400)]
    check("a long turn runs out of characters first",
          plan_bench.seen(wordy, limit=14, chars=500).bound, "chars")
    check("and the cap is soft on the last line, so a turn is never halved",
          plan_bench.seen(wordy, limit=14, chars=500).lines, 2,
          "two whole turns, not one and a half")
    check("both spent at once reads as the character cap",
          plan_bench.seen(wordy, limit=2, chars=500).bound, "chars",
          "raising the turn cap alone would show nothing more")
    check("and one more turn of room is what names the turn cap",
          plan_bench.seen(wordy, limit=2, chars=1800).bound, "turns")

    merged = [heard("one"), heard("two"), said("three")]
    check("consecutive turns of one speaker count once",
          plan_bench.seen(merged).lines, 2,
          "a spoken turn arrives in pieces and `_recent` joins them")


def tally_cases() -> None:
    print("== the distribution over a whole store ==")

    rows = [
        plan_bench.Seen(2, 30, 2, 30, "none"),
        plan_bench.Seen(14, 900, 40, 4000, "turns"),
        plan_bench.Seen(6, 1790, 30, 5000, "chars"),
        plan_bench.Seen(14, 1000, 20, 2000, "turns"),
    ]
    got = plan_bench.tally(rows)
    check("every dispatch is counted once",
          [got["points"], got["none"], got["turns"], got["chars"]],
          [4, 1, 2, 1])
    check("the worst loss is turns dropped, not turns kept",
          got["worst_lost"], 26, "40 - 14")
    check("and `spare` is the other budget left where turns did the cutting",
          got["spare"], plan_mod.RECENT_CHARS - 950,
          "median of 1800-900 and 1800-1000")
    check("a dispatch with nothing in front of it is counted",
          plan_bench.tally([plan_bench.Seen(0, 0, 0, 0, "none")])["empty"], 1)
    check("`spare` is None when no dispatch was cut by turns",
          plan_bench.tally([rows[0]])["spare"], None,
          "a median of nothing is not zero")
    check("an empty store reports no points",
          plan_bench.tally([]), {"points": 0})


def moved_cases() -> None:
    print("== what other caps would change ==")

    before = [plan_bench.Seen(14, 900, 40, 4000, "turns"),
              plan_bench.Seen(2, 30, 2, 30, "none")]
    same = plan_bench.moved(before, before)
    check("identical caps move nothing",
          [same["changed"], same["lines"], same["chars"]], [0, 0, 0],
          "which is the result that closes the question, not a null one")

    after = [plan_bench.Seen(28, 1700, 40, 4000, "turns"),
             plan_bench.Seen(2, 30, 2, 30, "none")]
    got = plan_bench.moved(before, after)
    check("only the dispatches that change are counted",
          [got["changed"], got["of"]], [1, 2])
    check("and the totals are the difference over those",
          [got["lines"], got["chars"]], [14, 800])


def run_cases() -> None:
    print("== what one dispatched plan cost ==")

    check("a real answer is an answer", plan_bench.ending("Kaikaya opens at 5."),
          "answer")
    check("and the four other endings keep the card's own names",
          sorted({plan_bench.ending(t) for t in
                  (plan_mod.FAILED, plan_mod.NO_ANSWER, plan_mod.UNCONFIGURED,
                   plan_mod.TOO_SLOW)}),
          ["empty", "failed", "slow", "unset"])
    check("a timeout carrying part of an answer is still a timeout",
          plan_bench.ending(f"{plan_mod.TOO_SLOW_PARTIAL}\n\nKaikaya at 5."),
          "slow", "`_kind` matches by prefix and so does this")

    events = [called("weather", location="Kyoto", lang="en"),
              called("weather", lang="ja", location="Kyoto"),
              called("weather", location="Nara", lang="en"),
              said("here they are")]
    calls = plan_bench.asked(events)
    check("every call is picked up, spoken turns are not", len(calls), 3)
    check("the language a call was made in is not part of it",
          "lang" in calls[0], False, calls[0])
    check("nor is the order the model wrote the arguments in",
          calls[0], calls[1], "otherwise a repeat reads as two calls")
    check("the same lookup twice is one repeat", plan_bench.repeated(calls), 1)
    check("a different argument is a different lookup",
          plan_bench.repeated([calls[0], calls[2]]), 0)
    check("and three of the same is two repeats",
          plan_bench.repeated([calls[0]] * 3), 2,
          "the first of each is the one that is not a repeat")
    check("nothing called is nothing repeated", plan_bench.repeated([]), 0)


# ---------------------------------------------------- the live half, unplugged

_SCRIPT: list[list] = []


class Scripted(BaseLlm):
    """A model that reads each turn off a list, so a real `Runner` runs.

    Enough to make ADK do the round trips, invoke `before_model_callback`, and
    put the tool traffic in the event stream — which is everything
    `plan_bench.dispatch` reads. The network is what is left out.
    """

    async def generate_content_async(self, llm_request, stream=False):
        parts = _SCRIPT.pop(0) if _SCRIPT else [
            types.Part.from_text(text="Kaikaya opens at 17:00.")]
        yield LlmResponse(content=types.Content(role="model", parts=parts))


async def lookup(place: str, lang: str = "") -> dict:
    """A stub the scripted model can call. Talks to nothing."""
    del lang
    return {"place": place}


async def live_cases() -> None:
    print("== a plan dispatched, with the model unplugged ==")

    task = {"task": "dinner", "context": "", "found": ""}
    agent = plan_mod.build_plan_agent()
    agent.model = Scripted(model="scripted")
    # The planner's own tools reach the network and one of them reads the
    # owner's mailbox. None of them is what is under check here.
    agent.tools = [FunctionTool(lookup)]

    quiet = logging.getLogger("google_adk.google.adk.telemetry._metrics")
    was, quiet.disabled = quiet.disabled, True
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            _SCRIPT.clear()
            plain = await plan_bench.dispatch(agent, task)
            check("one round trip is one turn", plain.turns, 1,
                  "off the budget the enforcement itself reads")
            check("a plan that called nothing repeated nothing",
                  [plain.tools, plain.repeats], [0, 0])
            check("and an answer is an answer",
                  [plain.ending, plain.answer],
                  ["answer", "Kaikaya opens at 17:00."])
            check("it took some time and not none", plain.seconds > 0, True)

            _SCRIPT[:] = [
                [types.Part(function_call=types.FunctionCall(
                    name="lookup", args={"place": "Kaikaya"})),
                 types.Part(function_call=types.FunctionCall(
                     name="lookup", args={"place": "Kaikaya"}))],
                [types.Part.from_text(text="Both said the same thing.")],
            ]
            twice = await plan_bench.dispatch(agent, task)
            check("two round trips are two turns", twice.turns, 2)
            check("both calls are counted", twice.tools, 2)
            check("and the second is the repeat the pad exists to stop",
                  twice.repeats, 1)

            _SCRIPT[:] = [[types.Part.from_text(text=plan_mod.NO_ANSWER)]]
            empty = await plan_bench.dispatch(agent, task)
            check("a plan that ended on one of the four is not an answer",
                  empty.ending, "empty")
    finally:
        quiet.disabled = was
        _SCRIPT.clear()


async def quiet_cases() -> None:
    print("== a replay says nothing anybody said ==")

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sessions.db"
        await written(db, {"talk": [
            heard(SAID), said(SAID), called("weather", location=SAID),
            heard(SAID), called("web_search", query=SAID),
        ]})
        done = subprocess.run(
            [sys.executable, str(TOOLS / "plan_bench.py"), "--db", str(db),
             "--recent", "1"],
            capture_output=True, text=True, cwd=TOOLS.parent)
        out = done.stdout + done.stderr
        check("the replay runs over a store on disk", done.returncode, 0,
              out.strip().splitlines()[-1] if out.strip() else "no output")
        check("and reports the dispatches it found",
              "2 dispatch point(s)" in out, True)
        check("and prints nothing anybody said", SAID in out, False,
              "the store is the household's own conversations")
        check("not even the tool arguments", "location=" in out, False)

        refused = subprocess.run(
            [sys.executable, str(TOOLS / "plan_bench.py"), "--db", str(db),
             "-v"], capture_output=True, text=True, cwd=TOOLS.parent)
        check("and there is no way to ask it to", refused.returncode, 2,
              "`-v` belongs to the live half")
        check("with a reason rather than a usage line alone",
              "never text" in refused.stderr, True)

        # The case above only ever feeds the bench a store this ADK parses, so
        # it cannot see the one path where the promise breaks by itself: a
        # parser's error message quotes what it rejected, and what it rejected
        # is a row. Break one deliberately.
        conn = sqlite3.connect(db)
        rid, blob = conn.execute(
            "SELECT rowid, event_data FROM events ORDER BY rowid LIMIT 1"
        ).fetchone()
        wrong = json.loads(blob)
        wrong.setdefault("content", {})["role"] = [SAID]
        conn.execute("UPDATE events SET event_data=? WHERE rowid=?",
                     (json.dumps(wrong), rid))
        conn.commit()
        conn.close()

        torn = subprocess.run(
            [sys.executable, str(TOOLS / "plan_bench.py"), "--db", str(db)],
            capture_output=True, text=True, cwd=TOOLS.parent)
        check("a row this release cannot read stops the replay",
              torn.returncode != 0, True)
        check("and still says nothing anybody said",
              SAID in torn.stdout + torn.stderr, False,
              "a pydantic error carries the value it rejected")
        check("naming the conversation so it can be found",
              "not readable by this release" in torn.stdout + torn.stderr,
              True)

    # The state column is parsed too and nothing points at its guard. Its
    # parser is the one that does not echo the document, so leaving it
    # unguarded costs no speech today and still swaps a sentence for a
    # traceback — which is the thing the docstring promises will not happen.
    # Two conversations, so the message has to name the right one.
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sessions.db"
        await written(db, {"kept-whole": [heard(SAID)],
                           "torn-state": [heard(SAID)]})
        conn = sqlite3.connect(db)
        conn.execute("UPDATE sessions SET state=? WHERE id=?",
                     ('{"note": "' + SAID, "torn-state"))
        conn.commit()
        conn.close()

        bad = subprocess.run(
            [sys.executable, str(TOOLS / "plan_bench.py"), "--db", str(db)],
            capture_output=True, text=True, cwd=TOOLS.parent)
        told = bad.stdout + bad.stderr
        check("a torn state column stops the replay the same way",
              "not readable by this release" in told, True,
              "unguarded it is a traceback, not a sentence")
        check("saying which of the two parses gave up", "its state is" in told,
              True)
        check("and naming the torn conversation, not the whole one before it",
              ["torn-state" in told, "kept-whole" in told], [True, False])


async def main() -> None:
    await store_cases()
    dispatch_cases()
    seen_cases()
    tally_cases()
    moved_cases()
    run_cases()
    await live_cases()
    await quiet_cases()
    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    asyncio.run(main())
