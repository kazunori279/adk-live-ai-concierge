"""Saying where a slow tool has got to, while it is still getting there.

`gws` takes five seconds for a calendar question and half a minute for a
mailbox one; `browse` runs the better part of a minute. Both are declared
NON_BLOCKING so the conversation stays open while they work, and both used to
spend that whole window silent — which on a device with no cursor and no
spinner is indistinguishable from not having heard the question.

A note arrives as a turn of text on the live request queue, addressed to the
model, which reads it and says it out loud. That is a roundabout way to do it,
and the two direct-looking routes were both tried first:

- **A second FunctionResponse against the same call id.** ADK's streaming path
  offers this — every chunk an async generator yields is built into one. But
  the Live API reads a response with no `will_continue=True` on it as the
  call's last word, and ADK never sets that flag
  (`flows/llm_flows/functions.py`, `_build_function_response_content`, which
  copies `response` and `scheduling` and nothing else). So the model took the
  note for gws having answered "still searching their mail", and asked the same
  question again, once a second, for as long as it was let: 85 calls in 70
  seconds. Setting the flag by hand does not work either — Vertex rejects
  `will_continue` on `tool_response.function_responses` outright, closing the
  socket with a 1007.
- **`Event(message=...)`**, which a streaming tool may also yield. That goes to
  the browser, not to the model — the screen already has :mod:`screen` for
  that, and a note nobody says out loud is not what this is for.

So a note goes in as a turn, marked `[terminal]` and carrying its own
instructions, because it lands with no conversation around it to explain what
it is. It is written in English, for the model rather than for the person: the
model is the only thing here that knows what language they are speaking, and it
is being asked to relay a state, not to read out a string. The card gets the
same note as a state name and picks its own words for it.

A turn is a blunter instrument than a FunctionResponse with a scheduling on it:
it is delivered when it is sent, not when the model next goes quiet. Hence the
timings below — nothing for the first few seconds, a gap between notes, and a
hard cap. Interrupting the person twice in half a minute is the budget.

None of that applies to the screen, which costs nobody anything to read and
gets its notes as they arrive, on the conditions :class:`Notes` gives. The two
paces are the whole design here: where there is a panel it keeps moving for as
long as the call is still running, and the voice speaks twice. Still running is
the end of it — the loop stops drawing the moment the work is done, so a note
that arrives in the same breath as the answer is dropped. `browse` has no panel
of this kind — it draws its own screenshots and passes `on_screen=False`, so
for that tool the design is the voice and nothing else.
"""

import asyncio
import contextvars
import logging
import time

from google.adk.tools import ToolContext
from google.genai import types

from .. import screen

logger = logging.getLogger(__name__)

# How long the call has to have been running before a note is worth speaking. A
# document already in the cache comes back in about a second, and announcing
# work that is already done is noise.
QUIET = 5.0

# And how long to leave between notes after that.
GAP = 10.0

# Two in a thirty-second call. A third is a running commentary. The screen is
# not rationed this way — see :meth:`Notes.watch`.
LIMIT = 2

# How long the opening line waits before it goes out. It is asked for the
# instant the tool is entered, which is also when the model is saying its own
# "let me look", and a turn landing mid-sentence clips it. Long enough for a
# short sentence to finish, short enough to still be filling the silence it
# exists for. There is no way to ask whether the model actually spoke, so this
# is a guess at the length of a sentence rather than a check.
OPENING = 2.0

# The note as the model receives it. The instruction travels with every one of
# them: this arrives as a turn of its own with no conversation around it, so
# there is nowhere else for the rules to sit.
SAY = (
    "[terminal] Nobody said this — it is the {tool} call you are waiting on, "
    "reporting where it has got to: {doing} Say that in one short sentence, in "
    "the language the person is speaking, and go back to waiting. It is not "
    "the answer and it holds no facts, so add none and guess nothing. The call "
    "is still running and will answer by itself: do not call any tool."
)

# And the one that goes out with the call itself. The agent is told to say it
# is looking as it dispatches, and often does — but not always, and a question
# that lands in silence reads as one that was not heard. This is the prompt
# that makes it reliable, and it takes no for an answer when the agent has
# already spoken.
OPEN = (
    "[terminal] Nobody said this either — it is the {tool} call you just made, "
    "confirming it has gone out and will take anywhere from five seconds to a "
    "minute. If you have already told the person you are looking, say nothing. "
    "If you have not, tell them now: one short sentence on what you have gone "
    "to find, in the language they are speaking. Then wait. Do not call any "
    "tool, and do not guess at what the answer will be."
)

# `_next` returning "the work has finished", as distinct from "no note yet".
_DONE = object()


class Notes:
    """The progress channel of one tool call.

    The work calls :meth:`note` wherever it happens to be; the tool awaits
    :meth:`watch`, which passes each one on until the work is done.

    The two channels move at different speeds, and that is the point.

    Every note is drawn, for as long as there is a card and the call is still
    running, so the panel keeps moving instead of freezing half a minute before
    the answer. Both of those conditions bite. `on_screen` is False for
    `browse`, which has its own screenshots and wants none of these — for that
    tool this channel is voice only and draws nothing at all. And "still
    running" is meant literally: a call that answers inside :data:`OPENING`
    never reaches the loop, and one that answers in the same breath as its
    notes drops all of them, however many are queued, because an update
    delivered alongside the answer it was warning about is not an update.

    Speech is rationed: `watch` holds one slot, so what gets said is whatever
    the work is doing *now* rather than what it was doing when the line came
    due, and it says at most :data:`LIMIT` of them. A burst of five `gws`
    commands inside one :data:`GAP` is five frames and a single sentence,
    naming the fifth.

    `tools/progress_check.py` pins all of the above.
    """

    def __init__(self, tool_context: ToolContext, tool: str,
                 on_screen: bool = True):
        # `screen.session_of`, not the context's own session: under `plan` the
        # context belongs to a sub-run whose session nobody has registered a
        # screen for, and every frame would be dropped. The call id stays the
        # context's, because the card these frames belong to is this tool's
        # own and not the plan's.
        self.session_id = screen.session_of(tool_context)
        self.call_id = tool_context.function_call_id
        self.tool = tool
        self.on_screen = on_screen
        self._out = getattr(
            tool_context._invocation_context, "live_request_queue", None)
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._opening = False

    def opening(self) -> None:
        """Ask for a line saying the call has gone out, once there is a gap.

        Nothing is sent here. This is called the instant the tool is entered,
        which is also when the model is saying its own "let me look" — see
        :data:`OPENING`. :meth:`watch` sends it a beat later, and drops it if
        the answer beat the beat: a call that came back in a second never had a
        silence to fill.
        """
        self._opening = True

    def note(self, state: str, text: str) -> None:
        """Record where the work has got to.

        Called from the work, so it does not block and does not care what
        becomes of it. Whether it reaches the card at all, and whether it is
        also spoken, are :meth:`watch`'s decisions on the conditions the class
        docstring gives — not this one's.

        Args:
            state: A short name for the state, for the card, e.g. `mail`.
            text: The same thing in a sentence, for the model.
        """
        self._queue.put_nowait((state, text))

    def start(self, coro) -> asyncio.Task:
        """Run `coro` as the work this channel reports on.

        The binding is set and immediately dropped around `create_task`, which
        snapshots the context: the task keeps this channel, and the caller does
        not. That matters because an async generator shares the context of
        whoever resumes it, so a binding left set here would leak into ADK.
        """
        token = _CURRENT.set(self)
        try:
            return asyncio.create_task(coro)
        finally:
            _CURRENT.reset(token)

    async def watch(self, work: asyncio.Task) -> None:
        """Report on the work until it is done, at two different paces.

        Every note reaches the screen the moment it arrives, on the two
        conditions the class docstring gives. The panel is the cheap channel —
        nobody has to stop talking for it — and it is the one that has to keep
        moving, because a card that stops updating half a minute before the
        answer looks like a card that has crashed.

        Speech is rationed against the same notes: nothing for the first
        :data:`QUIET` seconds, :data:`GAP` between lines, :data:`LIMIT` in
        total. Past the cap the loop keeps running and keeps drawing, and says
        nothing more.

        The work is left running — the caller awaits it for the answer.
        """
        spoken = 0
        pending: tuple[str, str] | None = None
        ready = time.monotonic() + QUIET

        if self._opening:
            await asyncio.wait({work}, timeout=OPENING)
            if work.done():
                return
            self._send(OPEN.format(tool=self.tool))

        while True:
            # A deadline to wake on only when there is something waiting to be
            # said and room left to say it. Otherwise sleep until the next note
            # or the answer, whichever comes first.
            speaking = pending is not None and spoken < LIMIT
            got = await self._next(
                work, ready - time.monotonic() if speaking else None)
            if got is _DONE:
                return
            if got is not None:
                pending = got
                # Here and not in the speak block below, which is where it
                # used to be and where it will end up again the next time
                # somebody tidies this loop. That one line is the difference
                # between a card that keeps moving and a card that freezes two
                # states in. `tools/progress_check.py` fails if it moves.
                await self._show(got[0])
            if pending is None or spoken >= LIMIT or time.monotonic() < ready:
                continue

            state, text = pending
            pending = None
            spoken += 1
            ready = time.monotonic() + GAP
            # The state, not the sentence. Every caller in this tree passes a
            # fixed English constant as `text` except `_plan_run.py:599-603`,
            # which passes `f"Step {n} of {m}: {steps[i]}"` — and `steps` is
            # what the planner wrote in the speaker's own language. `state` is
            # the short handle the card uses and is a closed vocabulary, so it
            # says which note this was without saying what the note said. See
            # #235; `plan.py`'s own five lines have been sized since #206 and
            # this is the model's restatement of the same task.
            logger.info("%s progress: %s", self.tool, state)
            self._send(SAY.format(tool=self.tool, doing=text))

    async def _show(self, state: str) -> None:
        """Put the state on the card. Unrationed — see :meth:`watch`."""
        if not self.on_screen:
            return
        await screen.push(self.session_id, {
            "type": "progress",
            "tool": self.tool,
            "id": self.call_id,
            "state": state,
        })

    def _send(self, text: str) -> None:
        """Put one note on the wire. See the module docstring for the channel.

        Silent when there is no queue to put it on. `live_request_queue` is
        typed optional and is only set on the live path, so a tool exercised
        from a test or a non-live runner has nowhere to send — and the same
        reasoning as :func:`note` applies: reporting progress is not a thing a
        tool should have to be able to fail at.
        """
        if self._out is None:
            return
        self._out.send_content(types.Content(
            role="user", parts=[types.Part(text=text)],
        ))

    async def _next(self, work: asyncio.Task, timeout: float | None):
        """The next note, `None` if the wait ran out, `_DONE` if work is over.

        A finished job beats a queued note: an update spoken in the same breath
        as the answer it precedes is worse than no update.
        """
        getter = asyncio.ensure_future(self._queue.get())
        try:
            await asyncio.wait({getter, work}, timeout=timeout,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not getter.done():
                getter.cancel()
        if work.done():
            return _DONE
        return getter.result() if getter.done() else None


_CURRENT: contextvars.ContextVar[Notes | None] = contextvars.ContextVar(
    "ai_terminal_progress", default=None
)


def note(state: str, text: str) -> None:
    """Record progress from wherever in the work you are.

    A no-op outside a call that is reporting any — the sub-agent's tools are
    also reachable from a test, and none of them should have to know.
    """
    notes = _CURRENT.get()
    if notes is not None:
        notes.note(state, text)
