"""The socket a tool can draw on directly, without going through the model.

Most tools answer inside the turn that called them, and their result reaches
the browser as an ordinary ADK event. A NON_BLOCKING tool does not: ADK runs it
in a background task and feeds the FunctionResponse straight back into the live
request queue, so ``run_live`` never yields it and the page never sees it. What
such a tool wants on screen — ``browse``'s screenshots, ``gws``'s answer — goes
down this side channel instead.

``main.py`` registers one sink per WebSocket. A tool call only knows the session
it is running in, so the session id is how it finds the right screen.

That last sentence stops being true one level down. ``plan`` runs its tools
inside a sub-``Runner``, which mints a session of its own on a private service
— so ``gws`` under a planner asks its `ToolContext` which session it is in and
gets an id nobody has ever registered, and every frame it draws is dropped
here. :func:`live` is how the live session is carried across that boundary, and
:func:`session_of` is what a tool asks instead of asking its context.
"""

import asyncio
import contextlib
import contextvars
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

Sink = Callable[[dict], Awaitable[None]]
Closer = Callable[[], Awaitable[None]]


# How long one screen may hold a single frame before it stops being a screen.
#
# Nobody waits on this: :func:`push` hands a frame to a writer and returns, so
# the number does not appear in the latency of any card on any other page. All
# it decides is when a page that has stopped reading is taken off the session
# and its socket closed. That makes a generous value the safe one, and this is
# generous on purpose: the largest thing on this path is one `browse`
# screenshot, a JPEG at quality 70 base64'd into a frame, and the furthest page
# is the kiosk one ssh hop away on the house LAN. Ten seconds is orders of
# magnitude past that and still inside a single browse step, so a laptop that
# went to sleep mid-run is gone before the run is.
#
# A module constant and not an env var, unlike `BROWSE_BUDGET_S` and
# `RECAP_BUDGET_S` next door. Those are budgets somebody might want to spend
# differently; this is a statement about what a WebSocket write on a LAN costs,
# and there is no household setting of it. Every knob in `.env.example` also
# carries a paragraph of prose and, for the ones that can be set wrong, an
# import-time refusal — more surface than a number nobody will type.
DEADLINE_S = 10.0


class _Screen:
    """One page on a session: where to send to it, what kind it is, and the
    task that does the sending.

    One object rather than registries side by side, because the parts are only
    ever added and removed together and a second structure is a second thing to
    get out of step — the stack is walked by identity (see :func:`unregister`)
    and a parallel set of kiosk sinks, or of queues, would have to be walked the
    same way, twice, on every path.

    `kiosk` is read by :func:`wake_sink` and by nothing else. `send` stays the
    thing every other caller gets back, so :func:`sink` returns a `Sink` here
    exactly as it did before.

    `close` is how a page learns it has been dropped: see :func:`_drop`. It is
    optional because most callers of :func:`register` are check scripts with a
    closure and no socket behind it, and a screen with nothing to close is
    simply removed in silence.

    `queue` and `writer` are the reason a page that has stopped reading cannot
    slow the page beside it. Frames go into the queue in order; the writer
    drains it, one send at a time, in a task of its own. A mutable object and
    not the `NamedTuple` this was, because the writer handle changes over the
    life of a page.
    """

    __slots__ = ("send", "kiosk", "close", "queue", "writer")

    def __init__(self, send: Sink, kiosk: bool,
                 close: Closer | None = None) -> None:
        self.send = send
        self.kiosk = kiosk
        self.close = close
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.writer: asyncio.Task | None = None


# A list per session and not one sink, because the session id stopped being
# one-to-one with a connection at #19: every page open under the same profile
# shares it. Every entry is a screen to draw on — :func:`push` writes to all of
# them — because two pages that both stay up are not a reload, and "the newest
# owns the screen" left the older one blind for as long as the newer one lived.
# A dict was worse again: popped, a page that was still standing there was left
# with no screen at all and no way to notice. A kiosk in that state is not
# merely missing cards, it is deaf: `/api/wake` reaches a terminal through this
# registry and nothing else, so the wake word stopped working until somebody
# reloaded the page.
#
# Which one of them a wake word reaches is the one question with a different
# answer, because that frame opens a microphone rather than drawing something —
# see :func:`wake_sink`.
_SINKS: dict[str, list[_Screen]] = {}

# The close of a dropped screen, in flight. Only so that the task is not
# collected while it is running — see :func:`_drop`.
_CLOSING: set[asyncio.Task] = set()

# The live session and function call a nested run is working for, or None when
# the run is the live one and its own context already knows.
_LIVE: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "ai_terminal_live_card", default=None
)


@contextlib.contextmanager
def live(session_id: str, call_id: str):
    """Name the live session everything started inside this block draws on.

    A ContextVar for the same reason `_gws_run.budget` is one: nothing between
    a tool and its sub-agent's tools can carry an argument, because ADK calls a
    tool with the model's arguments and nothing else. `asyncio.create_task`
    snapshots the context, so a task created inside the block keeps the handle.
    """
    token = _LIVE.set((session_id, call_id))
    try:
        yield
    finally:
        _LIVE.reset(token)


def session_of(tool_context) -> str:
    """The session whose screen this work draws on.

    The live one when there is a :func:`live` block above — which is what a
    tool nested under ``plan`` wants, and the only thing that reaches a
    browser. Otherwise the tool's own, which for a tool the live agent called
    directly is the same session.
    """
    handle = _LIVE.get()
    return handle[0] if handle is not None else tool_context.session.id


def live_card() -> tuple[str, str] | None:
    """The session and function-call id of the live card being worked on.

    Both halves, unlike :func:`session_of`, and only for the card that belongs
    to the live call itself. A nested tool must not draw into it: its frames
    are keyed by its own function-call id so they land in a card of their own.
    """
    return _LIVE.get()


def register(session_id: str, send: Sink, kiosk: bool = False,
             close: Closer | None = None) -> None:
    """Put this connection's screen in front of any already on this session.

    `kiosk` is the page saying it is the one standing in the kitchen, which it
    knows because the unit that launched it put `?kiosk=1` on its URL. Nothing
    about being drawn on changes with it — see :func:`wake_sink` for the one
    question it answers. Defaulted, so a caller that has no opinion registers
    an ordinary page, which is what every browser tab is.

    `close` is what to do to a page that stops taking frames: see
    :func:`_drop`. Also defaulted, because a caller with a bare closure and no
    socket behind it has nothing to close.
    """
    _SINKS.setdefault(session_id, []).append(_Screen(send, kiosk, close))


def unregister(session_id: str, send: Sink | None = None) -> None:
    """Take this connection's screen away, leaving any behind it.

    `send` identifies the connection doing the unregistering. Session ids are
    stable across a reload now, so two connections can hold the same id — for
    the moment it takes the old one's `finally` to run, and indefinitely when
    two pages are open on one profile. Removing only this connection's own sink
    is what keeps the two cases apart: the one that is leaving never takes the
    screen of the one that has just arrived, and never leaves a page that is
    still up without one either.

    Identity and not equality, the same test the dict did: two closures over
    different sockets are not interchangeable, and `list.remove` would compare
    them with `==` — on the entry now, whose `kiosk` half two different pages
    can agree about. Omitted, `send` means the whole session's screens go,
    which is what a caller with only an id to go on means.
    """
    if send is None:
        _SINKS.pop(session_id, None)
        return
    stack = _SINKS.get(session_id)
    if stack is None:
        return
    for i, held in enumerate(stack):
        if held.send is send:
            del stack[i]
            break
    if not stack:
        _SINKS.pop(session_id, None)


def sink(session_id: str) -> Sink | None:
    """One screen for this session, or None if nobody is connected to it.

    The last connection to arrive. Not what a card goes through any more —
    :func:`push` writes to every screen on the session — so what is left of
    this is the question underneath it, "is there a screen at all": `browse`
    asks before starting a browser it would have nowhere to show, and every
    other caller is a check script asserting the order of the list.

    Do not hold the answer. A connection can leave and another arrive while a
    tool is still working, and a sink captured before that is a socket that is
    closing; ask :func:`push` per frame instead.
    """
    stack = _SINKS.get(session_id)
    return stack[-1].send if stack else None


def wake_sink(session_id: str) -> Sink | None:
    """The screen a wake word should reach, which is not always the newest.

    :func:`sink` is right for a card and wrong for this: a wake frame opens a
    microphone rather than drawing something, so it belongs to the page in the
    room where the word was said and not to whichever tab arrived last. A
    kiosk says so in its URL; nothing else can tell them apart, because the
    kiosk reaches this server down an ssh tunnel and so connects from
    127.0.0.1 like every browser on this machine.

    Newest kiosk if there is one, newest anything if there is not — so a
    developer alone on the Mac still gets the wake word, and a Pi that is the
    only client behaves exactly as it did. One screen either way, which is the
    property :func:`sink` already has and the reason not to widen this to the
    whole stack: every sink told is another microphone opening, and most of
    them are in rooms nobody is in.

    Newest kiosk and not the only kiosk, because a kiosk reload puts a second
    one on the stack for as long as the old connection's `finally` takes to
    run, and the frame belongs to the page that is actually up.

    The send and not the screen, because that is what every caller outside
    this module has ever wanted: `history_check` asks which of two pages the
    word would reach and compares the answer with the closure it registered.
    :func:`push_wake` needs the queue behind it as well, so the choosing is
    :func:`_wake_screen` and this is the half of it that leaves the module.
    """
    held = _wake_screen(session_id)
    return None if held is None else held.send


def _wake_screen(session_id: str) -> _Screen | None:
    """Which screen on this session a wake word should reach, entry and all.

    Split out of :func:`wake_sink` when the wake frame stopped being written
    on the caller's own stack: a frame that goes into a queue has to find the
    queue, and a `Sink` is a closure with no way back to the screen holding
    it. Walking the stack twice — once for the send, once to match it back to
    an entry — would be a second answer to a question with one answer, and the
    stack can change between the two walks.
    """
    stack = _SINKS.get(session_id)
    if not stack:
        return None
    for held in reversed(stack):
        if held.kiosk:
            return held
    return stack[-1]


def any_kiosk() -> bool:
    """Whether a panel is connected to this server at all, on any session.

    :func:`wake_sink` asks the same question one session at a time, because a
    wake frame has a session to go to. This one is for a caller that has no
    session and cannot have one: `screensaver.set_home` decides which page
    owns a position that is kept per process, and the refresh loop that reads
    it back runs on a timer with no page attached at all.

    Live and not remembered, which is the whole point of asking `screen` and
    not keeping a flag. A panel that is unplugged stops being here the moment
    its `finally` unregisters, and the answer changes with it; a flag would
    have to be told, and the thing that would have to tell it is the socket
    that has just gone.
    """
    return any(
        (held.get("kiosk") if isinstance(held, dict) else getattr(held, "kiosk", False)) is True
        for stack in _SINKS.values()
        for held in stack
    )


def sessions() -> list[str]:
    """Every session with a screen attached right now.

    For the one caller that has no session id to look up: the wake word. The
    monitor on the Pi knows a phrase was said in the room and nothing about who
    is talking to what, so `/api/wake` tells one screen on each — the one
    :func:`wake_sink` picks, which is a screen per session and not a screen per
    page. A list rather than a view, because the caller pushes while iterating
    it and a push can end with a connection tearing itself down.
    """
    return list(_SINKS)


async def push(session_id: str, frame: dict) -> None:
    """Draw one frame on every screen this session has, shrugging at the dead.

    Every screen and not the newest, which is the whole of #138: a kiosk in the
    kitchen and a tab on the Mac share one session id, and a card resolved
    through :func:`sink` reached whichever of them connected last while the
    other showed nothing and said nothing about showing nothing. Two pages that
    both stay up are not the reload :func:`sink` was written for; the duplicate
    a reload draws is one frame on a socket that is closing, and every card
    frame carries the function-call id of the card it belongs to, so a page
    that is handed the same frame twice draws it once.

    A tool answering late may well outlive the socket that asked it — the
    person walked off, the kiosk reloaded — and a closed WebSocket is not a
    reason to fail the tool call. Nor is it a reason to skip the screens behind
    it: every send is isolated in :func:`_deliver`, so one raising leaves the
    rest of the list delivered.

    Handed to a writer per screen rather than awaited, which is the rest of
    #138. Concurrent sends fixed the first frame and nothing after it: `push`
    still returned only when the slowest page had taken the frame, and the
    caller that draws a card draws the next frame after that — so a `browse`
    with a suspended laptop attached advanced its screenshots at the laptop's
    pace on every screen, unbounded if the laptop never woke. The issue asks for
    the other thing: a page that will not take a frame inside a bound is
    dropped rather than waited on. Nothing here waits on a socket at all now, so
    no page can set another page's pace, and :data:`DEADLINE_S` in
    :func:`_writing` is the bound that ends the page that stalled.

    In order per screen and never sampled: one writer at a time drains the
    queue front to back, so `plan`'s steps arrive as steps and `browse`'s `done`
    still arrives last. Nothing on this path is safe to drop on its own — a
    progress note is a line of its own, and the `done` frame is the only thing
    that stops a browse card spinning — so a page that falls behind loses the
    whole connection and not a frame out of the middle of it.

    What `push` returning no longer means is that anything reached a socket. It
    never quite did: the send it awaited handed the frame to a transport buffer,
    not to a browser. Now it does not await even that.

    Over a copy, because a send can end with its own connection tearing itself
    down, and that mutates the list this is walking.
    """
    for held in list(_SINKS.get(session_id, ())):
        _hand(session_id, held, frame)
    # One turn of the loop, so that a screen whose send completes without
    # waiting on anything has been written to by the time this returns. Not a
    # guarantee and not relied on for anything: a real socket does wait, and a
    # caller that needs to know a frame landed cannot learn it here.
    await asyncio.sleep(0)


def _hand(session_id: str, held: _Screen, frame: dict) -> None:
    """Put one frame in one screen's queue and leave a writer draining it.

    The whole of what it costs to draw on a page: no socket is awaited here,
    so the caller's latency does not depend on the page's. Both callers go
    through it — :func:`push`, which hands to every screen on the session, and
    :func:`push_wake`, which hands to the one :func:`_wake_screen` picked — so
    the bound in :func:`_writing` and the drop in :func:`_drop` cover the wake
    word exactly as they cover a card, and there is one place where "queued,
    then a writer" is written down.
    """
    held.queue.put_nowait(frame)
    _wake_writer(session_id, held)


def _wake_writer(session_id: str, held: _Screen) -> None:
    """Make sure something is draining this screen's queue.

    Started on demand and left to finish, rather than one task per connection
    for the life of the connection: the queue is empty almost always, and a
    parked task per page is a thing to remember to cancel on every path a page
    can leave by. There is no window for two: a writer only ever suspends inside
    a send, and between `put_nowait` above and this line nothing else runs, so
    the writer is either still going — and will see the queue is not empty — or
    finished, and this starts the next one.
    """
    if held.writer is None or held.writer.done():
        held.writer = asyncio.create_task(_writing(session_id, held))


async def _writing(session_id: str, held: _Screen) -> None:
    """Drain one screen's queue, and end the screen if it will not drain.

    :data:`DEADLINE_S` per frame, not per queue: a page working through a
    backlog is a page that is reading, and the thing being watched for is one
    that has stopped. Over the deadline the send is cancelled where it stands —
    the frame it was part-way through writing is lost, which is why the socket
    is closed after it rather than left half-written.

    A send that raises is not a page that has stopped; it is the socket that has
    already gone, which every caller here is entitled to shrug at. Swallowed in
    :func:`_deliver` as before, and the queue keeps draining, because the frames
    behind it belong to the same dead socket and cost nothing to write to it.
    """
    while not held.queue.empty():
        frame = held.queue.get_nowait()
        try:
            await asyncio.wait_for(_deliver(session_id, held.send, frame),
                                   DEADLINE_S)
        except asyncio.TimeoutError:
            _drop(session_id, held)
            return


def _drop(session_id: str, held: _Screen) -> None:
    """Take a screen that has stopped reading off the session, and close it.

    Off the session first, so that the next frame is not queued behind the one
    that timed out.

    Closed, and not merely forgotten, because a page nobody draws on is the
    #138 bug wearing a different hat: it sits there showing the last card it
    got, with no way to notice that it is now the only thing it will ever show.
    A close it can see is the one thing that gets it back — `app.js` reconnects
    on `onclose` with backoff (`RECONNECT_MIN_MS`), and the reconnect registers
    a screen again. A laptop that was asleep therefore finds its panel live
    again a second after it wakes, which is the outcome to want and the reason
    not to leave the socket up.

    Detached, because closing a socket that would not take a frame may not take
    a close either, and this is called from the writer of one page on a session
    that has others. Held in a set until it finishes: a bare `create_task` is
    collectable while it is still running.
    """
    unregister(session_id, held.send)
    logger.warning(
        "dropped a screen on %s: it did not take a frame in %ss",
        session_id, DEADLINE_S)
    if held.close is None:
        return
    task = asyncio.create_task(_closing(held.close))
    _CLOSING.add(task)
    task.add_done_callback(_CLOSING.discard)


async def _closing(close: Closer) -> None:
    """Close one dropped page's socket, shrugging at a close that cannot."""
    try:
        await close()
    except Exception:  # pylint: disable=broad-except
        logger.debug("a dropped screen would not close either", exc_info=True)


async def push_wake(session_id: str, frame: dict) -> bool:
    """Draw one frame on the screen a wake word should reach.

    :func:`push` with :func:`_wake_screen` in place of the whole stack, and an
    answer where that has none: `/api/wake` reports how many screens it
    reached, and the only way to count screens rather than sessions is to be
    told which sessions had one to hand the frame to.

    True means handed over, not delivered. A socket that raised on the way out
    had a page behind it when the frame was written, and the monitor on the Pi
    can do nothing about a browser that closed in between.

    Handed to the screen's writer and not awaited, which is #138's shape on
    #138's other path. This used to `await` the send itself, and `/api/wake`
    walks every session in turn — so one page that had stopped reading held
    the wake frame for every *other* session on the box, and a laptop that
    never woke held it forever. That is worse here than it was for a card:
    `push` at least reached the pages behind the sleeping one on the same
    session, and the wake word is what the person in the kitchen is standing
    there waiting for. `wake.py`'s webhook gives up after `WEBHOOK_TIMEOUT`
    (2 s), so the monitor logs a failure and the microphone opens later or
    not at all.

    Through the same queue as the cards, and not a lane of its own. A page
    reads its socket in order and the frames are all going to the same page:
    a wake frame that overtook a `browse` step would arrive on a page whose
    card is still spinning, and the queue is empty almost always anyway. What
    the shared queue buys is :data:`DEADLINE_S` — a page that will not take the
    wake frame inside the bound is now dropped and closed like any other, so
    the next word finds either a page that reads or no page at all, rather
    than the same wedged socket every time.
    """
    held = _wake_screen(session_id)
    if held is None:
        return False
    _hand(session_id, held, frame)
    # The same turn of the loop :func:`push` gives, and for the same reason:
    # a check that pushes a wake frame and then asks what the page heard.
    await asyncio.sleep(0)
    return True


async def _deliver(session_id: str, send: Sink, frame: dict) -> None:
    """Hand one frame to one sink, swallowing a socket that has gone."""
    try:
        await send(frame)
    except Exception:  # pylint: disable=broad-except
        logger.debug("dropped a screen frame for %s", session_id, exc_info=True)
