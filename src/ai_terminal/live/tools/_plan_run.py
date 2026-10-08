"""What one plan is allowed to spend, and what it shows while it spends it.

Its own module for the same reason :mod:`._gws_run` is one: `plan.py` has the
tool the live agent calls, and the pieces here are what the planner's own tools
run through. Splitting them also keeps the import graph flat — `gws.py` reads
the step budget to know whether it is nested, and it cannot import `plan.py`,
which imports it.

Two counters, deliberately not talking to each other. :func:`spend` here counts
**planner turns** — one per model round trip, however many tools that turn
calls. `_gws_run.spend` counts **`gws` CLI calls**, and one turn holding a `gws`
step spends one turn here and up to twelve calls there. The same argument
`_gws_run.py` makes about `_agenda.agenda`: a budget is only meaningful in the
unit it counts, and adding the two together bounds neither.

The scratchpad is here too, because it is the other half of the same argument.
Eight turns is enough to gather the pieces of a real answer and not enough to
gather them twice, so a finding that is lost is a turn that has to be spent
again. :func:`note_finding` writes one down; :func:`keep` puts them on the live
session's ADK state, which is the one store that outlives both the planner's
own session and `plan._ask`'s timeout — see #64 for why it is that and not a
store of ours.
"""

import contextlib
import contextvars
import logging
from typing import Any

from google.adk.tools import FunctionTool, ToolContext

from .. import screen
from . import _progress

logger = logging.getLogger(__name__)

# How many model turns one plan gets, and when to start saying so.
#
# Turns, and not tool calls, which is the whole of #62. ADK builds one
# `asyncio.create_task` per function call in a model response and gathers them
# (`google/adk/flows/llm_flows/functions.py`), so three lookups asked for in one
# turn cost what one costs. Charged per call, the wide plan — the fast one — was
# the one that ran out first: the number written to stop somebody standing in
# front of the terminal for a minute charged the most to the plan that kept them
# standing there least.
#
# ESTIMATED, not measured, and cheap to revise. A planner turn is around five
# seconds of model on top of whatever its tools cost, so eight turns is already
# past a minute. The three-step chain this was built for — search, look one up,
# check the weather — takes four with a revision, so five is where a plan has
# gone further than its own example and is worth telling about. A plan that
# calls one tool per turn is charged exactly what it always was.
WARN_AT = 5
STOP_AT = 8

# How many tools one turn may fan out to. Six because that is the widest any
# example in the tool list reaches, and because a wider turn is not free to
# refuse: the calls past this come back with nothing in them and the turn has
# been spent on them. Wants a real distribution of fan-out widths off the
# device before it is worth moving.
WIDE = 6

# A backstop, counted in calls, for the one thing the turn count cannot see.
# :func:`turn` is driven by `before_model_callback`; unwire that, or have ADK
# stop invoking it, and `turns` stays at 0 while a loop runs forever. This is
# what a legitimate plan of full-width turns would reach, so it cannot fire on
# one that is behaving.
CEILING = STOP_AT * WIDE

# The sentence every refusal here has in common, and how :func:`refused` tells
# one from a warning — the same device as `_gws_run.STOPPED`, spelled out again
# rather than imported, because these two budgets deliberately do not know
# about each other and one importing the other's wording is a start.
STOPPED = "Nothing was run."

# "Past", not "at", exactly as `_gws_run.LEFT` — this is attached to a result
# the tool has just handed back, so "at 8 the tool stops" reads as a
# contradiction on the eighth.
#
# The last sentence is the fan-out rule arriving at the one moment the planner
# has a reason to act on it. It is in the instruction too, several hundred words
# before the model needs it.
LEFT = (
    "{spent} of {total} plan turns used. Past {total} the tools stop running "
    "and you answer from what you have, so start writing it. Tools called "
    "together in one turn cost one turn between them, however many they are."
)

OUT_OF_STEPS = (
    f"Out of plan turns. {STOPPED} Write the answer now from what you "
    "already found, and say what you did not get to."
)

# Not a refusal of the plan, only of this call: the turn's first :data:`WIDE`
# calls have run and their results are coming back. So the advice is to read
# those and ask again, where :data:`OUT_OF_STEPS` says to stop asking.
TOO_WIDE = (
    "Too many tools in one turn — {wide} of them ran and the rest, this one "
    f"included, did not. {STOPPED} Read what those came back with, then ask "
    "for the others if you still need them."
)

# A step that raised, as the planner reads it.
#
# Deliberately without :data:`STOPPED` in it. :func:`refused` keys on that
# sentence, and this is not a refusal: the call was made, it reached whatever it
# reaches, and only the answer is missing. Telling the model nothing was run of
# a step that may have got as far as writing something is the one thing here
# worth being careful about.
#
# The exception's type and not its message. This string goes into a prompt whose
# answer is read out loud, and the text of a raise off the wire carries URLs,
# request ids and occasionally a key. The message is in the log, once, where a
# person can go and read it.
BROKE = (
    "The `{tool}` step failed and came back with nothing ({why}). Carry on "
    "with the rest of the plan, and say in your answer that this part did not "
    "come back. Worth one more attempt if the answer turns on it, and no more."
)


# The scratchpad. How many findings carry from one plan to the next, how much
# of them, and under which key on the live session's state.
#
# The caps are `_recent`'s, scaled down, and for the same reason: this is
# injected back into the next planner's prompt, so a pad that grows is the
# context rot that giving the planner its own session was avoiding. The
# per-line cap is hard where `_recent`'s is soft, because a line here is
# supposed to be a sentence the planner wrote — a tool payload pasted in whole
# is the failure this is bounded against, and keeping half of one is no better
# than keeping all of it.
#
# ESTIMATED, and the two caps do not describe the same pad. PAD_CHARS is the
# one that usually bites: at the full PAD_LINE only six findings fit, and PAD
# only binds once they average under a hundred characters — which is what a
# real one-sentence finding looks like, so both are reachable. Raising PAD
# alone changes nothing. Wants a week of real pads before either moves.
PAD = 12
PAD_CHARS = 1200
PAD_LINE = 200

# Unprefixed, so ADK writes it into the *session* row and one conversation's
# pad is its own — see #64. `app:` would be `StorageAppState`, one row for the
# whole app, which buys cross-session contention nobody asked for and which
# SQLite has no row lock behind.
PAD_KEY = "plan_pad"

# The name the live agent calls the planner by, as a session's events spell it.
# `main._carry_forward` looks for it to tell a thread that ran a plan of its
# own from one that only inherited a pad — see :func:`ran`. Written out here
# rather than read off the tool, because `plan.py` imports this module and not
# the other way round; `tools/plan_check.py` holds the two to each other.
TOOL = "plan"

NOTHING_TO_KEEP = (
    "A finding needs something in it. Write the fact itself, in a sentence."
)

# Not an error. The planner's tools are the live agent's tools, and the live
# agent can call this one directly, where there is no plan and nothing to carry
# anything to.
NO_PAD = (
    "There is nowhere to write this — nothing is carrying findings here. Say "
    "what you found in your answer instead."
)


def refused(note: str | None) -> bool:
    """Whether what :func:`spend` said means the caller must run nothing.

    A predicate and not a comparison, for the reason `_gws_run.refused` is one:
    :data:`TOO_WIDE` is formatted with a number in it, so `note == TOO_WIDE`
    would have been false for every refusal it ever produced.
    """
    return bool(note) and STOPPED in note


class _Budget:
    """What one plan has spent so far.

    Three counters, because two of the limits are per-plan and one is per-turn.
    `spent` is calls and only :data:`CEILING` reads it; `turns` is what
    :data:`WARN_AT` and :data:`STOP_AT` are counted in; `in_turn` is reset by
    :func:`turn` and is what :data:`WIDE` bounds.
    """

    def __init__(self) -> None:
        self.spent = 0
        self.turns = 0
        self.in_turn = 0


_BUDGET: contextvars.ContextVar[_Budget | None] = contextvars.ContextVar(
    "ai_terminal_plan_budget", default=None
)


@contextlib.contextmanager
def budget():
    """Give everything started inside this block one shared step budget.

    Yields the tally, which `tools/plan_check.py` reads for the same reason
    `gws_bench.py` reads the gws one: the number this exists to hold down is
    better read off the object the enforcement uses than counted from logs.
    """
    tally = _Budget()
    token = _BUDGET.set(tally)
    try:
        yield tally
    finally:
        _BUDGET.reset(token)


def turn() -> None:
    """Start a new planner turn, on the budget this work is running under.

    Called once per model round trip. Outside a :func:`budget` there is nothing
    to count and this does nothing, which is what a test wants and what the
    live agent's own model turns want — they are not plan turns.
    """
    tally = _BUDGET.get()
    if tally is None:
        return
    tally.turns += 1
    tally.in_turn = 0


def on_model(callback_context=None, llm_request=None):
    """ADK's `before_model_callback`, which is where a turn is observed.

    ADK invokes this once per model round trip, from inside the task
    `plan.plan` created — so :data:`_BUDGET` is in context here and
    :func:`turn` finds the right tally. It is the exact signal the count wants;
    inferring turns down in :func:`spend` from calls arriving without an
    intervening await would be heuristic timing where a marker exists.

    The two parameter names are the contract, not decoration. The type alias
    declares them positionally and every call site passes them by keyword —
    `flows/llm_flows/base_llm_flow.py:281-286` says so in a comment. Renaming
    them raises `TypeError` inside the flow, `plan` catches it, and every
    planned question comes back `FAILED`.

    Always None. Returning a response here would skip the model call, and a
    plan that is out of turns still has to write an answer — the refusal
    belongs in :class:`Counted`, where the model can read it as a tool result
    and say what it did not get to.
    """
    del callback_context, llm_request
    turn()
    return None


def spend() -> str | None:
    """Charge one call to this plan's budget and say how it stands.

    :data:`OUT_OF_STEPS` when the plan has spent every turn, :data:`TOO_WIDE`
    when this turn has already fanned out as far as it may, a warning once the
    count is worth mentioning, or None while there is room. Use :func:`refused`
    to tell the first two from the third. Outside a :func:`budget` there is
    nothing to charge and the answer is None — which is what a planner tool
    called from a test wants, and also what the live agent wants when it calls
    the same tool object directly.

    The width check comes second because the plan's own ceiling is the more
    final of the two: a turn that is both out of turns and too wide should hear
    "write the answer", not "ask again next turn", because there is no next
    turn to ask in.

    A refused call is not charged. `TOO_WIDE` tells the model to ask again next
    turn, and charging the refusal to :data:`CEILING` meant obeying it melted
    the plan's budget: twelve calls a turn hit :data:`OUT_OF_STEPS` on turn five
    with twenty-four tools actually run, against the eight turns and forty-eight
    calls the prompt promises. `in_turn` carries the backstop instead — it is
    the count that keeps rising when the turn callback is unwired, because
    nothing resets it, so `in_turn > CEILING` is what fires on call forty-nine.
    `spent > CEILING` beside it is unreachable while :data:`CEILING` stays
    ``STOP_AT * WIDE`` — eight turns of six is exactly forty-eight — and is left
    in for whoever unties those three numbers.

    Which of a too-wide turn's calls get refused is not a race.
    `asyncio.create_task` schedules in creation order, which is the order the
    model listed the calls in, and :meth:`Counted.run_async` charges before its
    first await. So the first :data:`WIDE` the model asked for are the ones that
    run, every time.
    """
    tally = _BUDGET.get()
    if tally is None:
        return None
    tally.in_turn += 1
    if (tally.turns > STOP_AT or tally.spent > CEILING
            or tally.in_turn > CEILING):
        return OUT_OF_STEPS
    if tally.in_turn > WIDE:
        return TOO_WIDE.format(wide=WIDE)
    tally.spent += 1
    if tally.turns >= WARN_AT:
        return LEFT.format(spent=tally.turns, total=STOP_AT)
    return None


_PAD: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "ai_terminal_plan_pad", default=None
)


@contextlib.contextmanager
def pad(found: list[str]):
    """Collect this plan's findings into a list the caller owns.

    The same arrangement as `plan._ask`'s `seen`, and for the same reason: this
    is bounded by ``asyncio.timeout``, and a cancellation takes every local
    inside the cancelled coroutine with it. The list belongs to `plan.plan`,
    which is above the timeout, so a plan stopped at two hundred seconds still
    has everything it wrote down — which is the case the pad is most worth
    having in.

    A ContextVar because :func:`note_finding` is called two levels below here,
    inside the planner's own `Runner`, and has no other way to reach the list.
    Same trick as :data:`_BUDGET` and `screen.live`.
    """
    token = _PAD.set(found)
    try:
        yield found
    finally:
        _PAD.reset(token)


def note_finding(finding: str) -> dict:
    """Write down something you have found out, so it is not lost.

    Use this the moment a step settles something: the three places you picked
    out of twenty, the dates the calendar came back with, the one number the
    document turned out to hold. Findings survive this plan being cut short,
    and the next question you are asked starts with them in front of it — so
    "what about the second one?" can be answered without running the search
    again.

    Write the fact, not the result. One sentence, the way you would say it out
    loud: "free nights next month: 14, 15 and 21 Nov". A pasted-in tool
    response is not a finding and is cut to the first two hundred characters.

    It does not use a step. Call it in the same turn as your next lookup and it
    costs nothing at all — a turn holding nothing but findings is still a turn.

    Args:
        finding: What you now know, in a sentence.

    Returns:
        Confirmation, and how many findings are being carried.
    """
    found = _PAD.get()
    line = " ".join(str(finding or "").split())[:PAD_LINE]
    if not line:
        return {"kept": False, "note": NOTHING_TO_KEEP}
    if found is None:
        return {"kept": False, "note": NO_PAD}
    found.append(line)
    # Trimmed here as well as on the way to the session, so a planner that
    # writes findings in a loop cannot grow this without bound. Newest kept,
    # which is what :func:`_kept` would do to the same list a moment later.
    dropped = max(0, len(found) - PAD)
    if dropped:
        del found[:dropped]
    return {"kept": True, "held": len(found), "dropped": dropped}


def _kept(found) -> list[str]:
    """The findings that fit, newest first while collecting, oldest dropped.

    A sequence, and specifically not a string: `found` can come back off a
    session row somebody else's release wrote, and a bare string is iterable —
    it would come back as a pad of single characters rather than as nothing.
    """
    if not isinstance(found, (list, tuple)):
        return []
    kept: list[str] = []
    total = 0
    for one in reversed(found):
        line = " ".join(str(one).split())[:PAD_LINE]
        if not line:
            continue
        kept.append(line)
        total += len(line)
        if len(kept) >= PAD or total >= PAD_CHARS:
            break
    return list(reversed(kept))


def held(state) -> list[str]:
    """The findings carried on this conversation's session state.

    Tolerates whatever is under the key. Nothing else writes it, but a pad read
    back out of a session row is data from a previous release of this program,
    and a plan is not worth failing over a shape that changed.
    """
    try:
        return _kept(state.get(PAD_KEY))
    except Exception:
        logger.warning("plan pad unreadable, starting empty", exc_info=True)
        return []


def ran(events) -> bool:
    """Whether this thread ever called `plan`.

    What `main._carry_forward` asks before handing a pad to the thread
    replacing this one. A conversation that inherited a pad and never ran a
    plan of its own has nothing on it that belongs to it, and passing it on
    again is how a finding settled three days ago arrives in `found` labelled
    as a lookup not worth spending a turn on. The recap avoids the same thing
    by being rewritten on every rollover, empty and all — see
    `main._carry_forward`; this is the pad's version of that rule, and without
    one the pad never expires at all.

    Tolerant of the event shape for the reason :func:`held` is tolerant of the
    pad's: these are rows some previous release wrote, and a rollover is not
    worth failing over one of them.
    """
    for event in events or []:
        content = getattr(event, "content", None)
        for part in (content.parts if content else None) or []:
            for ref in (getattr(part, "function_call", None),
                        getattr(part, "function_response", None)):
                if ref is not None and getattr(ref, "name", None) == TOOL:
                    return True
    return False


def as_text(found: list[str]) -> str:
    """The findings as the planner reads them."""
    return "\n".join(f"- {line}" for line in found)


def keep(state, found: list[str]) -> None:
    """Add this plan's findings to the conversation's pad.

    Added rather than written over. `plan` can run twice at once in one
    conversation (`plan._RUNNING`), and the two calls hold their own deltas, so
    the later write can be built on a pad that does not have the earlier one's
    entries in it. Merging means the loser of that race loses its own findings
    and not the whole pad.

    Never raises. The answer has already been written by the time this runs,
    and losing the pad is much the cheaper of the two outcomes.
    """
    if not found:
        return
    try:
        already = held(state)
        state[PAD_KEY] = _kept(already + [f for f in found if f not in already])
    except Exception:
        logger.warning("plan pad not saved", exc_info=True)


def nested() -> bool:
    """Whether this work is running under a plan.

    `gws` asks because two of its own guards were written against the live
    model's reflexes and read as wrong answers when a planner trips them.
    """
    return _BUDGET.get() is not None


class Guarded(FunctionTool):
    """A planner tool whose failure is that step's and not the whole plan's.

    A raise from a planner tool has nowhere good to go. It leaves the tool's
    task, ends the sub-run, and arrives at `plan._ask`, which cannot tell one
    step of six falling over from the run itself falling over and so has only
    `plan.FAILED` to say: no answer at all, the five results beside it thrown
    away with the sixth, and a flat "no change to their account" that a `gws`
    write earlier in the same plan makes untrue. So the failure is caught here,
    where the tool's name is still known, and handed back as an `error` the
    model reads — the same shape as a budget refusal, for the same reason. The
    instruction already asks the planner to say what it did not get to.

    This is not a belt on top of tools that handle their own errors.
    `maps_search`, `maps_route`, `maps_search_along_route`, `web_search` and
    `image_search` have no `try` anywhere in them, and `web_search` calls
    `raise_for_status` on a 30 s request made from a Pi over Wi-Fi. A 429 on
    one lookup of six is the ordinary case.

    A subclass rather than a wrapper function because the declaration the model
    is shown has to stay the tool's own: ADK builds it from
    :attr:`FunctionTool.func`, so `Guarded(show_plan)` advertises exactly what
    `show_plan` does, down to the docstring.
    """

    async def run_async(self, *, args: dict[str, Any],
                        tool_context: ToolContext) -> Any:
        try:
            return await super().run_async(args=args,
                                           tool_context=tool_context)
        except Exception as err:  # not BaseException: a cancel is the plan's
            logger.exception("plan step %s failed", self.name)
            return {"error": BROKE.format(tool=self.name,
                                          why=type(err).__name__)}


class Counted(Guarded):
    """A planner tool charged to the plan's budget, and refused when it is out.

    Two ways to be refused, and they are not the same: out of turns is the end
    of the plan, and too wide is the end of this turn. Both come back as an
    `error` the model reads and can act on — :func:`refused` covers them
    together so a third one cannot be added and quietly not enforced.

    The refusal is a returned dict and not a raise. A planner that has run out
    of steps still has to write an answer out of what it found, and the string
    it gets back says so. :class:`Guarded`, which this extends, is the same
    argument applied to the step that was allowed to run and broke.
    """

    async def run_async(self, *, args: dict[str, Any],
                        tool_context: ToolContext) -> Any:
        note = spend()
        if refused(note):
            logger.warning("plan refusing %s: %s", self.name, note)
            return {"error": note}
        result = await super().run_async(args=args, tool_context=tool_context)
        # Only onto a dict, which is what every tool here returns. A string
        # result would have to be rewritten to carry this, and rewriting an
        # answer to append a budget note is how the note ends up read aloud.
        if note and isinstance(result, dict):
            result["budget"] = note
        return result


def counted(tool: FunctionTool) -> Counted:
    """The same tool, charged to the plan's step budget."""
    return Counted(tool.func)


def _within(where, steps: list[str]) -> list[int]:
    """The indexes in `where` that are actually steps, sorted and deduplicated.

    Written to be un-crashable rather than strict. This is the one call the
    person can see, and an `IndexError` raised while drawing it takes the whole
    picture away and leaves them watching nothing — the same argument the
    `0 <= current < len(steps)` guard this replaces was making. So a bare int,
    a numeric string, a repeat and an index off the end are all tolerated, and
    anything else is dropped.
    """
    if where is None:
        return []
    if isinstance(where, (int, str)):
        where = [where]
    out = set()
    for one in where:
        try:
            i = int(one)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(steps):
            out.add(i)
    return sorted(out)


async def show_plan(steps: list[str], running: list[int] | None = None,
                    done: list[int] | None = None) -> dict:
    """Show the plan on the terminal's screen, and update it when it changes.

    Call this as soon as you have a plan, and again every time what is running
    changes — when steps go out, when they come back, and when a result sends
    you back to change what is left. The person watching the screen has no
    other way to see what you are doing on their behalf.

    Args:
        steps: The steps, in order, one short phrase each — "find dinner
            nearby", "check it is open tonight". Write them in the language the
            person is speaking; they are shown as you write them. Six words is
            plenty.
        running: Which steps are going out this turn, counting from 0. Several
            at once when you are running several at once.
        done: Which steps have already come back, counting from 0.

    Returns:
        Confirmation that the screen was updated.
    """
    # A cursor was what this took, and neither half of what a cursor means
    # survives a fan-out: steps start together and come back out of order, so
    # "everything before this is finished and this one is running" is two
    # claims that are both false. Two sets, and a step named in both is drawn
    # as running, because the one still going is the more useful thing to see.
    live = _within(running, steps)
    settled = [i for i in _within(done, steps) if i not in live]

    # The voice channel too, and this is the only thing on it. A nested `gws`
    # or `browse` opens a `Notes` of its own, so its notes go there and stay
    # there — see `plan.plan`. This call is made by the planner itself, in the
    # context `Notes.start` snapshotted, so `_progress.note` still finds the
    # live channel and the person hears which step is running.
    #
    # One sentence either way. Three steps starting together is three calls to
    # `opening()` deep in the tools and none of them can speak, so this is the
    # whole account of a fan-out — and it names the steps, because "three steps
    # at once" tells the person nothing the screen is not already showing.
    if len(live) == 1:
        _progress.note("step", f"Step {live[0] + 1} of {len(steps)}: "
                               f"{steps[live[0]]}")
    elif live:
        _progress.note("step", f"{len(live)} steps of {len(steps)} at once: "
                               + "; ".join(str(steps[i]) for i in live))

    shown = {"steps": len(steps), "running": live, "done": settled}
    handle = screen.live_card()
    if handle is None:
        # No live call above this one: a test, or the planner reached from a
        # script. Reporting progress is not a thing a tool should be able to
        # fail at — see `_progress.note`.
        return {"shown": False, **shown}
    session_id, call_id = handle
    await screen.push(session_id, {
        "type": "plan",
        "id": call_id,
        "steps": [str(s) for s in steps],
        "running": live,
        "done": settled,
    })
    return {"shown": True, **shown}
