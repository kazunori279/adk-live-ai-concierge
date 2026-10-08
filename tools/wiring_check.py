"""Check the lines that connect a working feature to the running program.

Every other script here drives a feature and asks whether it does the right
thing. This one asks whether anything calls it. The distinction is not
academic: #55 deleted 23 wiring lines one at a time against a green suite and
13 of them changed nothing that any check could see, because the checks reach
each feature by calling it directly and the server is the only caller of the
line that was cut.

They fail in the same way, which is the reason for a script of their own — the
program keeps running, the log stays clean, and the screen shows something
plausible:

- **The endpoint's four registrations.** `screen.register` is what a card is
  drawn through and `open_session` is what a tool's in-flight work is tracked
  in. Delete the first and every card and every progress note is dropped by
  `screen.push`, which is written to shrug at a screen that has gone away and
  cannot tell that from one that was never there.
- **The two dictionary lines inside `gws`.** `asking.discard` at the end of a
  question is what makes the *next* identical question answerable; without it
  "what's on today?" works once per session and is silently refused after.
  `running.add` is what a socket closing has to cancel.
- **The per-question Drive memo.** `gws_cache.remembering()` is opened on one
  line, on the only path that reaches it. `cache_check` covers everything the
  memo does and nothing covers whether it is switched on.
- **The calendar on its way to the card.** `_agenda.showing()` is opened on the
  same line and the frame reads it two dozen lines later. `agenda_check` covers
  what the dict collects; nothing else covers whether the frame carries it, and
  the way that fails is a calendar answer that draws the paragraph it always
  drew — a screen nobody reports as broken because it is what it used to be.
- **The per-question source ledger, and the one line that fills it.**
  `_sources.collecting()` is opened beside the memo and `_sources.record()` is
  called once, inside the worker thread the CLI runs on. Cut the first and
  `collected` is handed the `None` the variable was initialised to, so every
  card loses its source row and says nothing about having lost it; cut the
  second and the ledger stays empty, which the card renders as "could not say
  which file this came from" under an answer that came out of a named file.
  `sources_check` covers what the ledger decides and neither line is reachable
  from it.
- **Which wait each tool declares.** One attribute assignment per tool. Unset,
  ADK treats the call as BLOCKING and the conversation freezes for the length
  of a run — the whole of what #23 was about, undone by deleting a line that
  reads like a default.
- **Which entry each teardown is handed.** Session ids outlive a reload since
  #19, so two connections briefly share one, and all four registries refuse to
  clear an entry that is not the caller's. They can only refuse what they are
  given: pass an id where a token belongs and every one of them goes back to
  clearing whatever it finds, which costs the connection that has just arrived
  its screen.
- **The household in `/api/config`.** `PROFILES` is read on one line and
  reaches the page on one more. Drop either and the picker has nobody in it,
  which `showProfiles` renders as a settings sheet with no picker at all — a
  terminal that answers as `terminal` for the whole family and looks right
  doing it.
- **The voice on the handshake.** One argument at one call site. The Live API
  takes the voice in its setup message and nowhere else, so `_run_config` is
  the last place it can be observed before it becomes one — and with the
  argument dropped every case about the picker, the query string and the
  whitelist still holds, over a settings sheet that changes nothing anyone can
  hear.
- **The one path the wake word has to the page.** The monitor posts to
  `/api/wake`, which pushes a frame down a screen registered by a WebSocket
  that is holding no Live session at that moment. Every link in that is a
  single line, and the failure is a terminal that hears the phrase, logs the
  detection on the Pi, and does nothing at all.
- **The revision in `/api/config`.** Read on one line at import and shipped on
  one more. Drop either and the payload is the three keys it was before, which
  is a page that cannot say what it is talking to — the state #73 measured,
  where the Pi held three revisions at once and no way to name any of them.
- **Who is allowed to open the socket.** One comparison of two headers, above
  `accept()`. The content-type guard on `/api/forget` cannot reach this route:
  a WebSocket handshake is outside the same-origin policy and is never
  preflighted, so any page on the network could open this one, write an address
  into the household's conversation and put words in the person's mouth — which
  the agent answers with `gws` in hand. Delete the comparison and all 52 cases
  that came before it still pass, because every one of them opens the socket
  from our own page. See #90.
- **Which names the server answers to.** One `add_middleware` line, and it is
  the half the comparison above cannot do: a page under a name the attacker has
  just repointed at this address sends that name as its `Origin` *and* as its
  `Host`, the two agree, and the comparison says yes. Registered rather than
  called, so the check is that it is in front of the router at all — inside the
  endpoint it would run after the comparison it exists to back up, which is a
  non-fix that passes every case about the comparison. See #95.
- **The build in the corner of the page.** Two lines: the commit date resolved
  at import beside that SHA, and `root()` writing both into the document rather
  than handing back the file. The second is the one worth a case. Drop it and
  the obvious repair is a badge that fetches `/api/config`, which names the
  server answering now and not the build the page is running — per #256's
  measurement, so the badge would have been wrong in exactly the situation it
  exists to catch. See #261 and #256.

No model is called and no socket is opened. `_ask` is a spy, `run_live` yields
nothing, and the WebSocket is a list of messages ending in a disconnect. The
revision cases do run `git`, in a two-project repo built under `tempfile`
rather than borrowed from the checkout this file is running in: that one's HEAD
moves while the suite does, and it moved twice on 2026-09-09.

    uv run python tools/wiring_check.py

Exits non-zero on the first case that does not hold.
"""

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types as types_mod
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("ENABLE_MANAGEMENT_ROUTES", "1")

from google.adk.events import Event, EventActions  # noqa: E402
from google.adk.tools import FunctionTool  # noqa: E402
from google.genai import types  # noqa: E402
from starlette.datastructures import Headers  # noqa: E402

from ai_terminal.live import agent, browser, conn, recap, screen  # noqa: E402
from ai_terminal.live.tools import _agenda, _client, _gws_cli, _lang  # noqa: E402
from ai_terminal.live.tools import _gws_run, _sources  # noqa: E402
from ai_terminal.live.tools import browse, gws, gws_cache  # noqa: E402
from ai_terminal.live.tools import image_search, maps, place, weather, web_search, youtube_search  # noqa: E402
from ai_terminal.live.tools import plan as plan_mod  # noqa: E402

_GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}


# ---------------------------------------------------------------- offline guard
#
# No case in this suite may make live model calls. `genai_client` and
# `global_client` are stubbed out to raise AssertionError at module level
# so an un-stubbed roll or card fails loudly rather than making real,
# billed Vertex AI / Gemini API calls (#287).


CALLED_GENAI: list[str] = []


def _offline_genai_client(*args, **kwargs):
    CALLED_GENAI.append("genai_client")
    raise AssertionError(
        "wiring_check must not make live model calls: genai_client was called (#287)"
    )


_client.genai_client = _offline_genai_client
_client.global_client = _offline_genai_client
_client.grounding_client = _offline_genai_client
recap.genai_client = _offline_genai_client
recap.grounding_client = _offline_genai_client
maps.genai_client = _offline_genai_client
maps.grounding_client = _offline_genai_client
weather.genai_client = _offline_genai_client
weather.grounding_client = _offline_genai_client
gws_cache.genai_client = _offline_genai_client
gws_cache.grounding_client = _offline_genai_client

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

    def __init__(self, session_id: str = "live-1", call_id: str = "call-1",
                 state: dict | None = None):
        self.session = type("S", (), {"id": session_id, "events": []})()
        self.function_call_id = call_id
        self._invocation_context = type(
            "I", (), {"live_request_queue": None})()
        # `plan` reads and writes the scratchpad through it — see
        # `_plan_run.held` and `_plan_run.keep`. What the handshake puts in it
        # is here too, which is how a tool sees the panel's language and the
        # terminal's own location (`_lang.panel_lang`, `maps.here`).
        self.state: dict = dict(state or {})


# --------------------------------------------------------------- the endpoint

# What this terminal is called, as far as the cases below are concerned. Any
# name would do for the origin guard, which compares two headers and is told
# none — the cases that drive the app put this one in `ALLOWED_HOSTS` so that
# the host guard in front of the router is not what they end up measuring.
OUR_HOST = "terminal.local:8000"

# The one frame the accept path sends before the page has said anything: the
# server telling `app.js` that `_thread` and the four registry opens all came
# back, which is what its reconnect backoff resets on. Named here because two
# things in this file have to look past it — a case counting what a *turn*
# produced, and the loop below that waits for a turn to produce anything.
READY = json.dumps({"type": "ready"})


class FakeSocket:
    """A WebSocket as far as `websocket_endpoint` reads one.

    `receive` works through a script and then disconnects, which is what ends
    the endpoint's `upstream` loop — a socket that only ever blocks would hang
    this file instead of failing it. Each step is an async callable so a case
    can run inside the connection, which is the only place the registrations
    it is about are visible.

    The headers default to a handshake from our own page, because that is what
    every case using this class is about; who else may open one is driven
    through the real ASGI app further down, where an accept can be told from a
    refusal. A `Headers` rather than a dict, because the real one is
    case-insensitive and a guard that only works on lowercase keys would look
    fine here and refuse the panel.
    """

    def __init__(self, script, **params):
        self.sent: list[str] = []
        self.query_params = params
        self.headers = Headers({"host": OUR_HOST,
                                "origin": f"http://{OUR_HOST}"})
        self.accepted = False
        self.closed: list[tuple[int, str]] = []
        self._script = list(script)

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))

    async def receive(self) -> dict:
        if not self._script:
            return {"type": "websocket.disconnect"}
        return await self._script.pop(0)()

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


class FakeRunner:
    """A Runner whose live stream is whatever it was handed, then ends.

    Empty for most of this file, which only needs to know that `run_live` was
    reached and with what. One event where a case has to see that an accepted
    connection still carries the model's turn the whole way down the socket —
    a guard that refused everybody would otherwise pass every refusal case.
    """

    def __init__(self, events=()):
        self.calls: list[dict] = []
        self._events = list(events)

    async def run_live(self, **kwargs):
        self.calls.append(kwargs)
        for event in self._events:
            yield event


@contextlib.contextmanager
def a_server_of_its_own(main, events=()):
    """`main`, with a throwaway model and a sessions file to match.

    The household's own conversations live in `sessions.db` in the working
    directory, and every connection driven here writes a session to whatever
    service is installed. Somewhere disposable, then.
    """
    runner, service = main.runner, main.session_service
    with tempfile.TemporaryDirectory() as tmp:
        main.runner = FakeRunner(events)
        main.session_service = type(service)(
            db_path=str(Path(tmp) / "sessions.db"))
        try:
            yield
        finally:
            main.runner, main.session_service = runner, service


@contextlib.contextmanager
def stub_carry(main, recorder: list | None = None):
    """Stand in for `_carry_forward` during rolls, so rolls never reach the model.

    Recorded and not called. `recap.summarise` is a real model call, and an
    offline check suite driving rolls must not make live calls to Vertex AI /
    Gemini (#287).
    """
    carried = recorder if recorder is not None else []
    was_carry = main._carry_forward

    async def spy_carry(*args, **kwargs) -> None:
        carried.append(args)

    main._carry_forward = spy_carry
    try:
        yield carried
    finally:
        main._carry_forward = was_carry


async def endpoint_cases() -> None:
    print("== what one WebSocket connection wires up ==")

    # Imported here and not at the top, because importing `main` builds the
    # live agent and reads `.env`, and a script that only wanted the three
    # scheduling cases below should not pay for that to find out one of them
    # failed.
    from ai_terminal.live import main

    # `main` calls `logging.basicConfig(INFO)` at import, which is right for a
    # server and turns this file into a page of connection logs. Every other
    # script here installs no handler at all and so prints from WARNING up;
    # this puts the suite back in step rather than going quieter than it.
    logging.getLogger().setLevel(logging.WARNING)

    inside: dict = {}
    # The id the server picked for this hello. The browser used to send one, so
    # this file used to pass it in; now the only way to know which id the four
    # registrations are keyed under is to ask the thing that chose it.
    chosen: list[str] = []

    async def during() -> dict:
        """One turn of the connection, from inside it."""
        session_id = chosen[0]
        # Everything the server said before the page had said anything. The
        # first `receive` is this callable, so whatever is here arrived on the
        # accept path and nothing else has.
        inside["hello"] = list(socket.sent)
        # The frame a tool answering late would push. It has to travel the
        # whole way — `screen.push` finds the sink, `to_screen` encodes it,
        # `send` takes the lock and writes it — because every one of those is
        # set up by the line under test and none of them raises when it is not.
        await screen.push(session_id, {"type": "gws", "id": "c-1"})
        inside["screen"] = screen.sink(session_id) is not None
        inside["browse"] = session_id in browse._JOBS
        inside["gws"] = session_id in gws._RUNNING
        inside["plan"] = session_id in plan_mod._RUNNING
        # And which of them a tool call running here would reach. The three
        # registries are keyed by connection since #133, so the handler has to
        # name this one — `conn.arrive()` — or every lookup falls through to
        # the newest entry on the session, which on a profile with two pages
        # open is the other page's. Measured against an entry deliberately
        # opened after this one, because "the newest" and "mine" are the same
        # answer while there is only one.
        mine = browse.jobs_of(session_id)
        later = browse.open_session(session_id)
        inside["named"] = conn.current() is not None
        inside["own"] = browse.jobs_of(session_id) is mine
        browse.close_session(session_id, later)
        return {"type": "websocket.disconnect"}

    real_thread = main._thread
    real_register = screen.register

    async def note_thread(user_id, state):
        # What had been sent when the conversation was opened, which is the
        # first of the two unguarded calls the `ready` frame has to come after.
        inside["at_thread"] = list(socket.sent)
        chosen.append(await real_thread(user_id, state))
        return chosen[-1]

    def note_register(*args, **kwargs):
        # And the last of them. `screen.register` is the fourth registry open,
        # so a frame sent before this one is a page told the server is healthy
        # while the calls that fail have not been made.
        inside["at_register"] = list(socket.sent)
        # And what it was handed to close this socket with, which nothing else
        # in this file can see: `screen` calls it and the endpoint writes it,
        # and in between there is one argument that can be dropped without a
        # line of output changing.
        inside["closer"] = (kwargs.get("close")
                            or (args[3] if len(args) > 3 else None))
        return real_register(*args, **kwargs)

    socket = FakeSocket([during], location="Tokyo")
    main._thread = note_thread
    screen.register = note_register
    try:
        with a_server_of_its_own(main):
            await main.websocket_endpoint(socket, "user-1")
    finally:
        main._thread = real_thread
        screen.register = real_register

    check("the socket is accepted before anything is sent down it",
          socket.accepted, True)
    # The one frame a connection carries before the page has said a word, and
    # the reason the reconnect backoff in `app.js` can tell a server that came
    # back from one that answered a handshake and fell over. It has to come
    # last on the accept path: `_thread` and the four registry opens are all
    # unguarded, and a `ready` in front of them credits the server for work it
    # has not done. Read as three snapshots of the same list rather than as a
    # line number, because where the send is written is not where it runs.
    check("the page is told the accept path came back, and only then",
          [inside.get("at_thread"), inside.get("at_register"),
           inside.get("hello")],
          [[], [], [READY]],
          "nothing at `_thread`, nothing at `screen.register`, one frame "
          "after")
    check("a frame pushed mid-conversation reaches the browser as JSON",
          [text for text in socket.sent if text != READY],
          [json.dumps({"type": "gws", "id": "c-1"})],
          detail=f"{len(socket.sent)} frame(s)")
    check("and each of the three tools has a session open to track work in",
          {k: inside[k] for k in ("screen", "browse", "gws", "plan")},
          {"screen": True, "browse": True, "gws": True, "plan": True})
    check("the connection names itself, so a tool call running under it finds "
          "its own jobs rather than the newest page's",
          [inside.get("named"), inside.get("own")], [True, True],
          "`conn.arrive()` in the handler is the only thing carrying it")
    # The other end of the same four lines. A session left open holds every
    # task it started, and `gws` reads a stale `_ASKING` entry as a question
    # already in flight — so the next connection with the same id is refused
    # its first repeat.
    check("all four are let go when the socket closes",
          [screen.sink(chosen[0]), chosen[0] in browse._JOBS,
           chosen[0] in gws._RUNNING, chosen[0] in plan_mod._RUNNING],
          [None, False, False, False])

    # The fifth thing that line carries, and the one with nothing else
    # watching it: what to do to this page when it stops taking frames.
    # `screen` drops such a page off the session on its own, and dropping it
    # without closing it leaves a tab that is still connected, still showing
    # the last card it got and with no way to find out it will never get
    # another — which is the shape of #138 all over again. Called here rather
    # than described, because an argument that is passed and does the wrong
    # thing reads the same as one that is passed and does the right thing.
    closer = inside.get("closer")
    before = len(socket.closed)
    if callable(closer):
        await closer()
    check("and the page is given a way to be told it has been dropped",
          [callable(closer), socket.closed[before:]],
          [True, [(1011, "")]],
          "drop the fourth argument to `screen.register` and a stalled tab "
          "is silently deaf instead of reconnecting")

    # And when the hello itself is what fails. A page that went away between
    # the accept and the send makes `send_text` raise, and the four
    # registrations have to be let go on that path too — which they are only
    # because the send sits inside the endpoint's `try`. Written one line
    # earlier, beside `screen.register`, the exception would leave this
    # connection's screen sink taking every later `/api/wake` for the profile
    # and its browse session holding jobs nobody will collect.
    class GoneSocket(FakeSocket):
        """A page that closed the tab while the accept path was running."""

        async def send_text(self, text: str) -> None:
            raise RuntimeError("the page went away")

    left: list[str] = []

    async def note_gone(user_id, state):
        left.append(await real_thread(user_id, state))
        return left[-1]

    main._thread = note_gone
    try:
        with a_server_of_its_own(main):
            with contextlib.suppress(RuntimeError):
                await main.websocket_endpoint(GoneSocket([]), "user-gone")
    finally:
        main._thread = real_thread

    check("and so are they when the hello is what the page was not there for",
          [bool(left), screen.sink(left[0]) if left else "no session",
           left[0] in browse._JOBS if left else None,
           left[0] in gws._RUNNING if left else None,
           left[0] in plan_mod._RUNNING if left else None],
          [True, None, False, False, False],
          "the send is inside the `try`, so the `finally` still runs")


async def reload_cases() -> None:
    print("\n== the same four lines, with a reload overlapping them ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    # Which entry each teardown is handed. The four registries do the checking
    # — `history_check` is where that rule is pinned — but they can only check
    # what they are given, and the endpoint passing an id where a token belongs
    # would put every one of them back to clearing whatever it finds.
    arrived = asyncio.Event()
    gone = asyncio.Event()
    after: dict = {}
    ids: list[str] = []

    real_thread = main._thread

    async def note_thread(user_id, state):
        ids.append(await real_thread(user_id, state))
        return ids[-1]

    async def leaving_turn() -> dict:
        # Held open until the new connection has registered over the top of it,
        # which is the order a kiosk reload actually produces.
        await arrived.wait()
        return {"type": "websocket.disconnect"}

    async def arriving_turn() -> dict:
        arrived.set()
        await gone.wait()
        sid = ids[-1]
        after["screen"] = screen.sink(sid) is not None
        after["browse"] = sid in browse._JOBS
        after["gws"] = sid in gws._RUNNING
        after["plan"] = sid in plan_mod._RUNNING
        return {"type": "websocket.disconnect"}

    main._thread = note_thread
    try:
        with a_server_of_its_own(main):
            old = asyncio.create_task(main.websocket_endpoint(
                FakeSocket([leaving_turn]), "user-1"))
            # Wait for the first connection to be all the way up before opening
            # the second: started any earlier, this races the thing it is about
            # instead of reproducing it.
            for _ in range(1000):
                if ids and screen.sink(ids[0]) is not None:
                    break
                await asyncio.sleep(0.001)
            new = asyncio.create_task(main.websocket_endpoint(
                FakeSocket([arriving_turn]), "user-1"))
            # Bounded, because every wait above is on something another task has
            # to do: a regression here should fail the suite, not hang it.
            await asyncio.wait_for(old, 10)
            gone.set()
            await asyncio.wait_for(new, 10)
    finally:
        main._thread = real_thread

    check("a reload lands back in the conversation it left",
          len(set(ids)), 1, detail=(ids[0] if ids else "nothing opened"))
    check("and the connection that went took none of the newcomer's wiring",
          after, {"screen": True, "browse": True, "gws": True, "plan": True},
          "each teardown is handed the entry that connection opened")


# ---------------------------------- a roll, and the other page it has to carry

async def roll_fanout_cases() -> None:
    """What happens to the *other* page when one of them rolls the thread.

    Two pages on one profile hold one session id, indefinitely — `_thread`
    hands the second one the conversation the first is already in. `rethread`
    then rolls that id out from under both of them on the first wake word past
    `IDLE_GAP_S`, and used to re-key only the four registrations belonging to
    the connection that armed. The other page stayed keyed to the thread that
    had just been rolled away: still connected, still drawn on by nothing.

    The visible half is the wake word. `/api/wake` tells one screen per session
    (`screen.wake_sink`, #132), so a roll that turns one profile's one thread
    into two turns one microphone into two — and the second one is a browser
    tab in another room.
    """
    print("\n== a roll, and the other page on the conversation ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    wake_frame = json.dumps({"type": "wake"})

    async def two_pages(kiosk_arms: bool = False,
                        kiosk_leaves: bool = False) -> dict:
        """A kiosk and a tab on one profile, with the tab arming past the gap.

        The kiosk connects first, so it is the older registration and the one a
        self-only re-key leaves behind. Only the tab's `arm` makes the thread
        look stale, so both pages are certain to have resolved to one id at
        accept and the roll is certain to be the tab's.

        `kiosk_leaves` closes the kiosk, and waits for its `finally` to have
        run, before the tab arms — the shape that says whether a roll reaches
        for pages that are no longer there.
        """
        ids: list[str] = []
        real_thread, real_gap = main._thread, main.IDLE_GAP_S
        runner = WakeRunner()
        stay = asyncio.Event()
        gone = asyncio.Event()
        proceed = asyncio.Event()
        up = [asyncio.Event(), asyncio.Event()]
        done = asyncio.Event()
        socks: list[FakeSocket] = []
        out: dict = {}

        async def note_thread(user_id, state):
            ids.append(await real_thread(user_id, state))
            return ids[-1]

        def frame(kind: str):
            return {"type": "websocket.receive",
                    "text": json.dumps({"type": kind})}

        def kiosk_script():
            async def do_arm():
                return frame("arm")

            async def settle():
                up[0].set()
                await (gone if kiosk_leaves else stay).wait()
                return {"type": "websocket.disconnect"}

            return ([do_arm] if kiosk_arms else []) + [settle]

        def tab_script():
            async def do_arm():
                up[1].set()
                await proceed.wait()
                main.IDLE_GAP_S = -1.0
                return frame("arm")

            async def settle():
                done.set()
                await stay.wait()
                return {"type": "websocket.disconnect"}

            return [do_arm, settle]

        main._thread = note_thread
        try:
            # Stand in for `_carry_forward` so thread rolls never reach
            # `recap.summarise` (a real model call) during offline checks (#287).
            with a_server_of_its_own(main), stub_carry(main):
                main.runner = runner
                socks.append(FakeSocket(kiosk_script(), kiosk="1"))
                kiosk = asyncio.create_task(
                    main.websocket_endpoint(socks[0], "alex"))
                await asyncio.wait_for(up[0].wait(), 10)
                socks.append(FakeSocket(tab_script()))
                tab = asyncio.create_task(
                    main.websocket_endpoint(socks[1], "alex"))
                await asyncio.wait_for(up[1].wait(), 10)
                if kiosk_leaves:
                    gone.set()
                    await asyncio.wait_for(kiosk, 10)
                proceed.set()
                await asyncio.wait_for(done.wait(), 10)

                live = runner.calls[-1]["session_id"]
                out["opened_on"] = sorted({c["session_id"]
                                           for c in runner.calls})
                out["rolled"] = live != ids[0]
                out["sessions"] = len(screen.sessions())
                out["screens"] = len(screen._SINKS.get(live, []))
                out["left_behind"] = [ids[0] in reg for reg in (
                    screen._SINKS, browse._JOBS, gws._RUNNING,
                    plan_mod._RUNNING)] if out["rolled"] else None
                out["tools"] = [len(reg.get(live, {})) for reg in (
                    browse._JOBS, gws._RUNNING, plan_mod._RUNNING)]
                for sock in socks:
                    sock.sent.clear()
                out["armed"] = sum([
                    await screen.push_wake(sid, {"type": "wake"})
                    for sid in screen.sessions()])
                out["woke"] = [sock.sent.count(wake_frame) for sock in socks]
                stay.set()
                await asyncio.wait_for(tab, 10)
                if not kiosk_leaves:
                    await asyncio.wait_for(kiosk, 10)
        finally:
            main._thread, main.IDLE_GAP_S = real_thread, real_gap
        return out

    quiet = await two_pages(kiosk_arms=False)
    # Green before the fix as well as after, and said out loud because of it:
    # the defect is not that the roll fails to happen, and a fix that simply
    # stopped rolling would satisfy every case below this one.
    check("the roll itself still happens when the gap has passed",
          [quiet["rolled"], len(quiet["opened_on"])], [True, 1])
    check("a roll takes every page on the conversation with it, not only the "
          "page that armed",
          [quiet["sessions"], quiet["screens"]], [1, 2],
          "one profile, one thread, and a screen on it for each page that is "
          "up")
    check("and leaves nothing of the other page on the thread it rolled away",
          quiet["left_behind"], [False, False, False, False],
          "screen, browse, gws, plan")
    check("with the other page's browse, gws and plan entries carried across",
          quiet["tools"], [2, 2, 2],
          "a tool answering late resolves its screen from the session it ran "
          "in, so an entry left on the old id draws nowhere")
    check("and the wake word still opens one microphone for the profile",
          [quiet["armed"], quiet["woke"]], [1, [1, 0]],
          "two threads for one profile is two screens woken, and the second "
          "is a tab in a room nobody is in")
    # Green before the fix as well, and it could not be otherwise: there is no
    # book of pages on the unfixed tree for a closed page to be left in. It is
    # here because the fix's own way of going wrong is to keep one — a roll
    # that re-registers a socket the browser has closed puts a dead screen at
    # the top of `wake_sink`'s preference, and the wake word then opens a
    # microphone on nothing while the page that is up hears no reply.
    left = await two_pages(kiosk_leaves=True)
    check("a page that closed before the roll is not carried across it",
          [left["screens"], left["armed"], left["woke"]], [1, 1, [0, 1]],
          "one screen on the new thread, and the wake word reaching the page "
          "that is still up")

    busy = await two_pages(kiosk_arms=True)
    check("a conversation another page is live on is not rolled out from "
          "under it",
          [len(busy["opened_on"]), busy["sessions"]], [1, 1],
          "the gap is measured on the session row, and a page that armed and "
          "then said nothing leaves it looking exactly as quiet as an empty "
          "kitchen does")


# ------------------------------ and the other page rolling at the same moment

async def roll_race_cases() -> None:
    """Both pages waking at once, one of them mid-roll when the other rolls.

    The guard above — "not while somebody else on this thread is live" — is a
    check, and the act it guards is two awaits later: `create_session` and the
    recap clear both suspend, and `session_id` is a closure local that the
    *other* connection's roll writes to through `_Page.move`. So the thread
    rolled is not always the thread that was checked. Both pages arming at once
    is the ordinary case rather than a contrived one: `/api/wake` opens one
    microphone, the microphone button on a tab opens another, and after a night
    of quiet both `rethread` calls find the same stale row.

    What goes wrong is invisible from every other case in this file, because
    every registry ends up consistent. `run_live` is the one thing that does
    not move: `downstream` is handed the thread it was started on, deliberately
    (`main.py`, `downstream`), so a roll that lands after another page went
    live leaves that page streaming into a conversation with no screen on it.
    Nothing draws, `/api/wake` finds nobody, and the log says the roll worked.

    Two shapes, and they are the same check-then-act down two branches of
    `quiet`. The first is the ordinary stale row. The second is the row that
    was swept out from under the socket, where `quiet is None` turns the guard
    off by design — re-reading the guard after the awaits fixes the first and
    not the second, which is why this is a lock and not a re-read.

    No `IDLE_GAP_S` override here, unlike the section above. The row this
    connection accepted on is backdated instead, so a thread created *inside*
    the window is correctly fresh and a second roll of a brand-new conversation
    would be the bug rather than the setup.
    """
    print("\n== two pages rolling one conversation at the same moment ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    async def both_arm(swept: bool = False) -> dict:
        """The kiosk's whole arm, run inside the tab's `create_session`.

        The tab arms first and is held at its `create_session`; the kiosk's arm
        frame is released into that suspension and the tab is let go once the
        kiosk has gone as far as it is going to get. "As far as it gets" is
        read rather than waited out — `run_live` where nothing serialises the
        two rolls, and a place in the queue for `user_id`'s roll where
        something does — so there is no sleep in here for a loaded machine to
        make a liar out of.

        `swept` hides the accept thread from the tab's `list_sessions` and from
        that one call only, which is what the retention sweep in `_thread`
        looks like from inside `rethread`: `mine` is None and `quiet` with it.
        """
        real_thread = main._thread
        ids: list[str] = []
        runner = WakeRunner()
        out: dict = {}
        tab_held = asyncio.Event()
        let_go = asyncio.Event()
        stay = asyncio.Event()
        up = [asyncio.Event(), asyncio.Event()]
        tab_done = asyncio.Event()

        async def note_thread(user_id, state):
            ids.append(await real_thread(user_id, state))
            return ids[-1]

        def frame(kind: str):
            return {"type": "websocket.receive",
                    "text": json.dumps({"type": kind})}

        main._thread = note_thread
        try:
            # Stand in for `_carry_forward` so thread rolls never reach
            # `recap.summarise` (a real model call) during offline checks (#287).
            with a_server_of_its_own(main), stub_carry(main):
                main.runner = runner
                service = main.session_service
                real_create = service.create_session
                real_list = service.list_sessions
                gate = {"hold": False, "old": False, "hide": False}

                async def held_create(**kwargs):
                    if gate["hold"]:
                        gate["hold"] = False
                        tab_held.set()
                        await asyncio.wait_for(let_go.wait(), 10)
                    return await real_create(**kwargs)

                async def aged_list(**kwargs):
                    listed = await real_list(**kwargs)
                    if gate["hide"]:
                        gate["hide"] = False
                        listed.sessions = [row for row in listed.sessions
                                           if row.id != ids[0]]
                        return listed
                    if gate["old"]:
                        for row in listed.sessions:
                            if row.id == ids[0]:
                                row.last_update_time -= 2 * main.IDLE_GAP_S
                    return listed

                service.create_session = held_create
                service.list_sessions = aged_list

                async def kiosk_committed() -> str:
                    """Where the kiosk's arm got to while the tab was held.

                    Held *and* queued behind, not merely counted: a lock
                    nobody acquires still counts its callers, and a case that
                    read the count alone would call that serialised. Bounded
                    rather than open-ended so that a shape which is neither —
                    a lock filed under something other than the person, say —
                    fails as a value instead of as a timeout in a traceback.
                    """
                    for _ in range(2000):
                        if runner.calls:
                            return "live"
                        roll = main._ROLLS.get("alex")
                        if (roll is not None and roll.waiting > 1
                                and roll.lock.locked()):
                            return "queued"
                        await asyncio.sleep(0.001)
                    return "neither"

                def kiosk_script():
                    async def wait_then_arm():
                        up[0].set()
                        await asyncio.wait_for(tab_held.wait(), 10)
                        return frame("arm")

                    async def settle():
                        await stay.wait()
                        return {"type": "websocket.disconnect"}

                    return [wait_then_arm, settle]

                def tab_script():
                    async def do_arm():
                        up[1].set()
                        gate["old"] = True
                        gate["hide"] = swept
                        gate["hold"] = True
                        return frame("arm")

                    async def settle():
                        tab_done.set()
                        await stay.wait()
                        return {"type": "websocket.disconnect"}

                    return [do_arm, settle]

                socks = [FakeSocket(kiosk_script(), kiosk="1"),
                         FakeSocket(tab_script())]
                kiosk = asyncio.create_task(
                    main.websocket_endpoint(socks[0], "alex"))
                await asyncio.wait_for(up[0].wait(), 10)
                tab = asyncio.create_task(
                    main.websocket_endpoint(socks[1], "alex"))
                await asyncio.wait_for(up[1].wait(), 10)
                await asyncio.wait_for(tab_held.wait(), 10)
                out["kiosk_got_to"] = await asyncio.wait_for(
                    kiosk_committed(), 10)
                out["rolling"] = sorted(main._ROLLS)
                let_go.set()
                await asyncio.wait_for(tab_done.wait(), 10)
                for _ in range(500):
                    if len(runner.calls) >= 2:
                        break
                    await asyncio.sleep(0.001)

                out["armed"] = len(runner.calls)
                out["live_threads"] = len(
                    {call["session_id"] for call in runner.calls})
                # The whole of it: a page holding a Live session still has its
                # screen on the thread `run_live` was handed.
                out["screens"] = [
                    len(screen._SINKS.get(call["session_id"], []))
                    for call in runner.calls]
                out["sessions"] = len(screen.sessions())
                # Rows in the store, which is not the same question as
                # `screen.sessions()` — that one counts the threads a screen
                # is on, so a thread nobody is attached to is invisible to it.
                # This is the case the three above stopped covering when #142
                # moved `rolled = session_id` above the awaits in `roll`: the
                # loser of the race then fans out over a key every page has
                # already left, so it drags nobody, and the whole visible cost
                # of losing becomes a session it created and abandoned. That
                # row carries the newest `last_update_time` for the profile,
                # which is exactly what `_thread` hands the next page to
                # connect — so the empty thread wins the next page load.
                out["rows"] = len((await real_list(
                    app_name=main.APP_NAME, user_id="alex")).sessions)
                stay.set()
                await asyncio.wait_for(tab, 10)
                await asyncio.wait_for(kiosk, 10)
        finally:
            main._thread = real_thread
        out["rolls_left"] = sorted(main._ROLLS)
        out["pages_left"] = sorted(main._PAGES)
        return out

    both = await both_arm()
    check("a page mid-roll waits for the roll already running on the profile",
          [both["kiosk_got_to"], both["rolling"]], ["queued", ["alex"]],
          "the second arm is behind the first's `rethread`, not inside it")
    check("a roll that lost the race does not take the screens off the "
          "conversation the other page is streaming into",
          [both["armed"], both["screens"]], [2, [2, 2]],
          "`downstream` is handed the thread it was started on, so a roll "
          "landing after another page went live leaves that page attached, "
          "healthy and drawn on by nothing")
    check("and one profile is still one conversation after both of them woke",
          [both["live_threads"], both["sessions"]], [1, 1])
    check("and the roll that lost leaves no thread behind it either",
          both["rows"], 2,
          "the accept thread and the one roll; a third is a session created "
          "and abandoned, and it is the newest row `_thread` will offer the "
          "next page that connects")

    # The same check-then-act down the other branch of `quiet`. Its own case
    # because re-reading the guard after the awaits — the smaller fix — passes
    # the three above and fails this one: `quiet is None` switches the guard
    # off by design, so a re-read has nothing to re-read.
    gone = await both_arm(swept=True)
    check("including when the row it measured was swept out from under it",
          [gone["armed"], gone["screens"], gone["live_threads"]],
          [2, [2, 2], 1],
          "`quiet is None` turns the live guard off, so the stale "
          "`session_id` is the only thing left holding this together")

    check("and the roll lock is filed under the person and dropped with them",
          [gone["rolls_left"], gone["pages_left"]], [[], []],
          "`user_id` is a path segment; a registry that only ever grew would "
          "be one more thing a page could add to")


# --------------------------- and the fact that decides it, set after the lock

async def arm_lock_cases() -> None:
    """One page arming while another is one await short of being live.

    The lock above serialises the rolls, and every fact `rethread` reads is
    read inside it — except the one the branch actually turns on.
    `held.live()` reads `live_task`, and `arm` assigns that two lines *after*
    `rethread` has returned and dropped the lock, with an `await _settle` in
    between. So a page that has committed to a Live session and has not yet
    opened one reads to everybody else as a page that is doing nothing, and
    the roll they are refused for its sake happens anyway.

    The window is the ordinary case rather than a contrived one, for the same
    reason as the section above: `/api/wake` opens one microphone and the
    button on a tab opens another, and after a night of quiet both arms find
    the same stale row.

    Held at `_settle` and not at `create_session`, which is what makes this a
    different case from the one above rather than a second spelling of it.
    Held there because it is where the shipping code already suspends — the
    probe adds no await of its own to the path it is measuring.
    """
    print("\n== a page arming while another is a step short of live ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    async def both_arm() -> dict:
        real_thread = main._thread
        real_settle = main._settle
        ids: list[str] = []
        runner = WakeRunner()
        out: dict = {}
        tab_held = asyncio.Event()
        let_go = asyncio.Event()
        stay = asyncio.Event()
        up = [asyncio.Event(), asyncio.Event()]
        tab_done = asyncio.Event()
        gate = {"hold": False, "old": False}

        async def note_thread(user_id, state):
            ids.append(await real_thread(user_id, state))
            return ids[-1]

        def frame(kind: str):
            return {"type": "websocket.receive",
                    "text": json.dumps({"type": kind})}

        async def held_settle(user_id, session_id):
            # Once, and for the tab only: the kiosk reaches `_settle` too, and
            # a gate that caught it as well would be holding both pages in the
            # window instead of one on each side of it.
            if gate["hold"]:
                gate["hold"] = False
                tab_held.set()
                await asyncio.wait_for(let_go.wait(), 10)
            return await real_settle(user_id, session_id)

        main._thread = note_thread
        main._settle = held_settle
        try:
            # Stand in for `_carry_forward` so thread rolls never reach
            # `recap.summarise` (a real model call) during offline checks (#287).
            with a_server_of_its_own(main), stub_carry(main):
                main.runner = runner
                service = main.session_service
                real_list = service.list_sessions

                async def aged_list(**kwargs):
                    listed = await real_list(**kwargs)
                    # Set once the kiosk is past its own accept, so `_thread`
                    # does not see the gap and hand it a thread of its own —
                    # that is #165, and it would take the two pages off one
                    # conversation before this case had begun.
                    if gate["old"]:
                        for row in listed.sessions:
                            if row.id == ids[0]:
                                row.last_update_time -= 2 * main.IDLE_GAP_S
                    return listed

                service.list_sessions = aged_list

                async def kiosk_committed() -> str:
                    """Where the kiosk's arm got to while the tab was held."""
                    for _ in range(2000):
                        if len(runner.calls) or len((await real_list(
                                app_name=main.APP_NAME,
                                user_id="alex")).sessions) > 1:
                            return "through"
                        roll = main._ROLLS.get("alex")
                        # Two holders, not one: the kiosk taking the lock for
                        # its own `rethread` is not the same fact as the kiosk
                        # waiting behind the tab, and only the second of those
                        # is what this case is about.
                        if (roll is not None and roll.waiting > 1
                                and roll.lock.locked()):
                            return "queued"
                        await asyncio.sleep(0.001)
                    return "neither"

                def kiosk_script():
                    async def wait_then_arm():
                        up[0].set()
                        await asyncio.wait_for(tab_held.wait(), 10)
                        gate["old"] = True
                        return frame("arm")

                    async def settle():
                        await stay.wait()
                        return {"type": "websocket.disconnect"}

                    return [wait_then_arm, settle]

                def tab_script():
                    async def do_arm():
                        up[1].set()
                        gate["hold"] = True
                        return frame("arm")

                    async def settle():
                        tab_done.set()
                        await stay.wait()
                        return {"type": "websocket.disconnect"}

                    return [do_arm, settle]

                socks = [FakeSocket(kiosk_script(), kiosk="1"),
                         FakeSocket(tab_script())]
                kiosk = asyncio.create_task(
                    main.websocket_endpoint(socks[0], "alex"))
                await asyncio.wait_for(up[0].wait(), 10)
                tab = asyncio.create_task(
                    main.websocket_endpoint(socks[1], "alex"))
                await asyncio.wait_for(up[1].wait(), 10)
                await asyncio.wait_for(tab_held.wait(), 10)
                out["kiosk_got_to"] = await asyncio.wait_for(
                    kiosk_committed(), 10)
                let_go.set()
                await asyncio.wait_for(tab_done.wait(), 10)
                for _ in range(500):
                    if len(runner.calls) >= 2:
                        break
                    await asyncio.sleep(0.001)

                out["armed"] = len(runner.calls)
                out["screens"] = [
                    len(screen._SINKS.get(call["session_id"], []))
                    for call in runner.calls]
                out["rows"] = len((await real_list(
                    app_name=main.APP_NAME, user_id="alex")).sessions)
                stay.set()
                await asyncio.wait_for(tab, 10)
                await asyncio.wait_for(kiosk, 10)
        finally:
            main._thread = real_thread
            main._settle = real_settle
        return out

    both = await both_arm()
    check("a page a step short of live is live enough to refuse a roll",
          [both["kiosk_got_to"], both["rows"]], ["queued", 1],
          "the second arm waits for the first to hold a session rather than "
          "rolling the conversation out from under it")
    check("and both pages end up armed on the one conversation anyway",
          [both["armed"], both["screens"]], [2, [2, 2]],
          "refusing the roll is not refusing the microphone")


# ----------------------------- active live connections and session retirement

async def retire_cases() -> None:
    """Active live connections are protected from retirement sweeps.

    `_retire` decides what to drop on `last_update_time` alone if it does
    not check `_PAGES`. An active Live session that has not written any DB
    events recently would be deleted out from under the connected page
    if `SESSION_RETAIN_S` has elapsed (#346).
    """
    print("\n== session retirement and active live connections ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    with a_server_of_its_own(main):
        service = main.session_service
        s_live = await service.create_session(
            app_name=main.APP_NAME, user_id="alex", state={})
        s_idle = await service.create_session(
            app_name=main.APP_NAME, user_id="alex", state={})
        s_empty = await service.create_session(
            app_name=main.APP_NAME, user_id="alex", state={})

        was_retain = main.RETAIN_S
        was_pages = {k: dict(v) for k, v in main._PAGES.items()}
        main.RETAIN_S = -1.0
        try:
            main._PAGES[s_live.id] = {
                "c1": main._Page(live=lambda: True, move=lambda sid: None)
            }
            main._PAGES[s_idle.id] = {
                "c2": main._Page(live=lambda: False, move=lambda sid: None)
            }

            await main._retire("alex", keep=set())

            remaining = {s.id for s in (await service.list_sessions(
                app_name=main.APP_NAME, user_id="alex")).sessions}

            check("a session holding an active live page is not deleted during retirement",
                  s_live.id in remaining, True,
                  "active live connection protects thread even when older than RETAIN_S")
            check("an idle session past RETAIN_S is retired",
                  s_idle.id in remaining, False,
                  "page with live() == False is swept")
            check("an untouched session past RETAIN_S without pages is retired",
                  s_empty.id in remaining, False,
                  "unreferenced thread is swept")

            # Once the live page disarms (live() == False), the next retire sweep drops it
            main._PAGES[s_live.id]["c1"] = main._Page(
                live=lambda: False, move=lambda sid: None)
            await main._retire("alex", keep=set())
            remaining_after = {s.id for s in (await service.list_sessions(
                app_name=main.APP_NAME, user_id="alex")).sessions}
            check("disarmed session past RETAIN_S is retired on subsequent sweep",
                  s_live.id in remaining_after, False,
                  "thread dropped once live() is False")
        finally:
            main.RETAIN_S = was_retain
            main._PAGES.clear()
            main._PAGES.update(was_pages)


# ------------------------------------------------------ what `gws` holds on to

async def asked(request: str, spy) -> dict:
    """One `gws` question, with the sub-agent replaced by `spy`."""
    real = gws._ask
    gws._ask = spy
    try:
        return await gws.gws(request, FakeContext(call_id=f"call-{request}"))
    finally:
        gws._ask = real


async def gws_bookkeeping_cases() -> None:
    print("\n== the two dictionaries a question passes through ==")

    seen: list[dict] = []

    async def spy(args, tool_context):
        seen.append({
            # Read from inside the question, because both are cleared on the
            # way out and looking afterwards would find them clear either way.
            "memo": gws_cache._META.get() is not None,
            "ledger": _sources._LEDGER.get() is not None,
            "tracked": len(gws._RUNNING.get("live-1", ())),
            "asking": sorted(gws._ASKING.get("live-1", ())),
        })
        return f"answer {len(seen)}"

    frames: list[dict] = []

    async def to_screen(frame):
        frames.append(frame)

    screen.register("live-1", to_screen)
    gws.open_session("live-1")
    try:
        first = await asked("what is on today", spy)
        second = await asked("what is on today", spy)
    finally:
        gws.close_session("live-1")
        screen.unregister("live-1")

    check("the question is answered", first, {"answer": "answer 1"})
    # The one that bit: `asking.discard` is in a `finally` and reads like
    # tidying up. It is not — `_ASKING` is what refuses a repeat while one is
    # in flight, and an entry left in it refuses every later ask of the same
    # question for the life of the session, with `already_running` and no card.
    check("and asking it again afterwards is not mistaken for a repeat",
          second, {"answer": "answer 2"})
    check("each drew its own card",
          [(f["type"], f["id"]) for f in frames],
          [("gws", "call-what is on today")] * 2)
    check("the work ran under a Drive memo of its own",
          [turn["memo"] for turn in seen], [True, True],
          "one `drive.meta` per question, not one per lookup")
    # Same shape and the same failure: opened around `create_task` so the work
    # inherits it, which means the block has closed by the time anything could
    # look, and a deleted line leaves a variable that was already `None`.
    check("and under a source ledger of its own",
          [turn["ledger"] for turn in seen], [True, True],
          "one ledger per question, so one card's sources are one card's")
    check("and was tracked so the socket closing can cancel it",
          [turn["tracked"] for turn in seen], [1, 1])
    check("with the question itself held only while it is in flight",
          [turn["asking"] for turn in seen],
          [["what is on today"]] * 2)


async def gws_agenda_frame_cases() -> None:
    print("\n== and the calendar it carries out to the card ==")

    frames: list[dict] = []

    async def to_screen(frame):
        frames.append(frame)

    # Stands in for the `agenda` tool, called from inside the sub-agent's task
    # the way the real one is. What it returns to the sub-agent is prose; the
    # events go the other way, into the dict `showing` put in the context.
    day = {"from": "2026-09-10", "to": "2026-09-10", "timezone": "JST",
           "count": 1, "overlaps": [],
           "events": [{"day": "2026-09-10", "from": "19:00", "to": "21:00",
                       "title": "dinner", "note": "not for the card"}]}

    async def read_the_calendar(args, tool_context):
        _agenda._remember(day)
        return "Dinner at seven."

    async def read_nothing(args, tool_context):
        return "Three unread from the school."

    async def ran_out(args, tool_context):
        _agenda._remember(day)
        return gws.TOO_SLOW

    screen.register("live-1", to_screen)
    gws.open_session("live-1")
    try:
        await asked("what is on tomorrow", read_the_calendar)
        drew = frames[-1]
        await asked("anything from the school", read_nothing)
        prose = frames[-1]
        await asked("what is on tomorrow evening", ran_out)
        stopped = frames[-1]
    finally:
        gws.close_session("live-1")
        screen.unregister("live-1")

    # The line this is here for. Without it the frame is `result` alone, the
    # card renders the Markdown it always rendered, and nothing anywhere says
    # the events were read and thrown away.
    check("a calendar question sends the events as well as the sentence",
          [e.get("title") for e in drew.get("agenda", {}).get("events", [])],
          ["dinner"])
    check("with the sentence still on it, because it is the answer",
          drew["result"], "Dinner at seven.")
    check("and the description prose left behind",
          "note" in drew["agenda"]["events"][0], False,
          "`SCREEN_FIELDS`, applied on the way into the dict")
    check("a question that never looked at the calendar sends no card for one",
          "agenda" in prose, False)
    # Each question gets its own `showing` block, so the calendar read by the
    # question before it must not still be in this frame.
    check("and does not inherit the last question's",
          prose["result"], "Three unread from the school.")
    # A run that read the calendar and then spent its clock has everything the
    # person asked for except the sentence about it. `gwsFill` draws both.
    check("a run stopped after reading it still draws it",
          (stopped.get("error"),
           [e.get("title") for e in stopped.get("agenda", {})
            .get("events", [])]),
          ("slow", ["dinner"]))


# ------------------------------------------ and what the answer came out of

# One Drive file as `gws` prints it, which is the shape the ledger reads. The
# name is the whole point: a `files export` prints the document and never its
# title, so this response is the only place the card's source line can come
# from and the only reason `record` runs on a listing at all.
A_FILE = json.dumps({
    "id": "1abc",
    "name": "Portland trip itinerary",
    "mimeType": "application/vnd.google-apps.document",
    "modifiedTime": "2026-09-04T11:02:00.000Z",
})

# The same file among the hits of a search wide enough that the search itself
# names nobody. That is the shape that matters for a refusal: the title exists
# only here, so a fetch that fails after this one is a fetch the ledger *can*
# put a name to and must not — and with the search ruled out too, the honest
# answer is the empty row.
A_LISTING = json.dumps({"files": [json.loads(A_FILE)] + [
    {"id": f"other{i}", "name": f"Something else {i}",
     "mimeType": "application/vnd.google-apps.document",
     "modifiedTime": "2026-09-01T00:00:00.000Z"} for i in range(5)]})


class FakeProc:
    """What `subprocess.run` returns, as far as `_run_sync` reads one."""

    def __init__(self, code: int, out: str):
        self.returncode, self.stdout, self.stderr = code, out, ""


@contextlib.contextmanager
def a_cli_that_prints(code: int, out: str):
    """`gws` replaced by a canned response, in a cache directory of its own.

    Nothing is spawned and nothing is written, so `gws_cache.record` sees an
    empty diff and asks Drive for nothing — which is the point, since the line
    under test runs on the same thread between those two.
    """
    was_run, was_dir = _gws_run.subprocess, gws_cache.cache_dir
    with tempfile.TemporaryDirectory() as scratch:
        gws_cache.cache_dir = lambda: Path(scratch)
        _gws_run.subprocess = type("S", (), {
            "run": staticmethod(lambda argv, **kw: FakeProc(code, out)),
            "TimeoutExpired": subprocess.TimeoutExpired,
        })
        try:
            yield
        finally:
            _gws_run.subprocess, gws_cache.cache_dir = was_run, was_dir


async def ran(code: int, out: str, command: str) -> list | None:
    """One CLI command through the real runner, and what it left in the ledger.

    Through `_gws_run.gws` and not `_run_sync`, because the ledger is a
    ContextVar and the call it has to survive is `asyncio.to_thread` — a
    context is copied into the worker, and this only works because the object
    on the far side is the same mutable ledger rather than a copy of it.
    """
    with a_cli_that_prints(code, out), _sources.collecting() as found:
        with _gws_run.budget():
            await _gws_run.gws(command)
    return _sources.collected(found)


async def source_wiring_cases() -> None:
    print("\n== and what the answer came out of ==")

    named = await ran(0, A_FILE, "gws drive files get --id 1abc")
    check("what the CLI printed reaches the ledger across the worker thread",
          [s["name"] for s in named or []], ["Portland trip itinerary"],
          "the one line in `_run_sync`; delete it and this is []")
    # Not a detail of `record` — that one is `sources_check`'s. This is the
    # `if` in front of the call, and it is here because it is one line away.
    failed = await ran(1, A_FILE, "gws drive files get --id 1abc")
    check("and a command that failed contributes nothing to it", failed, [],
          "a stack trace names no file, whatever it printed on the way")
    # The case above passes on a ledger that never saw a listing, so the
    # id it fetched is in nobody's `seen` and `[]` comes back for the
    # wrong reason. This is the shape that actually happens: a search, and
    # then an export of one of its hits that the API turns down.
    with a_cli_that_prints(0, A_LISTING), _sources.collecting() as after:
        with _gws_run.budget():
            await _gws_run.gws("gws drive files list --query trip")
        with a_cli_that_prints(1, "Error: cannot be exported"):
            with _gws_run.budget():
                await _gws_run.gws("gws drive files export --id 1abc")
    check("even where the listing in front of it holds a title for the id",
          _sources.collected(after), [],
          "otherwise the card names the one file it could not read")
    # The other half of the same wiring: a service with no files or threads in
    # it must leave the key off rather than say "no source" under every card.
    aside = await ran(0, "[]", "gws calendar events list --days 1")
    check("a question that touched no file and no thread says nothing at all",
          aside, None, "`None`, which the card renders as no row")

    # And the frame. `_ask` is a spy again, but this one records the way the
    # CLI hop does, so what is under test is the four lines in `gws.gws` that
    # turn a ledger into a key on the wire.
    async def spy(args, tool_context):
        _sources.record(["drive", "files", "get", "--id", "1abc"], A_FILE)
        return "It leaves on the 14th."

    async def quiet(args, tool_context):
        return "It leaves on the 14th."

    async def card(request: str, with_spy) -> dict:
        frames: list[dict] = []

        async def to_screen(frame):
            frames.append(frame)

        screen.register("live-2", to_screen)
        gws.open_session("live-2")
        real = gws._ask
        gws._ask = with_spy
        try:
            await gws.gws(request, FakeContext("live-2", f"call-{request}"))
        finally:
            gws._ask = real
            gws.close_session("live-2")
            screen.unregister("live-2")
        return frames[-1]

    drawn = await card("when does the flight leave", spy)
    check("the card is told which file the answer came out of",
          drawn.get("sources"),
          [{"kind": "doc", "name": "Portland trip itinerary",
            "detail": "2026-09-04"}])
    plain = await card("what is the weather", quiet)
    check("and is told nothing when the question read nothing",
          "sources" in plain, False, "the key is absent, not empty")

    async def gave_up(args, tool_context):
        # It read a file and then the run failed anyway, which is the case the
        # `else` exists for: a source row under "the request failed" is a claim
        # that something was nonetheless read, made by the half of the card the
        # household is meant to be able to trust.
        _sources.record(["drive", "files", "get", "--id", "1abc"], A_FILE)
        return gws.FAILED

    lost = await card("what did the hotel say", gave_up)
    check("and a card that failed is told nothing either",
          [lost.get("error"), "sources" in lost], ["failed", False])


# One cached document, as `_meta_fields` headers it. The four fields after
# `fetched` are Drive's own answer about the file, written at fetch time, and
# they are the whole of what a source line needs — which is why the warm path
# has nothing to parse.
A_CACHED = {
    "source": ('gws drive files export -o trip.md '
               '--params \'{"fileId": "1abc"}\''),
    "fetched": "2026-09-04T12:00:00+09:00",
    "file_id": "1abc",
    "title": "Portland trip itinerary",
    "mime_type": "application/vnd.google-apps.document",
    "modified": "2026-09-04T11:02:00.000Z",
}


@contextlib.contextmanager
def a_cache_holding(header: dict, name: str = "trip.md", current: str = ""):
    """A cache directory with one headered document in it, and a Drive to ask.

    `_is_stale` ends in a `gws` subprocess, so `json_output` is answered here.
    By default it says what the header says, which makes the copy fresh — the
    only branch of `read_cache` that reaches the line under test. `current` is
    a later `modifiedTime` for the case that has to go the other way.
    """
    was_cli, was_dir = _gws_cli.json_output, gws_cache.cache_dir
    with tempfile.TemporaryDirectory() as scratch:
        gws_cache.cache_dir = lambda: Path(scratch)
        _gws_cli.json_output = lambda argv, cwd, timeout=None: {
            "id": header.get("file_id", ""),
            "name": header.get("title", ""),
            "mimeType": header.get("mime_type", ""),
            "modifiedTime": current or header.get("modified", ""),
        }
        (Path(scratch) / name).write_text(
            gws_cache._header(header) + "the itinerary\n", encoding="utf-8")
        try:
            yield name
        finally:
            _gws_cli.json_output, gws_cache.cache_dir = was_cli, was_dir


async def cached_wiring_cases() -> None:
    print("\n== and the copy that answered without running a command ==")

    # The path `_prefetch` takes, which is the frequent one: a question naming
    # a document already on the SD card is answered out of the copy, no `gws`
    # subprocess runs, and `record` never sees it. Without the line in
    # `read_cache` the ledger is empty and the card says nothing — not "could
    # not name it" but nothing at all, which reads as a question that touched
    # no documents.
    with a_cache_holding(A_CACHED) as name:
        with _sources.collecting() as found:
            served = await gws_cache.read_cache(name)
        warm = _sources.collected(found)
    check("a document served out of the cache reaches the ledger",
          [(s["kind"], s["name"]) for s in warm or []],
          [("doc", "Portland trip itinerary")],
          "the one line in `read_cache`; delete it and this is None")
    check("with the document itself still handed to the sub-agent",
          served.get("text", "").endswith("the itinerary\n"), True)

    # And the other side of the same freshness check. A copy the original has
    # outrun is refused, and naming a document nobody was shown is the one
    # thing worse here than naming none.
    with a_cache_holding(A_CACHED,
                         current="2026-09-06T09:30:00.000Z") as name:
        with _sources.collecting() as found:
            stale = await gws_cache.read_cache(name)
        refused = _sources.collected(found)
    check("a copy refused as out of date is not claimed as a source",
          ["error" in stale, refused], [True, None])

    # The cold path's own half. The command wrote the file and printed the
    # document, so its stdout names nothing — the title is in the header
    # `_annotate` has just written, which is why the call sits after it.
    # `written` carries the mtime the diff saw, and it has to be the file's
    # own: `_still_ours` reads any other as a second command having replaced
    # it, and then the file is not this command's to name at all.
    with a_cache_holding(A_CACHED, "cold.md") as name:
        path = gws_cache.cache_dir() / name
        with _sources.collecting() as found:
            gws_cache.record({name: path.stat().st_mtime},
                             A_CACHED["source"])
        cold = _sources.collected(found)
    check("and a file just fetched is named off the header, not off stdout",
          [s["name"] for s in cold or []], ["Portland trip itinerary"],
          "`files export` prints the document and never its title")


async def plan_bookkeeping_cases() -> None:
    print("\n== and the one a plan passes through ==")

    seen: list[int] = []

    async def spy(args, tool_context):
        seen.append(len(plan_mod._RUNNING.get("live-1", ())))
        return "an answer"

    async def to_screen(frame):
        pass

    real = plan_mod._ask
    plan_mod._ask = spy
    screen.register("live-1", to_screen)
    plan_mod.open_session("live-1")
    try:
        got = await plan_mod.plan("dinner", FakeContext(call_id="call-plan"))
    finally:
        plan_mod._ask = real
        plan_mod.close_session("live-1")
        screen.unregister("live-1")

    check("the plan is answered", got, {"answer": "an answer"})
    check("and it too is tracked while it runs", seen, [1])


# ---------------------------------------------------- which wait each declares

def scheduling_cases() -> None:
    print("\n== which wait each tool declares ==")

    # One attribute assignment each, and the difference between them is a
    # judgement about the person rather than about the code: an answer they are
    # standing there waiting for interrupts, a train time waits for a gap.
    # Neither survives being deleted — the call goes back to BLOCKING, which is
    # the freeze #23 removed, and nothing anywhere says so.
    check("the gws tool interrupts the conversation to answer",
          gws.build_gws_tool().response_scheduling,
          types.FunctionResponseScheduling.INTERRUPT)
    check("and so does plan",
          plan_mod.build_plan_tool().response_scheduling,
          types.FunctionResponseScheduling.INTERRUPT)
    check("while browse waits for a gap",
          browse.browse_tool.response_scheduling,
          types.FunctionResponseScheduling.WHEN_IDLE)
    # Not a default anyone gets for free: an ADK tool with nothing set on it
    # is BLOCKING, so all three of the above are doing work.
    check("none of which is what a tool declares on its own",
          FunctionTool(gws.gws).response_scheduling, None,
          "an unset FunctionTool, which ADK runs as BLOCKING")


# ------------------------------------- one card style, five prompts to carry it

@contextlib.contextmanager
def maps_grounding():
    """Stand in for the Maps sidecar's one `generate_content`, and keep it.

    Patched at `genai_client` rather than at `_ground_on_maps`, because what is
    being pinned is which of `contents` and `system_instruction` the house style
    ends up in, and that split is made inside the real function.
    """
    sent: dict = {}

    class _Models:
        def generate_content(self, *, model, contents, config):
            sent.update(model=model, contents=contents, config=config)
            return types_mod.SimpleNamespace(
                candidates=[types_mod.SimpleNamespace(
                    content=None, grounding_metadata=None)])

    before = maps.grounding_client
    maps.grounding_client = lambda: types_mod.SimpleNamespace(models=_Models())
    try:
        yield sent
    finally:
        maps.grounding_client = before


@contextlib.contextmanager
def searching():
    """The same for web search, which posts JSON rather than calling the SDK.

    `search_url` goes too: it reads `GOOGLE_CLOUD_PROJECT_NUMBER` out of the
    environment and a check that needs a project number set is a check that
    fails on a clean machine for a reason that has nothing to do with it.
    """
    sent: dict = {}

    class _Session:
        def post(self, url, data=None, headers=None, timeout=None):
            sent.update(json.loads(data))
            return types_mod.SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"candidates": [{}]})

    before = web_search.authed_session, web_search.search_url
    web_search.authed_session = lambda: _Session()
    web_search.search_url = lambda: "https://example.invalid/"
    try:
        yield sent
    finally:
        web_search.authed_session, web_search.search_url = before


def screen_style_cases() -> None:
    """Whether the shared card style reaches each model that writes a card.

    #136 asks that every text the app shows be Markdown with effective emoji.
    The page half of that already shipped — every prose surface on the panel
    goes through `formatMarkdown` — so the whole of the change is one string,
    and a string is exactly the kind of thing that gets defined and then not
    sent. Three carriers hold it as a constant, which is cheap to read; two
    build it into a request at call time and get a faked transport instead.
    """
    print("\n== the shared card style, and the five prompts that carry it ==")

    style = _lang.SCREEN_STYLE

    # The clause that makes an emoji droppable, which is the reason this is a
    # rule and not a taste. The glyph is thrown away twice for real: by a panel
    # with no colour emoji font, and by the voice, which paraphrases the card
    # aloud. Anything spelled with a picture is meaning lost both times.
    check("the style says an emoji may never carry meaning",
          "read exactly the same when every emoji is deleted" in style, True)
    check("and puts it where it decorates rather than reads",
          "very first character of the line" in style, True)
    check("and keeps it out of the sentence that is spoken",
          "No emoji in the opening sentence" in style, True)
    # `markdown.js` renders bold, italic, code, links, bullets, numbers and one
    # heading rank. A style that asked for a table would put literal pipes on
    # the card.
    check("and asks only for what the renderer can draw",
          all(s in style for s in
              ("no headings", "no tables", "no blockquotes", "no raw HTML")),
          True)

    check("the browser carries it", style in browser.SYSTEM, True)
    check("and so does the planner", style in plan_mod.INSTRUCTION, True)
    # Not `gws.INSTRUCTION`. Nothing passes that constant to the sub-agent
    # (#146): the agent is built with `_instruction`, which rebuilds the prompt
    # at every turn so it can swap in the document cache it has. A patch that
    # appended to `INSTRUCTION` would satisfy a check written against it and
    # ship nothing to the model.
    was_cache_dir = gws_cache.cache_dir
    with tempfile.TemporaryDirectory() as scratch:
        gws_cache.cache_dir = lambda: Path(scratch)
        try:
            instruction = gws._instruction(None)
        finally:
            gws_cache.cache_dir = was_cache_dir
    check("and the Workspace sub-agent is given it at the turn it runs",
          style in instruction, True)

    with maps_grounding() as sent:
        maps._ground_on_maps("ramen near Shibuya", 3, "ja")
    check("maps grounding sends it as a system instruction",
          style in sent["config"].system_instruction, True)
    # And nowhere near `contents`. Maps grounding searches from `contents`, so
    # a house style moved in there is read as part of the query: the same
    # 家電量販店 with these sentences in the prompt returned shops in Yokohama.
    check("and not in the text it searches from",
          style in sent["contents"], False)

    with searching() as sent:
        web_search._web_search_sync("who won", "ja")
    check("web search sends it as a system instruction",
          style in sent["systemInstruction"]["parts"][0]["text"], True)
    check("web search sends no conflicting narrative instructions",
          "short paragraphs" not in sent["systemInstruction"]["parts"][0]["text"], True)
    check("style explicitly illustrates bullet marker and emoji",
          "* 🍜 **name**" in style, True)

    # The one prompt told the opposite, and the only one whose output is heard
    # rather than read. A voice handed "use emoji" says "bowl of steaming
    # ramen", which is the failure the owner would notice first.
    check("the voice is not given the card style",
          style in agent.INSTRUCTION, False)
    check("it is told the layout belongs to the screen",
          "never say one and never name one"
          in " ".join(agent.INSTRUCTION.split()), True)


def maps_closed_client_cases() -> None:
    """_ground_on_maps gracefully handles closed httpx transport (#647).

    #647: when the underlying httpx transport of a cached genai client is
    closed, generate_content raises RuntimeError("Cannot send a request, as
    the client has been closed."). _ground_on_maps must catch the exception,
    log a warning, evict the closed client from cache, and return safe fallback
    summaries ("Could not retrieve route information." for directions queries,
    "" for others) so the live ADK session does not crash.
    """
    print("\n== maps grounding error handling and closed transport eviction (#647) ==")

    # Test is_client_closed helper
    check("is_client_closed returns False on None",
          _client.is_client_closed(None), False)
    check("is_client_closed returns False on arbitrary object without api_client",
          _client.is_client_closed(types_mod.SimpleNamespace()), False)

    open_client = types_mod.SimpleNamespace(
        _api_client=types_mod.SimpleNamespace(
            _httpx_client=types_mod.SimpleNamespace(is_closed=False)))
    check("is_client_closed returns False on open httpx client",
          _client.is_client_closed(open_client), False)

    closed_sync_client = types_mod.SimpleNamespace(
        _api_client=types_mod.SimpleNamespace(
            _httpx_client=types_mod.SimpleNamespace(is_closed=True)))
    check("is_client_closed returns True on closed sync httpx client",
          _client.is_client_closed(closed_sync_client), True)

    closed_async_client = types_mod.SimpleNamespace(
        _api_client=types_mod.SimpleNamespace(
            _httpx_client=None,
            _async_httpx_client=types_mod.SimpleNamespace(is_closed=True)))
    check("is_client_closed returns True on closed async httpx client",
          _client.is_client_closed(closed_async_client), True)

    # Test evict_if_closed helper
    _client._cached_genai_client = closed_sync_client
    _client._cached_global_client = open_client
    evicted = _client.evict_if_closed(closed_sync_client)
    check("evict_if_closed returns True when evicted", evicted, True)
    check("evict_if_closed cleared closed genai client",
          _client._cached_genai_client is None, True)
    check("evict_if_closed preserved open global client",
          _client._cached_global_client is open_client, True)

    _client._cached_global_client = closed_sync_client
    _client.evict_if_closed()
    check("evict_if_closed() without arguments evicts closed global client",
          _client._cached_global_client is None, True)

    # Test _ground_on_maps exception handling on closed client
    class ClosedModels:
        def generate_content(self, *args, **kwargs):
            raise RuntimeError("Cannot send a request, as the client has been closed.")

    fake_closed = types_mod.SimpleNamespace(
        models=ClosedModels(),
        _api_client=types_mod.SimpleNamespace(
            _httpx_client=types_mod.SimpleNamespace(is_closed=True)))

    before = maps.grounding_client
    maps.grounding_client = lambda: fake_closed
    _client._cached_global_client = fake_closed

    try:
        route_res = maps._ground_on_maps(
            "Give me driving directions from Tokyo to Yokohama. Include the total travel time and distance.",
            5, "en")
        check("directions query returns fallback route summary on error",
              route_res["summary"], "Could not retrieve route information.")
        check("directions query returns empty places list on error",
              route_res["places"], [])
        check("closed client is evicted from cache on error",
              _client._cached_global_client is None, True)

        _client._cached_global_client = fake_closed
        search_res = maps._ground_on_maps("Return only the top 5 results for: ramen", 5, "ja")
        check("search query returns empty summary on error",
              search_res["summary"], "")
        check("search query returns empty places list on error",
              search_res["places"], [])
        check("closed client is evicted again from cache",
              _client._cached_global_client is None, True)
    finally:
        maps.grounding_client = before
        _client.clear_client_cache()


# --------------------------------------------- an explicit ask to watch one


def tool_bullet(name: str) -> str:
    """The tool list's entry for `name`, on one line, or "" for no entry.

    The list is `- <tool>: …` at column 0 with its continuation indented, so a
    bullet runs to the next one. Read off the bullet and not off the whole
    prompt: the tool list is where the model is choosing, and a sentence about
    choosing that landed anywhere else is a sentence it reads after it has.
    """
    found = re.search(
        rf"^- {name}: (.*?)(?=^- \w+:|\Z)",
        agent.INSTRUCTION, re.DOTALL | re.MULTILINE)
    return " ".join(found.group(1).split()) if found else ""


def video_steering_cases() -> None:
    """An ask to watch a video reaches `youtube_search`, place or not.

    #246, an hour before a demo: "show a video of best ramen shops" was
    answered "I could not find any videos". Two bullets match a question about
    ramen shops and both are about places — `place_info` with a prohibition on
    top of it, *"use it instead of calling those four yourself"*, and
    `maps_search` with *"a ramen shop nearby"* almost word for word. Nothing
    said what an explicit ask to *watch* outranks, so the strongest language in
    the list pointed away from the tool that answers the question.
    """
    print("\n== an explicit ask to watch a video ==")
    video = tool_bullet("youtube_search")
    check("the tool list has a youtube_search bullet", bool(video), True)
    check("an ask to watch goes there whatever it is about",
          "whatever it is about" in video, True)
    check("and the place rules are told they do not outrank it",
          "none of them outranks a request for a video" in video, True)
    check("youtube_search instruction mentions trailers",
          "trailers" in video, True)
    check("agent instruction steers tool composition for reviews and entertainment",
          "reviews and entertainment" in agent.INSTRUCTION, True)
    check("agent instruction steers tool composition for visual queries",
          "visual queries" in agent.INSTRUCTION, True)
    web = tool_bullet("web_search")
    check("web_search prohibits video, footage, or clip searches (#700)",
          "Never use web_search to find videos, footage, or clips" in web, True)
    check("web_search routes video requests strictly to youtube_search (#700)",
          "Any request for a video, footage, clips, or to watch something belongs strictly to youtube_search" in web, True)
    check("youtube_search explicitly routes news footage and weather events (#700)",
          "news footage, weather events like typhoons" in video, True)
    check("youtube_search strictly and exclusively routes video requests (#700)",
          "Strictly and exclusively route video requests here instead of web_search" in video, True)


def clarifying_instruction_cases() -> None:
    """Underspecified starter requests guide the agent to ask clarifying questions.

    #481: bare starter chips send natural conversational requests like
    "I want to look up directions" or "道順を調べたい" without parameters.
    The agent instruction must guide the model to ask a concise clarifying
    question when parameters (destination, video topic, document name) are missing.
    """
    print("\n== clarifying questions on underspecified requests (#481) ==")
    flat = " ".join(agent.INSTRUCTION.split())
    check("instruction guides clarifying questions when requests are underspecified",
          "underspecified" in flat, True)
    check("instruction mentions missing destination",
          "directions without giving a destination" in flat, True)
    check("instruction mentions missing video topic",
          "video without a topic" in flat, True)
    check("instruction mentions missing document name",
          "document without a name" in flat, True)
    check("instruction asks for concise clarifying question rather than guessing",
          "ask a concise clarifying question rather than guessing" in flat, True)


def plan_steering_cases() -> None:
    print("\n== trip planning and itinerary steering to plan (#714) ==")
    from ai_terminal.live.agent import INSTRUCTION
    from ai_terminal.live.tools.plan import DESCRIPTION
    check(
        "agent instruction steers trip planning to plan tool",
        "週末の京都旅行の計画を立てて" in INSTRUCTION and "観光プランを作って" in INSTRUCTION,
        True,
    )
    check(
        "agent judgement clarifies plans/itineraries are not underspecified",
        "Trip planning, travel itineraries, and schedules are not underspecified" in INSTRUCTION,
        True,
    )
    check(
        "plan tool description includes trip planning and schedules",
        "trip planning and schedules" in DESCRIPTION,
        True,
    )


def browse_steering_cases() -> None:
    print("\n== explicit open/browse website steering to browse (#716, #722) ==")
    from ai_terminal.live.tools.browse import browse_tool
    browse_bullet = tool_bullet("browse")
    web_bullet = tool_bullet("web_search")
    check(
        "agent instruction steers explicit open/browse website requests to browse",
        "ウェブサイトを開いて" in agent.INSTRUCTION and "ページを開いて" in agent.INSTRUCTION,
        True,
    )
    check(
        "browse tool bullet steers explicit website open requests directly to browse",
        "call browse directly rather than falling back to web_search" in browse_bullet,
        True,
    )
    check(
        "browse tool bullet instructs calling directly rather than asking which website to open",
        "rather than asking which website to open" in browse_bullet,
        True,
    )
    check(
        "web_search tool bullet routes explicit website open requests to browse",
        "route directly to browse instead of web_search" in web_bullet,
        True,
    )
    check(
        "browse tool description mentions explicit requests to open or browse a site",
        "open, browse, or read a website or web page" in browse_tool.description,
        True,
    )
    check(
        "browse tool description guides calling directly rather than asking which website to open",
        "rather than asking which website to open" in browse_tool.description,
        True,
    )
    check(
        "agent judgement clarifies website open requests are not underspecified",
        "Requests to open or browse a website on a topic or without a specific URL are not underspecified" in agent.INSTRUCTION,
        True,
    )


def maps_route_steering_cases() -> None:
    print("\n== directions and route steering to maps_route (#718) ==")
    from ai_terminal.live.tools.maps import maps_route_tool
    maps_bullet = tool_bullet("maps_route")
    check(
        "agent instruction steers directions requests with single place to maps_route",
        "道順を調べたい" in agent.INSTRUCTION and "鎌倉駅" in agent.INSTRUCTION,
        True,
    )
    check(
        "maps_route bullet steers single place directly to maps_route",
        "treat the terminal's location (here/home) as the origin and the named place as the destination" in maps_bullet,
        True,
    )
    check(
        "maps_route bullet instructs calling directly rather than asking whether origin or destination",
        "calling maps_route directly rather than asking whether it is origin or destination" in maps_bullet,
        True,
    )
    check(
        "agent judgement clarifies directions with single place are not underspecified",
        "Directions with a single named place are not underspecified" in agent.INSTRUCTION,
        True,
    )
    check(
        "maps_route tool description guides single place resolution to here and destination",
        "treat the terminal's location (here) as the origin" in maps_route_tool.description,
        True,
    )


def maps_search_steering_cases() -> None:
    print("\n== explicit map search steering to maps_search (#728) ==")
    from ai_terminal.live.tools.maps import maps_search_tool
    maps_bullet = tool_bullet("maps_search")
    check(
        "agent instruction steers explicit map search to maps_search",
        "地図で探して" in agent.INSTRUCTION and "東京駅近くの静かなカフェを地図で探して" in agent.INSTRUCTION,
        True,
    )
    check(
        "maps_search tool description guides explicit map search requests directly to maps_search",
        ("地図で探して" in maps_search_tool.description
         or "explicitly asks to search for places or find shops/spots on a map" in maps_search_tool.description)
        and ("call this tool directly" in maps_search_tool.description or "maps_search directly" in maps_search_tool.description),
        True,
    )
    check(
        "maps_search tool bullet steers explicit map search requests directly to maps_search",
        "call maps_search directly" in maps_bullet and "地図で探して" in maps_bullet,
        True,
    )
    flat_instruction = " ".join(agent.INSTRUCTION.split())
    check(
        "agent judgement clarifies explicit map search requests are not answered from conversation context without tools",
        "are not answered from conversation context" in flat_instruction
        and "never reply with spoken promises without invoking the tool" in flat_instruction,
        True,
    )





def empty_search_instruction_cases() -> None:
    """Empty search results in Drive/Workspace are normal answers, not request failures.

    #477: zero search results in Google Drive and Workspace must be reported
    plainly to the user rather than failing the request with generic errors.
    """
    print("\n== empty search result guidance (#477) ==")
    flat_gws = " ".join(gws.INSTRUCTION.split())
    check("gws.py contains Drive empty files guidance",
          "If `files` is empty (`[]`), do not call `files get` or `files export`" in flat_gws, True)
    check("gws.py instructs stating no matching file was found in Google Drive",
          "state plainly that no matching file was found in Google Drive" in flat_gws, True)
    check("gws.py contains empty search guidance in AFTER_DOCUMENTS",
          "an empty search is never an error or failure" in flat_gws, True)

    flat_agent = " ".join(agent.INSTRUCTION.split())
    check("agent.py instructs empty search results are normal answers not failures",
          "an empty search is a normal answer, not a request failure" in flat_agent, True)


def gws_payload_envelope_and_baseline_cases() -> None:
    """Payload over envelope and transactions supersede baselines in gws (#510).

    #510: boilerplate notification emails must have attachment payloads inspected
    before concluding information is missing, and recent transactions must supersede
    older baseline documents.
    """
    print("\n== payload over envelope and transactions supersede baselines (#510) ==")
    flat_gws = " ".join(gws.INSTRUCTION.split())
    check("gws.py contains payload over envelope principle",
          "Payload over envelope" in flat_gws, True)
    check("gws.py instructs inspecting payload/parts on boilerplate notice",
          "If `+read` returns a boilerplate notice, inspect `payload/parts`" in flat_gws, True)
    check("gws.py instructs fetching attachment before concluding information is missing",
          "fetch the attachment before concluding information is missing" in flat_gws, True)
    check("gws.py contains transactions supersede baselines principle",
          "Transactions supersede baselines" in flat_gws, True)
    check("gws.py instructs newer transaction supersedes older baseline document",
          "A newer transaction always supersedes an older baseline document" in flat_gws, True)
    check("gws.py instructs never asserting non-existence if newer transactions exist",
          "Never assert the non-existence of an option or current state from an older document" in flat_gws, True)


# ------------------------------------------ where a place search is searched

# Not an address the terminal could ever really be at. `here()` falls back to
# `HOME_LOCATION` out of the environment, and a machine with a real one set
# would make a leak of the pin look like a pass — this string appears in a
# prompt only if it came from the session state these cases put it in.
PINNED_AT = "Sentinel Pin, 0 Nowhere Street"


def near_clause(prompt: str) -> str:
    """What a grounding prompt ended up filtering on, or "" for nothing.

    Read off the text rather than out of the payload, because the payload's
    `near` is what the tool says it did and this is what Maps was actually
    asked. #196 was a case where those two agreed and both were wrong.
    """
    _, _, where = prompt.partition(" near ")
    return where


async def near_cases() -> None:
    """The three states of a location filter, on both tools that have one.

    #196: asking "日本で一番高い山は？" drew a map of 港区. `near` had two
    states where the domain has three — an omitted one meant the terminal's
    pin, so a national landmark was searched for within walking distance of
    the kitchen, and Tokyo has enough 富士塚 that Places found one. The word
    `here`, meanwhile, went to Maps as part of the search string.

    Both call sites, because `place_info` resolved the pin itself and passed
    it down positionally: fixing `maps.py` alone would leave the path the Fuji
    card most likely came through exactly as it was.
    """
    print("\n== where a place search is searched ==")

    pinned = FakeContext(state={"home_location": PINNED_AT})

    async def nothing(*args, **kwargs) -> dict:
        return {}

    async def searched(call) -> tuple[str, str]:
        """One tool call, and the prompt Maps was handed for it."""
        with maps_grounding() as sent:
            payload = await call
        return near_clause(sent["contents"]), payload.get("near", "")

    # `place_info` fans out to four tools and only one of them is the subject.
    # The other three are stubbed rather than left to fail through `_branch`,
    # which would work — it swallows everything — at the price of three real
    # network calls per case and a traceback in the log to read past.
    before = place.web_search, place.image_search, place.youtube_search
    place.web_search = place.image_search = place.youtube_search = nothing
    try:
        anywhere = await searched(maps.maps_search("富士山", "", "ja", pinned))
        word = await searched(maps.maps_search("ラーメン", "here", "ja", pinned))
        shouted = await searched(
            maps.maps_search("ラーメン", " Here ", "ja", pinned))
        named = await searched(
            maps.maps_search("ラーメン", "横浜駅", "ja", pinned))
        p_anywhere = await searched(place.place_info("富士山", "", "", "ja",
                                                     pinned))
        p_word = await searched(place.place_info("ラーメン", "", "here", "ja",
                                                 pinned))
        p_said = await searched(place.place_info("ラーメン", "", "現在地",
                                                 "ja", pinned))
    finally:
        place.web_search, place.image_search, place.youtube_search = before

    check("an omitted `near` searches the whole earth, which is what a "
          "mountain wants", anywhere, ("", ""))
    check("and `place_info` agrees, though it used to resolve the pin itself",
          p_anywhere, ("", ""))
    check("the word `here` is the pin, not a place called Here",
          word, (PINNED_AT, PINNED_AT))
    check("on that path too", p_word, (PINNED_AT, PINNED_AT))
    check("and the model is not under oath about capitalisation",
          shouted, (PINNED_AT, PINNED_AT))
    check("anything else is taken at its word", named, ("横浜駅", "横浜駅"))
    # The pin is what the card draws now (`cards.js`), so an empty one has to
    # mean the search really was unfiltered rather than the tool having
    # forgotten to say. Both halves of each pair above are asserted for that
    # reason: the prompt is what happened, the payload is what the screen is
    # told about it, and #196 was invisible because nothing drew the second.
    check("what the card is told and what Maps was asked are the same thing",
          [anywhere[0] == anywhere[1], named[0] == named[1]], [True, True])

    # A route is the deliberate exception. Empty cannot mean anywhere there —
    # a route starts somewhere — so the pin stays the default, and the only
    # part of #196 that reaches these two is the literal word.
    with maps_grounding() as sent:
        await maps.maps_route("", "横浜駅", "DRIVE", "ja", pinned)
    check("a route with no origin still starts here",
          PINNED_AT in sent["contents"], True)
    with maps_grounding() as sent:
        await maps.maps_route("here", "横浜駅", "DRIVE", "ja", pinned)
    check("and one that says `here` does not ask for directions from a place "
          "called Here",
          [PINNED_AT in sent["contents"], "from here to" in sent["contents"]],
          [True, False])
    with maps_grounding() as sent:
        await maps.maps_search_along_route("here", "横浜駅", "コーヒー", "ja",
                                           pinned)
    check("nor does the search along it", PINNED_AT in sent["contents"], True)

    # #223: `here` was the only spelling that landed, on a panel that is
    # spoken to in Japanese. `現在地` went to Maps as the name of a place and
    # `_HOUSE_STYLE` then asked the model to name the district it searched, so
    # the wrong place came back stated with confidence instead of as an error.
    # Every string below is one a turn could put in the slot; none of them is
    # a place.
    said = [
        "here", "nearby", "near me", "around here", "around me", "close by",
        "this area", "my location", "my current location", "current location",
        "current position", "home",
        "現在地", "現在位置", "現在の場所", "自宅",
        "ここ", "ここら", "ここらへん", "この辺", "この辺り", "このへん",
        "この近く", "この付近", "付近", "周辺", "近辺", "近く", "近所",
        "最寄り",
        # Not written into `HERE`: the same words with the particle a model
        # leaves on when it copies the turn's phrasing into the slot.
        "ここから", "現在地から", "この辺で", "近くの", "現在地より",
        # And the shapes `.strip()`, `.lower()` and NFKC are there for.
        "  Current Location ", "ＮＥＡＲＢＹ", "　ここ　",
    ]
    missed = []
    for token in said:
        got = await searched(maps.maps_search("ラーメン", token, "ja", pinned))
        if got != (PINNED_AT, PINNED_AT):
            missed.append(token)
    check("every way of saying the pin is the pin, not a place called that",
          missed, [])

    # The other half, and the reason membership is on the whole argument
    # rather than a substring: a real name that contains one of those words is
    # still a real name. `そば` is left out of `HERE` for this reason alone —
    # it is a noodle before it is an adverb.
    kept = []
    for name in ("Here Comes Cafe", "近くのコンビニ商店", "現在地ビル",
                 "ここから商店街", "そば処 まつや", "周辺機器ショップ",
                 "自宅サロン ひなた"):
        got = await searched(maps.maps_search("ラーメン", name, "ja", pinned))
        if got != (name, name):
            kept.append((name, got))
    check("and a place whose name merely contains one is still searched for",
          kept, [])

    # `origin` is where the Japanese spellings are likeliest of the three,
    # because "ここから横浜駅まで" hands the model the particle as well.
    for origin in ("現在地", "ここから", "この辺", "my location"):
        with maps_grounding() as sent:
            await maps.maps_route(origin, "横浜駅", "DRIVE", "ja", pinned)
        check(f"a route from {origin!r} starts at the pin",
              [PINNED_AT in sent["contents"],
               f"from {origin} to" in sent["contents"]], [True, False])
    with maps_grounding() as sent:
        await maps.maps_search_along_route("現在地", "横浜駅", "コーヒー",
                                           "ja", pinned)
    check("and so does the search along it", PINNED_AT in sent["contents"],
          True)

    # `destination` resolves `here` and home terms to coordinates, but leaves
    # empty alone (#227).
    for dest in ("here", "home", "自宅"):
        with maps_grounding() as sent:
            await maps.maps_route("横浜駅", dest, "DRIVE", "ja", pinned)
        check(f"a route to {dest!r} ends at the pin",
              [PINNED_AT in sent["contents"],
               f"to {dest}." in sent["contents"]], [True, False])

    # #474: maps_route and maps_search_along_route carry coordinates for home / here
    coords_pinned = FakeContext(
        state={
            "home_location": PINNED_AT,
            "home_coords": {"lat": 35.658, "lng": 139.771},
        }
    )
    with maps_grounding():
        res_empty = await maps.maps_route("", "横浜駅", "DRIVE", "ja", coords_pinned)
        res_here = await maps.maps_route("here", "横浜駅", "DRIVE", "ja", coords_pinned)
        res_named = await maps.maps_route(PINNED_AT, "横浜駅", "DRIVE", "ja", coords_pinned)
        res_dest = await maps.maps_route("横浜駅", "自宅", "DRIVE", "ja", coords_pinned)
        res_unrelated = await maps.maps_route("横浜駅", "渋谷駅", "DRIVE", "ja", coords_pinned)
        res_along = await maps.maps_search_along_route("here", "横浜駅", "コーヒー", "ja", coords_pinned)
        res_unpinned = await maps.maps_route("", "横浜駅", "DRIVE", "ja", pinned)

    check("maps_route with empty origin attaches home_coords",
          res_empty.get("origin_coords"), {"lat": 35.658, "lng": 139.771})
    check("maps_route with `here` origin attaches home_coords",
          res_here.get("origin_coords"), {"lat": 35.658, "lng": 139.771})
    check("maps_route with home location string attaches home_coords",
          res_named.get("origin_coords"), {"lat": 35.658, "lng": 139.771})
    check("maps_route to home attaches destination_coords",
          res_dest.get("destination_coords"), {"lat": 35.658, "lng": 139.771})
    check("maps_route between other places carries no coordinates",
          [res_unrelated.get("origin_coords"), res_unrelated.get("destination_coords")],
          [None, None])
    check("maps_search_along_route attaches origin_coords",
          res_along.get("origin_coords"), {"lat": 35.658, "lng": 139.771})
    check("maps_route without home_coords in state has no origin_coords",
          res_unpinned.get("origin_coords"), None)

    # `place_info` hands `near` to `maps_search` untouched (`place.py:110`),
    # so it inherits the widening rather than needing its own copy — the one
    # thing worth asserting, since #196 was exactly the case where it did not.
    check("place_info inherits every spelling", p_said, (PINNED_AT, PINNED_AT))

    # The docstrings are the only handle the planner's sub-agent has on this
    # argument: `plan.py` builds its instruction from `INSTRUCTION` and the
    # clock, so the "Where you are" paragraph never reaches it. Ship the code
    # with the old sentence and every "somewhere nearby" that goes through a
    # plan searches the planet.
    for name, tool in (("maps_search", maps.maps_search),
                       ("place_info", place.place_info)):
        doc = " ".join((tool.__doc__ or "").split())
        check(f"{name}'s docstring no longer says empty means here",
              "Leave empty to use the terminal's own location" in doc, False)
        check(f"and names `here` as the spelling that does",
              "`here`" in doc, True)
    # And the voice's own instruction, which is the other reader and the one
    # that used to say "pass this address rather than the word 'here'" with no
    # exception for the argument where an address is not the only option.
    said = " ".join(agent._instruction(FakeContext(
        state={"home_location": PINNED_AT})).split())
    check("the agent is told what an empty `near` costs",
          "an empty one there means anywhere on earth" in said, True)

    # The third tool with a location argument. `weather` has no `near` and no
    # three states — empty is here, like a route's origin — and the reason it
    # was left out of #196 was that defaulting to here is harmless because it
    # has the terminal's real coordinates. What that reasoning does not cover
    # is the model *passing* a location that means here: it discards the pin
    # and geocodes the address the pin was reverse-geocoded into, which is a
    # rounding of it and, through the grounding model, a different rounding
    # each time (#181).
    pinned = FakeContext(state={"home_location": PINNED_AT,
                                "home_coords": {"lat": 35.5, "lng": 139.5}})
    unpinned = FakeContext(state={"home_location": PINNED_AT})

    async def forecast(location: str, ctx=None) -> tuple:
        """One `weather` call, as the point it settled on and what it calls it.

        `_weather_sync` rather than `_fetch`, because what is being measured is
        which coordinates the tool chose and not what Open-Meteo would say
        about them. `_geocode` answers with somewhere that is not the pin, so a
        case that resolves through it fails on the coordinates rather than on
        the two being indistinguishable.
        """
        got: list[tuple] = []
        was = weather._weather_sync, weather._geocode
        weather._weather_sync = (
            lambda lat, lng, place, lang, past_days=0:
            got.append((lat, lng, place)) or {})
        weather._geocode = lambda place, lang: (34.0, 135.0, "Somewhere Else")
        try:
            await weather.weather(location, "en", ctx or pinned)
        finally:
            weather._weather_sync, weather._geocode = was
        return got[0]

    check("an empty `location` is the pin, which is what the agent is asked "
          "for", await forecast(""), (35.5, 139.5, PINNED_AT))
    check("the word `here` is the pin too, as it is on the other three tools",
          await forecast("here"), (35.5, 139.5, PINNED_AT))
    check("and so is the address out of the prompt, which is what the model "
          "actually sent", await forecast(PINNED_AT),
          (35.5, 139.5, PINNED_AT))
    check("still not under oath about capitalisation or spacing",
          await forecast(f"  {PINNED_AT.upper()}  "),
          (35.5, 139.5, PINNED_AT))
    check("anywhere else is geocoded, which is the whole point of the "
          "argument", await forecast("京都"), (34.0, 135.0, "Somewhere Else"))
    # An address that starts with the terminal's is a different place, and the
    # cheap version of the test above — a prefix or a substring — says it is
    # this one.
    check("and an address this one is only the start of is somewhere else",
          await forecast(f"{PINNED_AT}, 4th floor"),
          (34.0, 135.0, "Somewhere Else"))
    # A terminal whose browser reported no coordinates: there is no pin to
    # prefer, so every one of the three spellings has to go on geocoding the
    # address rather than falling through to the error.
    check("a terminal with an address and no pin still geocodes it",
          [await forecast("", unpinned), await forecast("here", unpinned)],
          [(34.0, 135.0, "Somewhere Else")] * 2)
    # The model-facing half. `plan.py` builds its sub-agent's instruction from
    # `INSTRUCTION` and the clock, so the "Where you are" paragraph never
    # reaches it and this docstring is all it has.
    doc = " ".join((weather.weather.__doc__ or "").split())
    check("weather's docstring names the spellings that mean here",
          ["`here`" in doc, "own address" in doc], [True, True])


# ------------------------------------------------- what /api/config hands over

async def config_cases() -> None:
    print("\n== the household the page is told about ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    def named(value: str | None) -> list[str]:
        if value is None:
            os.environ.pop("PROFILES", None)
        else:
            os.environ["PROFILES"] = value
        return main._profiles()

    before = os.environ.get("PROFILES")
    try:
        # `PROFILES` is typed into a `.env` by hand, so the shapes below are
        # the ones a person actually writes. A stray space becomes a name the
        # picker offers and a thread nobody ever speaks into again.
        check("nobody set, nobody named", named(None), [])
        check("and an empty setting is the same as none", named(""), [])
        check("one name", named("alex"), ["alex"])
        check("a comma-separated household", named("alex,sam"),
              ["alex", "sam"])
        check("written the way a person writes it", named(" alex , sam "),
              ["alex", "sam"])
        check("a trailing comma names nobody extra", named("alex,sam,"),
              ["alex", "sam"])

        # The wiring claim: nothing else reads `PROFILES`, so a picker with
        # nobody in it is what a page gets if this key is dropped from the
        # payload — and `showProfiles` stays hidden on an empty list, which is
        # a terminal that quietly answers as `terminal` for the whole family.
        named("alex,sam")
        payload = await main.api_config()
        check("and the page is told who they are",
              payload.get("profiles"), ["alex", "sam"])
        check("along with what it needed before",
              sorted(payload), ["build", "management_routes", "maps_api_key",
                                "model", "profiles",
                                "revision", "static_revision", "voice",
                                "voices", "wake_idle_s"])
        check("and management_routes is present in the config",
              "management_routes" in payload, True)
    finally:
        if before is None:
            os.environ.pop("PROFILES", None)
        else:
            os.environ["PROFILES"] = before


# ------------------------------------------------- which voice the terminal has

async def voice_cases() -> None:
    print("\n== which voice the connection is opened in ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    def spoken(run_config) -> str | None:
        """The voice name buried in a `RunConfig`, or None if there is none."""
        speech = run_config.speech_config
        if speech is None or speech.voice_config is None:
            return None
        prebuilt = speech.voice_config.prebuilt_voice_config
        return None if prebuilt is None else prebuilt.voice_name

    # The list has to be a list. google-genai 2.22.0 types `voice_name` as a
    # bare `Optional[str]` with no validator (`genai/types.py:5537-5542`), so
    # nothing between here and the wire will notice a name that is not a voice
    # — the Live API refuses the setup message and the socket never comes up.
    check("there is more than one voice to choose between", len(main.VOICES) > 1,
          True, ", ".join(main.VOICES))
    check("and the default is one of them", main.DEFAULT_VOICE in main.VOICES,
          True, main.DEFAULT_VOICE)

    # What a query string is allowed to do. It is `localStorage` off a page
    # this build may not have served, so the only safe reading of a name that
    # is not on the list is the default — the alternative is a terminal that
    # reconnects into the same refused setup until somebody clears storage on
    # a kiosk with no keyboard.
    for asked, want in ((main.VOICES[-1], main.VOICES[-1]),
                        ("", main.DEFAULT_VOICE),
                        ("  ", main.DEFAULT_VOICE),
                        ("kore", main.DEFAULT_VOICE),
                        ("Zephyr", main.DEFAULT_VOICE),
                        ("Kore; DROP", main.DEFAULT_VOICE)):
        check(f"a socket asking for {asked or '(nothing)'!r} gets {want}",
              main._voice({"voice": asked}), want)
    check("and a page that asks for none at all gets the same",
          main._voice({}), main.DEFAULT_VOICE)

    # `LIVE_VOICE` is the escape hatch for the one judgement in this feature
    # that cannot be checked in software — which of the five is neutral. It is
    # filtered by the same list, because a typo in a `.env` would otherwise
    # take down every connection at once and say nothing on the way.
    was = os.environ.get("LIVE_VOICE")
    try:
        os.environ["LIVE_VOICE"] = main.VOICES[-1]
        check("the default is overridable from the environment",
              main._default_voice(), main.VOICES[-1])
        os.environ["LIVE_VOICE"] = "Zephyr"
        check("but not to a voice the Live API would refuse",
              main._default_voice(), main.VOICES[0])
        os.environ["LIVE_VOICE"] = ""
        check("and an empty one is the same as not setting it",
              main._default_voice(), main.VOICES[0])
    finally:
        if was is None:
            os.environ.pop("LIVE_VOICE", None)
        else:
            os.environ["LIVE_VOICE"] = was

    # Which branch of `_run_config` carries it. The model is stubbed rather
    # than read off `.env`, so this says the same thing on a machine set up for
    # either one.
    agent = main.agent
    try:
        main.agent = type("A", (), {"model": "gemini-live-2.5-flash-native-audio"})()
        check("a speaking model is given the voice it was asked for",
              spoken(main._run_config("Fenrir")), "Fenrir")
        check("interruption is disabled by default",
              main._run_config("Fenrir").realtime_input_config.activity_handling,
              "NO_INTERRUPTION")
        check("interruption is enabled when requested",
              main._run_config("Fenrir", interrupt=True).realtime_input_config.activity_handling,
              "START_OF_ACTIVITY_INTERRUPTS")
        main.agent = type("A", (), {"model": "gemini-live-2.5-flash"})()
        check("a half-cascade one is given none, having nothing to say it in",
              spoken(main._run_config("Fenrir")), None)
    finally:
        main.agent = agent

    # The wiring line, and the reason this is in this file. Everything above
    # holds with `_run_config` never called and the query string never read:
    # what makes the setting a setting is one argument at one call site, and
    # dropping it leaves a picker that changes a socket parameter nothing
    # sends. `run_live` is the only place the voice can still be observed
    # before it becomes a setup message.
    real_thread = main._thread
    chosen: list[str] = []

    async def note_thread(user_id, state):
        chosen.append(await real_thread(user_id, state))
        return chosen[-1]

    main._thread = note_thread
    main.agent = type("A", (), {"model": "gemini-live-2.5-flash-native-audio"})()

    def armed() -> list:
        """A script that opens a session and then lets the socket go.

        Since #60 the socket and the session have separate lifetimes: `/ws/`
        accepts, registers the screen and waits, and `_run_config` is not
        reached until the page sends `arm`. A socket that only connects
        therefore says nothing about which voice the session is opened in,
        which is the whole of what is being measured here.
        """
        async def arm() -> dict:
            return {"type": "websocket.receive",
                    "text": json.dumps({"type": "arm"})}

        async def then_go() -> dict:
            # `arm` starts `run_live` in a task, so the call it is about may
            # not have happened yet when the next frame is read. Bounded: a
            # session that never opens is itself the failure being looked for.
            for _ in range(1000):
                if main.runner.calls:
                    break
                await asyncio.sleep(0)
            return {"type": "websocket.disconnect"}

        return [arm, then_go]

    try:
        with a_server_of_its_own(main):
            await asyncio.wait_for(main.websocket_endpoint(
                FakeSocket(armed(), voice="Fenrir"), "user-1"), 10)
            asked = main.runner.calls
        with a_server_of_its_own(main):
            await asyncio.wait_for(main.websocket_endpoint(
                FakeSocket(armed(), voice="Zephyr"), "user-1"), 10)
            refused = main.runner.calls
    finally:
        main._thread = real_thread
        main.agent = agent

    check("the voice on the handshake is the one the Live session is opened in",
          [spoken(call["run_config"]) for call in asked], ["Fenrir"])
    check("and one the Live API would refuse never reaches it",
          [spoken(call["run_config"]) for call in refused],
          [main.DEFAULT_VOICE])


# --------------------------------------------- which language a tool answers in

def panel_set_to(tag: str) -> FakeContext:
    """A tool context on a terminal whose language picker says `tag`."""
    return FakeContext(state={"panel_lang": tag})


async def lang_cases() -> None:
    """Whether the panel's language setting reaches the tools (#187).

    The wiring failure this is about is the quiet kind. Every tool already took
    a `lang` and `agent.py` already said "Always pass it", so nothing was
    missing and nothing was logged — the model simply left the argument out, or
    filled it in from the language it thought it heard, and the sidecar was
    asked to match a query the model itself had written in English. A Japanese
    kitchen got English cards beside a Japanese spoken answer, which reads as
    the model being inconsistent rather than as a line nobody wrote.

    So the cases run the length of it: the setting onto the handshake, the
    handshake into the session state, the state into the request a tool sends.
    Each of the three holds on its own with the next one cut, and the middle
    one is the one #18's body proposed and #18 closed without.
    """
    print("\n== the panel's language, and how far it gets ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    # -- the floor itself, which is one expression and a decision.
    check("a tool called with no language and no panel has nothing to go on",
          _lang.of_turn("", None), "",
          "which is where `_MATCH_QUERY` is still the last resort")
    check("the panel's setting is the floor under a `lang` nobody passed",
          _lang.of_turn("", panel_set_to("ja")), "ja")
    check("and under one that is not a language tag",
          _lang.of_turn("Answer in English please", panel_set_to("ja")), "ja")
    # The case that earns its place: it is the only one that fails if somebody
    # implements "the setting always wins", which is the obvious wrong version
    # of this and the one that breaks a bilingual household.
    check("but the person speaking wins over the setting",
          _lang.of_turn("en", panel_set_to("ja")), "en")
    # The state arrives off a query string like everything else on the
    # handshake, so it is checked on the way out as well as on the way in.
    check("and what is in the state is a tag or it is nothing",
          _lang.of_turn("", panel_set_to("Answer in English please")), "")

    # -- what the sidecar is actually told, which is the point of the tag.
    check("an unfloored empty tag asks the sidecar to match the query",
          "the language the query itself is written in" in _lang.answer_in(""),
          True, "and for a place search the model writes that query in English")
    check("which is not what a tool sends on a panel set to `ja`",
          "`ja`" in _lang.answer_in(_lang.of_turn("", panel_set_to("ja"))),
          True)
    check("nor what it sends looking for images and videos",
          "`ja`" in _lang.search_in(_lang.of_turn("", panel_set_to("ja"))),
          True, "`search_in` has the same empty-tag branch")

    # -- the handshake, into the state the tools read.
    real_thread = main._thread
    seen: list[dict] = []

    async def note_thread(user_id, state):
        seen.append(dict(state))
        return await real_thread(user_id, state)

    main._thread = note_thread
    try:
        for params in ({"lang": "ja"}, {}, {"lang": "answer in English"}):
            with a_server_of_its_own(main):
                await asyncio.wait_for(main.websocket_endpoint(
                    FakeSocket([], **params), "user-1"), 10)
    finally:
        main._thread = real_thread

    check("the language on the handshake reaches the session a tool reads",
          seen[0].get("panel_lang"), "ja")
    check("a handshake carrying none leaves nothing in it",
          "panel_lang" in seen[1], False,
          "not an empty string, which a tool would have to know to ignore")
    check("and neither does one carrying something that is not a tag",
          "panel_lang" in seen[2], False)

    # -- the state, into the request. The two halves above both hold with this
    # one cut: the setting would arrive, sit in the session, and be read by
    # nobody, which is precisely the shape #187 found.
    with maps_grounding() as sent:
        await maps.maps_search("ramen shops", tool_context=panel_set_to("ja"))
    check("a maps search with no language of its own asks for the panel's",
          "`ja`" in sent["config"].system_instruction, True,
          "the call in the owner's report")

    with maps_grounding() as sent:
        await maps.maps_search("ramen shops", lang="en",
                               tool_context=panel_set_to("ja"))
    instruction = sent["config"].system_instruction
    check("and one that names a language is answered in that one",
          ["`en`" in instruction, "`ja`" in instruction], [True, False])

    with searching() as sent:
        await web_search.web_search("who won", tool_context=panel_set_to("ja"))
    check("web search too, which had no context to floor from at all",
          "`ja`" in sent["systemInstruction"]["parts"][0]["text"], True)

    # -- and every tool, so there is no exception to remember. Read off the
    # agent's own registry rather than a list written here, because the way
    # this comes undone is the twelfth tool.
    withlang = [tool for tool in main.agent.tools
                if "lang" in inspect.signature(tool.func).parameters]
    check("every tool that takes a language can be told the panel's",
          [tool.name for tool in withlang
           if "tool_context" not in inspect.signature(tool.func).parameters],
          [])
    check("and every one of them takes the floor on the way in",
          [tool.name for tool in withlang
           if "of_turn(lang, tool_context)"
           not in inspect.getsource(tool.func)],
          [], f"{len(withlang)} tools")
    # Three of them grew a `tool_context` for this, and a tool's parameters are
    # things the model fills in. ADK keeps this one out of the declaration; the
    # failure if it ever stopped is a model inventing a session object.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        offered = {tool.name: (tool._get_declaration().parameters_json_schema
                               or {}).get("properties", {})
                   for tool in withlang}
    check("and none of them offers it to the model as an argument",
          [name for name, args in offered.items() if "tool_context" in args],
          [])
    check("while every one still offers the language",
          [name for name, args in offered.items() if "lang" not in args], [])

    # -- Issue #239 / #419 / #480: composite streaming sections via coroutines for ADK FunctionTool
    check("web_search is a coroutine function for ADK FunctionTool",
          inspect.iscoroutinefunction(web_search.web_search), True)
    check("image_search is a coroutine function for ADK FunctionTool",
          inspect.iscoroutinefunction(image_search.image_search), True)
    check("youtube_search is a coroutine function for ADK FunctionTool",
          inspect.iscoroutinefunction(youtube_search.youtube_search), True)

    pushed_frames = []

    async def mock_push(session_id, frame):
        pushed_frames.append((session_id, frame))

    orig_push = screen.push
    screen.push = mock_push
    try:
        ctx = panel_set_to("en")
        ctx.invocation_id = "turn-42"
        with searching():
            res = await web_search.web_search("test query", tool_context=ctx)
        check("web_search returns mapping with 'web_search' key",
              isinstance(res, dict) and "web_search" in res, True)
        check("web_search pushes card_section frame to screen",
              any(f["type"] == "card_section" and f["section"] == "web_search" and f["turn_id"] == "turn-42"
                  for _, f in pushed_frames), True)

        # -- Issue #603: topic image thumbnails for news/trending queries
        check("is_news_query detects Japanese news queries",
              web_search.is_news_query("今日のニュース教えて"), True)
        check("is_news_query detects English news queries",
              web_search.is_news_query("latest tech news and headlines"), True)
        check("is_news_query detects trending keywords",
              web_search.is_news_query("current trends in AI"), True)
        check("is_news_query detects search_queries with news intent",
              web_search.is_news_query("what happened today", ["world news"]), True)
        check("is_news_query returns False for non-news queries",
              web_search.is_news_query("weather in kamakura"), False)
        check("is_news_query returns False for simple math",
              web_search.is_news_query("1 + 1"), False)

        sample_grounding = {
            "sources": [
                {"title": "大谷翔平が50号本塁打達成 - NHKニュース", "uri": "https://news.example/1"},
                {"title": "自民党総裁選が告示 | 朝日新聞", "uri": "https://news.example/2"},
                {"title": "【速報】東京で季節外れの夏日を観測 - 日本経済新聞", "uri": "https://news.example/3"},
                {"title": "為替相場が円高進行 - 読売新聞", "uri": "https://news.example/4"},
            ],
            "summary": "今日の主なニュースです。\n* **大谷翔平の偉業**: 50本塁打達成\n* **政治の動き**: 総裁選告示",
            "search_queries": ["news today", "大谷翔平 50号"],
        }
        extracted = web_search.extract_topics(sample_grounding)
        check("extract_topics returns up to 3 distinct topic headlines", len(extracted), 3)
        check("extract_topics cleans publisher suffixes and brackets",
              extracted, ["大谷翔平が50号本塁打達成", "自民党総裁選が告示", "東京で季節外れの夏日を観測"])

        async def mock_topic_image_search(topic, lang=""):
            return {
                "image_search": {
                    "images": [
                        {
                            "thumbnail_uri": f"https://img.example/{topic}.jpg",
                            "source_title": f"Image of {topic}",
                            "source_uri": f"https://source.example/{topic}",
                        }
                    ]
                }
            }

        orig_img_search = web_search.image_search
        orig_web_sync = web_search._web_search_sync
        web_search.image_search = mock_topic_image_search
        try:
            web_search._web_search_sync = lambda q, l: {
                "query": q,
                "lang": l,
                "summary": "News summary",
                "search_queries": ["latest news"],
                "sources": [
                    {"title": "Topic A - News", "uri": "https://a.test"},
                    {"title": "Topic B - News", "uri": "https://b.test"},
                    {"title": "Topic C - News", "uri": "https://c.test"},
                ],
            }
            news_res = await web_search.web_search("breaking news today", tool_context=ctx)
            check("web_search enriches news results with 3 topic images",
                  len(news_res["web_search"].get("images", [])), 3)
            check("each enriched image has thumbnail and source metadata",
                  news_res["web_search"]["images"][0]["url"], "https://img.example/Topic A.jpg")
            check("card_section frame pushed to screen contains images",
                  pushed_frames[-1][1]["data"]["images"][0]["title"], "Image of Topic A")

            async def failing_image_search(topic, lang=""):
                raise RuntimeError("API timeout")

            web_search.image_search = failing_image_search
            fail_res = await web_search.web_search("latest news", tool_context=ctx)
            check("image search failure gracefully falls back to empty images list",
                  fail_res["web_search"].get("images"), [])
        finally:
            web_search.image_search = orig_img_search
            web_search._web_search_sync = orig_web_sync

        orig_sync = youtube_search._youtube_search_sync
        youtube_search._youtube_search_sync = lambda q, l: {"videos": [{"id": "v456"}]}
        try:
            yt_res = await youtube_search.youtube_search("test video query", tool_context=ctx)
        finally:
            youtube_search._youtube_search_sync = orig_sync
        check("youtube_search returns mapping with 'youtube_search' key",
              isinstance(yt_res, dict) and "youtube_search" in yt_res, True)
        check("youtube_search pushes card_section frame to screen",
              any(f["type"] == "card_section" and f["section"] == "youtube_search" and f["turn_id"] == "turn-42"
                  for _, f in pushed_frames), True)
    finally:
        screen.push = orig_push

    # -- under `plan`, where the state is not the socket's. `_tools` shares the
    # live agent's tool objects, and the session they read down there is one
    # `_run` mints per call — so the floor has to be carried in by hand or the
    # planner's own `maps_search` is this bug again, one level down.
    seeded: list = []

    class _Recording(plan_mod.InMemorySessionService):
        async def create_session(self, **kwargs):
            seeded.append(kwargs.get("state"))
            return await super().create_session(**kwargs)

    class _NoRunner:
        """A `Runner` that runs nothing. The session is made before it."""

        def __init__(self, **kwargs):
            pass

        def run_async(self, **kwargs):
            async def nothing():
                for event in ():
                    yield event

            return nothing()

        async def close(self):
            pass

    def asked_by(panel: str) -> FakeContext:
        context = panel_set_to(panel)
        context._invocation_context = types_mod.SimpleNamespace(
            app_name="ai_terminal", user_id="alex", run_config=None,
            live_request_queue=None)
        return context

    sessions, runner = plan_mod.InMemorySessionService, plan_mod.Runner
    try:
        plan_mod.InMemorySessionService = _Recording
        plan_mod.Runner = _NoRunner
        await plan_mod._run({"task": "dinner"}, asked_by("ja"))
        await plan_mod._run({"task": "dinner"}, asked_by(""))
        await plan_mod._run({"task": "dinner", "lang": "en"}, asked_by("ja"))
    finally:
        plan_mod.InMemorySessionService = sessions
        plan_mod.Runner = runner

    check("the planner's own session is seeded with the panel's language",
          seeded[0], {"panel_lang": "ja"},
          "its tools are the same objects and read the session they are in")
    check("and a terminal with no setting seeds it nothing",
          seeded[1], None)
    # The turn wins over the panel, because the turn is already the panel
    # unless something deliberately moved it: `of_turn` floors `lang` to the
    # panel before the model's value reaches here. Seeding the panel anyway
    # made one plan answer in two languages — the steps where the planner
    # passed `lang` in the turn's, the steps where it left it out in the
    # panel's.
    check("and a turn asked in another language seeds that one instead",
          seeded[2], {"panel_lang": "en"})

    # -- the live agent's instruction, into the model's system prompt.
    check("the live model instruction carries the panel language when set",
          "The user speaks with ja" in agent._instruction(
              FakeContext(state={"panel_lang": "ja"})),
          True)
    check("and omits it when no panel language is set",
          "The user speaks with" in agent._instruction(FakeContext(state={})),
          False)
    check("and omits it when the panel language is not a valid tag",
          "The user speaks with" in agent._instruction(
              FakeContext(state={"panel_lang": "invalid tag with spaces"})),
          False)


# -------------------------------------------- the one route that destroys

def posted(kind: str | None) -> "Request":
    """A POST to `/api/forget`, carrying one content type or none at all.

    Built from a scope rather than driven through a client, because the whole
    question is what one header does and a client would insist on supplying
    it. `None` is the case that matters: `fetch(url, {method: "POST"})` sends
    no content type, and that is precisely the request a browser will deliver
    cross-origin without asking anybody first.
    """
    from starlette.requests import Request
    headers = [] if kind is None else [(b"content-type", kind.encode())]
    return Request({"type": "http", "http_version": "1.1", "method": "POST",
                    "scheme": "http", "path": "/api/forget/alex",
                    "raw_path": b"/api/forget/alex", "root_path": "",
                    "query_string": b"", "headers": headers,
                    "server": ("terminal", 8000),
                    "client": ("10.0.0.9", 4242)})


async def forget_guard_cases() -> None:
    print("\n== the route that deletes, and what may reach it ==")

    from fastapi import HTTPException

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    # First, because it is the one the rest depend on: the parameter is what
    # carries the header in, and a route that does not declare it is a route
    # FastAPI calls with no request at all. It reads like a spare argument and
    # it is the whole check. Without it every case below dies in a TypeError
    # rather than saying what is missing.
    src = Path(main.__file__).read_text(encoding="utf-8")
    signature = src[src.index("async def api_forget("):][:80]
    check("the endpoint takes the request, or it can see no headers",
          "request: Request" in signature, True, signature.split("\n")[0])

    # `api_forget` now clears the Workspace cache too (#233), and `cache_dir()`
    # is relative to the working directory — which for a sweep run from the
    # repo root is the developer's own `gws_cache/`. Every case below calls the
    # endpoint for real, so without this the check script deletes the cache it
    # is not about.
    #
    # Put back at the end of the function, and deliberately not in a `finally`:
    # `check` calls `sys.exit(1)` on the first failure (`wiring_check.py:136`),
    # so the only path that skips the restore is one where the process is
    # already going, and the stub is the safe state to die in — it points at a
    # scratch directory. A `finally` here would mean reindenting the hundred
    # lines between, for a cleanup nothing that survives can observe.
    was_cache_dir = gws_cache.cache_dir
    scratch_cache = tempfile.TemporaryDirectory()
    gws_cache.cache_dir = lambda: Path(scratch_cache.name)

    async def forget(kind: str | None) -> tuple[object, int]:
        """Call the endpoint, and count what is left afterwards.

        The count is the half that matters. A guard that returns 415 and has
        already run the delete loop is not a guard, and the status code alone
        cannot tell the two apart.
        """
        with a_server_of_its_own(main):
            await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="alex", state={})
            try:
                answer: object = await main.api_forget("alex", posted(kind))
            except HTTPException as err:
                answer = err.status_code
            listed = await main.session_service.list_sessions(
                app_name=main.APP_NAME, user_id="alex")
            return answer, len(listed.sessions)

    # The three content types a CORS *simple* request may carry, plus no
    # content type at all. Every one of them is a request some page on the
    # home network can make the browser send us unpreflighted, so every one of
    # them has to bounce — and bounce with the conversation still there.
    for kind in (None, "text/plain", "application/x-www-form-urlencoded",
                 "multipart/form-data; boundary=x"):
        answer, left = await forget(kind)
        named = kind or "no content type at all"
        check(f"a request a browser sends unasked is refused: {named}",
              answer, 415)
        check("and the conversation is still there afterwards", left, 1,
              "a guard that runs after the delete loop is not a guard")

    # Ours. `app.js` sends exactly the first of these; the other three are the
    # same header as a browser or a proxy is entitled to spell it. The last is
    # the one with space around the semicolon, which RFC 9110's `parameters`
    # rule allows and a hand-rolled split does not.
    for kind in ("application/json", "application/json; charset=utf-8",
                 "APPLICATION/JSON", "application/json ; charset=utf-8"):
        answer, left = await forget(kind)
        check(f"and our own page is not: {kind}", answer, {"forgotten": 1})
        check("with nothing of theirs left behind", left, 0)

    # The guard only works because the preflight it forces goes unanswered.
    # Register a CORS middleware with a permissive `allow_headers` and the
    # browser is told `application/json` is fine to send cross-origin: the
    # guard stays, stays green, and stops guarding anything.
    added = [m.cls.__name__ for m in main.app.user_middleware]
    check("and nothing answers the preflight the guard relies on",
          [m for m in added if "CORS" in m], [],
          f"middleware: {added or 'none'}")

    # The store #206 was about is not the only one the button promises to
    # reach. A refused request must leave the cached documents where they are
    # for exactly the reason it must leave the sessions: a guard that runs
    # after the deletion is not a guard.
    doc = Path(scratch_cache.name) / "letter.md"
    doc.write_text("---\nsource: \"gws drive files export\"\n"
                   "fetched: \"2026-09-11T00:00:00+09:00\"\n---\n\nbody\n",
                   encoding="utf-8")
    await forget(None)
    check("a refused request leaves the cached documents alone", doc.exists(),
          True, "a guard that runs after the delete loop is not a guard")
    await forget("application/json")
    check("and our own page's request takes them with the conversations",
          doc.exists(), False)

    # An in-flight turn appending an event to a session that /api/forget just deleted (#431).
    with a_server_of_its_own(main):
        sess = await main.session_service.create_session(
            app_name=main.APP_NAME, user_id="alex", state={})
        await main.api_forget("alex", posted("application/json"))
        ev = Event(author="user", content=types.Content(role="user", parts=[types.Part(text="hello")]))
        res = await main.session_service.append_event(sess, ev)
        check("an in-flight event appended after /api/forget is dropped without SessionNotFoundError",
              res.author, "user",
              "concurrent /api/forget must not crash live flow via SessionNotFoundError (#431)")

    gws_cache.cache_dir = was_cache_dir
    scratch_cache.cleanup()


async def gws_auth_cases() -> None:
    print("\n== Google Workspace authentication endpoints ==")
    from fastapi import HTTPException
    from starlette.requests import Request
    from ai_terminal.live import main
    from ai_terminal.live.tools import _gws_cli

    # Route registrations
    routes = {r.path: r for r in main.app.routes}
    check("/api/gws/status is registered", "/api/gws/status" in routes, True)
    check("/api/gws/status is a GET route",
          "GET" in getattr(routes.get("/api/gws/status"), "methods", set()), True)
    check("/api/gws/login is registered", "/api/gws/login" in routes, True)
    check("/api/gws/login is a POST route",
          "POST" in getattr(routes.get("/api/gws/login"), "methods", set()), True)
    check("/api/gws/logout is registered", "/api/gws/logout" in routes, True)
    check("/api/gws/logout is a POST route",
          "POST" in getattr(routes.get("/api/gws/logout"), "methods", set()), True)
    check("/api/gws/cancel is registered", "/api/gws/cancel" in routes, True)
    check("/api/gws/cancel is a POST route",
          "POST" in getattr(routes.get("/api/gws/cancel"), "methods", set()), True)

    def make_post(path: str, kind: str | None) -> Request:
        headers = [] if kind is None else [(b"content-type", kind.encode())]
        return Request({
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": path, "raw_path": path.encode(),
            "root_path": "", "query_string": b"", "headers": headers,
            "server": ("terminal", 8000), "client": ("10.0.0.9", 4242),
        })

    # Content-type guard for login, logout, cancel: refuse non-JSON
    for endpoint, fn in (("/api/gws/login", main.api_gws_login),
                         ("/api/gws/logout", main.api_gws_logout),
                         ("/api/gws/cancel", main.api_gws_cancel)):
        for kind in (None, "text/plain", "application/x-www-form-urlencoded",
                     "multipart/form-data; boundary=x"):
            req = make_post(endpoint, kind)
            status_code = None
            try:
                await fn(req)
            except HTTPException as err:
                status_code = err.status_code
            named = kind or "no content type"
            check(f"{endpoint} refuses unprompted {named}", status_code, 415)

    # Status endpoint checks
    real_avail = _gws_cli.available
    real_run = _gws_cli.run_json
    try:
        # 1. When CLI is not available
        _gws_cli.available = lambda: False
        res = await main.api_gws_status()
        check("status when CLI missing reports unavailable",
              res, {"available": False, "connected": False, "expired": False, "account": None, "scopes": []})

        # 2. When CLI is available but not connected
        _gws_cli.available = lambda: True
        _gws_cli.run_json = lambda *a, **k: {"auth_method": "none"}
        res = await main.api_gws_status()
        check("status when disconnected reports not connected",
              res, {"available": True, "connected": False, "expired": False, "account": None, "scopes": []})

        # 3. When CLI is connected
        _gws_cli.run_json = lambda *a, **k: {
            "email": "user@example.com",
            "token_valid": True,
            "scopes": ["https://www.googleapis.com/auth/calendar"],
        }
        res = await main.api_gws_status()
        check("status when connected returns account and scopes",
              res, {
                  "available": True,
                  "connected": True,
                  "expired": False,
                  "account": "user@example.com",
                  "scopes": ["https://www.googleapis.com/auth/calendar"],
              })
        check("status exposes no token-shaped keys",
              set(res.keys()), {"available", "connected", "expired", "account", "scopes"})
    finally:
        _gws_cli.available = real_avail
        _gws_cli.run_json = real_run

    # Login endpoint with application/json
    real_popen = subprocess.Popen

    class FakeLoginProc:
        def __init__(self, *args, **kwargs):
            import io
            self.stderr = io.StringIO("Open URL: https://accounts.google.com/o/oauth2/auth?test=1\n")
            self.stdout = io.StringIO()
            self.returncode = 0

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

    try:
        subprocess.Popen = FakeLoginProc
        _gws_cli.available = lambda: True
        login_req = make_post("/api/gws/login", "application/json")
        login_res = await main.api_gws_login(login_req)
        check("login endpoint returns consent url",
              login_res.get("url"),
              "https://accounts.google.com/o/oauth2/auth?test=1")
        check("login endpoint also provides consent_url key",
              login_res.get("consent_url"),
              "https://accounts.google.com/o/oauth2/auth?test=1")
    finally:
        subprocess.Popen = real_popen
        _gws_cli.available = real_avail

    # Logout endpoint with application/json
    real_sub_run = subprocess.run
    try:
        class FakeCompleted:
            returncode = 0
            stdout = '{"status": "success"}'
            stderr = ""

        subprocess.run = lambda *a, **k: FakeCompleted()
        _gws_cli.available = lambda: True
        logout_req = make_post("/api/gws/logout", "application/json")
        logout_res = await main.api_gws_logout(logout_req)
        check("logout endpoint returns status ok",
              logout_res.get("status"), "ok")
    finally:
        subprocess.run = real_sub_run
        _gws_cli.available = real_avail

    # Cancel endpoint with application/json
    try:
        _gws_cli.available = lambda: True
        cancel_req = make_post("/api/gws/cancel", "application/json")
        cancel_res = await main.api_gws_cancel(cancel_req)
        check("cancel endpoint returns status ok",
              cancel_res.get("status"), "ok")
        check("cancel endpoint returns cancelled True",
              cancel_res.get("cancelled"), True)
    finally:
        _gws_cli.available = real_avail


async def gws_ephemeral_auth_cases() -> None:
    print("\n== gws ephemeral auth browser launcher (#642) ==")
    from ai_terminal.live.tools import gws as gws_tools

    called_cmds = []

    class FakeBrowserProc:
        def __init__(self, cmd, **kwargs):
            called_cmds.append(cmd)
            self.returncode = None

        def poll(self):
            return None

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            pass

    real_popen = subprocess.Popen
    try:
        subprocess.Popen = FakeBrowserProc
        test_url = "https://accounts.google.com/o/oauth2/auth?test=ephemeral"
        proc, pdir = gws_tools.launch_ephemeral_auth_browser(test_url)

        check("ephemeral browser directory exists in /tmp",
              pdir.exists() and str(pdir).startswith("/tmp/gws-auth-"), True)
        check("ephemeral browser command launches with --app",
              any(f"--app={test_url}" in arg for arg in called_cmds[0]), True)
        check("ephemeral browser command specifies --ozone-platform=wayland",
              "--ozone-platform=wayland" in called_cmds[0], True)
        check("ephemeral browser command specifies isolated --user-data-dir",
              f"--user-data-dir={pdir}" in called_cmds[0], True)
        check("ephemeral browser command enables touch events",
              "--touch-events=enabled" in called_cmds[0], True)

        gws_tools.cleanup_ephemeral_auth(proc, pdir)
        check("cleanup wipes ephemeral profile directory", pdir.exists(), False)
    finally:
        subprocess.Popen = real_popen



# ------------------------------------------ the wake word and what it opens

class WakeRunner:
    """A Runner whose live stream stays open until something cancels it.

    `FakeRunner` above ends the moment it is started, which is the wrong shape
    for the cases below: an arm has to leave a session running for a disarm to
    have anything to close, and for a cancellation to be visible at all.
    """

    def __init__(self, fails: bool = False, ends: bool = False):
        self.calls: list[dict] = []
        self.started = asyncio.Event()
        self.cancelled = 0
        # A stream that ends by itself instead: the 429, the key that expired
        # overnight, the WAN down. Nobody awaits the task it runs in, so what
        # happens next is entirely up to the done callback.
        self.fails = fails
        self.ends = ends

    async def run_live(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        if self.fails:
            raise RuntimeError("the key expired overnight")
        if self.ends:
            if False:
                yield
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        yield  # pragma: no cover - makes this an async generator


class Heard(logging.Handler):
    """Every line the endpoint logged, for the cases that are about a log.

    Two things the socket does on a failure are visible nowhere else: it says
    a session ended, and it says a frame arrived with nothing armed. Both are
    the whole of the fix — a terminal that silently drops questions is the
    state this is meant to get out of.
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def wake_cases() -> None:
    print("\n== the wake word, and the microphone it opens ==")

    # Local, the way `main` is above: only this section needs any of the four,
    # and `main` in particular reads `.env` and builds the agent at import.
    import inspect

    from fastapi import HTTPException
    from starlette.requests import Request

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    def waking(kind: str | None):
        """A `POST /api/wake` carrying this content type and nothing else.

        Built from a scope rather than driven through a `TestClient`, because
        starting the app is what the preamble of this file spends its effort
        avoiding, and the guard reads one header and nothing else.
        """
        headers = [] if kind is None else [(b"content-type", kind.encode())]
        return Request({"type": "http", "method": "POST",
                        "path": "/api/wake", "headers": headers,
                        "query_string": b""})

    async def wake(kind: str = "application/json"):
        """One knock at the endpoint, with every refusal reported as a value.

        Including the endpoint not being there at all, so a run against a tree
        where this is not wired up names the missing wiring in each case
        instead of dying in an AttributeError under the first one. Anything
        else that escapes is reported the same way: the monitor on the Pi gets
        a 500 for a screen that tore itself down mid-push, and a case has to be
        able to say so rather than take the suite down with it.
        """
        route = getattr(main, "api_wake", None)
        if route is None:
            return "no /api/wake endpoint"
        try:
            return await route(waking(kind))
        except HTTPException as refused:
            return refused.status_code
        except Exception as err:  # pylint: disable=broad-except
            return f"{type(err).__name__}: {err}"

    frames: dict[str, list[dict]] = {}
    takes = inspect.signature(screen.register).parameters

    def a_screen(session_id: str, then=None, waits=None, closed=None) -> None:
        """Register a screen that records what it is told, and unregister it
        again through `leave`.

        `waits` is a page that has stopped reading — a suspended laptop, a tab
        at TCP zero-window. It neither takes the frame nor raises, which is the
        one shape the `then=boom` cases below cannot stand in for and the one a
        `try` cannot cover.

        `closed` is a list this session's name goes into when the server gives
        up on the page and closes its socket. Handed to `register` only on a
        tree whose `register` takes it, so a tree without it fails the case
        about it and then goes on failing the rest on their own terms.
        """
        frames[session_id] = []

        async def sink(frame: dict) -> None:
            if then is not None:
                then()
            if waits is not None:
                await waits.wait()
            frames[session_id].append(frame)

        async def close() -> None:
            closed.append(session_id)

        if closed is not None and "close" in takes:
            screen.register(session_id, sink, False, close)
        else:
            screen.register(session_id, sink)

    def leave(*session_ids: str) -> None:
        for session_id in session_ids:
            screen.unregister(session_id)

    async def settled(turns: int = 200) -> None:
        """Let every writer that is not blocked finish what it is holding.

        Turns of the loop and not a `sleep`, so nothing here depends on how
        fast this machine is: a page that is blocked stays blocked however many
        turns go by, which is what the cases below are about.
        """
        for _ in range(turns):
            await asyncio.sleep(0)

    async def knocking():
        """One `POST /api/wake` in a task, so a case can read it before it
        answers — or say that it has not answered at all."""
        return asyncio.create_task(wake())

    async def done_with(task) -> None:
        """End a knock that may still be waiting on a page that never reads."""
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # First, because everything below it goes through this function and a
    # missing endpoint is worth one line saying so rather than sixteen.
    route = getattr(main, "api_wake", None)
    check("the monitor has an endpoint to post a detection to",
          [callable(route),
           list(inspect.signature(route).parameters) if route else None],
          [True, ["request"]],
          "`api_wake(request)` — it has a header to read, so it takes the "
          "request itself")
    check("registered as a POST on the path the monitor is pointed at",
          sorted(next((r.methods for r in main.app.routes
                       if getattr(r, "path", "") == "/api/wake"), ())),
          ["POST"], "`wake --webhook http://localhost:8000/api/wake`")

    a_screen("w-1")
    try:
        # `--host` defaults to 0.0.0.0 and this endpoint opens a microphone in
        # somebody's kitchen, so what it costs to reach is the whole point:
        # none of these four is preflighted, which means a page on the home
        # network can send them and never need to read the answer.
        check("a request a browser would send across origins unasked is "
              "refused",
              [await wake(kind) for kind in
               (None, "text/plain", "application/x-www-form-urlencoded",
                "multipart/form-data; boundary=x")],
              [415] * 4, "no header, then the three a simple request may "
              "carry")
        refused = list(frames["w-1"])
        check("while every spelling of `application/json` a client may "
              "actually send is let through",
              [await wake(kind) for kind in
               ("application/json", "application/json; charset=utf-8",
                "Application/JSON")],
              [{"armed": 1}] * 3, "parsed rather than compared")
        # Both halves in one case, because the empty list on its own is what a
        # server with no endpoint at all also produces. What it has to mean is
        # that this screen was reachable the whole time and the refusals chose
        # not to reach it.
        check("and nothing was pushed on the way to refusing the others",
              [refused, frames["w-1"]], [[], [{"type": "wake"}] * 3],
              "the guard runs before the first push")
    finally:
        leave("w-1")

    check("with nobody at the terminal it says so instead of failing",
          await wake(), {"armed": 0}, "a kiosk that has not loaded the page")

    a_screen("w-1")
    a_screen("w-2")
    try:
        check("every screen up is told, because the monitor knows a phrase "
              "was said and nothing about who is talking to what",
              [await wake(), frames["w-1"], frames["w-2"]],
              [{"armed": 2}, [{"type": "wake"}], [{"type": "wake"}]])
    finally:
        leave("w-1", "w-2")

    def boom() -> None:
        raise RuntimeError("this screen closed a moment ago")

    a_screen("w-1", then=boom)
    a_screen("w-2")
    try:
        # The monitor keeps listening whatever the terminal is doing, so a
        # dead socket must not come back to it as a failed webhook.
        check("a screen that has gone away costs the monitor nothing, and "
              "costs the screen beside it nothing either",
              [await wake(), frames["w-2"]],
              [{"armed": 2}, [{"type": "wake"}]])
    finally:
        leave("w-1", "w-2")

    a_screen("w-1", then=lambda: screen.unregister("w-1"))
    a_screen("w-2")
    try:
        check("nor does one that tears itself down while being written to",
              [await wake(), frames["w-2"]],
              [{"armed": 2}, [{"type": "wake"}]],
              "the push loop walks a copy, not the registry")
    finally:
        leave("w-1", "w-2")

    # --------------------------- and the sessions behind the one that stalled

    # `/api/wake` walks every session in turn, so what one page costs the
    # endpoint is what it costs every session after it. The two cases above
    # hold the shape where a socket *raises*; the shape they cannot stand in
    # for is a page that neither takes the frame nor raises — a suspended
    # laptop, a tab at TCP zero-window — because nothing about it is an
    # exception and no `try` covers it. `roll_fanout_cases` cannot see it
    # either: both its pages are on one session, and this is a crossing
    # between two.
    #
    # The stalled page is on the session registered first, which is the order
    # `screen.sessions()` walks: the laptop that has been open since morning,
    # and then the panel in the kitchen. Both halves are asserted while the
    # page is still stalled, because a case that first lets it read is a case
    # that measures nothing.
    asleep = asyncio.Event()
    a_screen("w-1", waits=asleep)
    a_screen("w-2")
    knock = await knocking()
    try:
        await settled()
        landed = list(frames["w-2"])
        answered = (knock.done(), knock.result() if knock.done() else None)
        check("a page that has stopped reading does not hold the wake word "
              "off the sessions behind it",
              landed, [{"type": "wake"}],
              "one laptop asleep in another room and the kiosk in the kitchen "
              "never hears the phrase, with nothing anywhere saying so")
        check("and the monitor is answered without waiting on a socket at all",
              answered, (True, {"armed": 2}),
              "`wake.py` gives up on the webhook after `WEBHOOK_TIMEOUT`, so "
              "a POST held open by a page in another room is a detection the "
              "monitor writes off as failed")
    finally:
        asleep.set()
        await settled()
        await done_with(knock)
        leave("w-1", "w-2")

    # The bound that comes with the queue, and the half that makes the fix
    # last: until the wake frame went through the writer, none of #138's
    # dropping reached this path, so the sleeping laptop stayed registered and
    # was in front of the *next* wake word too, and the one after that.
    #
    # The one wall-clock assertion in this section, because a deadline cannot
    # be measured in turns of the loop. The margin is thirty to one and it is a
    # deadline being exceeded rather than met, so a slow machine makes this
    # more true and not less.
    shut: list[str] = []
    forever = asyncio.Event()
    was, screen.DEADLINE_S = screen.DEADLINE_S, 0.01
    a_screen("w-1", waits=forever, closed=shut)
    a_screen("w-2")
    knock = await knocking()
    try:
        await asyncio.sleep(0.3)
        await settled()
        check("and one that will not take the wake frame inside the bound is "
              "dropped and its socket closed",
              [shut, "w-1" in screen.sessions(), frames["w-2"]],
              [["w-1"], False, [{"type": "wake"}]],
              "otherwise the same wedged socket stands in front of every wake "
              "word after this one, for as long as the browser is open")
    finally:
        forever.set()
        await settled()
        screen.DEADLINE_S = was
        await done_with(knock)
        leave("w-1", "w-2")

    # One queue per page and not a lane of its own for the wake word, which is
    # the other way to write this and the way that reorders the socket. A page
    # reads its frames in the order they were written: a wake frame that
    # overtook the card in front of it opens the microphone on a panel whose
    # card is still spinning, and the `browse` `done` that stops it arrives
    # afterwards.
    heard: list[str] = []

    async def unhurried(frame: dict) -> None:
        """A page that is reading, just not instantly — one card's worth of
        socket, which is where a second lane would overtake."""
        if frame["type"] != "wake":
            await asyncio.sleep(0.05)
        heard.append(frame["type"])

    screen.register("w-3", unhurried)
    try:
        await screen.push("w-3", {"type": "gws", "id": "c-1"})
        check("and the wake frame is queued behind the card already on its "
              "way to that page, not in front of it",
              [await wake(), heard], [{"armed": 1}, []],
              "handed over rather than delivered, so neither is on the socket "
              "yet")
        await asyncio.sleep(0.3)
        await settled()
        check("which is the order the page reads them in",
              heard, ["gws", "wake"],
              "a second writer for the wake word puts it on the socket ahead "
              "of the card the person is waiting to see")
    finally:
        await settled()
        leave("w-3")

    # Read on one line at import and shipped on one more, the way `REVISION`
    # is below. Drop either and the page keeps its own thirty seconds with
    # nothing anywhere to say the `.env` was ignored.
    was = getattr(main, "WAKE_IDLE_S", None)
    main.WAKE_IDLE_S = 12.5
    try:
        check("the page is told how long to stay armed for",
              (await main.api_config()).get("wake_idle_s"), 12.5)
    finally:
        main.WAKE_IDLE_S = was

    # ------------------------------- which of two screens the frame lands on

    # What the flag on the handshake is allowed to say. The unit writes `1`;
    # `0` is how a page turns it off without editing the URL down, and an
    # empty value is what an operator halfway through editing the unit leaves
    # behind. Neither is a claim to be the kiosk, and neither is a value
    # nobody wrote.
    for asked, want in (({"kiosk": "1"}, True), ({"kiosk": " 1 "}, True),
                        ({"kiosk": "yes"}, True), ({"kiosk": "0"}, False),
                        ({"kiosk": ""}, False), ({"kiosk": "  "}, False),
                        ({}, False)):
        said = asked.get("kiosk", "(nothing)")
        check(f"a handshake saying {said!r} "
              f"{'is' if want else 'is not'} the kiosk saying so",
              main._is_kiosk(asked), want)

    # And the wire between that reading and `screen.wake_sink`, which
    # `history_check` measures on its own. The flag has to come off the query
    # string, through `_is_kiosk`, and into the `register` call — left out of
    # that one argument the panel is registered as an ordinary page, #132 is
    # back, and every case in either file still holds.
    async def beside(**params) -> dict:
        """Open a second page on the session a real connection just made.

        From inside the connection, because a registration is the endpoint's
        `finally` away from being gone and the question is about the pair.
        """
        picked: dict = {}

        async def during() -> dict:
            ids = screen.sessions()
            picked["alone"] = len(ids) == 1

            async def mac_tab(frame):
                pass

            screen.register(ids[-1], mac_tab)
            picked["wake"] = screen.wake_sink(ids[-1]) is not mac_tab
            picked["card"] = screen.sink(ids[-1]) is mac_tab
            screen.unregister(ids[-1], mac_tab)
            return {"type": "websocket.disconnect"}

        socket = FakeSocket([during], **params)
        with a_server_of_its_own(main):
            main.runner = WakeRunner()
            await asyncio.wait_for(
                main.websocket_endpoint(socket, "user-wake"), 10)
        return picked

    check("a socket opened at ?kiosk=1 keeps the wake word when a tab opens "
          "in front of it, while the card goes to the tab",
          await beside(kiosk="1"),
          {"alone": True, "wake": True, "card": True},
          "the flag has to reach `screen.register`, not only be read")
    check("and one opened without it hands both to the newer page",
          await beside(), {"alone": True, "wake": False, "card": True})

    # ------------------------------------ the socket that outlives a session

    quiet = FakeSocket([])
    with a_server_of_its_own(main):
        # That helper installs a runner which ends the moment it starts, and
        # restores the real one whatever is here when the block closes.
        spy = main.runner = WakeRunner()
        await asyncio.wait_for(
            main.websocket_endpoint(quiet, "user-wake"), 10)
    check("a page that has only just loaded holds no session at all",
          [quiet.accepted, len(spy.calls)], [True, 0],
          "the socket is up so the wake word can reach it; `run_live` is not")

    seen: dict = {}

    def frame(payload: dict) -> dict:
        return {"type": "websocket.receive", "text": json.dumps(payload)}

    async def wake_arrives() -> dict:
        # The whole chain in the order it happens: the monitor posts, the
        # endpoint finds this connection's screen, and the frame comes out of
        # the socket the page is reading — with no session open underneath.
        seen["reply"] = await wake()
        # The `ready` the accept path sent is in front of it and stays in the
        # list: what this case is about is the order the page reads them in,
        # and a filter here would hide a wake frame that overtook the hello.
        seen["heard"] = [json.loads(text) for text in talking.sent]
        return frame({"type": "arm"})

    async def after_arm() -> dict:
        await asyncio.wait_for(live.started.wait(), 10)
        live.started.clear()
        seen["armed"] = len(live.calls)
        seen["session"] = live.calls[-1].get("session_id")
        return frame({"type": "arm"})

    async def after_second_arm() -> dict:
        # The page means it twice on purpose — a socket that reconnects under
        # a microphone that is still on arms again — and a second session here
        # is not a spare: it replaces the reference, so the first is left
        # running with nothing able to cancel it, answering into the same
        # screen as the second until the socket closes.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(live.started.wait(), 0.5)
        seen["twice"] = len(live.calls)
        return frame({"type": "disarm"})

    async def after_disarm() -> dict:
        seen["cancelled"] = live.cancelled
        seen["screen"] = screen.sink(seen["session"]) is not None
        return {"type": "websocket.receive", "bytes": b"\x00\x00"}

    async def after_audio() -> dict:
        # Reached at all only if the block above was dropped rather than
        # pushed into a queue that is not there.
        seen["survived"] = True
        return frame({"type": "arm"})

    async def after_rearm() -> dict:
        await asyncio.wait_for(live.started.wait(), 10)
        seen["again"] = len(live.calls)
        seen["same"] = live.calls[-1].get("session_id") == seen["session"]
        return {"type": "websocket.disconnect"}

    talking = FakeSocket([wake_arrives, after_arm, after_second_arm,
                          after_disarm, after_audio, after_rearm])
    with a_server_of_its_own(main):
        live = main.runner = WakeRunner()
        await asyncio.wait_for(
            main.websocket_endpoint(talking, "user-wake"), 10)

    check("the wake word reaches a page that is holding nothing open",
          [seen.get("reply"), seen.get("heard")],
          [{"armed": 1}, [{"type": "ready"}, {"type": "wake"}]],
          "monitor -> /api/wake -> screen sink -> this socket")
    check("and it is the page's `arm` that opens the session, not the accept",
          seen.get("armed"), 1)
    check("a second `arm` under the same socket opens nothing further",
          seen.get("twice"), 1,
          "the wake word and the microphone button are one request")
    check("`disarm` closes it by cancelling, and leaves the screen up",
          [seen.get("cancelled"), seen.get("screen")], [1, True],
          "`queue.close()` reconnects instead — base_llm_flow.py:859-869")
    check("audio arriving while nothing is armed is dropped, not crashed on",
          seen.get("survived"), True,
          "the recorder posts a block every 8 ms, arm frame or no arm frame")
    check("and the next wake word opens a session on the same conversation",
          [seen.get("again"), seen.get("same")], [2, True],
          "#19's thread outlives the microphone going off and on")
    # The socket ended armed, which is the ordinary way a kiosk closes one: a
    # reload, or the browser going away while somebody is mid-sentence. The
    # `finally` is then the only thing left holding the model connection, and
    # a session it does not cancel is one nothing ever will — the tool
    # registries below it are cleared, so the answers it goes on producing
    # arrive for a screen that is gone.
    check("and the socket closing takes whatever is armed with it",
          live.cancelled, 2, "one from the `disarm`, one from the `finally`")

    # ---------------------------- a session that stopped without being asked

    heard = Heard()
    talker = logging.getLogger("ai_terminal.live.main")
    died: dict = {}

    async def after_failed_arm() -> dict:
        await asyncio.wait_for(dying.started.wait(), 10)
        dying.started.clear()
        died["session"] = dying.calls[-1].get("session_id")
        # The failure lands in the task, and the done callback that clears the
        # reference runs a turn after that.
        for _ in range(10):
            await asyncio.sleep(0)
        died["reported"] = [line for line in heard.lines if "ended:" in line]
        # And what the *page* was told, which until #269 was nothing at all.
        # The spinner goes up when the question is heard and comes down on
        # `turnComplete` or `interrupted` (`app.js:1128`, `:1132`), both of
        # them events out of `run_live` — so a session that died before either
        # leaves the kiosk spinning until somebody reloads it.
        died["frames"] = [json.loads(text) for text in unlucky.sent]
        return frame({"type": "text", "text": "is the bus late?"})

    async def after_typing() -> dict:
        # What a cleared reference looks like from outside: the frame is
        # dropped and said so. Left set, it is handed to the queue of a
        # session that is not running — the question disappears, the log
        # stays clean, and every audio block after it accumulates in a queue
        # nobody will ever read.
        died["dropped"] = [line for line in heard.lines
                           if "nothing is armed" in line]
        return frame({"type": "arm"})

    async def after_retry() -> dict:
        await asyncio.wait_for(dying.started.wait(), 10)
        died["again"] = len(dying.calls)
        return {"type": "websocket.disconnect"}

    async def first_arm() -> dict:
        return frame({"type": "arm"})

    unlucky = FakeSocket([first_arm, after_failed_arm, after_typing,
                          after_retry])
    # Off the root handler for this connection only: the report under test is
    # logged with `exc_info`, and a suite that prints a traceback in the
    # middle of a green run reads as one that broke.
    was_propagating, talker.propagate = talker.propagate, False
    talker.addHandler(heard)
    try:
        with a_server_of_its_own(main):
            dying = main.runner = WakeRunner(fails=True)
            await asyncio.wait_for(
                main.websocket_endpoint(unlucky, "user-wake"), 10)
    finally:
        talker.removeHandler(heard)
        talker.propagate = was_propagating

    check("a session that dies on its own is reported rather than swallowed",
          died.get("reported"),
          [f"live session for {died.get('session')} ended: "
           "the key expired overnight"],
          "a 429 or an expired key, and nobody awaiting the task to see it")
    check("and the terminal knows it is holding nothing again",
          died.get("dropped"), ["dropped a text frame: nothing is armed"])
    check("so the next wake word can try, which on a 429 is the right answer",
          died.get("again"), 2)
    check("and the page is told the turn is over, so the spinner comes down",
          [f for f in died.get("frames", []) if f.get("turnComplete")],
          [{"turnComplete": True}],
          "#269: the model sends no `turnComplete` from a session that died, "
          "and a kiosk nobody reloads spins on that forever")
    check("and the page is told to go idle, so the thirty-second deaf window is closed",
          [f for f in died.get("frames", []) if f.get("type") == "idle"],
          [{"type": "idle"}],
          "#285: ending a session signals the page to disarm and stop listening")

    # And a twin for the silent return. ADK 2.8.0 exits without raising when a
    # connection closes normally before a resumption handle was set, and the
    # symptom is the same: the model sends nothing, the task ends, and without
    # turn_over the kiosk spins forever.
    ended_frames: list = []
    dropped_after_end: list = []
    heard_clean = Heard()

    async def after_ended_arm() -> dict:
        await asyncio.wait_for(ending.started.wait(), 10)
        ending.started.clear()
        for _ in range(10):
            await asyncio.sleep(0)
        ended_frames.extend(json.loads(text) for text in lucky.sent)
        return frame({"type": "text", "text": "any mail today?"})

    async def after_clean_typing() -> dict:
        dropped_after_end.extend(line for line in heard_clean.lines
                                 if "nothing is armed" in line)
        return {"type": "websocket.disconnect"}

    lucky = FakeSocket([first_arm, after_ended_arm, after_clean_typing])
    was_propagating, talker.propagate = talker.propagate, False
    talker.addHandler(heard_clean)
    try:
        with a_server_of_its_own(main):
            ending = main.runner = WakeRunner(ends=True)
            await asyncio.wait_for(
                main.websocket_endpoint(lucky, "user-clean-end"), 10)
    finally:
        talker.removeHandler(heard_clean)
        talker.propagate = was_propagating

    check("a session that ends cleanly without raising drops the spinner too",
          [f for f in ended_frames if f.get("turnComplete")],
          [{"turnComplete": True}],
          "#269: ADK silent return paths leave task.exception() as None")
    check("and a clean end signals idle as well",
          [f for f in ended_frames if f.get("type") == "idle"],
          [{"type": "idle"}],
          "#285: clean end signals idle so the next wake word is heard")
    check("and the clean end drops the session reference as well",
          dropped_after_end, ["dropped a text frame: nothing is armed"])

    # And the third ending: session taken over by another page.
    # When a laptop tab on the same profile arms, take_over cancels the kiosk's
    # Live session. The loser's done-callback must not raise CancelledError on
    # task.exception(), must send turnComplete and idle so the kiosk stops
    # listening and does not stay deaf for thirty seconds, and must drop
    # subsequent frames with the armed-state warnings. #291.
    taken_frames: list = []
    woken_frames: list = []
    dropped_after_takeover: list = []
    heard_takeover = Heard()
    kiosk_connected = asyncio.Event()
    kiosk_armed = asyncio.Event()
    tab_armed = asyncio.Event()
    kiosk_done = asyncio.Event()

    async def kiosk_first_arm() -> dict:
        kiosk_connected.set()
        return frame({"type": "arm"})

    async def kiosk_after_takeover() -> dict:
        await asyncio.wait_for(taking.started.wait(), 10)
        taking.started.clear()
        kiosk_armed.set()
        await asyncio.wait_for(tab_armed.wait(), 10)
        for _ in range(10):
            await asyncio.sleep(0)
        taken_frames.extend(json.loads(text) for text in taken.sent)
        return frame({"type": "text", "text": "is anyone there?"})

    async def kiosk_after_audio() -> dict:
        dropped_after_takeover.extend(line for line in heard_takeover.lines
                                      if "nothing is armed" in line)
        return {"type": "websocket.receive", "bytes": b"\x00\x00"}

    async def kiosk_after_wake() -> dict:
        dropped_after_takeover.extend(line for line in heard_takeover.lines
                                      if "nothing is armed" in line
                                      and line not in dropped_after_takeover)
        await wake()
        for _ in range(10):
            await asyncio.sleep(0)
        woken_frames.extend(json.loads(text) for text in taken.sent)
        kiosk_done.set()
        return {"type": "websocket.disconnect"}

    async def tab_arm() -> dict:
        await asyncio.wait_for(kiosk_armed.wait(), 10)
        return frame({"type": "arm"})

    async def tab_settle() -> dict:
        await asyncio.wait_for(taking.started.wait(), 10)
        tab_armed.set()
        await asyncio.wait_for(kiosk_done.wait(), 10)
        return {"type": "websocket.disconnect"}

    taken = FakeSocket([kiosk_first_arm, kiosk_after_takeover,
                        kiosk_after_audio, kiosk_after_wake], kiosk="1")
    usurper = FakeSocket([tab_arm, tab_settle])
    was_propagating, talker.propagate = talker.propagate, False
    talker.addHandler(heard_takeover)
    try:
        with a_server_of_its_own(main):
            taking = main.runner = WakeRunner()
            k_task = asyncio.create_task(
                main.websocket_endpoint(taken, "user-takeover"))
            await asyncio.wait_for(kiosk_connected.wait(), 10)
            t_task = asyncio.create_task(
                main.websocket_endpoint(usurper, "user-takeover"))
            await asyncio.wait_for(asyncio.gather(k_task, t_task), 10)
    finally:
        talker.removeHandler(heard_takeover)
        talker.propagate = was_propagating

    check("a session taken over by another page drops the spinner too",
          [f for f in taken_frames if f.get("turnComplete")],
          [{"turnComplete": True}],
          "#269: takeover cancels the task, and the losing page needs turnComplete")
    check("and a taken-over session signals idle as well",
          [f for f in taken_frames if f.get("type") == "idle"],
          [{"type": "idle"}],
          "#291: taken-over page is told to go idle so the deaf window is closed")
    check("and the cancellation is not logged as an unexpected failure",
          [line for line in heard_takeover.lines if "ended:" in line],
          [],
          "#291: takeover is a deliberate cancellation, not a crash")
    check("and the taken-over page drops subsequent frames",
          dropped_after_takeover,
          ["dropped a text frame: nothing is armed",
           "dropped an audio frame: nothing is armed"])
    check("and hears the next wake word without waiting out WAKE_IDLE_S",
          [f for f in woken_frames if f.get("type") == "wake"],
          [{"type": "wake"}],
          "#291: kiosk receives wake word immediately after takeover")
    check("while a page that cancelled its own session sends no idle frame",
          [f for f in [json.loads(t) for t in usurper.sent] if f.get("type") == "idle"],
          [],
          "#291: disarm and teardown do not send idle")

    # ------------------------------ and the gap that socket has to outlive too

    # `_thread` runs at accept, and a socket that stays up for days accepts
    # once. So the gap has to be asked about again where the conversation
    # actually restarts, which is `arm` — otherwise Tuesday morning's thread
    # is still the context on Thursday night and nothing says so.
    gap = main.IDLE_GAP_S
    STALE = "They were planning a trip to Kanazawa, and had not booked."
    carried: list = []
    rolling: dict = {}

    def registries(sid: str) -> list[bool]:
        """Whether all four of this connection's registries are on `sid`.

        A tool asks `screen.session_of` which session it is drawing on and
        gets `tool_context.session.id`, which is the new thread from the next
        event onwards. So a roll that moves the conversation and leaves these
        behind sends every card to a session with no socket on the end of it.
        """
        return [sid in browse._JOBS, sid in gws._RUNNING,
                sid in plan_mod._RUNNING, screen.sink(sid) is not None]

    async def arms() -> dict:
        return frame({"type": "arm"})

    async def armed_once() -> dict:
        await asyncio.wait_for(rolls.started.wait(), 10)
        rolls.started.clear()
        rolling["first"] = rolls.calls[-1].get("session_id")
        # What the last accept left behind. `_carry_forward` writes exactly
        # this on the path that does cross the gap, and its key is
        # `user:`-prefixed, so it is filed under the person and outlives the
        # row it was written on. Seeded here rather than mocked because the
        # whole question is what the *store* hands the next thread.
        seeded = await main.session_service.get_session(
            app_name=main.APP_NAME, user_id="user-gap",
            session_id=rolling["first"])
        await main.session_service.append_event(seeded, Event(
            author="user", actions=EventActions(
                state_delta={main.recap.RECAP_KEY: STALE})))
        # Quiet for longer than the gap, without spending the gap waiting.
        main.IDLE_GAP_S = -1.0
        return frame({"type": "arm"})

    async def armed_again_live() -> dict:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(rolls.started.wait(), 0.5)
        rolling["twice"] = len(rolls.calls)
        rolling["held"] = registries(rolling["first"])
        return frame({"type": "disarm"})

    async def armed_after_gap() -> dict:
        await asyncio.wait_for(rolls.started.wait(), 10)
        rolling["second"] = rolls.calls[-1].get("session_id")
        rolling["moved"] = registries(rolling["second"])
        rolling["left"] = registries(rolling["first"])
        # Read back out of the store, not off the object the roll happened to
        # be holding: what matters is what `build_agent` sees when it builds
        # the instruction, and that comes from a load.
        rolled_to = await main.session_service.get_session(
            app_name=main.APP_NAME, user_id="user-gap",
            session_id=rolling["second"])
        rolling["recap"] = main.recap.recalled(rolled_to)
        rolling["kept"] = (rolled_to.state or {}).get("home_location")
        return {"type": "websocket.disconnect"}

    # With an address on the handshake, because the clear below is aimed at
    # one key and a clear that took the rest of `state` with it would trade
    # this bug for a worse one — an agent that no longer knows where it is.
    patient = FakeSocket([arms, armed_once, armed_again_live, arms,
                          armed_after_gap], location="Hakone, Japan")
    try:
        # Recorded and not called. What this case is about is that it must not
        # happen here at all: `recap.summarise` is a real model call, and the
        # wake word is the worst trigger there is for one (#287).
        with a_server_of_its_own(main), stub_carry(main, recorder=carried):
            rolls = main.runner = WakeRunner()
            await asyncio.wait_for(
                main.websocket_endpoint(patient, "user-gap"), 10)
    finally:
        main.IDLE_GAP_S = gap

    check("a wake word after the gap starts a new conversation",
          [rolling.get("second") != rolling.get("first"),
           bool(rolling.get("second"))], [True, True],
          f"{rolling.get('first')} -> {rolling.get('second')}")
    check("and the screen and all three tool registries move with it",
          [rolling.get("moved"), rolling.get("left")],
          [[True] * 4, [False] * 4],
          "browse, gws, plan, screen — a tool reads the new id off ADK")
    check("and it does not buy a recap on the way in",
          carried, [],
          "`_carry_forward` is a model call; the wake word must not wait")
    check("but a second `arm` under a live session does not move it",
          [rolling.get("twice"), rolling.get("held")], [1, [True] * 4],
          "rolling out from under a conversation is the same bug reversed")
    # The recap survives the session it was written on — that is what its
    # `user:` prefix is for — so "rolls bare" is a claim about state the roll
    # has to make true, not one the new row gives it for free. Left alone, the
    # instruction on the fresh thread opens with "What was said before" and
    # then quotes a conversation from before the last one, which the agent is
    # told is its own note about this person.
    check("and the thread it rolls to carries no stale recap",
          rolling.get("recap"), "",
          "`user:` state outlives the session; a days-old note is worse "
          "than none")
    check("without taking the handshake state with it",
          rolling.get("kept"), "Hakone, Japan",
          "one key cleared, not the address the agent answers from")

    # --------------- and without taking the other page's recap with it either
    #
    # Two pages on one profile, on two threads: `_thread` runs at accept and
    # rolls whatever it finds stale, but leaves any mid-conversation page
    # alone — so the kiosk mid-conversation holds Sunday's id while the tab
    # that has just been opened holds the thread the roll made, with the
    # summary `_carry_forward` bought on the way. Then somebody says the wake
    # word in the kitchen. The roll it triggers is the kiosk's business; the
    # summary under `user:last_summary` is the whole profile's, and writing
    # `""` into it to say "this thread is bare" says it about the tab as well.
    STILL = ("They asked you to move the piano lesson to Thursday and to "
             "check whether the hall is free.")
    pair: dict = {}
    was_carry = main._carry_forward

    async def carry_a_summary(user_id, rolled_id, session) -> None:
        """`_carry_forward` with its one Gemini call replaced by a constant.

        The write is the real one — same key, same state-only event, same
        store. What is stubbed out is `recap.summarise`, which is a model call
        and is not what these cases are about.
        """
        await main.session_service.append_event(session, Event(
            author="user", actions=EventActions(
                state_delta={main.recap.RECAP_KEY: STILL})))

    async def recap_on(session_id: str) -> str:
        """What `_instruction` would read on that thread, out of the store."""
        got = await main.session_service.get_session(
            app_name=main.APP_NAME, user_id="user-pair", session_id=session_id)
        return main.recap.recalled(got) if got is not None else "<gone>"

    async def armed_on() -> str:
        await asyncio.wait_for(pages.started.wait(), 10)
        pages.started.clear()
        return pages.calls[-1].get("session_id")

    kiosk_live = asyncio.Event()
    kiosk_arms = asyncio.Event()
    kiosk_rolled = asyncio.Event()

    async def kiosk_arm_first() -> dict:
        return frame({"type": "arm"})

    async def kiosk_wait_live() -> dict:
        kiosk_live.set()
        await asyncio.wait_for(kiosk_arms.wait(), 10)
        return frame({"type": "disarm"})

    async def kiosk_roll() -> dict:
        return frame({"type": "arm"})

    async def kiosk_done() -> dict:
        pair["kiosk"] = await armed_on()
        pair["kiosk_recap"] = await recap_on(pair["kiosk"])
        kiosk_rolled.set()
        return {"type": "websocket.disconnect"}

    async def tab_arms() -> dict:
        # Its own accept has just crossed the gap and rolled the kiosk's
        # thread out from under it. Back over the gap before this arm, so the
        # tab is a page in the middle of a conversation and not one rolling.
        main.IDLE_GAP_S = 3600.0
        return frame({"type": "arm"})

    async def tab_heard() -> dict:
        pair["tab"] = await armed_on()
        pair["before"] = await recap_on(pair["tab"])
        return frame({"type": "disarm"})

    async def tab_waits() -> dict:
        # The wake word in the kitchen, on the socket that has been up for
        # days. Only the kiosk's thread is stale while this runs.
        main.IDLE_GAP_S = -1.0
        kiosk_arms.set()
        await asyncio.wait_for(kiosk_rolled.wait(), 10)
        main.IDLE_GAP_S = 3600.0
        return frame({"type": "arm"})

    async def tab_heard_again() -> dict:
        pair["tab_again"] = await armed_on()
        pair["after"] = await recap_on(pair["tab_again"])
        return {"type": "websocket.disconnect"}

    main._carry_forward = carry_a_summary
    try:
        with a_server_of_its_own(main):
            pages = main.runner = WakeRunner()
            main.IDLE_GAP_S = 3600.0
            kiosk_page = FakeSocket(
                [kiosk_arm_first, kiosk_wait_live, kiosk_roll, kiosk_done],
                kiosk="1")
            kiosk_task = asyncio.create_task(
                main.websocket_endpoint(kiosk_page, "user-pair"))
            await asyncio.wait_for(kiosk_live.wait(), 10)
            pages.started.clear()
            main.IDLE_GAP_S = -1.0
            tab_page = FakeSocket(
                [tab_arms, tab_heard, tab_waits, tab_heard_again])
            await asyncio.wait_for(asyncio.gather(
                main.websocket_endpoint(tab_page, "user-pair"),
                kiosk_task), 20)
    finally:
        main._carry_forward = was_carry
        main.IDLE_GAP_S = gap

    check("a second page on the profile is on a thread of its own by then",
          [pair.get("tab") != pair.get("kiosk"),
           pair.get("tab_again") == pair.get("tab")], [True, True],
          f"tab {pair.get('tab')}, kiosk {pair.get('kiosk')}")
    check("with the summary its own accept paid for in front of the agent",
          pair.get("before"), STILL,
          "`_carry_forward` runs on the accept that crosses the gap")
    check("and the wake word rolling the kiosk does not take it away",
          pair.get("after"), STILL,
          "the recap slot is the profile's; being rolled bare is one "
          "thread's")
    check("while the thread the wake word opened still reads nothing",
          pair.get("kiosk_recap"), "",
          "bare is still bare, or the kiosk answers with Sunday")

    # Read on one line at import and shipped on one more. In-process the two
    # cases above hold just as well with the thirty seconds typed into the
    # source, and the `.env` a household edits would go nowhere.
    source = str(Path(__file__).resolve().parent.parent / "src")
    with tempfile.TemporaryDirectory() as elsewhere:
        # With `load_dotenv` stubbed out, because `main.py:26` loads the real
        # `.env` with `override=True` and it would beat the environment set
        # here. The empty directory is not what stops it: `main` finds `.env`
        # from `__file__`, so where this runs from has no say in the matter.
        # The stub is the whole of what keeps it out; `cwd` is left pointing
        # somewhere harmless only for whatever else resolves relatively.
        asked = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {source!r})\n"
             "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
             "from ai_terminal.live import main\n"
             "print(main.WAKE_IDLE_S)"],
            env={**os.environ, "WAKE_IDLE_S": "7",
                 "SESSIONS_DB": str(Path(elsewhere) / "sessions.db"),
                 "GWS_CACHE_DIR": str(Path(elsewhere) / "cache")},
            cwd=elsewhere, capture_output=True, text=True)
    check("and the window itself comes off the environment, not the source",
          asked.stdout.strip().splitlines()[-1:], ["7.0"],
          f"{asked.stdout.strip()!r}{asked.stderr[-200:]}")

    # And the thing the children above rest on, which the tempdir they run in
    # is not: `main` reads `.env` from beside its own `__file__`, so a tree
    # that has one hands every one of them its values instead of the `env=`
    # set next to the case. Pinned on a copy of the tree rather than on this
    # one, because the `.env` a household really keeps sits next to these
    # lines and a case may not write over it.
    with tempfile.TemporaryDirectory() as planted:
        tree = Path(planted) / "proj"
        shutil.copytree(Path(__file__).resolve().parent.parent / "src",
                        tree / "src")
        (tree / ".env").write_text("WAKE_IDLE_S=1234\n", encoding="utf-8")
        with tempfile.TemporaryDirectory() as elsewhere:
            planted_out = subprocess.run(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, {str(tree / 'src')!r})\n"
                 "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
                 "from ai_terminal.live import main\n"
                 'print(f"{main.REPO_ROOT}|{main.WAKE_IDLE_S}")'],
                env={**os.environ, "WAKE_IDLE_S": "7",
                     "SESSIONS_DB": str(Path(elsewhere) / "sessions.db"),
                     "GWS_CACHE_DIR": str(Path(elsewhere) / "cache")},
                cwd=elsewhere, capture_output=True, text=True)
        said = planted_out.stdout.strip().splitlines()[-1:]
        root, _, window = (said[0] if said else "|").rpartition("|")
        check("the child looks for `.env` in the tree it imported from",
              Path(root) if root else None, tree.resolve(),
              f"{planted_out.stdout.strip()!r}{planted_out.stderr[-200:]}")
        check("and a `.env` sitting there does not beat the environment the "
              "case set", window, "7.0",
              "without the stub this reads 1234.0: `override=True`, found "
              "from `__file__` and never from `cwd`")

    # And the values it must not take, all of which end in the same silence.
    # Zero and negatives reach the page and `app.js` drops them: its
    # `Number.isFinite(...) && > 0` keeps the built-in 30 000 ms. `nan` and
    # `inf` arrive as `null` and are dropped by that same line — the route
    # does not 500 on them, for the reason the last three cases pin. Right of
    # the page either way, wrong place to find out. #114.
    source_dir = str(Path(__file__).resolve().parent.parent / "src")

    def imported(value: str) -> subprocess.CompletedProcess:
        """`main` imported with `WAKE_IDLE_S` set to `value`.

        With `load_dotenv` stubbed out for the reason given above: the real
        `.env` is found from `main.__file__` and loaded with `override=True`,
        so the working directory is no defence and the stub is.
        """
        with tempfile.TemporaryDirectory() as elsewhere:
            return subprocess.run(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, {source_dir!r})\n"
                 "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
                 "from ai_terminal.live import main\n"
                 "print(main.WAKE_IDLE_S)"],
                env={**os.environ, "WAKE_IDLE_S": value,
                     "SESSIONS_DB": str(Path(elsewhere) / "sessions.db"),
                     "GWS_CACHE_DIR": str(Path(elsewhere) / "cache")},
                cwd=elsewhere, capture_output=True, text=True)

    for value, why in (
            ("0", "the page keeps its own 30 s and says nothing"),
            ("-1", "the same, and it is not 'listen forever' either"),
            ("nan", "the page would be handed null and drop that too"),
            ("inf", "the same null, so no window is applied either")):
        out = imported(value)
        check(f"a wake window of {value} is refused at import",
              (out.returncode != 0, "WAKE_IDLE_S" in out.stderr),
              (True, True), why)

    # The refusal is worth nothing if the reason it exists stops being true,
    # so this asks the stack rather than trusting the comment above. Starlette
    # does reject a non-finite float, which is why the route reads like it
    # should 500; what stops it is `api_config`'s `-> dict` annotation, which
    # routes the value through pydantic on the way out, and pydantic writes it
    # as null. All three are pinned because the last one is the one the
    # message in `main.py` and the note in `.env.example` describe, and it is
    # the one that would change if somebody dropped the annotation.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.responses import JSONResponse
    try:
        JSONResponse({}).render({"wake_idle_s": float("nan")})
        strict = False
    except ValueError:
        strict = True
    check("the renderer on its own would reject a non-finite window",
          strict, True, "starlette JSONResponse renders allow_nan=False")
    check("but the route is serialised through its return annotation first",
          main.api_config.__annotations__.get("return"), dict)

    probe = FastAPI()

    @probe.get("/api/config")
    async def _config() -> dict:
        return {"wake_idle_s": float("nan")}

    got = TestClient(probe, raise_server_exceptions=False).get("/api/config")
    check("so the page is handed null rather than a 500 it cannot read past",
          (got.status_code, got.json()["wake_idle_s"]), (200, None))

    # And the two paragraphs in the template that say all of the above to the
    # person editing `.env`, which nothing held: both could be deleted outright
    # and every case above stayed green, while the `SESSION_IDLE_GAP_S` text
    # six lines up is pinned in `history_check`. The refusal is the half
    # somebody hits by accident and the null is the half that explains why it
    # is a refusal and not a clamp, so both are worth a line.
    block = ((Path(__file__).resolve().parent.parent / ".env.example")
             .read_text(encoding="utf-8")
             .split("# WAKE_IDLE_S=")[0].split("\n\n")[-1])
    check("and the template says the wake window is refused, not clamped",
          "finite number above zero, and the server refuses" in block, True)
    check("and that a non-finite one is discarded rather than fatal",
          "as null" in block, True,
          "the paragraph that replaced the 500 this never did")


# ------------------------------- one Live session per conversation, not per page

class RowRunner:
    """A Runner that keeps ADK's own hold on the session row.

    `WakeRunner` never touches the store, which is the right shape for the
    cases about arming and the wrong one for the case about what two streams
    do to one conversation. The two lines of `run_live` that decide that are
    kept verbatim — ADK loads a `Session` of its own when the stream starts
    (`runners.py:1963`, google-adk 2.8.0) and appends every non-partial turn
    back through that object (`base_llm_flow.py:1048`) — and only the model in
    between is stubbed. `SqliteSessionService.append_event` refuses an object
    older than the row it is writing to, so a second stream on one row does not
    interleave with the first: it raises and ends.
    """

    def __init__(self, service, app_name: str):
        self.session_service = service
        self.app_name = app_name
        self.auto_create_session = True
        self.streams: list[dict] = []

    async def run_live(self, *, user_id, session_id, live_request_queue,
                       run_config):
        from google.adk.runners import Runner

        held = {"go": asyncio.Event(), "spoke": 0, "error": None, "open": True}
        # Recorded before the row is loaded, not after: a stream stopped during
        # the load is still a stream that was opened, and counting it later
        # would let the case pass by losing track of it.
        self.streams.append(held)
        try:
            session = await Runner._get_or_create_session(
                self, user_id=user_id, session_id=session_id)
            while True:
                await held["go"].wait()
                held["go"].clear()
                try:
                    await self.session_service.append_event(session, Event(
                        author="model", content=types.Content(
                            role="model",
                            parts=[types.Part(text="an answer")])))
                except Exception as err:  # pylint: disable=broad-except
                    held["error"] = type(err).__name__
                    raise
                held["spoke"] += 1
                yield Event(author="model")
        finally:
            held["open"] = False


async def one_live_session_cases() -> None:
    """How many Live sessions two pages on one conversation add up to.

    A session id names a profile and not a socket, so two pages hold one
    indefinitely — and `arm` guards only its own connection's `live_task`, so
    both of them could open a `run_live` against the same ADK row. That is not
    two conversations sharing a history: each stream loads a `Session` object
    of its own and the SQLite backend rejects an append from the older one, so
    the second page to be answered has its stream torn down by a
    `StaleSessionError` while its page still shows a lit microphone.

    Not only two tabs. A reconnect re-arms from `app.js`'s `onopen` while the
    socket it replaced may not have reached its `finally` — the overlap
    `reload_cases` above is built on — so the kiosk on its own reaches this on
    a Wi-Fi blip.
    """
    print("\n== two pages arming on one conversation ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    async def opened(plan: list[tuple[str, list[str]]], stale: bool = False,
                     rows: bool = False) -> dict:
        """Open a connection per entry and send it the frames it names.

        Every connection is left standing at the end of its script rather than
        disconnecting, so what is measured is the state with all of them up at
        once. `stale` makes the thread look past `IDLE_GAP_S` to every arm but
        the first connection's, which is how a roll is put underneath the case
        without the first page's own arm rolling it first.
        """
        real_thread, real_gap = main._thread, main.IDLE_GAP_S
        ids: list[str] = []
        stay = asyncio.Event()
        socks: list[FakeSocket] = []
        tasks: list[asyncio.Task] = []
        out: dict = {}

        async def note_thread(user_id, state):
            ids.append(await real_thread(user_id, state))
            return ids[-1]

        def script(n: int, frames: list[str], here: asyncio.Event):
            steps = []
            for frame in frames:
                async def one(frame=frame):
                    if frame == "arm" and stale and n:
                        main.IDLE_GAP_S = -1.0
                    return {"type": "websocket.receive",
                            "text": json.dumps({"type": frame})}
                steps.append(one)

            async def settle():
                here.set()
                await stay.wait()
                return {"type": "websocket.disconnect"}
            return steps + [settle]

        main._thread = note_thread
        try:
            with a_server_of_its_own(main):
                runner = (RowRunner(main.session_service, main.APP_NAME)
                          if rows else WakeRunner())
                main.runner = runner
                for n, (user_id, frames) in enumerate(plan):
                    here = asyncio.Event()
                    socks.append(FakeSocket(script(n, frames, here),
                                            kiosk="1" if n == 0 else ""))
                    tasks.append(asyncio.create_task(
                        main.websocket_endpoint(socks[-1], user_id)))
                    await asyncio.wait_for(here.wait(), 10)

                # Let every arm reach its Runner before anything is counted.
                # `arm` creates the task and returns, so a page that has
                # answered the endpoint may not have opened its stream yet,
                # and sampling here would count whichever ones happened to be
                # scheduled. Wait for the count to hold still instead.
                seen, still = -1, 0
                for _ in range(200):
                    now = len(runner.streams if rows else runner.calls)
                    still = still + 1 if now == seen else 0
                    if still >= 3:
                        break
                    seen = now
                    await asyncio.sleep(0.005)

                if rows:
                    for held in runner.streams:
                        held["go"].set()
                        # A stream that is no longer open is one the takeover
                        # stopped, and it will never answer this.
                        for _ in range(500):
                            if (held["spoke"] or held["error"]
                                    or not held["open"]):
                                break
                            await asyncio.sleep(0.002)
                    out["turns"] = [h["spoke"] for h in runner.streams]
                    out["errors"] = [h["error"] for h in runner.streams]
                    out["opened"] = len(runner.streams)
                    out["open"] = sum(1 for h in runner.streams if h["open"])
                else:
                    out["opened"] = len(runner.calls)
                    out["open"] = len(runner.calls) - runner.cancelled
                    out["threads"] = len({c["session_id"]
                                          for c in runner.calls})
                stay.set()
                for task in reversed(tasks):
                    await asyncio.wait_for(task, 10)
        finally:
            main._thread, main.IDLE_GAP_S = real_thread, real_gap
        return out

    two = await opened([("alex", ["arm"]), ("alex", ["arm"])])
    check("two pages arming on one conversation is one Live session, not two",
          [two["threads"], two["opened"], two["open"]], [1, 2, 1],
          "one thread, two arms, and only the newest still streaming")

    three = await opened([("alex", ["arm"])] * 3)
    check("and a third page takes it in turn rather than adding to it",
          [three["opened"], three["open"]], [3, 1])

    rolled = await opened([("alex", ["arm"]), ("alex", ["arm"])], stale=True)
    check("a page that rolls the thread as it arms stops the other stream "
          "before it rolls, not after",
          rolled["open"], 1,
          "after the roll `session_id` is the new thread, which nobody holds, "
          "and the stream on the old one is left running")

    rows = await opened([("alex", ["arm"]), ("alex", ["arm"])], rows=True)
    check("so one page being answered cannot tear the other's Live session "
          "down",
          [rows["turns"], rows["errors"]], [[0, 1], [None, None]],
          "two streams on one row is one append and one StaleSessionError, "
          "and the page that lost is told nothing at all")

    # The three below are green before the fix as well as after, and are here
    # to hold the shapes a takeover must not swallow. Said out loud, because a
    # case that already passes on the unfixed tree is watching something next
    # to the defect and not the defect.
    twice = await opened([("alex", ["arm", "arm"])])
    check("one page arming twice is still one Live session",
          [twice["opened"], twice["open"]], [1, 1],
          "the wake word and the microphone button are the same request")

    apart = await opened([("alex", ["arm"]), ("sam", ["arm"])])
    check("and two people talking at once keep a Live session each",
          [apart["threads"], apart["open"]], [2, 2],
          "the reservation is on the conversation, not on the server")

    back = await opened([("alex", ["arm", "disarm"]), ("alex", ["arm"])])
    check("a page that disarms hands the conversation back rather than "
          "holding it",
          [back["threads"], back["open"]], [1, 1],
          "counted by what is still open, because whether the disarmed page's "
          "stream ever started is a matter of scheduling")


# ------------------------------------------- a name the agent does not have

async def unknown_tool_cases() -> None:
    """A function call the tool table has no entry for.

    ADK raises `ValueError` from `_get_tool` (`functions.py:1329`, 2.8.0) and
    on the live path nothing catches it: the Live session ends mid-turn. The
    one frame earlier where it can still be answered is
    `on_tool_error_callback`, which builds a function response instead when it
    returns a dict (`functions.py:840-854`).

    The name in the first case is the real one, from pi2 on 2026-09-11:
    `maps_search` with the title of its own generated parameter schema on the
    end — `create_model(f'{func_name}Params', ...)`, shipped to the model as
    `parametersJsonSchema.title`. #269.
    """
    print("\n== a function call naming a tool that does not exist ==")

    from ai_terminal.live import main

    callbacks = main.agent.canonical_on_tool_error_callbacks
    check("the agent answers a tool error rather than letting it end the run",
          len(callbacks), 1,
          "without one, `handle_function_calls_live` re-raises")

    def answered(name: str):
        return callbacks[0](
            tool=types_mod.SimpleNamespace(name=name),
            args={"query": "ishihara clinic", "near": "", "lang": "ja"},
            tool_context=FakeContext(),
            error=ValueError(f"Tool '{name}' not found."),
        )

    refused = answered("maps_searchParams")
    check("a hallucinated name comes back as a result the model can act on",
          isinstance(refused, dict) and "maps_search" in refused.get("error",
                                                                    ""),
          True, repr(refused))
    check("and the error names the tool the model actually asked for",
          "maps_searchParams" in (refused or {}).get("error", ""), True)
    # Every generated schema title is `<tool>Params`, so this is the shape of
    # the whole class and not one string that happened to come up once.
    check("which holds for every tool, not just the one it happened to be",
          sorted({bool(answered(f"{tool.name}Params"))
                  for tool in main.agent.tools}), [True])
    check("a tool the agent does have is left to its own error handling",
          answered("maps_search"), None,
          "returning a dict here would paper over a real traceback")
    # The pydantic title that the name came from, read off the declaration the
    # model is actually sent rather than asserted from memory.
    decl = maps.maps_search_tool._get_declaration()
    check("and the string the model copied is in the declaration it was sent",
          [decl.name, (decl.parameters_json_schema or {}).get("title")],
          ["maps_search", "maps_searchParams"],
          "google-adk 2.8.0, _function_tool_declarations.py:137")


# ---------------------------------- the card and every page that is up for it

async def card_fanout_cases() -> None:
    """Which screens a card reaches when a session has more than one page.

    A session id is a profile and not a connection since #19, so the kiosk in
    the kitchen and a tab on the Mac are two pages on one id, and unlike the
    two halves of a reload they both stay up for days. `screen.push` resolving
    only `sink` is #138: the older page is not behind, it never gets the card
    at all, and nothing on it says so.

    `/api/wake` is the one frame that must not fan out — it opens a microphone
    rather than drawing something — and the cases above hold that to one screen
    per session. These are about everything else: `gws` answers, `plan` steps,
    progress notes, browse screenshots.
    """
    print("\n== the card and every page on the session ==")

    got: dict[str, list[dict]] = {}
    takes = inspect.signature(screen.register).parameters

    def a_page(session_id: str, name: str, then=None, waits=None,
               closed=None):
        """Register a page that records what it is told, and hand it back.

        `waits` is a page that has stopped reading — a suspended laptop, a tab
        at TCP zero-window — which is a socket that neither takes the frame nor
        raises, and so is the one shape a `try` cannot cover.

        `closed` is a list this page's name goes into when the server gives up
        on it and closes its socket, which is the half of dropping a page that
        the page itself can see. Passed to `register` only on a tree whose
        `register` takes it, so that a tree without it fails the case that says
        so and then goes on to fail the rest on their own terms rather than
        taking the script down with a `TypeError` here.
        """
        got[name] = []

        async def send(frame: dict) -> None:
            if then is not None:
                then()
            if waits is not None:
                await waits.wait()
            got[name].append(frame)

        async def close() -> None:
            closed.append(name)

        if closed is not None and "close" in takes:
            screen.register(session_id, send, False, close)
        else:
            screen.register(session_id, send)
        return send

    async def settled(turns: int = 200) -> None:
        """Let every writer that is not blocked finish what it is holding.

        `push` hands a frame to a writer task and returns, so "has it landed"
        is a question about the loop having run and not about the push having
        finished. Turns of the loop and not a `sleep`, so that nothing here
        depends on how fast this machine is; a page that is blocked stays
        blocked however many turns go by, which is the whole point of the
        cases below.
        """
        for _ in range(turns):
            await asyncio.sleep(0)

    async def pushed(session_id: str, frame: dict):
        """`screen.push`, with anything it raises reported as a value.

        A push that raises is as much the failure here as a push that skips a
        page — it goes back up through the tool that drew the card, which is
        answering the person — so each case says so by name rather than
        letting the first one take the file down with it.
        """
        try:
            await screen.push(session_id, frame)
        except Exception as err:  # pylint: disable=broad-except
            return f"{type(err).__name__}: {err}"
        return None

    sid = "card-1"
    kitchen = a_page(sid, "kitchen")
    mac = a_page(sid, "mac")
    try:
        raised = await pushed(sid, {"type": "gws", "id": "c-1"})
        check("a card reaches every page on the session and not only the "
              "newest",
              [raised, got["kitchen"], got["mac"]],
              [None] + [[{"type": "gws", "id": "c-1"}]] * 2,
              "the kiosk that has been up all day is the one that loses it")
    finally:
        screen.unregister(sid, kitchen)
        screen.unregister(sid, mac)

    # Registered second, so it is the one `sink` would have resolved to: the
    # page that is still up has to be reached past it, not instead of it.
    def boom() -> None:
        raise RuntimeError("this screen closed a moment ago")

    staying = a_page(sid, "staying")
    gone = a_page(sid, "gone", then=boom)
    try:
        raised = await pushed(sid, {"type": "gws", "id": "c-2"})
        check("a page whose socket has gone costs the page beside it nothing",
              [raised, got["staying"], got["gone"]],
              [None, [{"type": "gws", "id": "c-2"}], []],
              "each page has a writer of its own and each frame is isolated "
              "inside it; share either and one dead socket costs the page "
              "beside it a card")
    finally:
        screen.unregister(sid, staying)
        screen.unregister(sid, gone)

    # The same shape `wake_cases` holds for `/api/wake`: a send can end with a
    # connection tearing itself down, and the frame in flight to the page next
    # to it must still land.
    left = a_page(sid, "left")
    closer = a_page(sid, "closer", then=lambda: screen.unregister(sid))
    try:
        raised = await pushed(sid, {"type": "gws", "id": "c-3"})
        check("nor does one that tears the registry down while being written "
              "to",
              [raised, got["left"], got["closer"]],
              [None] + [[{"type": "gws", "id": "c-3"}]] * 2,
              "the push walks a copy, not the registry")
    finally:
        screen.unregister(sid, left)
        screen.unregister(sid, closer)

    # The failure a `try` cannot catch, because nothing is raised. Registered
    # first, so a loop that awaits each page in turn reaches it before the page
    # that is awake and the card stops there — on a screen that is meant to be
    # left up all day, next to somebody waiting for it.
    held = asyncio.Event()
    stuck = a_page(sid, "stuck", waits=held)
    awake = a_page(sid, "awake")
    try:
        drawing = asyncio.create_task(
            screen.push(sid, {"type": "gws", "id": "c-4"}))
        # Enough turns for a send that is waiting on nothing to have finished.
        for _ in range(5):
            await asyncio.sleep(0)
        landed = list(got["awake"])
        held.set()
        await asyncio.wait_for(drawing, 10)
        await settled()
        check("a page that has stopped reading does not hold the card off "
              "the page beside it",
              [landed, got["stuck"]],
              [[{"type": "gws", "id": "c-4"}]] * 2,
              "written to in turn, the slowest socket is the latency of every "
              "screen behind it")
    finally:
        held.set()
        await settled()
        screen.unregister(sid, stuck)
        screen.unregister(sid, awake)

    # The same shape as the raising case above, with the raiser registered
    # *first*. That case says "every sink after the raiser is skipped" and
    # cannot see it: its raiser is last, so there is no sink after it, and one
    # `try` around the whole loop passes it unnoticed.
    early = a_page(sid, "early", then=boom)
    late = a_page(sid, "late")
    try:
        raised = await pushed(sid, {"type": "gws", "id": "c-5"})
        await settled()
        check("and a page whose socket has gone costs the page registered "
              "after it nothing either",
              [raised, got["early"], got["late"]],
              [None, [], [{"type": "gws", "id": "c-5"}]],
              "the case above cannot see this: its raiser is last on the "
              "stack, so nothing is behind it to be skipped")
    finally:
        screen.unregister(sid, early)
        screen.unregister(sid, late)

    # And a page that raised once is not a page that has gone: the frame
    # behind the one that raised belongs to the same socket and costs nothing
    # to hand it. The isolation is per frame for that reason, and one `try`
    # round a page's whole run of frames would strand everything queued behind
    # a single bad write.
    #
    # The send yields before it raises, which is the only way to get two frames
    # into one run: `push` gives the loop a turn on its way out, so without the
    # yield the first frame is finished with before the second is written.
    once: list[str] = []

    async def flaky(frame: dict) -> None:
        await asyncio.sleep(0)
        if frame["id"] == "c-6":
            raise RuntimeError("that write did not go out")
        once.append(frame["id"])

    screen.register(sid, flaky)
    try:
        await screen.push(sid, {"type": "gws", "id": "c-6"})
        await screen.push(sid, {"type": "gws", "id": "c-7"})
        await settled()
        check("and the frame queued behind one that raised is still written",
              once, ["c-7"],
              "one `try` round a page's whole run and a single bad write "
              "strands every frame already behind it")
    finally:
        screen.unregister(sid, flaky)

    # ------------------------------------- and the frames after the first one

    # Concurrent sends fixed the first card and no card after it. `push`
    # returned only when the slowest page had taken the frame, and the caller
    # that draws a card awaits `push` once per frame — `browse.watch` does —
    # so frame N+1 could not be drawn until frame N had been taken by the page
    # that had stopped reading. Measured on the tree that closed #138: with a
    # stalled tab attached, a three-screenshot browse put step-1 on the panel
    # at once and step-2 and step-3 at the tab's pace, and never finished at
    # all when the tab was a laptop that stayed asleep.
    #
    # The stalled page is registered first, which is the ordering a kiosk
    # reload or a tunnel reconnect produces: the tab that has been open since
    # morning, and then the panel.
    asleep = asyncio.Event()
    tab = a_page(sid, "tab", waits=asleep)
    panel = a_page(sid, "panel")
    try:
        async def three_frames() -> None:
            for step in (1, 2, 3):
                await screen.push(sid, {"type": "browse", "phase": "step",
                                        "step": step})

        run = asyncio.create_task(three_frames())
        await settled()
        check("nor does it set the pace of the frames after the first",
              [[f["step"] for f in got["panel"]], run.done()],
              [[1, 2, 3], True],
              "one page holding frame N is one page holding frame N+1 too, "
              "and a browse is thirty of them")
        asleep.set()
        await settled()
        check("and it is handed every frame it was holding up, in order, if "
              "it starts reading again",
              [f["step"] for f in got["tab"]], [1, 2, 3],
              "a progress note is a line of its own and a browse `done` is "
              "the only thing that stops the card spinning, so nothing here "
              "is safe to drop out of the middle")
    finally:
        asleep.set()
        await settled()
        screen.unregister(sid, tab)
        screen.unregister(sid, panel)

    # A page nobody waits on is still a page nobody draws on, which is #138
    # again in a quieter form: it sits there showing the last card it got with
    # no way to notice that it is the last one it will ever get. So the bound
    # in the issue drops it *and* closes its socket, and `app.js` reconnects on
    # `onclose` and registers a screen again — a laptop that was asleep finds a
    # live panel a second after it wakes.
    check("`register` is told what to do to a page the server has to drop",
          "close" in takes, True,
          "dropping a page and leaving its socket up is the deaf kiosk this "
          "issue is about, one layer down")

    # The one wall-clock assertion in this file, because a deadline cannot be
    # measured in turns of the loop. The margin is thirty to one, and it is a
    # deadline being exceeded rather than met, so a slow machine makes this
    # more true and not less.
    shut: list[str] = []
    forever = asyncio.Event()
    drawing = []
    was, screen.DEADLINE_S = screen.DEADLINE_S, 0.01
    dead = a_page(sid, "dead", waits=forever, closed=shut)
    live_page = a_page(sid, "live")
    try:
        # Each `push` in a task of its own, for the same reason the pacing
        # case above uses one: a `push` that waits on the page that has
        # stopped reading does not come back, and a case that cannot be read
        # because the file it is in never finished says nothing.
        drawing.append(asyncio.create_task(
            screen.push(sid, {"type": "gws", "id": "c-8"})))
        await asyncio.sleep(0.3)
        await settled()
        check("a page that will not take a frame inside the bound is dropped "
              "and its socket closed",
              [shut, [f["id"] for f in got["live"]]],
              [["dead"], ["c-8"]],
              "#138: dropped from the stack rather than waited on")
        drawing.append(asyncio.create_task(
            screen.push(sid, {"type": "gws", "id": "c-9"})))
        await settled()
        forever.set()
        await settled()
        check("and nothing is drawn on it after that, even once it reads "
              "again",
              [got["dead"], [f["id"] for f in got["live"]]],
              [[], ["c-8", "c-9"]],
              "off the list first, so the next frame is not queued behind the "
              "one that timed out")
    finally:
        forever.set()
        await settled()
        for task in drawing:
            task.cancel()
        screen.DEADLINE_S = was
        screen.unregister(sid, dead)
        screen.unregister(sid, live_page)

    # Started when there is something to write and gone when there is not. A
    # writer parked on every page for the life of the page would be one more
    # thing to cancel on every path a page can leave by, and the paths a page
    # leaves by are the ones #127 and #133 keep finding holes in.
    quiet = a_page(sid, "quiet")
    try:
        before = len(asyncio.all_tasks())
        await screen.push(sid, {"type": "gws", "id": "c-10"})
        await settled()
        check("a session that has gone quiet leaves no writer parked on it",
              [len(asyncio.all_tasks()), [f["id"] for f in got["quiet"]]],
              [before, ["c-10"]],
              "on demand, and ended when the queue is empty")
    finally:
        screen.unregister(sid, quiet)


# ----------------------------- and the pages that come and go under one job

async def browse_fanout_cases() -> None:
    """A page that joins a browse job, and one that leaves it.

    A browse run is the better part of a minute. `browse` used to resolve the
    screen once, before starting the browser, and hand that one closure to
    `browser.run` for the whole run — so every screenshot went to whichever
    socket was newest at call time, including after a second page had joined
    the session and including after that socket had closed with another page
    still standing there.
    """
    print("\n== a browse job and the pages that come and go under it ==")

    sid = "live-1"
    seen: dict[str, list[str]] = {}
    stepped = asyncio.Event()
    carry_on = asyncio.Event()

    async def stub(task, lang, context="", sink=None):
        """One step, a pause long enough for a page to arrive or leave, one
        more step."""
        await sink({"type": "browse", "phase": "step", "step": 1,
                    "url": "https://shop.test/", "shot": "data:x"})
        stepped.set()
        await carry_on.wait()
        await sink({"type": "browse", "phase": "step", "step": 2,
                    "url": "https://shop.test/hours", "shot": "data:y"})
        return {"answer": "Open 10:00-18:00.",
                "url": "https://shop.test/hours", "seconds": 2}

    def a_page(name: str):
        seen[name] = []

        async def send(frame: dict) -> None:
            seen[name].append(f"{frame.get('phase')}-{frame.get('step')}")

        return send

    async def run_with(joins: bool) -> None:
        """Drive one job, with the second page either arriving or leaving
        halfway through it."""
        stepped.clear()
        carry_on.clear()
        first, second = a_page("first"), a_page("second")
        real, browse.browser.run = browse.browser.run, stub
        screen.register(sid, first)
        if not joins:
            screen.register(sid, second)
        browse.open_session(sid)
        try:
            job = asyncio.create_task(
                browse.browse("opening hours", FakeContext()))
            await asyncio.wait_for(stepped.wait(), 10)
            if joins:
                screen.register(sid, second)
            else:
                screen.unregister(sid, second)
            carry_on.set()
            await asyncio.wait_for(job, 10)
        finally:
            browse.close_session(sid)
            screen.unregister(sid, first)
            screen.unregister(sid, second)
            browse.browser.run = real

    await run_with(joins=True)
    check("a page that opens mid-browse is drawn the rest of the job",
          [seen["first"], seen["second"]],
          [["step-1", "step-2"], ["step-2"]],
          "the sink used to be resolved once, before the browser started")

    await run_with(joins=False)
    check("and the page still up is drawn the rest of it when the newest "
          "closes",
          [seen["first"], seen["second"]],
          [["step-1", "step-2"], ["step-1"]],
          "otherwise the run finishes into a socket that has gone")


def gws_dynamic_fan_out_cases() -> None:
    """Dynamic selective fan-out and ReAct in gws (#511).

    #511: for queries concerning distributed state spanning multiple services,
    issue lightweight discovery calls concurrently in parallel on Turn 1,
    target the latest transaction in Turn 2, and maintain single-tool ReAct
    for focused single-source queries.
    """
    print("\n== dynamic selective fan-out and ReAct in gws (#511) ==")
    flat_gws = " ".join(gws.INSTRUCTION.split())
    check("gws.py contains dynamic fan-out over serial search principle",
          "Dynamic fan-out over serial search" in flat_gws, True)
    check("gws.py instructs issuing lightweight discovery calls concurrently in parallel on Turn 1 for distributed state",
          "issue lightweight discovery calls (e.g. `gmail +triage` and `drive files list`) concurrently in parallel" in flat_gws, True)
    check("gws.py instructs targeting latest transaction in Turn 2",
          "Use the combined metadata timeline to target the latest transaction in Turn 2" in flat_gws, True)
    check("gws.py instructs using single-tool ReAct for focused single-source queries",
          "For focused single-source asks (calendar, single list), use single-tool ReAct" in flat_gws, True)


def verify_queue_drain_cases() -> None:
    """The verify pass iterates the detection query every tick and codifies skill restart (#522)."""
    print("\n== verify queue drain and skill restart discipline (#522) ==")
    root = Path(__file__).resolve().parent.parent
    repo_root = root.parent if (root.parent / ".claude").is_dir() else root
    reviewer_skill = repo_root / ".claude" / "skills" / "issue-looping-reviewer" / "SKILL.md"
    shared_skill = repo_root / ".claude" / "skills" / "issue-looping-shared" / "SKILL.md"

    if not reviewer_skill.exists():
        try:
            top = Path(subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(root), text=True, stderr=subprocess.DEVNULL
            ).strip())
            reviewer_skill = top / ".claude" / "skills" / "issue-looping-reviewer" / "SKILL.md"
            shared_skill = top / ".claude" / "skills" / "issue-looping-shared" / "SKILL.md"
        except Exception:
            pass

    reviewer_text = reviewer_skill.read_text(encoding="utf-8") if reviewer_skill.exists() else ""
    shared_text = shared_skill.read_text(encoding="utf-8") if shared_skill.exists() else ""

    check("reviewer skill asserts verifying detection query every tick",
          "The verify pass iterates the detection query, not the recent merge event" in reviewer_text,
          True,
          "reviewer iterates the detection query every tick (#522)")
    check("reviewer skill asserts the stated hold rule",
          "The stated hold rule" in reviewer_text,
          True,
          "reviewer must state holds on the thread rather than silently leaving issues (#522)")
    check("shared skill codifies skill reload discipline",
          "Skill lifecycle and session restart discipline" in shared_text,
          True,
          "shared skill codifies session restart discipline for skill edits (#522)")


# ------------------------------------------------ which interface it offers

def bind_cases() -> None:
    print("\n== which interface the server offers itself on ==")

    from ai_terminal.live import main

    # Read off the parser rather than off a running server, because `run`
    # ends in `uvicorn.run` and never comes back. This is the whole reason
    # `_parser` exists as its own function.
    bare = main._parser().parse_args([])
    check("the server binds loopback when nobody says otherwise",
          bare.host, "127.0.0.1",
          "the Pi's tunnel lands here; a listener on the LAN served nobody "
          "and takes no credential")
    check("on the port the tunnel's other end names", bare.port, 8000)
    check("and does not reload unless asked", bare.reload, False)
    # Still reachable, and it has to be: #10 puts the server on the Pi, where
    # the kiosk browser is a different host and loopback is not an option.
    check("widening it is a thing somebody can still ask for",
          main._parser().parse_args(["--host", "0.0.0.0"]).host, "0.0.0.0")

    # What counts as "only this machine", which is the test the warning above
    # turns on. The two wildcards and the empty string are the ones worth
    # pinning: uvicorn reads all three as every interface, and a predicate
    # that let any of them through would warn about nothing on the one
    # invocation that needs the warning.
    for host, want in [("127.0.0.1", True), ("localhost", True),
                       ("::1", True), ("0.0.0.0", False), ("::", False),
                       ("", False), ("192.0.2.10", False)]:
        check(f"is {host or '(empty)'} only this machine",
              main._is_loopback(host), want)

    # And the warning, which is the only part of this a person ever sees and
    # so the only part that can be wrong while everything still starts. `run`
    # ends in `uvicorn.run`, so the module is stubbed out from under it: the
    # question is what was said on the way past, not whether a socket appeared.
    grabbed: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record) -> None:
            grabbed.append(f"{record.levelname} {record.getMessage()}")

    def said(host: str) -> list[str]:
        grabbed.clear()
        stub = types_mod.ModuleType("uvicorn")
        stub.run = lambda *a, **k: None
        was_mod = sys.modules.get("uvicorn")
        was_argv = sys.argv
        handler = Grab()
        main.logger.addHandler(handler)
        # An earlier section drops the root logger to WARNING to keep this
        # script readable, and `logger.info` checks the level before it
        # reaches any handler — so without this the INFO line below is
        # missing here and present in production, which is the wrong way
        # round for a case to be green.
        was_level = main.logger.level
        main.logger.setLevel(logging.INFO)
        sys.modules["uvicorn"] = stub
        sys.argv = ["live-server", "--host", host]
        try:
            main.run()
        finally:
            sys.argv = was_argv
            main.logger.setLevel(was_level)
            main.logger.removeHandler(handler)
            if was_mod is None:
                sys.modules.pop("uvicorn", None)
            else:
                sys.modules["uvicorn"] = was_mod
        return grabbed[:]

    wide = said("0.0.0.0")
    check("offering the LAN says so, at WARNING",
          [line for line in wide if line.startswith("WARNING")],
          ["WARNING 0.0.0.0 accepts connections from other machines, and "
           "this server asks nobody for a credential"])
    check("under a line naming what it bound", wide[0],
          "INFO serving on http://0.0.0.0:8000")
    quiet = said("127.0.0.1")
    check("while loopback warns about nothing",
          [line for line in quiet if line.startswith("WARNING")], [],
          "the ordinary case is not a thing to shout about")
    check("and still says where it is", quiet,
          ["INFO serving on http://127.0.0.1:8000"])


# --------------------------------------- and the one that talks to the agent

async def handshake(main, origin: str | None,
                    host: str = OUR_HOST) -> list[dict]:
    """One handshake, put through the real ASGI app, and what came back.

    Built from a scope rather than through a test client, for the same reason
    `posted` above is: the whole question is which two headers arrive, and a
    client insists on supplying both of them itself. Through `main.app` rather
    than through the endpoint function, which is the other half — it is the
    only way to see that nothing in front of the endpoint has already accepted.

    What comes back is the ASGI messages the app sent, in order.
    `websocket.accept` first is a connection. A bare `websocket.close` is a
    refusal, and it is a refusal all the way to the wire: uvicorn answers that
    with a 403 on the handshake instead of upgrading
    (`protocols/websockets/websockets_impl.py`), so the socket the attacker
    asked for never exists. Accepting and *then* closing would be a different
    thing entirely — by then every side effect below `accept()` has run.

    A `websocket.http.response.start` is the other refusal, and it comes from
    in front of the router rather than from the endpoint: that is what
    `TrustedHostMiddleware` sends for a name the server does not answer to
    (`responses.py:154-167` turns its 400 into one), and no route is reached
    at all.

    An accepted socket is armed before it is let go. Since #60 the socket and
    the Live session have separate lifetimes: `/ws/` accepts, registers the
    screen and waits, and nothing reaches the model until the page sends
    `arm`. So a handshake that only connects and disconnects says nothing
    about whether the agent is reachable, which is the half of this section
    that matters.
    """
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    queued = [{"type": "websocket.connect"},
              {"type": "websocket.receive", "text": '{"type": "arm"}'}]
    sent: list[dict] = []

    async def receive() -> dict:
        if queued:
            return queued.pop(0)
        # Then a disconnect, so an accepted connection ends instead of hanging
        # this file — but not before the turn it armed has had the loop back.
        # Bounded, because a refusal never gets here and a session that never
        # answers is itself the failure the caller is looking at.
        #
        # `READY` does not count. It is sent on the accept path, before this
        # socket has been asked for anything, so counting it would disconnect
        # ahead of the `arm` reaching `run_live` and every case here would go
        # on passing with `reached` at zero.
        for _ in range(1000):
            if any(message["type"] == "websocket.send"
                   and message.get("text") != READY for message in sent):
                break
            await asyncio.sleep(0)
        return {"type": "websocket.disconnect", "code": 1005}

    async def send(message: dict) -> None:
        sent.append(message)

    # The query string is the attack in the handshake: `location` and
    # `lat`/`lng` are appended to the resumed conversation as a state delta, so
    # a refused handshake has to leave them nowhere.
    await main.app(
        {"type": "websocket", "asgi": {"version": "3.0"},
         "http_version": "1.1", "scheme": "ws", "path": "/ws/alex",
         "raw_path": b"/ws/alex", "root_path": "", "subprotocols": [],
         "query_string": b"location=Evil+Street&lat=1&lng=2",
         "headers": headers, "server": ("terminal.local", 8000),
         "client": ("10.0.0.9", 4242)},
        receive, send)
    return sent


@contextlib.contextmanager
def answering_to(main, hosts):
    """`main.app`, with its host guard configured to these names.

    The middleware is constructed once, out of the list `main.py` read at
    import, when Starlette builds the stack. So the only way to ask what
    another allowlist would do is to put one in the app's `user_middleware`
    and drop the built stack, which Starlette rebuilds on the next call
    (`applications.py:94-96`).

    A guard that is not there is left missing rather than installed here.
    Whether the app has one at all is a case of its own below, and a helper
    that supplied one would make every case after it pass on a tree that never
    registered the middleware.
    """
    from starlette.middleware import Middleware

    was_stack, was_added = main.app.middleware_stack, main.app.user_middleware
    main.app.user_middleware = [
        Middleware(entry.cls, *entry.args,
                   **{**entry.kwargs, "allowed_hosts": list(hosts)})
        if entry.cls.__name__ == "TrustedHostMiddleware" else entry
        for entry in was_added]
    main.app.middleware_stack = None
    try:
        yield
    finally:
        main.app.user_middleware = was_added
        main.app.middleware_stack = was_stack


async def host_guard_cases() -> None:
    print("\n== and which names this server will answer to ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    # First, because every case below drives the app and a stack with no host
    # guard in it answers all of them the way the unfixed tree does. It has to
    # be middleware and not a line in the endpoint: Starlette wraps the router
    # in whatever was added here (`applications.py:63-83`), which is the only
    # arrangement that runs before the origin comparison rather than after it.
    added = [entry.cls.__name__ for entry in main.app.user_middleware]
    check("the server has a host guard in front of the router at all",
          "TrustedHostMiddleware" in added, True,
          f"middleware: {added or 'none'}")
    check("configured with the list the module worked out at import",
          next((entry.kwargs.get("allowed_hosts")
                for entry in main.app.user_middleware
                if entry.cls.__name__ == "TrustedHostMiddleware"), None),
          getattr(main, "ALLOWED_HOSTS", "no ALLOWED_HOSTS in the server"))

    # ----------------------------------------- what the list comes out as

    parse = getattr(main, "_allowed_hosts", None)
    before = os.environ.get("ALLOWED_HOSTS")

    def configured(value: str | None):
        if parse is None:
            return "no `_allowed_hosts` in the server"
        if value is None:
            os.environ.pop("ALLOWED_HOSTS", None)
        else:
            os.environ["ALLOWED_HOSTS"] = value
        return parse()

    PAIR = ["localhost", "127.0.0.1"]
    try:
        # Both topologies put the kiosk on `http://localhost:8000` — the panel
        # forwards the port precisely so it can, because `getUserMedia` wants a
        # secure context — so a terminal nobody has configured is a terminal
        # that already answers to the only name it is called by.
        check("a terminal nobody configured answers to the loopback pair",
              configured(None), PAIR, "both topologies browse to localhost")
        check("and an empty setting is the same as none", configured(""), PAIR)
        # Added to, never in place of. Read as the whole list, one name here
        # would take the kiosk's own `localhost` away, and the terminal would
        # come up refusing the one browser it has — which is how an allowlist
        # ends up set to `*`.
        check("a name is added to the pair rather than put in its place",
              configured("raspberrypi.local"), PAIR + ["raspberrypi.local"])
        check("written the way a person writes it",
              configured(" raspberrypi.local , 192.0.2.10 "),
              PAIR + ["raspberrypi.local", "192.0.2.10"])
        # `trustedhost.py:40` splits the port off the *header* before comparing
        # and `:44` does not split it off the pattern, so a port typed in here
        # would be a name no request could ever have.
        check("a port typed into it is dropped, not left to never match",
              configured("raspberrypi.local:8000"), PAIR + ["raspberrypi.local"])
        # And `:44` is `host == pattern` with no `lower()` anywhere in that
        # file, so a capital here is a name the browser's lowercase header
        # never equals.
        check("and a capital letter is not a different machine",
              configured("Pi2.Local"), PAIR + ["raspberrypi.local"])
        # The one that opens the guard instead of narrowing it. An empty
        # pattern equals the `""` that `headers.get("host", "")` returns for a
        # request carrying no `Host` at all, so a trailing comma would admit
        # exactly the client `_from_our_page`'s absent-header case refuses.
        check("a trailing comma does not make the empty name a name",
              configured("raspberrypi.local,"), PAIR + ["raspberrypi.local"],
              "an empty pattern equals a request with no Host at all")
        check("and the pair is never listed twice",
              configured("localhost, 127.0.0.1"), PAIR)

        # `*` is a real off switch — `trustedhost.py:28` sets `allow_any` and
        # every scope walks straight through — so it is said out loud. Not
        # refused: somebody may mean it, and a server that will not start is a
        # worse answer to a deliberate setting than one that starts and says
        # what it did.
        heard = Heard()
        talker = logging.getLogger(main.__name__)
        was_level, was_propagating = talker.level, talker.propagate
        talker.setLevel(logging.INFO)
        talker.propagate = False
        talker.addHandler(heard)
        try:
            configured("raspberrypi.local")
            named = list(heard.lines)
            configured("*")
        finally:
            talker.removeHandler(heard)
            talker.setLevel(was_level)
            talker.propagate = was_propagating
        check("what it settled on is written down, because nothing else says "
              "it", named,
              ["answering to localhost, 127.0.0.1, raspberrypi.local"],
              "a refusal is a bare `Invalid host header`, with no log")
        check("and a `*` in the list is said out loud as the off switch it is",
              heard.lines[len(named):],
              ["answering to localhost, 127.0.0.1, *",
               "ALLOWED_HOSTS contains `*`, so this server answers to any "
               "name at all and the rebinding guard is off. Name the hosts "
               "instead."])
    finally:
        if before is None:
            os.environ.pop("ALLOWED_HOSTS", None)
        else:
            os.environ["ALLOWED_HOSTS"] = before

    # ------------------------------------------- and what one of them does

    async def under(host: str, origin: str | None = None) -> dict:
        """One handshake naming `host`, from a page on that same name.

        The rebinding shape rather than #90's: the attacker's page is served
        from the name whose record they have just repointed here, so the
        browser puts that one name in both headers and the origin comparison
        agrees with itself. Only something in front of the router can refuse
        it.
        """
        with a_server_of_its_own(main, [Event(
                author="model",
                content=types.Content(parts=[types.Part(text="hello")]))]):
            seeded = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="alex", state={"seeded": True})
            sent = await handshake(
                main, origin if origin is not None else f"http://{host}", host)
            after = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=seeded.id)
            return {
                "sent": [message["type"] for message in sent],
                "status": [message["status"] for message in sent
                           if message["type"].endswith("response.start")],
                "body": b"".join(
                    message.get("body", b"") for message in sent
                    if message["type"].endswith("response.body")),
                "reached": len(main.runner.calls),
                "events": len(after.events) if after else None,
            }

    # Four sends: the `ready` the accept path writes before the page
    # has asked for anything, the model's turn, and the `turnComplete` and
    # `idle` frames sent by turn_over() when downstream completes (#269, #285).
    # Spelled out rather than counted loosely, because "the socket sent something"
    # is exactly the test a refusal has to fail and a shorter list would let one
    # through.
    OPENED = ["websocket.accept", "websocket.send", "websocket.send",
              "websocket.send", "websocket.send"]
    REFUSED = ["websocket.http.response.start", "websocket.http.response.body"]

    with answering_to(main, PAIR):
        for host in ("localhost:8000", "127.0.0.1:8000"):
            got = await under(host)
            check(f"the kiosk's own two names are answered to: {host}",
                  [got["sent"], got["reached"]], [OPENED, 1],
                  "the panel and the shipped Pi both browse to one of these")

        # The issue. Every header is the attacker's own name, so #90's
        # comparison finds them equal and would let this through.
        got = await under("evil.example")
        check("a name whose record was repointed at us is refused",
              got["sent"], REFUSED, "before the router, so before the compare")
        check("with a 400 and the middleware's own body on it",
              [got["status"], got["body"]], [[400], b"Invalid host header"])
        check("and nothing was accepted on the way to refusing it",
              ["websocket.accept" in got["sent"], got["reached"],
               got["events"]], [False, 0, 0],
              "a refused handshake, not an accepted socket closed after")
        # The suffix a hand-rolled `endswith` would have waved through, and
        # the port a hand-rolled comparison would have tripped over.
        for host in ("evil.example:8000", "127.0.0.1.evil.example",
                     "localhost.evil.example:8000"):
            got = await under(host)
            check(f"and so is a name that merely contains ours: {host}",
                  got["sent"], REFUSED)
        # `Host` has been mandatory since RFC 2616 and uvicorn answers a
        # request without one itself, so nothing real gets here — which is the
        # reason to pin it, because an empty pattern in the list would open it
        # and nothing else in this file would notice.
        got = await under("", origin="http://")
        check("a client that names nothing at all is refused too",
              got["sent"], REFUSED)
        # The cost, stated rather than discovered on the Pi: a name that is
        # neither loopback spelling needs the setting, and what it looks like
        # without it is a kiosk that will not connect.
        got = await under("raspberrypi.local:8000")
        check("a name nobody listed does not work by being plausible",
              got["sent"], REFUSED, "`ALLOWED_HOSTS` is how it becomes ours")

    with answering_to(main, PAIR + ["raspberrypi.local"]):
        got = await under("raspberrypi.local:8000")
        check("and listing it is the whole of what it takes",
              [got["sent"], got["reached"]], [OPENED, 1])
        # The port is off the header before the comparison, so the terminal
        # does not have to be told which one it is served on.
        got = await under("raspberrypi.local:8010")
        check("on any port, because the guard is about the name",
              got["sent"], OPENED)

    # Why it has to be middleware, in one case. `_from_our_page` is handed the
    # rebound handshake directly, and it says yes — the two headers agree, and
    # they agree because the attacker chose them both. A host check written
    # under it in the endpoint would be a non-fix that passed every case above.
    spoofed = FakeSocket([])
    spoofed.headers = Headers({"host": "evil.example",
                               "origin": "http://evil.example"})
    check("the origin comparison on its own would have let it in",
          main._from_our_page(spoofed), True,
          "which is why the allowlist is in front of the router")
    # And the pair the comparison does refuse on its own, which the case above
    # can no longer reach through the app: the guard in front now answers it
    # first. `urlsplit("http://").netloc` is `""` and so is a missing `Host`,
    # so a comparison that reached them would find them equal.
    blank = FakeSocket([])
    blank.headers = Headers({})
    check("while an origin with no authority is still not a host with no name",
          main._from_our_page(blank), False, "neither header is an origin")


async def origin_guard_cases() -> None:
    print("\n== the socket that talks to the agent, and who may open one ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    # First and source-level, for the reason the `api_forget` signature case is
    # first: every case below reads a socket that either was or was not
    # accepted, and a guard written under `accept()` would leave all of them
    # measuring the wrong thing while still looking green.
    body = Path(main.__file__).read_text(encoding="utf-8")
    body = body[body.index("async def websocket_endpoint("):]
    guard, opened = (body.find("_from_our_page"),
                     body.find("await websocket.accept()"))
    check("the handshake is judged before it is accepted",
          0 <= guard < opened, True,
          f"guard at +{guard}, accept at +{opened}" if guard >= 0
          else "nothing is read off the handshake before `accept`")

    async def asked(origin: str | None, host: str = OUR_HOST) -> dict:
        """What one handshake did, and what it left behind.

        Three answers rather than one, because a refusal is only a refusal if
        all three hold: nothing was accepted, the model was never reached, and
        the conversation the handshake was aimed at is exactly as it was.

        The host guard in front of the router answers to every name these
        cases use, so what they measure is the comparison rather than the
        guard. Which names a real terminal answers to, and what happens to a
        handshake under any other one, is `host_guard_cases` above.
        """
        with answering_to(main, ["localhost", "127.0.0.1",
                                 "terminal.local", "raspberrypi.local"]), \
                a_server_of_its_own(main, [Event(
                author="model",
                content=types.Content(parts=[types.Part(text="hello")]))]):
            seeded = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="alex", state={"seeded": True})
            sent = await handshake(main, origin, host)
            after = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=seeded.id)
            listed = await main.session_service.list_sessions(
                app_name=main.APP_NAME, user_id="alex")
            return {
                "sent": [message["type"] for message in sent],
                # The accept path's `ready` is skipped: it carries no
                # content, and what this is reading is the model's turn.
                "said": [payload["content"]["parts"][0]["text"]
                         for message in sent
                         if message["type"] == "websocket.send"
                         and message.get("text") != READY
                         and "content" in (payload := json.loads(message["text"]))],
                "close": [(message.get("code"), message.get("reason"))
                          for message in sent
                          if message["type"] == "websocket.close"],
                "reached": len(main.runner.calls),
                "state": dict(after.state) if after else None,
                "events": len(after.events) if after else None,
                "threads": len(listed.sessions),
            }

    # Four sends: the accept path's `ready`, the model's turn, and the
    # `turnComplete` and `idle` frames sent by turn_over() when downstream
    # completes (#269, #285).
    ACCEPTED = ["websocket.accept", "websocket.send", "websocket.send",
                "websocket.send", "websocket.send"]
    REFUSED = ["websocket.close"]

    # Ours, and there are two of them. The panel arrives through
    # `ssh -L 8000:127.0.0.1:8000`, and a browser pointed at `localhost:8000`
    # and one pointed at `127.0.0.1:8000` are different origins by the spec
    # while both being the real terminal. Anything that names one of them as
    # *the* origin loses the other.
    for host in ("localhost:8000", "127.0.0.1:8000"):
        got = await asked(f"http://{host}", host)
        check(f"our own page connects, whichever loopback it named: {host}",
              got["sent"], ACCEPTED)
        check("and the model's turn still comes back down the socket",
              got["said"], ["hello"])

    # The trap in picking either one: a page served from the other is
    # cross-origin to it, and nothing about them being the same machine makes
    # them the same origin.
    got = await asked("http://localhost:8000", "127.0.0.1:8000")
    check("and the two spellings are not quietly treated as one origin",
          got["sent"], REFUSED)

    # The hole. Any page the household's browser can be pointed at, on a
    # handshake no browser preflights and #83's guard cannot reach.
    got = await asked("http://evil.example")
    check("a page on somebody else's origin is refused before the upgrade",
          got["sent"], REFUSED,
          "a bare close is the 403 uvicorn answers the handshake with")
    check("with a policy-violation code and a reason on it",
          got["close"], [(1008, "cross-origin handshake")])
    # Which nobody on the wire ever reads: uvicorn answers a pre-accept close
    # with a bare 403 and drops the code and the reason. So the log line is
    # the only account of a refusal there is, and the first thing a person
    # whose panel will not connect has to go on.
    heard = Heard()
    talker = logging.getLogger(main.__name__)
    was_propagating, talker.propagate = talker.propagate, False
    talker.addHandler(heard)
    try:
        await asked("http://evil.example")
    finally:
        talker.removeHandler(heard)
        talker.propagate = was_propagating
    check("and says out loud which two headers disagreed",
          [line for line in heard.lines if line.startswith("refused")],
          [f"refused a handshake for alex: origin=http://evil.example "
           f"host={OUR_HOST}"])
    check("the agent is never reached, so `gws` never runs for them",
          got["reached"], 0)
    check("nothing of theirs lands in the household's conversation",
          got["state"], {"seeded": True},
          "`?location=` and `?lat=`/`?lng=` never reach the state delta")
    # The count either side, which the state comparison alone would not give:
    # what a handshake writes is one state-only event appended to whichever
    # thread it resolved to, so the thread count stays put whether the guard
    # holds or not and only the event count moves.
    check("and not one event is appended to the thread they aimed at",
          [got["events"], got["threads"]], [0, 1],
          "no state delta, and no thread rolled on somebody else's schedule")

    # Absent is a decision rather than a default. Only a browser is obliged to
    # send `Origin`; refusing an absent one therefore costs no browser, and
    # `app.js` is the only thing in the tree that opens this socket, so it
    # costs no client either. **Absent is refused**, and what that buys is the
    # other half of the same hole: `--host` still defaults to `0.0.0.0`, so
    # until that changes anything on the Wi-Fi with an HTTP client could reach
    # the agent with no page involved at all.
    for origin, named in ((None, "no Origin header at all, as curl sends it"),
                          ("", "an empty Origin"),
                          ("null", "`null`, from a sandboxed iframe")):
        got = await asked(origin)
        check(f"a client that names no origin is refused too: {named}",
              got["sent"], REFUSED, "absent is refused, on purpose")
        check("and reaches the agent no further than the page did",
              got["reached"], 0)

    # A different port on this very machine is a different origin, and it is
    # where a second server on the same box sits. The one an `endswith` on the
    # hostname would wave through.
    got = await asked("http://terminal.local:9999", OUR_HOST)
    check("another port on this same machine is another origin",
          got["sent"], REFUSED)

    # And the one a `startswith` would: anybody can register a name that begins
    # with ours.
    got = await asked("http://terminal.local.evil.example:8000", OUR_HOST)
    check("so is a name that merely begins with ours", got["sent"], REFUSED)

    # The other side of the same coin, and the one the two cases above leave
    # open: a name that *ends* with ours, authority and port and all. Nothing
    # above fails if the comparison is loosened from `==` to `in` — the port
    # case and the prefix case both still refuse, because neither origin
    # contains `terminal.local:8000` whole — and `xterminal.local` is a name
    # anybody can register. Measured: with `host in origin_netloc` substituted
    # for the equality, every other case in this file still holds.
    got = await asked("http://xterminal.local:8000", OUR_HOST)
    check("nor a name that merely ends with ours", got["sent"], REFUSED)

    # Neither header on its own is an origin. `urlsplit("http://").netloc` is
    # `""` and so is a missing `Host`, so a comparison that reached them would
    # find them equal and let through a client that named neither. HTTP/1.1
    # makes `Host` mandatory, so no real client gets here — which is the whole
    # reason to pin it, because nothing else in the file would notice if the
    # guard stopped asking.
    # Since #95 the guard in front of the router reaches this one first — a
    # request carrying no `Host` is a request under no name at all — so what
    # comes back is its 400 rather than this comparison's close. What the
    # comparison itself makes of the pair is pinned in `host_guard_cases`.
    got = await asked("http://", "")
    check("and a client that named neither gets no socket either",
          got["sent"], ["websocket.http.response.start",
                        "websocket.http.response.body"],
          "`http://` against an absent Host, refused by the name it lacks")

    # Host names are case-insensitive and the comparison has to be, or a
    # browser that spells it differently loses the panel over nothing.
    got = await asked("http://TERMINAL.local:8000", OUR_HOST)
    check("while the same name in another case is the same origin",
          got["sent"], ACCEPTED)

    # A browser that reaches the terminal by another name — the Pi's own, say,
    # rather than through the tunnel. This comparison is told nothing about it:
    # the page connects to the origin it was served from, so the two headers
    # agree by construction whatever the terminal is called. The name itself
    # has to be in `ALLOWED_HOSTS`, which is the guard above and not this one.
    got = await asked("http://raspberrypi.local:8000", "raspberrypi.local:8000")
    check("and a page served under another name is our page under that name",
          got["sent"], ACCEPTED, "no origin list to edit, only a host list")


# ------------------------------------------------ what the page is talking to

def a_workspace(root: Path) -> None:
    """A small repo shaped the way this project is deployed from.

    `project/` is the directory that ships and `other/` stands in for the
    thirty that do not. Two levels because the claim under test is that
    somebody else's edit in the same repo leaves this project's revision
    clean.

    Tagged, and the tag is on neither HEAD nor `project/`, because the real
    workspace is: its only tag is `speaker-support`, five hundred commits back
    on a doorbell-project commit. That is what rules out `git describe`, and
    an untagged repo cannot tell the two apart — `describe --always` and
    `rev-parse --short HEAD` return the same string there, so the case that
    watches this would pass either way. Annotated for the same reason: plain
    `git describe` walks annotated tags only, so a lightweight one leaves the
    two indistinguishable just as an untagged repo does.
    """
    def git(*args: str) -> None:
        subprocess.run(("git", "-C", str(root), "-c", "user.name=check",
                        "-c", "user.email=check@localhost",
                        "-c", "commit.gpgsign=false",
                        "-c", "tag.gpgsign=false", *args),
                       check=True, capture_output=True, env=_git_env())

    for name in ("project", "other"):
        (root / name).mkdir()
        (root / name / "a.txt").write_text("one\n")
    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "one")
    git("tag", "-a", "somebody-elses-tag", "-m", "another project")
    (root / "other" / "b.txt").write_text("two\n")
    git("add", "-A")
    git("commit", "-qm", "two")


async def revision_cases() -> None:
    print("\n== what the page is talking to ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    stamped, at = os.environ.get("AI_TERMINAL_REVISION"), main.REPO_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a_workspace(root)
            head = subprocess.run(
                ("git", "-C", str(root), "rev-parse", "--short", "HEAD"),
                capture_output=True, text=True, env=_git_env()).stdout.strip()
            main.REPO_ROOT = root / "project"
            os.environ.pop("AI_TERMINAL_REVISION", None)

            check("a checkout answers with its own HEAD",
                  main._revision(), head)
            # The workspace repo holds thirty projects and takes automatic
            # commits from the Discord bridge, so an unscoped dirty flag would
            # be lit most of the time by edits that never reach the Pi.
            (root / "other" / "a.txt").write_text("two\n")
            check("and somebody else's edit in the same repo is not its mess",
                  main._revision(), head)
            (root / "project" / "a.txt").write_text("two\n")
            check("its own is, and the SHA alone would have hidden it",
                  main._revision(), f"{head}-dirty")

            # The Pi is deployed by copy, so the value the unit was given
            # outranks anything the machine can work out for itself.
            os.environ["AI_TERMINAL_REVISION"] = "c71311d"
            check("a stamp outranks the checkout it sits in",
                  main._revision(), "c71311d")

        with tempfile.TemporaryDirectory() as tmp:
            main.REPO_ROOT = Path(tmp)
            os.environ.pop("AI_TERMINAL_REVISION", None)
            check("an unstamped copy says so rather than guessing",
                  main._revision(), "unknown")
            os.environ["AI_TERMINAL_REVISION"] = "   "
            check("and a blank in the unit is not somebody's answer",
                  main._revision(), "unknown")
    finally:
        main.REPO_ROOT = at
        if stamped is None:
            os.environ.pop("AI_TERMINAL_REVISION", None)
        else:
            os.environ["AI_TERMINAL_REVISION"] = stamped

    # The wiring claim: one line reads it and one line ships it.
    was = main.REVISION
    main.REVISION = "c71311d-dirty"
    try:
        check("the page is handed whatever the process decided it was",
              (await main.api_config()).get("revision"), "c71311d-dirty")
    finally:
        main.REVISION = was

    # And decided once. A process serves the bytes it loaded; re-reading git
    # here would describe the tree, which moves on without it — twice in an
    # hour on 2026-09-09, while the server from 12:50 kept running.
    was_fn, main._revision = main._revision, lambda: "read-per-request"
    try:
        check("having decided that at import, not at every request",
              (await main.api_config()).get("revision"), main.REVISION)
    finally:
        main._revision = was_fn


# --------------------------------------------- when the build on screen is from

async def build_cases() -> None:
    """The date beside that SHA, and the one place it is allowed to come from.

    Same three-way resolution as the revision and the same fixture repo, plus
    the thing this feature is actually about: the stamp reaches the page
    *inside the document*, not through a fetch the page makes afterwards.

    That is the whole of #261 and it is easy to get backwards. #256 measured it
    on pi2 on 2026-09-11: the document Chromium was showing had loaded at
    23:03 the night before and the unit under it had been restarted at 08:52
    that morning, so a badge fed from a live `/api/config` would have named
    the morning's build over the previous night's JavaScript — confirming the
    fixes were on screen at the one moment they were not. So the cases below
    move `REVISION` and `BUILD_LABEL` after import and read what `root()`
    hands back, which is the only thing that can tell a stamped document from
    a fetched one.
    """
    print("\n== when the build the page is running was made ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    stamped, at = os.environ.get("AI_TERMINAL_BUILT"), main.REPO_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a_workspace(root)
            when = subprocess.run(
                ("git", "-C", str(root), "show", "-s", "--format=%cI", "HEAD"),
                capture_output=True, text=True, env=_git_env()).stdout.strip()
            main.REPO_ROOT = root / "project"
            os.environ.pop("AI_TERMINAL_BUILT", None)

            check("a checkout answers with its own HEAD's commit date",
                  main._built(), when)
            # An offset, because the badge is read in a kitchen in JST and a
            # bare naive string would be an hour nobody can place. `%cI` is
            # the commit's own, which is what `git log` shows here.
            check("dated at an offset rather than as a bare local time",
                  main._built()[10:11] == "T" and len(main._built()) == 25,
                  True, main._built())
            # The Pi is deployed by copy, so the drop-in outranks anything the
            # machine can work out for itself — and this is the whole reason
            # there is a second variable rather than a `git` call.
            os.environ["AI_TERMINAL_BUILT"] = "2026-09-11T07:33:00+09:00"
            check("a stamp outranks the checkout it sits in",
                  main._built(), "2026-09-11T07:33:00+09:00")

        with tempfile.TemporaryDirectory() as tmp:
            main.REPO_ROOT = Path(tmp)
            os.environ.pop("AI_TERMINAL_BUILT", None)
            check("an unstamped copy has no date rather than a made-up one",
                  main._built(), "")
            os.environ["AI_TERMINAL_BUILT"] = "   "
            check("and a blank in the unit is not somebody's answer",
                  main._built(), "")
    finally:
        main.REPO_ROOT = at
        if stamped is None:
            os.environ.pop("AI_TERMINAL_BUILT", None)
        else:
            os.environ["AI_TERMINAL_BUILT"] = stamped

    # What the corner actually says. To the minute: seconds on a build stamp
    # are noise at 10 px, and the field exists to be compared against a closed
    # issue's timestamp by eye.
    check("the corner reads as a date somebody can compare by eye",
          main._label("2026-09-11T07:33:14+09:00", "4822126"),
          "2026-09-11 07:33 · 4822126")
    check("an offset-aware stamp converts to the reader's local time zone (#699)",
          main._label("2026-09-25T19:22:28-04:00", "1d067ea"),
          "2026-09-26 08:22 · 1d067ea")
    # The two degradations, and the difference between them is the point. A
    # deploy that could not date its commit shows the SHA alone; one that
    # showed the *last* deploy's date would be the badge actively lying, which
    # is worse than no badge.
    check("with no date, the SHA stands on its own rather than beside a "
          "fabricated one", main._label("", "4822126"), "4822126")
    check("and a drop-in carrying something that is not a timestamp is the "
          "same case, not sixteen characters of it",
          main._label("last tuesday", "4822126"), "4822126")
    check("with neither, the corner is as honest as the log line",
          main._label("", "unknown"), "unknown")

    # The wiring claim, and the one this feature lives or dies on. The stamp
    # is in the bytes of the document, so a page loaded before a restart goes
    # on saying what it said.
    was = main.STATIC_REVISION, main.BUILD_LABEL
    main.STATIC_REVISION = "aaaa11112222"
    main.BUILD_LABEL = "2026-09-10 23:01 · abc1234"
    try:
        res = await main.root()
        served = res.body.decode() if hasattr(res, "body") else ""
        check("the page is served with this process's build written into it",
              [main.BUILD_SLOT in served, main.STATIC_SLOT in served,
               "2026-09-10 23:01 · abc1234" in served,
               'data-static-revision="aaaa11112222"' in served],
              [False, False, True, True])
        # The attribute is the digest and not the SHA, because `checkRevision`
        # compares `static_revision` since #256. A page stamped with the SHA
        # would differ from every answer the server gives — a reload per
        # reconnect, which is the failure #256's own guard is written against.
        check("and the compared field is the one the reconnect compares",
              main.STATIC_REVISION in served
              and (await main.api_config())["static_revision"]
              == main.STATIC_REVISION, True)
        # The badge needs an element to be, and `app.js` needs an id to mark
        # stale. A substitution that landed in a page with neither would pass
        # every case above and show nothing.
        check("into an element the page has, in the corner it asked for",
              'id="buildBadge"' in served and 'class="build"' in served, True)
        # Cache-busting queries on static assets (#178).
        check("the static assets carry cache-busting queries matching static revision",
              ['href="/static/css/style.css?v=' + main.STATIC_REVISION + '"' in served,
               'src="/static/js/app.js?v=' + main.STATIC_REVISION + '"' in served],
              [True, True])

        # And now the restart. The constants move the way an import in a new
        # process would move them; the document already handed out must not.
        main.STATIC_REVISION = "bbbb33334444"
        main.BUILD_LABEL = "2026-09-11 08:51 · def5678"
        after_res = await main.root()
        after = after_res.body.decode() if hasattr(after_res, "body") else ""
        check("a page served after a restart carries the new build, and the "
              "one served before it is untouched",
              ["def5678" in after, "abc1234" in served, "def5678" in served],
              [True, True, False],
              "a badge fetched after the fact would say def5678 on both")

        # The same failure one level down. A `/` that a browser may answer
        # from its own cache hands back a page stamped by a server that has
        # since been replaced — which is the badge lying, for a saving of
        # 9 KB per reload.
        check("and the document is not cacheable, since it now carries "
              "process state",
              (await main.root()).headers.get("cache-control"), "no-store")

        # Static assets carry Cache-Control: no-cache so Chromium sends conditional
        # revalidation requests instead of serving stale assets (#178).
        from starlette.testclient import TestClient
        client = TestClient(main.app, base_url="http://localhost")
        try:
            check("static css returns Cache-Control: no-cache",
                  client.get("/static/css/style.css").headers.get("cache-control"),
                  "no-cache")
            check("static js returns Cache-Control: no-cache",
                  client.get("/static/js/app.js").headers.get("cache-control"),
                  "no-cache")
        finally:
            client.close()
    finally:
        main.STATIC_REVISION, main.BUILD_LABEL = was

    # Escaped on the way in. Both fields come off `Environment=` lines in a
    # systemd drop-in today, so neither can hold a quote — but the day one of
    # them can is not the day to discover it lands raw inside an attribute.
    was = main.STATIC_REVISION, main.BUILD_LABEL
    main.STATIC_REVISION, main.BUILD_LABEL = '"><script>x()</script>', "x&y"
    try:
        res = await main.root()
        served = res.body.decode() if hasattr(res, "body") else ""
        check("what the drop-in said is escaped, not pasted into the markup",
              ["<script>x()</script>" in served, "x&amp;y" in served],
              [False, True])
    finally:
        main.STATIC_REVISION, main.BUILD_LABEL = was


async def offline_safety_cases() -> None:
    """Offline safety guards: no case in this suite may make live model calls.

    `recap.summarise` and sidecars build `genai.Client` to talk to Gemini /
    Vertex AI. An offline check suite must never hit real API endpoints (#287).
    """
    print("\n== offline check suite safety guards ==")
    from ai_terminal.live import main, recap
    from ai_terminal.live.tools import _client

    # genai_client raises when called without a case-specific stub (#287).
    try:
        _client.genai_client()
        reached = True
    except AssertionError:
        reached = False
    check("genai_client is guarded against un-stubbed real model calls",
          reached, False,
          "#287: suite fails loudly if any case forgets to stub model calls")
    CALLED_GENAI.clear()

    # grounding_client raises when called without a case-specific stub (#307).
    try:
        _client.grounding_client()
        reached = True
    except AssertionError:
        reached = False
    check("grounding_client is guarded against un-stubbed real model calls",
          reached, False,
          "#307: suite fails loudly if any case forgets to stub grounding calls")
    CALLED_GENAI.clear()

    # recap.summarise on speech events hits the guard without a stub (#287).
    event = Event(author="user", content=types.Content(
        parts=[types.Part.from_text(text="hello from earlier")]))
    res = await recap.summarise([event])
    check("recap.summarise on speech events hits the suite guard",
          [res, len(CALLED_GENAI)], ["", 1],
          "#287: summarise caught AssertionError and returned empty recap")
    CALLED_GENAI.clear()

    # stub_carry intercepts rolls before they reach recap.summarise (#287).
    with stub_carry(main) as carried:
        await main._carry_forward("user", "roll-old", None)
    check("stub_carry intercepts rolls before recap.summarise",
          [len(carried), len(CALLED_GENAI)], [1, 0],
          "#287: stub_carry isolates roll cases from summarise")
    CALLED_GENAI.clear()


async def gap_arrival_cases() -> None:
    """What happens when a page connects after IDLE_GAP_S has passed.

    A fresh connection past the gap creates a new session. Any idle pages
    holding the rolled session must be carried across to the new session
    via held.move(session.id), so one profile does not split into two threads
    and the wake word does not arm multiple screens (#165). Mid-conversation
    pages (held.live() == True) are left behind on the rolled session.
    """
    print("\n== gap arrival: thread roll and idle page migration ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    # 1. Direct registry unit tests on _thread
    with a_server_of_its_own(main), stub_carry(main):
        service = main.session_service
        s1 = await service.create_session(
            app_name=main.APP_NAME, user_id="alex", state={"key": "v1"})

        moved_to: dict[str, list[str]] = {"idle1": [], "idle2": [], "live1": []}

        def make_page(conn_name: str, is_live: bool):
            return main._Page(
                live=lambda: is_live,
                move=lambda sid: moved_to[conn_name].append(sid),
            )

        was_pages = {k: dict(v) for k, v in main._PAGES.items()}
        was_gap = main.IDLE_GAP_S
        main.IDLE_GAP_S = -1.0
        try:
            main._PAGES[s1.id] = {
                "idle1": make_page("idle1", is_live=False),
                "idle2": make_page("idle2", is_live=False),
                "live1": make_page("live1", is_live=True),
            }

            new_sid = await main._thread("alex", {"key": "v2"})

            check("a page connecting after IDLE_GAP_S rolls the session",
                  new_sid != s1.id, True,
                  f"rolled {s1.id} to fresh {new_sid}")

            check("idle pages on rolled session are moved to the fresh session",
                  [moved_to["idle1"], moved_to["idle2"]],
                  [[new_sid], [new_sid]],
                  "held.move(new_sid) called for both idle pages")

            check("live page mid-conversation is not moved to the fresh session",
                  moved_to["live1"], [],
                  "held.live() == True left on rolled session")

        finally:
            main.IDLE_GAP_S = was_gap
            main._PAGES.clear()
            main._PAGES.update(was_pages)

    # 2. Integration test through websocket_endpoint
    wake_frame = json.dumps({"type": "wake"})
    real_gap = main.IDLE_GAP_S
    runner = WakeRunner()
    stay = asyncio.Event()
    kiosk_up = asyncio.Event()
    tab_up = asyncio.Event()
    socks: list[FakeSocket] = []
    out: dict = {}

    def kiosk_script():
        async def settle():
            kiosk_up.set()
            await stay.wait()
            return {"type": "websocket.disconnect"}
        return [settle]

    def tab_script():
        async def settle():
            tab_up.set()
            await stay.wait()
            return {"type": "websocket.disconnect"}
        return [settle]

    try:
        with a_server_of_its_own(main), stub_carry(main):
            main.runner = runner
            socks.append(FakeSocket(kiosk_script(), kiosk="1"))
            kiosk = asyncio.create_task(
                main.websocket_endpoint(socks[0], "alex"))
            await asyncio.wait_for(kiosk_up.wait(), 10)

            initial_sid = list(main._PAGES.keys())[0]

            # Gap passes
            main.IDLE_GAP_S = -1.0

            socks.append(FakeSocket(tab_script()))
            tab = asyncio.create_task(
                main.websocket_endpoint(socks[1], "alex"))
            await asyncio.wait_for(tab_up.wait(), 10)

            fresh_sid = [sid for sid in main._PAGES if sid != initial_sid]
            fresh_id = fresh_sid[0] if fresh_sid else None

            out["fresh_sid"] = fresh_id
            out["initial_sid"] = initial_sid
            out["sessions"] = len(screen.sessions())
            out["screens"] = len(screen._SINKS.get(fresh_id, [])) if fresh_id else 0
            out["old_screens"] = len(screen._SINKS.get(initial_sid, []))

            for s in socks:
                s.sent.clear()
            out["armed"] = sum([
                await screen.push_wake(sid, {"type": "wake"})
                for sid in screen.sessions()])
            out["woke"] = [s.sent.count(wake_frame) for s in socks]

            stay.set()
            await asyncio.wait_for(tab, 10)
            await asyncio.wait_for(kiosk, 10)
    finally:
        main.IDLE_GAP_S = real_gap

    check("tab connecting after the gap gets a fresh session id",
          out["fresh_sid"] is not None and out["fresh_sid"] != out["initial_sid"], True,
          f"rolled from {out['initial_sid']} to {out['fresh_sid']}")
    check("kiosk was moved from old session to fresh session",
          [out["old_screens"], out["screens"]], [0, 2],
          "both screens now registered on fresh session, old session has 0 screens")
    check("wake word arms only one profile session with kiosk screen preferred",
          [out["sessions"], out["armed"], out["woke"]], [1, 1, [1, 0]],
          "one thread, one wake dispatched to kiosk screen")


async def calculate_wiring_cases() -> None:
    """The safe math calculation tool for live agent and planner (#393)."""
    print("\n== safe math calculation tool wiring (#393) ==")
    from ai_terminal.live.tools import calculate, calculate_tool
    from ai_terminal.live import agent
    from ai_terminal.live.tools import plan as plan_mod

    built_tools = agent.build_agent().tools
    check("calculate_tool is registered on agent",
          calculate_tool in built_tools, True)

    # Basic arithmetic and formatting
    res = calculate("125 * 34")
    check("basic multiplication returns exact product",
          (res.get("result"), res.get("formatted")), (4250, "4,250"))

    res = calculate("sqrt(144) + 2**8")
    check("powers and sqrt evaluate correctly",
          (res.get("result"), res.get("formatted")), (268.0, "268"))

    res = calculate("(1500 * 0.1) / 12")
    check("float division formatting",
          (res.get("result"), res.get("formatted")), (12.5, "12.5"))

    res = calculate("sin(0)")
    check("math function sin evaluates correctly",
          (res.get("result"), res.get("formatted")), (0.0, "0"))

    res = calculate("pi")
    check("math constant pi evaluates correctly",
          res.get("formatted"), "3.14159")

    # kwargs compatibility (expression=...)
    res_kw = calculate(expression="25 * 4")
    check("expression keyword argument works",
          (res_kw.get("result"), res_kw.get("formatted")), (100, "100"))

    # Division by zero handled safely without exception
    res_zero = calculate("10 / 0")
    check("division by zero returns error string without raising",
          res_zero.get("error"), "Division by zero")

    # Empty expression
    res_empty = calculate("")
    check("empty expression returns error string",
          res_empty.get("error"), "Empty expression")

    # Safety limits: large exponent
    res_pow = calculate("2 ** 10001")
    check("exponent exceeding limit is rejected",
          "exceeds safety limit" in res_pow.get("error", ""), True)

    # Safety limits: arbitrary code execution injection attempt
    res_inject = calculate("__import__('os').system('ls')")
    check("arbitrary code injection attempt is blocked",
          bool(res_inject.get("error")), True)

    # Safety limits: unsupported built-in function
    res_bad_func = calculate("eval('1+1')")
    check("unsupported function call is rejected",
          "Unknown variable or function" in res_bad_func.get("error", ""), True)

    # Planner tools include calculate_tool
    sub_tools = plan_mod._tools()
    sub_names = [getattr(t, "name", "") for t in sub_tools]
    check("planner sub_tools include calculate",
          "calculate" in sub_names, True)


async def starter_suggestions_cases() -> None:
    print("\n== dynamic starter research via search grounding (#489) ==")
    import time
    from ai_terminal.live import main
    from ai_terminal.live.tools import _client

    # Route registration
    routes = {r.path: r for r in main.app.routes}
    check("/api/starters/suggestions is registered", "/api/starters/suggestions" in routes, True)
    check("/api/starters/suggestions is a GET route",
          "GET" in getattr(routes.get("/api/starters/suggestions"), "methods", set()), True)

    # Empty query returns empty suggestions immediately without calling model
    empty_resp = await main.api_starters_suggestions(q="")
    check("empty query returns empty suggestions list", empty_resp, {"suggestions": []})

    # Assert no hardcoded template literals exist in main.py (#489)
    main_code = Path(main.__file__).read_text(encoding="utf-8")
    banned_literals = (
        "の天気を教えて",
        "の週間天気",
        "おすすめのお店",
        "明日の天気",
        "週間予報",
        "雨雲・降水確率",
        "気温と服装",
        "Tomorrow's Weather",
        "7-Day Forecast",
        "Chance of Rain",
        "Top Restaurants",
        "Fastest Route",
        "Top Sights",
    )
    for lit in banned_literals:
        check(f"no hardcoded template literal {lit!r} in main.py", lit in main_code, False)

    # Legacy static/scraper functions removed
    check("_is_unrelated_or_address is removed from main",
          hasattr(main, "_is_unrelated_or_address"), False)
    check("_generate_contextual_suggestions is removed from main",
          hasattr(main, "_generate_contextual_suggestions"), False)
    check("_fetch_search_suggestions_sync is removed from main",
          hasattr(main, "_fetch_search_suggestions_sync"), False)

    # Dynamic search-grounded research with mock Gemini response
    class _MockModels:
        def __init__(self, response_text: str):
            self._text = response_text
            self.last_call = None

        def generate_content(self, *args, **kwargs):
            self.last_call = (args, kwargs)
            return types_mod.SimpleNamespace(text=self._text)

    mock_items = [
        {"label": "見どころ", "prompt": "鎌倉の主要な見どころを教えて", "icon": "explore"},
        {"label": "おすすめグルメ", "prompt": "鎌倉で人気のご当地グルメを教えて", "icon": "restaurant"},
        {"label": "混雑状況", "prompt": "鎌倉の現在の混雑状況を教えて", "icon": "group"},
    ]
    mock_models = _MockModels(json.dumps(mock_items))
    mock_client = types_mod.SimpleNamespace(models=mock_models)

    before_client = _client.grounding_client
    _client.grounding_client = lambda: mock_client
    try:
        main._STARTER_SUGGESTIONS_CACHE.clear()
        resp = await main.api_starters_suggestions(q="鎌倉", lang="ja")
        items = resp.get("suggestions", [])
        check("dynamic starters returns parsed list", len(items), 3)
        check("dynamic starter labels match model output",
              [it["label"] for it in items], ["見どころ", "おすすめグルメ", "混雑状況"])
        check("dynamic starter prompts match model output",
              [it["prompt"] for it in items],
              ["鎌倉の主要な見どころを教えて", "鎌倉で人気のご当地グルメを教えて", "鎌倉の現在の混雑状況を教えて"])
        check("dynamic starter icons match model output",
              [it["icon"] for it in items], ["explore", "restaurant", "group"])

        # Grounding tool configuration check
        config = mock_models.last_call[1].get("config") if mock_models.last_call else None
        tools = getattr(config, "tools", []) or []
        check("grounding call configured google_search tool",
              any(hasattr(t, "google_search") and t.google_search is not None for t in tools),
              True)

        # Cache check: repeated request does not invoke model again
        mock_models.last_call = None
        cached_resp = await main.api_starters_suggestions(q="鎌倉", lang="ja")
        check("cached starter suggestions returned identical results", cached_resp, resp)
        check("cache hit made no generate_content call", mock_models.last_call, None)
    finally:
        _client.grounding_client = before_client

    # Markdown-fenced JSON handling and default icon
    fenced_raw = "```json\n[{\"label\": \"紫陽花\", \"prompt\": \"鎌倉の紫陽花の見頃はいつですか\"}]\n```"
    fenced_models = _MockModels(fenced_raw)
    _client.grounding_client = lambda: types_mod.SimpleNamespace(models=fenced_models)
    try:
        main._STARTER_SUGGESTIONS_CACHE.clear()
        fenced_items = await main._research_topic_starters("鎌倉", lang="ja")
        check("markdown-fenced json is parsed cleanly", len(fenced_items), 1)
        check("missing icon defaults to lightbulb", fenced_items[0]["icon"], "lightbulb")
        check("fenced item label parsed correctly", fenced_items[0]["label"], "紫陽花")
    finally:
        _client.grounding_client = before_client

    # Fallback on model error returns empty list without raising
    class _ErrorModels:
        def generate_content(self, *args, **kwargs):
            raise RuntimeError("API quota exceeded or network unavailable")

    _client.grounding_client = lambda: types_mod.SimpleNamespace(models=_ErrorModels())
    try:
        main._STARTER_SUGGESTIONS_CACHE.clear()
        res_error = await main._research_topic_starters("ErrorTopic", lang="ja")
        check("model error returns empty list without raising", res_error, [])
    finally:
        _client.grounding_client = before_client

    # Strict timeout fallback: returns empty list on timeout without raising
    def _slow_generate(*args, **kwargs):
        time.sleep(0.1)
        return "[]"

    before_timeout = main.RESEARCH_STARTERS_TIMEOUT_S
    before_call = main._call_grounded_starters
    main.RESEARCH_STARTERS_TIMEOUT_S = 0.02
    main._call_grounded_starters = _slow_generate
    try:
        main._STARTER_SUGGESTIONS_CACHE.clear()
        res_timeout = await main._research_topic_starters("SlowTopic", lang="ja")
        check("model timeout returns empty list without raising", res_timeout, [])
    finally:
        main.RESEARCH_STARTERS_TIMEOUT_S = before_timeout
        main._call_grounded_starters = before_call

    # Cache poisoning check: empty suggestions (timeout or failure) must not be cached (#758)
    main._STARTER_SUGGESTIONS_CACHE.clear()
    class _EmptyModels:
        def generate_content(self, *args, **kwargs):
            return types_mod.SimpleNamespace(text="[]")
    _client.grounding_client = lambda: types_mod.SimpleNamespace(models=_EmptyModels())
    try:
        empty_res = await main.api_starters_suggestions(q="tokyo", lang="ja")
        check("empty model result returns empty suggestions", empty_res, {"suggestions": []})
        check("empty suggestions not stored in cache (#758)", ("tokyo", "", "ja") in main._STARTER_SUGGESTIONS_CACHE, False)

        # Timeout setting check (#758)
        check("RESEARCH_STARTERS_TIMEOUT_S is at least 4.0s (#758)", main.RESEARCH_STARTERS_TIMEOUT_S >= 4.0, True)
    finally:
        _client.grounding_client = before_client



async def place_unwrap_cases() -> None:
    """place_info un-nests wrapped payloads from tool coroutines (#478)."""
    print("\n== place_info un-nests wrapped tool coroutines (#478) ==")
    before = (place.web_search, place.image_search,
              place.youtube_search, place.maps_search)

    async def mock_web(query, lang=""):
        return {
            "web_search": {
                "summary": "A famous garden in Kanazawa.",
                "sources": [{"title": "Kenrokuen"}],
                "search_queries": ["Kenrokuen"],
            }
        }

    async def mock_image(p, lang=""):
        return {
            "image_search": {
                "images": [{"url": "https://example.com/pic.jpg"}],
            }
        }

    async def mock_youtube(p, lang=""):
        return {
            "youtube_search": {
                "videos": [{"id": "v123"}],
            }
        }

    async def mock_maps(p, near="", lang="", ctx=None):
        return {
            "maps_search": {
                "places": [{"name": "Kenroku-en"}],
                "near": "Kanazawa",
            }
        }

    place.web_search = mock_web
    place.image_search = mock_image
    place.youtube_search = mock_youtube
    place.maps_search = mock_maps
    try:
        res = await place.place_info("兼六園", "どんな庭園？", "Kanazawa", "ja")
    finally:
        (place.web_search, place.image_search,
         place.youtube_search, place.maps_search) = before

    check("place_info preserves images from wrapped image_search",
          [len(res.get("images", [])),
           (res.get("images") or [{}])[0].get("url")],
          [1, "https://example.com/pic.jpg"])
    check("place_info preserves summary from wrapped web_search",
          res.get("summary"), "A famous garden in Kanazawa.")
    check("place_info preserves videos from wrapped youtube_search",
          [len(res.get("videos", [])),
           (res.get("videos") or [{}])[0].get("id")],
          [1, "v123"])
    check("place_info preserves places from wrapped maps_search",
          [len(res.get("places", [])),
           (res.get("places") or [{}])[0].get("name")],
          [1, "Kenroku-en"])


async def tool_query_normalization_cases() -> None:
    """Normalize agent tool queries for grounding and sub-agent dispatch (#598)."""
    print("\n== tool query normalization (#598) ==")
    flat = " ".join(agent.INSTRUCTION.split())
    # 1. Prompt instructions in agent.py
    check("instruction contains tool query normalization section",
          "### Tool query normalization" in agent.INSTRUCTION, True)
    check("instruction guides grounding tools to use concise keyword terms",
          "concise, keyword-focused entity terms without conversational filler" in flat, True)
    check("instruction guides place_info to pass proper entity name only",
          "Strictly pass the proper entity/place name only" in flat, True)
    check("instruction guides sub-agent tools to pass scoped directives",
          "concise scoped directive that explicitly specifies the target service/domain and entity" in flat, True)

    # 2. place_info query sanitization in place.py
    check("place query sanitization trims leading conversational fillers",
          place._sanitize_place("where is Tokyo Tower?"), "Tokyo Tower")
    check("place query sanitization trims question and descriptive clause",
          place._sanitize_place("What is Shibuya Crossing and why is it famous?"), "Shibuya Crossing")
    check("place query sanitization trims trailing Japanese questions",
          place._sanitize_place("金閣寺とは？"), "金閣寺")
    check("place query sanitization trims trailing Japanese filler",
          place._sanitize_place("富士山について教えて？"), "富士山")
    check("place query sanitization preserves proper entity names",
          place._sanitize_place("Shibuya Crossing Tokyo"), "Shibuya Crossing Tokyo")
    check("place query sanitization trims tell me about",
          place._sanitize_place("tell me about Senso-ji temple"), "Senso-ji temple")

    # Verify place_info passes sanitized place to maps_search
    captured_maps_place = []
    before_maps = place.maps_search

    async def mock_maps(p, near="", lang="", ctx=None):
        captured_maps_place.append(p)
        return {"places": []}

    place.maps_search = mock_maps
    try:
        await place.place_info("What is Shibuya Crossing and why is it famous?", "", "Tokyo", "en")
    finally:
        place.maps_search = before_maps

    check("place_info passes sanitized place to maps_search",
          captured_maps_place, ["Shibuya Crossing"])

    # 3. youtube_search.py query formatting and site:youtube.com constraint
    check("youtube prompt formats query with site:youtube.com and directive",
          youtube_search._format_search_prompt("kettle descaling"),
          "kettle descaling YouTube site:youtube.com")
    check("youtube prompt strips duplicate 'youtube video' suffix",
          youtube_search._format_search_prompt("kettle descaling youtube video"),
          "kettle descaling YouTube site:youtube.com")
    check("youtube prompt preserves existing site:youtube.com constraint",
          youtube_search._format_search_prompt("kettle descaling site:youtube.com"),
          "kettle descaling YouTube site:youtube.com")
    check("youtube prompt strips 'youtube video' suffix when site:youtube.com already present",
          youtube_search._format_search_prompt("kettle descaling site:youtube.com youtube video"),
          "kettle descaling YouTube site:youtube.com")
    check("youtube prompt handles site:youtube.com alone",
          youtube_search._format_search_prompt("site:youtube.com"),
          "YouTube site:youtube.com")
    check("youtube prompt formats arbitrary query with YouTube keyword",
          youtube_search._format_search_prompt("cooking"),
          "cooking YouTube site:youtube.com")

    # Check payload construction in _youtube_search_sync
    posted_payloads = []

    class FakeSession:
        def post(self, url, data=None, headers=None, timeout=None):
            posted_payloads.append(json.loads(data.decode() if isinstance(data, bytes) else data))

            class FakeResp:
                def raise_for_status(self): pass
                def json(self): return {"candidates": [{"groundingMetadata": {}}]}
            return FakeResp()

    before_session = youtube_search.authed_session, youtube_search.search_url
    youtube_search.authed_session = lambda: FakeSession()
    youtube_search.search_url = lambda: "https://example.invalid/"
    try:
        youtube_search._youtube_search_sync("kettle descaling", "en")
        check("youtube_search_sync payload contains site:youtube.com search prompt",
              posted_payloads[-1]["contents"][0]["parts"][0]["text"],
              "kettle descaling YouTube site:youtube.com")
        check("youtube_search_sync payload systemInstruction enforces site:youtube.com",
              "site:youtube.com" in (
                  posted_payloads[-1].get("systemInstruction", {})
                  .get("parts", [{}])[0]
                  .get("text", "")
              ),
              True)
        check("youtube_search_sync payload systemInstruction preserves language directive",
              youtube_search.search_in("en") in (
                  posted_payloads[-1].get("systemInstruction", {})
                  .get("parts", [{}])[0]
                  .get("text", "")
              ),
              True)
    finally:
        youtube_search.authed_session, youtube_search.search_url = before_session

    # Check fallback retry when grounding drops site:youtube.com
    retry_posted = []

    class RetrySession:
        def __init__(self):
            self.calls = 0

        def post(self, url, data=None, headers=None, timeout=None):
            self.calls += 1
            payload = json.loads(data.decode() if isinstance(data, bytes) else data)
            retry_posted.append(payload)

            class FakeResp:
                def __init__(self, call_num):
                    self.call_num = call_num
                def raise_for_status(self): pass
                def json(self):
                    if self.call_num == 1:
                        return {
                            "candidates": [{
                                "groundingMetadata": {
                                    "webSearchQueries": ["kettle descaling"],
                                    "groundingChunks": [{"web": {"uri": "https://example.com/blog"}}]
                                }
                            }]
                        }
                    else:
                        return {
                            "candidates": [{
                                "groundingMetadata": {
                                    "webSearchQueries": ["kettle descaling site:youtube.com"],
                                    "groundingChunks": [{"web": {"uri": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}}]
                                }
                            }]
                        }
            return FakeResp(self.calls)

    retry_sess = RetrySession()
    before_session = youtube_search.authed_session, youtube_search.search_url
    before_resolve = youtube_search._resolve_video_id
    before_title = youtube_search._video_title
    youtube_search.authed_session = lambda: retry_sess
    youtube_search.search_url = lambda: "https://example.invalid/"
    youtube_search._resolve_video_id = lambda uri: "dQw4w9WgXcQ" if "youtube.com" in uri else ""
    youtube_search._video_title = lambda vid: "How to Descale a Kettle" if vid else ""
    try:
        res = youtube_search._youtube_search_sync("kettle descaling", "en")
        check("youtube_search triggers retry when grounding drops constraint",
              len(retry_posted), 2)
        check("youtube_search retry payload contains reinforced prompt",
              "site:youtube.com" in retry_posted[-1]["contents"][0]["parts"][0]["text"],
              True)
        check("youtube_search retry payload uses language-aware video keyword for en",
              retry_posted[-1]["contents"][0]["parts"][0]["text"],
              "kettle descaling YouTube video site:youtube.com")
        check("youtube_search resolves videos on retry",
              res["watchable"], 1)

        retry_posted.clear()
        retry_sess.calls = 0
        youtube_search._youtube_search_sync("ケトル", "ja")
        check("youtube_search retry payload uses language-aware video keyword for ja",
              retry_posted[-1]["contents"][0]["parts"][0]["text"],
              "ケトル YouTube 動画 site:youtube.com")
    finally:
        youtube_search.authed_session, youtube_search.search_url = before_session
        youtube_search._resolve_video_id = before_resolve
        youtube_search._video_title = before_title


async def camera_wiring_cases() -> None:
    print("\n== camera tool wiring and SDM WebRTC stream management (#742) ==")
    from starlette.requests import Request
    from starlette.exceptions import HTTPException
    from ai_terminal.live import main
    from ai_terminal.live.tools import _sdm, camera, camera_tool

    # 1. Tool export and registration
    check("camera tool is callable", callable(camera), True)
    check("camera_tool is exported FunctionTool", isinstance(camera_tool, FunctionTool), True)
    check("camera_tool has name 'camera'", getattr(camera_tool, "name", None), "camera")
    built_tools = agent.build_agent().tools
    check("camera_tool is registered on agent",
          camera_tool in built_tools, True)

    # 2. Steering instruction
    cam_bullet = tool_bullet("camera")
    check("agent instruction includes camera steering bullet", bool(cam_bullet), True)
    check("camera steering bullet mentions living room / nest hub",
          "living room" in cam_bullet.lower() or "nest hub" in cam_bullet.lower(), True)

    # 3. Side-channel frame emission
    frames: list[dict] = []

    async def to_screen(frame: dict) -> None:
        frames.append(frame)

    screen.register("cam-test-session", to_screen)
    try:
        class FakeContext:
            session_id = "cam-test-session"
            turn_id = "cam-turn-1"
        res = await camera(device="living_room", tool_context=FakeContext())
        check("camera tool returns streaming status", res.get("camera", {}).get("status"), "streaming")
        check("camera tool returns device", res.get("camera", {}).get("device"), "living_room")
        await asyncio.sleep(0.01)
        check("screen received camera frame", bool(frames), True)
        frame = frames[-1]
        check("camera tool pushes camera frame type", frame.get("type"), "camera")
        check("camera frame carries turn_id", frame.get("turn_id"), "cam-turn-1")
        check("camera frame data carries device", frame.get("data", {}).get("device"), "living_room")
    finally:
        screen.unregister("cam-test-session")

    # 4. _sdm module defaults & functions
    check("_sdm exports generate_webrtc_stream", callable(_sdm.generate_webrtc_stream), True)
    check("_sdm exports extend_webrtc_stream", callable(_sdm.extend_webrtc_stream), True)
    check("_sdm exports stop_webrtc_stream", callable(_sdm.stop_webrtc_stream), True)
    from unittest.mock import patch
    with patch.dict(os.environ, {"SDM_PROJECT_ID": "test-project"}):
        check("_sdm project_id returns configured string", _sdm.project_id(), "test-project")

    # 5. Route registrations in main.app
    routes = {r.path: r for r in main.app.routes}
    check("/api/camera/offer is registered", "/api/camera/offer" in routes, True)
    check("/api/camera/offer is a POST route",
          "POST" in getattr(routes.get("/api/camera/offer"), "methods", set()), True)
    check("/api/camera/extend is registered", "/api/camera/extend" in routes, True)
    check("/api/camera/extend is a POST route",
          "POST" in getattr(routes.get("/api/camera/extend"), "methods", set()), True)
    check("/api/camera/stop is registered", "/api/camera/stop" in routes, True)
    check("/api/camera/stop is a POST route",
          "POST" in getattr(routes.get("/api/camera/stop"), "methods", set()), True)

    # 6. Offer endpoint guard against spoofed Content-Type
    def make_scope(headers: list[tuple[bytes, bytes]]) -> dict:
        return {
            "type": "http",
            "method": "POST",
            "path": "/api/camera/offer",
            "headers": headers,
        }

    req_no_ct = Request(make_scope([(b"host", b"localhost")]))
    caught_415 = False
    try:
        await main.api_camera_offer(req_no_ct)
    except HTTPException as e:
        caught_415 = (e.status_code == 415)
    check("offer endpoint rejects non-JSON content-type with 415", caught_415, True)


async def main() -> None:
    await offline_safety_cases()
    await calculate_wiring_cases()
    await endpoint_cases()
    await reload_cases()
    await roll_fanout_cases()
    await roll_race_cases()
    await gap_arrival_cases()
    await arm_lock_cases()
    await retire_cases()
    await gws_bookkeeping_cases()
    await gws_agenda_frame_cases()
    await source_wiring_cases()
    await cached_wiring_cases()
    await plan_bookkeeping_cases()
    await near_cases()
    await place_unwrap_cases()
    await tool_query_normalization_cases()
    await config_cases()
    await voice_cases()
    await lang_cases()
    await forget_guard_cases()
    await gws_auth_cases()
    await gws_ephemeral_auth_cases()
    await wake_cases()
    await one_live_session_cases()
    await unknown_tool_cases()
    await card_fanout_cases()
    await browse_fanout_cases()
    await host_guard_cases()
    await origin_guard_cases()
    await revision_cases()
    await build_cases()
    await starter_suggestions_cases()
    scheduling_cases()
    screen_style_cases()
    maps_closed_client_cases()
    video_steering_cases()
    clarifying_instruction_cases()
    plan_steering_cases()
    browse_steering_cases()
    maps_route_steering_cases()
    maps_search_steering_cases()
    empty_search_instruction_cases()
    gws_payload_envelope_and_baseline_cases()
    gws_dynamic_fan_out_cases()
    verify_queue_drain_cases()
    await camera_wiring_cases()
    bind_cases()
    check("no un-stubbed genai_client call was made during the run",
          CALLED_GENAI, [],
          "#287: offline suite made no live model calls")
    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    asyncio.run(main())
