"""Reading a web page as an agent tool, answered out of band.

Every other tool answers inside the spoken turn that called it. This one takes
the better part of a minute — 42 s in the spike — and the Live API holds the
conversation open while a blocking tool runs.

So ``browse`` is declared NON_BLOCKING: ADK sees ``response_scheduling`` on the
tool, marks the declaration for the Live API, and runs the call in a background
task, feeding the FunctionResponse back into the live queue when it returns.
The tool itself is an ordinary coroutine that waits for the answer; nobody has
to hand-roll a second channel to the model.

What ADK does not carry is either half of the progress. The screenshots go on
the screen down the :mod:`screen` side channel; a minute of silence is worth
breaking too, so the same steps become the odd spoken note, which
:mod:`._progress` sends for itself.
"""

import asyncio
import contextlib
import logging
from urllib.parse import urlparse

from google.adk.tools import FunctionTool, ToolContext
from google.genai import types

from .. import browser, conn, screen
from .._log import _sized
from . import _progress
from ._lang import of_turn

logger = logging.getLogger(__name__)

# The browse jobs each live session has running, keyed by session id, then by
# connection, and then by task.
#
# By connection because a session id is one per profile and not one per socket
# — see :mod:`..conn`. Keyed on the id alone, the second page to open on the
# profile replaced the kiosk's entry and took it away again on the way out, and
# the kiosk was then told "No screen is connected" by the guard below for the
# rest of its life, on a terminal whose screen was connected.
#
# Keyed by task because the native-audio model repeats a function call
# two or three times inside one turn. Every other tool shrugs that off as a
# duplicate search; here each copy would be a second Chromium. The duplicates
# wait on the first job and all get its answer.
#
# By task and not by `(task, context)`, even though `context` reaches the model
# too. Those repeats are the same call retold, and the model rarely writes the
# paragraph the same way twice — keying on it would put the second Chromium
# back for a difference in wording. The cost of getting that wrong is the
# second caller reading an answer found under the first one's context, and the
# two are the same question a second apart.
_JOBS: dict[str, dict[str, dict[str, asyncio.Task]]] = {}


def open_session(session_id: str,
                 conn_id: str | None = None) -> dict[str, asyncio.Task]:
    """Start tracking this connection's jobs, and hand the entry back as a
    token.

    The entry itself is the token, so :func:`close_session` can tell "the
    connection I opened" from any other on the same id without a second
    dictionary to keep in step. `conn_id` is what a *later* lookup uses to find
    it again; omitted, the connection gets an anonymous id of its own rather
    than sharing one.
    """
    return conn.opened(_JOBS, session_id, {}, conn_id)


def jobs_of(session_id: str) -> dict[str, asyncio.Task] | None:
    """The jobs of the connection asking, or the newest on this session."""
    return conn.own(_JOBS, session_id)


def close_session(session_id: str,
                  jobs: dict[str, asyncio.Task] | None = None) -> None:
    """Cancel the jobs of the connection that is leaving.

    Its own jobs, always — they were started for a browser that has gone. Its
    own registry entry and no other: a reload can open the next connection on
    the same id before this one gets here, and two pages on one profile stay
    that way indefinitely. Either way the connection still standing keeps the
    entry it is being tracked under.
    """
    gone = conn.taken(_JOBS, session_id, jobs)
    for entry in (gone if jobs is None else [jobs]):
        for job in entry.values():
            job.cancel()


async def browse(task: str, tool_context: ToolContext, context: str = "",
                 lang: str = "") -> dict:
    """Read a web page that no other tool can reach, and report what it says.

    Use for anything that lives on a site rather than in an API: a timetable, a
    shop's own opening hours, a form that has to be filled in to see a result,
    a page web_search only summarised second-hand, or when explicitly asked to
    open, browse, or read a website or web page. Call browse directly when asked
    to open or browse a site rather than asking which website to open; prefer web_search when a search summary would do
    — this takes the better part of a minute.

    It reads; it does not act. It will not log in, buy, book, send or delete.

    Args:
        task: What to find out, in a sentence, naming the site if it matters —
            for example "on jorudan.co.jp, how long does the train from Ebisu
            to Kanazawa take?".
        context: A short paragraph — three or four sentences at most — on what
            the person is doing and what earlier turns already found out for
            it, e.g. "They are catching a flight from Komatsu tomorrow morning
            and have been working out the day backwards. An earlier turn found
            check-in closes 40 minutes before the 09:15 departure." A page
            offers several answers and the browser has no way to tell which one
            was wanted: without this it reads the first departure on the board.
            Write what they are doing, not what to answer.
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. The answer is written in it and shown on screen.

    Returns:
        What the page said, the address it was read from, and how long it took.
    """
    lang = of_turn(lang, tool_context)
    # The live session, which under `plan` is not the one this context is in —
    # see `screen.session_of`. Both things keyed on it want the live one: the
    # screen, and `_JOBS`, whose whole job is that one Chromium serves every
    # copy of a request whoever made it.
    session_id = screen.session_of(tool_context)
    # This connection's jobs where the socket has named itself, the newest
    # otherwise. Not `_JOBS.get(session_id)`: that answered with whichever
    # connection registered last, so a second page closing left the kiosk with
    # no entry at all and the refusal below became a lie.
    jobs = jobs_of(session_id)
    # Asked, not held. `screen.sink` answers "is anybody there" and nothing
    # else here: a run takes the better part of a minute, and the page that was
    # newest when it started is not the set of pages that will be up when it
    # finishes. `watch` resolves the screens per frame instead — see below.
    if jobs is None or screen.sink(session_id) is None:
        return {"error": "No screen is connected, so the browser cannot run."}

    # Not on screen: the browse card is showing a screenshot of the page and
    # the step it is on, which says more than a note would.
    notes = _progress.Notes(tool_context, "browse", on_screen=False)

    async def watch(frame: dict) -> None:
        # Stamped with the call id, which is what tells two browse jobs apart
        # on screen. `browser.run` cannot do it: it does not know which caller
        # it is running for, and under `plan` there can be two — the planner's
        # own step and whatever the live agent started while it was still
        # thinking, since `plan` is NON_BLOCKING and the person kept talking.
        # Without the id both push into one card, which then alternates the two
        # jobs' screenshots and closes on whichever finishes first.
        #
        # Only the call that starts the job passes this as `sink`, so a
        # duplicate's `watch` is never called and a duplicate gets no card.
        # That is the intended shape — one browser, one card — and it is why
        # anything sent from outside `browser.run` has to ask whether it is the
        # owner first. There is no such thing here as "each caller's own card".
        #
        # Through `screen.push` and not a sink captured before the browser
        # started: a job runs for a minute, and over that minute a page can
        # arrive, leave, or be joined by a second one on the same profile.
        # Resolved per frame, every page attached at that moment gets the
        # screenshot; captured once, the frames keep going to whichever socket
        # happened to be newest at call time, including after it has closed.
        await screen.push(session_id,
                          {**frame, "id": tool_context.function_call_id})
        if frame.get("phase") == "step":
            notes.note("page", f"Reading {_host(frame.get('url', ''))}.")

    job = jobs.get(task)
    mine = job is None
    ended_sent = False

    async def ended() -> None:
        """End the card, from the tool rather than from the run.

        The card's only terminal frame is the `done` on the last line of the
        happy path, so without this one a job that did not get there leaves its
        spinner running for the rest of the session — on a screen meant to be
        left up all day, saying a page is still being read while the voice has
        already said it could not be. And a browse card has no deadline to fall
        back on: `app.js`'s `LATE_TOOLS` gives `gws` and `plan` a number and
        can give this one none, because a run may legitimately overrun
        `BROWSE_BUDGET_S` by a whole step.

        Thin on purpose. No `shot`, so the last screenshot stays; an empty
        `url`, which `host("")` renders as nothing; and an empty `answer`,
        which the card turns into its own bilingual label rather than take a
        sentence of English from here.

        From the owner only. A duplicate call has no card — its `watch` was
        never handed to the browser — so its frame would arrive under an id the
        client has never seen and draw a second, screenshotless card saying
        only that the page could not be read. Three duplicates and a crash used
        to mean the right card plus two of those.

        Suppressed, though `watch` no longer reaches `websocket.send_text`
        unguarded — `screen.push` swallows a socket that has gone. What the
        `suppress` still covers is this being the one call site inside an
        `except`: anything raising here, from `push` or from the frame it is
        handed, propagates out of `browse` instead of the error below and the
        call's FunctionResponse never arrives. A screen that has gone away is
        not a reason to lose the model's answer.

        Note what routing `watch` through `screen.push` gave up: a send that
        raised used to travel out of `watch`, out of `browser.run` and into
        `read`'s `except` below, ending the job. That was an accident of one
        client. With two attached, one socket dying must not cancel a browse
        the other one is watching, and "a closed WebSocket is not a reason to
        fail the tool call" is already the rule everywhere else.

        Shielded, because a second cancellation would otherwise take the frame
        with it. Every caller is inside an `except CancelledError` already, and
        a real send awaits a lock before `send_text` (`main.py`), so there is a
        point to be cancelled at: the runner tearing the tool task down behind
        `close_session` is exactly that, and `suppress(Exception)` cannot catch
        it — `CancelledError` is a `BaseException`. The suppression lives
        *inside* the shielded coroutine so the task it detaches can never end
        holding an exception nobody retrieves.
        """
        nonlocal ended_sent
        if not mine or ended_sent:
            return
        ended_sent = True

        async def send_done() -> None:
            with contextlib.suppress(Exception):
                await watch({"type": "browse", "phase": "done", "url": "",
                             "answer": ""})

        await asyncio.shield(send_done())

    async def read() -> dict:
        """The job, owning the frame that says it is over.

        `browser.run` ends the card on its return path only, so a crash
        leaves the spinner up, and the caller cannot cover it: under
        `plan`'s timeout the call is gone and the job is still here.
        """
        try:
            return await browser.run(task, lang, context=context, sink=watch)
        except asyncio.CancelledError:
            await ended()
            raise
        except Exception:
            await ended()
            raise

    if job is None:
        job = notes.start(read())
        jobs[task] = job
        job.add_done_callback(lambda _: jobs.pop(task, None))
        # Only for the call that started the job. A duplicate rides the same
        # browser and would have the agent announce the same page twice.
        notes.opening()

    # Its own `try`, and only for the cancellation. `notes.watch` runs for the
    # whole life of the job — it returns when the job is done — so a socket
    # that drops mid-browse cancels *here*, and an `except` around the shield
    # below would see one only in the sliver between the two awaits. Nothing
    # else is caught: a progress panel that raised is not a browser that
    # failed, and reporting it as one would throw away an answer the browser
    # may well have.
    try:
        await notes.watch(job)
    except asyncio.CancelledError:
        # Only when the job is going down with the call. A cancel that reaches
        # `browse` does not always reach the browser: `_JOBS` is keyed on the
        # live session, so `plan.py`'s 250 s timeout cancels the planner's call
        # and leaves the job running in the live session's registry, where it
        # goes on to send its own `done` at the end of `browser.run`. A frame
        # from here as well draws a "could not read the page" card, and the
        # answer then arrives beside it in a second card — `handleBrowse` gives
        # up the id on the first `done`. A dropped socket is the other shape:
        # `main.py`'s `close_session` cancels the job first, so by the time
        # this runs the job is cancelling and the card is ours to end.
        if job.done() or job.cancelling():
            await ended()
        raise

    try:
        result = await asyncio.shield(job)
    except asyncio.CancelledError:
        # The same story arriving in the narrow window: cancelled after the
        # notes stopped and before the answer was handed over. Usually the
        # socket dying, in which case this send fails and the card goes with
        # the page; when it is anything else, this is what keeps the card from
        # spinning until the screen is reloaded. Guarded for the same reason as
        # above — a job that outlives the call ends its own card.
        if job.done() or job.cancelling():
            await ended()
        raise
    except Exception as err:  # a browser crash must not take the session down
        logger.exception("browse failed")
        return {"error": f"The browser could not read that: {err}"}

    # The size and not the first eighty characters. The words come off the
    # page rather than out of the household's mouth, but they were selected
    # to answer the question and the question is often recoverable from them
    # — and this line is INFO on the happy path, so it fires every browse.
    # See #235.
    logger.info("browse done in %ss: %s", result["seconds"],
                _sized(result["answer"]))
    return {
        "answer": result["answer"],
        "url": result["url"],
        "seconds": result["seconds"],
    }


def _host(url: str) -> str:
    """The site, for a note that has to be short enough to say out loud."""
    return urlparse(url).hostname or "the page"


browse_tool = FunctionTool(browse)
# Answering late is the whole point, and only a NON_BLOCKING declaration is
# allowed to. WHEN_IDLE rather than INTERRUPT: a minute is long enough that the
# person may well have started saying something else, and the timetable they
# asked for is not worth talking over them for.
browse_tool.response_scheduling = types.FunctionResponseScheduling.WHEN_IDLE
