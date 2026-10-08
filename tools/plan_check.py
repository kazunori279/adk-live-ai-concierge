"""Check the planner's plumbing: the budget, the screen handle, the history.

`plan` is a sub-agent, so the interesting parts are the four seams around it
rather than what the model decides. Each of them fails silently, which is why
they are worth pinning:

- **The turn budget.** `Counted` charges one turn per model round trip, not one
  per tool call, so that three lookups asked for together cost what one costs.
  Get it wrong in the safe direction and a plan runs until the timeout; get it
  wrong in the other and the wide plan — the fast one — is the one that runs
  out first. Two limits guard the width: `WIDE` tools in a turn actually run
  and the rest are refused, and `CEILING` stops a model that answers the width
  refusal by asking again forever without ever ending its turn.
- **What the prompt promises against what is enforced.** Every number in the
  instruction — eight turns, six tools, twelve Workspace commands over one, two
  or three steps — is checked against the constant behind it. A prompt that
  offers more than the code allows costs the model the turn it spends finding
  out.
- **The gws budget composing, and dividing.** `_gws_run.budget` is reentrant so
  that four `gws` steps in one plan share one twelve-call ceiling instead of
  getting twelve each. A non-reentrant version passes every other check in the
  repo and quietly quadruples the bill. Since those steps can now run at once,
  each gets a `fair` share of what is left rather than a first-come-first-served
  run at the whole thing — the failure being one greedy step spending twelve
  and the two beside it getting nothing.
- **What the gws budget refuses, and on which of its three limits.** Calls,
  clock and repeated fetches, each of which fails in both directions: too
  loose and the question runs out of budget mid-answer, too tight and a
  perfectly ordinary second read of one email is refused. What counts as the
  same fetch is the delicate part — key it on the argv and `--format json`
  makes a duplicate look new, key it on the params alone and five `+read --id`
  calls for five different messages all look like one. Every way of getting it
  too tight refuses something ordinary and says "nothing was run" about it,
  which is a lie the sub-agent has no way to check: three events created, one
  name searched across three months, three messages forwarded.
- **Which screen a nested tool draws on.** Under `plan` a tool's own
  `ToolContext` names a sub-`Runner`'s session that nobody registered a sink
  for, so `screen.push` drops the frame and the card never moves. `session_of`
  is the fix and it is invisible when it breaks.
- **The conversation the planner is handed.** `_recent` is the only thing that
  makes "book the other one instead" answerable, and it is a pile of small
  rules — merge consecutive speakers, cap by count and by characters, oldest
  dropped first — each of which reads fine while being wrong.
- **The screen the planner is handed.** `_shown` is the other half of the same
  events, and it is the only thing that makes "book the *second* one"
  answerable. Every rule in it is a rule about matching a page nothing here can
  see: which answers drew a card and which drew none, where a slow card sits,
  what happens to the same question asked twice, and — the one that carries the
  whole point — that the number beside a name is the number the card shows.
- **What a timeout has left to say.** `asyncio.timeout` cancels the planner
  outright, taking every local with it, so the turns it had already settled
  have to be collected into a list the caller owns. Without them a plan that
  found the restaurant and its hours before the forecast ran long reports
  nothing at all and costs the person the whole 250 s again.

No model is called. `Counted` is driven over a stub function, `_recent` over
hand-built events, and `_run` over a `Runner` that replays a fixed list.

    uv run python tools/plan_check.py

Exits non-zero on the first case that does not hold.
"""

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
import shlex
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.adk.tools import FunctionTool  # noqa: E402
from google.genai import types  # noqa: E402

from ai_terminal.live import conn, recap, screen  # noqa: E402
from ai_terminal.live.tools import (  # noqa: E402
    _agenda, _gws_cli, _gws_run, _plan_run, _progress,
)
from ai_terminal.live.tools import plan as plan_mod  # noqa: E402

# The slowest the mailbox question has been seen to take, over thirteen runs on
# 2026-09-09 — `tools/gws_bench.py -q next` plus a driver that recorded the
# elapsed time at every chargeable call. The median was 58 s; this is the tail,
# and the tail is what `gws.TIMEOUT` is sized against. Written down because it
# costs a quarter of an hour of live model time to measure and the case below
# has to run in a second. Re-measure it before lowering either number.
#
# What was written down is the run totals. The driver's per-call output was
# not retained anywhere — not in the tree and not outside it — so 79.7 s, the
# figure `gws.py` and the README both quote, is the last call of the slowest
# run *whose trace survives*, and no per-call time is available for this one.
# See the comment above the timing case for what that costs.
SLOWEST_MAILBOX = 130.5

# Counted rather than written down, so the closing line cannot drift from the
# number of cases the way a figure typed into a commit message does.
RAN = 0


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got!r}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


class FakeContext:
    """What a `ToolContext` is, as far as anything here reads one."""

    def __init__(self, session_id: str = "sub", call_id: str = "call-sub",
                 state: dict | None = None):
        self.session = type("S", (), {"id": session_id, "events": []})()
        self.function_call_id = call_id
        self._invocation_context = type(
            "I", (), {"live_request_queue": None})()
        # A plain dict, which is what `Context.state` behaves as here: ADK's
        # `State` reads through to the session and writes into an event's
        # `state_delta`, and every access in `plan` is a `get` or a `__setitem__`.
        self.state = {} if state is None else state


class Event:
    """One session event, in the three shapes `_readable` knows.

    Built by hand rather than with `google.adk.events.Event`, because what is
    being checked is which attributes `_readable` reaches for — and a real
    Event would supply defaults for the ones it should be tolerating the
    absence of.
    """

    def __init__(self, said=None, heard=None, role="model", parts=None):
        self.input_transcription = (
            type("T", (), {"text": heard})() if heard is not None else None)
        self.output_transcription = (
            type("T", (), {"text": said})() if said is not None else None)
        self.content = (
            type("C", (), {"role": role, "parts": parts})() if parts else None)


def part(text=None, thought=False, call=None, args=None, response=None,
         answered=None, ident=None):
    """One content part: text, a thought, a function call or its answer.

    `ident` is the function-call id, which is how `_shown` holds an answer to
    the call that asked for it — a card is headed with the call's arguments
    when the answer carries no handle of its own. `answered` is the payload the
    tool came back with, which is the half of a part `_readable` has never
    read.
    """
    fc = (type("F", (), {"name": call, "args": args or {}, "id": ident})()
          if call else None)
    fr = (type("R", (), {"name": response, "id": ident,
                         "response": {} if answered is None else answered})()
          if response else None)
    return type("P", (), {"text": text, "thought": thought,
                          "function_call": fc, "function_response": fr})()


def drew(name, payload, ident=None, args=None):
    """One event that called a tool and one that answered, as a card is drawn.

    The pair rather than either half: `_shown` orders by the call and reads the
    answer, so a fixture with only one of them is not a card.
    """
    ident = ident or f"c-{name}-{id(payload)}"
    return [Event(parts=[part(call=name, args=args or {}, ident=ident)]),
            Event(role="user",
                  parts=[part(response=name, answered=payload, ident=ident)])]


# ---------------------------------------------------------------- the budget

def budget_cases() -> None:
    print("== the turn budget ==")

    # One tool per turn, which is what a serial plan is and is exactly what the
    # budget used to count. It has to be charged the same as it always was, or
    # #62 is not a change of unit, it is a change of ceiling.
    with _plan_run.budget() as tally:
        first = []
        for _ in range(_plan_run.WARN_AT - 1):
            _plan_run.turn()
            first.append(_plan_run.spend())
        check("nothing is said while there is room", first,
              [None] * (_plan_run.WARN_AT - 1),
              f"{_plan_run.WARN_AT - 1} turns of one tool, no note")
        check("the tally counts the turns", tally.turns, _plan_run.WARN_AT - 1)

        _plan_run.turn()
        warned = _plan_run.spend()
        check("WARN_AT is where it starts saying so",
              warned.startswith(f"{_plan_run.WARN_AT} of {_plan_run.STOP_AT}"),
              True, warned)

        while tally.turns < _plan_run.STOP_AT:
            _plan_run.turn()
            last = _plan_run.spend()
        check("the last turn is still a turn, not a refusal",
              _plan_run.refused(last), False, f"turn {_plan_run.STOP_AT} runs")

        _plan_run.turn()
        check("and the one past it is refused", _plan_run.spend(),
              _plan_run.OUT_OF_STEPS)

    # The change itself. Eight tools in one turn is one turn of somebody
    # waiting, and used to be the whole plan; the serial version above spent
    # its eight on eight turns and got the same refusal at the same point.
    with _plan_run.budget() as wide:
        _plan_run.turn()
        notes = [_plan_run.spend() for _ in range(_plan_run.WIDE)]
        check("a whole turn of tools costs one turn", wide.turns, 1,
              f"{_plan_run.WIDE} calls in it")
        check("and none of them is refused",
              [n for n in notes if _plan_run.refused(n)], [])
        check("though the calls are counted too", wide.spent, _plan_run.WIDE,
              "CEILING reads this, nothing else does")

        over = _plan_run.spend()
        check("past WIDE the extra call is refused, not the turn",
              over == _plan_run.TOO_WIDE.format(wide=_plan_run.WIDE), True,
              over)
        check("and it says so as a refusal", _plan_run.refused(over), True)
        check("but the plan is not out of turns",
              over == _plan_run.OUT_OF_STEPS, False,
              "read what ran, then ask again next turn")
        check("and the refused call is not charged for", wide.spent,
              _plan_run.WIDE, "it did not run, so CEILING never saw it")

        _plan_run.turn()
        check("and the next turn starts wide again", _plan_run.spend(), None,
              "in_turn is reset by the turn, not by the plan")

    # A model that over-fans. It asks for twice the width every turn and is
    # told to ask again next turn; the point is that obeying costs it nothing.
    # When the refusals were charged to CEILING this plan died on turn five
    # having run twenty-four tools, while a well-behaved six-per-turn plan got
    # all eight turns and forty-eight.
    with _plan_run.budget() as greedy:
        turns_had = 0
        while True:
            _plan_run.turn()
            notes = [_plan_run.spend() for _ in range(_plan_run.WIDE * 2)]
            if notes[0] == _plan_run.OUT_OF_STEPS:
                break
            turns_had += 1
        check("over-fanning does not cost the plan its turns", turns_had,
              _plan_run.STOP_AT, "five before this")
        check("and it ran every tool it was allowed", greedy.spent,
              _plan_run.STOP_AT * _plan_run.WIDE,
              f"{_plan_run.CEILING} promised, twenty-four before this")

    # The backstop, and the loop it exists to end. Nothing calls `turn` here,
    # which is what an unwired `before_model_callback` looks like from in here:
    # `turns` never moves, so STOP_AT can never fire and `in_turn` never resets.
    # The width rule then answers every call after the sixth with "ask again
    # next turn" — and there are no turns, so the model asks forever, for free.
    # CEILING is the only thing that ends that.
    with _plan_run.budget() as loose:
        notes = [_plan_run.spend() for _ in range(_plan_run.CEILING)]
        check("with no turns counted, the width rule answers on its own",
              set(notes[_plan_run.WIDE:]),
              {_plan_run.TOO_WIDE.format(wide=_plan_run.WIDE)},
              "which tells the model to ask again next turn, forever")
        check("so the calls are what run out", _plan_run.spend(),
              _plan_run.OUT_OF_STEPS, f"CEILING {_plan_run.CEILING}")
        check("the turn count having gone nowhere", loose.turns, 0)

    check("outside a budget there is nothing to charge",
          _plan_run.spend(), None, "a tool called from a test, or by the "
          "live agent directly")
    _plan_run.turn()
    with _plan_run.budget() as clean:
        check("and a turn counted out there lands on nothing", clean.turns, 0,
              "the live agent's own model turns are not plan turns")
    check("and nothing claims to be nested", _plan_run.nested(), False)
    with _plan_run.budget():
        check("inside one, it does", _plan_run.nested(), True)

    # Not nested, and the budget is gone: the block resets its token.
    check("the block puts it back on the way out", _plan_run.nested(), False)

    # The numbers themselves, not only the constants. Every case above reads
    # WARN_AT and STOP_AT out of the module, so raising STOP_AT to twenty
    # passes all of them — and a planner that has talked itself into a loop
    # then runs twenty turns against a timeout it can no longer reach.
    check("the cap is eight turns", _plan_run.STOP_AT, 8)
    check("and the warning starts at five", _plan_run.WARN_AT, 5)
    check("with turns left to change course after it",
          _plan_run.STOP_AT - _plan_run.WARN_AT >= 3, True,
          "a warning on the last turn is not a warning")
    check("a turn may hold six tools", _plan_run.WIDE, 6)
    check("and the backstop is what a full-width plan would reach",
          _plan_run.CEILING, _plan_run.STOP_AT * _plan_run.WIDE,
          "so it cannot fire on a plan that is behaving")

    # The refusals, against the sentence they have in common. A fifth one added
    # later without it would be handed to the model as a result to read.
    check("a warning is not a refusal",
          _plan_run.refused(_plan_run.LEFT.format(spent=5, total=8)), False)
    check("nor is having said nothing", _plan_run.refused(None), False)
    check("out of turns is", _plan_run.refused(_plan_run.OUT_OF_STEPS), True)
    check("and so is a turn that fanned out too far",
          _plan_run.refused(_plan_run.TOO_WIDE.format(wide=_plan_run.WIDE)),
          True, "formatted, so `note == TOO_WIDE` never was")

    # The one sentence in `LEFT` that is not about the count. The warning is
    # where the fan-out rule reaches the model at the moment it has a reason to
    # act on it — several hundred words after the instruction said it.
    check("and the warning says what a turn costs",
          "one turn between them" in _plan_run.LEFT, True)


async def counted_cases() -> None:
    print("== Counted ==")

    ran: list[dict] = []

    async def stub(where: str) -> dict:
        """Stand in for a grounding tool.

        Args:
            where: Anything at all.
        """
        ran.append({"where": where})
        return {"answer": where}

    tool = _plan_run.counted(FunctionTool(stub))
    ctx = FakeContext()

    check("the declaration stays the wrapped tool's", tool.name, "stub",
          "ADK builds it from `func`, which Counted keeps")

    with _plan_run.budget() as tally:
        _plan_run.turn()
        got = await tool.run_async(args={"where": "one"}, tool_context=ctx)
        check("an early step answers with no note attached",
              got, {"answer": "one"})

        for _ in range(_plan_run.WARN_AT - 1):
            _plan_run.turn()
            got = await tool.run_async(args={"where": "n"}, tool_context=ctx)
        check("a late one carries the warning back with the answer",
              got.get("budget", "").startswith(f"{_plan_run.WARN_AT} of "),
              True, got.get("budget"))
        check("and the answer is still in there", got["answer"], "n")

        while tally.turns < _plan_run.STOP_AT:
            _plan_run.turn()
            await tool.run_async(args={"where": "n"}, tool_context=ctx)
        check("every turn up to STOP_AT ran", len(ran), _plan_run.STOP_AT)

        _plan_run.turn()
        refused = await tool.run_async(args={"where": "past"},
                                       tool_context=ctx)
        check("past it the tool refuses", refused,
              {"error": _plan_run.OUT_OF_STEPS})
        check("and the function was not called", len(ran), _plan_run.STOP_AT,
              "'Nothing was run' has to be true")

    # The other refusal, which used to reach the model as an answer. `Counted`
    # compared against OUT_OF_STEPS by name, so a too-wide call ran its tool and
    # returned the result with the refusal stapled on as `budget` — the model
    # was told nothing had run, and handed what had.
    ran.clear()
    with _plan_run.budget():
        _plan_run.turn()
        for _ in range(_plan_run.WIDE):
            await tool.run_async(args={"where": "w"}, tool_context=ctx)
        wide = await tool.run_async(args={"where": "over"}, tool_context=ctx)
        check("a turn fanned out too far refuses the extra call",
              wide, {"error": _plan_run.TOO_WIDE.format(wide=_plan_run.WIDE)})
        check("and that one did not run either", len(ran), _plan_run.WIDE,
              "'Nothing was run' has to be true of both refusals")

    outside = await tool.run_async(args={"where": "free"}, tool_context=ctx)
    check("outside a plan the same object is uncharged", outside,
          {"answer": "free"}, "the live agent holds one of these too")

    # Which calls of a too-wide turn get refused is not a race. ADK creates one
    # task per call in the order the model listed them and `run_async` charges
    # before its first await, so the ones that run are the first WIDE asked for
    # — deterministic, and the message says "read what those came back with",
    # which is only useful advice if it is the same ones every time.
    ran.clear()
    with _plan_run.budget():
        _plan_run.turn()
        out = await asyncio.gather(*(
            tool.run_async(args={"where": str(i)}, tool_context=ctx)
            for i in range(_plan_run.WIDE + 3)))
    check("and it is the first ones asked for that run",
          [r["where"] for r in ran], [str(i) for i in range(_plan_run.WIDE)])
    check("the rest coming back refused, in order",
          [i for i, r in enumerate(out) if "error" in r],
          list(range(_plan_run.WIDE, _plan_run.WIDE + 3)))

    # A step that raised, which is the same situation as a refusal and used to
    # be the one that got out. `maps_search`, `web_search` and `image_search`
    # have no error handling of their own, and `web_search` calls
    # `raise_for_status` over a 30 s request from a Pi on Wi-Fi, so this is a
    # 429 on one lookup of six rather than anything exotic.
    async def sidecar_down(where: str) -> dict:
        """Stand in for a grounding tool the network took out.

        Args:
            where: Anything at all.
        """
        raise RuntimeError("sidecar 500: https://example.test/x?key=sekrit")

    broken = _plan_run.counted(FunctionTool(sidecar_down))
    noisy = logging.getLogger(_plan_run.__name__)
    was, noisy.disabled = noisy.disabled, True  # the traceback is expected
    try:
        with _plan_run.budget():
            _plan_run.turn()
            try:
                step = await broken.run_async(args={"where": "Ebisu"},
                                              tool_context=ctx)
            except Exception as err:  # pylint: disable=broad-except
                step = f"raised {type(err).__name__}"
    finally:
        noisy.disabled = was
    said = step.get("error", "") if isinstance(step, dict) else str(step)
    check("a step that raised comes back as a step that failed, not a raise",
          said.startswith("The `sidecar_down` step failed"), True, said)
    check("named, so the planner knows which one to do without",
          "sidecar_down" in said, True)
    check("carrying the exception's type", "RuntimeError" in said, True)
    check("and not its text, which is a log line and can hold a key",
          "sekrit" in said, False, "this string ends up in a spoken answer")
    check("it is not a refusal: that one says nothing ran, and this ran",
          _plan_run.refused(said), False, _plan_run.STOPPED)

    # The whole of it. One step failing costs its own answer and nothing else:
    # the steps beside it keep theirs, and the plan keeps the turns it paid
    # for. Unhandled, the raise leaves the sub-run and reaches `plan._ask`,
    # which has only `plan.FAILED` to say — no answer at all, and a claim that
    # nothing was changed that a `gws` write earlier in the same plan makes
    # false.
    ran.clear()
    was, noisy.disabled = noisy.disabled, True
    try:
        with _plan_run.budget():
            _plan_run.turn()
            try:
                side = [r.get("answer") for r in await asyncio.gather(
                    tool.run_async(args={"where": "one"}, tool_context=ctx),
                    broken.run_async(args={"where": "two"}, tool_context=ctx),
                    tool.run_async(args={"where": "three"},
                                   tool_context=ctx))]
            except Exception as err:  # pylint: disable=broad-except
                side = f"raised {type(err).__name__}"
    finally:
        noisy.disabled = was
    check("one step failing does not take the turn's other steps with it",
          side, ["one", None, "three"],
          "unhandled, the gather raises and the plan answers FAILED")
    check("and the two that worked did their work", [r["where"] for r in ran],
          ["one", "three"])

    # Charged or not. `show_plan` and `note_finding` cost no step, which says
    # nothing about how they behave — and `show_plan` is the one tool the model
    # hands a structure to rather than a string. `_within` is written to be
    # un-crashable and sanitises `running` and `done`; `steps` reaches `len()`
    # as it arrives, and ADK does not type-check a tool's arguments, so a model
    # that sends `null` for it used to end the plan.
    loose = sorted(t.name for t in plan_mod._tools()
                   if type(t).run_async is FunctionTool.run_async)
    check("every planner tool handles its own raise, the free two included",
          loose, [], "costing no step says nothing about behaving")
    drawing = next(t for t in plan_mod._tools() if t.name == "show_plan")
    was, noisy.disabled = noisy.disabled, True
    try:
        try:
            drawn = await drawing.run_async(args={"steps": None},
                                            tool_context=ctx)
        except Exception as err:  # pylint: disable=broad-except
            drawn = f"raised {type(err).__name__}"
    finally:
        noisy.disabled = was
    check("a plan card drawn from a shape that cannot be drawn says so",
          isinstance(drawn, dict) and "error" in drawn, True, drawn)


async def gws_budget_cases() -> None:
    print("== the gws budget composing under a plan ==")

    with _gws_run.budget() as outer:
        _gws_run.spend()
        _gws_run.spend()
        _gws_run.spend()
        with _gws_run.budget() as inner:
            check("a second entry yields a share of the tally running",
                  (inner is outer, inner.pool is outer), (False, True))
            _gws_run.spend()
        check("so the count carries across the inner block", outer.spent, 4,
              "one question is twelve gws calls whether or not a plan is in "
              "the middle")
        check("and the share counted only its own", inner.spent, 1)

    with _gws_run.budget() as first:
        _gws_run.spend()
    with _gws_run.budget() as second:
        check("two calls in sequence each get a fresh one",
              second.spent, 0, "reentrant, not global")
        check("and the outer one is its own pool", second.pool is second, True)

    # The whole of it, in one shape: three steps of a plan running together, and
    # the greedy one no longer eating what the other two were held back for.
    # Measured before this changed — one branch wanting fourteen against two
    # wanting three got 12/0/0, which is a fast half-answer where the serial
    # version was a slow whole one.
    # It yields between calls because that is what three steps each awaiting a
    # subprocess do, and the barrier is what makes them concurrent at all —
    # `spend` has no await in it, so without one the first coroutine runs to
    # completion and takes twelve, which is the bug rather than the fix.
    #
    # In `gws.gws`'s own shape, and this matters more than it looks: the budget
    # block closes around `create_task` and nothing awaits inside it, so the
    # share is only still counted because `hold` keeps it counted. Written the
    # obvious way — the `with` held open for the branch's whole life, which is
    # what this was — every case below passes whether `hold` exists or not, and
    # in production `fair` sees `live` at 0 and hands each step all twelve.
    turned_away: list[str] = []
    seen_live: set[int] = set()

    async def branch(want: int, gate: asyncio.Barrier, pace: int = 1) -> int:
        async def calls() -> int:
            await gate.wait()
            got = 0
            for _ in range(want):
                seen_live.add(_gws_run._BUDGET.get().pool.live)
                note = _gws_run.spend()
                if _gws_run.refused(note):
                    turned_away.append(note)
                else:
                    got += 1
                # `pace` is how long this step's call takes, in scheduler
                # turns. A mailbox question is thirty times a calendar one, and
                # that gap is the whole reason a share exists.
                for _ in range(pace):
                    await asyncio.sleep(0)
            return got

        with _gws_run.budget() as share:
            work = asyncio.create_task(calls())
        _gws_run.hold(share, work)
        return await work

    with _gws_run.budget() as pool:
        gate = asyncio.Barrier(3)
        even = await asyncio.gather(*(branch(6, gate) for _ in range(3)))
    check("three steps at once get a fair share each", even, [4, 4, 4],
          f"STOP_AT {_gws_run.STOP_AT} over three")
    check("and the question spends exactly its allowance", pool.spent,
          _gws_run.STOP_AT, "6/3/3 before this, and the pool overdrawn to 18")
    # Which says the *share* did this and not the pool ceiling: 4/4/4 comes out
    # of the ceiling alone when `hold` is missing and every step is handed all
    # twelve, so the count above cannot tell one from the other.
    check("and the steps saw each other running", sorted(seen_live), [3],
          "0 at every call before `hold`, which sends `fair` down its "
          "`<= 1` branch and hands each step the whole pool")

    turned_away.clear()
    with _gws_run.budget() as pool:
        gate = asyncio.Barrier(3)
        greedy = await asyncio.gather(branch(14, gate), branch(3, gate),
                                      branch(3, gate))
    check("a greedy step is held to its share while the others run",
          greedy, [6, 3, 3], "12/0/0 before this — a fast half-answer where "
          "the serial version was a slow whole one")
    check("and takes the rest once they have finished", pool.spent,
          _gws_run.STOP_AT, "the remainder came back, without a wakeup")
    # And the reviewer's own shape for #62: one fast step wanting everything
    # beside two slow ones. This is the case that could not be made at all
    # before `hold` — with `live` stuck at 0 no step was ever over a share,
    # `OUT_OF_SHARE` was unreachable, and every refusal in the system read
    # `OUT_OF_CALLS` while the fast step took all twelve.
    turned_away.clear()
    with _gws_run.budget() as pool:
        gate = asyncio.Barrier(3)
        mixed = await asyncio.gather(branch(10, gate),
                                     branch(2, gate, pace=30),
                                     branch(2, gate, pace=30))
    check("a fast step beside two slow ones stops at its share", mixed,
          [4, 2, 2], "10/1/1 before this, and the slow ones' calls gone")
    check("and it was the share that turned it away, not the ceiling",
          [n.startswith(_gws_run.OUT_OF_SHARE[:20]) for n in turned_away],
          [True] * len(turned_away),
          f"{len(turned_away)} refusals, with {pool.spent} of "
          f"{_gws_run.STOP_AT} spent — the ceiling was never reached")

    # A question asked with no plan around it. Its budget *is* the pool, and
    # `budget` never counted the pool as a live branch — `hold` must not start,
    # or the only step there is ends up sharing with itself.
    alone: list[int] = []

    async def solo() -> None:
        await asyncio.sleep(0)
        alone.append(_gws_run._BUDGET.get().pool.live)

    with _gws_run.budget(seconds=_gws_run.RESERVE) as own:
        lone = asyncio.create_task(solo())
    _gws_run.hold(own, lone)
    await lone
    check("a question with no plan around it shares with nobody", alone, [0],
          "its own budget is the pool, and the pool is not a branch of itself")

    # And that `gws.gws` itself does this. Every case above calls `hold` from
    # the harness, so none of them can see the production call go missing —
    # which is the only place it actually matters.
    # Matched with its indent: at the `with`'s own column it runs after the
    # block closes, which is the whole point. A column further in and it is
    # inside the block, where the release is hung on a count that has not been
    # given back yet and every step reads the pool as its own.
    src = inspect.getsource(plan_mod.gws_mod.gws)
    check("gws holds its share past the block that opened it",
          src.count("\n            _gws_run.hold(share, work)\n"), 1,
          "outdented to the `with`, and after it")

    # And what handing the remainder back by not handing it out costs. A branch
    # that spends its whole share without yielding is refused for the rest, and
    # a share growing afterwards only helps a branch with another call left to
    # make. Deterministic here rather than under `gather`, where which
    # coroutine the barrier wakes first decides the numbers.
    with _gws_run.budget():
        with _gws_run.budget() as big:
            with _gws_run.budget():
                with _gws_run.budget():
                    got = sum(1 for _ in range(14)
                              if not _gws_run.refused(_gws_run.spend()))
                    check("a step asking for everything at once stops at its "
                          "share", got, 4)
            check("and its share grows the moment the others close",
                  big.fair, _gws_run.STOP_AT,
                  "too late for a call already refused — that is what a "
                  "recomputed share costs against a reservation")

    # The remainder, handed back by not being handed out. `fair` is recomputed
    # per call, so a step that finishes stops being counted and the ones still
    # running see a wider share on their very next call — no wakeup, no
    # reservation to reclaim, one integer of state.
    with _gws_run.budget() as pool:
        with _gws_run.budget() as one:
            with _gws_run.budget() as two:
                with _gws_run.budget() as three:
                    check("with three shares open each may spend a third",
                          (one.fair, two.fair, three.fair), (4, 4, 4))
                check("and when one closes the survivors' share grows",
                      (one.fair, two.fair), (6, 6), "STOP_AT over the two left")
            check("down to the whole thing when it is the only one left",
                  one.fair, _gws_run.STOP_AT)
        check("which is what a step running on its own always had",
              pool.fair, _gws_run.STOP_AT, "one step of a plan is the question")

    # Never nothing. A step given no calls at all cannot say anything about
    # what it was asked, and the answer it writes is a step of somebody's
    # answer — floor of 12/13 is zero, and a zero share is a mute branch.
    with _gws_run.budget() as pool:
        pool.live = _gws_run.STOP_AT + 1
        with _gws_run.budget() as thin:
            check("a share is never zero, however many are running",
                  thin.fair, 1, f"{pool.live} shares over {_gws_run.STOP_AT}")

    # And the pool's own ceiling still ends the question, whatever the shares
    # say. Two branches with six each is twelve; the thirteenth call is refused
    # on the count, not on a share.
    with _gws_run.budget() as pool:
        with _gws_run.budget():
            for _ in range(_gws_run.STOP_AT):
                _gws_run.spend()
            check("the pool's ceiling outranks a share that has room left",
                  _gws_run.spend(), _gws_run.OUT_OF_CALLS)
        check("and a refused call is not charged", pool.spent,
              _gws_run.STOP_AT, "it used to read 13, which is a call nobody "
              "ran being held against the branch that did not make it")

    check("out of share is a refusal", _gws_run.refused(
        _gws_run.OUT_OF_SHARE.format(spent=4)), True)
    check("and it does not tell the step to write the whole answer",
          "this step" in _gws_run.OUT_OF_SHARE, True,
          "its siblings are still going; it is writing one part")


async def gws_clock_cases() -> None:
    print("\n== the gws budget running out of clock ==")

    # The two limits used to be independent, and that is the whole bug: the
    # count's escape hatch fires on the twelfth call, the sub-agent is told to
    # start writing, and `gws.TIMEOUT` cancels the turn 2.5 s later. A question
    # that found four emails and read three came back as "there is no answer".
    with _gws_run.budget():
        check("no seconds given, no deadline",
              _gws_run._DEADLINE.get(), None)
        check("and the count is all that stops it",
              [_gws_run.spend() for _ in range(_gws_run.WARN_AT - 1)],
              [None] * (_gws_run.WARN_AT - 1))

    # The number itself, because a reserve is only worth having if it is wider
    # than a model turn. 5.9 s was the mean over the fifteen turns of the
    # question that prompted this, so under about fifteen is the same bug with
    # a smaller number: the instruction to write lands with no room to write in.
    check("the reserve is wider than the answer it is holding room for",
          _gws_run.RESERVE >= 15.0, True, f"{_gws_run.RESERVE}s")

    with _gws_run.budget(seconds=1000):
        check("with room to spare the clock says nothing",
              _gws_run.spend(), None)
        plenty = _gws_run._DEADLINE.get()
        check("and the deadline is out where it was put",
              plenty is not None and
              plenty - asyncio.get_running_loop().time() > 999, True)

    # Under RESERVE from the start, which is what the last seconds of a slow
    # question look like: still calls in hand, no time to use them.
    with _gws_run.budget(seconds=_gws_run.RESERVE / 2) as tight:
        check("inside the reserve the first call is refused",
              _gws_run.spend(), _gws_run.OUT_OF_TIME,
              "before the count has gone anywhere near twelve")
        check("with the count nowhere near spent", tight.spent, 1)

    # The deadline is a `loop.time()` reading and there is no supported way to
    # move it, so the reserve is shrunk instead and the gap genuinely waited
    # out. This is the transition the fix turns on: calls allowed while there
    # is room to answer in, refused the moment there is not.
    reserve = _gws_run.RESERVE
    try:
        _gws_run.RESERVE = 0.1
        with _gws_run.budget(seconds=0.35):
            check("outside the reserve a call still runs", _gws_run.spend(),
                  None, "there is time to write the answer in")
            await asyncio.sleep(0.3)
            check("and inside it, a moment later, it does not",
                  _gws_run.spend(), _gws_run.OUT_OF_TIME)
    finally:
        _gws_run.RESERVE = reserve

    # One question, one count — but the nearest clock, and that is where the
    # two limits part company. A plan's four gws steps share the tally, and
    # each of them is cancelled by an `asyncio.timeout` of its own counted from
    # its own start, so the deadline `spend` holds `RESERVE` back from has to
    # be the nearest one in force and not the outermost.
    with _gws_run.budget(seconds=1000) as outer:
        was = _gws_run._DEADLINE.get()
        with _gws_run.budget(seconds=1) as inner:
            check("a nested entry still shares the tally already running",
                  inner.pool is outer, True, "the count stays shared")
            check("but pulls the deadline in to its own",
                  _gws_run._DEADLINE.get() < was, True,
                  "the step is cancelled by its own timeout, not the plan's")
            check("so inside the step's reserve the call is refused",
                  _gws_run.spend(), _gws_run.OUT_OF_TIME)
        check("and the tightening is undone on the way out",
              _gws_run._DEADLINE.get(), was)
        check("so the plan's own clock is what is left in force",
              _gws_run.spend(), None)

    # Tighten only, never loosen. A step cannot award itself more clock than
    # the plan holding it has: the plan's `asyncio.timeout` would cancel it
    # anyway, with its own bookkeeping still saying there was room to write in
    # — which is the exact failure this guard exists to stop.
    with _gws_run.budget(seconds=_gws_run.RESERVE / 2):
        near = _gws_run._DEADLINE.get()
        with _gws_run.budget(seconds=1000):
            check("a nested entry cannot push the deadline out",
                  _gws_run._DEADLINE.get(), near, "min, never max")
            check("and is refused on the outer clock all the same",
                  _gws_run.spend(), _gws_run.OUT_OF_TIME)

    # No clock of its own means nothing to contribute, not permission to clear
    # the one in force. Every caller outside `gws` and `plan` opens the block
    # that way — `gws_bench.py` does, and so does half of this file.
    with _gws_run.budget(seconds=_gws_run.RESERVE / 2):
        near = _gws_run._DEADLINE.get()
        with _gws_run.budget():
            check("an entry with no clock of its own leaves the one in force",
                  _gws_run._DEADLINE.get(), near)
            check("rather than handing back the seconds the outer one spent",
                  _gws_run.spend(), _gws_run.OUT_OF_TIME)

    # Both spent. The count is what gets said, because it is the one the
    # sub-agent has been warned about since the sixth call.
    with _gws_run.budget(seconds=0.001) as done:
        done.spent = _gws_run.STOP_AT
        check("out of calls and out of clock reports the calls",
              _gws_run.spend(), _gws_run.OUT_OF_CALLS)

    ran: list[list[str]] = []

    # Sync, because `gws` reaches it through `asyncio.to_thread`. A coroutine
    # here would break on the path where the command is *not* refused, which
    # is the path a mutation that removes the refusal takes.
    def never(argv, command):
        ran.append(argv)
        return {"returncode": 0, "stdout": "", "stderr": ""}, []

    run_sync = _gws_run._run_sync
    _gws_run._run_sync = never
    try:
        with _gws_run.budget(seconds=_gws_run.RESERVE / 2):
            refused = await _gws_run.gws("gws gmail messages list")
        check("a refused command comes back as an error, not an answer",
              refused.get("error"), _gws_run.OUT_OF_TIME)
        check("and nothing was run", ran, [],
              "'Nothing was run' has to be true here too")
    finally:
        _gws_run._run_sync = run_sync


def _read(ident: str, **params) -> str:
    """One `gmail messages get`, however much of the message is asked for."""
    blob = json.dumps({"userId": "me", "id": ident, **params})
    return f"gws gmail messages get --params '{blob}'"


async def gws_repeat_cases() -> None:
    print("\n== the gws budget charging repeats ==")

    ident = "19e8186d37a77929"
    key = f'gmail messages get {{"id": "{ident}", "userId": "me"}}'

    # The four ways one message was read in #57, and what each of them keys on.
    # `--format json` is an output flag: it changes how the CLI prints and not
    # what it fetches, so the two forms of the same read have to be one key or
    # the count never reaches the threshold.
    full = shlex.split(_read(ident, format="full"))[1:]
    printed = shlex.split(_read(ident, format="full") + " --format json")[1:]
    projected = shlex.split(_read(ident, fields="payload/parts"))[1:]
    check("how it will be printed is not part of what it fetches",
          _gws_run._target(printed), _gws_run._target(full))
    check("nor is which slice of the JSON is asked for",
          _gws_run._target(projected), _gws_run._target(full),
          "one message read three ways is one thing read three times")
    # The key is the whole request minus the printing, so it spells the
    # parameters out rather than picking one id off them. `format` is dropped
    # on the way in, which is why it is absent from a command that carries it.
    check("and the key names the message", _gws_run._target(full), key)

    other = shlex.split(_read("1a07950575bfbcc3", format="full"))[1:]
    check("a different message is a different key",
          _gws_run._target(other) != _gws_run._target(full), True)

    # gws takes the blob either way, and a command can carry both flags.
    inline = ["gmail", "messages", "get",
              "--params=" + json.dumps({"userId": "me", "id": ident,
                                        "format": "full"})]
    check("a params blob written with an equals sign reads the same",
          _gws_run._target(inline), _gws_run._target(full))
    check("a blob that will not parse is not an exception",
          _gws_run._target(["gmail", "messages", "get", "--params", "{oops"]),
          "gmail messages get")
    check("and a command carrying two of them reads the later one",
          _gws_run._target(["docs", "documents", "get",
                            "--params", json.dumps({"documentId": "a"}),
                            "--json", json.dumps({"documentId": "b"})]),
          'docs documents get {"documentId": "b"}',
          "which is what the CLI does with them")

    # The `+read` shortcuts take the id as a plain flag rather than inside
    # `--params`, and five of the twelve calls in #57 were written that way.
    # Read only the params and they all collapse to one key, which would refuse
    # the third *different* message somebody looked at.
    flagged = ["gmail", "+read", "--id", ident, "--headers"]
    equals = ["gmail", "+read", f"--id={ident}", "--headers"]
    check("an id written as a flag is found too",
          _gws_run._target(flagged), f"gmail +read {ident}")
    check("written either way", _gws_run._target(equals),
          _gws_run._target(flagged))
    check("and a different message is still a different key",
          _gws_run._target(["gmail", "+read", "--id", "1a07", "--headers"])
          != _gws_run._target(flagged), True)

    # Nothing in a search names an object, so the parameters are the question.
    # Three weeks of calendar are three questions, not one asked three times.
    def events(start):
        blob = json.dumps({"calendarId": "primary", "timeMin": start})
        return shlex.split(f"gws calendar events list --params '{blob}'")[1:]

    check("with nothing named, the parameters are the key",
          _gws_run._target(events("2026-06-01")) !=
          _gws_run._target(events("2026-06-08")), True)
    check("and with no parameters either, the command is",
          _gws_run._target(["gmail", "+triage"]), "gmail +triage")

    # The count itself. Two reads of one message are a narrow one and a wider
    # one; the third is the waste.
    with _gws_run.budget() as tally:
        first = _gws_run.spend(_gws_run._target(full))
        second = _gws_run.spend(_gws_run._target(printed))
        third = _gws_run.spend(_gws_run._target(projected))
        check("the first fetch of a thing runs", first, None)
        check("so does the second, which is how a read gets wider", second,
              None, "+read --headers then messages get format=full")
        check("the third is refused", _gws_run.refused(third), True)
        check("and says how many times it has already run", third,
              _gws_run.REPEATED.format(n=2, target=key))
        check("the number is what ran, not what was asked for",
              "already run this 2 times" in third, True)
        check("a refused repeat is still charged against the count",
              tally.spent, 3, "or the model retries it for free, forever")
        check("and is not tallied again", tally.seen, {key: 2})
        check("a different thing is unaffected",
              _gws_run.spend(_gws_run._target(other)), None)

    # The key is not only a key: it goes back to the sub-agent inside the
    # refusal to say *which* request was already made. Two ways that reading
    # broke when the whole blob went in.
    #
    # This terminal is asked questions in Japanese, and the query lands in the
    # blob verbatim. `json.dumps` escapes non-ASCII by default, so the sentence
    # naming the repeated request named it as `カード`.
    search = ["gmail", "users", "messages", "list", "--params",
              json.dumps({"q": "after:2026/05/31 (カード OR 明細)"})]
    check("the request quoted back is readable in Japanese",
          "カード" in _gws_run._target(search), True,
          "not \\u30ab\\u30fc\\u30c9, which names no query anyone can read")

    # And a repeated write echoes its whole body. Ten rows is a 709-character
    # refusal; a 40x12 sheet passes six thousand. Cut in the message only — the
    # tally still keys on the whole body, or two long bodies sharing a prefix
    # would be one request.
    rows = [[f"r{i}c{j}" for j in range(4)] for i in range(10)]
    append = (f"gws sheets spreadsheets values append --params "
              f"'{json.dumps({'spreadsheetId': 'S', 'range': 'A1'})}' "
              f"--json '{json.dumps({'values': rows})}'")
    long_key = _gws_run._target(shlex.split(append)[1:])
    check("a long body is still keyed whole", len(long_key) > 400, True,
          f"{len(long_key)} chars")
    with _gws_run.budget() as bulky:
        bulky.seen[long_key] = _gws_run.REPEAT_AT - 1
        said = _gws_run.spend(long_key)
    check("but the refusal does not read the body back out",
          len(said) < 400, True,
          f"{len(said)} chars of refusal for {len(long_key)} of key")
    check("with the quoted part bounded by TARGET_SHOWN",
          len(said) - len(_gws_run.REPEATED.format(n=2, target="")),
          _gws_run.TARGET_SHOWN + 1, "the one extra character is the ellipsis")
    check("and says enough of it to be recognised",
          said.startswith("You have already run this 2 times: sheets "
                          "spreadsheets values append"), True)

    with _gws_run.budget() as fresh:
        check("the tally is per question", fresh.seen, {})
        check("so the next question may read it again",
              _gws_run.spend(_gws_run._target(full)), None)

    # One question, one tally — the same rule the count and the clock follow.
    with _gws_run.budget():
        _gws_run.spend(_gws_run._target(full))
        with _gws_run.budget():
            _gws_run.spend(_gws_run._target(full))
            check("a plan's steps share it",
                  _gws_run.refused(_gws_run.spend(_gws_run._target(full))),
                  True, "four gws steps are one question")

    # `_agenda.agenda` charges the count without being a gws command, so it has
    # no request to be a repeat of.
    with _gws_run.budget() as agenda:
        check("no target, nothing tallied",
              [_gws_run.spend() for _ in range(4)], [None] * 4)
        check("and the count still moved", agenda.spent, 4)
        check("with nothing to repeat", agenda.seen, {})

    # The predicate the call site uses. A warning is not a refusal, and the
    # three refusals are told from it by the sentence they share.
    check("a warning is not a refusal",
          _gws_run.refused(_gws_run.LEFT.format(spent=6, total=12)), False)
    check("nor is having said nothing", _gws_run.refused(None), False)
    check("out of calls is", _gws_run.refused(_gws_run.OUT_OF_CALLS), True)
    check("out of time is", _gws_run.refused(_gws_run.OUT_OF_TIME), True)
    check("a repeat is", _gws_run.refused(
        _gws_run.REPEATED.format(n=2, target="x")), True)

    # Three and not two, deliberately: a second read with a wider projection is
    # how an ordinary question works, and a threshold of two would break it.
    check("two fetches of one thing are allowed", _gws_run.REPEAT_AT, 3,
          "a narrow read then a wider one is not waste")

    # Which of the three refusals gets said when more than one applies. The
    # repeat is last because it is the only one the sub-agent can do something
    # about: the other two end the question, and this one ends the command and
    # says to ask a different one.
    with _gws_run.budget(seconds=_gws_run.RESERVE / 2) as squeezed:
        squeezed.seen[key] = _gws_run.REPEAT_AT - 1
        check("out of clock and repeating reports the clock",
              _gws_run.spend(key), _gws_run.OUT_OF_TIME,
              "one ends the question, the other only ends the command")
    with _gws_run.budget() as maxed:
        maxed.spent = _gws_run.STOP_AT
        maxed.seen[key] = _gws_run.REPEAT_AT - 1
        check("out of calls and repeating reports the calls",
              _gws_run.spend(key), _gws_run.OUT_OF_CALLS)

    ran: list[list[str]] = []

    def never(argv, command):
        ran.append(argv)
        return {"returncode": 0, "stdout": "", "stderr": ""}, []

    run_sync = _gws_run._run_sync
    _gws_run._run_sync = never
    try:
        with _gws_run.budget():
            out = [await _gws_run.gws(_read(ident, format="full")),
                   await _gws_run.gws(_read(ident, format="full")
                                      + " --format json"),
                   await _gws_run.gws(_read(ident, fields="payload/parts"))]
        check("through the tool, the first two run",
              [bool(r.get("error")) for r in out], [False, False, True])
        check("the third comes back as an error, not an answer",
              _gws_run.refused(out[2].get("error")), True)
        check("and only two commands were run", len(ran), 2,
              "'Nothing was run' has to be true here too")
    finally:
        _gws_run._run_sync = run_sync


async def gws_false_refusal_cases() -> None:
    print("\n== what the repeat rule must not refuse ==")

    ran: list[list[str]] = []

    def never(argv, command):
        ran.append(argv)
        return {"returncode": 0, "stdout": "", "stderr": ""}, []

    async def sequence(commands: list[str]) -> list[bool]:
        """Run these as one question; report which of them were refused."""
        ran.clear()
        run_sync = _gws_run._run_sync
        _gws_run._run_sync = never
        try:
            with _gws_run.budget():
                out = [await _gws_run.gws(c) for c in commands]
        finally:
            _gws_run._run_sync = run_sync
        return [_gws_run.refused(r.get("error")) for r in out]

    # A write is never the same fetch twice. None of `summary`, `start` or
    # `end` is in `_NAMES` and the skill writes them as plain flags, so all
    # three of these used to key on `calendar +insert` alone: two events landed
    # instead of three, and the third was told it had already run twice.
    inserts = [f"gws calendar +insert --summary {what} --start 2026-09-10"
               for what in ("Standup", "Review", "Retro")]
    check("three events in one question are three events",
          await sequence(inserts), [False] * 3)
    check("and all three reached the CLI", len(ran), 3)
    check("a write is keyed on nothing at all",
          _gws_run._writes(shlex.split(inserts[0])[1:]), True)

    sends = ["gws gmail +send --to a@b.c --subject one --body hi",
             "gws gmail +send --to a@b.c --subject two --body hi",
             "gws gmail +send --to a@b.c --subject three --body hi"]
    check("so are three mails", await sequence(sends), [False] * 3)

    # The generic spelling of the same thing, where the verb is a subcommand
    # word rather than a `+` shortcut.
    check("and the generic verbs count as writes",
          [_gws_run._writes(shlex.split(f"gws drive files {verb}")[1:])
           for verb in ("create", "update", "delete", "copy", "list")],
          [True, True, True, True, False])
    # Read off the leading words, so a value cannot masquerade as a verb. This
    # fails open either way — a read mistaken for a write is only untallied —
    # but an untallied read is the bug this rule exists to catch.
    check("a search for the word is not a send",
          _gws_run._writes(shlex.split("gws gmail +search --query send")[1:]),
          False)
    # Writes still cost the question, or twelve of them empty the budget for
    # free. Only the repeat tally skips them. Through `gws` and not through
    # `spend`, because the exemption is written at the call site and a case
    # that calls `spend` itself cannot see the call site drop the charge.
    run_sync = _gws_run._run_sync
    _gws_run._run_sync = never
    try:
        with _gws_run.budget() as tally:
            await _gws_run.gws(inserts[0])
            check("a write is still charged against the count",
                  tally.spent, 1)
            check("and leaves the tally alone", tally.seen, {})
    finally:
        _gws_run._run_sync = run_sync

    # The same `q` over three windows is three questions. `gws.py:341-343`
    # tells the sub-agent to hunt an event by name exactly this way, so this is
    # an instructed shape; `_target` used to return at the first `_NAMES` hit
    # and throw the window away with the rest of the parameters.
    def window(lo: str, hi: str) -> str:
        blob = json.dumps({"calendarId": "primary", "q": "dentist",
                           "timeMin": lo, "timeMax": hi})
        return f"gws calendar events list --params '{blob}'"

    months = [window("2026-05-01", "2026-05-31"),
              window("2026-06-01", "2026-06-30"),
              window("2026-07-01", "2026-07-31")]
    check("one name across three months is three questions",
          await sequence(months), [False] * 3)
    check("and the key carries the window, not just the name",
          _gws_run._target(shlex.split(months[0])[1:]),
          'calendar events list {"calendarId": "primary", "q": "dentist", '
          '"timeMax": "2026-05-31", "timeMin": "2026-05-01"}')
    check("so two windows are two keys",
          _gws_run._target(shlex.split(months[1])[1:])
          != _gws_run._target(shlex.split(months[0])[1:]), True)
    # And the rule this issue was about is still standing: the same window
    # asked a third time is still a repeat.
    check("but the same window three times is not",
          await sequence(months[:1] * 3), [False, False, True])

    # A range is the sheets spelling of a window, and it arrives as a plain
    # flag rather than inside a blob.
    quarters = [f"gws sheets +read --spreadsheet SID --range Q{n}"
                for n in (1, 2, 3)]
    check("three ranges of one sheet are three reads",
          await sequence(quarters), [False] * 3)
    check("while one range three times is not",
          await sequence(quarters[:1] * 3), [False, False, True])

    # `_NAMES` carried `message`, and `_flag` only matches `--message`; the
    # real flag is `--message-id`, 21 times across the vendored skills and
    # never once bare. So every `+forward` in a question keyed alike.
    forwards = [f"gws gmail +forward --message-id {mid} --to a@b.c"
                for mid in ("19e8186d", "19e8199c", "1a079505")]
    check("three forwards of three messages are three messages",
          await sequence(forwards), [False] * 3)
    check("and the id is in the key",
          "19e8186d" in _gws_run._target(shlex.split(forwards[0])[1:]), True)
    # `+forward` is a write, so the case above passes on the write exemption
    # alone. `+read` is the same flag on the path where the id has to be in the
    # key, and it is the one that pins the flag name.
    reads = [f"gws gmail +read --message-id {mid} --headers"
             for mid in ("19e8186d", "19e8199c", "1a079505")]
    check("as it is for a read, which is not exempt from the rule",
          [_gws_run._target(shlex.split(r)[1:]) for r in reads],
          [f"gmail +read {mid}"
           for mid in ("19e8186d", "19e8199c", "1a079505")])
    check("so three messages read in one question are three reads",
          await sequence(reads), [False] * 3)

    # Naming *one* object was never enough. Each of these names a file and a
    # child of it, and the key used to be the file: every proposal on one file
    # was the same write, every comment on one file the same read. A write
    # exemption cannot reach the reads, which is why the key and not `_WRITES`
    # is what moved.
    def resolve(proposal: str) -> str:
        blob = json.dumps({"fileId": "F1", "proposalId": proposal,
                           "action": "ACCEPT"})
        return f"gws drive accessproposals resolve --params '{blob}'"

    def comment(cid: str) -> str:
        blob = json.dumps({"fileId": "F1", "commentId": cid, "fields": "*"})
        return f"gws drive comments get --params '{blob}'"

    def label(lid: str) -> str:
        params = json.dumps({"fileId": "F1"})
        body = json.dumps({"labelModifications": [{"labelId": lid}]})
        return (f"gws drive files modifyLabels --params '{params}' "
                f"--json '{body}'")

    def row(value: str) -> str:
        params = json.dumps({"spreadsheetId": "S1", "range": "Sheet1!A1"})
        body = json.dumps({"values": [[value]]})
        return (f"gws sheets spreadsheets values append --params '{params}' "
                f"--json '{body}'")

    def getf(**kw) -> str:
        return _gws_run._target(["drive", "files", "get", "--params",
                                 json.dumps({"fileId": "F1", **kw})])

    def msg(fmt: str) -> str:
        blob = json.dumps({"userId": "me", "id": "M1", "format": fmt})
        return f"gws gmail users messages get --params '{blob}'"

    check("three proposals on one file are three writes",
          await sequence([resolve(p) for p in ("P1", "P2", "P3")]),
          [False] * 3)
    check("and the same proposal three times is still a repeat",
          await sequence([resolve("P1")] * 3), [False, False, True])
    check("three comments on one file are three reads",
          await sequence([comment(c) for c in ("C1", "C2", "C3")]),
          [False] * 3)
    check("three label edits on one file are three edits",
          await sequence([label(lid) for lid in ("L1", "L2", "L3")]),
          [False] * 3)
    check("without modifyLabels having to be named a write",
          _gws_run._writes(["drive", "files", "modifyLabels"]), False)
    # Keep this one forever. `modifyLabels` is missed today only because the
    # match is whole-token, and the obvious wrong fix is a substring or prefix
    # match — which takes `listLabels` with it and silently untallies a read.
    check("and a substring is still not a match",
          _gws_run._writes(["drive", "files", "listLabels"]), False)
    check("three rows appended to one range are three rows",
          await sequence([row(v) for v in ("a", "b", "c")]), [False] * 3)

    # The other direction, which is the rule #57 asked for: what only changes
    # the printing is dropped before the key is built, so the same fetch asked
    # for three ways is still one thing fetched three times.
    # `fields` alone, so this case is about the projection and nothing else.
    # It used to carry a `pageSize` on one side as well, which meant it also
    # stood in for the page-size trade and failed first whenever that moved —
    # the trade has its own case below now.
    check("three projections of one file are still one read",
          getf(fields="id"), getf(fields="name,mimeType"))
    check("but alt=media is a different request, not a different printing",
          getf(alt="media") != getf(), True,
          "a file's bytes are not its metadata printed differently")
    check("and a body makes a write tell itself apart with no verb list",
          _gws_run._target(shlex.split(label("L1"))[1:])
          != _gws_run._target(shlex.split(label("L2"))[1:]), True)
    check("the #57 pathology still bites",
          await sequence([msg(fmt) for fmt in ("full", "metadata", "raw")]),
          [False, False, True])
    # The deliberate stretch in `_OUTPUT`, pinned so nobody flips it back by
    # accident. `pageSize` changes how much answer comes back and not only how
    # it is printed, so this is the one place the refusal's "It will not return
    # anything new" is false — and it is kept anyway, because `pageToken` is
    # the real pagination mechanism and letting one search spend three calls
    # bumping its page size is the worse trade. Written in the `--params`
    # spelling `skills/gws-shared/SKILL.md:73` demonstrates, which is the one
    # that actually reaches this: with a `q` alongside, `_NAMES` no longer
    # short-circuits, but the blob carries the sizes either way.
    check("and bumping the page size is not a new question",
          await sequence([f"gws drive files list --params '{{\"pageSize\": "
                          f"{n}}}'" for n in (5, 25, 100)]),
          [False, False, True])
    check("under gmail's spelling of the same parameter too",
          _gws_run._target(["gmail", "users", "messages", "list", "--params",
                            json.dumps({"q": "x", "maxResults": 50})]),
          _gws_run._target(["gmail", "users", "messages", "list", "--params",
                            json.dumps({"q": "x", "maxResults": 5})]),
          "maxResults is pageSize by another name")
    # The other eight names had no case at all: dropping `$.xgafv`, `quotaUser`
    # or any of the credential parameters from `_OUTPUT` left the whole suite
    # green. Two cases for the set — the membership written down, because a
    # sweep over the set itself would pass whatever the set happens to hold,
    # and then the sweep, because being in the list is not the same as being
    # dropped.
    check("and _OUTPUT is the closed list its comment claims",
          sorted(_gws_run._OUTPUT),
          sorted(("fields", "format", "pageSize", "maxResults", "prettyPrint",
                  "quotaUser", "key", "access_token", "oauth_token", "userIp",
                  "callback", "$.xgafv")),
          "adding a name here means arguing for it in the comment")
    check("and every name in it drops out of the key",
          sorted(n for n in _gws_run._OUTPUT if getf(**{n: "1"}) != getf()),
          [])
    check("while a different page of the same search is",
          await sequence([f"gws drive files list --params "
                          f"'{{\"q\": \"x\", \"pageToken\": \"{t}\"}}'"
                          for t in ("p1", "p2", "p3")]),
          [False, False, False], "pageToken is the mechanism, not the size")

    # The twelve calls in the issue body still behave the way #57 left them:
    # call 8 is the third fetch of one message and is the one that goes.
    ident = "19e8186d37a77929"
    check("and the question this rule was written for still refuses call 8",
          await sequence([_read(ident, format="full"),
                          _read(ident, format="full") + " --format json",
                          _read(ident, fields="payload/parts")]),
          [False, False, True])


# ---------------------------------------------------------------- the screen

def screen_cases() -> None:
    print("== which screen the work draws on ==")

    ctx = FakeContext(session_id="sub-run", call_id="call-sub")

    check("with no live block, a tool asks its own context",
          screen.session_of(ctx), "sub-run")
    check("and there is no card to draw a plan on",
          screen.live_card(), None)

    with screen.live("live-1", "call-live"):
        check("inside one, the live session wins",
              screen.session_of(ctx), "live-1")
        check("the plan's own card is both halves",
              screen.live_card(), ("live-1", "call-live"))

    check("and the block puts it back", screen.session_of(ctx), "sub-run")
    check("both halves gone", screen.live_card(), None)

    # The distinction the whole thing turns on: a nested tool gets the live
    # session so its frames arrive, and its own call id so they land in a card
    # of their own rather than overwriting the plan's.
    with screen.live("live-1", "call-live"):
        notes = _progress.Notes(FakeContext("sub-run", "call-gws"), "gws")
        check("a nested tool's notes go to the live screen",
              notes.session_id, "live-1")
        check("under its own call id", notes.call_id, "call-gws",
              "not the plan's, or gwsFill would overwrite the plan card")


async def show_plan_cases() -> None:
    print("== show_plan ==")

    drawn: list[tuple[str, dict]] = []

    async def sink(frame: dict) -> None:
        drawn.append(("live-1", frame))

    screen.register("live-1", sink)
    try:
        outside = await _plan_run.show_plan(["a", "b"], [0])
        check("with no live call above it, it draws nothing and says so",
              outside,
              {"shown": False, "steps": 2, "running": [0], "done": []})
        check("and nothing reached a screen", drawn, [])

        with screen.live("live-1", "call-live"):
            got = await _plan_run.show_plan(["find dinner", "check it is open",
                                             "check the rain"], [1], [0])
        check("inside one it reports drawn", got,
              {"shown": True, "steps": 3, "running": [1], "done": [0]})
        check("on the plan's own card", drawn[-1][1], {
            "type": "plan", "id": "call-live",
            "steps": ["find dinner", "check it is open", "check the rain"],
            "running": [1], "done": [0],
        })

        # The whole point of #62 on the screen: a cursor could say one step was
        # running, and three independent lookups go out together.
        with screen.live("live-1", "call-live"):
            wide = await _plan_run.show_plan(["a", "b", "c", "d"], [1, 2, 3])
        check("three steps can be running at once", wide["running"],
              [1, 2, 3], "which is a thing a cursor could not say")

        with screen.live("live-1", "call-live"):
            await _plan_run.show_plan([1, 2], [0])
        check("steps are stringified on the way out",
              drawn[-1][1]["steps"], ["1", "2"],
              "the model is loose about this and the card renders text")

        # Everything below is the model being loose about the two lists, which
        # it will be. This is the one call the person can see, so it draws
        # something for anything it is handed rather than raising over it — the
        # same argument the `0 <= current < len(steps)` guard used to make.
        with screen.live("live-1", "call-live"):
            await _plan_run.show_plan(["a", "b"], 1, "0")
        check("a bare index is a list of one, on either side",
              (drawn[-1][1]["running"], drawn[-1][1]["done"]), ([1], [0]),
              "and a numeric string is an index")

        with screen.live("live-1", "call-live"):
            await _plan_run.show_plan(["a", "b"], [1, 1, 0])
        check("repeats and order are not the model's problem",
              drawn[-1][1]["running"], [0, 1])

        with screen.live("live-1", "call-live"):
            await _plan_run.show_plan(["a", "b"], [7, -1, "x", None], [9])
        check("and an index that is not one is dropped",
              (drawn[-1][1]["running"], drawn[-1][1]["done"]), ([], []),
              "an IndexError here takes the whole picture away")

        # Both, which the model will do — it is reporting what came back and
        # what went out in the same call, and a step it has just re-run is in
        # its notion of both. Running wins: the one still going is the more
        # useful thing to look at, and `planUpdate` is not asked to decide.
        with screen.live("live-1", "call-live"):
            both = await _plan_run.show_plan(["a", "b", "c"], [1], [0, 1])
        check("a step named as running and done is drawn as running",
              (both["running"], both["done"]), ([1], [0]))

        with screen.live("live-1", "call-live"):
            quiet = await _plan_run.show_plan(["a", "b"])
        check("and neither list is required", quiet,
              {"shown": True, "steps": 2, "running": [], "done": []},
              "a plan shown before anything has started")
    finally:
        screen.unregister("live-1")


async def show_plan_voice_cases() -> None:
    print("== show_plan on the voice channel ==")

    said: list[tuple[str, str]] = []
    notes = _progress.Notes(FakeContext("live-1", "call-live"), "plan",
                            on_screen=False)
    notes.note = lambda state, text: said.append((state, text))

    async def spoken(*args) -> list:
        """What `show_plan` says out loud, with the channel set up for it."""
        said.clear()
        # Set the way `Notes.start` sets it, which is what puts the channel
        # where `_progress.note` can find it two levels down.
        token = _progress._CURRENT.set(notes)
        try:
            await _plan_run.show_plan(*args)
        finally:
            _progress._CURRENT.reset(token)
        return list(said)

    check("the running step is spoken",
          await spoken(["find dinner", "check the rain"], [1]),
          [("step", "Step 2 of 2: check the rain")],
          "the card is off (on_screen=False), so this is the only channel")

    # One sentence for a fan-out, and it names the steps. Three tools starting
    # together is three nested `Notes` opening, and none of them can speak — a
    # plan's sub-run has no live queue — so this is the whole account of it.
    # "Three steps at once" would tell the person nothing the screen does not.
    check("and several running steps are one sentence between them",
          await spoken(["find dinner", "check the rain", "read my mail"],
                       [0, 2]),
          [("step", "2 steps of 3 at once: find dinner; read my mail")])

    check("an index past the end says nothing",
          await spoken(["a", "b"], [7]), [],
          "the model gets this wrong and an IndexError would kill the step")
    check("and neither does a plan with nothing running yet",
          await spoken(["a", "b"], [], [0, 1]), [],
          "shown before it starts, and shown again when it is over")


# --------------------------------------------------------------- the history

def readable_cases() -> None:
    print("== one event, as the planner reads it ==")

    check("what the person said",
          plan_mod._readable(Event(heard="somewhere for dinner")),
          ("person", "somewhere for dinner"))
    check("what the assistant said",
          plan_mod._readable(Event(said="I'll have a look")),
          ("you", "I'll have a look"))
    check("an event that says nothing at all",
          plan_mod._readable(Event()), None)
    check("an empty transcription is nothing too",
          plan_mod._readable(Event(heard="")), None)

    check("text from a half-cascade turn",
          plan_mod._readable(Event(parts=[part(text="two of them")])),
          ("you", "two of them"))
    check("the person's, when the role says so",
          plan_mod._readable(Event(role="user", parts=[part(text="hello")])),
          ("person", "hello"))
    check("a thought is not something anybody said",
          plan_mod._readable(Event(parts=[part(text="hmm", thought=True)])),
          None)

    called = Event(parts=[part(call="weather", args={"location": "Ebisu",
                                                     "lang": "ja"})])
    check("a tool call, as the one line that stops a step repeating",
          plan_mod._readable(called), ("you", "[called weather: location=Ebisu]"))
    check("`lang` and `context` are dropped from the arguments",
          "lang" in plan_mod._readable(called)[1], False,
          "they are plumbing, not what was looked up")

    long = Event(parts=[part(call="web_search",
                             args={"query": "x" * 200})])
    check("and a long argument is cut",
          len(plan_mod._readable(long)[1]) < 200, True,
          plan_mod._readable(long)[1])


def recent_cases() -> None:
    print("== the conversation as a paragraph ==")

    check("nothing at all", plan_mod._recent([]), "")
    check("None, which is what a session with no events can hand over",
          plan_mod._recent(None), "")

    events = [
        Event(heard="somewhere for dinner"),
        Event(said="I'll have a look."),
        Event(said="Two nearby."),
    ]
    check("consecutive lines from one speaker are joined",
          plan_mod._recent(events),
          "person: somewhere for dinner\nassistant: I'll have a look. Two nearby.",
          "a spoken turn arrives in pieces and would read as six speakers")

    check("reading order, oldest first",
          plan_mod._recent(events).index("dinner")
          < plan_mod._recent(events).index("Two nearby"), True)

    many = [Event(heard=f"{i}", role="user") if i % 2 else Event(said=f"{i}")
            for i in range(10)]
    kept = plan_mod._recent(many, limit=3)
    check("the count cap keeps the newest", kept.count("\n"), 2,
          f"3 lines: {kept!r}")
    check("and drops the oldest", "person: 1" in kept, False)
    check("the newest is there", "9" in kept, True)

    big = [Event(heard="a" * 100, role="user"), Event(said="b" * 100),
           Event(heard="c" * 100, role="user")]
    tight = plan_mod._recent(big, chars=150)
    check("the character cap stops collecting",
          tight.count("\n"), 1, "two lines, the newest two")
    check("but does not cut the last one mid-word",
          "c" * 100 in tight and "b" * 100 in tight, True,
          "soft on the line that crosses the cap")

    check("whitespace is flattened",
          plan_mod._recent([Event(heard="two\n\n  words")]),
          "person: two words")
    check("a line that is only whitespace is not a line",
          plan_mod._recent([Event(heard="   ")]), "")

    # #192. The terminal's own notes are addressed to the model and nobody in
    # the room said them, and `recap` has dropped them since #172 — but the
    # planner reads the same event list through `_readable`, which had no
    # guard at all. Both carriers, because they arrive with different
    # authorship: a progress note is written as the person, and `UNANSWERED`
    # as the model.
    #
    # What makes this worse here than in the recap is the join above: two
    # consecutive lines from one speaker become one line, so an unfiltered
    # note is glued onto the end of the question it interrupted and the
    # planner reads a sentence that opens as the user asking for something
    # and closes as an instruction not to call a tool.
    from ai_terminal.live import main  # noqa: E402  (the real UNANSWERED)

    asked = "what is on today"
    slow = _progress.SAY.format(tool="gws", doing="still looking.")
    said_it = plan_mod._recent([Event(heard=asked, role="user"),
                                Event(heard=slow, role="user")])
    check("a progress note the server wrote is not something the person said",
          "do not call any tool" in said_it, False, repr(said_it))
    check("and the question it interrupted is still a line of its own",
          said_it, f"person: {asked}",
          "joined onto the question, it read as one instruction")

    settled = plan_mod._recent([Event(heard=asked, role="user"),
                                Event(parts=[part(text=main.UNANSWERED)])])
    check("the settle marker is not something the assistant said either",
          "It is history, not a question" in settled, False, repr(settled))
    check("and the question it followed survives it",
          settled, f"person: {asked}")

    # Whichever branch of `_readable` the note arrives through. A note that
    # came back as a transcription is the same note.
    check("dropped as a transcription too, both ways round",
          [plan_mod._readable(Event(heard=slow)),
           plan_mod._readable(Event(said=main.UNANSWERED))], [None, None])
    check("and the marker is spelled once, in the module that writes it",
          [recap.INJECTED,
           slow.startswith(recap.INJECTED),
           main.UNANSWERED.startswith(recap.INJECTED)],
          ["[terminal]", True, True])


def shown_cases() -> None:
    print("== the cards on the screen, as the planner reads them ==")

    check("nothing at all", plan_mod._shown([]), "")
    check("None, which is what a session with no events can hand over",
          plan_mod._shown(None), "")

    # The half of a part `context` has never had. Both halves are read off one
    # event list, and the split between them is the whole design: keep them
    # pinned against each other or a future `_readable` quietly absorbs one.
    answered = Event(parts=[part(response="weather", ident="x",
                                 answered={"location": "Tokyo"})])
    check("`_readable` still says nothing about a tool's answer",
          plan_mod._readable(answered), None,
          "`context` is what was said; the answer is `shown`'s half")

    check("a call nothing has answered yet is a card with nothing on it",
          plan_mod._shown([Event(parts=[part(call="weather", ident="c1",
                                             args={"location": "Ebisu"})])]),
          "", "a spinner is not something anybody can point at")

    places = {"query": "izakaya near Ebisu", "places": [
        {"title": "Bar Trench"}, {"title": "Buri"}, {"title": "Ushitora"}]}
    check("a card is its heading and the things on it, numbered",
          plan_mod._shown(drew("maps_search", places)),
          'maps_search "izakaya near Ebisu": 1. Bar Trench; 2. Buri; '
          '3. Ushitora',
          "the numbers are what \"the second one\" is an ordinal into")
    check("the first thing on a card is number one",
          plan_mod._shown(drew("maps_search", places)).split(": ")[1]
          .startswith("1. "), True, "they count from one, not from zero")

    # A handle, not a cache. Everything a card holds that only the card can use
    # is left behind, which is the ruling `_ARGS_CHARS` already makes for the
    # call side of the same event.
    fat = {"query": "izakaya near Ebisu", "summary": "Three of them. " * 40,
           "places": [{"title": "Bar Trench", "uri": "https://maps/x",
                       "place_id": "ChIJ-long-opaque-identifier"}]}
    line = plan_mod._shown(drew("maps_search", fat))
    check("the summary paragraph is not carried",
          "Three of them" in line, False,
          "a handle to look something up by, not the lookup back")
    check("nor the link under it", "https://maps/x" in line, False)
    check("nor the place id", "ChIJ" in line, False)

    check("a heading that arrived with a newline stays on one line",
          plan_mod._shown(drew("web_search", {"query": "a\n  b"})),
          'web_search "a b"')
    check("and so does a name",
          plan_mod._shown(drew("youtube_search",
                               {"query": "q", "videos": [{"title": "x\ny"}]})),
          'youtube_search "q": 1. x y')

    check("a forecast is headed with where it is for",
          plan_mod._shown(drew("weather", {"location": "Tokyo",
                                           "current": {"temperature": 9}})),
          'weather "Tokyo"')
    check("a route with where it goes",
          plan_mod._shown(drew("maps_route", {"destination": "Shibuya"})),
          'maps_route "Shibuya"')
    check("a place with which place",
          plan_mod._shown(drew("place_info", {"place": "Bar Trench"})),
          'place_info "Bar Trench"')

    # The two that answer `{"answer": ...}`: nothing in the response names the
    # card, and the page heads it with the argument the call was made with.
    check("an answer with no handle in it is headed with what was asked",
          plan_mod._shown(drew("plan", {"answer": "a paragraph"},
                               args={"task": "dinner and an umbrella"})),
          'plan "dinner and an umbrella"')
    check("gws too, under its own argument name",
          plan_mod._shown(drew("gws", {"answer": "two things"},
                               args={"request": "what is on tomorrow"})),
          'gws "what is on tomorrow"')
    check("and a tool with neither is still a card that was drawn",
          plan_mod._shown(drew("something_new", {"ok": True})),
          "something_new")

    # The two answers the page draws nothing at all for. Listed, each would be
    # a line about something nobody can see.
    check("a forecast that failed drew no card, so it is not one",
          plan_mod._shown(drew("weather", {"error": "nowhere to look"})), "",
          "`weatherCard` returns null and `appendCard` drops it")
    check("nor is a repeat of a question already running",
          plan_mod._shown(drew("plan", {"already_running": True,
                                        "note": plan_mod.ALREADY},
                               args={"task": "dinner"})), "",
          "its frame carries `drop`, which takes the card back off")
    check("and the card beside a dropped one is untouched",
          plan_mod._shown(drew("weather", {"error": "no"}, ident="w")
                          + drew("web_search", {"query": "ramen"},
                                 ident="s")),
          'web_search "ramen"')

    # Ordered by the call. `gws` and `plan` put an empty card up when they are
    # called and fill it in place later, so a slow answer belongs where its
    # call is and not at the bottom.
    slow, fast = "c-gws", "c-weather"
    interleaved = [
        Event(parts=[part(call="gws", ident=slow,
                          args={"request": "tomorrow"})]),
        Event(parts=[part(call="weather", ident=fast, args={})]),
        Event(role="user", parts=[part(response="weather", ident=fast,
                                       answered={"location": "Tokyo"})]),
        Event(role="user", parts=[part(response="gws", ident=slow,
                                       answered={"answer": "two things"})]),
    ]
    check("a slow card keeps the place its call put it in",
          plan_mod._shown(interleaved),
          'gws "tomorrow"\nweather "Tokyo"',
          "ordered by the answer, gws would list below a card drawn under it")

    check("an answer whose call is off the front is still a card",
          plan_mod._shown([Event(role="user", parts=[
              part(response="weather", ident="z",
                   answered={"location": "Kyoto"})])]),
          'weather "Kyoto"')

    twice = (drew("maps_search", {"query": "ramen",
                                  "places": [{"title": "Ichiran"}]},
                  ident="a", args={"query": "ramen"})
             + drew("weather", {"location": "Tokyo"}, ident="b")
             + drew("maps_search", {"query": "ramen",
                                    "places": [{"title": "Ichiran"},
                                               {"title": "Afuri"}]},
                    ident="c", args={"query": "ramen"}))
    check("the same question asked twice is one card, the newer one",
          plan_mod._shown(twice),
          'weather "Tokyo"\nmaps_search "ramen": 1. Ichiran; 2. Afuri',
          "a repeat draws its card at the bottom, `app.js:1049`")
    check("a different argument is a different card",
          plan_mod._shown(
              drew("maps_search", {"query": "ramen"}, ident="a",
                   args={"query": "ramen"})
              + drew("maps_search", {"query": "soba"}, ident="b",
                     args={"query": "soba"})),
          'maps_search "ramen"\nmaps_search "soba"')
    check("a repeat still in flight leaves the answered card standing",
          plan_mod._shown(
              drew("gws", {"answer": "tomorrow is empty"}, ident="a",
                   args={"request": "what is on tomorrow"})
              + [Event(parts=[part(call="gws",
                                   args={"request": "what is on tomorrow"},
                                   ident="b")])]),
          'gws "what is on tomorrow"')
    check("and so does one answered `already_running`",
          plan_mod._shown(
              drew("gws", {"answer": "tomorrow is empty"}, ident="a",
                   args={"request": "what is on tomorrow"})
              + drew("gws", {"already_running": True}, ident="b",
                     args={"request": "what is on tomorrow"})),
          'gws "what is on tomorrow"')
    check("a repeat whose own answer drew nothing is the same",
          plan_mod._shown(
              drew("maps_search", {"query": "ramen",
                                   "places": [{"title": "Ichiran"}]},
                   ident="a", args={"query": "ramen"})
              + drew("maps_search", {"error": "quota exceeded"}, ident="b",
                     args={"query": "ramen"})),
          'maps_search "ramen": 1. Ichiran')

    # Eight cards, six of which fit. The count is written out rather than read
    # off `SHOWN`: a case that says `SHOWN - 1` lines holds for every value of
    # `SHOWN` and so pins none of them.
    many = []
    for i in range(8):
        many += drew("web_search", {"query": f"q{i}"}, ident=f"i{i}",
                     args={"query": f"q{i}"})
    window = plan_mod._shown(many)
    check("the card cap keeps the newest six", window.count("\n"), 5,
          f"six lines: {window!r}")
    check("and drops the oldest", '"q0"' in window, False)
    check("the newest is there", '"q7"' in window, True)

    big = []
    for i in range(4):
        big += drew("web_search", {"query": f"{i}{'x' * 200}"},
                    ident=f"b{i}", args={"query": str(i)})
    tight = plan_mod._shown(big)
    check("the character cap stops collecting", tight.count("\n"), 1,
          "two lines, the newest two")
    check("but does not cut the last one mid-word",
          tight.startswith('web_search "2'), True,
          "soft on the line that crosses the cap, the way `_recent` is")

    # Eight things on one card, five of which are named — written out for the
    # same reason the six above is.
    long_card = {"query": "ramen",
                 "places": [{"title": f"shop {i}"} for i in range(8)]}
    listed = plan_mod._shown(drew("maps_search", long_card))
    check("a card names at most five of the things on it",
          listed.count(";"), 4, listed)
    check("and it is the end that goes, so the rest keep their numbers",
          listed.endswith("5. shop 4"), True,
          "cut from the front, every remaining number would be wrong")
    check("so the sixth is gone and the first is still the first",
          ["shop 5" in listed, listed.split(": ")[1].startswith("1. shop 0")],
          [False, True])

    check("a card with several lists names the places on it",
          plan_mod._shown(drew("place_info", {
              "place": "Bar Trench",
              "images": [{"uri": "https://x/1.jpg"}],
              "places": [{"title": "Bar Trench"}],
              "sources": [{"title": "a blog post"}]})),
          'place_info "Bar Trench": 1. Bar Trench',
          "and not the sources under them")
    check("pictures are not named",
          plan_mod._shown(drew("image_search", {
              "query": "sakura", "images": [{"uri": "https://x/1.jpg"}]})),
          'image_search "sakura"',
          "an image has no title and nobody says \"the second picture\"")
    check("an item the card labels with its link keeps its number",
          plan_mod._shown(drew("web_search", {
              "query": "q", "sources": [{"uri": "https://a/b"},
                                        {"title": "B"}]})),
          'web_search "q": 1. https://a/b; 2. B',
          "`linkChips` shows the uri when there is no title")

    # The two numbers, against what #80 measured rather than against taste.
    check("the screen has a budget of its own, under the conversation's",
          plan_mod.SHOWN_CHARS < plan_mod.RECENT_CHARS, True,
          f"{plan_mod.SHOWN_CHARS} against {plan_mod.RECENT_CHARS}: #80 found "
          "the character cap already cutting at 13 of 34 dispatch points")
    check("and it holds one plan turn's worth of screen",
          plan_mod.SHOWN, _plan_run.WIDE,
          "six tools may run in one turn, so six cards is one turn of cards")


def request_cases() -> None:
    print("== the message the planner is handed ==")

    body = plan_mod.PlanRequest(task="dinner", context="", lang="ja")
    check("it is JSON, not prose",
          body.model_dump_json(exclude_none=True),
          '{"task":"dinner","context":"","found":"","earlier":"",'
          '"shown":"","lang":"ja"}')

    bare = plan_mod.PlanRequest(task="dinner")
    check("the five that are not the task default to empty, not missing",
          bare.model_dump(),
          {"task": "dinner", "context": "", "found": "", "earlier": "",
           "shown": "", "lang": ""},
          "`plan` always sends all six, but the schema is what the model "
          "is shown")
    check("what was said and what was found are separate fields",
          [f for f in ("context", "found") if f in plan_mod.PlanRequest.
           model_fields], ["context", "found"],
          "`_recent` can rebuild `context` from the events and cannot rebuild "
          "`found` at all — `_readable` never reads a function_response")
    # And the third, which is neither: a summary of a conversation that has
    # already ended. Folded into `context` it would read as something said in
    # this one, which is the sentence `agent._instruction` spends four lines
    # stopping the live model believing.
    check("and so is what the conversation before this one was about",
          "earlier" in plan_mod.PlanRequest.model_fields, True,
          "`context` is this conversation; a recap is a different one")
    # And the fourth, which is neither speech nor a finding. It is not folded
    # into `found` because the pad is written only by a plan that ran, and #80
    # measured that across eight real conversations no plan ever has — a card
    # is on the screen either way.
    check("and what is on the screen is a field of its own",
          "shown" in plan_mod.PlanRequest.model_fields, True,
          "\"the second one\" is an ordinal into a card, not into a sentence")


# Enough of them to spell every number the instruction quotes. The prompt says
# "eight turns" and "twelve commands" in words, because it is prose somebody
# reads aloud in their head, and the enforcement says 8 and 12.
_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven",
          "eight", "nine", "ten", "eleven", "twelve"]


def instruction_cases() -> None:
    print("== what the planner is told, against what is enforced ==")

    # Unwrapped, because the prompt is hard-wrapped at 79 columns and half
    # these phrases straddle a newline. A check that only holds while the
    # paragraph happens to wrap a certain way is a check that breaks on a
    # reword that changed nothing.
    text = " ".join(plan_mod.INSTRUCTION.split())

    # The prompt is the whole behaviour change in #62 — the engine already ran
    # tools in parallel, and one sentence telling the planner not to was all
    # that made it serial. So the sentence that replaced it is worth pinning.
    check("it asks for every ready step in one turn",
          "Call *all* the ready steps in the same turn" in text, True)
    check("and says what ready means, so there is something to read it off",
          "which earlier step you need" in text, True,
          "asked at planning time or item 2 has nothing to work from")
    check("and still says to wait for one that is not",
          "Guessing at the input to save a turn is worse" in text, True)

    # Every number the prose quotes, against the constant that enforces it. A
    # prompt that promises more than the code allows is worse than a smaller
    # promise: the model spends the turn finding out.
    check("the turns it promises are the turns it gets",
          f"You have {_WORDS[_plan_run.STOP_AT]} turns" in text, True,
          f"STOP_AT {_plan_run.STOP_AT}")
    check("and the width it promises is the width that runs",
          f"{_WORDS[_plan_run.WIDE].capitalize()} tools in one turn" in text,
          True, f"WIDE {_plan_run.WIDE}")
    check("and it says what a wide turn costs",
          "one turn between them" in text, True,
          "the same sentence `_plan_run.LEFT` repeats when it bites")

    # The gws share, in the one place the model can act on it: it is deciding
    # whether to split a Workspace question across two steps, and the answer
    # depends on arithmetic it cannot do without being told the numbers.
    share = (f"{_WORDS[_gws_run.STOP_AT]} commands",
             f"two running together get {_WORDS[_gws_run.STOP_AT // 2]} each",
             f"three get {_WORDS[_gws_run.STOP_AT // 3]}")
    check("the Workspace share is spelled out the way `fair` computes it",
          [s for s in share if s not in text], [],
          f"STOP_AT {_gws_run.STOP_AT} over one, two and three steps")

    # And the two rules that are prompt-only on purpose. Nothing enforces
    # either: `browser._Session` is per-run, so two parallel `browse` calls
    # launch two Chromium instances on a Pi 5, and a lock plus a queue buys
    # nothing until somebody watches the model ignore the rule.
    check("one browse at a time is asked for, not enforced",
          "One `browse` at a time" in text, True)
    check("and so is running a write on its own",
          "A step that writes runs on its own" in text, True,
          "two writes in one turn can land in either order")

    # The tool the model reports with, whose signature moved from a cursor to
    # two lists. A prompt still asking for a cursor is a prompt asking for an
    # argument the tool does not take.
    args = inspect.signature(_plan_run.show_plan).parameters
    check("show_plan takes the two lists the prompt names",
          list(args), ["steps", "running", "done"])
    check("and the prompt names them",
          "`running` set to every step going out this turn" in text, True)
    check("and no longer mentions a cursor", "current" in text, False,
          "there is nothing left that would take one")

    # The scratchpad, which only exists if the planner reaches for it. The
    # code cannot make it write a finding; the prompt is the whole of #63 in
    # the same way the fan-out sentence was the whole of #62.
    # Both halves of the phrase, because `show_plan` two paragraphs up says
    # "costs nothing" as well and matching that would let this pass while the
    # pad is being sold to the planner as something it has to pay for.
    check("the pad is named, and named as not a step",
          "`note_finding` keeps one fact" in text
          and "**It is not a step**" in text, True)
    # `turn()` charges every model round trip, note-only ones included, so the
    # prompt has to say which half is free. It used to claim both.
    check("and the prompt no longer claims it is free of a turn",
          "not a turn" in text, False)
    check("it says a note-only turn is still a turn",
          "a turn spent on nothing but `note_finding` is still one of the "
          "eight" in text, True)
    check("the line cap the prompt quotes is the one that cuts",
          f"past {_WORDS[_plan_run.PAD_LINE // 100]} hundred characters "
          f"is cut off" in text, True, f"PAD_LINE {_plan_run.PAD_LINE}")
    check("and it says where a finding comes back",
          "arrive in `found`" in text, True,
          "the field it is told to read at the top is the field this fills")
    check("which is a field the request has",
          "found" in plan_mod.PlanRequest.model_fields, True)
    check("and the prompt says to read it before spending a turn",
          "read it before you run anything" in text, True)

    # The other thing that outlives a conversation, and the one the planner
    # has to be told is old. `found` is this person's own findings and worth
    # trusting; `earlier` is a note about a conversation that ended, and a
    # planner that answers out of it reports last week as today.
    check("the note from before the gap is named too",
          "`earlier` is your own note about the conversation before this one"
          in text, True)
    check("which is a field the request has as well",
          "earlier" in plan_mod.PlanRequest.model_fields, True)
    check("and the prompt says it is not a fact to answer out of",
          "`context`, `earlier` and `shown` say what to look for" in text
          and "None of them is a source of facts." in text, True,
          "the rule used to name `context` alone, and there was only one")

    # The screen, in the two places the model can act on it: it has to know the
    # field exists, and it has to know the numbers in it are the card's.
    on_screen = "`shown` is what is on the screen in front of them right now"
    check("the prompt says what is on the screen is on the screen now",
          on_screen in text, True,
          "not a transcript and not a conversation that ended")
    check("and that the numbers in it are the ones the card shows",
          "numbered as the card numbers them" in text, True,
          "an ordinal it invented is worse than none")
    check("and that a name in it is a thing to look up, not an answer",
          "a name is something to look up rather than an answer about it"
          in text, True)


def _card_deadline(app: str, name: str) -> int | None:
    """The `deadline` on one `LATE_TOOLS` entry in `app.js`, read as a number.

    Read rather than substring-matched. `"deadline: 192000" in app` is true
    whichever card carries it, so swapping the two entries leaves both
    assertions passing while the gws card sits at 1.875x its tool and the plan
    card at 0.768x — the inversion the comment above `LATE_TOOLS` says cannot
    happen. Keyed on the tool name so each card is checked against its own
    timeout.

    `None` when nothing matches, which fails the case rather than skipping it:
    a reformatted `LATE_TOOLS` should be noticed, not silently unchecked.
    """
    # `[ \t]*` and not `\s*`: `\s` matches a newline, so under `re.M` the
    # indent could start on one line and the key be found on the next. `.*?`
    # so a line ever carrying two `deadline:` keys yields the first.
    m = re.search(rf"^[ \t]*{name}:.*?deadline:\s*(\d+)", app, re.M)
    return int(m.group(1)) if m else None


def frame_cases() -> None:
    print("== what the card is told ==")

    check("the four ways a call ends with no answer",
          sorted(set(plan_mod._NOT_AN_ANSWER.values())),
          ["empty", "failed", "slow", "unset"],
          "five messages, four names — both timeouts draw the same")

    # The table moved out of `cards.js` into `i18n.js` when the settings sheet
    # got a language of its own; the card labels and the sheet's own labels are
    # one table now, so this reads the file that holds it.
    strings = Path(__file__).resolve().parent.parent / (
        "src/ai_terminal/live/static/js/i18n.js")
    text = strings.read_text()
    for kind in sorted(set(plan_mod._NOT_AN_ANSWER.values())):
        check(f"i18n.js can say `{kind}` in both languages",
              text.count(f"plan_{kind}:"), 2,
              "one label per language block")

    check("the timeout chain still holds",
          plan_mod.TIMEOUT > __import__(
              "ai_terminal.live.tools.gws", fromlist=["gws"]).TIMEOUT, True,
          f"plan {plan_mod.TIMEOUT}s > gws {plan_mod.gws_mod.TIMEOUT}s")

    # Pairwise was all this ever checked, and there are five numbers. #65
    # raised `gws.TIMEOUT` from 100 to 160 and every one of the pairs below
    # still held at each intermediate state, so nothing said that the card had
    # been left waiting less time than the tool it draws.
    gws_timeout = plan_mod.gws_mod.TIMEOUT
    chain = [_gws_cli.TIMEOUT, _agenda.MAX_SECONDS + _gws_cli.TIMEOUT,
             gws_timeout, plan_mod.TIMEOUT]
    check("and each link of it is longer than the one inside it",
          chain, sorted(set(chain)),
          "_gws_cli < _agenda's worst case < gws < plan")

    # Each card's own number, read off its own entry by `_card_deadline`. A
    # substring test passes on whichever card happens to carry the figure, so
    # the two entries could be swapped with both cases still green.
    app = (strings.parent / "app.js").read_text()
    check("and the card waits longer than the tool does",
          _card_deadline(app, "plan"), round(plan_mod.TIMEOUT * 1.2 * 1000),
          f"{plan_mod.TIMEOUT * 1.2:.0f}s against plan.TIMEOUT "
          f"{plan_mod.TIMEOUT}s")
    # The one the suite had no case for at all. `app.js` says both cards sit
    # 1.2x above their tool, and only the plan card's number was written down
    # anywhere — so raising `gws.TIMEOUT` past 120 s left the gws card giving
    # up on a call still running, which is the inversion that comment says
    # cannot happen, and the whole suite stayed green.
    check("and so does the gws one, which nothing used to check",
          _card_deadline(app, "gws"), round(gws_timeout * 1.2 * 1000),
          f"{gws_timeout * 1.2:.0f}s against gws.TIMEOUT {gws_timeout}s")

    # The deadline is only worth having if the two places that open a budget
    # hand it one, and each has to hand it *its own* timeout — the one it also
    # wraps the run in. A budget opened with no seconds is the old behaviour
    # back, silently: the count fires, the sub-agent is told to write, and the
    # clock takes the turn away from it.
    src = Path(__file__).resolve().parent.parent / "src/ai_terminal/live/tools"
    check("gws gives the budget its own clock",
          "_gws_run.budget(seconds=TIMEOUT)" in (src / "gws.py").read_text(),
          True, f"gws.TIMEOUT {plan_mod.gws_mod.TIMEOUT}s")
    check("and plan gives it plan's, because plan opens the outer one",
          "_gws_run.budget(seconds=TIMEOUT)" in (src / "plan.py").read_text(),
          True, f"plan.TIMEOUT {plan_mod.TIMEOUT}s")
    check("with room left in either for an answer to be written",
          [t > _gws_run.RESERVE * 2
           for t in (plan_mod.gws_mod.TIMEOUT, plan_mod.TIMEOUT)],
          [True, True], f"RESERVE {_gws_run.RESERVE}s")
    # Room for the question the number was sized on, and not merely room for
    # some answer. Against `_gws_run.RESERVE` rather than against 20, so that
    # #56's number and this one cannot drift apart quietly.
    #
    # Deliberately stricter than the rule it stands in for. `spend` refuses a
    # call when `deadline - now < RESERVE`, so what the wall really has to
    # clear is the elapsed time at which the *last chargeable call* is asked
    # for, plus the reserve. `SLOWEST_MAILBOX` is a whole run, answer-writing
    # turn included — the very turn the reserve holds room for — so adding the
    # reserve to it counts that turn twice, by about one model turn (5.9 s
    # mean, the figure behind `_gws_run.RESERVE`). The tail run's per-call
    # trace was not retained, so its last-call time is not a number anyone has
    # today; bounding it by the total is the only figure there is, and
    # bounding it high is the safe direction. Read this as an upper bound with
    # one model turn of slack in it, not as the rule `spend` applies.
    #
    # The tighter form — `SLOWEST_LAST_CALL + RESERVE` — was declined on #67,
    # and not on the strength of that missing number: it goes quiet at
    # `RESERVE = 60.0` on exactly the configuration the constant exists to
    # guard, which was measured either way.
    check("and room for the slowest mailbox question measured, plus that "
          "again as an upper bound",
          gws_timeout >= SLOWEST_MAILBOX + _gws_run.RESERVE, True,
          f"gws {gws_timeout}s vs {SLOWEST_MAILBOX}s + {_gws_run.RESERVE}s, "
          "which is one model turn stricter than spend()")


# -------------------------------------------------- the plan that ran too long

class FakeRunner:
    """A `Runner` that replays a fixed list of events, then closes."""

    events: list = []

    def __init__(self, **_kwargs):
        pass

    def run_async(self, **_kwargs):
        async def stream():
            for event in FakeRunner.events:
                yield event
        return stream()

    async def close(self) -> None:
        pass


def content(*parts):
    return type("C", (), {"role": "model", "parts": list(parts)})()


def event(*parts, error=None):
    return type("E", (), {"error_message": error,
                          "content": content(*parts) if parts else None})()


class RunContext(FakeContext):
    """A `ToolContext` with the invocation fields `_run` reads."""

    def __init__(self, state: dict | None = None):
        super().__init__(state=state)
        sess = type("S", (), {"id": "sub", "events": [],
                              "state": {} if state is None else state})()
        self.session = sess
        self._invocation_context = type(
            "I", (), {"live_request_queue": None, "app_name": "checks",
                      "user_id": "u", "run_config": None, "session": sess})()


async def collect_cases() -> None:
    print("== the turns a timeout has to report ==")

    runner, FakeRunner.events = plan_mod.Runner, [
        event(part(text="Looking up the hours."), part(text=" Two of them.")),
        event(error="one bad event, recovered from"),
        event(part(text="   \n  ")),
        event(part(text="Kaikaya opens at 17:00.")),
    ]
    plan_mod.Runner = FakeRunner
    try:
        seen: list[str] = []
        answer = await plan_mod._run({"task": "dinner"}, RunContext(), seen)
        check("the answer is still the last turn's text", answer,
              "Kaikaya opens at 17:00.")
        check("one entry per turn that said something", len(seen), 2, seen)
        check("a turn's parts are joined", seen[0],
              "Looking up the hours. Two of them.")
        check("an event with no content is not a turn",
              "one bad event, recovered from" in seen, False)
        check("nor is a turn that is only whitespace",
              [s for s in seen if not s.strip()], [],
              "it would report a blank as the partial answer")

        FakeRunner.events = [
            event(part(text="I should check the hours next.", thought=True)),
            event(part(text="Kaikaya opens at 17:00.")),
        ]
        thought: list[str] = []
        await plan_mod._run({"task": "dinner"}, RunContext(), thought)
        check("a thought is not a turn either", thought,
              ["Kaikaya opens at 17:00."],
              "the planner thinks in the open and it is read out loud")

        check("and the collector is optional",
              await plan_mod._run({"task": "dinner"}, RunContext()),
              "Kaikaya opens at 17:00.", "no list, no crash")
    finally:
        plan_mod.Runner = runner


async def session_state_cases() -> None:
    print("== the session state a planner sub-run inherits ==")

    from ai_terminal.live.tools import maps as maps_mod

    class SpyRunner:
        last_session = None

        def __init__(self, app_name=None, agent=None, session_service=None):
            self.session_service = session_service
            self.app_name = app_name

        def run_async(self, user_id=None, session_id=None, **kwargs):
            async def stream():
                SpyRunner.last_session = await self.session_service.get_session(
                    app_name=self.app_name, user_id=user_id, session_id=session_id)
                yield event(part(text="done"))
            return stream()

        async def close(self) -> None:
            pass

    runner = plan_mod.Runner
    plan_mod.Runner = SpyRunner
    was_env = os.environ.get("HOME_LOCATION")
    os.environ["HOME_LOCATION"] = "ENV-FALLBACK-VISIBLE"
    try:
        # A live session carrying the browser's pin and arbitrary ambient state.
        orig_state = {"home_location": "3 Chome-1 Shibakoen",
                      "ambient_key": "ambient_val"}
        ctx = RunContext(state=orig_state)
        await plan_mod._run({"task": "dinner", "lang": "ja"}, ctx)

        sub_state = SpyRunner.last_session.state
        check("invocation session state carries into planner sub-run",
              sub_state.get("home_location"), "3 Chome-1 Shibakoen")
        check("other session state keys survive",
              sub_state.get("ambient_key"), "ambient_val")
        check("panel_lang is set from task lang",
              sub_state.get("panel_lang"), "ja")

        # maps.here reads the sub-session's state, returning the pin rather
        # than falling back to HOME_LOCATION from the environment.
        sub_ctx = FakeContext("sub", state=sub_state)
        check("here() returns browser pin rather than HOME_LOCATION fallback",
              maps_mod.here(sub_ctx), "3 Chome-1 Shibakoen")

        # The sub-run gets a copy rather than a reference, so writes inside it
        # cannot mutate the live session's state.
        sub_state["leaked_key"] = "mutation"
        check("sub-run session state is a copy, not a shared reference",
              "leaked_key" in orig_state, False)

        # An invocation without session state still creates a sub-run cleanly.
        bare_ctx = RunContext()
        await plan_mod._run({"task": "dinner"}, bare_ctx)
        check("sub-run without session state still runs cleanly",
              SpyRunner.last_session is not None, True)
    finally:
        plan_mod.Runner = runner
        if was_env is not None:
            os.environ["HOME_LOCATION"] = was_env
        else:
            os.environ.pop("HOME_LOCATION", None)


async def timeout_cases() -> None:
    print("== a plan stopped before it finished ==")

    agent, run, limit = plan_mod._agent, plan_mod._run, plan_mod.TIMEOUT
    # `_ask` answers UNCONFIGURED without one, and never reaches the timeout.
    plan_mod._agent = object()
    plan_mod.TIMEOUT = 0.05
    try:
        async def slow(args, tool_context, seen=None):
            if seen is not None:
                seen.append("Looking up the hours.")
                seen.append("Kaikaya opens at 17:00.")
            await asyncio.sleep(5)
            return "an answer nobody waits for"

        plan_mod._run = slow
        got = await plan_mod._ask({"task": "dinner"}, FakeContext())
        check("it says it was stopped",
              got.startswith(plan_mod.TOO_SLOW_PARTIAL), True, got)
        check("and carries what the planner had got to",
              got.endswith("Kaikaya opens at 17:00."), True, got)
        check("the newest turn only", "Looking up the hours" in got, False,
              "the earlier ones narrate; the newest carries settled facts")
        check("the card still draws it as a timeout", plan_mod._kind(got),
              "slow", "an exact lookup reads this as an ordinary answer")

        async def mute(args, tool_context, seen=None):
            await asyncio.sleep(5)
            return "an answer nobody waits for"

        plan_mod._run = mute
        bare = await plan_mod._ask({"task": "dinner"}, FakeContext())
        check("with nothing to report, the message is unchanged", bare,
              plan_mod.TOO_SLOW)
        check("and that is a timeout too", plan_mod._kind(bare), "slow")

        async def quick(args, tool_context, seen=None):
            if seen is not None:
                seen.append("a turn that landed in time")
            return "Kaikaya opens at 17:00."

        plan_mod._run = quick
        check("a plan that finishes is untouched by any of it",
              await plan_mod._ask({"task": "dinner"}, FakeContext()),
              "Kaikaya opens at 17:00.")
        check("and is not a not-an-answer",
              plan_mod._kind("Kaikaya opens at 17:00."), None)

        # The path the pad is worth the most on, and the reason `plan` owns the
        # list and writes it back outside `_ask`: the timeout cancels the run,
        # so anything the pad were reached through the cancelled frame would go
        # with it. Real `_ask`, real `asyncio.timeout`, real cancellation.
        async def noting(args, tool_context, seen=None):
            _plan_run.note_finding("Kaikaya opens at 17:00.")
            await asyncio.sleep(5)
            return "an answer nobody waits for"

        plan_mod._run = noting
        frames: list[dict] = []

        async def to_screen(frame):
            frames.append(frame)

        ctx = FakeContext("live-slow", "call-slow")
        screen.register("live-slow", to_screen)
        plan_mod.open_session("live-slow")
        try:
            late = await plan_mod.plan("dinner", ctx)
        finally:
            plan_mod.close_session("live-slow")
            screen.unregister("live-slow")
        check("a plan that ran out of time still answers as one",
              plan_mod._kind(late["answer"]), "slow")
        check("and what it found before the clock ran out is kept",
              _plan_run.held(ctx.state), ["Kaikaya opens at 17:00."],
              "cancelling `_ask` must not take the findings with it")

        # The second half of #63. The pad paying off on the *next* question is
        # not the whole of it: the turn that ran out of time has to report the
        # facts it settled, and `_ask` only ever saw the narration.
        check("and the person is told them on this turn, not the next",
              late["answer"].endswith("- Kaikaya opens at 17:00."), True,
              late["answer"])
        check("under the wording that says part of an answer follows",
              late["answer"].startswith(plan_mod.TOO_SLOW_PARTIAL), True,
              "a plan with findings is never TOO_SLOW's 'there is no answer'")
        drawn = [f for f in frames if f.get("type") == "plan"]
        check("and the card gets them in `partial`",
              [f.get("partial") for f in drawn],
              ["- Kaikaya opens at 17:00."],
              "the seam is cut after the findings are in")

        async def both(args, tool_context, seen=None):
            _plan_run.note_finding("Kaikaya opens at 17:00.")
            if seen is not None:
                seen.append("Now checking whether they take a booking.")
            await asyncio.sleep(5)
            return "an answer nobody waits for"

        plan_mod._run = both
        ctx = FakeContext("live-both", "call-both")
        plan_mod.open_session("live-both")
        try:
            mixed = await plan_mod.plan("dinner", ctx)
        finally:
            plan_mod.close_session("live-both")
        check("a narrated line and a finding both reach the person",
              "Now checking whether they take a booking."
              in mixed["answer"]
              and mixed["answer"].endswith("- Kaikaya opens at 17:00."), True,
              mixed["answer"])
        check("with one copy of the stopped-early wording, not two",
              mixed["answer"].count(plan_mod.TOO_SLOW_PARTIAL), 1)

        async def silent(args, tool_context, seen=None):
            await asyncio.sleep(5)
            return "an answer nobody waits for"

        plan_mod._run = silent
        ctx = FakeContext("live-none", "call-none")
        plan_mod.open_session("live-none")
        try:
            empty = await plan_mod.plan("dinner", ctx)
        finally:
            plan_mod.close_session("live-none")
        check("a plan that found nothing says there is no answer",
              empty["answer"], plan_mod.TOO_SLOW,
              "nothing to append, so nothing is rewritten")
    finally:
        plan_mod._agent, plan_mod._run, plan_mod.TIMEOUT = agent, run, limit


async def partial_frame_cases() -> None:
    print("== the seam the card is sent ==")

    # `result` carries the whole string, instructions and all, because that is
    # what the model is given. The card needs the answer without them, and only
    # this side knows where TOO_SLOW_PARTIAL stops — so the offset is applied
    # here and sent as its own field. `cards.js` splitting on the constant would
    # mean a copy of this English living in the client.
    frames: list[dict] = []

    async def to_screen(frame):
        frames.append(frame)

    ask = plan_mod._ask
    screen.register("live-1", to_screen)
    plan_mod.open_session("live-1")
    try:
        async def answered(text):
            frames.clear()

            async def stub(args, tool_context):
                return text

            plan_mod._ask = stub
            await plan_mod.plan("dinner", FakeContext("live-1", "call-plan"))
            return frames[-1]

        part = await answered(
            f"{plan_mod.TOO_SLOW_PARTIAL}\n\nKaikaya opens at 17:00.")
        check("a stopped plan with something to show is still a timeout",
              part.get("error"), "slow")
        check("and hands over the answer on its own",
              part.get("partial"), "Kaikaya opens at 17:00.")
        check("without the prose aimed at the model",
              plan_mod.TOO_SLOW_PARTIAL[:20] in part["partial"], False)
        check("while `result` keeps both, for the model to read",
              part["result"].startswith(plan_mod.TOO_SLOW_PARTIAL), True)

        bare = await answered(plan_mod.TOO_SLOW)
        check("a stopped plan with nothing to show sends no seam",
              "partial" in bare, False,
              "an empty one would draw a blank line under the card")
        check("and is the same kind of timeout", bare.get("error"), "slow")

        # The three whose `result` is wholly an instruction. A `partial` on any
        # of them puts "say you could not do it" on the kitchen screen.
        for name in ("FAILED", "NO_ANSWER", "UNCONFIGURED"):
            frame = await answered(getattr(plan_mod, name))
            check(f"{name} sends no seam either", "partial" in frame, False)

        whole = await answered("Kaikaya opens at 17:00.")
        check("and a real answer is neither",
              [whole.get("error"), "partial" in whole], [None, False])
    finally:
        plan_mod._ask = ask
        plan_mod.close_session("live-1")
        screen.unregister("live-1")


async def two_pages_cases() -> None:
    """A plan started on one page, and a different page closing under it.

    The kiosk and a tab on the Mac serving it share a session id (#19). With
    one `_RUNNING` entry per id, the tab's `open_session` took the entry over
    and the kiosk's plan went into it, so the tab closing cancelled a plan
    running for a terminal that is still up and still waiting — a whole plan,
    `gws` steps and all, thrown away with nothing on either screen to say why.
    """
    print("\n== a plan and a second page on the same profile ==")

    started, release = asyncio.Event(), asyncio.Event()

    async def stub(args, tool_context):
        started.set()
        await release.wait()
        return "Kaikaya opens at 17:00."

    async def to_screen(frame):
        pass

    ask = plan_mod._ask
    plan_mod._ask = stub
    screen.register("live-1", to_screen)
    kiosk = plan_mod.open_session("live-1", "c-kiosk")
    laptop = plan_mod.open_session("live-1", "c-laptop")
    try:
        # Inside the block, because that is where `main.py` creates the task
        # this stands in for: `create_task` snapshots the context, so the
        # connection travels into the call and on into everything it starts.
        with conn.speaking("c-kiosk"):
            call = asyncio.create_task(
                plan_mod.plan("dinner", FakeContext("live-1", "call-plan")))
        await started.wait()
        await asyncio.sleep(0)
        check("a plan started on the kiosk is tracked under the kiosk",
              [len(kiosk), len(laptop)], [1, 0],
              "under the newest, the wrong socket closing cancels it")
        plan_mod.close_session("live-1", laptop)
        for _ in range(3):
            await asyncio.sleep(0)
        check("so the other page closing does not take the plan with it",
              call.done(), False,
              "a cancelled plan says nothing on either screen")
    finally:
        release.set()
        plan_mod._ask = ask
        plan_mod.close_session("live-1", kiosk)
        screen.unregister("live-1", to_screen)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await call


class Scripted(BaseLlm):
    """A model that answers with one line and never calls a tool.

    Enough to make a real `Runner` do one round trip, which is all the turn
    counter needs to see.
    """

    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="Kaikaya opens at 17:00.")]))


async def one_real_turn(agent) -> int:
    """Turns counted while ADK runs `agent` for one round trip."""
    agent.model = Scripted(model="scripted")
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="check", user_id="u")
    runner = Runner(app_name="check", agent=agent, session_service=sessions)
    message = types.Content(role="user", parts=[types.Part.from_text(
        text='{"task":"dinner","context":"","found":"","lang":"en"}')])
    # Quiet: ADK warns about an experimental declaration feature and logs a
    # missing token count for the scripted model, and both land mid-line in
    # this script's output.
    quiet = logging.getLogger("google_adk.google.adk.telemetry._metrics")
    was, quiet.disabled = quiet.disabled, True
    with warnings.catch_warnings(), _plan_run.budget() as tally:
        warnings.simplefilter("ignore", UserWarning)
        try:
            async for _event in runner.run_async(
                    user_id="u", session_id=session.id, new_message=message):
                pass
        finally:
            quiet.disabled = was
        return tally.turns


async def bindings_cases() -> None:
    print("== the bindings the work runs under ==")

    # `plan` enters them around `create_task`, which snapshots the context, so
    # they are the task's and not the caller's. Nothing else in the repo calls
    # `plan` itself: without this, deleting `_gws_run.budget()` from that line
    # leaves every check green and quadruples the CLI bill of a four-`gws`
    # plan.
    seen: dict = {}

    async def spy(args, tool_context):
        seen["plan budget"] = _plan_run.nested()
        # Before entering one of its own, which is what `gws` does a call
        # later — that entry finds a tally either way and would say nothing.
        tally = _gws_run._BUDGET.get()
        seen["gws budget"] = tally is not None
        seen["gws deadline"] = _gws_run._DEADLINE.get() is not None
        seen["card"] = screen.live_card()
        seen["context"] = args["context"]
        seen["found"] = args["found"]
        seen["earlier"] = args["earlier"]
        seen["shown"] = args["shown"]
        # From two levels down, the way the tool is really reached.
        seen["kept"] = _plan_run.note_finding("the shop shuts at six")
        return "an answer"

    frames: list[dict] = []

    async def to_screen(frame):
        frames.append(frame)

    ask = plan_mod._ask
    plan_mod._ask = spy
    screen.register("live-1", to_screen)
    plan_mod.open_session("live-1")
    try:
        ctx = FakeContext("live-1", "call-plan", state={
            _plan_run.PAD_KEY: ["shortlist: A, B, C"],
            recap.RECAP_KEY: "they were choosing a hotel"})
        ctx.session.events = [Event(heard="somewhere for dinner")] + drew(
            "maps_search",
            {"query": "izakaya near Ebisu",
             "places": [{"title": "Bar Trench"}, {"title": "Buri"}]},
            ident="c-maps", args={"query": "izakaya near Ebisu"})
        got = await plan_mod.plan("dinner", ctx)
        check("the answer comes back", got, {"answer": "an answer"})
        check("the work runs under a step budget", seen["plan budget"], True)
        check("with a deadline on it, and not just a count",
              seen["gws deadline"], True,
              "plan's clock, because plan opened the outer entry")
        check("and under one shared gws budget", seen["gws budget"], True,
              "gws enters its own a call later, and it is reentrant")
        check("and knows which card is the plan's",
              seen["card"], ("live-1", "call-plan"))
        check("the conversation travelled with the task", seen["context"],
              "person: somewhere for dinner\n"
              "assistant: [called maps_search: query=izakaya near Ebisu]")
        # The other half of the same events, and the one nothing recorded. The
        # call is in `context` above and the answer is only here: without this
        # binding "book the second one" reaches the planner naming nothing.
        check("and so did what is on the screen beside it", seen["shown"],
              'maps_search "izakaya near Ebisu": 1. Bar Trench; 2. Buri')
        # The pad, both directions, through the one call that wires them. The
        # planner's own session is thrown away when the run ends, so if `plan`
        # does not carry the findings across nothing else will.
        check("what an earlier plan found arrived with the task",
              seen["found"], "- shortlist: A, B, C")
        # The third thing on that state, and the only one of the three that
        # survives a rollover by itself. Nothing below `plan` can reach it:
        # `_run` mints a session of its own, and this is the last frame that
        # still has the live one.
        check("and so did what the conversation before this one was about",
              seen["earlier"], "they were choosing a hotel")
        check("without being folded into what was said in this one",
              "hotel" in seen["context"], False,
              "`context` is the events; a recap is a different conversation")
        check("nor is a card folded into either of them",
              ["Bar Trench" in seen["context"],
               "Bar Trench" in seen["earlier"]], [False, False],
              "three kinds of thing, three fields")
        check("and a finding written mid-run had somewhere to go",
              seen["kept"], {"kept": True, "held": 1, "dropped": 0})
        check("which is on the conversation afterwards",
              _plan_run.held(ctx.state),
              ["shortlist: A, B, C", "the shop shuts at six"],
              "added to the earlier plan's, not written over them")

        check("neither budget outlives the call",
              [_plan_run.nested(), _gws_run._BUDGET.get()], [False, None])
        check("nor does the pad", _plan_run._PAD.get(), None)
        check("and the card frame went to the screen",
              [(f["type"], f["id"], f.get("error")) for f in frames],
              [("plan", "call-plan", None)])

        # And the other path that loses the answer. `_ask` returns its timeout
        # message rather than raising, so it is this one — not the timeout —
        # that says the write has to be past the `try` and not beside
        # `text = await work`.
        async def blows_up(args, tool_context):
            _plan_run.note_finding("the shop shuts at six")
            raise RuntimeError("no")

        plan_mod._ask = blows_up
        broke = FakeContext("live-1", "call-broke")
        # Quiet, because `plan` logs the exception it swallows with a
        # traceback, and a traceback in this script's output is how a mutation
        # run tells that something died.
        quiet = logging.getLogger(plan_mod.__name__)
        was, quiet.disabled = quiet.disabled, True
        try:
            got = await plan_mod.plan("a different dinner", broke)
        finally:
            quiet.disabled = was
        check("a plan that raised says so", got, {"answer": plan_mod.FAILED})
        check("and keeps what it found before it did",
              _plan_run.held(broke.state), ["the shop shuts at six"],
              "the write is past the `try`, not inside it")

        # The last binding, and the one nothing else can see. The budget is
        # in turns, and a turn is only ever counted by ADK calling this — leave
        # it unwired and `turns` stays at 0, STOP_AT never fires, and the only
        # thing left holding a loop down is `CEILING`, forty-eight calls later.
        agent = plan_mod.build_plan_agent()
        check("the planner counts a turn when ADK starts one",
              agent.before_model_callback, _plan_run.on_model,
              "before_model_callback, once per model round trip")

        # By keyword, which is the only shape ADK ever uses: the type alias
        # declares the two parameters positionally and
        # `flows/llm_flows/base_llm_flow.py:281-286` passes them by name, with
        # a comment saying it always has. Driven positionally — which is what
        # this did — a callback whose parameters are named anything else looks
        # wired, raises `TypeError` inside the flow, and `plan` turns that into
        # `FAILED` for every question anybody asks.
        with _plan_run.budget() as tally:
            wrong_name = ""
            try:
                agent.before_model_callback(callback_context=None,
                                            llm_request=None)
                agent.before_model_callback(callback_context=None,
                                            llm_request=None)
            except TypeError as exc:
                # Caught and reported rather than raised, so a rename reads as
                # one named failure instead of taking the script out here.
                wrong_name = str(exc)
            check("the callback takes ADK's two keywords", wrong_name, "",
                  "an underscore on either name is a TypeError in the flow")
            check("and driving it twice is two turns", tally.turns, 2)
        check("it returns nothing, so the model call still happens",
              _plan_run.on_model(callback_context=None, llm_request=None),
              None, "a plan out of turns still has to write an answer")

        # And once through a real `Runner`, because the case above still knows
        # the calling convention and a case that knows it cannot discover that
        # it changed. Nothing else in the suite runs the planner's own agent:
        # every other case stubs `_ask`.
        turns = await one_real_turn(agent)
        check("and ADK itself counts one, driving it the way ADK does",
              turns, 1, "a scripted model, one round trip, no tool calls")
    finally:
        plan_mod._ask = ask
        plan_mod.close_session("live-1")
        screen.unregister("live-1")


async def nested_browse_cases() -> None:
    print("== which card a nested browse draws on ==")

    from ai_terminal.live.tools import browse as browse_mod

    frames: list[dict] = []

    async def to_screen(frame):
        frames.append(frame)

    async def fake_run(task, lang="", context="", sink=None):
        await sink({"type": "browse", "phase": "step", "url": "https://x/"})
        await sink({"type": "browse", "phase": "done"})
        return {"answer": "42", "url": "https://x/", "seconds": 1.0}

    real = browse_mod.browser.run
    browse_mod.browser.run = fake_run
    screen.register("live-1", to_screen)
    browse_mod.open_session("live-1")
    try:
        # A browse the planner started, and a second the live agent started
        # while the plan was still running — `plan` is NON_BLOCKING, so both
        # are in flight and both push down the one channel.
        with screen.live("live-1", "call-plan"):
            await browse_mod.browse("timetable", FakeContext("sub", "call-a"))
        await browse_mod.browse("hours", FakeContext("live-1", "call-b"))

        check("every frame reached the live screen", len(frames), 4)
        check("and each carries the call it belongs to",
              [f.get("id") for f in frames],
              ["call-a", "call-a", "call-b", "call-b"],
              "without the id one card alternates the two jobs' screenshots")
        check("not the plan's own card",
              "call-plan" in [f.get("id") for f in frames], False,
              "that one is the plan card, and browse would overwrite it")
        check("the frames themselves are untouched",
              [f["phase"] for f in frames], ["step", "done"] * 2)
    finally:
        browse_mod.browser.run = real
        browse_mod.close_session("live-1")
        screen.unregister("live-1")


def pad_cases() -> None:
    print("== the scratchpad ==")

    # Outside a plan first, because the live agent holds this same tool and
    # calling it there is not an error — it is a planner that has nothing to
    # carry, and it has to say so rather than throw.
    check("outside a plan there is nowhere to write, and it says so",
          _plan_run.note_finding("the shop shuts at six"),
          {"kept": False, "note": _plan_run.NO_PAD})
    check("and with nothing to write either, it is the finding that is named",
          _plan_run.note_finding("")["note"], _plan_run.NOTHING_TO_KEEP,
          "the empty test runs first, so an empty call is answered the same "
          "way wherever it came from")

    found: list[str] = []
    with _plan_run.pad(found):
        check("inside one, a finding is kept",
              _plan_run.note_finding("free nights: 14, 15 and 21 Nov"),
              {"kept": True, "held": 1, "dropped": 0})
        check("and the caller owns the list, not the tool",
              found, ["free nights: 14, 15 and 21 Nov"],
              "`plan` reads it after `_ask` has been cancelled")

        check("an empty finding is refused before the pad is touched",
              _plan_run.note_finding("   "),
              {"kept": False, "note": _plan_run.NOTHING_TO_KEEP})
        check("and refusing it kept the pad as it was", len(found), 1)
        check("None is the same nothing, not the string 'None'",
              _plan_run.note_finding(None)["kept"], False)

        check("a finding is flattened onto one line",
              _plan_run.note_finding("two\n\n  words")
              and found[-1], "two words",
              "it is read back as a bullet, and a newline would end it early")
        long = _plan_run.note_finding("x" * (_plan_run.PAD_LINE + 50))
        check("and cut at the line cap", len(found[-1]), _plan_run.PAD_LINE,
              f"PAD_LINE {_plan_run.PAD_LINE}, hard here where `_recent`'s is "
              f"soft: a finding is meant to be a sentence")
        check("which the model is not told is an error", long["kept"], True)

    # The count cap, from inside, so a planner writing in a loop cannot grow
    # the list without bound before `keep` ever sees it.
    many: list[str] = []
    with _plan_run.pad(many):
        for i in range(_plan_run.PAD + 3):
            last = _plan_run.note_finding(f"finding {i}")
    check("past the count cap the oldest goes", len(many), _plan_run.PAD,
          f"PAD {_plan_run.PAD}")
    check("and the newest is what is left",
          many[-1], f"finding {_plan_run.PAD + 2}")
    check("and the tool says it dropped one", last["dropped"], 1,
          "one per call once it is full, not a silent trim")

    # `_kept` again on the way out, because the pad on the session is the union
    # of every plan's, and the caps belong to that total and not to one run.
    lines = [f"finding {i}" for i in range(_plan_run.PAD + 3)]
    check("past the count cap it is the oldest that go",
          set(_plan_run._kept(lines)), set(lines[3:]),
          "as a set, because which end they come out of is the next case")
    check("the character cap keeps the newest that fit",
          len(_plan_run._kept(["y" * _plan_run.PAD_LINE] * 20)),
          _plan_run.PAD_CHARS // _plan_run.PAD_LINE,
          f"PAD_CHARS {_plan_run.PAD_CHARS}")
    check("in the order they were written, oldest first",
          _plan_run._kept(["one", "two", "three"]), ["one", "two", "three"],
          "collected newest-first so the newest survive, then turned back")
    check("blank lines are not findings", _plan_run._kept(["a", "  ", "b"]),
          ["a", "b"])
    check("and an over-long one is cut here as well as on the way in",
          len(_plan_run._kept(["z" * (_plan_run.PAD_LINE + 1)])[0]),
          _plan_run.PAD_LINE,
          "note_finding cuts what it writes; a pad off a session row was cut "
          "by whichever release wrote it")
    check("a string is not a pad of twelve single characters",
          _plan_run._kept("not a list"), [],
          "a session row written by another release of this is data, and a "
          "string is iterable")
    check("and neither is nothing at all", _plan_run._kept(None), [])

    check("the findings read as a list",
          _plan_run.as_text(["a", "b"]), "- a\n- b")
    check("and an empty pad reads as nothing, not as a bullet",
          _plan_run.as_text([]), "")

    # The literal, not `PAD_KEY`, or moving the constant moves both sides of
    # the comparison and the case says nothing. ADK reads a `app:` / `user:` /
    # `temp:` prefix off the key and routes the write by it; no prefix is the
    # session row, which is one conversation — #64.
    check("the pad is keyed to this conversation, not the whole app",
          _plan_run.PAD_KEY, "plan_pad",
          "`app:` is StorageAppState, one row for the household, and SQLite "
          "has no row lock behind it")

    state: dict = {}
    _plan_run.keep(state, ["first plan"])
    check("keep writes them there", state, {"plan_pad": ["first plan"]})
    _plan_run.keep(state, ["second plan"])
    check("a second plan adds to the pad rather than replacing it",
          _plan_run.held(state), ["first plan", "second plan"],
          "two plans can run at once in one conversation — `plan._RUNNING`")
    _plan_run.keep(state, ["first plan"])
    check("and writing the same finding twice does not double it",
          _plan_run.held(state), ["first plan", "second plan"])
    _plan_run.keep(state, [])
    check("an empty run leaves the pad alone",
          _plan_run.held(state), ["first plan", "second plan"])
    blank: dict = {}
    _plan_run.keep(blank, [])
    check("and does not write the key at all", blank, {},
          "assigning it is a state delta the framework goes and applies, so "
          "an empty plan would put a row where there was none")

    # Which threads pass a pad on, which is what stops one lasting forever.
    # `main._carry_forward` reads this off the events of the thread rolling
    # away: it wrote the pad itself, or it inherited one and added nothing.
    # Built here rather than through `build_plan_tool`, which also assigns
    # `plan._agent` — a global three other cases save and restore around
    # themselves. It is the same construction: the name is the function's.
    check("the name it looks for is the name the live agent calls",
          FunctionTool(plan_mod.plan).name, _plan_run.TOOL)
    called = type("E", (), {"content": type("C", (), {
        "parts": [part(call=_plan_run.TOOL, args={"task": "dinner"})]})()})()
    spoke = type("E", (), {"content": type("C", (), {
        "parts": [part(text="what is the time")]})()})()
    quiet = type("E", (), {"content": None})()
    check("a thread that ran a plan says so", _plan_run.ran([spoke, called]),
          True)
    check("one that only talked does not", _plan_run.ran([spoke, quiet]),
          False, "its pad is one it inherited, and is a gap older every gap")
    check("neither does a thread with nothing in it at all",
          [_plan_run.ran([]), _plan_run.ran(None)], [False, False],
          "the state-only event a rollover writes is the whole of a new one")
    check("and a different tool is not the planner",
          _plan_run.ran([type("E", (), {"content": type("C", (), {
              "parts": [part(call="weather")]})()})()]), False)
    # The answer counts as well as the question, which is what makes the read
    # tolerant rather than exact: a call and its response are two parts and
    # need not be on one event, and which of the two a row from some earlier
    # release kept is not something a rollover should have an opinion about.
    check("and the answer coming back counts the same as the call",
          _plan_run.ran([type("E", (), {"content": type("C", (), {
              "parts": [part(response=_plan_run.TOOL)]})()})()]), True)

    check("held tolerates junk under the key rather than killing the plan",
          _plan_run.held({_plan_run.PAD_KEY: "not a list"}), [],
          "a string is iterable and would otherwise come back as characters")
    check("and a key that is not there at all", _plan_run.held({}), [])

    class Hostile(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("no")

    class Unreadable(dict):
        def get(self, key, default=None):
            raise RuntimeError("no")

    # Quiet, because `keep` logs the exception it swallows with a traceback,
    # and a traceback in this script's output is how a mutation run tells that
    # something died.
    quiet = logging.getLogger(_plan_run.__name__)
    was, quiet.disabled = quiet.disabled, True
    try:
        _plan_run.keep(Hostile(), ["swallowed"])
        # Caught rather than left to fall out of the script: a `held` that
        # raises is the defect this case is about, and it should read as a
        # failure here rather than as a traceback with no name on it.
        try:
            unreadable = _plan_run.held(Unreadable())
        except Exception as err:
            unreadable = f"raised {err!r}"
    finally:
        quiet.disabled = was
    check("keep never raises: the answer is already written",
          True, True, "a pad that cannot be saved must not lose the answer")
    check("nor does held, which runs before there is an answer to lose",
          unreadable, [],
          "a state row this release cannot read starts the plan empty rather "
          "than ending it")

    # The two uncharged tools, against the ten that are. `note_finding` is a
    # step the planner is asked to take *often*, and charging it would buy a
    # planner that stops writing things down when the plan is long enough to
    # need it.
    charged = {tool.name: isinstance(tool, _plan_run.Counted)
               for tool in plan_mod._tools()}
    check("note_finding costs no step", charged.get("note_finding"), False)
    check("nor does show_plan", charged.get("show_plan"), False)
    check("and everything that goes somewhere does",
          sorted(n for n, paid in charged.items() if not paid),
          ["note_finding", "show_plan"])


async def main() -> None:
    budget_cases()
    await counted_cases()
    await gws_budget_cases()
    await gws_clock_cases()
    await gws_repeat_cases()
    await gws_false_refusal_cases()
    screen_cases()
    await show_plan_cases()
    await show_plan_voice_cases()
    readable_cases()
    recent_cases()
    shown_cases()
    request_cases()
    pad_cases()
    instruction_cases()
    await bindings_cases()
    await nested_browse_cases()
    await collect_cases()
    await session_state_cases()
    await timeout_cases()
    await partial_frame_cases()
    await two_pages_cases()
    frame_cases()
    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    asyncio.run(main())
