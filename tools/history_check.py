"""Check that a conversation survives a reload, and whose it is.

The browser used to mint a session id on every page load, so the terminal
forgot the conversation each time the kiosk reloaded — and remembering is most
of what a voice assistant standing in a kitchen is for. The id belongs to the
server now: one per person, kept in SQLite, rolled over once the room has been
quiet long enough that "it" means something new. See #19.

Seven things follow that no other script here would catch.

**Which conversation a hello belongs to.** `main._thread` reuses the last one
unless it is older than `IDLE_GAP_S`. That is one comparison, and it is quiet
whichever way it goes wrong: too eager and every hello replays a week of talk
into the Live setup, too shy and the terminal is amnesiac again. The handshake
state rides along on every connect, as a state-only event, because moving the
pin used to cost a new id — that was how the address reached the agent.

**And when one is over.** That comparison was the only thing that ever ended a
conversation, and half an hour of silence is rare in a kitchen: a terminal
spoken to on and off through a day never rolled, so every wake word replayed
everything said before it into a fresh Live connection and the arm cost
whatever the day had cost so far. The screensaver ends it now — the page sends
a frame when the face comes back up, and the next wake word starts something
new. The cases drive the real endpoint over a real store and read what reaches
the Live socket, because the only symptom of this going wrong is a terminal
that gets slower to answer as the day goes on. See #142.

**What survives the rollover.** `recap` reads the thread being left behind and
carries two or three sentences into the one replacing it. The reading is where
this can fail silently: a native-audio reply is stored as an
`output_transcription` and has no content parts at all, so a transcript built
the obvious way is one-sided — every question, no answers — and summarises to
a list of things the person asked with no record of what they were told. The
progress notes the server injects as user turns are the mirror image: text
that is there and should not be, in a conversation nobody had.

**What forgetting has to reach.** `main.api_forget` is behind a button on the
settings sheet, and it has two ways to leave the terminal still remembering.
Deleting only the thread on screen uncovers the one before it, because
`_thread` resolves to the newest session that is left. And deleting every
session still leaves the recap: `user:`-prefixed state is filed under the
person, so it survives the sessions it was written through and the next hello
opens with a summary of a conversation that no longer exists.

**And the one store it cannot.** The Pi's journal is persistent and no button
empties it, so what the server writes there outlives every promise the settings
sheet makes. `_log_tool_traffic` used to print each tool call's arguments in
full — the fact, the mail query, the address — two frames before `memory`
logged the topic and carefully not the fact. Sizes now, except for the few
argument names that are a closed vocabulary rather than somebody's words.

**What outlives the conversation anyway.** A recap is about a conversation and
is cleared when that one ends, which under #142 is every time the panel goes
back to being a clock. `memory` is the other half: standing facts filed per
person under a `user:` key nothing on the roll path touches. The two ways it
can fail are opposites — a fact that does not survive the roll, and a
"memory" that quietly restores the transcript with it, which is #142 coming
back through the side door. Both are checked, and so is the third case that
makes it usable: a correction has to replace the fact it corrects rather than
sit beside it.

**Two connections holding one id at once.** A reload opens the next connection
before the old one's `finally` has run. Teardown keyed on the id alone would
then unregister the screen and cancel the jobs of the connection that has just
arrived, and the symptom is a terminal that draws no cards until somebody
reloads it again. So each of the four registries hands out its own entry as a
token, and a connection tidies up only what is still its own — while always
cancelling the work it started itself, which belongs to a browser that has gone.

**Which of those two screens hears the wake word.** A card goes to the newest
page, because drawing it twice through a reload is the thing to avoid. A wake
frame is not drawn at all — it opens a microphone — so the same index sends the
word said in the kitchen to a tab on the Mac. `screen.wake_sink` answers the
kiosk where a page says it is one and the newest otherwise, one screen either
way, and `/api/wake` counts the screens it reached rather than the sessions it
walked past.

No model is called and no socket is opened. The sessions go into a temporary
SQLite file, never the household's own `sessions.db`.

    uv run python tools/history_check.py

Exits non-zero on the first case that does not hold.
"""

import ast
import asyncio
import contextlib
import inspect
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("ENABLE_MANAGEMENT_ROUTES", "1")

from google.adk.events import Event, EventActions  # noqa: E402
from google.adk.sessions.state import State  # noqa: E402
from google.genai import types  # noqa: E402

from ai_terminal.live import conn, memory, recap, screen  # noqa: E402
from ai_terminal.live.tools import _plan_run, browse, gws  # noqa: E402
from ai_terminal.live.tools import gws_cache  # noqa: E402
from ai_terminal.live.tools import plan as plan_mod  # noqa: E402

RAN = 0


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got!r}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


# ------------------------------------------ what survives being rolled away

def typed(text: str, author: str = "user") -> Event:
    """A turn somebody wrote into the text box, or the model wrote back."""
    return Event(author=author, content=types.Content(
        role="user" if author == "user" else "model",
        parts=[types.Part(text=text)]))


def heard(text: str, partial: bool = False) -> Event:
    """A turn somebody said out loud, as the Live API transcribes it."""
    return Event(author="user", partial=partial,
                 input_transcription=types.Transcription(
                     text=text, finished=not partial))


def replied(text: str, partial: bool = False) -> Event:
    """The agent answering in native audio, which stores no content at all."""
    return Event(author="ai_terminal", partial=partial,
                 output_transcription=types.Transcription(
                     text=text, finished=not partial))


def planned(task: str) -> Event:
    """The live agent handing a question to the planner.

    The one mark a thread leaves that says the pad on it is its own — see
    `_plan_run.ran`, which is what `main._carry_forward` asks before passing
    the findings to the thread replacing this one.
    """
    # The literal, not `_plan_run.TOOL`: moving the constant would move both
    # sides at once and the carry would go on passing whatever this writes.
    # `tools/plan_check.py` is where the constant is held to the tool's name.
    return Event(author="ai_terminal", content=types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(
            name="plan", args={"task": task}))]))


class Reply:
    """The one field :func:`recap._call` reads off a `generate_content`."""

    def __init__(self, text: str):
        self.text = text


class Models:
    """A stand-in for `genai_client().models`, holding what it was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.seen: dict = {}

    def generate_content(self, *, model, contents, config):
        self.seen = {"model": model, "contents": contents, "config": config}
        if isinstance(self.answer, BaseException):
            raise self.answer
        if callable(self.answer):
            return Reply(self.answer())
        return Reply(self.answer)


@contextlib.contextmanager
def model_says(answer):
    """Put `answer` where the summariser's one Gemini call would go.

    Patched at `genai_client` rather than at `_call`, so the prompt, the
    config and the whitespace squeeze on the way back are all still the real
    ones — those are the parts of a one-call module worth checking.
    """
    models = Models(answer)
    original = recap.grounding_client
    recap.grounding_client = lambda: type("C", (), {"models": models})()
    try:
        yield models
    finally:
        recap.grounding_client = original


def ctx(**state):
    """Something with a `.state`, which is all `recalled` asks for."""
    return type("Ctx", (), {"state": state})()


async def recap_cases() -> None:
    print("\n== reading back a conversation that is being left behind ==")

    talk = [
        typed("book the dentist for Tuesday"),
        replied("Booked. Tuesday the 14th at three."),
        heard("and tell Jordan"),
        replied("Sent."),
    ]
    text = recap.transcript(talk)
    check("what somebody typed is in the transcript",
          "Them: book the dentist for Tuesday" in text, True)
    check("and what they said out loud, which arrives transcribed",
          "Them: and tell Jordan" in text, True)
    # The one that decides whether any of this works. Native audio stores the
    # reply as a transcription and leaves `content` empty, so a transcript
    # built from content parts is every question and no answers — and a
    # summary of that says the dentist was never booked.
    check("and what the agent said back, which is in no content part",
          "You: Booked. Tuesday the 14th at three." in text, True)
    check("with who said it on every line",
          [line.split(":")[0] for line in text.splitlines()],
          ["Them", "You", "Them", "You"])

    check("the terminal's own progress notes are not somebody talking",
          recap.transcript([typed("[terminal] the gws call you just made")]),
          "", "nobody in the room said it")
    # And the same note written the other way round, which is the one that
    # actually reaches a recap. `_settle` appends its marker with the agent's
    # authorship, so it arrives on the model branch; filtered on the user
    # branch alone it read as `You: [terminal] The connection ended here…`,
    # and `transcript` keeps the newest line, so it was the one thing certain
    # to be carried forward. #172.
    check("nor when the terminal writes one as the agent",
          recap.transcript([Event(
              author="ai_terminal",
              content=types.Content(role="model", parts=[types.Part(
                  text="[terminal] The connection ended here before this "
                       "was answered.")]))]),
          "", "a note about a dropped socket is not something it said")
    check("nor is the state-only event the handshake writes",
          recap.transcript([Event(author="user", actions=EventActions(
              state_delta={"home_location": "Ebisu"}))]), "")
    check("nor the bookkeeping events with nothing in them at all",
          recap.transcript([Event(author="ai_terminal"),
                            Event(author="user")]), "")
    check("nor a partial, which the finished one repeats in full",
          recap.transcript([replied("Boo", partial=True),
                            replied("Booked.")]), "You: Booked.")
    check("a function call is the machinery, not the conversation",
          recap.transcript([Event(author="ai_terminal", content=types.Content(
              role="model", parts=[types.Part(function_call=types.FunctionCall(
                  name="weather", args={}))]))]), "")
    check("and an empty conversation reads as nothing", recap.transcript([]),
          "")

    long_talk = [typed(f"turn {i} " + "x" * 40) for i in range(20)]
    trimmed = recap.transcript(long_talk, limit=200)
    check("too much to send is trimmed from the front", len(trimmed) <= 200,
          True, f"{len(trimmed)} chars of {len(recap.transcript(long_talk))}")
    check("keeping the end, where what was decided is",
          trimmed.endswith("turn 19 " + "x" * 40), True)
    check("and only whole turns, never half a sentence",
          all(line.startswith(("Them: ", "You: "))
              for line in trimmed.splitlines()), True)
    check("one turn longer than the whole budget still says something",
          len(recap.transcript([typed("x" * 500)], limit=100)), 100)

    with model_says("Booked the dentist\n\nfor  Tuesday.") as models:
        summary = await recap.summarise(talk)
    check("the summary is what the model said", summary,
          "Booked the dentist for Tuesday.", "on one line, as prompt text")
    check("it was shown both sides of the conversation",
          [side in models.seen["contents"] for side in ("Them: ", "You: ")],
          [True, True])
    check("under the note-to-yourself prompt",
          models.seen["config"].system_instruction.startswith(
              "You are writing a note to yourself"), True)
    check("and at temperature 0, this being the recall half not the talking",
          models.seen["config"].temperature, 0)

    with model_says(RuntimeError("429")) as models:
        check("a model that will not answer starts the thread clean",
              await recap.summarise(talk), "")
    with model_says(RuntimeError("429")) as models:
        check("and one is not called at all for a conversation with nothing "
              "in it", await recap.summarise([]), "")
        check("so nothing was asked", models.seen, {})

    budget = recap.BUDGET_S
    try:
        recap.BUDGET_S = 0.05
        with model_says(lambda: (time.sleep(1), "too late")[1]):
            check("a model too slow to answer is given up on",
                  await recap.summarise(talk), "",
                  "the hello it holds up is somebody standing there")

        # Giving up is not the same as not paying. `asyncio.to_thread`
        # submits the work before the deadline is ever looked at, so a
        # budget at or below zero buys a `generate_content` on every
        # rollover and drops the answer on the floor. Green before the guard
        # as well as after: the budget is set here rather than read, so this
        # is the premise the refusal below rests on. #115.
        entered = threading.Event()

        def slow() -> str:
            entered.set()
            time.sleep(0.2)
            return "too late again"

        recap.BUDGET_S = 0.0
        with model_says(slow):
            gave = await recap.summarise(talk)
        check("a budget of zero still pays for the call it discards",
              (gave, entered.wait(5)), ("", True),
              "the thread is already submitted when the deadline passes")
    finally:
        recap.BUDGET_S = budget

    # And what the length must not be, measured the same way: `transcript`
    # trims from the front against the limit, so a limit of zero keeps the
    # tail of the first line it looks at, which is nothing at all. Green on
    # both sides for the same reason — the limit is an argument here.
    check("a length of zero reads none of the conversation, not all of it",
          [recap.transcript(talk, 0), recap.transcript(talk, -1)], ["", ""],
          "so `summarise` returns before it calls anything")

    # The two of them off the environment, which no case above can see
    # because both are read once, at import. A subprocess with `load_dotenv`
    # stubbed out: nothing on `recap`'s import path reads `.env` today, but
    # the one module in the tree that does — `main.py:26` — finds it from
    # `__file__` and loads it with `override=True`, so the directory this
    # runs from would not keep it out the day that path changes. The stub is
    # what does, and it costs a line whether or not it is needed yet.
    def imported(**names) -> subprocess.CompletedProcess:
        """`recap` imported with `names` in the environment."""
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as elsewhere:
            return subprocess.run(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, {str(root / 'src')!r})"
                 "\nimport dotenv; dotenv.load_dotenv = lambda *a, **k: False"
                 "\nfrom ai_terminal.live import recap\n"
                 "print(recap.BUDGET_S, recap.INPUT_CHARS)"],
                env={**os.environ, **names,
                     "GWS_CACHE_DIR": str(Path(elsewhere) / "cache")},
                cwd=elsewhere, capture_output=True, text=True)

    both = imported(RECAP_BUDGET_S="7", RECAP_INPUT_CHARS="99")
    check("both numbers come off the environment",
          both.stdout.strip().splitlines()[-1:], ["7.0 99"],
          f"{both.stdout.strip()!r}{both.stderr[-200:]}")

    # `nan` is the one worth the P1. It does not make a comparison misfire:
    # `asyncio.timeout` puts the deadline at `loop.time() + nan`, neither of
    # `_run_once`'s clamps is true for it, and the selector is handed `nan`
    # — kqueue raises `TypeError`, epoll reaches `math.ceil` and raises
    # `ValueError`. Raised by the loop, outside the task, so `summarise`'s
    # `except Exception` never sees it and the server goes down on the first
    # hello after a quiet half hour.
    for value, why in (
            ("0", "it would pay for a recap on every rollover and bin it"),
            ("-1", "the same, and 0 is what somebody types for 'no wait'"),
            ("nan", "it would take the event loop down, not just the recap"),
            ("inf", "there would be no budget: the hello waits as long as "
                    "the model does")):
        out = imported(RECAP_BUDGET_S=value)
        check(f"a recap budget of {value} is refused at import",
              (out.returncode != 0, "RECAP_BUDGET_S" in out.stderr),
              (True, True), why)

    for value in ("0", "-1"):
        out = imported(RECAP_INPUT_CHARS=value)
        check(f"and a recap length of {value}",
              (out.returncode != 0, "RECAP_INPUT_CHARS" in out.stderr),
              (True, True), "no thread would ever carry a recap forward")

    # The refusal is worth nothing if the reason for it stops being true, so
    # this asks asyncio rather than trusting the comment above. In a
    # subprocess because there is no catching it in this one: it comes out
    # of the loop this case is running on.
    dies = subprocess.run(
        [sys.executable, "-c",
         "import asyncio\n"
         "async def main():\n"
         "    async with asyncio.timeout(float('nan')):\n"
         "        await asyncio.sleep(0.05)\n"
         "asyncio.run(main())"],
        capture_output=True, text=True)
    check("and a nan deadline still dies in the loop, not in the task",
          (dies.returncode != 0, "_run_once" in dies.stderr), (True, True),
          dies.stderr.strip().splitlines()[-1:])

    check("the instruction reads it out of session state",
          recap.recalled(ctx(**{recap.RECAP_KEY: "they booked a dentist"})),
          "they booked a dentist")
    check("and nothing there is nothing to say", recap.recalled(ctx()), "")
    check("as is a context with no state at all", recap.recalled(object()), "")

    # And a thread `rethread` rolled bare, which is a different thing from
    # there being no summary. The summary is the person's — one slot, shared by
    # every thread of theirs at once — so a wake word that rolls one of them
    # cannot say "not this one" by emptying it without saying it about all of
    # them. It says it here instead, on the row of the thread it opened.
    check("a thread rolled bare says nothing, whatever is filed for the person",
          recap.recalled(ctx(**{recap.RECAP_KEY: "they booked a dentist",
                                recap.BARE_KEY: True})), "",
          "the wake word rolls with no recap, and only for its own thread")
    check("and the mark is a session key, not a user one",
          recap.BARE_KEY.startswith("user:"), False,
          f"{recap.BARE_KEY!r} rides the session row, like the plan pad")


# ------------------------------------------- which conversation this hello is

class Locked:
    """The session store with the one call the retention sweep makes wedged.

    A wrapper and not a patched attribute, because the service is handed
    around by reference and this has to be put back afterwards intact.
    `database is locked` is what SQLite raises when another connection is
    mid-write, which on this machine is the other half of a reload.
    """

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def delete_session(self, **kwargs):
        raise sqlite3.OperationalError("database is locked")


async def thread_cases() -> None:
    print("== which conversation a hello belongs to ==")

    # Imported here rather than at the top: importing `main` builds the live
    # agent and reads `.env`, and `main.basicConfig(INFO)` turns this file into
    # a page of connection logs. Every other script here installs no handler and
    # prints from WARNING up, so this puts the suite back in step.
    from ai_terminal.live import agent as agent_mod, main
    logging.getLogger().setLevel(logging.WARNING)

    service, gap = main.session_service, main.IDLE_GAP_S
    retain = main.RETAIN_S
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "sessions.db"
        main.session_service = type(service)(db_path=str(store))

        def turns(session_id: str) -> int:
            """Rows the events table still holds for one thread.

            Read out of SQLite rather than through `get_session`, because a
            deleted session answers `None` there and the question is whether
            what it held went with it. The bound is about rows: nothing else
            in the server ever takes one out.
            """
            with sqlite3.connect(store) as db:
                return db.execute(
                    "SELECT count(*) FROM events WHERE session_id=?",
                    (session_id,)).fetchone()[0]

        try:
            main.IDLE_GAP_S = 3600.0
            first = await main._thread("alex", {"home_location": "Ebisu"})
            again = await main._thread("alex", {})
            other = await main._thread("jordan", {})

            check("a first hello starts a conversation", bool(first), True,
                  detail=first)
            check("and speaking again a moment later stays inside it",
                  again, first, "the reload the terminal used to forget")
            check("while the next person gets one of their own",
                  other != first, True, detail=other)

            stored = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=first)
            check("the address off the handshake is there to be read",
                  stored.state.get("home_location"), "Ebisu")

            await main._thread("alex", {"home_location": "Kanazawa"})
            moved = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=first)
            check("and moving the pin reaches the conversation already running",
                  moved.state.get("home_location"), "Kanazawa",
                  "which used to cost a new id, and the talk with it")
            # A state-only event: no content, so `contents.py` leaves it out of
            # what gets replayed. Written as an ordinary turn instead, the model
            # would open the next connection having just heard the person recite
            # their own coordinates.
            check("without putting anything into what was said",
                  [event.content for event in moved.events], [None])

            # ------------------------- and a reconnect that says nothing new
            #
            # The same write, on the far commoner path: the page comes back
            # after a Wi-Fi blip with the pin it already had.
            # `last_update_time` is stamped from every appended event, and it
            # is the one column `IDLE_GAP_S` is measured against — so a
            # handshake written whether or not it changes anything makes the
            # socket coming up look like somebody speaking. The rule then
            # reads "half an hour since the last of speech or a reconnect",
            # and on a kiosk that reconnects oftener than the gap it never
            # fires at all. #114.
            pinned = {"home_location": "Ebisu",
                      "home_coords": {"lat": 35.6, "lng": 139.7}}
            began = await main._thread("hana", pinned)
            hers = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="hana", session_id=began)
            await main.session_service.append_event(
                hers, typed("what is the weather"))
            rows = turns(began)

            # Rolled forward rather than backdated, the same wall the rollover
            # below runs into. Two waits either side of the reconnect, because
            # what is being measured is whether the middle one restarts the
            # clock: each is under the gap and the pair is over it.
            main.IDLE_GAP_S = 1.0
            await asyncio.sleep(0.6)
            check("a reconnect inside the gap is still the same conversation",
                  await main._thread("hana", pinned), began)
            check("and one that changes nothing writes nothing",
                  turns(began), rows,
                  "an event is a timestamp, and this one is not a person")
            await asyncio.sleep(0.6)
            with model_says("they asked about the weather"):
                rolled_out = await main._thread("hana", pinned)
            check("so the silence either side of it still adds up to a gap",
                  rolled_out != began, True,
                  "or the socket coming up is what IDLE_GAP_S measures")
            main.IDLE_GAP_S = 3600.0

            # Something for the rollover to find worth carrying. Appended as
            # ordinary turns because that is what the runner writes; `_thread`
            # never sees them any other way.
            said = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=first)
            for event in (typed("book the dentist for Tuesday"),
                          planned("a dentist appointment on Tuesday"),
                          replied("Booked, Tuesday at three.")):
                await main.session_service.append_event(said, event)
            # And what the plan in it wrote down. On the session row, not the
            # user row — #64 — so this is the one thing here that nothing but
            # `_carry_forward` can move across the gap.
            #
            # The second finding is over `PAD_LINE`, which is what makes the
            # carry read the row through `held` rather than lift it: the pad
            # coming off a session row was written by whichever release was
            # running that day, and this release's caps are the ones the next
            # conversation is sized for.
            long = "and " + "so on, " * 60
            await main.session_service.append_event(said, Event(
                author="user", actions=EventActions(state_delta={
                    _plan_run.PAD_KEY: ["the surgery shuts at six", long]})))

            # Rolled forward rather than backdated: `last_update_time` is a
            # column in the sessions table and there is no supported way to
            # write it. So the gap is made small and genuinely waited out.
            main.IDLE_GAP_S = 0.05
            await asyncio.sleep(0.15)
            with model_says("they booked a dentist for Tuesday at three"):
                fresh = await main._thread("alex", {})
            check("but a long enough silence makes the next hello a new one",
                  fresh != first, True, detail=fresh)

            carried = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=fresh)
            check("with what the last conversation was about carried into it",
                  carried.state.get(recap.RECAP_KEY),
                  "they booked a dentist for Tuesday at three",
                  "or rolling over is just forgetting on a timer")
            # `held` cuts on the way in and normalises again on the way out,
            # so the row keeps the cut that ends mid-space and a reader gets
            # it back one character shorter. Both spellings are written out
            # here rather than one being reused, because a check that reads
            # the row through the same function that wrote it cannot tell the
            # two apart, and telling them apart is the whole of the case below.
            cut = " ".join(long.split())[:_plan_run.PAD_LINE]
            check("and what a plan in it worked out carried too",
                  _plan_run.held(carried.state),
                  ["the surgery shuts at six", " ".join(cut.split())],
                  "the expensive half of the last conversation, and the pad "
                  "is a session row, so the gap is where it used to die")
            # Read raw, which is the only way to see where the cutting
            # happened. Lifted straight across, the over-long finding would
            # sit on the new row at its full length and every reader after
            # this one would pay to cut it down again.
            check("cut to this release's caps on the way, not on the way out",
                  carried.state.get(_plan_run.PAD_KEY),
                  ["the surgery shuts at six", cut],
                  f"{len(long)} chars written, {_plan_run.PAD_LINE} carried")
            # Same reasoning as the handshake state above, and the same risk if
            # it were written as a turn: the agent would open the conversation
            # having apparently just heard somebody read out a summary of it.
            check("without putting a word of it into what was said",
                  [event.content for event in carried.events], [None])
            check("and the agent is told, in the instruction it takes once",
                  "dentist" in agent_mod._instruction(ctx(**carried.state)),
                  True, "the Live API asks for that once and never again")
            agent_src = (ROOT / "src" / "ai_terminal" / "live" / "agent.py").read_text(encoding="utf-8")
            check("agent prompt guides sequential entity resolution for follow-ups",
                  "Sequential entity resolution" in agent_src and "that place" in agent_src,
                  True)

            main.IDLE_GAP_S = 3600.0
            check("and it is that one, the most recent, the hello after "
                  "continues", await main._thread("alex", {}), fresh)

            # Nothing said in the new thread, so the next rollover has nothing
            # to carry. The point is that it says so rather than leaving the
            # dentist standing: two rollovers on, that note describes a
            # conversation from days ago and reads as if it were this morning.
            main.IDLE_GAP_S = 0.05
            await asyncio.sleep(0.15)
            with model_says(RuntimeError("429")):
                empty = await main._thread("alex", {})
            cleared = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=empty)
            check("and a rollover with nothing to carry clears the last note",
                  cleared.state.get(recap.RECAP_KEY), "",
                  "a stale memory is worse to talk to than none")
            # The same rule for the pad, which needs one of its own: the recap
            # is rewritten from the events every time and so expires by
            # itself, where a pad handed on unconditionally would be handed on
            # every gap forever. That thread ran no plan, so it passes none.
            check("and one whose thread ran no plan passes no findings on",
                  _plan_run.held(cleared.state), [],
                  "or the surgery still shuts at six a fortnight later")

            # ------------------ and how far a write to either key reaches
            #
            # The two keys the carry writes are scoped differently on purpose,
            # and the difference is invisible until one person has two threads
            # open at once — which the kiosk and a tab on the Mac routinely
            # are, on two ids, because an accept that crosses the gap rolls
            # without telling the sockets already up. Asked of the store
            # rather than of `State.USER_PREFIX`, because what a reader gets
            # is what the backend merges back on load.
            one = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="noe", state={})
            two = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="noe", state={})

            async def noes(session_id: str):
                return await main.session_service.get_session(
                    app_name=main.APP_NAME, user_id="noe",
                    session_id=session_id)

            await main.session_service.append_event(one, Event(
                author="user", actions=EventActions(state_delta={
                    recap.RECAP_KEY: "they asked you to move the lesson"})))
            check("a summary written through one thread is the whole person's",
                  recap.recalled(await noes(two.id)),
                  "they asked you to move the lesson",
                  "one slot, and the `user:` prefix is what asks for that")

            # What `rethread` writes when it rolls one of them bare. It has to
            # be true of the thread it opened and of nothing else: emptying
            # the shared slot instead takes the summary off the other page,
            # whose own accept paid a `generate_content` for it a moment ago.
            await main.session_service.append_event(two, Event(
                author="user", actions=EventActions(
                    state_delta={recap.BARE_KEY: True})))
            check("but rolling one of them bare is true of that one only",
                  [recap.recalled(await noes(two.id)),
                   recap.recalled(await noes(one.id))],
                  ["", "they asked you to move the lesson"],
                  "a wake word at the kiosk must not blank the tab")
            check("and it leaves the summary itself where it was",
                  (await noes(one.id)).state.get(recap.RECAP_KEY),
                  "they asked you to move the lesson",
                  "skipped on one row, not deleted off the person")

            # ------------------------------------ what a hello throws away
            #
            # Nothing takes a row out of `sessions.db`, so it grows by every
            # reconnect for as long as the terminal is plugged in, and both
            # queries above are on the path of every page load. The bound is
            # here rather than at startup because this is where the list it
            # needs is already in hand — and because a reboot is as likely to
            # be a power cut as it is to be somebody using the thing.

            # Rolled forward rather than backdated, the same wall the rollover
            # above runs into: `update_time` is a column and the service has
            # no setter for it.
            main.IDLE_GAP_S = 3600.0
            main.RETAIN_S = 3600.0
            await main._thread("alex", {})
            check("a thread inside the window is not touched by a hello",
                  bool(await main.session_service.get_session(
                      app_name=main.APP_NAME, user_id="alex",
                      session_id=first)), True,
                  "the bound is a floor on what is kept, not a broom")

            held = turns(first)
            main.RETAIN_S = 0.1
            await asyncio.sleep(0.15)
            carries_on = await main._thread("alex", {})

            check("one nobody has come back to goes on the next hello",
                  await main.session_service.get_session(
                      app_name=main.APP_NAME, user_id="alex",
                      session_id=first), None,
                  f"{first} was {held} turns")
            check("and the turns in it go with it, which is the growth",
                  turns(first), 0, f"{held} rows before, none after")
            check("while the thread they are in is kept, however old it is",
                  bool(await main.session_service.get_session(
                      app_name=main.APP_NAME, user_id="alex",
                      session_id=carries_on)), True,
                  "or the connect that was about to resume it deletes it")
            check("and it is still the one the next hello continues",
                  await main._thread("alex", {}), carries_on)
            check("nobody else's thread goes on somebody else's hello",
                  bool(await main.session_service.get_session(
                      app_name=main.APP_NAME, user_id="jordan",
                      session_id=other)), True,
                  "`list_sessions` here is per person and the sweep is too")

            # And a store that will not let the sweep delete anything. This
            # is the one failure on the path that costs more than it is
            # about: `_thread` runs after `websocket.accept()` with no `try`
            # around it, so a row that cannot be dropped this minute would
            # close the socket on somebody standing at the terminal, and do
            # it again on every reconnect, because the same rows are still
            # there. Caught here rather than left to propagate so a
            # regression reads as a failed case and not as a traceback.
            older = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="toshi", state={})
            await asyncio.sleep(0.05)
            current = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="toshi", state={})
            main.RETAIN_S = 0.1
            await asyncio.sleep(0.15)
            store = main.session_service
            main.session_service = Locked(store)
            try:
                opened = await main._thread("toshi", {})
            except Exception as exc:
                opened = f"{type(exc).__name__}: {exc}"
            finally:
                main.session_service = store
            check("a store that refuses the delete still opens the "
                  "conversation", opened, current.id,
                  "the sweep is not what the hello is for")
            check("and the row it could not take is left where it was",
                  bool(await main.session_service.get_session(
                      app_name=main.APP_NAME, user_id="toshi",
                      session_id=older.id)), True, "skipped, not lost")
            resumed = await main._thread("toshi", {})
            check("which the next hello, on a store that answers, takes out",
                  (resumed, await main.session_service.get_session(
                      app_name=main.APP_NAME, user_id="toshi",
                      session_id=older.id)), (current.id, None),
                  "the skip is for this connect and not for good")

            # --------------------------- GC orphaned profiles on PROFILES rename
            #
            # When someone is removed from PROFILES or renamed, connections
            # under their old name stop arriving, so per-person hello retention
            # never sweeps them. _gc_profiles purges orphaned sessions, events,
            # and user_states from SQLite.
            def profile_counts(user_id: str) -> tuple[int, int, int]:
                with sqlite3.connect(Path(tmp) / "sessions.db") as db:
                    s_count = db.execute(
                        "SELECT count(*) FROM sessions WHERE app_name=? AND user_id=?",
                        (main.APP_NAME, user_id)).fetchone()[0]
                    e_count = db.execute(
                        "SELECT count(*) FROM events WHERE app_name=? AND user_id=?",
                        (main.APP_NAME, user_id)).fetchone()[0]
                    u_count = db.execute(
                        "SELECT count(*) FROM user_states WHERE app_name=? AND user_id=?",
                        (main.APP_NAME, user_id)).fetchone()[0]
                    return s_count, e_count, u_count

            alice_sess = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="alice",
                state={State.USER_PREFIX + "fact": "alice fact"})
            await main.session_service.append_event(
                alice_sess, typed("hello from alice"))

            term_sess = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="terminal",
                state={State.USER_PREFIX + "fact": "terminal fact"})
            await main.session_service.append_event(
                term_sess, typed("hello from terminal"))

            alex_sess = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=carries_on)
            await main.session_service.append_event(
                alex_sess, Event(author="user", actions=EventActions(
                    state_delta={State.USER_PREFIX + "fact": "alex fact"})))

            saved_profiles = os.environ.pop("PROFILES", None)
            try:
                main._gc_profiles()
                check("unconfigured PROFILES leaves existing profiles intact",
                      all(c > 0 for c in profile_counts("alice")), True,
                      "unconfigured terminal skips GC to avoid data loss")

                os.environ["PROFILES"] = "alex,jordan"
                main._gc_profiles()
                check("setting PROFILES purges orphaned profile sessions, events, and user_states",
                      profile_counts("alice"), (0, 0, 0),
                      "orphaned profile alice purged from SQLite")
                check("while active profiles are preserved",
                      all(c > 0 for c in profile_counts("alex")), True,
                      "alex kept")
                check("and default terminal profile is preserved",
                      all(c > 0 for c in profile_counts("terminal")), True,
                      "terminal kept as DEFAULT_PROFILE")
            finally:
                if saved_profiles is not None:
                    os.environ["PROFILES"] = saved_profiles
                else:
                    os.environ.pop("PROFILES", None)

            # The number the comment above `RETAIN_S` argues for. It is a
            # judgement and worth arguing with; what it must not do is drift
            # from the sentence that explains it.
            check("and the window a household gets is thirty days",
                  retain, 30 * 86400.0)
            def imported(value: str, key: str = "SESSION_RETAIN_S",
                         attr: str = "RETAIN_S"
                         ) -> subprocess.CompletedProcess:
                """`main` imported with `key` set to `value`.

                A subprocess because the number is read once, at import, so
                assigning the module attribute — which every case above does
                — says nothing about what the environment does. With
                `load_dotenv` stubbed out, because `main.py:26` loads the
                real `.env` with `override=True` and it would win over what
                is set here. Running from an empty directory does not stop
                that — `main` finds `.env` from `__file__`, not from the
                working directory — and the stub is what does.

                Two settings go through here now, so the name of the one
                being asked about is a parameter rather than a literal: they
                are read six lines apart and refused by the same shape, and a
                second copy of this helper would be the thing that drifts.
                """
                root = Path(__file__).resolve().parent.parent
                with tempfile.TemporaryDirectory() as elsewhere:
                    return subprocess.run(
                        [sys.executable, "-c",
                         f"import sys; sys.path.insert(0, {str(root/'src')!r})"
                         "\nimport dotenv; dotenv.load_dotenv = lambda *a, "
                         "**k: False"
                         "\nfrom ai_terminal.live import main\n"
                         f"print(main.{attr})"],
                        env={**os.environ, key: value,
                             "SESSIONS_DB": str(Path(elsewhere) / "db"),
                             "GWS_CACHE_DIR": str(Path(elsewhere) / "cache")},
                        cwd=elsewhere, capture_output=True, text=True)

            # Ours, and the same question `cache_check` asks of the two cache
            # caps: thirty days is a judgement, so the household has to be able
            # to be wrong about it cheaply. The case above holds just as well
            # when the number is typed into the source where nobody can reach
            # it.
            asked = imported("4242")
            check("and it comes off the environment, not off the source",
                  asked.stdout.strip().splitlines()[-1:], ["4242.0"],
                  f"{asked.stdout.strip()!r}{asked.stderr[-200:]}")

            # Zero is the value somebody reaches for to turn a retention
            # setting off, and here it is the most destructive one there is:
            # the cutoff lands on `time.time()` and every thread but the one
            # being resumed is behind it. Nothing else takes a row out of
            # `sessions.db`, so there is no off to offer instead — which
            # leaves refusing it, at import, before the terminal has answered
            # anybody. #99.
            off = imported("0")
            check("but zero is not an off switch, and is refused at import",
                  (off.returncode != 0, "SESSION_RETAIN_S" in off.stderr),
                  (True, True),
                  "it would put the cutoff at now and take everything else")
            back = imported("-1")
            check("and so is a negative one, which does the same thing",
                  (back.returncode != 0, "SESSION_RETAIN_S" in back.stderr),
                  (True, True))

            # The two values `float()` takes that a `<= 0` test lets past, and
            # they are not a pedantic pair: `nan` fails every comparison, so
            # `last_update_time >= cutoff` is False for every row and the
            # sweep takes all of them — the same deletion zero causes, through
            # the guard rather than around it. `inf` is the other end, and it
            # is the off switch the refusal says there is none of.
            wrong = imported("nan")
            check("a nan is refused too, and would have swept every row",
                  (wrong.returncode != 0, "SESSION_RETAIN_S" in wrong.stderr),
                  (True, True))
            forever = imported("inf")
            check("and an infinity, which would be the off switch",
                  (forever.returncode != 0,
                   "SESSION_RETAIN_S" in forever.stderr), (True, True))

            # The two things the template has to say, because it is the only
            # place most people meet this setting: what a hello sweeps is one
            # person's threads, and no number here disables it. Neither is
            # inferable from the sentence that was there.
            says = (Path(__file__).resolve().parent.parent
                    / ".env.example").read_text(encoding="utf-8")
            block = says.split("# SESSION_RETAIN_S=")[0].split("\n\n")[-1]
            check("the template says the window is one person's",
                  "per person" in block, True,
                  "a profile nobody connects as any more keeps its threads")
            check("and that no value here turns the sweep off",
                  "refuses to start" in block, True)

            # And the gap, which is read six lines above `RETAIN_S` and was
            # left bare when that one was guarded. It is the more interesting
            # of the two, because two places read it and they disagree about
            # the bad values: `_thread` rolls when the quiet is
            # `> IDLE_GAP_S`, `rethread` returns early when it is `<=`. #114.
            gapped = imported("4242", "SESSION_IDLE_GAP_S", "IDLE_GAP_S")
            check("the gap comes off the environment too",
                  gapped.stdout.strip().splitlines()[-1:], ["4242.0"],
                  f"{gapped.stdout.strip()!r}{gapped.stderr[-200:]}")

            # Zero here is the same reach as zero there — "never split a
            # conversation" — and does the opposite: both readers roll, so
            # every hello mints a thread and buys a `recap.summarise` model
            # call for it, and every wake word after that rolls again with no
            # recap at all. A terminal that works, forgets everything, and
            # spends a Gemini call per hello saying so to nobody.
            for value, why in (("0", "every hello would roll and pay a recap"),
                               ("-1", "the same, one second further past it")):
                out = imported(value, "SESSION_IDLE_GAP_S", "IDLE_GAP_S")
                check(f"and a gap of {value} is refused at import",
                      (out.returncode != 0,
                       "SESSION_IDLE_GAP_S" in out.stderr), (True, True), why)

            # `nan` is the one that splits the two readers: `>` is False so
            # the hello never rolls and the thread on disk grows without
            # bound, `<=` is False so every arm rolls the page onto a fresh
            # one. Growing storage and a terminal with no memory, at once.
            # `inf` is the other end — nothing ever rolls, and ADK replays the
            # whole thread into the Live connection at every setup.
            split = imported("nan", "SESSION_IDLE_GAP_S", "IDLE_GAP_S")
            check("a nan gap is refused, which the two readers would split on",
                  (split.returncode != 0,
                   "SESSION_IDLE_GAP_S" in split.stderr), (True, True),
                  "`>` never rolls the hello, `<=` rolls every arm")
            never = imported("inf", "SESSION_IDLE_GAP_S", "IDLE_GAP_S")
            check("and an infinity, which would never roll a thread again",
                  (never.returncode != 0,
                   "SESSION_IDLE_GAP_S" in never.stderr), (True, True))

            gap_block = (says.split("# SESSION_IDLE_GAP_S=")[0]
                         .split("\n\n")[-1])
            check("and the template says this one is not a switch either",
                  "finite number above zero" in gap_block, True)

            # And that it no longer claims to be the only thing that ends a
            # conversation, which is what it read as before #142. Somebody
            # tuning this number to make the wake word quicker will read this
            # block first, and the answer they want is the screensaver.
            gap_all = says.split("# SESSION_IDLE_GAP_S=")[0].split(
                "# How long the room can be quiet")[-1]
            check("and that the screensaver is the other thing that ends one",
                  ["screensaver" in gap_all, "never slept" in gap_all],
                  [True, True],
                  "this number is what is left for a page that did not")
        finally:
            main.session_service, main.IDLE_GAP_S = service, gap
            main.RETAIN_S = retain


# --------------------------------------------- and asking it to forget it all

def from_our_page() -> "Request":
    """The request the panel makes, which is the only kind the route takes.

    `api_forget` refuses anything a browser would send across origins without
    asking first (#83), so calling it directly means supplying the header the
    page sends. Which content types get through is `wiring_check`'s subject;
    here the guard is only in the way of the question, which is what
    forgetting deletes.
    """
    from starlette.requests import Request
    return Request({"type": "http", "http_version": "1.1", "method": "POST",
                    "scheme": "http", "path": "/api/forget/alex",
                    "raw_path": b"/api/forget/alex", "root_path": "",
                    "query_string": b"", "server": ("terminal", 8000),
                    "client": ("127.0.0.1", 4242),
                    "headers": [(b"content-type", b"application/json")]})


async def forget_cases() -> None:
    print("\n== forgetting one person's history, and nobody else's ==")

    from ai_terminal.live import agent as agent_mod, main
    logging.getLogger().setLevel(logging.WARNING)

    service, gap = main.session_service, main.IDLE_GAP_S
    # `api_forget` clears the Workspace cache too now (#233), and `cache_dir()`
    # resolves against the working directory — the developer's own `gws_cache/`
    # for a sweep run from the repo root. These cases call the endpoint for
    # real, so the directory has to be somewhere disposable before the first
    # one does.
    was_cache_dir = gws_cache.cache_dir
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "sessions.db"
        main.session_service = type(service)(db_path=str(store))

        def people() -> list[str]:
            """Who `user_states` holds a row for.

            Read out of SQLite for the same reason `turns` above is: the row
            outlives every session it was written through, and nothing the
            service answers tells "cleared" apart from "never written". The
            bound #72 asks for is about rows, so the case has to count rows.
            """
            with sqlite3.connect(store) as db:
                return sorted(row[0] for row in db.execute(
                    "SELECT user_id FROM user_states"))

        cached = Path(tmp) / "cache"
        cached.mkdir()
        gws_cache.cache_dir = lambda: cached
        try:
            # Two threads for one person, the older of them summarised into the
            # newer — which is the state the button has to cope with, and the
            # one where deleting a session is not enough.
            main.IDLE_GAP_S = 0.05
            first = await main._thread("alex", {})
            await main.session_service.append_event(
                await main.session_service.get_session(
                    app_name=main.APP_NAME, user_id="alex", session_id=first),
                typed("book the dentist for Tuesday"))
            await asyncio.sleep(0.15)
            with model_says("they booked a dentist for Tuesday at three"):
                second = await main._thread("alex", {})
            jordan = await main._thread("jordan", {})
            await main.session_service.append_event(
                await main.session_service.get_session(
                    app_name=main.APP_NAME, user_id="jordan", session_id=jordan),
                Event(author="user", actions=EventActions(
                    state_delta={recap.RECAP_KEY: "the car is in the shop"})))
            main.IDLE_GAP_S = 3600.0

            recalled = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=second)
            check("before the button: the older thread is summarised into the "
                  "newer",
                  "dentist" in recalled.state.get(recap.RECAP_KEY, ""), True)

            check("forgetting says how many conversations went",
                  await main.api_forget("alex", from_our_page()),
                  {"forgotten": 2},
                  "both of them: the one on screen is not the only one there")

            left = await main.session_service.list_sessions(
                app_name=main.APP_NAME, user_id="alex")
            check("and leaves the person none, not even the scratch one it "
                  "wrote through", [s.id for s in left.sessions], [])

            after = await main._thread("alex", {})
            check("so the next hello is a conversation nobody has had",
                  after not in (first, second), True, detail=after)
            blank = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=after)
            check("with nothing said in it", len(blank.events), 0)
            # The one a `delete_session` loop on its own does not get. `user:`
            # state is filed under the person (`sessions/state.py`), so it
            # outlives every session it was written through: measured on ADK
            # 2.8.0, a session created after deleting all of them still comes
            # back carrying the old summary.
            check("and no memory of what those conversations were about",
                  blank.state.get(recap.RECAP_KEY, ""), "",
                  "the summary outlives the sessions unless it is cleared")
            check("so the agent stops reciting it back at the next hello",
                  "dentist" in agent_mod._instruction(ctx(**blank.state)),
                  False, "the instruction is taken once, at setup")

            theirs = await main.session_service.list_sessions(
                app_name=main.APP_NAME, user_id="jordan")
            check("while the person standing next to them keeps their thread",
                  [s.id for s in theirs.sessions], [jordan])
            kept = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="jordan", session_id=jordan)
            check("and their summary, which is stored under the same key",
                  kept.state.get(recap.RECAP_KEY), "the car is in the shop",
                  "`user:` is per person, and the clear must be too")

            check("and forgetting somebody who has said nothing is not an "
                  "error", await main.api_forget(
                      "nobody", from_our_page()), {"forgotten": 0})

            # ---- and is not a row either. #176.
            #
            # The id is a path parameter on a route that authenticates nobody,
            # so this is what anything reaching the host can post ten thousand
            # of. The answer was already honest; the row it wrote on the way to
            # saying it was the part nothing reported and nothing ever removed.
            check("nor a row filed under a name nobody has ever used",
                  people(), ["alex", "jordan"],
                  "`delete_session` takes sessions only, so a row written "
                  "here is written for good")
            for ghost in range(5):
                await main.api_forget(f"ghost{ghost}", from_our_page())
            check("so posting the route made-up ids cannot grow the store",
                  people(), ["alex", "jordan"], "five of them, no rows")

            # The other half of the guard, and the way a cheap version of it
            # gets this wrong: "no sessions" is not the same question as
            # "nothing to hold on to". The `user:` row is the one thing built
            # to outlive the sessions, so a forget that skips on the session
            # count alone no-ops on precisely the state it exists for. Not
            # reachable through the server today — the sweep in `_thread`
            # never takes the newest thread — which is why it is asserted
            # here rather than left to be found.
            await main.session_service.delete_session(
                app_name=main.APP_NAME, user_id="jordan", session_id=jordan)
            check("a person whose threads have gone but whose summary has not "
                  "is still forgotten",
                  await main.api_forget("jordan", from_our_page()),
                  {"forgotten": 0}, "no sessions left to count, one thing "
                                    "left to clear")
            held = await main.session_service.get_user_state(
                app_name=main.APP_NAME, user_id="jordan")
            check("which is what skipping the write must not skip",
                  held.get(recap.RECAP_KEY.removeprefix(State.USER_PREFIX)),
                  "", "`get_user_state` answers without the `user:` prefix")

            # #233. Copies of the household's Workspace documents are the other
            # store the button promises to reach and did not: nothing else in
            # the product deletes one, because `_evict` waits for 200 newer
            # documents rather than for a clock. The index goes back to saying
            # so, or the next listing names files that are gone.
            (cached / "letter.md").write_text(
                '---\nsource: "gws drive files export"\n'
                'fetched: "2026-09-11T00:00:00+09:00"\n---\n\nbody\n',
                encoding="utf-8")
            (cached / "scan.pdf").write_bytes(b"%PDF-1.4 not really")
            await main.api_forget("alex", from_our_page())
            check("and the cached Workspace documents go with them",
                  sorted(p.name for p in cached.iterdir()
                         if not p.name.startswith(".")), ["index.md"],
                  "the button says forgotten; a copy on the card is not")
            check("with the index rewritten rather than left naming them",
                  "letter.md" not in
                  (cached / "index.md").read_text(encoding="utf-8"), True)
        finally:
            main.session_service, main.IDLE_GAP_S = service, gap
            gws_cache.cache_dir = was_cache_dir


# ----------------------------------- and the one store the button cannot reach

class Journaled(logging.Handler):
    """What the server logged while this was installed, formatted.

    Formatted, not kept as records: the arguments reach the journal through
    `%s`, so a record still holds the live dict and a case reading
    `record.args` would pass on a line that goes on to print the fact.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        # Which logger each line came off, so a case can ask whether the
        # capture is reaching past the module it is nominally about. #235.
        self.names: list[str] = []

    def emit(self, record) -> None:
        self.lines.append(record.getMessage())
        self.names.append(record.name)


def calling(name: str, args) -> Event:
    """The model reaching for a tool, with the arguments it chose.

    `called` further down does the same with none, which is what the replay
    cases want. Here the arguments are the whole subject.
    """
    return Event(author="ai_terminal", content=types.Content(
        role="model", parts=[types.Part(
            function_call=types.FunctionCall(name=name, args=args))]))


async def journal_cases() -> None:
    print("\n== and the log the button cannot reach ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    def logged(event) -> str:
        """One event through `_log_tool_traffic`, as the journal would hold it.

        The level is raised for the duration because the rest of this script
        runs at WARNING to keep the suite readable, and a case that measured
        nothing because nothing was logged would pass on any code at all.
        """
        held = Journaled()
        source = logging.getLogger("ai_terminal.live.main")
        was, quiet = source.level, source.propagate
        source.setLevel(logging.INFO)
        source.propagate = False
        source.addHandler(held)
        try:
            main._log_tool_traffic(event)
        finally:
            source.removeHandler(held)
            source.setLevel(was)
            source.propagate = quiet
        assert held.lines, "nothing was logged, so nothing was measured"
        return "\n".join(held.lines)

    # The fact `remember` exists to keep past the conversation, and so the one
    # thing *Forget* most has to be able to erase. `memory.py:406` never logged
    # it; `_log_tool_traffic` did, two frames earlier, into a journal no button
    # reaches. See #206.
    fact = "alex does not eat meat"
    filed = logged(calling("remember", {"topic": "alex's diet", "fact": fact}))
    check("the fact somebody asked to have remembered does not reach the log",
          fact in filed, False, filed)
    check("only how long it was", "<21 chars>" in filed, True, filed)
    # Green before this and after it, and here because a line that logged
    # nothing at all would pass the case above. The topic is two or three words
    # the model picked as a handle, it is what `memory.py:406` already prints,
    # and without it the log cannot say which fact was written.
    check("while the topic it was filed under still does",
          "alex's diet" in filed, True, filed)

    # A tool the rule was not written for. The list is of argument names and
    # not of tools, so this needs no entry to be quiet — which is the point:
    # the next tool added is safe before anybody thinks about it.
    asked = logged(calling("gws", {"request": "did the school mail about "
                                              "Friday", "lang": "en"}))
    check("a tool nobody wrote a rule for says nothing either",
          "school" in asked, False, asked)
    check("but the language tag is nobody's words and is printed whole",
          "'lang': 'en'" in asked, True, asked)

    trip = logged(calling("maps_route", {"origin": "home",
                                         "destination": "Kanazawa station",
                                         "travel_mode": "TRANSIT"}))
    check("an address is sized at either end of a trip",
          ["<4 chars>" in trip, "<16 chars>" in trip], [True, True], trip)
    check("and the mode of travel, being one of four words, is not",
          "'travel_mode': 'TRANSIT'" in trip, True, trip)

    check("a call that carries no arguments at all still logs that it "
          "happened", logged(calling("plan", None)), "tool call plan None")
    check("a value that is neither words nor a number is named rather than "
          "measured", main._sized({"nested": "anything"}), "<dict>")
    check("and a number is printed, since `<1 chars>` says less than `5`",
          [main._sized(5), main._sized(None)], [5, None])

    # Two frames down, the same argument. `_log_tool_traffic` sizes `task` on
    # the way in and `plan.py` printed it whole again on every path of its own
    # that logs — including the double-ask at `plan.py:836`, which is INFO and
    # reached without anything having gone wrong. Driven through the functions
    # rather than read out of the format strings, so a sixth log line added
    # later is covered by a case that runs the code. #206, reopened.
    #
    # "A sixth line added later" used to mean a sixth line added to one of the
    # two modules named below, and the comment did not say so: `journalled`
    # attached to that module and set `propagate = False`, so a line emitted a
    # module down was not merely unasserted, it was unseen. It reads the whole
    # tree now, and the last case in this function is what holds it to that.
    # The general form of the property lives in `tools/leak_check.py`; these
    # cases stay because they name what each line has to go on saying, which a
    # sentinel cannot ask for. #235.
    heard: set[str] = set()

    async def journalled(source_name: str, run) -> str:
        """What the whole tree logged while `run` ran, as the journal holds it.

        `logged` above with the root of the tree in place of `main`'s logger.
        Both raise the level for the duration, for the same reason.

        `source_name` is only a label now. It used to be the logger this
        attached to, and that is precisely the hole #235 names: `plan.py` was
        sized by #206 and `gws.py` by #231, and both times the question came
        back out of a module the case was not listening to — the second time
        out of `_progress.py:251`, which `plan` reaches through `Notes` inside
        the very run these cases drive. Every module here calls
        `logging.getLogger(__name__)`, so a record from any of them propagates
        to `ai_terminal` and lands in `held`. `tools/leak_check.py` makes the
        same move as its whole design.

        The loggers each capture reached are collected into `heard`, so the
        widening is something a case can assert rather than something the
        docstring claims.
        """
        held = Journaled()
        source = logging.getLogger("ai_terminal")
        was, quiet = source.level, source.propagate
        source.setLevel(logging.INFO)
        source.propagate = False
        source.addHandler(held)
        try:
            await run()
        finally:
            source.removeHandler(held)
            source.setLevel(was)
            source.propagate = quiet
        assert held.lines, f"{source_name} logged nothing, nothing was measured"
        heard.update(held.names)
        return "\n".join(held.lines)

    async def planned(run) -> str:
        """The planner's journal."""
        return await journalled("ai_terminal.live.tools.plan", run)

    async def asked(run) -> str:
        """Workspace's, whose five request lines are the same five. #231."""
        return await journalled("ai_terminal.live.tools.gws", run)

    goal = "book the vet for the cat on thursday"
    sized = f"<{len(goal)} chars>"

    async def asking(answer):
        """One `_ask`, with the planner itself replaced by `answer`.

        `_agent` is stood up as a bare object because `_ask` only checks it
        against `None` (`plan.py:773`) before handing the work to `_run`, and
        `_run` is what is being replaced. Nothing reaches a model.
        """
        async def _run(args, tool_context, seen):
            return await answer()

        was_run, was_agent = plan_mod._run, plan_mod._agent
        plan_mod._run, plan_mod._agent = _run, object()
        try:
            return await plan_mod._ask({"task": goal}, None)
        finally:
            plan_mod._run, plan_mod._agent = was_run, was_agent

    async def too_slow():
        raise TimeoutError

    async def fell_over():
        raise RuntimeError("the planner fell over")

    async def said_nothing():
        return ""

    late = await planned(lambda: asking(too_slow))
    check("a plan that ran out of time does not log what was asked for",
          goal in late, False, late)
    check("only how long the asking was", sized in late, True, late)

    broke = await planned(lambda: asking(fell_over))
    check("nor does the traceback line, whose format string is the whole of "
          "what reaches the log", goal in broke, False, broke)

    empty = await planned(lambda: asking(said_nothing))
    check("nor the line for a planner that came back with nothing",
          goal in empty, False, empty)

    class Asked:
        """The little of a `ToolContext` that these paths read.

        The double-ask is answered at `plan.py:840`, above everything that
        needs a real session. The one below it gets as far as `_progress.Notes`
        (`_progress.py:145-150`), which is why the invocation context is here
        with an empty queue. `lang="en"` keeps `of_turn` from reading this
        object at all.

        `gws` reads the same four and no more: its two paths reach
        `screen.session_of` (`screen.py:143`, which falls back to
        `.session.id` with no live block above it) and the same `Notes`.
        """

        def __init__(self, session_id: str, call_id: str) -> None:
            self.session = type("S", (), {"id": session_id, "events": []})()
            self.function_call_id = call_id
            self.state: dict = {}
            self._invocation_context = type(
                "I", (), {"live_request_queue": None})()

    async def again():
        plan_mod._ASKING["s-206"] = {goal}
        try:
            await plan_mod.plan(goal, Asked("s-206", "c-206"), lang="en")
        finally:
            plan_mod._ASKING.pop("s-206", None)

    twice = await planned(again)
    check("and neither does asking for the same thing twice, which is the "
          "path that needs nothing to have gone wrong", goal in twice, False,
          twice)
    check("that one being the size too, rather than silence",
          sized in twice, True, twice)

    async def never_started():
        """The fifth line, which is above `_ask` rather than inside it.

        `_recent` is what raises because it is the first thing in the argument
        dict and `found` is bound by then, so the `finally` and the write after
        it run the way they would on a real failure.
        """
        was = plan_mod._recent
        plan_mod._recent = boom
        try:
            await plan_mod.plan(goal, Asked("s-207", "c-207"), lang="en")
        finally:
            plan_mod._recent = was

    def boom(*args, **kwargs):
        raise RuntimeError("the plan never got as far as the planner")

    stumbled = await planned(never_started)
    check("nor the one for a plan that fell over before the planner saw it",
          goal in stumbled, False, stumbled)

    # The same five lines in the same five shapes, one module over. `plan.py`
    # was brought into line by #206 and `gws.py` was not, so a household that
    # asked Workspace anything twice put the question in the journal — #231.
    # Driven, like the seven above, so a sixth line here is covered too.
    async def enquiring(answer):
        """One `_ask`, with the sub-agent replaced by `answer`.

        Two stubs where `plan` needs one. `_agent` is a bare object for the
        same reason — `gws.py:912` only compares it against `None` — and
        `available()` is what the same line asks next: it is a PATH lookup for
        the CLI that holds the owner's token, it answers False on a machine
        without one, and a False there returns UNCONFIGURED above all three of
        the lines under test. Nothing shells out: `_run` is what would have,
        and `_run` is the thing being replaced.
        """
        async def _run(args, tool_context):
            return await answer()

        was_run, was_agent = gws._run, gws._agent
        was_available = gws._gws_cli.available
        gws._run, gws._agent = _run, object()
        gws._gws_cli.available = lambda: True
        try:
            return await gws._ask({"request": goal}, None)
        finally:
            gws._run, gws._agent = was_run, was_agent
            gws._gws_cli.available = was_available

    late = await asked(lambda: enquiring(too_slow))
    check("a Workspace question that ran out of time does not log what was "
          "asked for either", goal in late, False, late)
    check("only how long the Workspace question was", sized in late, True,
          late)

    broke = await asked(lambda: enquiring(fell_over))
    check("nor does the traceback line for a sub-agent that fell over",
          goal in broke, False, broke)
    # The size on every one of the five and not only on the two the shape of
    # the bug made obvious. `goal not in text` alone passes a line that sizes
    # the wrong argument — sizing `context`, which is empty here, or the call
    # id — and a line reporting `<0 chars>` for a question somebody asked is a
    # log that has stopped saying anything. Measured: three mutants survived
    # the seven cases above this pair and the one below it, and die on these.
    check("and it says how long, rather than sizing whatever else was to hand",
          sized in broke, True, broke)

    empty = await asked(lambda: enquiring(said_nothing))
    check("nor the line for a sub-agent that came back with nothing",
          goal in empty, False, empty)
    check("which sizes the question too", sized in empty, True, empty)

    async def enquiring_again():
        gws._ASKING["s-231"] = {goal}
        try:
            await gws.gws(goal, Asked("s-231", "c-231"), lang="en")
        finally:
            gws._ASKING.pop("s-231", None)

    twice = await asked(enquiring_again)
    check("and neither does asking Workspace for the same thing twice, the "
          "other path that needs nothing to have gone wrong",
          goal in twice, False, twice)
    check("that one being the size too", sized in twice, True, twice)

    async def never_asked():
        """The fifth line, which is above `_ask` rather than inside it.

        `_ask` itself is what raises, and synchronously: `gws.py:1040` evaluates
        the call before handing the coroutine to `notes.start`, so the raise
        lands inside the `with` block with `work` still None — which is the
        state the `finally` below it is written for.
        """
        def stumble(*args, **kwargs):
            raise RuntimeError("the question never got as far as the sub-agent")

        was = gws._ask
        gws._ask = stumble
        try:
            await gws.gws(goal, Asked("s-231b", "c-231b"), lang="en")
        finally:
            gws._ask = was

    fumbled = await asked(never_asked)
    check("nor the one for a question that fell over before the sub-agent "
          "saw it", goal in fumbled, False, fumbled)
    check("and that one is the question's own size and not the card's id",
          sized in fumbled, True, fumbled)

    # A sixth line in the same module, and not one a grep for the question
    # finds: what it prints is a cached document's filename, which `named`
    # hands back only when a phrase out of it was said out loud. Both of
    # `_prefetch`'s outward calls are replaced, so nothing reads the cache
    # directory and nothing starts a subprocess. #234.
    doc = "sentinel-tax-return-2025.md"

    async def unusable(refusal: dict) -> str:
        """What `_prefetch` logged for a copy it could not use."""
        was = gws_cache.named, gws_cache.read_cache

        async def read_cache(name: str) -> dict:
            return refusal

        gws_cache.named = lambda request: doc
        gws_cache.read_cache = read_cache
        try:
            return await asked(
                lambda: gws._prefetch(f"what does the {doc} say"))
        finally:
            gws_cache.named, gws_cache.read_cache = was

    # The refusal the sub-agent is really handed on this branch, prose and
    # all: three of the four spell a filename inside their advice, so a case
    # built on a bare `{"error": "..."}` would pass a line that goes on
    # printing it.
    dropped = await unusable({
        "name": doc, "why": "pdf-no-copy",
        "error": "That is the PDF itself, not its text — there is no "
                 f"readable copy beside it. Fetch `{doc}` again.",
    })
    check("the line for a copy the resolver could not use names no document",
          doc in dropped, False, dropped)
    check("only how long the name was", f"<{len(doc)} chars>" in dropped,
          True, dropped)
    # Which is the half a sentinel case cannot ask for on its own: a line
    # saying `unusable` and nothing else would pass both cases above and
    # stop answering the question it is read for.
    check("and why the resolve was dropped, in the word `read_cache` filed "
          "it under", "unusable, pdf-no-copy" in dropped, True, dropped)

    # The other half of the same function, unchanged: it has logged the shape
    # of a result and not its content since it was written, which is the
    # precedent the call line was brought into line with.
    back = logged(came_back("place_info", "a" * 40))
    check("the result line goes on saying how big the answer was",
          "{'answer': 40}" in back, True, back)

    # And the loudest of them all, which is not a tool argument: the accept
    # path logs the reverse-geocoded street address on every page load and
    # every reconnect, with nothing having gone wrong. Read rather than run —
    # nothing in the suite opens a socket, and standing one up to read a log
    # line would be a scaffold larger than the thing it guards. Structural
    # rather than a pair of string matches, because the way this comes undone
    # is the next line somebody adds beside them. #226.
    private = {"home_location", "home_coords"}
    accept = ast.parse(inspect.getsource(main.websocket_endpoint))
    whole = []
    for node in ast.walk(accept):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"):
            continue
        for arg in node.args:
            # `x or "(none)"` is the shape both of these had: a bare name with
            # a fallback beside it, which reads like a default and prints the
            # address whenever there is one.
            beside = arg.values if isinstance(arg, ast.BoolOp) else [arg]
            whole += [one.id for one in beside
                      if isinstance(one, ast.Name) and one.id in private]
    check("neither line the accept path writes hands a logger the panel's "
          "own address", whole, [])
    # Green before this and after it. Both lines still have to say whether
    # the panel knows where it is, which is what either was ever read for.
    said = inspect.getsource(main.websocket_endpoint)
    check("while both still say whether there is one",
          [said.count("_sized(home_location)"),
           said.count('"set" if home_coords else "(none)"')], [2, 1])

    # Last, and about the capture rather than about any one line. Every case
    # above drives one module and reads what came out; the four rounds before
    # #235 each did the same and each missed a line one module further down,
    # because `journalled` attached to the module named and set
    # `propagate = False` on it. It attaches at the root of the tree now, and
    # this is the case that says so — asserting the widening bought something
    # on the runs already here rather than that it was made. `_timing` is the
    # one it catches: `plan.py`'s `_timing.step("planner")` renders its fields
    # as JSON from `_timing.py:112`, one module away and invisible before,
    # which is the exact shape #234 fell through.
    check("the journal these cases read is the whole tree's, not one "
          "module's", sorted(heard),
          sorted({"ai_terminal.live.tools.plan",
                  "ai_terminal.live.tools.gws",
                  "ai_terminal.live.tools._timing"}),
          "a line one module below the one being driven is where four "
          "rounds of enumeration each found the next leak")


# ----------------------------------- what it still knows tomorrow morning

class Wrote:
    """A stand-in for the `ToolContext` a tool writes standing facts through.

    The real one hands the tool a `State` over the session's own values with
    the event's `state_delta` as its write side, and `State.__setitem__`
    (`sessions/state.py:91-98`) is what puts a key in both at once; that delta
    is what ADK appends when the call returns. Built the same way here, so a
    case exercises the write ADK would really make rather than a plain dict
    the check invented — which is the whole question for a `user:`-prefixed
    key, because it is the delta and not the dict that the SQLite backend
    files under the person.
    """

    def __init__(self, values=None):
        self.delta: dict = {}
        self.state = State(value=dict(values or {}), delta=self.delta)


async def rolled_bare(main, user_id: str, session_id: str) -> str:
    """The thread `rethread` would leave behind it, by the same two calls.

    `rethread` is a closure inside `websocket_endpoint`, so this makes the roll
    the way that function does — `create_session`, then one state-only event
    clearing `recap.RECAP_KEY` and nothing else (`main.py:1688-1702`). Under
    #142 this is the path that fires most, because a conversation now ends when
    the panel goes back to being a clock. What matters here is the "and nothing
    else": the facts are not on the session row, so a roll that clears the
    recap cannot reach them.
    """
    fresh = await main.session_service.create_session(
        app_name=main.APP_NAME, user_id=user_id, state={})
    await main.session_service.append_event(fresh, Event(
        author="user",
        actions=EventActions(state_delta={recap.RECAP_KEY: ""})))
    return fresh.id


async def memory_cases() -> None:
    print("\n== the few things that outlive the conversation ==")

    from ai_terminal.live import agent as agent_mod, main
    logging.getLogger().setLevel(logging.WARNING)

    # Everything below is about a tool nothing else in the suite pins onto the
    # agent: deleting the line that adds it left all nineteen scripts green.
    check("the tool the rest of this rests on is on the agent",
          any(tool is memory.remember_tool
              for tool in agent_mod.build_agent().tools), True,
          "a store nothing can write to is a store that is never wrong")

    # --- reading a row back, whatever is on it

    check("a person nobody has told anything about has nothing to read back",
          memory.known(ctx()), "")
    check("one fact reads back under the topic it was filed against",
          memory.known(ctx(**{memory.FACTS_KEY: {"the car": "It is blue."}})),
          "- the car: It is blue.")
    # The shape another release wrote. `_plan_run` keeps a list under its key
    # and this keeps a mapping, and a terminal that will not talk is a worse
    # answer than one that has to be told the household's name again.
    check("a store that came back as a list reads as nothing rather than as "
          "a store of single characters",
          memory.held({memory.FACTS_KEY: ["the car is blue"]}), {},
          "a bare sequence under this key is a previous release's pad")
    check("and so does a bare string", memory.held({memory.FACTS_KEY: "x"}),
          {})
    check("a fact with no topic to look it up under is dropped on read",
          memory.held({memory.FACTS_KEY: {"": "It is blue.", " ": "x"}}), {})
    check("and a topic with no fact under it",
          memory.held({memory.FACTS_KEY: {"the car": "   "}}), {})
    check("a fact longer than one sentence is trimmed and its whitespace "
          "collapsed",
          memory.held({memory.FACTS_KEY: {"the car": "a  b\n" * 200}}
                      )["the car"][:4], "a b ",
          f"one line of at most {memory.FACT_LINE} characters")
    check("and trimmed to exactly the cap",
          len(memory.held({memory.FACTS_KEY: {"c": "x" * 500}})["c"]),
          memory.FACT_LINE)

    # Both caps drop the oldest, because the newest thing somebody said about
    # their household is the one most likely to still be true.
    many = {f"t{n}": "x" for n in range(memory.FACTS + 5)}
    check(f"past {memory.FACTS} facts the oldest fall off the front",
          list(memory.held({memory.FACTS_KEY: many}))[:1] +
          list(memory.held({memory.FACTS_KEY: many}))[-1:],
          ["t5", f"t{memory.FACTS + 4}"],
          "the newest are the ones kept")
    fat = {f"t{n}": "x" * memory.FACT_LINE for n in range(40)}
    check("and the character budget bites first when the facts are long",
          len(memory.held({memory.FACTS_KEY: fat})) < memory.FACTS, True,
          f"{len(memory.held({memory.FACTS_KEY: fat}))} of {memory.FACTS} "
          f"fit inside {memory.FACT_CHARS} characters")
    # Inside it, and not inside it plus one more entry. The budget is a claim
    # about the system instruction, which is where all of this is spent.
    wide = sum(len(t) + len(f)
               for t, f in memory.held({memory.FACTS_KEY: fat}).items())
    check("and the budget is a cap rather than the line it is crossed on",
          wide <= memory.FACT_CHARS, True,
          f"{wide} characters against {memory.FACT_CHARS}")

    # `_one` calls `str()` on purpose, and its own comment says why: whatever
    # the model put in the argument. A falsy one is still an answer.
    check("a fact somebody gave as a number is kept, not read as empty",
          memory.held({memory.FACTS_KEY: {"cats here": 0, "alex drives": False,
                                          "nothing": None}}),
          {"cats here": "0", "alex drives": "False"},
          "0 and False are answers; None is the argument not being there")

    # --- what the tool writes

    wrote = Wrote()
    said = memory.remember("The Car ", " The car is the blue one. ", wrote)
    check("remembering a fact puts it in the delta ADK will append, under a "
          "key of its own filed per person",
          wrote.delta,
          {memory.FACT_PREFIX + "The Car":
           {"fact": "The car is the blue one.", "at": 1}},
          "`user:`-prefixed, so it outlives the session it was said in")
    check("and that key is the only thing in the delta",
          list(wrote.delta), [memory.FACT_PREFIX + "The Car"],
          "a delta carrying a topic this call did not touch overwrites "
          "whatever the other page on this profile did to it")
    check("and says what is now known", said,
          {"kept": True, "topic": "The Car", "known": 1, "dropped": 0})

    memory.remember("alex's diet", "Alex is vegetarian.", wrote)
    check("a second topic sits beside the first",
          list(memory.held(wrote.state)), ["The Car", "alex's diet"])

    # The third of the three the issue names, and the reason this is a mapping
    # and not the list `_plan_run` keeps: two contradictory memories are worse
    # than neither, and a list has nowhere to put the contradiction but beside
    # the thing it contradicts.
    again = memory.remember("The Car", "The car is the red one.", wrote)
    check("correcting a fact replaces it rather than sitting beside it",
          memory.held(wrote.state),
          {"The Car": "The car is the red one.",
           "alex's diet": "Alex is vegetarian."})
    check("so the count does not grow and nothing was dropped to make room",
          (again["known"], again["dropped"]), (2, 0))
    check("and the correction keeps the place the fact already had rather "
          "than jumping to the end",
          list(memory.held(wrote.state)), ["The Car", "alex's diet"],
          "a fact that was never wrong should not be aged out by a fix")

    # Correcting a fact must not be how it is lost. The reverse walk reaches
    # the entry being corrected last, because a correction stays where it was,
    # so at the budget it is the one the walk has no room left for — and the
    # old fact went with it, since the correction overwrote it first.
    budget = Wrote()
    memory.remember("the car", "Blue.", budget)
    for n in range(7):
        memory.remember(f"t{n}", "x" * memory.FACT_LINE, budget)
    filled = memory.held(budget.state)
    check("a store filled to just inside the character budget holds all of it",
          (len(filled), "the car" in filled), (8, True),
          f"{sum(len(t) + len(f) for t, f in filled.items())} characters "
          f"against {memory.FACT_CHARS}")
    fixed = memory.remember("the car", "y" * memory.FACT_LINE, budget)
    check("correcting the oldest fact in a store at the budget keeps it, "
          "rather than losing both it and the correction",
          memory.held(budget.state).get("the car"), "y" * memory.FACT_LINE,
          "re-filed as the newest, and only on the store that would have "
          "dropped it")
    check("and what fell off to make the room was the next oldest",
          (fixed["dropped"], "t0" in memory.held(budget.state)), (1, False),
          "the fact somebody is fixing is not the one to sacrifice for it")
    # A topic that fell off the end is still on the row, unread, so saying it
    # again is a write under the same key with a new age. Under the other
    # spelling it comes back under the older one, which is the rule the whole
    # module folds case by.
    memory.remember("T0", "Said again.", budget)
    check("a topic that fell off the end comes back when somebody says it "
          "again", memory.held(budget.state).get("t0"), "Said again.",
          "nothing on the write path blanks a key another page may have "
          "written since")
    # The write that evicts is the write that erases. A second page holds the
    # snapshot its run armed with, so what it thinks is the oldest topic is
    # the one the household corrected a moment ago on the other page — and a
    # correction at the budget is re-filed as the newest, which is exactly the
    # move the stale snapshot cannot see.
    kiosk = Wrote()
    memory.remember("the car", "Blue.", kiosk)
    for n in range(7):
        memory.remember(f"u{n}", "x" * memory.FACT_LINE, kiosk)
    tab = Wrote(dict(kiosk.state.to_dict()))
    memory.remember("the car", "y" * memory.FACT_LINE, kiosk)
    memory.remember("the wifi", "z" * memory.FACT_LINE, tab)
    merged = dict(kiosk.state.to_dict())
    merged.update(tab.delta)
    check("a second page at the budget does not delete the topic the first "
          "page just corrected",
          memory.held(merged).get("the car"), "y" * memory.FACT_LINE,
          "its snapshot is too old to say which topic is the oldest")

    # Concurrent tabs holding separate Wrote instances allocate strictly
    # monotonic ages, even when initialized with an older snapshot.
    tab_a = Wrote()
    memory.remember("topic a1", "Fact A1", tab_a)
    age_a1 = tab_a.delta[memory.FACT_PREFIX + "topic a1"]["at"]

    tab_b = Wrote(dict(tab_a.state.to_dict()))
    memory.remember("topic a2", "Fact A2", tab_a)
    age_a2 = tab_a.delta[memory.FACT_PREFIX + "topic a2"]["at"]

    memory.remember("topic b1", "Fact B1", tab_b)
    age_b1 = tab_b.delta[memory.FACT_PREFIX + "topic b1"]["at"]

    memory.remember("topic a3", "Fact A3", tab_a)
    age_a3 = tab_a.delta[memory.FACT_PREFIX + "topic a3"]["at"]

    tab_c = Wrote()
    memory.remember("topic c1", "Fact C1", tab_c)
    age_c1 = tab_c.delta[memory.FACT_PREFIX + "topic c1"]["at"]

    check("concurrent tabs with separate wrote instances allocate strictly "
          "monotonic ages",
          age_a1 < age_a2 < age_b1 < age_a3 < age_c1, True,
          "an older snapshot must not rewind or duplicate sequence age")

    # A topic differing only in case is the same topic. The model is told to
    # re-use the exact spelling and cannot be relied on to, and two entries
    # under one topic is the contradiction this store exists to avoid.
    cased = Wrote()
    memory.remember("The Car", "It is blue.", cased)
    corrected = memory.remember("the car", "It is red.", cased)
    check("a topic that differs only in case corrects the fact rather than "
          "sitting beside it",
          memory.held(cased.state), {"The Car": "It is red."},
          "one topic to everybody except a dictionary")
    check("under the spelling it was first filed with, which is the one "
          "already in front of the model", corrected["topic"], "The Car")
    check("and the older shape of the store folds the same way on read",
          memory.held({memory.FACTS_KEY: {"The Car": "It is blue.",
                                          "the car": "It is red."}}),
          {"The Car": "It is red."})
    # And that row is one somebody can still be talking to. Correcting it
    # writes a key under the older spelling only, so the sibling is left in
    # the old mapping — where it is aged below zero and would be the newer of
    # the two on age alone, since a correction inherits the folded topic's
    # age. Until the key wins outright, every correction is swallowed by a
    # spelling nobody typed.
    legacy = Wrote({memory.FACTS_KEY: {"The Car": "It is blue.",
                                       "the car": "It is red."}})
    memory.remember("the car", "It is green.", legacy)
    check("a correction to a legacy row carrying two spellings of one topic "
          "replaces the fact",
          memory.held(legacy.state), {"The Car": "It is green."},
          "the spelling left behind in the old mapping is not a newer fact")
    memory.remember("The Car", "It is white.", legacy)
    check("and so does the one after it, under either spelling",
          memory.held(legacy.state), {"The Car": "It is white."},
          "a topic with a key of its own is not read out of the mapping "
          "again")
    # However the mapping happens to capitalise them, which is not something
    # anybody chose. A skip matched on the letters rather than on the topic
    # only catches the spelling that is already in lower case, and leaves
    # every other sibling sitting there.
    shouted = Wrote({memory.FACTS_KEY: {"The Bins": "Tuesday.",
                                        "The bins": "Friday."}})
    memory.remember("the bins", "Wednesday.", shouted)
    check("and it supersedes them whichever way the mapping spells them",
          memory.held(shouted.state), {"The Bins": "Wednesday."},
          "two capitalised spellings and the correction is swallowed by the "
          "one nobody typed")
    # What the key must not do is take the rest of the mapping with it. One
    # topic filed since the split says nothing about the others, which have
    # no key and are still only readable there.
    mixed = Wrote({memory.FACTS_KEY: {"the wifi": "It is slow."}})
    memory.remember("the car", "It is blue.", mixed)
    check("a topic left in the old mapping is still read after another topic "
          "is filed under a key of its own",
          memory.held(mixed.state),
          {"the wifi": "It is slow.", "the car": "It is blue."},
          "the entries a key supersedes are that topic's, not the row's")
    # And the age it folds to is the newest of the spellings, not the oldest.
    # A correction inherits that age, so an age folded backwards is a fact
    # filed before things it was written after — which at the budget is the
    # correction being the next thing to fall off, and the topic nobody has
    # touched in weeks outliving it.
    straddled = Wrote({memory.FACTS_KEY: {
        "The Car": "It is blue." + "x" * 100,
        "the wifi": "It is slow." + "x" * 100,
        "the car": "It is red." + "x" * 100}})
    memory.remember("the car", "It is green." + "x" * 100, straddled)
    for n in range(7):
        memory.remember(f"w{n}", "x" * memory.FACT_LINE, straddled)
    standing = memory.held(straddled.state)
    check("a corrected topic keeps the newest of its spellings' ages, so it "
          "is not the first thing the budget drops",
          ("the car" in {t.casefold() for t in standing},
           "the wifi" in standing), (True, False),
          "folded to the oldest, the correction sorts before the topic it "
          "was written after")
    # `kept: True` is a promise the household can read the fact back, and the
    # write path has to ask that of the whole row. Nothing is evicted on the
    # write path now, so a store showing seven facts can be holding eleven,
    # and the read path caps from the front — which is exactly where that
    # overflow sits. Asked only of what is readable, the re-file that saves a
    # correction from the budget never fires.
    deep = Wrote({memory.FACTS_KEY: {"alex's diet": "No meat." + "x" * 100}})
    for n in range(10):
        memory.remember(f"v{n}", "x" * memory.FACT_LINE, deep)
    corrected = memory.remember("alex's diet", "Vegan now." + "x" * 180, deep)
    check("a correction that says it was kept is one the household can read "
          "back, on a row holding more than it shows",
          (corrected["kept"], memory.held(deep.state).get("alex's diet")),
          (True, "Vegan now." + "x" * 180),
          "measured against the readable facts, the write is measured "
          "against the wrong store and falls off the end it was re-filed to "
          "escape")
    # A topic that fell off the end and is said again is news: it folds
    # against what is readable, so it comes back under the spelling it was
    # said with and takes a fresh age. The row then holds two keys for it and
    # `_scan` folds them, keeping the newest age — but the order it hands
    # `kept` is where the older key sits, and `kept` drops from the front.
    revived = Wrote()
    memory.remember("the bins", "Tuesday.", revived)
    for n in range(9):
        memory.remember(f"u{n}", "x" * memory.FACT_LINE, revived)
    again = memory.remember("THE BINS", "Wednesday.", revived)
    readable = {topic.casefold(): fact
                for topic, fact in memory.held(revived.state).items()}
    check("a topic said again after falling off the end is read back, not "
          "dropped for sitting where its older key does",
          (again["kept"], readable.get("the bins")), (True, "Wednesday."),
          "ordered on where a topic first appeared and capped on the age it "
          "ended up with, the row drops the newest fact on it")
    # And the correction after that one, which is where the second key the
    # comeback left behind stops being harmless. The fold renders under the
    # older spelling while the fact is on the newer key, so a write addressed
    # by the rendered spelling lands on the key that is not holding it — and
    # on the age it inherited from the fold it ties with the one that is,
    # where the tie breaks on the topic string. The household is told the
    # correction was kept and reads back the fact it replaced.
    twinned = Wrote()
    memory.remember("The Car", "It was blue. " + "x" * (memory.FACT_LINE - 13),
                    twinned)
    for n in range(8):
        memory.remember(f"p{n}", f"pad {n} " + "y" * (memory.FACT_LINE - 7),
                        twinned)
    memory.remember("the car", "It is red. " + "z" * (memory.FACT_LINE - 11),
                    twinned)
    fixed = memory.remember("The Car", "It is green now.", twinned)
    check("a topic that came back under the other spelling takes the next "
          "correction with it, rather than leaving it on a second key",
          (fixed["kept"],
           {topic: fact for topic, fact in memory.held(twinned.state).items()
            if topic.casefold() == "the car"}),
          (True, {"The Car": "It is green now."}),
          "the spelling the store renders under is not always the key the "
          "fact is on")
    # The same row, arrived at the other way. Two live keys for one topic do
    # not need nine pads to appear: an older release, a hand-edited row and a
    # second page all leave spellings `_one` folds together, and the three
    # below are the shapes where picking the wrong one of them costs the
    # household the correction. The key to write is the greatest `(at, key)`
    # among the live ones, ranked on the key's own suffix because that is what
    # the read is ranked on (`memory.py:225` and `:237`) — not on the folded
    # topic, which orders ` the car ` and `the car ` on either side of it.

    def corrected(row: dict) -> tuple:
        """What the household reads back about the car, after correcting it."""
        page = Wrote(row)
        said = memory.remember("The Car", "It is green now.", page)
        return said["kept"], {
            topic: fact
            for topic, fact in memory.held({**row, **page.delta}).items()
            if topic.casefold() == "the car"}

    check("a correction goes to a live key even when an emptied one holds the "
          "newest age",
          corrected({
              "user:fact:The Car": {"fact": "It was blue.", "at": 3},
              "user:fact:the car": {"fact": "", "at": 9},
              "user:fact:the car ": {"fact": "It was towed.", "at": 3}}),
          (True, {"The Car": "It is green now."}),
          "a row emptied to retire a fact must not take the next write, and "
          "the spelling said last must not lose to one merely sorted later")
    check("and to the newer of two live keys, not the one it renders under",
          corrected({
              "user:fact:The Car": {"fact": "It was blue.", "at": 9},
              "user:fact:the car": {"fact": "It is red.", "at": 3}}),
          (True, {"the car": "It is green now."}),
          "the age on the row is what `_scan` reads the fact by")
    check("and to the raw key where two spellings fold together but sort "
          "apart",
          corrected({
              "user:fact:the car": {"fact": "It was blue.", "at": 3},
              "user:fact:the car ": {"fact": "It is red.", "at": 3}}),
          (True, {"the car": "It is green now."}),
          "ranked on the folded topic the write lands one key away from the "
          "one the household reads out of")

    # A topic whose length and spaces cause it to fold differently on one
    # vs two applications of `_one`.
    long_topic = "the bins, which go out on Tuesday night unless it is a bank holiday"
    t_once = memory._one(long_topic, memory.TOPIC_LINE)
    t_twice = memory._one(t_once, memory.TOPIC_LINE)
    twinned_row = {
        memory.FACT_PREFIX + t_twice: "",
        memory.FACT_PREFIX + t_once: {"fact": "It was blue.", "at": 3},
    }
    check("a live fact on a once-folded key is not swallowed by an emptied "
          "twice-folded key for the same topic",
          memory.held(twinned_row),
          {t_twice: "It was blue."},
          "a headstone left on one spelling must not kill a live fact on another")

    twinned_page = Wrote(twinned_row)
    twinned_said = memory.remember(t_twice, "Wednesday now.", twinned_page)
    twinned_after = memory.held({**twinned_row, **twinned_page.delta})
    check("correcting a topic whose spelling folds twice replaces the fact and "
          "reads back as the correction",
          (twinned_said["kept"],
           len(twinned_after) == twinned_said["known"],
           twinned_after.get(t_twice)),
          (True, True, "Wednesday now."),
          "the correction must be kept and readable, matching the reported count")

    # Path A: long topic remembered, forgotten, then re-stated under rendered spelling
    a = Wrote()
    memory.remember(long_topic, "Tuesday.", a)
    rowa = {**a.state.to_dict(), **memory.forgetting(a.state)}
    a2 = Wrote(rowa)
    r = memory.remember(t_twice, "Wednesday now.", a2)
    after_a = memory.held({**rowa, **a2.delta})
    check("a fact stated again after being forgotten reads back under its rendered spelling",
          (r["kept"],
           len(after_a) == r["known"],
           after_a.get(t_twice)),
          (True, True, "Wednesday now."),
          "an emptied key from forgetting must not prevent re-remembering the topic")

    check("a fact with nothing in it is refused rather than filed empty",
          memory.remember("the car", "   ", wrote)["why"],
          memory.NOTHING_TO_KEEP)
    check("and so is a fact with no topic",
          memory.remember("", "It is blue.", wrote)["kept"], False)
    check("neither of which disturbs what is already known",
          list(memory.held(wrote.state)), ["The Car", "alex's diet"])
    check("and a call with no conversation under it is answered, not raised",
          memory.remember("the car", "It is blue.",
                          type("NoCtx", (), {})())["why"], memory.NOT_HERE,
          "the live agent can reach a tool where there is no state")

    # --- and what survives the thread being rolled away

    service, gap = main.session_service, main.IDLE_GAP_S
    # The same guard as `forget_cases`, and for the same reason: `:1954` calls
    # `api_forget` for real, and since #233 that unlinks every file under
    # `cache_dir()`, which resolves against the working directory. A sweep from
    # the repo root would delete the developer's own `gws_cache/`. Measured, not
    # anticipated — landing the #233 patch with only `forget_cases` stubbed took
    # out 18 real documents on the first run here.
    was_cache_dir = gws_cache.cache_dir
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "sessions.db"
        main.session_service = type(service)(db_path=str(store))
        cached = Path(tmp) / "cache"
        cached.mkdir()
        gws_cache.cache_dir = lambda: cached
        try:
            main.IDLE_GAP_S = 0.05
            first = await main._thread("alex", {})
            opened = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=first)
            await main.session_service.append_event(
                opened, heard("I'm vegetarian, and the car is the blue one"))
            await main.session_service.append_event(opened, replied("noted"))
            # Exactly what the tool call would leave behind: the delta off a
            # `State` over this session's values, appended as its own event.
            wrote = Wrote(opened.state)
            memory.remember("alex's diet", "Alex is vegetarian.", wrote)
            memory.remember("the car", "The car is the blue one.", wrote)
            await main.session_service.append_event(opened, Event(
                author="user",
                actions=EventActions(state_delta=dict(wrote.delta))))

            await asyncio.sleep(0.15)
            with model_says("they said they were vegetarian"):
                second = await main._thread("alex", {})
            check("a quiet gap rolls the thread, as it always did",
                  second != first, True, detail=second)
            after = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=second)

            # 1. The fact is still reachable.
            check("and the facts stated on the old one are still there",
                  memory.held(after.state),
                  {"alex's diet": "Alex is vegetarian.",
                   "the car": "The car is the blue one."},
                  "nothing carried them: they are not on the session row")
            # 2. The conversation is not — a memory that quietly restores the
            # transcript is #142 coming back through the side door.
            check("while the conversation they were said in is not",
                  recap.transcript(after.events), "",
                  "the new thread replays nothing of the old one")

            # 3. The bare roll, which under #142 is the one that fires most.
            third = await rolled_bare(main, "alex", second)
            bare = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=third)
            check("a roll that clears the recap does not reach the facts",
                  (bare.state.get(recap.RECAP_KEY),
                   list(memory.held(bare.state))),
                  ("", ["alex's diet", "the car"]),
                  "the wake-word path rolls bare, and bare is one key")

            # And a reboot, which is what rules out `InMemoryMemoryService`:
            # #10 says this machine has to come back on its own.
            main.session_service = type(service)(db_path=str(store))
            restarted = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=third)
            check("nor does the server being restarted under it",
                  list(memory.held(restarted.state)),
                  ["alex's diet", "the car"],
                  "a fresh service object over the same file")

            check("so the next setup opens knowing them",
                  "Alex is vegetarian." in
                  agent_mod._instruction(ctx(**restarted.state)), True,
                  "read once, when the Live API takes the instruction")
            check("and not knowing what was said",
                  "the car is the blue one" in
                  agent_mod._instruction(ctx(**restarted.state)), False,
                  "the fact is capitalised; the spoken turn is not")

            # --- two pages on one person, which is two threads

            # `main.py:1538-1542` says it in the code this is about: a page
            # that loads after the gap gets a conversation of its own. So two
            # pages on one profile are two sessions writing to one row, each
            # holding the snapshot it armed with, because `Runner.run_live`
            # reads the session once (`runners.py:1963-1967`). ADK's own
            # `StaleSessionError` keys on the session id and cannot see this.
            kiosk = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="sam", state={})
            here = Wrote(kiosk.state)
            memory.remember("the car", "The car is the blue one.", here)
            await main.session_service.append_event(kiosk, Event(
                author="user",
                actions=EventActions(state_delta=dict(here.delta))))

            phone = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="sam", state={})
            there = Wrote(phone.state)
            memory.remember("sam's diet", "Sam is vegetarian.", there)
            await main.session_service.append_event(phone, Event(
                author="user",
                actions=EventActions(state_delta=dict(there.delta))))

            # The kiosk writes again on the snapshot it still holds, which
            # knows nothing about the diet. Under one key for the whole store
            # this is the write that erased it.
            here.delta.clear()
            memory.remember("the wifi", "The wifi is called koala.", here)
            await main.session_service.append_event(kiosk, Event(
                author="user",
                actions=EventActions(state_delta=dict(here.delta))))

            shared = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="sam", session_id=phone.id)
            check("a page writing on a stale snapshot does not erase what the "
                  "other page on the same profile wrote",
                  sorted(memory.held(shared.state)),
                  ["the car", "the wifi", "sam's diet"],
                  "one key per topic, so a delta cannot carry a topic the "
                  "call never touched")

            # --- 35 registered facts: historical keys preserved in storage, render capped
            crowded = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="ken", state={})
            ken_wrote = Wrote(crowded.state)
            for n in range(35):
                memory.remember(f"t{n}", f"fact {n}", ken_wrote)
            await main.session_service.append_event(crowded, Event(
                author="user",
                actions=EventActions(state_delta=dict(ken_wrote.delta))))

            ken_sess = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="ken", session_id=crowded.id)
            with sqlite3.connect(store) as db:
                raw_user_state = db.execute(
                    "SELECT state FROM user_states WHERE user_id=?",
                    ("ken",)).fetchone()[0]
                stored_keys = [k for k in json.loads(raw_user_state)
                               if k.startswith("fact:")]

            check("35 registered facts preserve all historical keys in storage",
                  (len(stored_keys),
                   len([k for k in ken_sess.state if k.startswith(memory.FACT_PREFIX)])),
                  (35, 35),
                  "sqlite user_states is append-only merge, no eviction on write")
            check("while rendering stays within the cap",
                  (len(memory.held(ken_sess.state)),
                   list(memory.held(ken_sess.state))[:1],
                   list(memory.held(ken_sess.state))[-1:]),
                  (memory.FACTS, ["t5"], ["t34"]),
                  f"prompt render capped at {memory.FACTS}, oldest 5 dropped")

            # --- and the button called forget means it

            await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="jordan", state={})
            hers = Wrote()
            memory.remember("jordan's name", "Jordan is said MA-ri.", hers)
            scratch = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="jordan", state={})
            await main.session_service.append_event(scratch, Event(
                author="user",
                actions=EventActions(state_delta=dict(hers.delta))))

            await main.api_forget("alex", from_our_page())
            blank = await main._thread("alex", {})
            emptied = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="alex", session_id=blank)
            check("forgetting a person takes the standing facts with the "
                  "conversations", memory.held(emptied.state), {},
                  "deleting every session does not: the key is per person")
            # The whole sentence, not the word. `INSTRUCTION` uses the same
            # word as an example of what `remember` is for, so a substring
            # check on "vegetarian" alone passes on the empty store and fails
            # on the full one — a case that reads as green while asserting the
            # opposite of what it says.
            check("so the terminal stops opening by knowing them",
                  "Alex is vegetarian." in
                  agent_mod._instruction(ctx(**emptied.state)), False,
                  "the fact itself, since the instruction names the word")
            keeps = await main.session_service.get_session(
                app_name=main.APP_NAME, user_id="jordan", session_id=scratch.id)
            check("while the person standing next to them keeps theirs",
                  memory.held(keeps.state), {"jordan's name": "Jordan is said "
                                                            "MA-ri."},
                  "`user:` is per person, and the clear must be too")
        finally:
            main.session_service, main.IDLE_GAP_S = service, gap
            gws_cache.cache_dir = was_cache_dir


# ------------------------------------ two connections holding one id at once

def idle() -> asyncio.Task:
    """Work that is still running when somebody comes to cancel it."""
    return asyncio.create_task(asyncio.sleep(30))


async def settle() -> None:
    """Let the loop deliver the cancellations that were just asked for."""
    for _ in range(3):
        await asyncio.sleep(0)


async def handover_cases() -> None:
    print("\n== the moment a reload has two connections on one id ==")

    sid = "s-reload"

    leaving = browse.open_session(sid)
    going = idle()
    leaving["a timetable"] = going
    arriving = browse.open_session(sid)
    staying = idle()
    arriving["a timetable"] = staying

    browse.close_session(sid, leaving)
    await settle()
    check("the browse job of the connection that left is cancelled",
          going.cancelled(), True, "its browser is nobody's now")
    check("the one the new connection started is not", staying.cancelled(),
          False)
    check("and the new connection is the one still being tracked",
          browse.jobs_of(sid) is arriving, True,
          "or nothing can reach its Chromium when it goes")

    browse.close_session(sid, arriving)
    await settle()
    check("an ordinary close, with nobody behind it, does clear the id",
          [staying.cancelled(), sid in browse._JOBS], [True, False])

    gws._ASKING[sid] = {"what is on today"}
    leaving = gws.open_session(sid)
    going = idle()
    leaving.add(going)
    arriving = gws.open_session(sid)
    staying = idle()
    arriving.add(staying)

    gws.close_session(sid, leaving)
    await settle()
    check("the Workspace question the old connection had running is cancelled",
          going.cancelled(), True)
    check("the new connection's question is left alone", staying.cancelled(),
          False)
    check("its own set is still the tracked one",
          gws.running_of(sid) is arriving, True)
    # `_ASKING` is keyed by id alone, with no token to check it against, so it
    # has to go with the entry that owns it rather than on the way past.
    check("and the question it is waiting on is still held",
          sorted(gws._ASKING.get(sid, ())), ["what is on today"],
          "cleared, a repeat runs a second sub-agent over the same question")

    gws.close_session(sid, arriving)
    await settle()
    check("both go when the last connection does",
          [sid in gws._RUNNING, sid in gws._ASKING], [False, False])

    leaving = plan_mod.open_session(sid)
    going = idle()
    leaving.add(going)
    arriving = plan_mod.open_session(sid)
    staying = idle()
    arriving.add(staying)

    plan_mod.close_session(sid, leaving)
    await settle()
    check("a plan follows the same rule, so the old one's is cancelled",
          going.cancelled(), True)
    check("the new one's keeps running", staying.cancelled(), False)
    check("under the entry it opened for itself",
          plan_mod.running_of(sid) is arriving, True)
    plan_mod.close_session(sid, arriving)
    await settle()

    # The overlap that does not end, on the three tool registries. #127 gave
    # `screen` a stack for this and left these as one entry per session id; the
    # difference is that the reload above is two connections passing each other
    # and this is two that stay — the kiosk in the kitchen and a tab on the Mac
    # serving it, one session id between them since #19.
    #
    # Kept empty on purpose. Two connections that have not started anything yet
    # hold registry entries that compare equal, which is the ordinary state and
    # the one where an identity test is the only thing that can tell them
    # apart: matched with `==`, a close takes whichever entry it reaches first
    # and that is the kiosk's.
    kiosk_jobs = browse.open_session(sid, "c-kiosk")
    laptop_jobs = browse.open_session(sid, "c-laptop")
    kiosk_gws = gws.open_session(sid, "c-kiosk")
    laptop_gws = gws.open_session(sid, "c-laptop")
    kiosk_plan = plan_mod.open_session(sid, "c-kiosk")
    laptop_plan = plan_mod.open_session(sid, "c-laptop")

    with conn.speaking("c-kiosk"):
        held = browse.jobs_of(sid) is kiosk_jobs
    check("a browse asked for on the kiosk is filed under the kiosk",
          held, True,
          "filed under the newest, it dies when somebody else's tab closes")
    # And with no connection named — a caller the ContextVar never reached —
    # the newest, which is the rule `screen.sink` settled on and what a lookup
    # keyed on the id alone answered before this. The other end of the list
    # would be a quieter version of the bug above: a browse filed under a tab
    # that has been open since before the kiosk booted.
    check("and with nobody named it is the newest, as the screen's is",
          browse.jobs_of(sid) is laptop_jobs, True)

    browse.close_session(sid, laptop_jobs)
    gws.close_session(sid, laptop_gws)
    plan_mod.close_session(sid, laptop_plan)
    await settle()
    check("and the tab closing leaves the kiosk the browser it had",
          browse.jobs_of(sid) is kiosk_jobs, True,
          "None here is 'No screen is connected' at a connected terminal")
    check("its Workspace questions too", gws.running_of(sid) is kiosk_gws,
          True, "untracked work is work no dropped socket ever cancels")
    check("and its plans", plan_mod.running_of(sid) is kiosk_plan, True)

    # The question the kiosk raises next. `_ASKING` is keyed by the session id
    # alone and shared by every connection on it, so it can only go when the
    # last of them does — and hung on owning the `_RUNNING` entry instead, it
    # never went at all: the tab had taken that entry over and the kiosk
    # matched nothing on the way out. A stranded entry refuses that one
    # question, silently, for the life of the process.
    gws._ASKING.setdefault(sid, set()).add("what is on today")
    browse.close_session(sid, kiosk_jobs)
    gws.close_session(sid, kiosk_gws)
    plan_mod.close_session(sid, kiosk_plan)
    await settle()
    check("and the last connection out clears the questions in flight",
          [sid in gws._RUNNING, sid in gws._ASKING, sid in browse._JOBS],
          [False, False, False])

    # The same two connections with nobody naming either. `conn.new_id` is all
    # that keeps them apart then, and a default that handed out one id would
    # put this whole issue back a layer down, where the connection ids look
    # right and there is only ever one of them.
    first = browse.open_session(sid)
    second = browse.open_session(sid)
    check("two connections that name themselves nothing are still two",
          [len(browse._JOBS[sid]), browse.jobs_of(sid) is second], [2, True],
          "one shared default id is one connection overwriting the other")
    browse.close_session(sid, second)
    check("so the one that stays keeps its entry when the other leaves",
          browse.jobs_of(sid) is first, True)
    browse.close_session(sid, first)

    async def old_screen(frame):
        pass

    async def new_screen(frame):
        pass

    screen.register(sid, old_screen)
    screen.register(sid, new_screen)
    screen.unregister(sid, old_screen)
    check("the screen belongs to whichever connection arrived last",
          screen.sink(sid) is new_screen, True,
          "taken away, every card is dropped and nothing says so")
    screen.unregister(sid, new_screen)
    check("and goes when that one leaves too", screen.sink(sid), None)

    # The overlap that does not end. A reload is one connection leaving and
    # one arriving, but two pages open on the same profile are two that stay —
    # the kiosk in the kitchen and a laptop on the same server, and #19 gave
    # them one session id between them. The newest owns the screen either way.
    # The question is what the older page is left holding when the newer one
    # closes with its own socket still up: nothing, and no way to tell.
    # `/api/wake` reaches a terminal through this registry and nothing else, so
    # the wake word goes dead on a screen that otherwise looks fine, and stays
    # dead until somebody reloads a page nobody is standing at.
    screen.register(sid, old_screen)
    screen.register(sid, new_screen)
    check("a second page on the same profile takes the screen while it is up",
          screen.sink(sid) is new_screen, True)
    screen.unregister(sid, new_screen)
    check("and hands it back when it goes, rather than leaving none at all",
          screen.sink(sid) is old_screen, True,
          "a kiosk with no sink is deaf: the wake word has nowhere to land")
    screen.unregister(sid, old_screen)
    check("and only then is the session without a screen",
          screen.sink(sid), None)

    screen.register(sid, old_screen)
    screen.unregister(sid)
    check("a caller with only an id to go on still drops what is there",
          screen.sink(sid), None)


# ---------------------------------- which of those screens hears a wake word

def a_kiosk(session_id: str, send) -> None:
    """Register a page that says it is the one standing in the kitchen.

    Through the signature rather than a bare call, so a tree where the flag
    does not exist yet registers an ordinary page and the cases below fail on
    the answer they came for. A `TypeError` here would take the suite down
    under the first of them and name nothing.
    """
    if "kiosk" in inspect.signature(screen.register).parameters:
        screen.register(session_id, send, kiosk=True)
    else:
        screen.register(session_id, send)


def wake_screen(session_id: str):
    """`screen.wake_sink`, with the absence of it reported as a value.

    The same shape as `wiring_check`'s `wake`, and for the same reason: a run
    against a tree that has not been wired up should say which wiring is
    missing in every case, not raise an `AttributeError` in the first.
    """
    resolve = getattr(screen, "wake_sink", None)
    return "no screen.wake_sink" if resolve is None else resolve(session_id)


# ------------------------------ what a resumed thread asks the model to do

class Wire:
    """The Live socket, recorded instead of opened.

    Stands in for `google.genai.live.AsyncSession`, which is the object ADK's
    `GeminiLlmConnection` writes the setup and the replay to. The real
    `GeminiLlmConnection` sits on top of it here rather than being faked in
    turn, because the line under check is inside it: `send_history` sets
    `turn_complete=contents[-1].role == 'user'`, and that flag is the
    difference between handing the model a conversation and asking it a
    question.

    `receive` blocks rather than ending, the way a socket with nothing to say
    does. A generator that returned would end `run_live` before the case had
    looked at what it sent.
    """

    session_id = "wire"

    def __init__(self):
        self.sent: list[tuple[list, bool | None]] = []
        self.shut = asyncio.Event()

    async def send_client_content(self, turns=None, turn_complete=None):
        self.sent.append((list(turns or []), turn_complete))

    async def send_realtime_input(self, **kwargs):
        pass

    async def close(self) -> None:
        self.shut.set()

    async def receive(self):
        await self.shut.wait()
        return
        yield  # pragma: no cover - unreachable, and makes this a generator


@contextlib.contextmanager
def recorded():
    """`Gemini.connect`, wired to a `Wire` instead of to Google.

    Patched on the class because that is where ADK looks it up: the live model
    is a bare string on the agent, and `canonical_live_model` turns it into a
    `Gemini` the server never gets a reference to.
    """
    from google.adk.models.gemini_llm_connection import GeminiLlmConnection
    from google.adk.models.google_llm import Gemini

    wires: list[Wire] = []
    was = Gemini.connect

    @contextlib.asynccontextmanager
    async def connect(self, llm_request):
        wire = Wire()
        wires.append(wire)
        opened = GeminiLlmConnection(wire, model_version="fake-live")
        try:
            yield opened
        finally:
            await wire.close()

    Gemini.connect = connect
    try:
        yield wires
    finally:
        Gemini.connect = was


class ArmingSocket:
    """A page that connects, says its frames, waits, and goes away.

    The frames a terminal sends around a wake word, and nothing else. The
    endpoint is driven rather than `arm` itself because `arm` is a closure
    over one connection and there is no other way in — which is the point:
    what this file is about is the thread the *server* picks up, and that is
    settled between `accept` and the `arm` frame.

    The frames are a list because there is more than one of them now: the
    screensaver's `sleep` arrives on this socket too, and what it does is
    decided by where it falls among the arms. The default is the one frame
    every case here used before that existed.
    """

    def __init__(self, frames: list[dict] | None = None,
                 hold: float = 0.4):
        self.sent: list[str] = []
        self.query_params: dict = {}
        self.headers = {"host": "terminal.local:8000",
                        "origin": "http://terminal.local:8000"}
        self.accepted = False
        self._steps = [
            (lambda frame=frame: {"type": "websocket.receive",
                                  "text": json.dumps(frame)})
            for frame in (frames if frames is not None else [{"type": "arm"}])
        ]
        self._steps.append(
            lambda: {"type": "websocket.receive", "text": "", "wait": hold})

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass

    async def receive(self) -> dict:
        if not self._steps:
            return {"type": "websocket.disconnect"}
        step = self._steps.pop(0)()
        if "wait" in step:
            # The replay runs in the task `arm` started, so the endpoint has
            # to be left in its receive loop long enough for it to happen.
            await asyncio.sleep(step.pop("wait"))
            return {"type": "websocket.disconnect"}
        return step

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def send_json(self, data: dict) -> None:
        self.sent.append(json.dumps(data))


class StaleArmingSocket(ArmingSocket):
    """The same page, on a thread that goes past the gap while it sits there.

    Which is a socket left up overnight, and it is the only way to reach the
    roll in `rethread` from here. Backdated rather than slept over, so the
    case holds at whatever `SESSION_IDLE_GAP_S` is set to and does not turn on
    how long a machine takes: `rethread` reads `sessions.update_time` and
    nothing else. Done on the first `receive` and not before the endpoint
    runs, because `_thread` reads that same column at accept and crossing the
    gap in *there* calls `_carry_forward` -> `recap.summarise`, which is a
    real model call this file will not make.
    """

    def __init__(self, store: Path, gap: float):
        super().__init__()
        self._store, self._backdate = store, gap * 2

    async def receive(self) -> dict:
        if self._backdate:
            with sqlite3.connect(self._store) as db:
                db.execute("UPDATE sessions SET update_time=update_time-?",
                           (self._backdate,))
            self._backdate = 0
        return await super().receive()


class GatedArmingSocket(ArmingSocket):
    """The same page, holding its last frame back until the case says so.

    Two pages have to take turns for a roll to reach a page that did not ask
    for one: the first dims, the second's wake word rolls the conversation,
    and only then does the first one hear its own wake word. Sleeping long
    enough to be sure of that order would make the case turn on how fast the
    machine is, so the order is stated instead. The gate opens before the last
    frame, which is the only place any case here needs it.
    """

    def __init__(self, frames: list[dict], gate: asyncio.Event,
                 hold: float = 0.4):
        super().__init__(frames, hold=hold)
        self._gate = gate

    async def receive(self) -> dict:
        # Two left is the last frame and the disconnect that follows it.
        if len(self._steps) == 2:
            await self._gate.wait()
        return await super().receive()


def bookkeeping() -> Event:
    """A state-only write, of the kind `_thread` and `rethread` both make.

    Invisible to the replay (`_contains_empty_content`), so it does not change
    whose turn the thread ends on — but it does sit between that turn and the
    end of the thread, which is the whole reason the tail read in `_settle` is
    more than one event deep.
    """
    return Event(author="user",
                 actions=EventActions(state_delta={recap.RECAP_KEY: ""}))


def called(name: str) -> Event:
    """The model reaching for a tool.

    Paired with `came_back` below and never left on its own, because ADK drops
    a response whose call it cannot find when it rebuilds the history
    (`flows/llm_flows/functions.py`), and a thread that replayed nothing would
    pass the cases here for the wrong reason.
    """
    return Event(author="ai_terminal", content=types.Content(
        role="model", parts=[types.Part(
            function_call=types.FunctionCall(name=name, args={}))]))


def came_back(name: str, answer: str) -> Event:
    """A tool's answer, on its way back to the model.

    Role `user`, which is not a choice this repo made: ADK writes function
    responses that way (`flows/llm_flows/functions.py`), and it is the role
    the replay reads off the end of the thread.
    """
    return Event(author="ai_terminal", content=types.Content(
        role="user", parts=[types.Part(
            function_response=types.FunctionResponse(
                name=name, response={"answer": answer}))]))


def note(text: str) -> Event:
    """A progress note, as the server's own injection is stored.

    `live/tools/_progress.py` pushes these into the live queue as
    `role='user'` content, and ADK persists them authored by the user.
    """
    return Event(author="user", content=types.Content(
        role="user", parts=[types.Part(text=text)]))


async def replayed(main, store: Path, events: list) -> tuple:
    """Seed one thread, let a page arm over it, and read what went up.

    Answers `(turn_complete, roles, texts)` for the one `send_client_content`
    the replay is: the flag the Live API reads as "your turn", the role on
    every turn replayed, and their text.
    """
    main.session_service = type(main.session_service)(db_path=str(store))
    main.runner.session_service = main.session_service
    thread = await main.session_service.create_session(
        app_name=main.APP_NAME, user_id="alex", state={})
    for event in events:
        await main.session_service.append_event(thread, event)

    with recorded() as wires:
        await main.websocket_endpoint(ArmingSocket(), "alex")

    frames = [frame for wire in wires for frame in wire.sent]
    if not frames:
        return (None, [], [])
    turns, complete = frames[0]
    return (complete,
            [turn.role for turn in turns],
            [(turn.parts[0].text or "") if turn.parts else ""
             for turn in turns])


async def rows(main, session_id: str) -> int:
    """How many events that thread holds now."""
    got = await main.session_service.get_session(
        app_name=main.APP_NAME, user_id="alex", session_id=session_id)
    return len(got.events) if got else 0


async def resume_cases() -> None:
    print("\n== what the next arm asks a resumed thread's model to do ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    service, runner = main.session_service, main.runner
    keep = runner.session_service
    with tempfile.TemporaryDirectory() as tmp:
        try:
            complete, roles, said = await replayed(
                main, Path(tmp) / "a.db",
                [heard("what is the weather"), replied("it is sunny")])
            check("a thread that ended on the agent is replayed as history",
                  [complete, roles], [False, ["user", "model"]],
                  "turn_complete False is 'here is the conversation'")

            complete, roles, said = await replayed(
                main, Path(tmp) / "b.db",
                [heard("what is the weather"), replied("it is sunny"),
                 heard("and how about tomorrow")])
            check("a question the terminal never answered is not asked again "
                  "on the next arm", complete, False,
                  "True here is the model answering with nobody in the room")
            check("and the replay ends on the agent, which is what makes it "
                  "history", roles[-1], "model")
            check("the question itself is still replayed, so a follow-up "
                  "still has it",
                  any("how about tomorrow" in text for text in said), True)

            complete, roles, said = await replayed(
                main, Path(tmp) / "c.db",
                [heard("what is on my calendar"),
                 called("ask_workspace"),
                 came_back("ask_workspace", "two meetings")])
            check("a tool result the agent never read out is not answered "
                  "either", [complete, roles[-1]], [False, "model"],
                  "a function response is role='user' on the wire")

            complete, roles, said = await replayed(
                main, Path(tmp) / "d.db",
                [heard("plan dinner"), called("plan"),
                 came_back("plan", "still working"),
                 note("[terminal] Still working on it.")])
            check("nor is a progress note the server injected itself",
                  [complete, roles[-1]], [False, "model"])

            complete, roles, said = await replayed(
                main, Path(tmp) / "e.db",
                [typed("book a table"), replied("which night?"),
                 typed("thursday, at eight")])
            check("nor a question somebody typed and walked away from",
                  [complete, roles[-1]], [False, "model"],
                  "the text box leaves a user turn with no transcription on "
                  "it")

            complete, roles, said = await replayed(
                main, Path(tmp) / "f.db",
                [heard("what is the weather"), replied("it is sunny"),
                 heard("and how about tomorrow"), bookkeeping()])
            check("a state write landing after the question does not hide it",
                  [complete, roles[-1]], [False, "model"],
                  "the handshake and the recap both write one, and neither is "
                  "a turn")

            complete, roles, said = await replayed(
                main, Path(tmp) / "g.db", [])
            check("a thread with nothing in it replays nothing at all",
                  [complete, roles], [None, []],
                  "and so cannot ask the model anything")

            # Counted on the thread the arm resumed, which is the newest one
            # `_thread` found and the only one in this store.
            main.session_service = type(service)(
                db_path=str(Path(tmp) / "h.db"))
            main.runner.session_service = main.session_service
            settled = [heard("what is the weather"), replied("it is sunny")]
            thread = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="alex", state={})
            for event in settled:
                await main.session_service.append_event(thread, event)
            before = await rows(main, thread.id)
            ws = ArmingSocket()
            with recorded():
                await main.websocket_endpoint(ws, "alex")
            check("a thread that settled on its own has nothing written to it",
                  await rows(main, thread.id) - before, 0,
                  "an arm that always writes grows the thread on every wake "
                  "word")
            history_frames = [json.loads(s) for s in ws.sent
                              if json.loads(s).get("type") == "history"]
            check("an established session emits a history frame on connect",
                  len(history_frames), 1,
                  "reloading mid-conversation must restore the settled turns")
            check("and carries the settled turns in order",
                  history_frames[0].get("turns") if history_frames else [],
                  [{"role": "user", "text": "what is the weather"},
                   {"role": "agent", "text": "it is sunny"}])
            check("feed_turns drops partial transcriptions, notes, and tool calls",
                  main.feed_turns([
                      heard("first question"),
                      heard("partial text", partial=True),
                      replied("partial answer", partial=True),
                      called("search"),
                      came_back("search", "result"),
                      note("[terminal] Progress note"),
                      replied("first answer"),
                      bookkeeping(),
                  ]),
                  [{"role": "user", "text": "first question"},
                   {"role": "agent", "text": "first answer"}])

            # `_settle` runs after `rethread` and not before it, and this is
            # the case that holds the order. The settle write stamps
            # `sessions.update_time`, which is the one column `rethread`
            # measures the quiet against, so settling first moves the thread's
            # age to zero and the roll never fires. The wake word then replays
            # a thread `IDLE_GAP_S` had already retired -- #114 arrived at
            # from the other side, and the argument for the order is not
            # something the reader should have to reconstruct.
            store = Path(tmp) / "i.db"
            main.session_service = type(service)(db_path=str(store))
            main.runner.session_service = main.session_service
            stale = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="alex", state={})
            await main.session_service.append_event(
                stale, heard("who is fetching the parcel"))
            with recorded() as wires:
                await main.websocket_endpoint(
                    StaleArmingSocket(store, main.IDLE_GAP_S), "alex")
            listed = await main.session_service.list_sessions(
                app_name=main.APP_NAME, user_id="alex")
            check("a thread the gap has retired is rolled away before it is "
                  "settled",
                  [len(listed.sessions), await rows(main, stale.id)], [2, 1],
                  "settling first stamps the column the roll is measured on")
            check("so the arm does not replay the thread that was retired",
                  [[turn.role for turn in turns]
                   for wire in wires for turns, _ in wire.sent], [],
                  "a fresh thread has nothing to replay, and the old one is "
                  "not this connection's any more")

            # The marker itself, and not a copy of it. `recap_cases` pins the
            # shape of the event `_settle` writes, which is only worth
            # anything while the text the server writes is still one of these
            # — the two live in different files and nothing else holds them
            # together. #172.
            check("and the settle marker the server writes is one of those "
                  "notes",
                  recap.transcript([Event(
                      author=main.agent.name,
                      content=types.Content(role="model", parts=[
                          types.Part(text=main.UNANSWERED)]))]),
                  "", "the transcript is where a recap of it would come from")
        finally:
            main.session_service = service
            main.runner = runner
            main.runner.session_service = keep


# ------------------------- and what the screensaver coming on does to it

ARM = {"type": "arm"}
DISARM = {"type": "disarm"}
DARK = {"type": "sleep"}


async def after(main, store: Path, events: list, frames: list) -> dict:
    """Seed one thread, run one socket over it, and read what is left.

    Three numbers, because the fix has to hold all three at once: how much the
    last arm replayed, how many threads the store holds now, and how many
    events the seeded one still has. The third is the one that says a roll is
    not a delete.
    """
    main.session_service = type(main.session_service)(db_path=str(store))
    main.runner.session_service = main.session_service
    thread = await main.session_service.create_session(
        app_name=main.APP_NAME, user_id="alex", state={})
    for event in events:
        await main.session_service.append_event(thread, event)

    with recorded() as wires:
        await main.websocket_endpoint(ArmingSocket(frames), "alex")

    # The last one, because a run with several arms in it cancels the earlier
    # ones before their replay lands — only the arm the hold sits behind gets
    # that far, and it is the one every case here is about.
    up = [len(turns) for wire in wires for turns, _ in wire.sent]
    listed = await main.session_service.list_sessions(
        app_name=main.APP_NAME, user_id="alex")
    return {"replayed": up[-1] if up else 0,
            "threads": len(listed.sessions),
            "kept": await rows(main, thread.id)}


async def dark_cases() -> None:
    print("\n== and what the screen going back to a clock does to it ==")

    from ai_terminal.live import main
    logging.getLogger().setLevel(logging.WARNING)

    service, runner = main.session_service, main.runner
    keep, retain = runner.session_service, main.RETAIN_S
    with tempfile.TemporaryDirectory() as tmp:
        room = Path(tmp)
        try:
            # Forty events, which is a short evening. Nothing about the fix
            # depends on the size, which is what the 640-event case below is
            # for.
            talk = []
            for i in range(20):
                talk.append(heard(f"question {i}"))
                talk.append(replied(f"answer {i}"))

            # A deliberate control, and the only case in this section that
            # holds both before the fix and after it: the recency rule is
            # unchanged for a page that never slept. A tab on a Mac, or a
            # kiosk somebody has been touching all afternoon, still gets the
            # thread `IDLE_GAP_S` says it gets.
            plain = await after(main, room / "a.db", talk, [ARM])
            check("a wake word on a page that never slept still replays the "
                  "whole thread", [plain["replayed"], plain["threads"]],
                  [40, 1], "the half hour is still the ceiling for that page")

            # The defect. Before this, the only bound on a replay was thirty
            # minutes of silence, and a kitchen in use never has thirty
            # minutes of silence — so the arm cost whatever the day had cost
            # so far, growing all day, with nothing in the log to say so.
            slept = await after(main, room / "b.db", talk, [DARK, ARM])
            check("but a screen that went back to being a clock takes the "
                  "conversation with it", slept["replayed"], 0,
                  "the next wake word starts one rather than continuing one")

            # Rolled away is not deleted, and this is the case that says so.
            # The events stay on the row they were written to and age out
            # under `RETAIN_S` like any other thread's; what the roll changes
            # is only which thread the next connection replays.
            check("and the conversation it left is still on disk, whole",
                  [slept["kept"], slept["threads"]], [40, 2],
                  "a bound that lost the transcript would be a worse bug "
                  "than the one it fixed")

            # The property rather than the number. A bound that still grew
            # with the thread would not be a bound, and this is the shape a
            # measured day had: 636 events over ten hours, never rolled.
            # Built rather than `talk * 16`: an `Event` mints its id once, and
            # the same object appended twice is a UNIQUE violation on the
            # store rather than a longer conversation.
            day = []
            for i in range(320):
                day.append(heard(f"question {i}"))
                day.append(replied(f"answer {i}"))
            big = await after(main, room / "c.db", day, [DARK, ARM])
            check("and the length of the day stops mattering to a wake word",
                  [big["replayed"], big["kept"]], [0, 640],
                  "640 stored and none replayed")

            # The mark is a flag, not a counter. A screen that wakes on a
            # touch and goes dark again without anybody speaking must not
            # leave two conversations behind it.
            twice = await after(main, room / "d.db", talk, [DARK, DARK, ARM])
            check("two screensavers and one wake word make one new "
                  "conversation, not two", twice["threads"], 2)

            # And the roll waits for the wake word rather than riding the
            # screensaver, which is the whole reason it is a mark. A hallway
            # kiosk nobody speaks to dims every three minutes; rolling there
            # would mint a session row each time, and both queries on the
            # path of every page load are linear in how many rows there are.
            quiet = await after(main, room / "e.db", talk, [DARK])
            check("a screen that goes dark and stays dark writes nothing at "
                  "all", [quiet["threads"], quiet["kept"]], [1, 40],
                  "the roll is worth something only where somebody speaks")

            # The mark is cleared by the roll it caused, so a second wake word
            # on the same conversation is the same conversation.
            again = await after(main, room / "f.db", talk, [ARM, DISARM, ARM])
            check("and a second wake word with no screensaver between them "
                  "does not roll", again["threads"], 1)

            # The same thing after a screensaver, which is where forgetting to
            # clear the mark actually costs something: the roll happens, and
            # then the next wake word rolls away the conversation that just
            # started on it. Somebody says hello, asks one question, pauses
            # under thirty seconds so `WAKE_IDLE_S` shuts the microphone, says
            # hello again — and with the mark left standing the follow-up is
            # answered by a model that never heard the question.
            stuck = await after(main, room / "h.db", talk,
                                [DARK, ARM, DISARM, ARM])
            check("and the mark is spent by the roll it caused, not left "
                  "standing", [stuck["threads"], stuck["replayed"]], [2, 0],
                  "two threads and not three: one screensaver, one roll")

            # `sleep` in `app.js` bails out to `wake()` on a live microphone,
            # a card still spinning or a browse in flight, so this frame
            # should never arrive over an open session. If the two ever
            # disagree, re-keying the four registries under a running
            # `downstream` task sends its cards to a session with no socket
            # behind it. Refused instead, and it heals: the page's countdown
            # was restarted by that same `wake()` and the next one lands with
            # nothing armed.
            live = await after(main, room / "g.db", talk,
                               [ARM, DARK, DISARM, ARM])
            check("a screensaver frame arriving over a live session is "
                  "refused rather than rolling underneath it",
                  [live["threads"], live["replayed"]], [1, 40],
                  "one missed roll, and the page will say it again")

            # And the two-page version of the same thing, which #159 settled
            # for the gap and this rule reopens: the mark belongs to the page
            # that went dark, and the Live session it would roll away belongs
            # to another page on the same thread. A kiosk that dims while
            # somebody is talking to the Mac in the next room is exactly that.
            # The roll is refused and the mark kept, so the wake word that
            # follows the other page finishing still gets a new conversation.
            store = room / "i.db"
            main.session_service = type(main.session_service)(db_path=str(
                store))
            main.runner.session_service = main.session_service
            thread = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="alex", state={})
            for event in talk:
                await main.session_service.append_event(thread, event)
            with recorded() as wires:
                # The other page, armed and held open for the whole of the
                # first page's run. `ArmingSocket`'s hold is what keeps its
                # `run_live` alive; the sleep below is only long enough for
                # the arm to have reached the model.
                other = asyncio.create_task(main.websocket_endpoint(
                    ArmingSocket([ARM], hold=2.5), "alex"))
                await asyncio.sleep(0.8)
                await main.websocket_endpoint(
                    ArmingSocket([DARK, ARM], hold=0.4), "alex")
                shared = await main.session_service.list_sessions(
                    app_name=main.APP_NAME, user_id="alex")
                await other
            up = [len(turns) for wire in wires for turns, _ in wire.sent]
            check("a screensaver on one page does not roll the thread the "
                  "other page is talking on",
                  [len(shared.sessions), up[-1]], [1, 40],
                  "#159's rule, and the mark is kept rather than spent")

            # And the other two-page shape, where the roll happens and the
            # page that did not ask for it is carried across. A dims and
            # marks; B's own wake word rolls the profile, and the fan-out puts
            # A on the fresh thread. The mark A is still holding was taken on
            # a conversation nobody is on any more, so reading it at A's wake
            # word rolls away the one B has just started — a third thread, and
            # B's two lines stranded on the second.
            store = room / "j.db"
            main.session_service = type(main.session_service)(db_path=str(
                store))
            main.runner.session_service = main.session_service
            thread = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="alex", state={})
            for event in talk:
                await main.session_service.append_event(thread, event)
            woken = asyncio.Event()
            with recorded() as wires:
                dimmed = asyncio.create_task(main.websocket_endpoint(
                    GatedArmingSocket([DARK, ARM], woken), "alex"))
                await asyncio.sleep(0.3)
                # B, on the same profile and so on the same thread: it dims,
                # its wake word rolls, and A goes across with it.
                await main.websocket_endpoint(
                    ArmingSocket([DARK, ARM], hold=0.4), "alex")
                # What B then says. Appended rather than spoken, because the
                # recorded upstream never answers and the point of the case is
                # what A's arm finds on the thread B left it on.
                listed = await main.session_service.list_sessions(
                    app_name=main.APP_NAME, user_id="alex")
                fresh = await main.session_service.get_session(
                    app_name=main.APP_NAME, user_id="alex",
                    session_id=next(s.id for s in listed.sessions
                                    if s.id != thread.id))
                for event in [heard("and tomorrow?"), replied("rain")]:
                    await main.session_service.append_event(fresh, event)
                woken.set()
                await dimmed
            after_roll = await main.session_service.list_sessions(
                app_name=main.APP_NAME, user_id="alex")
            up = [len(turns) for wire in wires for turns, _ in wire.sent]
            check("a mark does not survive the thread it was taken on",
                  [len(after_roll.sessions), up[-1] if up else 0], [2, 2],
                  "two threads and not three, and A joins what B started")

            # Where the roll and the retention window meet, which is the one
            # place nothing was watching. The sweep used to live in `_thread`
            # alone, so it ran at accept and nowhere else — and the kiosk is
            # the page that never accepts twice. It reconnects on a deploy, a
            # restart or a reboot, so the window was applied eventually, but
            # only because something forced a reconnect; on a socket left up,
            # every thread the wake word rolled away stayed on disk however
            # old it got. Two rolls on one connection is the smallest shape
            # that shows it: the first leaves a thread behind, the window
            # passes over it, and the second is the sweep that should take it.
            # #173.
            store = room / "k.db"
            main.session_service = type(main.session_service)(db_path=str(
                store))
            main.runner.session_service = main.session_service
            thread = await main.session_service.create_session(
                app_name=main.APP_NAME, user_id="alex", state={})
            for event in talk:
                await main.session_service.append_event(thread, event)
            # Short enough to pass while the page holds its last frame, and
            # the hold is what makes that an order rather than a race. The
            # thread seeded above is the only row at accept and the newest, so
            # the sweep there leaves it alone whatever this is set to.
            main.RETAIN_S = 0.15
            woken = asyncio.Event()
            with recorded():
                rolling = asyncio.create_task(main.websocket_endpoint(
                    GatedArmingSocket([DARK, ARM, DISARM, DARK, ARM], woken),
                    "alex"))
                await asyncio.sleep(0.4)
                woken.set()
                await rolling
            swept = await main.session_service.list_sessions(
                app_name=main.APP_NAME, user_id="alex")
            check("a thread rolled away on a socket that never reconnects "
                  "still ages out",
                  [len(swept.sessions), bool(
                      await main.session_service.get_session(
                          app_name=main.APP_NAME, user_id="alex",
                          session_id=thread.id))], [2, False],
                  "otherwise the retention window applies only on the days "
                  "somebody deploys")

            # When the page connects while asleep, it notifies the server immediately
            # with a sleep frame so the server marks dark_at (#448).
            src = (Path(__file__).resolve().parent.parent /
                   "src/ai_terminal/live/static/js/app.js").read_text(encoding="utf-8")
            opened = src[src.find("socket.onopen = () => {"):src.find("checkRevision();")]
            check("socket.onopen reports sleep to server when asleep (#448)",
                  'socket.send(JSON.stringify({ type: "sleep" }))' in opened,
                  True,
                  "reconnect while asleep must tell server the screen went dark")
        finally:
            main.RETAIN_S = retain
            main.session_service = service
            main.runner = runner
            main.runner.session_service = keep


async def wake_cases() -> None:
    """Which screen `/api/wake` opens a microphone on, when there are two.

    `sink` answers "the newest page", which is right for a card — drawn to
    both, a card is drawn twice through a reload — and wrong for a wake frame,
    which is not a thing to draw at all. It opens a microphone, so it belongs
    to the screen in the room where the word was said. See #132: with a tab
    open on the Mac, the word said at the kiosk in the kitchen opened the
    microphone on the Mac and nothing on either screen said why.

    Still one screen per session, though. Widening it to the whole stack
    replaces one microphone in the wrong room with two, one of which is right.
    """
    print("\n== which screen a wake word reaches ==")
    sid = "s-wake"

    async def kiosk(frame):
        pass

    async def laptop(frame):
        pass

    async def reloaded(frame):
        pass

    screen.register(sid, kiosk)
    screen.register(sid, laptop)
    check("with no kiosk among them the newest is still the one, which is "
          "what a lone developer on the Mac has always had",
          wake_screen(sid) is laptop, True)
    screen.unregister(sid)

    # The failure the issue was filed for. The kiosk has been up all day; a
    # tab opens on the Mac to look at the panel and takes `stack[-1]`.
    a_kiosk(sid, kiosk)
    screen.register(sid, laptop)
    check("a page that says it is the kiosk gets the wake word even with a "
          "newer page in front of it",
          wake_screen(sid) is kiosk, True,
          "otherwise the word said in the kitchen opens the Mac's microphone")
    check("while the card still goes to the newest, which #127 settled",
          screen.sink(sid) is laptop, True)

    # A kiosk reload is two kiosks on the stack for as long as the old
    # connection's `finally` takes to run, and the frame belongs to the one
    # that is up.
    a_kiosk(sid, reloaded)
    check("the kiosk reloading hands the wake word to the new connection",
          wake_screen(sid) is reloaded, True)
    screen.unregister(sid, kiosk)
    check("and the old one leaving afterwards changes nothing",
          wake_screen(sid) is reloaded, True)

    screen.unregister(sid, reloaded)
    check("with the kiosk gone it falls back to the page that is left, "
          "rather than to nobody",
          wake_screen(sid) is laptop, True,
          "a wake word with a screen up should reach it")
    screen.unregister(sid, laptop)
    check("and only an empty session has no screen to wake",
          wake_screen(sid), None)

    class Same:
        """Two sinks that `==` cannot tell apart, which `is` can.

        The stack holds a pair now, so `list.remove` would compare the whole
        entry — and two kiosk connections differ in neither half by that test.
        #127 established identity here for exactly this reason and the flag
        must not quietly undo it.
        """

        def __eq__(self, other):
            return isinstance(other, Same)

        __hash__ = None

        async def __call__(self, frame):
            pass

    first, second = Same(), Same()
    a_kiosk(sid, first)
    a_kiosk(sid, second)
    screen.unregister(sid, second)
    check("unregistering one kiosk takes that connection's entry and no "
          "other, by identity and not by equality",
          wake_screen(sid) is first, True,
          "`list.remove` would drop the wrong one and leave a dead socket")
    screen.unregister(sid, first)
    check("and the session is empty once both have gone",
          wake_screen(sid), None)

    # Through the endpoint now, because `{"armed": N}` is the only thing the
    # monitor on the Pi ever sees and `main.py:1074-1079` says it counts
    # screens.
    from ai_terminal.live import main  # noqa: E402  pylint: disable=C0415
    from starlette.requests import Request  # noqa: E402

    def knock() -> Request:
        """A `POST /api/wake` the guard will let through, and nothing else."""
        return Request({"type": "http", "method": "POST", "path": "/api/wake",
                        "headers": [(b"content-type", b"application/json")],
                        "query_string": b""})

    reached: dict[str, int] = {}

    def counting(name: str):
        async def send(frame):
            reached[name] = reached.get(name, 0) + 1
        return send

    kitchen, mac, phone = (counting("kitchen"), counting("mac"),
                           counting("phone"))
    a_kiosk("w-kitchen", kitchen)
    screen.register("w-kitchen", mac)
    screen.register("w-phone", phone)
    try:
        check("two sessions, one of them with two pages open, is two screens "
              "woken and not three",
              [await main.api_wake(knock()), reached],
              [{"armed": 2}, {"kitchen": 1, "phone": 1}],
              "the count is screens told, and the Mac's tab is not one")
    finally:
        screen.unregister("w-kitchen")
        screen.unregister("w-phone")

    # The other half of counting rather than measuring: `sessions()` is a
    # snapshot, and its own docstring says a push can end with a connection
    # tearing itself down. Taken off the list in front of the loop, the number
    # counts a screen the loop then found nothing to write to.
    reached.clear()
    going = counting("going")

    async def takes_the_other_down(frame):
        screen.unregister("w-going")
        reached["closer"] = reached.get("closer", 0) + 1

    screen.register("w-closer", takes_the_other_down)
    screen.register("w-going", going)
    try:
        check("a session that loses its screen mid-loop is not counted as a "
              "screen the frame was handed to",
              [await main.api_wake(knock()), reached],
              [{"armed": 1}, {"closer": 1}],
              "`{armed: 2}` here is a microphone the monitor thinks it opened")
    finally:
        screen.unregister("w-closer")
        screen.unregister("w-going")


async def main() -> None:
    await session_roll_replay_cases()
    await recap_cases()
    await thread_cases()
    await forget_cases()
    await journal_cases()
    await memory_cases()
    await handover_cases()
    await resume_cases()
    await dark_cases()
    await wake_cases()
    await entity_resolution_behavioural_cases()
    print(f"\nall {RAN} cases hold")



async def session_roll_replay_cases() -> None:
    from ai_terminal.live import main

    print("\n== session roll and tool call replay (#396) ==")
    db_path = Path("/tmp/test-roll-replay.db")
    if db_path.exists():
        db_path.unlink()
    svc = main.TerminalSessionService(db_path=str(db_path))
    try:
        sess = await svc.create_session(
            app_name=main.APP_NAME, user_id="u1", session_id="s1")
        ev = Event(
            author="ai_terminal",
            live_session_id="live-1234",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="call-396",
                            name="gws",
                            args={"request": "test query"},
                        )
                    )
                ],
            ),
        )
        saved = await svc.append_event(sess, ev)
        check("append_event strips live_session_id",
              saved.live_session_id, None,
              "live_session_id left set causes ADK _fencing.py:100 to "
              "misclassify turn on replay (#396)")

        reloaded = await svc.get_session(
            app_name=main.APP_NAME, user_id="u1", session_id="s1")
        check("get_session yields event with no live_session_id",
              reloaded.events[0].live_session_id, None)

        from google.adk.flows.llm_flows import contents
        resp_ev = Event(
            author="user",
            content=types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="call-396",
                            name="gws",
                            response={"result": "found answer"},
                        )
                    )
                ],
            ),
        )
        await svc.append_event(sess, resp_ev)
        reloaded2 = await svc.get_session(
            app_name=main.APP_NAME, user_id="u1", session_id="s1")

        contents_res = contents._get_contents(None, reloaded2.events, main.agent.name)
        has_fc = any(p.function_call is not None for c in contents_res for p in c.parts)
        has_fr = any(p.function_response is not None for c in contents_res for p in c.parts)
        check("ADK preserves function call on replay across session roll", has_fc, True)
        check("ADK preserves function response without orphan drop (#396)", has_fr, True)
    finally:
        if db_path.exists():
            db_path.unlink()


async def entity_resolution_behavioural_cases() -> None:
    from ai_terminal.live import main

    print("\n== sequential entity resolution behavioural history retention (#468) ==")
    # #446 added prompt instructions for resolving demonstratives like "that place"
    # or "そのお店" to the primary entity in recent turns. This behavioural check
    # asserts that multi-turn conversation structures actually preserve sequential
    # referential turns and entity context across history payloads and replay
    # rather than merely checking prompt regex in source text.
    with tempfile.TemporaryDirectory() as tmp:
        spoken_events = [
            heard("Recommend some ramen shops in Kyoto"),
            replied("You could visit Menya Inoichi, Honke DaiichiAsahi, or Gion Duck Noodles."),
            heard("What are the opening hours of that place?"),
            replied("Menya Inoichi opens from 11:00 AM to 2:30 PM and 5:30 PM to 9:00 PM."),
            called("place_info"),
            came_back("place_info", "Menya Inoichi: 15 mins by bus"),
            note("[terminal] Looking up directions"),
            heard("そのお店は京都駅から歩けますか？"),
            replied("はい、Menya Inoichiは京都駅から徒歩約15分です。"),
        ]

        # 1. feed_turns strips intermediate non-speech events while preserving sequential referential turns
        turns = main.feed_turns(spoken_events)
        check("feed_turns preserves sequential multi-turn dialogue count",
              len(turns), 6, "drops tool call, tool response, and progress note")
        check("initial turn contains candidate entity list",
              "Menya Inoichi" in turns[1]["text"] and "Honke DaiichiAsahi" in turns[1]["text"],
              True)
        check("second turn carries referential demonstrative 'that place'",
              "that place" in turns[2]["text"], True)
        check("second response resolves to primary entity Menya Inoichi",
              "Menya Inoichi" in turns[3]["text"], True)
        check("third turn carries sequential demonstrative 'そのお店'",
              "そのお店" in turns[4]["text"], True)
        check("third response preserves entity context",
              "Menya Inoichi" in turns[5]["text"], True)

        # 2. Live API replay (send_client_content) preserves sequential turns for model context
        clean_speech = [e for e in spoken_events if e not in (spoken_events[4], spoken_events[5], spoken_events[6])]
        complete, roles, texts = await replayed(main, Path(tmp) / "ref_replay.db", clean_speech)
        check("replayed multi-turn conversation ends with turn_complete False", complete, False)
        check("replayed roles alternate between user and model across all 3 turns",
              roles, ["user", "model", "user", "model", "user", "model"])
        check("replayed turns preserve entity candidate in turn 1",
              "Menya Inoichi" in texts[1], True)
        check("replayed turns preserve referential query in turn 2",
              "that place" in texts[2], True)
        check("replayed turns preserve sequential demonstrative in turn 3",
              "そのお店" in texts[4], True)

        # 3. Established session emits history frame preserving multi-turn referential context on reconnect
        db_path = Path(tmp) / "ref_ws.db"
        main.session_service = type(main.session_service)(db_path=str(db_path))
        main.runner.session_service = main.session_service
        thread = await main.session_service.create_session(
            app_name=main.APP_NAME, user_id="alex", state={})
        for ev in spoken_events:
            await main.session_service.append_event(thread, ev)

        ws = ArmingSocket()
        with recorded():
            await main.websocket_endpoint(ws, "alex")
        history_frames = [json.loads(s) for s in ws.sent if json.loads(s).get("type") == "history"]
        check("reconnect emits history frame for multi-turn session", len(history_frames), 1)
        emitted_turns = history_frames[0].get("turns", []) if history_frames else []
        check("history frame carries settled turns in order",
              len(emitted_turns), 6)
        check("history frame preserves entity context across turns",
              "Menya Inoichi" in emitted_turns[1]["text"] and "そのお店" in emitted_turns[4]["text"],
              True)


if __name__ == "__main__":
    asyncio.run(main())
