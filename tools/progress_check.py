"""Check that the card and the voice keep their two different paces.

`Notes.watch` reports one slow tool call on two channels at once: a note is
drawn on the card the moment it arrives, on the two conditions the `Notes`
docstring gives, and a rationed few of the same notes are spoken. The two
channels used to be one — `_show` sat inside the speak block — and the card
froze after two states with half a minute of the call still to run.

That regression is invisible from the outside and cheap to reintroduce: moving
one line back inside the gate does it, and so does any change that lets the
loop park on the work instead of draining the queue. So this drives a real
`Notes` against a fake queue and a fake screen and asserts the shape.

The timings are scaled down by :data:`SCALE`, because the real ones make a
faithful run take 40 seconds. What is being checked is the shape — the notes
drawn, at most `LIMIT` spoken, `QUIET` before the first and `GAP` between —
and that survives the scaling. The constants themselves are one read of
`_progress.py`.

The one case here that is not timed at all is the tail drop, because timing it
would be a millisecond race. The work emits its notes and returns in the same
event-loop step instead, which is what "in the same breath" means.

    uv run python tools/progress_check.py

Exits non-zero on the first case that does not hold.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ai_terminal.live import screen  # noqa: E402
from ai_terminal.live.tools import _progress  # noqa: E402

# A tenth of everything. Nothing here depends on the wall-clock values, only on
# notes falling either side of them.
SCALE = 0.1

# How far under a deadline a stamped time may land and still count as having
# waited for it. The two ends of the one measurement below are not the two ends
# of the rule it checks: `watch` sets its next deadline and only then formats
# and sends the line (`_progress.py:242`), while `FakeQueue` stamps inside the
# send. What lands here is therefore GAP plus the difference between the two
# sends' own latencies, and the first line pays for whatever `logging` sets up
# on its way out. Measured on 2026-09-10, fifteen runs: the gap clears its 1.0s
# bar by about 0.9ms and by as little as 0.17ms, and a sweep the day before saw
# it land under and take the rest of this file with it. 5ms is twenty times the
# thinnest margin seen and four hundred times under the 0.4s between the
# closest notes on any timeline here, so a loop that dropped GAP entirely still
# fails.
SLACK = 0.005

# What a sent turn has to look like to count as one of the two kinds. Taken out
# of the constants rather than copied from them: a phrase copied from `SAY`
# stops matching the day `SAY` is reworded, and then the three checks that
# assert *nothing* was spoken all pass having matched nothing. A check that
# fails loudly on a reword is fine; one that goes quietly green is not.
SAY_HEAD = _progress.SAY.split("{doing}")[0].format(tool="gws")
OPEN_TEXT = _progress.OPEN.format(tool="gws")


class FakeScreen:
    """`screen.push`, remembering when each frame was drawn."""

    def __init__(self):
        self.frames: list[tuple[float, dict]] = []
        self.started = time.monotonic()

    async def push(self, session_id: str, frame: dict) -> None:
        self.frames.append((time.monotonic() - self.started, frame))

    # The real one, not a stand-in. `Notes` asks it which session to draw on,
    # and that answer is `plan`'s whole reason for existing — see
    # `screen.session_of`. Faking it here would fake the only interesting part.
    session_of = staticmethod(screen.session_of)

    def states(self) -> list[str]:
        return [f["state"] for _, f in self.frames]


class FakeQueue:
    """`live_request_queue`, remembering when each turn was sent."""

    def __init__(self):
        self.sent: list[tuple[float, str]] = []
        self.started = time.monotonic()

    def send_content(self, content) -> None:
        self.sent.append(
            (time.monotonic() - self.started, content.parts[0].text))

    def spoken(self) -> list[tuple[float, str]]:
        return [(t, s) for t, s in self.sent if s.startswith(SAY_HEAD)]

    def said(self) -> list[str]:
        """The state each spoken line names, dug back out of the sentence.

        `run` writes every note's text as `Doing <state>.`, and `SAY` drops it
        in whole, so this recovers which note the line was about. Which is the
        only way to see it: what goes on the wire is the sentence, not the
        state name the card gets.
        """
        return [s.split("Doing ")[1].split(".")[0] for _, s in self.spoken()]

    def opened(self) -> list[tuple[float, str]]:
        return [(t, s) for t, s in self.sent if s == OPEN_TEXT]


class FakeContext:
    """The three attributes `Notes.__init__` reads off a `ToolContext`."""

    def __init__(self, queue):
        self.session = type("S", (), {"id": "check"})()
        self.function_call_id = "call-1"
        self._invocation_context = type(
            "I", (), {"live_request_queue": queue})()


async def run(timeline: list[tuple[float, str]], seconds: float,
              live: bool = True, on_screen: bool = True,
              gasp: tuple[str, ...] = (), then: float = 0.0,
              module_note: bool = False):
    """Feed `Notes` a timeline of notes over a call lasting `seconds`.

    Returns the fake screen and the fake queue, both timestamped from the
    moment the watch started. `live=False` is the non-live path, where
    `live_request_queue` is None and nothing can be said; `on_screen=False` is
    what `browse` passes, where there is a queue but no card.

    `gasp` is notes the work itself emits immediately before returning, with no
    await between them and the return, which is the only way to put a note on
    the queue and finish the job in one event-loop step reliably. `then` keeps
    the work alive for that long afterwards, which is the same notes arriving
    in time to be drawn — the contrast that makes the drop a decision rather
    than an accident of scheduling. It is also the only shape here that ever
    has more than one note queued at once, so the runs that use it are the only
    ones that can see the queue hand them back out of order.

    `module_note=True` sends the gasped notes through `_progress.note` instead
    of through this channel, which is the route the sub-agent's own tools take
    — they are handed nothing and find the channel in the contextvar.
    """
    screen = FakeScreen()
    queue = FakeQueue()
    _progress.screen = screen
    notes = _progress.Notes(FakeContext(queue if live else None), "gws",
                            on_screen=on_screen)
    notes.opening()

    emit = _progress.note if module_note else notes.note

    async def work():
        await asyncio.sleep(seconds)
        for state in gasp:
            emit(state, f"Doing {state}.")
        if then:
            await asyncio.sleep(then)
        return "the answer"

    async def feed():
        for at, state in timeline:
            await asyncio.sleep(max(0.0, at - (time.monotonic() - start)))
            notes.note(state, f"Doing {state}.")

    start = time.monotonic()
    screen.started = queue.started = start
    task = notes.start(work())
    await asyncio.gather(notes.watch(task), feed())
    await task
    return screen, queue


# Counted rather than written down, so the closing line cannot drift from the
# number of cases the way a figure typed into a commit message does.
RAN = 0


def check(name: str, ok: bool, detail: str, got=None, want=None) -> None:
    global RAN
    RAN += 1
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        if want is not None:
            print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


async def main() -> None:
    real = (_progress.QUIET, _progress.GAP, _progress.OPENING)
    _progress.QUIET, _progress.GAP, _progress.OPENING = (v * SCALE for v in real)
    quiet, gap, opening = (v * SCALE for v in real)
    print(f"scaled by {SCALE}: QUIET={quiet}s GAP={gap}s "
          f"LIMIT={_progress.LIMIT} (real: {real[0]}s {real[1]}s)")

    # The run this issue is about: a long call with notes throughout, the shape
    # of a question that touches the calendar, then Drive, then the mailbox.
    # Eleven distinct names, not nine identical ones. `drawn == wanted` can
    # only see a state mix-up across a boundary between two different names, so
    # a run of nine `mail` frames left the check discriminating at two points
    # out of eleven, and left nine of the frames interchangeable. Drawing
    # `got[0].split("-")[0]` — the right number of frames, the wrong names —
    # was invisible on that timeline and is nine wrong frames on this one.
    print("\n== a 3.8 s call with eleven notes ==")
    timeline = [
        (0.13, "calendar"), (0.53, "drive"), (0.54, "mail-1"), (0.94, "mail-2"),
        (0.95, "mail-3"), (1.25, "mail-4"), (2.18, "mail-5"), (2.19, "mail-6"),
        (2.53, "mail-7"), (2.91, "mail-8"), (2.92, "mail-9"),
    ]
    screen, queue = await run(timeline, 3.8)
    spoken = queue.spoken()
    print(f"   screen: {[f'{t:.2f}' for t, _ in screen.frames]}")
    print(f"   spoken: {[f'{t:.2f}' for t, _ in spoken]}")

    # On the states and not on the count. Counting frames says the loop kept
    # going; it does not say it drew the right thing. `_show(got[1])` — the
    # sentence instead of the state name, off the same tuple — is one character
    # and draws all eleven frames with all eleven states wrong.
    drawn, wanted = screen.states(), [state for _, state in timeline]
    check("every note is drawn, in order", drawn == wanted,
          f"{len(drawn)} frames for {len(wanted)} notes", drawn, wanted)
    check("speech stops at LIMIT", len(spoken) == _progress.LIMIT,
          f"{len(spoken)} spoken")
    # Indexed only after the count above has held, and guarded anyway: an
    # IndexError here is a traceback where a FAIL line belongs, and the count
    # check stops being the thing that catches it the moment it changes.
    first = spoken[0][0] if spoken else -1.0
    check("nothing spoken before QUIET", bool(spoken) and first >= quiet,
          f"first at {first:.2f}s")
    apart = spoken[1][0] - spoken[0][0] if len(spoken) > 1 else -1.0
    check("GAP between spoken lines",
          len(spoken) > 1 and apart >= gap - SLACK,
          f"{apart:.4f}s apart")

    # The regression itself. With `_show` back inside the speak block, or with
    # the loop parking on the work once it has said its two lines, every frame
    # lands before the last spoken one and the card is still after that.
    after = [t for t, _ in screen.frames if spoken and t > spoken[-1][0]]
    check("the card keeps moving after the voice stops", len(after) >= 3,
          f"{len(after)} frames after the last line"
          + (f", up to {after[-1]:.2f}s" if after else ""))

    check("the opening line goes out once", len(queue.opened()) == 1,
          f"{len(queue.opened())} opening lines")

    # What was said, not only when. `watch` holds one slot and overwrites it,
    # so the line names the state the work is in when the line comes due, not
    # the one that was pending when the slot opened. Reverse it — `if pending
    # is None: pending = got` — and the whole suite stayed green while the
    # second line changed from `mail-4` to `drive`.
    #
    # Only the second line is asserted. The first comes due at QUIET, 0.03 s
    # before `drive` arrives, and which side of that boundary the loop wakes on
    # is a race; `mail-4` is alone in a 0.9 s window.
    said = queue.said()
    check("the spoken line names the latest state, not the pending one",
          said[1:] == ["mail-4"], f"said {said}", said[1:], ["mail-4"])

    # The burst the class docstring describes: five notes inside one GAP are
    # five frames and one sentence. Read off this run rather than staged on its
    # own — the notes between the two spoken lines already have that shape.
    #
    # It is the *when* of the frames that this one holds, which is what the
    # checks above leave open: hold every `_show` in a list and flush the lot
    # just before `_DONE` and the order is still right, the count is still
    # eleven, and every frame still lands after the last spoken line. Only this
    # count goes to zero.
    burst = [t for t, _ in screen.frames if spoken[0][0] < t < spoken[1][0]]
    check("a burst inside one GAP is five frames and one sentence",
          len(burst) == 5, f"{len(burst)} frames between the two lines")

    # The three fields `static/js/app.js` reads: `:365` switches on `type`,
    # and `handleProgress` looks the card up by `id` before drawing `state`
    # (`:397-398`) — a wrong `id` finds no card and draws nothing. `type` and
    # `id` are touched nowhere else here, so renaming either is silent without
    # this line — the card simply stops updating, which is the bug this whole
    # module exists for. `state` is the one field the rest of the suite reads,
    # and renaming that gets caught either way, as a `KeyError` out of
    # `states()` rather than as a FAIL line.
    #
    # `.get` and not `[...]`: a dropped field is the mutation being caught, and
    # a KeyError here is a traceback where a FAIL line belongs.
    frame = screen.frames[0][1]
    read_by_app = {k: frame.get(k) for k in ("type", "id", "state")}
    want_frame = {"type": "progress", "id": "call-1", "state": "calendar"}
    check("the frame carries what app.js reads", read_by_app == want_frame,
          f"{read_by_app}", read_by_app, want_frame)

    # The other half of the one-slot rule, and the half every run above is
    # blind to. `watch` clears the slot when it speaks (`_progress.py:240`);
    # drop that line and the slot still holds the last note, so the same state
    # is spoken again every GAP until LIMIT. Nothing above sees it because
    # every run that reaches a second line also supplies a second state to fill
    # the slot with. One note and two GAPs of runtime is the shape that has
    # nothing to refill it: one line, or two identical ones.
    print("\n== one note, and two GAPs of runtime after it ==")
    _, queue = await run([(0.05, "calendar")], quiet + gap * 2)
    said = queue.said()
    check("a note is spoken once, not once per GAP", said == ["calendar"],
          f"said {said}", said, ["calendar"])

    # A cached document comes back in about a second, which is inside OPENING.
    print("\n== a call that answers inside OPENING ==")
    screen, queue = await run([(0.05, "document")], opening * 0.5)
    # On `sent` rather than on `spoken()` and `opened()`: those two classify,
    # and nothing at all should go out here, which is the stronger claim and
    # the one that cannot pass by failing to match.
    check("nothing goes out at all", not queue.sent,
          "the answer beat the opening beat")
    # And nothing is drawn either, which is the case the class docstring now
    # names: `watch` returns before the loop, so a note already on the queue
    # is dropped. This is the path item 1 of the review lived on.
    check("nothing is drawn either", screen.states() == [],
          "watch returns before the loop", screen.states(), [])

    print("\n== a call that answers inside QUIET ==")
    screen, queue = await run([(0.05, "document")], quiet * 0.9)
    check("the opening line goes out and nothing else",
          [s for _, s in queue.sent] == [OPEN_TEXT],
          f"{len(queue.sent)} turn(s), since the silence was long enough "
          "to need filling", [s[:40] for _, s in queue.sent], [OPEN_TEXT[:40]])
    check("the note is drawn regardless", screen.states() == ["document"],
          f"{screen.states()}", screen.states(), ["document"])

    # The other face of "still running", and the one nothing defended until
    # now. Above, the call is over before the loop starts; here it ends while
    # the loop is in it. `_next` returns _DONE on `work.done()` whatever is
    # sitting in the queue, so a call that answers in the same breath as its
    # notes drops all of them. Not a timing case — the work emits and returns
    # without awaiting in between.
    print("\n== a call that answers in the same breath as its notes ==")
    gasp = ("mail-1", "mail-2", "mail-3", "mail-4", "mail-5")
    screen, _ = await run([], 0.3, gasp=gasp)
    check("five notes queued as the answer lands are all dropped",
          screen.states() == [],
          "not just the last one", screen.states(), [])
    # The contrast, and what makes the case above about the decision rather
    # than about five notes never reaching the queue. It is also one of the two
    # runs here that ever have more than one note on the queue at once — the
    # `module_note` one below is the other — so between them they are what an
    # `asyncio.LifoQueue` at `_progress.py:151` would fail. Everywhere else the
    # loop drains each note before the next arrives and order is unobservable.
    screen, _ = await run([], 0.3, gasp=gasp, then=1.0)
    check("the same five with a second of work left are all drawn, in order",
          screen.states() == list(gasp),
          f"{len(screen.states())} frames", screen.states(), list(gasp))

    # `start` binds the channel for the work and unbinds it for the caller.
    # Both halves matter: the sub-agent's tools are handed nothing and reach
    # this channel through the module-level `note()`, while `_CURRENT` is
    # process-wide, so a binding left set here is one an ADK generator
    # resuming in this context would pick up as its own.
    print("\n== note() from inside the work, and nothing left bound ==")
    pair = ("mail-1", "mail-2")
    screen, _ = await run([], 0.3, gasp=pair, then=1.0, module_note=True)
    check("note() finds the channel with nothing passed to it",
          screen.states() == list(pair), f"{screen.states()}",
          screen.states(), list(pair))
    check("and the caller is left with nothing bound",
          _progress._CURRENT.get() is None, "start reset the token it set")

    print("\n== off the live path (no queue) ==")
    screen, queue = await run([(0.05, "mail"), (0.4, "mail")], 1.0, live=False)
    check("the card is drawn anyway", screen.states() == ["mail", "mail"],
          f"{len(screen.frames)} frames, no exception",
          screen.states(), ["mail", "mail"])
    check("nothing is sent", not queue.sent, "the queue was never reached")

    # `browse` asks for this: it draws its own screenshots and wants none of
    # these frames. Voice only, which is a whole tool's worth of the drawing
    # claim not holding.
    print("\n== off screen (what browse passes) ==")
    screen, queue = await run([(0.05, "page"), (0.4, "page")], 1.0,
                              on_screen=False)
    check("on_screen=False draws nothing", screen.states() == [],
          "browse gets the voice and none of the card", screen.states(), [])
    # Both channels, not just the opening one. This run is long enough to speak
    # as well, so counting only `opened()` would pass with the rationed line
    # suppressed — "voice only" is the claim, not "opening only".
    check("and still speaks, on both channels",
          len(queue.opened()) == 1 and len(queue.spoken()) >= 1,
          f"{len(queue.opened())} opening line, "
          f"{len(queue.spoken())} rationed line(s)")

    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    asyncio.run(main())
