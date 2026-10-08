"""What the last conversation was about, carried into the next one.

A thread rolls over after `SESSION_IDLE_GAP_S` of quiet, and the one before it
stops being replayed. That is what keeps every hello from being slower than the
last, and it is also how "book it for the same time" stops meaning anything
over breakfast. So the thread that is being left behind is summarised into two
or three sentences, and those go into `user:last_summary`, which ADK shares
across every session belonging to one `user_id` — `agent._instruction` reads it
back on the next connect.

**Two or three sentences rather than the events themselves.** The same trade
the gws cache already makes for documents. Replaying the old thread as well as
the new one is the thing the rollover exists to stop, and a recap is one line
of prompt where a transcript is thousands.

**It costs the hello it happens on.** One `generate_content` on the small
model, inside `_thread`, before the connection opens — so the person who has
just walked up waits for it. That is the price of the recap being there on the
turn they actually need it on: written in the background it would land after
the Live API had already taken its system instruction, and the first thing
they say is exactly the thing it was carried forward for. It happens once per
idle gap, never on a reload, and `RECAP_BUDGET_S` caps it. Over budget, or a
model that will not answer, and the new thread simply starts clean.

**Nothing here reads the audio.** Native-audio turns are stored as
transcription-only events, so a spoken turn is `input_transcription` on a
`user` event and `output_transcription` on the agent's. Typed turns are
ordinary content parts. Both are text by the time they reach the file; there
is no recording to summarise and none to leak.
"""

import asyncio
import logging
import math
import os

from google.genai.types import GenerateContentConfig

from .tools._client import grounding_client, grounding_model

logger = logging.getLogger(__name__)

# The shared-across-sessions key. `State.USER_PREFIX` is `"user:"`, and the
# SQLite backend splits those out into a per-user row on write and merges them
# back on read, so every thread of one person's sees the same value.
RECAP_KEY = "user:last_summary"

# And the one that says "not on this thread". `main.rethread` rolls without
# buying a summary, so the thread it opens must not read the one already filed
# under the person — but the key above is the person's, and writing `""` into
# it to say so takes the recap away from every other thread they have open.
# The kiosk's socket and a tab on the Mac are two of those, on two different
# threads whenever an accept has rolled one of them, and the tab's recap was
# paid for with a `generate_content` a moment earlier. So the bare-ness is a
# fact about one thread and is filed against one: a plain key, like
# `_plan_run.PAD_KEY`, which the SQLite backend keeps on the session row.
BARE_KEY = "rolled_bare"

# How much of the old thread to read, newest first. Long enough for a morning
# of kitchen talk, short enough that the call stays inside the budget below.
INPUT_CHARS = int(os.getenv("RECAP_INPUT_CHARS", "6000"))
if INPUT_CHARS <= 0:
    # No `isfinite` on this one: `int("nan")` and `int("inf")` are already a
    # `ValueError` out of the read above, so only zero and negatives get
    # this far. Zero is the one somebody types, meaning "read all of it".
    #
    # It reads none of it. `transcript` trims from the front against this
    # limit, and the first line it looks at is already over a limit of zero,
    # so the branch that keeps the tail of one enormous turn keeps
    # `line[len(line):]` — the empty string. `summarise` sees no text and
    # returns before it calls anything, so no recap is ever written and
    # nothing in the log says why: it reads as a terminal that simply does
    # not carry a conversation forward, which is what it was before this
    # module existed.
    raise ValueError(
        "RECAP_INPUT_CHARS is how much of the finished conversation is read "
        "back, newest first, in characters: it wants a whole number above "
        "zero. 0 is not 'all of it' — `transcript` trims to nothing, and "
        "every thread then rolls with no recap behind it and no log line "
        "saying so. Set a length, or leave it unset for 6000.")

# What the hello is allowed to wait. Six seconds is not a target — it is the
# point at which not having a recap is better than standing there.
BUDGET_S = float(os.getenv("RECAP_BUDGET_S", "6"))
if not math.isfinite(BUDGET_S) or BUDGET_S <= 0:
    # The worst of the seven #115 lists, because `nan` here does not make a
    # comparison misfire — it takes the process down, and `summarise`'s
    # `except Exception` cannot catch it.
    #
    # `asyncio.timeout(nan)` schedules the deadline at `loop.time() + nan`,
    # which is `nan`. `BaseEventLoop._run_once` then computes the select
    # timeout as `when - self.time()`, and clamps it with `> MAXIMUM` and
    # `< 0` — both false for `nan` — so `nan` is handed straight to the
    # selector. kqueue answers `TypeError: timeout argument must be a number
    # or None`; epoll, which is what the Pi runs, reaches
    # `math.ceil(nan * 1e3)` and answers `ValueError: cannot convert float
    # NaN to integer`. Either way it is raised by the loop and not inside
    # the task, so it goes past every `except` in this module and out of
    # `run_forever`: the server dies on the first thread rollover, which is
    # the first hello after a quiet half hour.
    #
    # The other three are only expensive. At zero or below the deadline has
    # already passed, but `asyncio.to_thread` has already submitted the work
    # — measured: the stub is entered every time — so the household pays a
    # `generate_content` on every rollover and the answer is thrown away
    # before it lands. At `inf` there is no budget at all, and the person
    # who has walked up waits out whatever the model does.
    raise ValueError(
        "RECAP_BUDGET_S is what the first hello after a gap may wait for "
        "its summary, in seconds: it wants a finite number above zero. 0 or "
        "less still pays for the call and discards the answer, `inf` is no "
        "budget at all, and `nan` is worse than either — asyncio hands it "
        "to the selector, which raises outside the task and takes the "
        "server down. Set a wait, or leave it unset for 6 seconds.")

# Progress notes the server injects as user turns so the agent can say "I'm
# looking" out loud. Nobody in the room said them and they describe the
# machinery rather than the conversation.
#
# Public, because the recap is not the only reader of a session's events:
# `plan._readable` builds the planner's context from the same list and has to
# drop the same lines. One spelling of the marker, in the module that writes
# it — #192.
INJECTED = "[terminal]"

_PROMPT = (
    "You are writing a note to yourself about a conversation that has just "
    "ended, to be read at the start of the next one. Two or three sentences, "
    "no more. Keep what a person would still want you to know: what they "
    "asked you to do, what was decided, names, places, times, numbers. Drop "
    "the small talk and anything already finished with. Write it in the "
    "language they were speaking, in the past tense, as a record of what "
    "happened — \"they asked you to ...\", never \"do ...\", because you will "
    "read this back at the start of a conversation and it must not look like "
    "something you have just been told to do. Write nothing else: no "
    "preamble, no heading, no apology if there is little to say."
)


def _said(event) -> tuple[str, str] | None:
    """One turn as (who, text), or None if the event is not somebody talking.

    Most events in a live session are not a turn: usage metadata, turn markers,
    session-resumption handles and the state-only events the handshake writes.
    Partials are skipped too — the finished transcription carries the whole of
    what was said, and the fragments would repeat it word by word.
    """
    if event.partial:
        return None
    parts = (event.content.parts if event.content else None) or []
    typed = " ".join(p.text.strip() for p in parts if p.text)
    if event.author == "user":
        spoken = (event.input_transcription.text
                  if event.input_transcription else "") or ""
        who, text = "Them", typed or spoken.strip()
    else:
        spoken = (event.output_transcription.text
                  if event.output_transcription else "") or ""
        who, text = "You", spoken.strip() or typed
    # An injected note is addressed to the model, not from the person — and
    # not all of them are written as the person either. `_settle` appends
    # `UNANSWERED` with the agent's authorship, so the filter has to sit where
    # both branches reach it. On the user branch alone, a dropped socket left
    # the terminal's own note about itself in the transcript as `You:
    # [terminal] The connection ended here…`, and `transcript` trims from the
    # front, so it is the line most certain to survive into the next recap.
    # #172.
    if not text or text.startswith(INJECTED):
        return None
    return (who, text)


def transcript(events, limit: int = INPUT_CHARS) -> str:
    """The conversation as plain text, newest kept when it will not all fit.

    Trimmed from the front rather than the back: the end of a conversation is
    where the thing worth carrying usually is — what was decided, what someone
    asked to be reminded of — and the opening is "hey" and the weather. Whole
    turns, so the model is never handed half a sentence and left to guess who
    was saying it.
    """
    lines = []
    for event in events:
        turn = _said(event)
        if turn is not None:
            lines.append(f"{turn[0]}: {turn[1]}")

    kept, total = [], 0
    for line in reversed(lines):
        if total + len(line) > limit:
            # One turn over the whole budget by itself. Nobody speaks that
            # long, so something upstream has gone wrong — but the end of it
            # is still the part worth reading.
            if not kept:
                kept.append(line[len(line) - limit:])
            break
        kept.append(line)
        total += len(line) + 1
    return "\n".join(reversed(kept))


def _call(text: str) -> str:
    """One blocking `generate_content`, run off the loop by `summarise`."""
    response = grounding_client().models.generate_content(
        model=grounding_model(),
        contents=text,
        config=GenerateContentConfig(
            system_instruction=_PROMPT,
            temperature=0,
            max_output_tokens=300,
        ),
    )
    return " ".join((response.text or "").split())


async def summarise(events) -> str:
    """Two or three sentences about a finished conversation, or `""`.

    Empty on anything going wrong, and that is the whole error handling: a
    thread with no recap in front of it is the behaviour this repo had until
    now, and it is a great deal better than a hello that hangs on a quota
    error at seven in the morning.
    """
    text = transcript(events)
    if not text:
        return ""
    try:
        async with asyncio.timeout(BUDGET_S):
            return await asyncio.to_thread(_call, text)
    except TimeoutError:
        logger.warning("recap: over %.0fs, starting the thread without one",
                       BUDGET_S)
    except Exception as err:  # pylint: disable=broad-except
        logger.warning("recap: could not summarise: %s", err)
    return ""


def recalled(context) -> str:
    """What the last conversation was about, for the instruction to read.

    `context` is anything carrying a `.state`, the same contract as
    :func:`tools.maps.here`, because the instruction is built from a
    `ReadonlyContext` and nothing else here needs a tool context.

    Nothing, on a thread `main.rethread` rolled bare. The summary is the
    person's and this thread is not the one it was carried into, so it is
    skipped here rather than deleted there — see `BARE_KEY`.
    """
    state = getattr(context, "state", None) or {}
    if state.get(BARE_KEY):
        return ""
    return state.get(RECAP_KEY) or ""
