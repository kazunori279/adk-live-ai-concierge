"""FastAPI server for the live voice agent.

Bridges a browser WebSocket to the ADK Live API: PCM audio up, model audio and
events down. The browser is the one on the Pi's own touch display, so the
frontend it serves is laid out for a 1280x800 landscape screen.
"""

import argparse
import asyncio
import base64
import contextlib
import hashlib
import html
import json
import logging
import math
import os
import subprocess
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import urlsplit

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env", override=True)

# pylint: disable=wrong-import-position
from fastapi import (  # noqa: E402
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from google.adk.agents.live_request_queue import LiveRequestQueue  # noqa: E402
from google.adk.agents.run_config import RunConfig, StreamingMode  # noqa: E402
from google.adk.errors.session_not_found_error import (  # noqa: E402
    SessionNotFoundError,
)
from google.adk.events import Event, EventActions  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
# Same reason as the SQLite import below: `google.adk.sessions` does not
# re-export this one either, so it is imported from the module that defines it.
from google.adk.sessions.base_session_service import (  # noqa: E402
    GetSessionConfig,
)
# Not `from google.adk.sessions import ...`: ADK 2.8.0 ships this class but
# leaves it out of the package's `__all__` and its lazy `__getattr__`, so the
# short import raises `AttributeError` and reads like "this ADK has no SQLite
# backend". The other one, `DatabaseSessionService`, wants the `db` extra and
# SQLAlchemy; this one runs on `aiosqlite`, which google-adk already depends on.
from google.adk.sessions.session import Session  # noqa: E402
from google.adk.sessions.sqlite_session_service import (  # noqa: E402
    SqliteSessionService,
)
from google.genai import types  # noqa: E402
# From starlette rather than `fastapi.middleware.trustedhost`, which is a
# one-line re-export of this same class. Every line number cited around
# `_allowed_hosts` below is in starlette's file, and a reader who follows one
# should land in the file it names.
from starlette.middleware.trustedhost import (  # noqa: E402
    TrustedHostMiddleware,
)

from . import conn, memory, recap, screen, screensaver  # noqa: E402
# Re-exported rather than kept here: `plan.py` needs `_sized` too and cannot
# import this module. `history_check.py` reads both off `main`, which is where
# a reader of `_log_tool_traffic` will look for them.
from ._log import _PLAIN_ARGS, _sized  # noqa: E402,F401
from .agent import build_agent  # noqa: E402
from .tools import _client, _gws_cli, _plan_run, _sdm, browse, close_home_toolset, gws, gws_cache, plan  # noqa: E402
from .tools._lang import normalize  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

# ADK logs every audio blob at DEBUG, which drowns out everything else.
for _noisy in (
    "google_adk.google.adk.flows.llm_flows.audio_cache_manager",
    "google_adk.google.adk.flows.llm_flows.base_llm_flow",
    "google_adk.google.adk.models.gemini_llm_connection",
    "websockets.client",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

APP_NAME = "raspi-ai-terminal"
STATIC_DIR = Path(__file__).parent / "static"

# Said once, at startup, rather than found out one thin card at a time. Every
# grounding branch runs inside an `except Exception`, so an unset variable is a
# `KeyError` swallowed into an empty result: the card comes back missing its
# photos and says nothing about why. `place.py:54` does log a traceback per
# branch, so the answer is findable — but only by someone who already suspects
# the credentials and goes looking. See `tools/_client.py`.
if _missing := _client.check_env():
    logger.error("grounding will fail, missing: %s", ", ".join(_missing))
    logger.error("variables are read from %s; credentials from "
                 "`gcloud auth application-default login`", REPO_ROOT / ".env")

# Its own line because it is not this process's key and grounding does not
# want it: `api_config` below hands it to the browser. Unset, Maps JS never
# loads and the place card draws its text with a blank where the map goes —
# quiet in the same way, so it gets told the same way.
if _no_map := _client.check_browser_env():
    logger.error("place cards will draw without a map, missing: %s",
                 ", ".join(_no_map))

# The two tools that can answer this for themselves, asked here so that the
# machine says it once instead of one question at a time. `gws.py:880` does say
# it, but only to whoever asked. `browse` cannot say it at all: what it raises
# is a Playwright message telling the user to run `playwright install`, which
# on the Pi downloads nothing that will run.
#
# Split by level, because the two halves are not the same news. `gws` is absent
# on the shipped Pi on purpose and is not going on it, so at `error` a
# correctly installed terminal logs a red line at every boot for the rest of
# its life and the level stops meaning anything. `browse` is the opposite: on
# that same Pi it is meant to work, and an entry for it means `BROWSE_CHROMIUM`
# is unset or names nothing that runs. Both are still said out loud — a warning
# is quieter, not silent.
_no_tool = _client.check_tool_env()
if _absent := [tool for tool in _no_tool if tool.startswith("gws ")]:
    logger.warning("these tools will refuse work: %s", ", ".join(_absent))
if _broken := [tool for tool in _no_tool if not tool.startswith("gws ")]:
    logger.error("these tools will refuse work: %s", ", ".join(_broken))


_GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}


def _git(*args: str) -> str | None:
    """git in the tree this server was started from.

    stdout, or None for no git, no repo, and a git that said no. `REPO_ROOT`
    is read at call time rather than closed over, because `wiring_check`
    points it at a fixture repo to drive both callers below — a checkout whose
    HEAD does not move while the suite does.
    """
    try:
        done = subprocess.run(("git", "-C", str(REPO_ROOT), *args),
                              capture_output=True, text=True, timeout=5,
                              env=_git_env())
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _revision() -> str:
    """Which commit this server is running.

    Worth asking because the deploy is a file copy. Measured on 2026-09-09:
    the Pi's `~/ai-terminal` is not a checkout, and of the 66 files in it, 65
    match some commit and no single commit holds all 65 — the smallest set
    that explains them is three, and the sixty-sixth, `README.md`, matches no
    commit in the project's history at all. Nothing in a log line or an HTTP
    response could settle which of the three was in play.

    `AI_TERMINAL_REVISION` wins, because a copied tree has no `.git` to ask
    and the unit that started the server is the last thing that knew. Then the
    checkout, which is what serves this under the staging topology. Then
    `"unknown"`, which is what an unstamped copy honestly is and is worth more
    than a SHA that reads as though somebody checked.

    Not `git describe`: this directory sits in a workspace repo whose only tag
    is `speaker-support`, on a 2026-06-19 commit belonging to the doorbell
    project, so describe answers `speaker-support-519-gc71311d` and buries the
    part anyone wanted. The dirty check carries a pathspec for a related
    reason — thirty other projects share the repo and the Discord bridge
    commits into `.claude/` on its own schedule, so an unscoped `--dirty`
    would report edits that never reach the Pi.
    """
    if stamped := os.getenv("AI_TERMINAL_REVISION", "").strip():
        return stamped

    head = _git("rev-parse", "--short", "HEAD")
    if not head:
        return "unknown"
    return f"{head}-dirty" if _git("status", "--porcelain", ".") else head


def _built() -> str:
    """When the commit this server is running was made. ISO 8601, or empty.

    A time, because a time is what somebody standing in the kitchen can hold
    against a closed issue's timestamp by eye; a short SHA cannot be compared
    against anything without a lookup. #261.

    `_revision()`'s three-way shape for the same three reasons. A copied tree
    has no `.git`, so `AI_TERMINAL_BUILT` — written into the same drop-in as
    the SHA, by the same step (`tools/pi_deploy.py:1030`) — wins; then the
    checkout, which is what serves this under the staging topology; then
    nothing.

    `%cI` is the commit's own date at its own offset, which is the hour
    `git log` shows on the machine that wrote it. Deliberately not the
    deploy's clock: redeploying an old commit is still that commit, and a
    stamp that moved because somebody ran the installer again would answer a
    question nobody asked.

    Empty rather than `"unknown"`, and that is the difference between this and
    `_revision()`: this one is joined to the SHA in `_label` below, and a
    second `unknown` beside the first says the same thing twice. `_label` then
    drops the field entirely, which is what "no date at all" has to look like
    — the one reading worse than none is the *previous* deploy's date.
    """
    if stamped := os.getenv("AI_TERMINAL_BUILT", "").strip():
        return stamped
    return _git("show", "-s", "--format=%cI", "HEAD") or ""


def _label(built: str, revision: str) -> str:
    """The one line the panel's corner carries.

    Parsed rather than sliced, so that a drop-in carrying something that is
    not a timestamp degrades to the SHA on its own instead of printing the
    first sixteen characters of whatever it was. `datetime.fromisoformat`
    refuses an empty string too, which is the ordinary no-git path.

    Rendered here and not in the page: the badge is markup `root()` fills in,
    so there is no JavaScript in the path at all, and the minute is as far as
    the resolution goes — seconds on a build stamp are noise at 10px.
    """
    try:
        when = datetime.fromisoformat(built).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return revision
    return f"{when} · {revision}"


# Read once, at import, rather than per request. A process serves the bytes it
# loaded and the tree under it moves on: on 2026-09-09 the checkout behind port
# 8000 had started at 12:50 and HEAD advanced twice within the hour. Asking git
# per request would answer for the tree and label the answer the process, which
# is the same class of wrong answer this field exists to end.
#
# The same argument covers the build stamp, and it is what makes the badge
# worth having: #256 measured it on pi2 on 2026-09-11: the document Chromium
# was showing had loaded at 23:03 the night before and the unit under it had
# been restarted at 08:52 that morning. #261.
REVISION = _revision()
BUILT = _built()
BUILD_LABEL = _label(BUILT, REVISION)
logger.info("running revision %s from %s", REVISION, REPO_ROOT)


def _static_revision() -> str:
    """A name for the bytes the panel is running, not for the commit round them.

    `REVISION` is the deploy's git SHA, and the panel's freshness is not a
    question about git. A backend-only deploy moves the SHA and moves nothing
    the browser holds, and a second deploy off the same dirty HEAD moves
    `app.js` and leaves the SHA at the same `<sha>-dirty` string it had — so
    the SHA over-reloads on the first and under-reloads on the second. This
    digests what `/static` actually serves, which is the only thing a reload
    can change.

    At import, beside `REVISION` and for the same reason: the process serves
    the bytes it started with, and the tree under it moves on.
    """
    if stamped := os.getenv("AI_TERMINAL_STATIC_REVISION", "").strip():
        return stamped
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(STATIC_DIR).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


STATIC_REVISION = _static_revision()


async def _migrate_cache() -> None:
    try:
        await asyncio.to_thread(gws_cache.migrate)
    except Exception:  # pylint: disable=broad-except
        logger.warning("could not migrate the gws cache headers",
                       exc_info=True)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Run background tasks for as long as the server is up.

    Started here rather than at import because a module-level `create_task`
    needs a running loop and there is none while uvicorn is still importing
    this file; and cancelled on the way out so `--reload` does not leave a loop
    per reload running against Open-Meteo.

    Nothing waits on the first fetch. The clock is allowed to come up with no
    weather on it — `payload()` answers `{}` and the face draws the time over
    the gradient — and a server that blocked its own startup on a WAN that is
    down at 3 a.m. would cost the household the microphone as well.

    The cache migration is also a background task rather than a startup step.
    While the migration itself is a local directory walk with no network in it,
    `migrate()` takes `writing()`, which polls for the cache lock for up to
    `LOCK_TIMEOUT = 45.0` seconds. An active `gws` command or an exiting server
    instance holding the lock during a deploy restart would otherwise block
    startup from yielding, leaving the port unresponsive while systemd reports
    the unit active. Running it in the background lets the server answer
    immediately; an unmigrated header read before migration completes is safely
    treated as a cache miss. See `gws_cache.migrate`, #233, and #259.
    """
    refresh_task = asyncio.create_task(screensaver.refresh_loop())
    migrate_task = asyncio.create_task(_migrate_cache())
    try:
        _gc_profiles()
    except Exception:  # pylint: disable=broad-except
        pass
    try:
        yield
    finally:
        for task in (refresh_task, migrate_task):
            task.cancel()
        for task in (refresh_task, migrate_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if _gws_auth_browser_proc is not None:
            gws.cleanup_ephemeral_auth(_gws_auth_browser_proc, _gws_auth_profile_dir)
        close_home_toolset()

class _StaticFilesNoCache(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title=APP_NAME, lifespan=_lifespan)
app.mount("/static", _StaticFilesNoCache(directory=STATIC_DIR), name="static")

agent = build_agent()

# On disk, so a conversation survives a page reload and a reboot. ADK replays a
# session's events into the Live connection when it opens one, so persisting the
# session is the whole of "remember what we were talking about" — there is no
# history to inject by hand. Relative to the working directory, like the gws
# cache: the repo root under `uv run live-server`, the unit's `WorkingDirectory`
# on the Pi. The two sub-agents keep their throwaway in-memory services; a row
# per Workspace question would be a row nobody ever reads again.
SESSIONS_DB = Path(os.getenv("SESSIONS_DB", "sessions.db")).resolve()


class TerminalSessionService(SqliteSessionService):
    """SqliteSessionService that neutralizes live_session_id on stored turns.

    ADK's `_fencing.py:100` treats any non-user event carrying `live_session_id`
    as another agent's message on replay, turning function calls into text and
    dropping matching function responses as orphans (#396). Stripping it here
    keeps single-agent tool call/response pairs intact across disarm/reconnect.
    """

    async def append_event(self, session: Session, event: Event) -> Event:
        if getattr(event, "live_session_id", None) is not None:
            event = event.model_copy(update={"live_session_id": None})
        try:
            return await super().append_event(session, event)
        except SessionNotFoundError:
            logger.info(
                "session %s deleted before append_event; dropping event",
                session.id,
            )
            return event

    async def get_session(self, *args, **kwargs) -> Session | None:
        session = await super().get_session(*args, **kwargs)
        if session is not None and getattr(session, "events", None) is not None:
            for i, ev in enumerate(session.events):
                if getattr(ev, "live_session_id", None) is not None:
                    session.events[i] = ev.model_copy(update={"live_session_id": None})
        return session


session_service = TerminalSessionService(db_path=str(SESSIONS_DB))

# How long the kitchen can go quiet before the next hello is a new conversation
# rather than the same one continued. Half an hour is a guess at what a person
# means by "it", and it has a second job: the system instruction pins the date
# at setup, so a gap this short never spans midnight.
IDLE_GAP_S = float(os.getenv("SESSION_IDLE_GAP_S", "1800"))
if not math.isfinite(IDLE_GAP_S) or IDLE_GAP_S <= 0:
    # Same refusal as `RETAIN_S` below and for the same reason, but the
    # damage is a different one and worth naming, because this number is read
    # in two places that disagree about it. `_thread` rolls when the quiet is
    # `> IDLE_GAP_S`; `rethread` returns early when it is `<= IDLE_GAP_S`.
    #
    # At zero or below, both say roll: every hello mints a thread and pays
    # `_carry_forward` -> `recap.summarise`, which is a real model call while
    # somebody stands at the terminal, and every wake word after that rolls
    # again with no recap at all. The terminal keeps working and remembers
    # nothing, one Gemini call per hello. Zero is also what somebody types
    # meaning "never split a conversation", which is the opposite.
    #
    # At `nan` the two disagree: `>` is False so the hello never rolls, `<=`
    # is False so every arm does. The thread on disk grows without bound
    # while the person at the panel is thrown into a fresh one on every wake
    # word. At `inf` neither rolls, ever, and ADK replays the whole thread
    # into the Live connection at setup — the unbounded hello this rolling
    # exists to prevent.
    #
    # At import, like `RETAIN_S`, so a `.env` typo is a server that says why
    # rather than a terminal that behaves strangely for a week. In-process
    # assignment is untouched: several cases set this attribute directly,
    # including to a negative, to force a roll.
    raise ValueError(
        "SESSION_IDLE_GAP_S is how long the room can be quiet before the "
        "next hello is a new conversation, in seconds: it wants a finite "
        "number above zero. 0 or less starts a new one every time and pays "
        "a summary for each, `nan` grows one thread forever while rolling "
        "the page onto a new one at every wake word, and `inf` never rolls "
        "at all. Set a gap, or leave it unset for 30 minutes.")

# How long a conversation nobody has come back to is kept. Thirty days of
# nothing means nobody is going to scroll back to it, and `sessions.db` grows
# by about 630 bytes per event with nothing at all taking anything out of it.
#
# A floor on how much history a person keeps and not a ceiling: the thread they
# are using is never eligible, however old it is. Without that, somebody who
# last spoke thirty-one days ago would have it deleted by the very connect that
# was about to resume it — the one deletion a retention bound exists to avoid
# being surprising.
#
# Per person, too, and that is a property of where the sweep runs rather than
# a choice: it rides on one profile's own hello and lists only that profile's
# threads, so a name nobody connects as any more keeps everything it had. The
# growth bound survives it — a profile that has stopped writing rows has
# stopped growing — but the residue is real, and renaming somebody in
# `PROFILES` makes some: `_profiles` says a new name is a new person.
RETAIN_S = float(os.getenv("SESSION_RETAIN_S", str(30 * 86400)))
if not math.isfinite(RETAIN_S) or RETAIN_S <= 0:
    # Not a switch, whatever the convention elsewhere says. Zero puts the
    # cutoff at `time.time()`, so every thread except the one this hello is
    # about to resume is older than it and goes on the next connect — which is
    # the opposite of the "0 means unlimited" that somebody typing a zero here
    # is reaching for. There is no third reading to fall back on either:
    # nothing else in the server takes a row out of `sessions.db`, so a sweep
    # that is off is no bound at all. Refused at import, the way a value that
    # is not a number is already refused, rather than guessed at quietly.
    #
    # Finite as well as positive, because `float()` takes two more things that
    # are not windows and both walk past a bare `<= 0`. A `nan` cutoff makes
    # `last_update_time >= cutoff` False for every row, so the sweep deletes
    # all of them — the destructive reading again, arriving through the guard
    # rather than around it. An `inf` cutoff sits before every row and sweeps
    # nothing, which is the off switch the message below says there is none
    # of. Both measured on a scratch store.
    raise ValueError(
        "SESSION_RETAIN_S is how long a conversation is kept in seconds, "
        "not a switch: it wants a finite number above zero. 0 or less keeps "
        "only the thread being resumed and drops every other one, `nan` "
        "drops those too, and no value here turns the sweep off. Set a "
        "window, or leave it unset for 30 days.")

# How long the terminal stays armed with nobody saying anything, before the
# page turns the microphone off and closes the Live session under it. The page
# owns the timer — it is the side that can see a person typing and hear the
# model still answering — and reads this off `/api/config`, so the number lives
# with the rest of the server's configuration rather than in a constant only a
# reload can change. Thirty seconds is the README's suggestion and is a guess:
# long enough to think about the answer before asking the follow-up, short
# enough that a room walked out of is not left listening.
WAKE_IDLE_S = float(os.getenv("WAKE_IDLE_S", "30"))
if not math.isfinite(WAKE_IDLE_S) or WAKE_IDLE_S <= 0:
    # This one leaves the process before it means anything: `/api/config`
    # ships it and the page turns it into `wakeIdleMs`. Both ends of that
    # already refuse the bad values, and neither says so.
    #
    # Zero and negatives reach the page, and `app.js` drops them on the
    # floor: `if (Number.isFinite(config.wake_idle_s) && config.wake_idle_s
    # > 0)` keeps the built-in 30 000 ms instead. That is the right thing for
    # the page to do and the wrong place to find out — the household set a
    # number, the terminal ignored it, and no log line mentions either.
    #
    # `nan` and `inf` end in the same silence by a longer road. Starlette
    # renders JSON with `allow_nan=False`, which reads like a 500; measured,
    # the route answers 200 with `"wake_idle_s": null`, because `api_config`
    # is annotated `-> dict` and FastAPI serialises through that field before
    # the renderer sees the float, and pydantic writes a non-finite one as
    # null. `Number.isFinite(null)` is false, so the page takes the same
    # fallback. Drop that annotation and it would be a 500 instead; either
    # way the setting does nothing and nothing says so.
    raise ValueError(
        "WAKE_IDLE_S is how long the terminal stays armed with nobody "
        "saying anything, in seconds: it wants a finite number above zero. "
        "0 or less is not 'listen forever', and `nan` and `inf` are not "
        "either — /api/config passes them on as null and the page discards "
        "all three, keeping its own 30 s. Set a window, or leave it unset "
        "for 30 seconds.")

# What the terminal writes into a thread whose last word was somebody else's
# question. ADK replays a thread into every cold connection and ends the replay
# with `turn_complete=contents[-1].role == 'user'`
# (`models/gemini_llm_connection.py:104`), which is the Live API's "your turn":
# a thread that stops on a user turn is re-answered on the next arm, out loud,
# with nobody having said anything. #152.
#
# A turn and not a state write, because only a turn is replayed —
# `_contains_empty_content` (`flows/llm_flows/contents.py:439`) drops an event
# with no visible parts, which is exactly why the handshake and recap writes
# elsewhere in this file are state-only. It is authored by the agent so the
# replay ends on `role='model'`, which is the whole of the fix.
#
# Marked `[terminal]` and carrying its own instruction, the same shape and for
# the same reason as the progress notes in `tools/_progress.py`: it arrives
# with no conversation around it to say what it is. English, for the model
# rather than for the person.
UNANSWERED = (
    "[terminal] The connection ended here before this was answered. It is "
    "history, not a question: do not answer it unless it is asked again.")

# How far back to look for the last thing anybody said. A full `get_session`
# on this path would read the whole thread twice, since `run_live` is about to
# read it again (`runners.py:1133`) — and how expensive that read is on a
# thread nothing bounds is #142. Sixteen is generous for a tail: the events
# after the last spoken turn are the state writes above, and there are never
# more than a handful. A tail that is *all* invisible reads as "nothing was
# said" and this writes nothing, which is the safe way round — the cost of
# missing one is the repeat this exists to stop, not a wrong turn in the
# thread.
SETTLE_TAIL = 16

runner = Runner(app_name=APP_NAME, agent=agent, session_service=session_service)


# What the model is allowed to carry inside one connection before the server
# starts sliding the window. Two different things keep the context bounded and
# it is worth knowing which is which: this one runs during a conversation, and
# says nothing about how much of an old one is replayed into a new connection.
COMPRESSION = types.ContextWindowCompressionConfig(
    trigger_tokens=16000,
    sliding_window=types.SlidingWindow(target_tokens=8000),
)


# Which voices the terminal will offer, in the order the sheet lists them.
#
# Written down here because nothing in the toolchain will say it. google-genai
# 2.22.0 types the field as a bare string — `PrebuiltVoiceConfig.voice_name` is
# `Optional[str]` with no validator (`genai/types.py:5537-5542`) — so a
# misspelling is not caught client-side; it is caught by the Live API refusing
# the setup, which on a kiosk looks like a socket that will not stay up. The
# names are the five the installed ADK's own dev UI hard-codes
# (`adk/cli/browser/main-KSQARI5D.js:4155`), which is the only list of them
# that ships with anything this project depends on.
#
# So this tuple is a whitelist as much as a menu: `_voice` refuses anything
# that is not in it, and the page is filled from it rather than from a copy.
VOICES = ("Kore", "Puck", "Charon", "Aoede", "Fenrir")


def _default_voice() -> str:
    """The voice a terminal nobody has asked answers in.

    Before this existed the setup message carried no `speechConfig` at all and
    the Live API picked for us, which is a default nobody here can name and
    which is free to differ between two models and two weeks. An explicit one
    is worth having even if it turns out to be the same voice.

    Overridable, because which of the five is the *neutral* one is a judgement
    made by ear and nobody should have to edit Python to overrule it. Refused
    rather than trusted: an unknown name here would reach the Live API and take
    down every connection, and a terminal that will not speak is a worse answer
    to a typo than a terminal that speaks in the wrong voice and says why.
    """
    named = os.getenv("LIVE_VOICE", "").strip()
    if named and named not in VOICES:
        logger.error("LIVE_VOICE=%s is not one of %s; using %s",
                     named, ", ".join(VOICES), VOICES[0])
    return named if named in VOICES else VOICES[0]


DEFAULT_VOICE = _default_voice()


def _run_config(voice: str, interrupt: bool = False) -> RunConfig:
    """Pick the response modality the configured model actually supports.

    Native-audio and Gemini 3.x Live models speak and need transcription
    config to also produce text; half-cascade models are driven in TEXT mode.

    The voice rides on the audio branch and only there. In TEXT mode nothing is
    synthesised, so a `speech_config` would be a setting with no output to
    apply to — and it is sent in the setup message, where the API is entitled
    to have an opinion about a voice for a modality it was not asked for.
    """
    model = agent.model.lower()
    if "native-audio" in model or model.endswith("-live") or ("gemini-3." in model and "live" in model):
        return RunConfig(
            streaming_mode=StreamingMode.BIDI,
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice,
                    ),
                ),
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            session_resumption=types.SessionResumptionConfig(),
            context_window_compression=COMPRESSION,
            realtime_input_config=types.RealtimeInputConfig(
                activity_handling="START_OF_ACTIVITY_INTERRUPTS" if interrupt else "NO_INTERRUPTION",
            ),
        )
    return RunConfig(
        streaming_mode=StreamingMode.BIDI,
        response_modalities=["TEXT"],
        session_resumption=types.SessionResumptionConfig(),
        context_window_compression=COMPRESSION,
    )


def _log_tool_traffic(event) -> None:
    """Log every tool call and the shape of its result.

    The Pi runs this headless behind a kiosk browser, so the server log is the
    only place a missing card can be traced back to an empty tool result.

    Which is why the arguments are sized rather than printed. That log is the
    Pi's journal, `Storage=persistent`, and nothing the household can press
    reaches it: *Forget* clears the sessions and the standing facts, and this
    line used to leave a copy of both — the fact, the mail query, the URL, the
    plain-text goal — sitting there until the disk filled. `remember` logs the
    topic and deliberately not the fact two frames later; the care taken there
    was cancelled here. See #206.

    Sizing everything except `_PLAIN_ARGS`, rather than naming the tools that
    carry personal text, because that list would be right only until the next
    tool was added and it fails by leaking. This fails by printing a size
    where a word would have been more useful, which is a thing somebody
    reading the log notices.
    """
    for part in (event.content.parts if event.content else None) or []:
        if part.function_call:
            args = part.function_call.args
            told = (
                {k: v if k in _PLAIN_ARGS else _sized(v)
                 for k, v in args.items()}
                if isinstance(args, dict) else _sized(args)
            )
            logger.info("tool call %s %s", part.function_call.name, told)
        elif part.function_response:
            response = part.function_response.response
            shape = (
                {k: len(v) if isinstance(v, (list, str)) else v
                 for k, v in response.items()}
                if isinstance(response, dict) else type(response).__name__
            )
            logger.info("tool result %s %s",
                        part.function_response.name, shape)


DEFAULT_PROFILE = "terminal"


def _profiles() -> list[str]:
    """Who the terminal can carry a conversation on for, from `PROFILES`.

    A name here is the `user_id` in the socket path and the key a thread is
    stored under, so renaming somebody starts them a new one. Empty is the
    ordinary case for a terminal nobody has configured, and the settings sheet
    hides the picker rather than offering a choice of one.
    """
    return [name.strip() for name in os.getenv("PROFILES", "").split(",")
            if name.strip()]


def _gc_profiles(active: set[str] | None = None) -> None:
    """Purge orphaned profiles, sessions, and user_states in SQLite.

    When `PROFILES` is configured, profiles absent from the active roster
    (along with `DEFAULT_PROFILE`) are pruned. When `PROFILES` is unset or
    empty, pruning is skipped to avoid dropping data on an unconfigured
    terminal.
    """
    if active is None:
        if not os.getenv("PROFILES", "").strip():
            return
        active = set(_profiles()) | {DEFAULT_PROFILE}
    if not active:
        return
    db_path = Path(getattr(session_service, "_db_path", None) or SESSIONS_DB)
    if not db_path.exists():
        return
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" not in tables and "user_states" not in tables:
            return
        conn.execute("PRAGMA foreign_keys = ON;")
        placeholders = ",".join("?" for _ in active)
        if "events" in tables:
            conn.execute(
                "DELETE FROM events WHERE app_name = ? "
                f"AND user_id NOT IN ({placeholders})",
                (APP_NAME, *active),
            )
        if "sessions" in tables:
            conn.execute(
                "DELETE FROM sessions WHERE app_name = ? "
                f"AND user_id NOT IN ({placeholders})",
                (APP_NAME, *active),
            )
        if "user_states" in tables:
            conn.execute(
                "DELETE FROM user_states WHERE app_name = ? "
                f"AND user_id NOT IN ({placeholders})",
                (APP_NAME, *active),
            )
        conn.commit()


@app.get("/api/config")
async def api_config() -> dict:
    """Client-side config: the Maps JS key, the model, the household, and what
    is running.

    The last one is here rather than only in the log because the question is
    asked from a laptop about a machine across the house, where `journalctl -u`
    needs ssh and `curl /api/config` does not.
    """
    return {
        "build": BUILD_LABEL,
        "maps_api_key": os.getenv("GOOGLE_MAPS_API_KEY", ""),
        "model": agent.model,
        "profiles": _profiles(),
        # Both, and from here rather than in markup: the server refuses a voice
        # it does not know. A list drifted from `VOICES` is a picker offering a
        # setting the socket ignores, and the terminal then answers in the
        # default while the sheet says otherwise.
        "voices": list(VOICES),
        "voice": DEFAULT_VOICE,
        "revision": REVISION,
        # What the panel compares on a reconnect. Separate from `revision`
        # because that one names the commit and this one names the bytes: a
        # deploy that touched no static file must not cost the household its
        # screen. See #256.
        "static_revision": STATIC_REVISION,
        "wake_idle_s": WAKE_IDLE_S,
        "management_routes": _management_routes_enabled(),
    }


# What `root()` fills in on the way out, and the reason it is not a
# `FileResponse` any more. The corner badge has to name the build that served
# *these bytes*: a page fetching the answer after the fact reports whatever is
# running when it asks, which on 2026-09-11 was this morning's server behind
# last night's JavaScript — the badge would have confirmed the fixes were on
# screen at the one moment they were not. #261.
#
# Two slots, and the second is not the SHA. What the page has to be able to
# compare itself against is what `checkRevision` compares — since #256 that is
# `static_revision`, the digest of what `/static` serves, because the SHA moves
# on a Python-only deploy and stands still across two deploys off one dirty
# HEAD. The SHA is in the label for a person to read; the digest is in the
# attribute for the code to compare.
#
# `_static_revision()` digests this file as it sits on disk, placeholders and
# all, which is what makes the two agree: `pi_deploy.static_revision()` on the
# Mac hashes the same unsubstituted bytes. Digesting the served page instead
# would be a hash of its own input.
BUILD_SLOT = "__BUILD__"
STATIC_SLOT = "__BUILD_STATIC__"
INDEX = STATIC_DIR / "index.html"


@app.get("/")
async def root() -> HTMLResponse:
    """Serve the terminal UI, with this process's build written into it.

    Read per request rather than at import, which is what `FileResponse` did
    and what `--reload` and a deploy's rsync both rely on. It is 9 KB off the
    page cache once per document load, on a panel that loads one a day.

    `no-store`, because the document is now carrying process state. Left
    cacheable it is the badge's own failure mode one level down: a 304 on `/`
    would hand back a page stamped by a server that has since been replaced,
    and the honest answer costs one 9 KB fetch per reload.

    Escaped, though today one field is a hex digest and the other a timestamp
    beside a short SHA. Two of the three arrive from `Environment=` lines in a
    systemd drop-in, and the day one of them holds something else is not the
    day to find out that it lands unescaped inside an attribute.
    """
    page = INDEX.read_text(encoding="utf-8")
    return HTMLResponse(
        page.replace(BUILD_SLOT, html.escape(BUILD_LABEL))
            .replace(STATIC_SLOT, html.escape(STATIC_REVISION)),
        headers={"Cache-Control": "no-store"})


_STARTER_SUGGESTIONS_CACHE: dict[tuple[str, str, str], list[dict]] = {}
RESEARCH_STARTERS_TIMEOUT_S = 4.0


def _format_starter_research_prompt(topic: str, lang: str = "ja", domain: str = "") -> str:
    focus = f" (context: {domain})" if domain else ""
    return (
        f"Given the topic '{topic}'{focus}, identify 3-4 popular, current, or helpful "
        f"follow-up questions people ask about this topic in {lang}. "
        "Respond with a JSON array of objects with fields: "
        "'label' (2-5 word label for a chip), 'prompt' (clean natural question), "
        "'icon' (Material Symbols icon name)."
    )


def _call_grounded_starters(prompt: str) -> str:
    """One blocking generate_content with Google Search tool."""
    client = _client.grounding_client()
    response = client.models.generate_content(
        model=_client.grounding_model(),
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.2,
        ),
    )
    return getattr(response, "text", "") or ""


def _parse_starter_json(raw: str) -> list[dict]:
    import re

    if not raw or not isinstance(raw, str):
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if not text.startswith("["):
        match = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
        if match:
            text = match.group(0)
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    valid: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        prompt = str(item.get("prompt", "")).strip()
        icon = str(item.get("icon", "")).strip() or "lightbulb"
        if label and prompt:
            valid.append({"label": label, "prompt": prompt, "icon": icon})
    return valid


async def _research_topic_starters(
    topic: str, lang: str = "ja", domain: str = ""
) -> list[dict]:
    t = topic.strip()
    if not t:
        return []
    prompt = _format_starter_research_prompt(t, lang=lang, domain=domain)
    try:
        async with asyncio.timeout(RESEARCH_STARTERS_TIMEOUT_S):
            raw = await asyncio.to_thread(_call_grounded_starters, prompt)
            return _parse_starter_json(raw)
    except TimeoutError:
        logger.warning(
            "starters: research timed out after %.1fs for %r",
            RESEARCH_STARTERS_TIMEOUT_S,
            t,
        )
        return []
    except Exception as err:
        logger.warning("starters: research failed for %r: %s", t, err)
        return []


@app.get("/api/starters/suggestions")
async def api_starters_suggestions(
    q: str = "",
    topic: str = "",
    domain: str = "",
    lang: str = "ja",
) -> dict:
    """Dynamic contextual starter suggestions for topic queries."""
    query = (topic or q).strip()
    if not query:
        return {"suggestions": []}
    cache_key = (query.lower(), domain.lower(), lang.lower())
    if cache_key in _STARTER_SUGGESTIONS_CACHE:
        return {"suggestions": _STARTER_SUGGESTIONS_CACHE[cache_key]}

    suggestions = await _research_topic_starters(query, lang=lang, domain=domain)
    if suggestions:
        _STARTER_SUGGESTIONS_CACHE[cache_key] = suggestions
    return {"suggestions": suggestions}


@app.get("/api/screensaver")
async def api_screensaver(lang: str = "") -> dict:
    """What the clock face should be wearing, in the language it is set to.

    Never an error. Every field is optional and `paintScreensaver` is written
    to draw whatever arrives, so `{}` — nothing fetched since boot, no
    picture generated yet — is a complete answer rather than a 503. A status
    the page turns into the same gradient either way is only a line in a log
    nobody reads.

    `lang` is the panel's own setting, off `<html lang>` (#96), and it is the
    only thing on this route that comes from the request. It picks a word for
    the sky out of a table and nothing else: an unrecognised tag reads
    English, so there is no input here that can produce an answer the page
    cannot draw, and a page too old to send one is answered exactly as
    before.

    Reads what the background task last stored and does not fetch: see
    `screensaver`. Kept a `GET` with no body, so the content-type guard #83
    puts on `POST /api/forget` is not this route's problem — there is nothing
    here for a form on another origin to do that curl cannot already do.
    """
    return screensaver.payload(lang)


@app.get("/api/screensaver.jpg")
async def api_screensaver_image() -> FileResponse:
    """The photograph behind the clock, off disk.

    This function takes no arguments, and that is the security property
    rather than an accident of it having nothing to ask for: the path served
    is a fixed filename inside `screensaver_dir()`, so no part of it comes
    from the request and there is nowhere for a `..` to be smuggled in. A
    query string reaches a route that never reads one, and an extra path
    segment matches no route at all.

    The type is read off the bytes and not off the `.jpg` in the path
    (`screensaver.image_media_type`). The path is a route name; the file is
    whatever the image model drew, which is PNG. Serving one as the other
    renders anyway — Chromium sniffs an `<img>` — so what it costs is every
    consumer that believes a header instead: a cache, a proxy, a download,
    and the next person to read this line (#110).

    404 rather than a placeholder when the file is not there, which is every
    terminal's state until #86 lands. The page only asks for this after
    `/api/screensaver` handed it a URL, so a 404 means the file went away in
    between — and an `<img>` whose load fails renders as nothing, which is
    the gradient it started on. Checked here rather than left to
    `FileResponse`, which raises `RuntimeError` from inside the response and
    turns a missing file into a 500 (`starlette/responses.py:353`).
    """
    try:
        picture = screensaver.image_path()
    except OSError as err:
        # `image_path` creates the directory on the way to naming the file, so
        # an unwritable `SCREENSAVER_DIR` raises here rather than returning a
        # path that is not there. `payload()` already swallows the same
        # exception and degrades to `{}`, so the two routes have to agree:
        # without this one answers "no background" and the other answers a
        # traceback, for the same cause.
        logger.warning("screensaver: cannot reach the picture: %s", err)
        raise HTTPException(
            status_code=404, detail="no background yet") from err
    if not picture.is_file():
        raise HTTPException(status_code=404, detail="no background yet")
    return FileResponse(
        picture, media_type=screensaver.image_media_type(picture))


def _coords(params) -> dict | None:
    """`lat` and `lng` off the handshake, or None if they are not a position."""
    try:
        lat, lng = float(params["lat"]), float(params["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"lat": lat, "lng": lng} if -90 <= lat <= 90 and -180 <= lng <= 180 else None


def _voice(params) -> str:
    """The voice this connection asked for, or the terminal's own.

    A name off a query string, which means it is whatever a page had in
    `localStorage` — including one this build no longer serves, after `VOICES`
    is shortened or a name is spelled differently. Membership rather than
    truthiness: an unknown name reaches the Live API in the setup message, and
    what the API does with it is refuse the connection, so the browser
    reconnects into the same refusal for as long as the setting survives. The
    default is the failure that leaves someone able to change their mind.
    """
    asked = params.get("voice", "").strip()
    return asked if asked in VOICES else DEFAULT_VOICE


def _is_kiosk(params) -> bool:
    """Whether this page says it is the one standing in the kitchen.

    Set by `deploy/ai-terminal-kiosk.service`, which opens
    `http://localhost:8000/?kiosk=1`, and forwarded onto the handshake by
    `connect` in `app.js`. The server cannot work it out: the panel reaches a
    development Mac down the ssh tunnel in `deploy/ai-terminal-tunnel.service`,
    so its socket arrives from 127.0.0.1 exactly like a browser tab on the Mac
    itself, and there is nothing else in the connection that differs.

    Trusted, because there is nothing here worth forging. The handshake is
    already refused unless it comes from this server's own page
    (`_from_our_page`), and a page that far in can open the microphone by
    tapping the button on it. All the flag decides is which of the household's
    own screens a wake word reaches, and it can only ever be one of them —
    `screen.wake_sink` picks a single screen either way.

    `0` counts as no, so a page can turn it off explicitly rather than by
    editing the URL down; anything else non-empty counts as yes, because the
    unit writes `1` and a value nobody wrote is not a case to invent.
    """
    return params.get("kiosk", "").strip() not in ("", "0")


def _is_interrupt(params) -> bool:
    """Whether the client requested barge-in interruption (#398)."""
    return params.get("interrupt") in ("1", "true")


async def _carry_forward(user_id: str, rolled_id: str, session) -> None:
    """Carry the thread just rolled away into the one replacing it.

    Two things, and they are not the same thing. The summary is what was
    *said* — see `recap`. The plan pad is what a plan *worked out*, which the
    summary has only where the assistant happened to read it out loud, and
    which otherwise lives on the old session row and dies with it. `plan` is
    the tool whose whole point is that the answer to one lookup is what the
    next one needs, so a gap that drops its findings drops the expensive half
    of the last conversation and the planner spends the lookups again.

    A state-only event rather than an argument to `create_session`, for the
    same reason the handshake state is one: it is the supported way to change
    state after the fact, and ADK keeps such events out of the history it
    replays. Only the summary's key is `user:`-prefixed, so the SQLite backend
    files that one under the person and survives the next rollover on its own;
    the pad's is bare and belongs to the session, which is #64 and is why it
    has to be carried by hand at all.

    Both are written even when there is nothing to write, which is what clears
    what came before. A stale value would put a week-old thread in front of the
    agent labelled as the conversation it has just had, and a wrong memory is
    worse to talk to than none. For the pad that means only a thread that ran a
    plan itself passes one on: one that inherited a pad and added nothing hands
    the same findings back a gap older, every gap, forever.
    """
    old = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=rolled_id)
    summary = await recap.summarise(old.events) if old else ""
    # Through `held`, so the pad is trimmed to this release's caps on the way.
    # They were sized for one conversation and the row it comes off could have
    # been written by a release with different ones.
    pad = (_plan_run.held(old.state)
           if old is not None and _plan_run.ran(old.events) else [])
    await session_service.append_event(session, Event(
        author="user",
        actions=EventActions(state_delta={recap.RECAP_KEY: summary,
                                          _plan_run.PAD_KEY: pad})))
    logger.info("carried %d chars and %d findings forward from %s",
                len(summary), len(pad), rolled_id)


async def _retire(user_id: str, keep: set[str], rows=None) -> None:
    """Drop `user_id`'s threads nothing has touched inside `RETAIN_S`.

    `keep` is the ids to leave alone whatever their age — the thread the caller
    is about to hand to somebody. Deleting that one would be the connect that
    was going to resume a conversation ending it instead.

    `rows` is a listing the caller already has. `_thread` has one, because the
    sweep rides on the query it makes anyway; the roll path does not, and passes
    none, and then this makes the query itself.

    **Nothing here raises.** Both callers are on a path where an exception costs
    the person standing at the terminal their connection: `_thread` runs after
    `websocket.accept()` with no `try` around it, and `roll` runs inside `arm`,
    where it would surface as a wake word that does nothing. The sweep is not
    what either of them is for. A window that runs a minute long costs nothing;
    a terminal that will not answer costs the conversation. So the listing is
    guarded and each delete is skipped on its own — same shape as
    `gws_cache._evict`, which skips a file it cannot unlink. `Exception` and not
    `OSError`: a locked or corrupt store answers through the DB driver, not the
    filesystem.
    """
    if rows is None:
        try:
            listed = await session_service.list_sessions(
                app_name=APP_NAME, user_id=user_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("could not list %s's threads to retire any: %s",
                           user_id, exc)
            return
        rows = listed.sessions
    cutoff = time.time() - RETAIN_S
    for old in rows:
        if old.id in keep or old.last_update_time >= cutoff:
            continue
        if any(held.live() for held in _PAGES.get(old.id, {}).values()):
            continue
        # Cascades to the events (`sessions/sqlite_session_service.py`, the
        # `ON DELETE CASCADE` on the events table and the `PRAGMA foreign_keys`
        # every connection opens with), which is the row count this is about.
        # SQLite keeps the pages for reuse rather than shrinking the file, so
        # the bound shows as `sessions.db` levelling off, not as it getting
        # smaller.
        try:
            await session_service.delete_session(
                app_name=APP_NAME, user_id=user_id, session_id=old.id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("could not drop %s for %s, leaving it: %s",
                           old.id, user_id, exc)
            continue
        logger.info("dropped %s for %s, untouched for %.0f days", old.id,
                    user_id, (time.time() - old.last_update_time) / 86400)


async def _thread(user_id: str, state: dict) -> str:
    """The conversation this hello belongs to: the last one, or a new one.

    The browser used to choose this, and minted a fresh id on every page load,
    which is why the terminal forgot everything each time. It cannot choose it
    any more: whoever chooses decides what gets replayed, and that is a question
    about how long ago someone last spoke, which only the server knows.

    Not one permanent thread per person, either. ADK replays the thread into the
    Live connection at setup, so a thread that is never rolled makes every hello
    slower than the last, for a conversation nobody remembers having.

    What the rolled-away thread was about goes forward as two or three
    sentences — see `recap`. Without that carry-forward, "book it for the same
    time" stops meaning anything over breakfast, and the only fix is no roll.

    `list_sessions` carries no events and no state
    (`sessions/base_session_service.py`), so this costs one small query.

    The retention sweep rides on that same query — `_retire`, over the list
    already in hand: firing on activity is better than a boot sweep across
    profiles for a power cut.

    It is not the only place it fires, and it used to be. Accept happens on
    every reconnect, so on a server restart, a deploy, or `app.js`'s `reopen` —
    but a kiosk socket stays up for days, and while it does nothing here runs at
    all. `roll` sweeps too, so a thread that crosses the window on a socket that
    never re-accepts still ages out. #173.
    """
    listed = await session_service.list_sessions(
        app_name=APP_NAME, user_id=user_id)
    latest = max(listed.sessions, key=lambda s: s.last_update_time,
                 default=None)
    await _retire(user_id, {latest.id} if latest is not None else set(),
                  listed.sessions)
    rolled = None
    if latest is not None and (
            time.time() - latest.last_update_time > IDLE_GAP_S):
        rolled, latest = latest, None

    if latest is None:
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, state=state)
        if rolled is not None:
            await _carry_forward(user_id, rolled.id, session)
            for held in list(_PAGES.get(rolled.id, {}).values()):
                if not held.live():
                    held.move(session.id)
        logger.info("new conversation for %s: %s", user_id, session.id)
        return session.id

    # The handshake state is rewritten on every connect, and this is the half
    # that used to be missed: it was written only where the session was created,
    # so confirming a new pin had to mint a new id to get the address in front
    # of the agent, and the conversation went with it. There is no
    # `update_session` on the service — a state-only event is the supported way,
    # and ADK keeps those out of the history it replays.
    #
    # Only when it says something the row does not already say, and that
    # condition is the whole of it. The sqlite backend stamps
    # `last_update_time` from every appended event
    # (`sessions/sqlite_session_service.py`), so a write that changes no value
    # still moves the one column `IDLE_GAP_S` is measured against — and the
    # page reconnects with the same pin it had, on a Wi-Fi blip, a server
    # restart, or the backoff in `app.js`. Written unconditionally, the rule
    # stops being "half an hour since anybody spoke" and becomes "half an hour
    # since the last of speech or a reconnect": the roll in `rethread` never
    # fires under a socket that flapped, the thread grows without bound, and
    # the retention sweep never reaches it. Nothing says the rule lapsed. #114.
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=latest.id)
    if session is not None and any(
            session.state.get(key) != value for key, value in state.items()):
        await session_service.append_event(session, Event(
            author="user", actions=EventActions(state_delta=dict(state))))
    logger.info("resuming %s for %s, %s events", latest.id, user_id,
                len(session.events) if session else 0)
    return latest.id


def _last_speaker(events) -> str | None:
    """Whose turn the replay of these events would end on, or None.

    The role ADK will put on the last content it builds, worked out on the
    events rather than on the contents, because the contents do not exist
    until `run_live` has opened a connection and by then the flag is set.

    The three shapes are the three this server stores: a native-audio reply is
    an `output_transcription` and carries no content at all, a spoken question
    is an `input_transcription`, and everything else — typed turns, function
    calls, function responses, the notes `tools/_progress.py` injects — is a
    content with a role on it. A function response is `role='user'`
    (`flows/llm_flows/functions.py:1637`), which is not a mistake here: the
    Live API reads one at the end of a replay as a turn to answer, the same as
    a question.

    Invisible events are skipped on the same rule ADK skips them by
    (`_is_part_invisible`, `flows/llm_flows/contents.py:388`), narrowed to the
    part kinds this server writes. Audio parts are left out deliberately:
    `send_history` filters them (`content_utils.filter_audio_parts`) and
    `_should_append_event` (`runners.py:1653`) keeps them out of the store, so
    an event that held only audio is not one the replay would end on.
    """
    for event in reversed(events):
        if event.input_transcription and event.input_transcription.text:
            return "user"
        if event.output_transcription and event.output_transcription.text:
            return "model"
        content = event.content
        if content is None or not content.role or not content.parts:
            continue
        if any(part.text or part.function_call or part.function_response
               for part in content.parts):
            return content.role
    return None


async def _settle(user_id: str, session_id: str) -> None:
    """Leave nothing at the end of the thread that reads as a question.

    Called from `arm`, and it is the arm that needs it rather than the disarm
    that creates the dangling turn: a thread is left dangling by anything that
    stops a Live session between somebody speaking and the agent answering,
    and `disarm` is only one of those. The socket dropping mid-answer, the
    server being restarted, a 429 caught by `live_ended` — none of them runs
    `disarm`'s tail, and all of them leave a thread on disk that the next wake
    word replays. Reading it here catches every one, including one written by
    a release that predates this function.

    Costs one small query and, only when the thread is dangling, one state-
    sized write. Both are off the model, so the wake word pays no round trip
    for it — the same trade `rethread` makes just above.
    """
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id,
        config=GetSessionConfig(num_recent_events=SETTLE_TAIL))
    if session is None or _last_speaker(session.events) != "user":
        return
    await session_service.append_event(session, Event(
        author=agent.name,
        content=types.Content(
            role="model", parts=[types.Part(text=UNANSWERED)])))
    logger.info("closed an unanswered turn on %s for %s", session_id, user_id)


def _sent_deliberately(request: Request) -> bool:
    """Whether this request could only have come from our own page.

    Not authentication — there is none on this terminal and there is not going
    to be, because a passphrase on a kitchen panel with no keyboard is worse
    than the exposure. This is the narrower question of whether a browser would
    have sent it *without being told to by us*, and the answer turns on one
    header.

    `fetch(url, {method: "POST"})` with no headers and no body is a CORS
    **simple** request. Simple requests are not preflighted: the browser sends
    them to any origin, and then refuses the *response* to the calling script.
    By that point the delete has happened. So any page open on the home network
    could fire `POST /api/forget/alex` and take a month of conversation with it,
    unable to read the reply and not needing to.

    `application/json` is not one of the three content types a simple request
    may carry. Asking for it turns every cross-origin attempt into a preflight,
    the preflight has no `Access-Control-Allow-Origin` to answer it — the
    server registers no CORS middleware at all — and the POST is never sent.
    Same-origin requests are never preflighted, so our own page pays nothing.

    Parsed rather than compared, because a browser is entitled to send
    `application/json; charset=utf-8` and a string match would refuse it.
    """
    kind = request.headers.get("content-type", "")
    return kind.split(";")[0].strip().lower() == "application/json"


def _management_routes_enabled() -> bool:
    """Whether management and administrative routes are enabled.

    Disabled by default to protect demo environments (e.g. Cloud Run) where
    unauthenticated visitors could otherwise trigger a logout, clear history,
    or fire wake events. Enabled on the Pi via `ENABLE_MANAGEMENT_ROUTES=1`.
    """
    return os.environ.get("ENABLE_MANAGEMENT_ROUTES", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


@app.post("/api/forget/{user_id}")
async def api_forget(user_id: str, request: Request) -> dict:
    """Throw away everything the terminal remembers about one person.

    Every thread of theirs, not the current one. `_thread` above resolves to
    the newest session that still exists, so deleting one only uncovers the one
    before it: the next hello either resumes a conversation from last week or
    rolls it and recaps it out loud. A control called "forget" that leaves the
    terminal reciting Tuesday's dentist appointment is worse than no control,
    which is why this takes the person rather than the thread.

    The recap has to go the same way and does not go with the sessions.
    `user:`-prefixed state is filed under the person (`sessions/state.py`), so
    it outlives every session it was written through — measured on ADK 2.8.0, a
    session created after deleting all of them still comes back carrying the
    old summary. Clearing it means appending an empty one, and appending needs
    a session that exists, so a scratch session is created to write it through
    and deleted with the rest. Listing happens first so the scratch is not in
    the list twice.

    The household facts go with them, on the same key mechanism and for the
    stronger version of the same reason: they are the part that was built to
    outlive every conversation, so they are the part a button called "forget"
    is most obviously about. A terminal that has deleted every transcript and
    still opens by knowing somebody is vegetarian has not forgotten them. If
    the household ever wants "forget this conversation" and "forget who I am"
    as two controls, this is the line that splits — but one button means one
    meaning, and the wider one is the only safe reading of the narrower word.

    The Workspace cache goes too, for the same reason and it is the same
    reading: it is copies of the household's own documents, on the card, and
    nothing else in the product ever deletes one — `gws_cache._evict` drops the
    oldest only once 200 documents have arrived, deliberately not on a clock.
    A terminal that has deleted every transcript and still holds last month's
    medical letter has not forgotten them. See #233, and #206 for the store
    this is the second instance of.

    Off the loop, because `gws_cache.forget` takes the cache lock and a `gws`
    command in flight can be holding it.

    Nothing is written for a person the terminal is holding nothing about. The
    clear is a write, and a `user:` write is the one write here that cannot be
    taken back: `delete_session` takes the session row and cascades to its
    events and stops there (`sessions/sqlite_session_service.py:403-411`), and
    a state delta merges into the row rather than replacing it, so no delta
    removes a key either (`_upsert_user_state`, `:588-603`). Unguarded, that
    leaves one permanent `user_states` row per id this route is handed, each
    one answered `{"forgotten": 0}` as though nothing had happened. The route
    takes the id from the path and authenticates nobody, so the bound is
    whatever reaches the host — today the tunnel and the box itself. #176, and
    the second place #72's bound on what the store can grow to has not held.
    """
    if not _management_routes_enabled():
        raise HTTPException(
            status_code=403,
            detail="route disabled in demo configuration",
        )
    # First, and before anything is read or written: a guard that runs after
    # the delete loop is not a guard.
    if not _sent_deliberately(request):
        raise HTTPException(
            status_code=415,
            detail="This endpoint deletes conversations, so it only accepts a "
                   "request a browser would not send across origins on its "
                   "own. Send `Content-Type: application/json`.")
    listed = await session_service.list_sessions(
        app_name=APP_NAME, user_id=user_id)
    if not listed.sessions:
        # Having no threads is not the same question as having nothing to
        # clear, and answering the second one with the first is how a guard
        # here goes wrong: the `user:` row is the one thing built to outlive
        # the sessions, so a forget that returns on the session count alone
        # no-ops on precisely the state it exists for. Asked only on this
        # branch, so the ordinary press — somebody with threads — makes the
        # queries it made before and no more.
        #
        # Not asked of `PROFILES`, either. That list is empty on a terminal
        # nobody has configured and never contains the browser's own default
        # (`_profiles` above, `DEFAULT_PROFILE` in `app.js`), so a roster check
        # would refuse the ordinary household's only forget. `get_user_state`
        # is the question itself, it is public on the service
        # (`sessions/base_session_service.py:126-164`) and implemented by the
        # backend this app pins, and it is keyed by person and needs no
        # session — which is the shape of the state being asked about. Its
        # keys come back without the `user:` prefix, so they are read for
        # emptiness rather than by name.
        carried = await session_service.get_user_state(
            app_name=APP_NAME, user_id=user_id)
        if not any(carried.values()):
            logger.info("nothing to forget for %s", user_id)
            return {"forgotten": 0}
    scratch = await session_service.create_session(
        app_name=APP_NAME, user_id=user_id, state={})
    # The scratch session comes back carrying this person's `user:` rows
    # already merged in — measured on ADK 2.8.0, `create_session` merges them
    # the same way `get_session` does — which is what lets `memory.forgetting`
    # name every fact key there is to clear. The facts are one key per topic,
    # so there is no single key to blank.
    await session_service.append_event(scratch, Event(
        author="user",
        actions=EventActions(state_delta={recap.RECAP_KEY: "",
                                          **memory.forgetting(scratch.state)})))
    for old in (*listed.sessions, scratch):
        await session_service.delete_session(
            app_name=APP_NAME, user_id=user_id, session_id=old.id)
    # After the sessions and not before them: the cached documents are the part
    # a failure here would leave behind, and a walk that raised before the
    # transcripts were gone would report nothing forgotten while having deleted
    # the documents anyway.
    dropped = await asyncio.to_thread(gws_cache.forget)
    logger.info("forgot %d conversations and %d cached documents for %s",
                len(listed.sessions), dropped, user_id)
    # The body stays `{"forgotten": <conversations>}`. `app.js:1161`
    # destructures exactly that key and `wiring_check.py:2231` and
    # `history_check.py:915` compare the whole dict, so the document count is
    # reported in the log rather than added here: it is a number for whoever is
    # reading the journal, not one the page draws.
    return {"forgotten": len(listed.sessions)}


def _is_pi_host() -> bool:
    return os.getenv("AI_TERMINAL_IS_PI") == "1"


def _trigger_system_reboot() -> None:
    """Execute system reboot after response has returned."""
    if not _is_pi_host():
        logger.warning("refusing reboot on non-pi host")
        return
    try:
        subprocess.Popen(["sudo", "shutdown", "-r", "+0"])
    except Exception:
        pass


@app.post("/api/reboot")
async def api_reboot(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Trigger a graceful host system reboot via background task."""
    if not _management_routes_enabled():
        raise HTTPException(
            status_code=403,
            detail="route disabled in demo configuration",
        )
    if not _sent_deliberately(request):
        raise HTTPException(
            status_code=415,
            detail="POST requires Content-Type: application/json",
        )
    if not _is_pi_host():
        raise HTTPException(
            status_code=409,
            detail="reboot only supported on Raspberry Pi appliance host",
        )
    background_tasks.add_task(_trigger_system_reboot)
    return {"status": "rebooting"}


@app.post("/api/wake")
async def api_wake(request: Request) -> dict:
    """Somebody said the wake word at the terminal.

    Posted by `wake.py --webhook` on the Pi, which reaches this through the
    ssh tunnel in `deploy/ai-terminal-tunnel.service`, so the URL the monitor
    is configured with is `http://localhost:8000/api/wake` whichever machine
    the server is on. One screen on each session currently attached is told,
    and what a page does about it is the page's business — see `handleWake` in
    `app.js`.

    One and not every, because a wake frame opens a microphone rather than
    drawing something: told to all of them, a word said in the kitchen records
    every room a page is open in. Which one is `screen.wake_sink`'s decision —
    the kiosk if a page on that session says it is the kiosk, and the newest
    otherwise — and it is deliberately not `screen.sink`, which a card resolves
    through and which answers "the newest page" because drawing twice is the
    thing a card has to avoid. See #132 for why the same index cannot answer
    both.

    The guard runs before anything is pushed, and this endpoint needs it more
    than `/api/forget` does rather than less: `--host` defaults to `127.0.0.1`
    (see `run` below), the request needs no body, and what it costs is a
    household microphone opening in a room nobody is in.

    The body is not read at all. The monitor sends `{model, score}` and nothing
    here wants either — the page has one microphone and no choice to make about
    which detection deserves it — and reading it would turn a truncated POST
    from a monitor whose refractory window expired mid-send into a 422 that
    leaves the terminal deaf. The content type is required for what carrying it
    proves, not for what is inside it.

    Never raises for a screen that has gone away: `screen.push_wake` swallows a
    dead socket, and the monitor must keep listening whatever the agent is
    doing. The count is screens the frame was handed to, which is what
    `{"armed": 0}` is for — a kiosk that has not loaded the page yet. Counted
    as the loop goes rather than taken off `screen.sessions()` in front of it:
    the list is a snapshot, and a push can end with a connection tearing itself
    down, so a session on it can have no screen left by the time it comes up.

    In turn, and that is safe because no iteration waits on a socket:
    `screen.push_wake` queues the frame on the page it picked and hands it to
    that page's writer. It did await the send once, and then one page that had
    stopped reading stalled this loop before it reached the sessions behind it
    — the kitchen kiosk deaf because a laptop in another room was asleep. The
    fan-out is not what has to be concurrent here; the send is what must not be
    awaited. See `screen.push_wake`.
    """
    if not _management_routes_enabled():
        raise HTTPException(
            status_code=403,
            detail="route disabled in demo configuration",
        )
    if not _sent_deliberately(request):
        raise HTTPException(
            status_code=415,
            detail="This endpoint opens a microphone, so it only accepts a "
                   "request a browser would not send across origins on its "
                   "own. Send `Content-Type: application/json`.")
    armed = 0
    for session_id in screen.sessions():
        if await screen.push_wake(session_id, {"type": "wake"}):
            armed += 1
    logger.info("wake word reached %d screen(s)", armed)
    return {"armed": armed}


_gws_login_proc: subprocess.Popen | None = None
_gws_auth_browser_proc: subprocess.Popen | None = None
_gws_auth_profile_dir: Path | None = None
_gws_login_lock = asyncio.Lock()


def _gws_status() -> dict:
    if not _gws_cli.available():
        return {
            "available": False,
            "connected": False,
            "expired": False,
            "account": None,
            "scopes": [],
        }
    try:
        data = _gws_cli.run_json(["auth", "status"], timeout=10.0)
    except _gws_cli.GwsAuthExpiredError:
        return {
            "available": True,
            "connected": False,
            "expired": True,
            "account": None,
            "scopes": [],
        }
    except Exception:
        return {
            "available": True,
            "connected": False,
            "expired": False,
            "account": None,
            "scopes": [],
        }
    raw_account = data.get("email") or data.get("account")
    account = str(raw_account) if raw_account else None
    scopes_val = data.get("scopes")
    scopes = [str(s) for s in scopes_val] if isinstance(scopes_val, list) else []

    has_creds = bool(
        account
        or data.get("has_refresh_token")
        or data.get("plain_credentials_exists")
        or data.get("encrypted_credentials_exists")
        or data.get("auth_method") not in (None, "none")
    )

    is_expired = bool(
        data.get("expired")
        or (has_creds and data.get("token_valid") is False)
        or (has_creds and str(data.get("status", "")).lower() in ("expired", "revoked", "invalid"))
        or (has_creds and _gws_cli.is_auth_expired(json.dumps(data)))
    )

    if is_expired:
        return {
            "available": True,
            "connected": False,
            "expired": True,
            "account": account,
            "scopes": scopes,
        }

    connected = bool(
        account
        or data.get("token_valid")
        or (data.get("auth_method") not in (None, "none") and (
            data.get("has_refresh_token")
            or data.get("plain_credentials_exists")
            or data.get("encrypted_credentials_exists")
        ))
    )
    global _gws_auth_browser_proc
    global _gws_auth_profile_dir
    if connected and _gws_auth_browser_proc is not None:
        gws.cleanup_ephemeral_auth(_gws_auth_browser_proc, _gws_auth_profile_dir)
        _gws_auth_browser_proc = None
        _gws_auth_profile_dir = None
    return {
        "available": True,
        "connected": connected,
        "expired": False,
        "account": account if connected else None,
        "scopes": scopes if connected else [],
    }


@app.get("/api/gws/status")
async def api_gws_status() -> dict:
    """Status of the Google Workspace CLI connection.

    Never exposes tokens, secrets, or credential paths.
    """
    return await asyncio.to_thread(_gws_status)


def _start_gws_login() -> str:
    """Launch `gws auth login` and return the consent URL.

    Terminates any previously launched login process. The URL and any account
    email must not be passed to loggers or printed.
    """
    global _gws_login_proc
    global _gws_auth_browser_proc
    global _gws_auth_profile_dir
    gws.cleanup_ephemeral_auth(_gws_auth_browser_proc, _gws_auth_profile_dir)
    _gws_auth_browser_proc = None
    _gws_auth_profile_dir = None
    if _gws_login_proc is not None and _gws_login_proc.poll() is None:
        try:
            _gws_login_proc.terminate()
            _gws_login_proc.wait(timeout=1.0)
        except Exception:
            try:
                _gws_login_proc.kill()
            except Exception:
                pass
    proc = subprocess.Popen(
        [_gws_cli.binary(), "auth", "login"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _gws_login_proc = proc
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and proc.poll() is None:
        line = proc.stderr.readline() if proc.stderr else ""
        if not line and proc.stdout:
            line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            continue
        line = line.strip()
        if "https://" in line:
            for part in line.split():
                if part.startswith("https://"):
                    return part
    if proc.poll() is not None and proc.returncode != 0:
        err_out = (proc.stderr.read() if proc.stderr else "").strip()[:200]
        raise HTTPException(
            status_code=500,
            detail=f"gws login failed: {err_out or 'exit ' + str(proc.returncode)}",
        )
    raise HTTPException(status_code=504, detail="Timed out waiting for consent URL")


@app.post("/api/gws/login")
async def api_gws_login(request: Request) -> dict:
    """Initiate Google Workspace login and return the consent URL."""
    if not _sent_deliberately(request):
        raise HTTPException(
            status_code=415,
            detail="This endpoint initiates login, so it only accepts a "
                   "request a browser would not send across origins on its "
                   "own. Send `Content-Type: application/json`.",
        )
    if not _gws_cli.available():
        raise HTTPException(status_code=503, detail="gws CLI is not available")
    try:
        body = await request.json()
    except Exception:
        body = {}
    async with _gws_login_lock:
        url = await asyncio.to_thread(_start_gws_login)
        if body.get("kiosk"):
            global _gws_auth_browser_proc
            global _gws_auth_profile_dir
            proc, pdir = gws.launch_ephemeral_auth_browser(url)
            _gws_auth_browser_proc = proc
            _gws_auth_profile_dir = pdir
    return {"url": url, "consent_url": url}


def _do_gws_logout() -> dict:
    """Run `gws auth logout` to clear saved credentials."""
    global _gws_login_proc
    global _gws_auth_browser_proc
    global _gws_auth_profile_dir
    if _gws_login_proc is not None and _gws_login_proc.poll() is None:
        try:
            _gws_login_proc.terminate()
            _gws_login_proc.wait(timeout=1.0)
        except Exception:
            try:
                _gws_login_proc.kill()
            except Exception:
                pass
        _gws_login_proc = None
    gws.cleanup_ephemeral_auth(_gws_auth_browser_proc, _gws_auth_profile_dir)
    _gws_auth_browser_proc = None
    _gws_auth_profile_dir = None
    if not _gws_cli.available():
        return {"status": "ok", "cleared": False}
    try:
        proc = subprocess.run(
            [_gws_cli.binary(), "auth", "logout"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        return {"status": "ok", "cleared": proc.returncode == 0}
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"gws logout failed: {err}")


@app.post("/api/gws/logout")
async def api_gws_logout(request: Request) -> dict:
    """Clear Google Workspace credentials."""
    if not _management_routes_enabled():
        raise HTTPException(
            status_code=403,
            detail="route disabled in demo configuration",
        )
    if not _sent_deliberately(request):
        raise HTTPException(
            status_code=415,
            detail="This endpoint clears credentials, so it only accepts a "
                   "request a browser would not send across origins on its "
                   "own. Send `Content-Type: application/json`.",
        )
    return await asyncio.to_thread(_do_gws_logout)


@app.post("/api/gws/cancel")
async def api_gws_cancel(request: Request) -> dict:
    """Cancel any in-progress Google Workspace login and clean up processes."""
    if not _sent_deliberately(request):
        raise HTTPException(
            status_code=415,
            detail="This endpoint cancels login, so it only accepts a "
                   "request a browser would not send across origins on its "
                   "own. Send `Content-Type: application/json`.",
        )
    global _gws_login_proc
    global _gws_auth_browser_proc
    global _gws_auth_profile_dir
    async with _gws_login_lock:
        if _gws_login_proc is not None and _gws_login_proc.poll() is None:
            try:
                _gws_login_proc.terminate()
                _gws_login_proc.wait(timeout=1.0)
            except Exception:
                try:
                    _gws_login_proc.kill()
                except Exception:
                    pass
            _gws_login_proc = None
        gws.cleanup_ephemeral_auth(_gws_auth_browser_proc, _gws_auth_profile_dir)
        _gws_auth_browser_proc = None
        _gws_auth_profile_dir = None
    return {"status": "ok", "cancelled": True}


@app.post("/api/camera/offer")
async def api_camera_offer(request: Request) -> dict:
    """Exchange browser WebRTC offer SDP for SDM answer SDP."""
    if not _sent_deliberately(request):
        raise HTTPException(
            status_code=415,
            detail="This endpoint negotiates WebRTC, so it only accepts a "
                   "request a browser would not send across origins on its "
                   "own. Send `Content-Type: application/json`.",
        )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Missing JSON body")
    offer_sdp = body.get("sdp") or body.get("offerSdp")
    if not offer_sdp:
        raise HTTPException(status_code=400, detail="Missing offer SDP")
    try:
        return await asyncio.to_thread(_sdm.generate_webrtc_stream, offer_sdp)
    except _sdm.SdmAuthError as err:
        raise HTTPException(status_code=401, detail=str(err))
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@app.post("/api/camera/extend")
async def api_camera_extend(request: Request) -> dict:
    """Extend an active WebRTC stream session."""
    if not _sent_deliberately(request):
        raise HTTPException(
            status_code=415,
            detail="This endpoint extends WebRTC, so it only accepts a "
                   "request a browser would not send across origins on its "
                   "own. Send `Content-Type: application/json`.",
        )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Missing JSON body")
    media_session_id = body.get("mediaSessionId", "")
    if not media_session_id:
        raise HTTPException(status_code=400, detail="Missing mediaSessionId")
    try:
        return await asyncio.to_thread(_sdm.extend_webrtc_stream, media_session_id)
    except _sdm.SdmAuthError as err:
        raise HTTPException(status_code=401, detail=str(err))
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@app.post("/api/camera/stop")
async def api_camera_stop(request: Request) -> dict:
    """Stop an active WebRTC stream session and turn off camera LED."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    media_session_id = body.get("mediaSessionId", "")
    if not media_session_id:
        return {"status": "ok"}
    try:
        return await asyncio.to_thread(_sdm.stop_webrtc_stream, media_session_id)
    except Exception:
        return {"status": "ok"}


def _allowed_hosts() -> list[str]:
    """Which names this server will answer to: loopback, plus `ALLOWED_HOSTS`.

    The other half of the pair `_from_our_page` below is one of, and the half
    that a rebound DNS record needs. The attacker's page is served from
    `evil.example`, whose record they control; once it is loaded the record
    flips to this server's address, and the browser then sends
    `Origin: http://evil.example` beside `Host: evil.example`. Those two agree,
    so the comparison below says yes and the script is talking to the agent
    with `gws` in hand. What the comparison answers is "is this our page", and
    by the browser's reckoning it is. This answers the question it cannot:
    which names are ours at all.

    Loopback is the default because it is the only name either deployment
    uses. `getUserMedia` needs a secure context and the Chromium policy file
    grants the microphone to one origin, so the README pins the kiosk to
    `http://localhost:8000` on the shipped Pi and on the panel alike — and the
    panel's `ssh -L 8000:127.0.0.1:8000` forwards TCP without touching the
    header, so what arrives here is the name the browser was pointed at. The
    pair rather than one of them because `localhost:8000` and
    `127.0.0.1:8000` are different origins by the spec and both real.

    That is what makes this list affordable where a list of *client origins*
    would not have been: it names the server, so it changes when somebody
    changes the deployment, and a list whose failure mode is a panel that will
    not connect is a list somebody eventually sets to `*`.

    `ALLOWED_HOSTS` adds to the pair rather than replacing it, for the same
    reason. Read as the whole list, `ALLOWED_HOSTS=raspberrypi.local` would take the
    kiosk's own name away and the terminal would come up refusing the one
    browser it has. Nothing is lost by keeping loopback: `localhost` is the
    one name nobody can hold a DNS record for, so no attacker's page is ever
    served from it.

    Four normalisations, and each is a way `TrustedHostMiddleware` would
    silently refuse a name somebody meant. It compares `host == pattern`
    against `headers.get("host", "").split(":")[0]` (starlette 1.6.0,
    `middleware/trustedhost.py:40-44`), so:

    - **an empty entry is not dropped for tidiness.** `""` equals the `""` a
      request with no `Host` header at all produces, so a trailing comma would
      open the guard rather than narrow it — and open it to exactly the
      client `_from_our_page`'s absent-`Host` paragraph is about.
    - **a port never matches.** The header's is split off before the
      comparison and the pattern's is not, so `raspberrypi.local:8000` is a name no
      request can have.
    - **a capital letter never matches** either: there is no `lower()`
      anywhere in that file. Only the configured side can be fixed from here.
      A `Host` arriving in another case is refused, which is the safe
      direction to fail in and is why this is not worth a subclass.
    - a wildcard is passed through untouched, because that file supports one
      form of it — `*.example.com`, which matches a subdomain and not the bare
      name.

    Two edges are left as they are. IPv6 is broken upstream: that `split(":")`
    turns `[::1]:8000` into `"["`, so a `--host ::` deployment would refuse
    every request. And `*` anywhere in the list sets `allow_any`
    (`trustedhost.py:28`) and turns the guard off outright, which is said out
    loud below rather than refused — it is a thing somebody may mean, unlike
    `SESSION_RETAIN_S=0`, and a server that will not start is a worse answer
    to it than a server that starts and says what it did.
    """
    hosts = ["localhost", "127.0.0.1"]
    for entry in os.getenv("ALLOWED_HOSTS", "").split(","):
        name = entry.strip().lower()
        if name.count(":") == 1 and name.rsplit(":", 1)[1].isdigit():
            name = name.rsplit(":", 1)[0]
        if name and name not in hosts:
            hosts.append(name)
    # Said out loud because nothing says it per request: the middleware
    # answers a refusal with `Invalid host header` and writes no log line of
    # its own, so a panel that will not connect leaves nothing behind on
    # either side of the socket. This is what the person reading the log has.
    logger.info("answering to %s", ", ".join(hosts))
    if "*" in hosts:
        logger.error(
            "ALLOWED_HOSTS contains `*`, so this server answers to any name "
            "at all and the rebinding guard is off. Name the hosts instead.")
    return hosts


ALLOWED_HOSTS = _allowed_hosts()

# In front of the router, and that is the whole reason this is middleware
# rather than another line in the endpoint. Starlette builds the stack as
# `ServerErrorMiddleware`, then whatever was added here, then
# `ExceptionMiddleware`, then the router (`applications.py:63-83`), so this
# runs before any route resolves and before `_from_our_page` is called at all.
# The other order would be a non-fix that looks like one: the endpoint's own
# guard passes a rebound name, because under it the two headers agree.
#
# It does guard the handshake and not only `/api/`. `websocket` is in the
# tuple of scope types it acts on (`trustedhost.py:31-37`), and the refusal is
# a pre-handshake one rather than an accept followed by a close: `Response`
# wraps `send` when the scope is a websocket, so the 400 goes out as
# `websocket.http.response.start` (`responses.py:154-167`).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


# The Live session open on each conversation, and nothing where there is none.
#
# One per thread and not one per socket, because the thread is what a Live
# session writes into: ADK loads the session row when `run_live` starts
# (`runners.py:1963`) and appends every non-partial turn back to it, and
# `SqliteSessionService.append_event` refuses an append from a session object
# older than the row (`sqlite_session_service.py:443-448`, `StaleSessionError`).
# Two streams on one row therefore do not interleave — the second one to write
# dies, mid-answer, with nothing said on the page it was answering.
#
# A session id names a profile and not a socket, so two pages arming on one
# profile is not an exotic state: it is a kiosk plus a tab on the Mac, and it
# is also a reconnect, which re-arms from `app.js`'s `onopen` while the socket
# it replaced may not have reached its `finally` yet.
_LIVE_ON: dict[str, asyncio.Task] = {}


def _from_our_page(websocket: WebSocket) -> bool:
    """Whether this handshake came from the page this server serves.

    A WebSocket handshake is not a fetch. It sits outside the same-origin
    policy, it is never preflighted, and a browser will open one to any host a
    page names — so the content-type guard above, which works by making the
    browser ask permission first, cannot reach this route at all. What a
    browser does do is send `Origin`, and it sends the page's real one: script
    cannot set that header. That is the whole of what this reads.

    It is worth more than the route it guards. Anything that gets in here
    writes `home_location` into the household's conversation and can put words
    in the person's mouth, which the agent answers with `gws` in hand — the
    family's own Gmail, Calendar, Drive and Keep, writes included.

    Compared against our own `Host` rather than against a configured list.
    `app.js` builds the socket URL from `window.location.host`, so our page
    always connects to the origin it was served from and the two headers match
    by construction, with nothing anywhere having to be told what this terminal
    is called. That matters more than it sounds. There are two right answers
    today — the panel arrives through `ssh -L 8000:127.0.0.1:8000`, and
    `http://localhost:8000` and `http://127.0.0.1:8000` are different origins
    by the spec and both real — and a third the day somebody points a browser
    at the terminal by another name. A list of *client origins* would need
    editing at each of those, and a list whose failure mode is a panel that
    will not connect is a list somebody eventually sets to `*`. The allowlist
    above is not that list: it names the server rather than its callers, and
    under both topologies the server's name is `localhost`.

    The authority only, not the scheme. A proxy terminating TLS would put
    `https://` in the `Origin` of a page whose `Host` is unchanged, and
    dropping the scheme costs nothing: no browser will offer an `Origin` on a
    host and port it is not really on, so there is no attacker this would let
    through that the authority comparison catches.

    **No `Origin` at all is refused**, and that is a decision rather than a
    default. Only a browser is obliged to send one, so refusing costs no
    browser; what it costs is a non-browser client, and there is none —
    `app.js` is the only thing in the tree that opens this socket. What it buys
    is the other half of the same hole: `--host` still defaults to `0.0.0.0`,
    so until that changes, anything on the Wi-Fi with an HTTP client could open
    this socket and talk to the agent with no page involved at all. `null` goes
    the same way, which is what a sandboxed iframe and a `file://` page send.

    Both halves have to be there, which is why the absent `Host` is refused
    beside the absent `Origin` rather than compared as `""`. An empty authority
    is a real parse: `urlsplit("http://").netloc` is `""` too, so the two would
    agree and the comparison would say yes to a client that named neither. No
    browser and no HTTP/1.1 client can reach it — `Host` has been mandatory
    since RFC 2616 and uvicorn answers a request without one itself — but this
    guard is the last thing standing between the Wi-Fi and the family's mail,
    and a guard whose weakest case is unreachable *today* is one refactor away
    from being reachable tomorrow.

    What this cannot stop is DNS rebinding, where the browser is handed our
    address under the attacker's own name and then sends that name in *both*
    headers — so the two agree and this returns True. That is a question about
    who may name this server rather than who may reach it, and the answer to
    it is `_allowed_hosts` above rather than anything that can be done to this
    comparison. Neither guard covers the other's attack: an allowed `Host`
    under a foreign `Origin` is this one's, and a matching `Origin` under a
    name we do not answer to is the allowlist's.
    """
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host", "")
    if not origin or not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()


class _Page(NamedTuple):
    """One connection on a conversation, as far as a roll needs to know.

    `live` is whether it has a Live session open right now, which is the one
    state that makes rolling the thread out from under it unsafe. `move` puts
    it on another conversation, taking its four registry entries and its own
    idea of which thread it is in along with it.
    """

    live: Callable[[], bool]
    move: Callable[[str], None]


# Every connection on a conversation, keyed the way `conn.Registry` is: session
# id, then connection id. A session id names a profile and not a socket, so two
# pages on one profile share one id indefinitely — and `rethread` used to move
# only its own four registrations, leaving the others keyed to a thread nothing
# would ever push to again. The roll is a fact about the conversation, so every
# page on it has to be told.
_PAGES: dict[str, dict[str, _Page]] = {}

# The `turn_over` sends still in flight. `asyncio` holds only a weak reference
# to a task, so one created and dropped from a done callback can be collected
# before it has run; these are kept here until they finish. See `live_ended`.
_RELEASING: set[asyncio.Task] = set()



class _Roll:
    """The lock one person's rolls take turns on, and who is still on it.

    `rethread` is a check-then-act, and the act is two awaits past the check:
    it establishes that nobody else on this thread is live, then suspends in
    `create_session` and again in `append_event` before it reads `session_id`
    back and fans the roll out. Two pages on one profile arm from two tasks,
    so the loop is free to run the *other* page's whole `rethread` in between
    — and that page rolls the thread, arms on the new one, and leaves this one
    holding a `session_id` that has already moved. The fan-out then lands on
    the conversation that page's `run_live` is streaming into and takes every
    screen off it: nothing draws, the wake word reaches nobody, and the log
    says a roll succeeded. The self-only re-key this replaced left one screen
    on the live thread; the fan-out leaves none. See #159.

    `waiting` counts the holders and the queue, so the entry goes when the last
    of them does. Popping on `lock.locked()` alone would not do it — `release`
    clears the flag before it wakes the next waiter, so a page arriving in that
    instant would find no entry, mint a second lock, and be serialised against
    nobody, which is the bug again with an extra step in front of it.
    """

    __slots__ = ("lock", "waiting")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.waiting = 0


# Per person rather than one for the server: a roll is a fact about one
# profile's conversation, and two households arming in the same second have
# nothing to take turns over. Emptied the way `_PAGES` is — `user_id` is a path
# segment, so a dict that only ever grew would be one more thing a page could
# add to for as long as the server is up.
_ROLLS: dict[str, _Roll] = {}


@contextlib.asynccontextmanager
async def _one_roll_at_a_time(user_id: str):
    """Serialise `user_id`'s rolls, so a check still holds when it is acted on.

    Taking turns rather than re-checking after the awaits, which is the smaller
    shape and the one that does not close the family. A re-check has to be
    right about *every* fact the roll rests on — the thread id, whether anybody
    is live on it, and the idle gap, which by then was measured against a row
    that is no longer this connection's thread — and it has already created the
    session it then has to abandon. That orphan is not free: `create_session`
    and the recap clear both stamp `last_update_time`, so the abandoned thread
    is the newest row for the profile and `_thread` hands it to the next page
    that connects, in place of the conversation the kiosk is holding. A lock
    leaves nothing to abandon and nothing to re-derive.

    Cheap enough to be uninteresting: what it serialises is one `list_sessions`
    and, only on a roll, one `create_session`, one state write and the retention
    sweep, plus the `_settle` query and the `create_task` that follow it in
    `arm` — all off the model and all against the local sqlite store. Two pages arming together is
    already the rare case, and the second of them waits for that and nothing
    else; the Live connection itself is opened by the task and not under the
    block.

    Taken twice by one arm, in sequence and never nested: `rethread` holds it
    for the decision and `arm` re-takes it for the two lines that make that
    decision true for everybody else. Held across both instead would be a
    nested acquire on a lock that is not re-entrant, which hangs every arm.
    """
    roll = _ROLLS.setdefault(user_id, _Roll())
    roll.waiting += 1
    try:
        async with roll.lock:
            yield
    finally:
        roll.waiting -= 1
        if not roll.waiting:
            _ROLLS.pop(user_id, None)


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str) -> None:
    """Stream audio and text between one browser and one Live API session.

    The socket outlives the session. It is opened when the page loads and kept
    for as long as the page is up, because it is the only thing `/api/wake` can
    reach a kiosk through: the screen sink is registered on this connection, so
    a terminal that closed its socket to save a Live session could never be
    told to open one again. The Live session starts and stops underneath it on
    the page's `arm` and `disarm` frames.
    """
    # First, and before `accept`: everything under this line is a side effect
    # on somebody else's conversation, and a refusal that accepts and then
    # closes has already run them. Closing while the connection is still
    # CONNECTING is what makes it a refusal — Starlette forwards
    # `websocket.close` from that state without accepting first
    # (`websockets.py`, `WebSocket.send`), and uvicorn answers the handshake
    # with a 403 rather than completing it.
    if not _from_our_page(websocket):
        logger.warning(
            "refused a handshake for %s: origin=%s host=%s", user_id,
            websocket.headers.get("origin") or "(none)",
            websocket.headers.get("host") or "(none)")
        await websocket.close(code=1008, reason="cross-origin handshake")
        return

    await websocket.accept()

    # Where the terminal was told it is — a spot set by pin in the settings
    # sheet and reverse-geocoded to an address. It travels with the handshake
    # because the Live API takes its system instruction once, at setup. Empty
    # means nobody has set it, and HOME_LOCATION from .env stands in.
    home_location = websocket.query_params.get("location", "").strip()
    # The coordinates travel too, because reverse-geocoding threw away the
    # precision the weather tool wants and re-geocoding the address does not
    # get it back. The address is for the prompt; these are for the tools.
    home_coords = _coords(websocket.query_params)
    # Travels with the handshake for the same reason the location does, and
    # with less choice about it: the voice is part of the Live API's setup
    # message and there is no client message that carries a new one
    # (`genai/types.py:21432-21447` — setup, content, realtime input, tool
    # response, and nothing else). Changing it is a new socket, which is what
    # `reopen` in `app.js` is for.
    voice = _voice(websocket.query_params)
    kiosk = _is_kiosk(websocket.query_params)
    interrupt = _is_interrupt(websocket.query_params)
    # The panel's own language, off `<html lang>` — the setting someone chose
    # in the settings sheet (#96). It rides the handshake with the rest rather
    # than being read per tool call, because it is a setting and not a turn:
    # what changes it is the picker, and the picker reopens, exactly as the
    # voice's does. That is the only way a new setting gets down here — this
    # key is written at accept and nothing later in the connection moves it.
    # What it is for is `_lang.of_turn`, the floor under a model that left a
    # tool's `lang` empty (#187).
    panel_lang = normalize(websocket.query_params.get("lang", ""))
    # Sized, because this fires on every accept — every page load, every
    # reconnect, with nothing having gone wrong — and `home_location` is the
    # street address the panel reverse-geocoded. That makes it the loudest
    # household-content line in the server, into a journal kept for a month
    # that the *Forget* button cannot clear (#226). The coordinates go to
    # `set` / `(none)`, because whether the panel has been pinned is the whole
    # question anyone asks of that field.
    logger.info(
        "client connected: user=%s location=%s coords=%s voice=%s kiosk=%s "
        "lang=%s",
        user_id, _sized(home_location) if home_location else "(none)",
        "set" if home_coords else "(none)", voice,
        kiosk, panel_lang or "(none)",
    )

    # And to the clock, which has no socket of its own and used to be the one
    # thing on the panel the map pin did not move (#131). Kept rather than
    # read per request, because the refresh loop runs on a timer with no page
    # attached; the argument for trusting it here is in `screensaver.where`.
    #
    # `kiosk` goes with it because that state is per process while this
    # handler is per socket, and the two are not the same thing: without it
    # the last page to connect owns the panel's clock, and a tab on the Mac
    # with a pin of its own moves the kitchen's weather to its town. Which
    # page wins is `set_home`'s to decide, not this line's.
    if screensaver.set_home(home_coords, home_location, kiosk):
        logger.info("screensaver: the panel says where it is (%s)",
                    _sized(home_location) if home_location else "(unnamed)")

    state = {}
    if home_location:
        state["home_location"] = home_location
    if home_coords:
        state["home_coords"] = home_coords
    if panel_lang:
        state["panel_lang"] = panel_lang

    session_id = await _thread(user_id, state)

    # The Live session, or None while the terminal is asleep. A page that has
    # just loaded is not talking to the model and must not hold a connection to
    # it: the kiosk is up all day in an empty kitchen, and every one of those
    # connections replays the thread at setup and grows a context nobody is
    # adding to.
    live_request_queue: LiveRequestQueue | None = None
    live_task: asyncio.Task | None = None
    self_cancelled: set[asyncio.Task] = set()

    # When the page last told us its screen had gone back to being a clock, or
    # None if it has not since the current thread began. The next wake word
    # rolls on it — see `went_dark`. Kept here rather than written to the store
    # because it is a fact about this page and this socket, and the only reader
    # is `rethread` below.
    dark_at: float | None = None

    # Two writers now share this socket — the event stream and the frames a
    # tool that answers late draws — and Starlette's send is not safe to
    # interleave.
    sending = asyncio.Lock()

    async def send(text: str) -> None:
        async with sending:
            await websocket.send_text(text)

    async def to_screen(frame: dict) -> None:
        await send(json.dumps(frame))

    async def drop_screen() -> None:
        """Close this socket, because it stopped taking frames.

        Handed to `screen.register` and called by nothing else. A page that has
        not taken a card in `screen.DEADLINE_S` is off the session by the time
        this runs, and a page nobody draws on that does not know it is the #138
        bug again — so it is told the only way a WebSocket can say anything,
        which is by closing. `app.js` reconnects on `onclose` and registers a
        screen again, so a laptop that was asleep comes back with a live panel
        rather than a dead one.

        1011 rather than 1000: the server is giving up, the page did not ask to
        go, and `app.js` treats every close the same way anyway.
        """
        logger.info("closing a socket for %s that stopped taking frames",
                    user_id)
        await websocket.close(code=1011)

    # Who this connection is, for everything that runs under this task. A
    # session id names the profile and not the socket, so without this the
    # three registries below cannot tell the kiosk from a tab on the Mac that
    # is looking at it — see :mod:`.conn`. Set here rather than scoped, because
    # the scope wanted is this handler's task and this is it.
    conn_id = conn.arrive()

    # Each of these hands back what it just put in the registry, and each is
    # given it back at the end. The identity is the point: the session id is
    # stable now, so a reload can open the next connection before this one's
    # `finally` has run, and an unconditional teardown would then unregister the
    # screen and cancel the jobs of a connection that has only just started.
    jobs = browse.open_session(session_id, conn_id)
    asking = gws.open_session(session_id, conn_id)
    planning = plan.open_session(session_id, conn_id)
    screen.register(session_id, to_screen, kiosk, drop_screen)

    def live_now() -> bool:
        """Whether this connection is holding a Live session open."""
        return live_task is not None and not live_task.done()

    def move(fresh_id: str) -> None:
        """Put this connection on `fresh_id`, four registries and all.

        The body `rethread` used to run inline, lifted out so that a roll can
        run it for every page on the conversation and not only for the one that
        armed. Everything it touches is this connection's own — its entry in
        each registry, its screen, and its idea of which thread it is in — so
        it is safe to call from another connection's task, which is where the
        roll happens.
        """
        nonlocal session_id, jobs, asking, planning, dark_at
        rolled = session_id
        browse.close_session(rolled, jobs)
        gws.close_session(rolled, asking)
        plan.close_session(rolled, planning)
        screen.unregister(rolled, to_screen)
        pages = _PAGES.get(rolled)
        if pages is not None:
            pages.pop(conn_id, None)
            if not pages:
                _PAGES.pop(rolled, None)
        session_id = fresh_id
        jobs = browse.open_session(session_id, conn_id)
        asking = gws.open_session(session_id, conn_id)
        planning = plan.open_session(session_id, conn_id)
        screen.register(session_id, to_screen, kiosk, drop_screen)
        _PAGES.setdefault(session_id, {})[conn_id] = page
        # The mark went with the thread. `roll` clears the mark of the page
        # that caused it and nothing else clears anyone's, so without this a
        # page carried across by the fan-out keeps a darkness taken on a
        # conversation it is no longer in — and its next wake word reads that
        # as "this thread went dark" and rolls away the one it has just been
        # put on. Two pages, three threads, and whatever the other page had
        # started left behind on the second. #142.
        dark_at = None

    # The fifth registration, and the one that makes the other four movable
    # from outside this task. Filed under the thread rather than the profile:
    # a roll moves every idle page on the old conversation to the fresh one,
    # leaving mid-turn ones behind. Two pages on one profile can diverge since
    # `_thread` gives a page that loads after the gap a conversation of its own.
    page = _Page(live_now, move)
    _PAGES.setdefault(session_id, {})[conn_id] = page

    async def upstream() -> None:
        """Browser -> Live API."""
        try:
            while True:
                message = await websocket.receive()

                if message.get("type") == "websocket.disconnect":
                    break

                if "bytes" in message:
                    # Dropped rather than queued while asleep. The recorder
                    # posts a block every 8 ms whether or not anyone is
                    # speaking, so audio that arrived before the `arm` was
                    # processed is a fraction of a syllable, and holding it for
                    # a session that may never open is a leak with a microphone
                    # on the end of it.
                    if live_request_queue is not None:
                        live_request_queue.send_realtime(types.Blob(
                            mime_type="audio/pcm;rate=16000",
                            data=message["bytes"],
                        ))
                    else:
                        logger.warning("dropped an audio frame: nothing is armed")
                    continue

                if "text" not in message:
                    continue

                payload = json.loads(message["text"])
                kind = payload.get("type")
                if kind == "arm":
                    await arm()
                elif kind == "disarm":
                    await disarm()
                elif kind == "sleep":
                    await went_dark()
                elif live_request_queue is None:
                    # Said out loud, because the alternative is a question
                    # typed into a terminal that answers nothing and logs
                    # nothing. Every page path that speaks arms first
                    # (`armSession` in `app.js`); one that forgot to is the
                    # bug this line names.
                    logger.warning("dropped a %s frame: nothing is armed",
                                   kind)
                elif kind == "text":
                    live_request_queue.send_content(types.Content(
                        parts=[types.Part(text=payload["text"])],
                    ))
                elif kind == "image":
                    live_request_queue.send_realtime(types.Blob(
                        mime_type=payload.get("mimeType", "image/jpeg"),
                        data=base64.b64decode(payload["data"]),
                    ))
        except (WebSocketDisconnect, RuntimeError):
            logger.info("client disconnected (upstream)")

    async def downstream(queue: LiveRequestQueue, thread: str) -> None:
        """Live API -> browser, one JSON-encoded ADK event per frame.

        Takes the queue it was started with rather than reading the one above,
        so a disarm that has already dropped the reference cannot leave this
        pumping into a queue nobody reads. The thread is passed the same way
        and on the same principle: `session_id` moves now — `rethread` below
        reassigns it — and a stream that read it when its first event arrived
        rather than when it was started would finish into the conversation
        that replaced it.
        """
        async for event in runner.run_live(
            user_id=user_id,
            session_id=thread,
            live_request_queue=queue,
            run_config=_run_config(voice, interrupt=interrupt),
        ):
            _log_tool_traffic(event)
            try:
                await send(event.model_dump_json(exclude_none=True, by_alias=True))
            except (WebSocketDisconnect, RuntimeError):
                logger.info("client disconnected (downstream)")
                break

    async def turn_over() -> None:
        """Tell the page the turn is finished, because the model will not.

        The panel puts a spinner up when it hears the question — `startTurn`
        and `showWorking` in `app.js` — and takes it down on `turnComplete`,
        on `interrupted`, and on nothing else (`app.js:1128`, `:1132`). Those
        are all events out of `run_live`, so a Live session that died mid-turn
        sends none of them: the log has the traceback and the kitchen has a
        panel that spins until somebody reloads it, which on a kiosk is
        nobody. #269.

        And signal the page to go idle. Dropping the session reference on the
        server leaves the page's `armed` and `listening` flags set, so the
        microphone keeps streaming audio into the void and the wake word is
        ignored until `WAKE_IDLE_S` (30 s) expires. #285.

        Suppressed rather than logged, because the two failures that end a
        session most often — the socket dropped, the server going down — are
        the same two that make this send fail, and a stack trace saying the
        page is gone under an error that already says why is noise.
        """
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await to_screen({"turnComplete": True})
            await to_screen({"type": "idle"})

    def live_ended(task: asyncio.Task) -> None:
        """Note a Live session that stopped without being asked to.

        A task nobody awaits keeps its exception to itself, and the one that
        matters here is the ordinary one — a 429, a key that expired overnight,
        the WAN down. Without this the reference stays set, the next `arm`
        finds a session already open, and the terminal is deaf until somebody
        reboots it. Clearing it lets the next wake word try again, which on a
        429 is the right answer a minute later.
        """
        nonlocal live_task, live_request_queue
        # Off the conversation's registry first, whichever way it ended and
        # whichever thread it was opened on — `session_id` moves under a roll,
        # so the key cannot be looked up and has to be matched by identity.
        # Left behind, the entry refuses the next `arm` on that thread the
        # takeover it no longer needs, and holds a finished task forever.
        for held_id, held in list(_LIVE_ON.items()):
            if held is task:
                del _LIVE_ON[held_id]
        if task is live_task:
            live_task, live_request_queue = None, None
        def release() -> None:
            # Held in a set until it is done, because the loop keeps only a
            # weak reference to a bare `create_task` and is free to collect it
            # mid-send — a spinner that stops on most failures and not on the
            # busy ones is worse than one that never stops, because nobody
            # goes looking for it.
            released = asyncio.create_task(turn_over())
            _RELEASING.add(released)
            released.add_done_callback(_RELEASING.discard)

        if task.cancelled():
            # Another page's `take_over`, not our own `disarm`: this page is
            # still armed and listening, and deaf until WAKE_IDLE_S. #285, #291.
            if task not in self_cancelled:
                release()
            self_cancelled.discard(task)
            return
        if task.exception() is not None:
            logger.error("live session for %s ended: %s", session_id,
                         task.exception(), exc_info=task.exception())
        release()

    async def roll(why: str) -> None:
        """Start the next conversation under this socket, bare.

        Split out of `rethread` because there are two rules that end a
        conversation now and only one way of ending it. `rethread` owns the
        recency rule, `went_dark` owns the screensaver's, and what both need is
        this: a fresh id, the recap cleared off it, and every page on the old
        thread moved onto it.

        Bare means bare, and the append below is the line that makes it so.
        The recap's key is `user:`-prefixed, so it is filed under the person
        rather than the session and a new thread comes up already holding it —
        see `_carry_forward`, whose "both are written even when there is
        nothing to write" is the clear this path skipped along with the model
        call. Without this the fresh thread reads the *last accept's* summary
        under "What was said before", so the socket that stayed up for three
        days answers Thursday with Sunday's conversation named as Wednesday's,
        and keeps doing it on every later roll. A wrong memory is worse to talk
        to than none.

        Marked on this thread rather than cleared off the person, and that is
        the whole of the difference. `RECAP_KEY: ""` here empties the one slot
        the profile has, which every other page of theirs reads through — and
        the other page is routinely on another thread, because an accept that
        crosses the gap rolls without telling the sockets already up. So a
        wake word at the kiosk took the summary `_carry_forward` had just
        bought for the tab, and the tab's next arm opened with no "What was
        said before" where its last one had it. `BARE_KEY` is a plain key, so
        it stays on this row. Either way it is a state write and not a model
        call, so it costs the wake word nothing.

        Every page's registrations re-keyed and not only this one's, which is
        #159's rule and applies whichever of the two ended the conversation: a
        tool resolves the session it draws on from ADK's context —
        `screen.session_of` reads `tool_context.session.id` — and that is the
        new id from the next event onwards. Leave the other page's and it is
        still up, still connected, and on a thread nothing will ever push to.
        Over a copy, because `move` takes each page out of `_PAGES[rolled]` on
        its way across.

        The thread being left behind is not deleted. It keeps every event it
        had and goes on ageing out under `RETAIN_S` like any other; what
        changes is only which one the next connection replays.

        Which is what the sweep at the end is for. `_retire` otherwise runs
        only at accept, and a kiosk socket does not re-accept for days — so on
        the one terminal that is never reloaded, the retention window was
        applied only when something forced a reconnect. Running it here is
        where it belongs: the roll is the moment a thread stops being the
        current one, and every thread this profile has just fell one further
        behind. Kept: the fresh thread and the one it replaced, the same pair
        `_thread` protects, because a roll is not a reason to lose the
        conversation somebody was having a minute ago. Last, after the fan-out,
        so nothing about the roll waits on it — and `_retire` raises nothing,
        because an arm is not a place to fail over an old row. #173.
        """
        nonlocal dark_at
        rolled = session_id
        fresh = await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, state=state)
        # Cleared here and not in `went_dark`, so that the mark survives
        # anything that stops a roll happening — `create_session` raising, the
        # other page on this thread still being live, the socket going away
        # between the frame and the wake word — and the next arm tries again
        # rather than quietly resuming the thread the screensaver already
        # ended. Cleared before the append rather than after, because from the
        # line above onwards the roll has happened as far as this page is
        # concerned and a retry would only mint a second empty thread.
        dark_at = None
        await session_service.append_event(fresh, Event(
            author="user",
            actions=EventActions(state_delta={recap.BARE_KEY: True})))
        for held in list(_PAGES.get(rolled, {}).values()):
            held.move(fresh.id)
        logger.info("rolled %s to %s for %s (%s), no recap", rolled,
                    session_id, user_id, why)
        await _retire(user_id, {rolled, fresh.id})

    async def went_dark() -> None:
        """The screen has gone back to being a clock, so the talk is over.

        The rule this serves is the household's, not this file's: when the
        panel stops showing a conversation, that conversation is finished, and
        the next wake word starts a new one. Until it existed `IDLE_GAP_S` was
        the only thing that ever rolled a thread, and half an hour of silence
        is rare in a kitchen — so a terminal spoken to on and off through a day
        never rolled, and every wake word after the first replayed everything
        said before it into a fresh Live connection. The arm cost whatever the
        day had cost so far. #142.

        The page has to say it because the server cannot see it. The two idle
        clocks are measured against different things: `IDLE_MS` in `app.js`
        resets on a touch, so somebody reading a recipe off the panel keeps it
        awake without a word, while `IDLE_GAP_S` is measured off
        `last_update_time`, which only moves when an event is appended. There
        is no arithmetic on this side that turns one into the other.

        A mark and not a roll. Writing the new thread here would mint a session
        row every time a screen dimmed, including in the kitchens nobody ever
        speaks in, and every one of those rows is a row the retention sweep and
        `_thread`'s `list_sessions` walk on the path of every page load. Marked
        instead, the roll happens on the next wake word — which is the one
        moment the roll is worth anything — and a screen that goes dark and
        stays dark writes nothing at all.

        Refused while this connection has a session open, which the page should
        never let happen: `sleep` in `app.js` bails out to `wake()` on a live
        microphone, a card still spinning or a browse in flight. If the two
        ever disagree, marking would be a roll under a running `downstream`
        task at the next wake word. Dropping the frame instead costs one missed
        roll and heals itself: the page's countdown was restarted by that same
        `wake()`, `WAKE_IDLE_S` closes the microphone long before it, and the
        next `sleep` lands with nothing armed.

        Only this connection's session, because the mark is only about this
        page's screen. Another page being live is checked where it matters, at
        the roll in `rethread`, which is the same place #159 checks it for the
        gap.
        """
        nonlocal dark_at
        if live_now():
            logger.warning("ignored a sleep frame for %s: still armed",
                           session_id)
            return
        if dark_at is None:
            dark_at = time.time()
            logger.info("%s went dark; the next wake word starts over",
                        session_id)

    async def rethread() -> None:
        """Start a new conversation if this one is over.

        `_thread` runs once, at accept, and that was enough while every
        conversation began with a page load. It is not enough now: the socket
        stays up for days and the wake word arms the microphone underneath it,
        so without this a kiosk answers Thursday night into Tuesday morning's
        thread and `IDLE_GAP_S` quietly stops applying. The symptom is not an
        error — it is context nobody asked for, with nothing on the screen or
        in the log to say the rule lapsed.

        Not `_thread` itself, deliberately. Crossing the gap in there calls
        `_carry_forward` -> `recap.summarise`, which is a real model call, and
        the wake word is the worst trigger there is for one: every "hey
        terminal" would buy a summary before the person finished the sentence.
        The recap is also worth least on this path — somebody who walks up
        after eight hours of quiet is starting something, not continuing it —
        so this rolls bare, and says so in the log.

        Called from `arm`, and only after `arm` has established that no session
        is open. Rolling the thread out from under a live conversation would be
        the same bug pointed the other way — and "a live conversation" means
        anybody's on this thread, not only this connection's, which is what the
        `live()` test below is for.

        Two rules end a conversation and this is where they meet. The
        screensaver's is checked first and short-circuits the query: a page
        that has already told us the room emptied has settled the question, and
        `last_update_time` cannot unsettle it. The gap is what is left for a
        page that never slept — a tab on a Mac, a kiosk somebody has been
        touching all afternoon — and it stays the ceiling for those. #142.

        One at a time per person, and that is what makes the `live()` test mean
        anything. What is below the lock is a check, two awaits, and then an
        act — and `session_id` is a closure local that another connection's
        roll writes to, so without taking turns the thread rolled is not the
        thread that was checked. See `_Roll`.
        """
        async with _one_roll_at_a_time(user_id):
            # Every other page on this thread, computed once because both
            # rules need it. #159 established that a roll may not run under
            # somebody else's Live session; the screensaver's rule needs that
            # guard just as much and more visibly, because there the mark
            # belongs to one page and the session being rolled away belongs to
            # another. Refused and not dropped: `dark_at` stays set, so the
            # next wake word tries again once the other page has finished.
            others = [held for held_id, held
                      in _PAGES.get(session_id, {}).items()
                      if held_id != conn_id]
            busy = any(held.live() for held in others)
            if dark_at is not None:
                if busy:
                    logger.info(
                        "not rolling %s for %s: the screen went back to the "
                        "clock, but another page on it is live",
                        session_id, user_id)
                    return
                await roll("the screen went back to the clock "
                           f"{(time.time() - dark_at) / 60:.0f} min ago")
                return
            listed = await session_service.list_sessions(
                app_name=APP_NAME, user_id=user_id)
            mine = next((s for s in listed.sessions
                         if s.id == session_id), None)
            # `list_sessions` and not `get_session`: it carries no events and
            # no state, and the one column this wants is on the row either way.
            # The sqlite backend writes it from every appended event
            # (`sqlite_session_service.py:501`), so it is the time somebody
            # last said something and not the time the socket came up.
            quiet = (time.time() - mine.last_update_time
                     if mine is not None else None)
            if quiet is not None and quiet <= IDLE_GAP_S:
                return
            # Not while somebody else on this thread is mid-conversation. The
            # gap is measured on the row, and a page that armed and then said
            # nothing for half an hour leaves the row looking exactly as quiet
            # as an empty kitchen does — so without this, one page's wake word
            # rolls the thread another page's Live session is streaming into,
            # and every card and every late answer that run produces is keyed
            # to a thread with no screen left on it. Only where the row is
            # still there: `quiet is None` means it was swept out, and arming
            # against an id `run_live` cannot load is worse than interrupting
            # anybody.
            if quiet is not None and busy:
                logger.info(
                    "not rolling %s for %s: another page on it is live",
                    session_id, user_id)
                return
            # Gone counts as well as quiet, because the remedy is the same one
            # and the alternative is arming against an id `run_live` cannot
            # load. A socket up for days outlives the retention window, and the
            # sweep that takes the row out runs on another connection's accept.
            await roll(f"{quiet / 60:.0f} min of quiet" if quiet is not None
                       else "the thread was swept out from under us")

    async def take_over(target_id: str | None = None) -> None:
        """Stop any other connection's Live session on this conversation.

        One `run_live` per thread is not a preference, it is what the store
        will take: each stream loads a `Session` of its own when it starts and
        appends every non-partial turn through it, and the SQLite backend
        rejects an append whose object is older than the row. So two streams on
        one thread is not two conversations sharing a history — it is one
        working stream and one that raises `StaleSessionError` on its first
        turn and ends there, with the page it was answering still showing a lit
        microphone and no answer coming.

        The newest arm wins, which is the rule the rest of this file already
        follows — `screen.sink`, `screen.wake_sink` and `conn.own` all prefer
        the connection that arrived last — and it is the answer the common case
        wants: the arm that has to take one over is nearly always a reconnect
        re-arming (`app.js`, `onopen`) under a socket whose `finally` has not
        run yet, and refusing it would leave the page that is actually up mute
        until the dead one was reaped.

        Awaited, so the old stream's connection is gone before the new one
        opens, for the same reason `disarm` awaits its own.
        """
        sid = session_id if target_id is None else target_id
        held = _LIVE_ON.get(sid)
        if held is None or held is live_task or held.done():
            return
        logger.info("taking the Live session on %s over from another page",
                    sid)
        held.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await held

    async def arm() -> None:
        """Open a Live session under this socket, if one is not open already.

        Idempotent because the page can mean it twice: the microphone button
        and a wake word are the same request, and the socket reconnecting under
        a microphone that is still on sends it again on purpose.
        """
        nonlocal live_request_queue, live_task
        if live_task is not None and not live_task.done():
            return
        old_id = session_id
        await rethread()
        if old_id != session_id:
            await take_over(old_id)
        await take_over(session_id)
        # Under the roll lock as well, because `live_task` below is the fact
        # every other page's `rethread` decides on and it is the last thing
        # this one sets. Without this the two are two awaits apart: a page
        # that has committed to a Live session and not yet opened one reads
        # as idle, so another page arming in that window is not refused the
        # roll the guard exists to refuse, and rolls the conversation out
        # from under it. #159.
        #
        # Two acquisitions in a row rather than one around both, because the
        # lock is a plain `asyncio.Lock` and `rethread` takes it itself —
        # wrapping the line above instead gives a nested acquire on the same
        # `user_id`, and every arm hangs.
        async with _one_roll_at_a_time(user_id):
            # After `rethread`, so a thread it has just rolled away is not the
            # one read: a fresh thread has nothing dangling on it, and this
            # returns on the first query.
            await _settle(user_id, session_id)
            live_request_queue = LiveRequestQueue()
            live_task = asyncio.create_task(
                downstream(live_request_queue, session_id))
            # Claimed before the callback is attached, so that a stream which fails
            # immediately cannot have its entry cleared by `live_ended` before it
            # is written.
            _LIVE_ON[session_id] = live_task
            live_task.add_done_callback(live_ended)
        logger.info("armed %s for %s", session_id, user_id)

    async def disarm() -> None:
        """Close it again, leaving the socket, the screen and the thread up.

        Cancelled rather than closed, and the difference is not style.
        `live_request_queue.close()` puts a close request in front of the flow,
        which closes the connection from underneath `_receive_from_model` — and
        ADK 2.8.0 then *reconnects* rather than returning whenever a resumption
        handle has arrived (`flows/llm_flows/base_llm_flow.py:859-869`), which
        `_run_config` asks for on both model paths. Cancelling instead unwinds
        the `aclosing` blocks `run_live` is built out of
        (`runners.py:2007-2011`), and those close the connection on the way
        out. Awaited, so the connection is gone before this returns and the
        next arm cannot overlap it.
        """
        nonlocal live_request_queue, live_task, jobs, asking, planning
        task, live_task = live_task, None
        live_request_queue = None
        # A session that has already stopped needs no stopping, and awaiting it
        # here would re-raise the failure `live_ended` has already reported —
        # taking the socket down with it, on the one path whose whole job is to
        # keep the socket up.
        if task is None or task.done():
            return
        self_cancelled.add(task)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        gws.close_session(session_id, asking)
        browse.close_session(session_id, jobs)
        plan.close_session(session_id, planning)
        jobs = browse.open_session(session_id, conn_id)
        asking = gws.open_session(session_id, conn_id)
        planning = plan.open_session(session_id, conn_id)
        logger.info("disarmed %s", session_id)

    try:
        # One frame, before anything is asked of the page, saying that every
        # unguarded call on the accept path came back: the conversation above
        # and the four registries under it. It exists for the reconnect backoff
        # in `app.js`, which otherwise has only wall-clock uptime to judge the
        # server by and cannot tell a socket a Wi-Fi blip took down from one
        # that died in this window — so a run of short-lived connections walks
        # the ladder to its ceiling and leaves the panel `Offline` for up to a
        # minute after the server is well again.
        #
        # Inside the `try` rather than beside `screen.register`, so that a page
        # that went away between the accept and here is a send that raises with
        # the `finally` still to run. Outside it, the registries this handler
        # just opened would be left behind.
        #
        # After the registries and not before: a frame sent earlier would tell
        # the page a server is healthy while the call that is actually likely
        # to fail has not been made yet, which is the backoff it is meant to
        # protect being reset by the outage it is meant to back off from.
        await to_screen({"type": "ready"})
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id)
        if session and session.events:
            turns = feed_turns(session.events)
            if turns:
                await to_screen({"type": "history", "turns": turns})
        await upstream()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("streaming failed")
    finally:
        # The session goes first: the three tool registries below cancel work
        # that is still running, and a Live connection left open would keep
        # feeding function responses into a conversation nobody is watching.
        await disarm()
        browse.close_session(session_id, jobs)
        gws.close_session(session_id, asking)
        plan.close_session(session_id, planning)
        screen.unregister(session_id, to_screen)
        pages = _PAGES.get(session_id)
        if pages is not None:
            # By id and not by value: the entry is this connection's own, and
            # a reload's next connection has an id of its own, so there is
            # nothing here to take from somebody else. The key goes with the
            # last page, so `_PAGES` does not grow a row per rolled thread.
            pages.pop(conn_id, None)
            if not pages:
                _PAGES.pop(session_id, None)


def _is_loopback(host: str) -> bool:
    """Whether a bind address reaches this machine and nothing else.

    Three spellings reach only here. Everything else is an address on a real
    interface, including uvicorn's two wildcards — `0.0.0.0`, `::` — and the
    empty string, which it reads as the same thing.
    """
    return host in ("127.0.0.1", "localhost", "::1")


def _is_wildcard(host: str) -> bool:
    """Whether a bind address is *every* interface rather than a chosen one.

    The three spellings the paragraph above calls uvicorn's wildcards, named
    here because the warning below has to tell them from a deliberate
    `--host 192.0.2.20`. Both are off loopback and both reach the Wi-Fi,
    but one of them is a person who looked up an address and the other is the
    thing somebody types when they want it to work.
    """
    return host in ("0.0.0.0", "::", "")


def _parser() -> argparse.ArgumentParser:
    """The CLI on its own, so a default can be read without binding a port.

    `run` below is the only caller. It is split out because `run` ends in
    `uvicorn.run`, which takes the socket and does not come back — there is no
    other way to ask what this program would listen on.
    """
    parser = argparse.ArgumentParser(description="Live voice agent server")
    # Loopback, because that is where the only client lands. The panel reaches
    # this through the Pi's `ssh -N -L 8000:127.0.0.1:8000` (see the deployment
    # section of the README), which terminates on the Mac's loopback, so the
    # listener on every other interface served nobody and only widened what a
    # page or a device on the same network could reach. What it reached is not
    # small: the WebSocket asks for no credential of any kind, and
    # `/api/forget` destroys a person's history on a POST.
    #
    # Anyone who wants the old behaviour passes `--host 0.0.0.0` and has said
    # so out loud. #10 will need exactly that — once the server is on the Pi
    # the kiosk browser is a different host and loopback stops being an
    # option, at which point the origin check is the whole of the defence
    # rather than the second layer it is here.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    # TLS, because the third topology needs it and neither of the first two
    # does. Loopback and the Pi's `ssh -L` both put the browser on an origin
    # the platform already trusts, so plain HTTP has been enough; a browser on
    # *another machine* is not, and the difference is the microphone.
    # `getUserMedia` and `AudioWorklet` are secure-context only, `localhost` is
    # a secure context by fiat and `http://192.168.x.y:8000` is not — so from
    # the second machine the page loads, the mic button does nothing, and
    # nothing in this server's log says why.
    #
    # Named here rather than left to bare uvicorn. `uvicorn
    # ai_terminal.live.main:app --ssl-certfile …` does work — the app and its
    # allowlist are both built at import — but it is a thing somebody has to
    # work out again every time, and it silently drops the two lines below
    # that are the only account of what binding off loopback costs. See #245.
    parser.add_argument("--ssl-certfile")
    parser.add_argument("--ssl-keyfile")
    # For an encrypted key. Prefer a mode-600 key with no passphrase: this one
    # is on the command line, so it is in the shell's history and in every
    # `ps` on the machine for as long as the server is up.
    parser.add_argument("--ssl-keyfile-password")
    return parser


def run() -> None:
    """Console-script entry point."""
    import uvicorn

    parser = _parser()
    args = parser.parse_args()
    # Refused here because uvicorn refuses it worse. Measured against uvicorn
    # 0.52.4: `Config.load` answers a `--ssl-keyfile` with no certificate with
    # a bare `AssertionError` carrying no message at all, and a
    # `--ssl-certfile` with no key with `[SSL] PEM lib` out of
    # `load_cert_chain`. Neither names the flag that is missing, and both
    # arrive after the log has already said which port it is serving on.
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error("--ssl-certfile and --ssl-keyfile go together")
    tls = bool(args.ssl_certfile)
    logger.info("serving on %s://%s:%d",
                "https" if tls else "http", args.host, args.port)
    # Said out loud, because the case worth noticing is the one that widens
    # it. A `--host 0.0.0.0` typed once into a shell is otherwise invisible for
    # as long as the process lives, and this process lives for days.
    if not _is_loopback(args.host):
        logger.warning(
            "%s accepts connections from other machines, and this server "
            "asks nobody for a credential", args.host or "0.0.0.0")
    # And louder for the one configuration that looks safe and is not. TLS on
    # a wildcard bind reads as the hardened option — there is a certificate,
    # the browser shows a padlock, the word is *secure* — and every bit of
    # that is about the wire. None of it is a door. What the padlock buys is
    # the microphone, because the browser will not open one on an insecure
    # origin; what it costs is that anyone else on the Wi-Fi now reaches an
    # agent that asks for no credential, holds the household's Gmail,
    # Calendar, Drive and Keep with writes included, and destroys a person's
    # history on a POST to `/api/forget`. `error` rather than `warning` so it
    # survives a scroll, and it names the exit because leaving this running is
    # the actual risk: the flags are typed for a demo and the process lives
    # for days. A chosen address gets the warning above and not this — that is
    # somebody who looked an interface up, not somebody who wanted it to work.
    if tls and _is_wildcard(args.host):
        logger.error(
            "TLS on every interface is an encrypted door with no lock: it "
            "buys the microphone on other machines and admits everyone on "
            "the network. Bind one address instead, and stop this process "
            "when you are done with it")

    uvicorn.run(
        "ai_terminal.live.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        ssl_keyfile_password=args.ssl_keyfile_password,
    )


if __name__ == "__main__":
    run()


def feed_turns(events) -> list[dict[str, str]]:
    """Extract settled conversation turns for replaying to the feed.

    Drops partial transcriptions, tool calls, state deltas, and progress notes.
    """
    turns: list[dict[str, str]] = []
    for event in events:
        turn = recap._said(event)
        if turn is not None:
            turns.append({
                "role": "user" if turn[0] == "Them" else "agent",
                "text": turn[1],
            })
    return turns
