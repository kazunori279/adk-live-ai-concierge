"""A step that thinks before it acts, as an agent tool.

The live model hears a sentence and answers it in the same breath, which is
what it was chosen for and what makes it bad at the other thing. Ask it
something that needs three tools in a particular order and it dispatches the
first one it recognises, answers from what comes back, and never revisits the
plan — because nothing asked it to make one.

So: a reasoning model on its own ``Runner``, off the audio path, with the same
tools and a loop. Plan, run a step, read what came back, change the rest of the
plan if it needs changing, answer. It is the :mod:`.gws` shape one level up,
and most of this file is that file with the Workspace parts taken out — the
dedupe, the never-raising `_ask`, the cancel-on-disconnect register, and the
frame that draws the card are all the same problems with the same answers.

Two things are new, and both come from being a level further down.

**The live agent keeps its tools.** Moving all ten onto the planner is what
#25 proposed and it would blank most of the screen: the map, the weather chart,
the photo grid and the video are drawn from `functionResponse` parts in the
live event stream, and ``AgentTool``-shaped nesting consumes the sub-run's
events internally — the browser never sees them. `gws` and `browse` are the
exception because they already answer out of band through :mod:`..screen`. So
the split is not "hard questions go to the planner", which is a judgement the
live model would make badly; it is *one lookup answers it, call the tool; the
answer to one lookup is the input to the next, call `plan`*.

**The planner is told what was already said.** `tool_context.session` in a live
tool call is the real live session, and its `events` list is the conversation,
appended to as it happens. That handle exists here and nowhere below here, so
:func:`_recent` renders it to text in this function and it travels down in
:class:`PlanRequest`. "Book the other one instead" is unanswerable without it.

On the first turn after a rollover those events are as old as the thread they
belong to, which is one turn. What the conversation before it was about is on
the session's state, where :mod:`..recap` left it on the way past, so that is
read here too and travels as `earlier` — a field of its own for the reason
`found` is one, and because nobody in this conversation said it.
"""

import asyncio
import logging
import os
import re
from datetime import datetime

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.agents.run_config import RunConfig
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool, ToolContext
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from pydantic import BaseModel, Field

from .. import conn, recap, screen
from .._log import _sized
from . import _gws_run, _plan_run, _progress, _timing
from . import gws as gws_mod
from ._lang import SCREEN_STYLE, of_turn, panel_lang
from .browse import browse_tool
from .calc import calculate_tool
from .image_search import image_search_tool
from .maps import (
    maps_route_tool,
    maps_search_along_route_tool,
    maps_search_tool,
)
from .place import place_info_tool
from .weather import weather_tool
from .web_search import web_search_tool
from .youtube_search import youtube_search_tool

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Work a question out in several steps, where the answer to one lookup is "
    "what the next one needs, or for multi-step trip planning and schedules. "
    "Runs the tools itself, reads each result, and "
    "changes the remaining steps when a result calls for it."
)

NO_ANSWER = (
    "The steps ran but did not add up to an answer. Say so, and offer to try "
    "the question a piece at a time."
)

FAILED = (
    "Working that out failed and nothing came of it — no answer, and no change "
    "to their account if a step would have made one. Say that plainly, say "
    "nothing about what the answer might have been, and offer to try again."
)

# How long a whole plan gets. It has to hold one of everything the planner can
# call, and the most expensive thing it can call is `gws`, which gives up at
# its own 160 s (`gws.TIMEOUT`) — so anything at or under that would stop the
# plan while its slowest legal step was still legitimately running.
#
# 160 s for that one step, plus seven cheaper ones at around six seconds each,
# plus a model turn of about five between them: 160 + 42 + 45 is 247. 250 is
# that rounded up, and it keeps the same 1.2 ratio to the card's own deadline
# (`LATE_TOOLS.plan.deadline` in app.js, 300 s) that `gws.TIMEOUT` keeps to the
# gws one, for the same reason — the screen should hear the tool give up rather
# than give up on it.
#
# So this moves whenever `gws.TIMEOUT` does. Leaving it at 200 when that went
# from 100 to 160 would have left the other seven steps and eight model turns
# 40 s between them against the 87 s budgeted here, and the plan would be cut
# off with a legitimate step still running — which is the one thing this
# number exists to prevent. `frame_cases` in `tools/plan_check.py` checks the
# chain rather than each pair, so the next person to move one has to move the
# rest.
TIMEOUT = 250.0

TOO_SLOW = (
    "Working that out ran too long and was stopped, so there is no answer. Say "
    "that plainly and offer to try one part of it instead."
)

# The same stop, with something to show for it. A separate wording rather than
# a suffix on TOO_SLOW, because TOO_SLOW says "there is no answer" and here
# there is part of one — a plan that got the restaurant and its hours before
# the forecast timed out has two thirds of what was asked for, and throwing
# that away costs the person the whole 250 s over again.
TOO_SLOW_PARTIAL = (
    "Working that out ran too long and was stopped before it finished. What it "
    "had got to is below. Say it is only part of the answer, give the part "
    "there is, and offer to go after the rest."
)

UNCONFIGURED = (
    "The planner is not set up on this terminal. Answer with the tools you "
    "have, one at a time."
)

# The four ways a call ends without the answer it was asked for, as the card
# knows them. Same four names `gws` uses, so `cards.js` can say them with one
# table. Two of the five entries are "slow": a timeout with partial results is
# still a timeout to the card, and it should draw the same way.
_NOT_AN_ANSWER = {
    FAILED: "failed",
    NO_ANSWER: "empty",
    UNCONFIGURED: "unset",
    TOO_SLOW: "slow",
    TOO_SLOW_PARTIAL: "slow",
}


def _kind(text: str) -> str | None:
    """Which of those an answer is, or None when it is a real answer.

    By prefix, not by equality: TOO_SLOW_PARTIAL is followed by the part of the
    answer there was, so an exact lookup reads it as an ordinary answer and the
    card draws a plan that succeeded. Equality was right only while every one
    of these was a constant returned whole.
    """
    return next((k for m, k in _NOT_AN_ANSWER.items() if text.startswith(m)),
                None)


ALREADY = (
    "This exact task is already being worked out and its answer is coming. Do "
    "not call plan for it again, and say nothing — the person has been told "
    "you are on it. Wait."
)

# The tasks each live session has in flight, by session id. Same reflex as
# `gws._ASKING` guards: the native-audio model re-dispatches a call it is
# waiting on, and one progress note is enough to set it off. A duplicate here
# is worse than a duplicate gws call, because each copy runs a whole plan.
_ASKING: dict[str, set[str]] = {}

# And the runs themselves, so a socket that closes takes them with it. The same
# reason as `gws._RUNNING`, one level stronger: a plan can have `gws` in it, so
# an abandoned run can still create a calendar event for a conversation that
# ended. Keyed by session id and then by connection for the same reason too —
# two pages on one profile share the id, and one entry between them means the
# page that leaves first takes the other's runs out of reach.
_RUNNING: dict[str, dict[str, set[asyncio.Task]]] = {}

# How much of the conversation the planner is told about. Two caps rather than
# one because they fail differently: a long silence full of tool traffic blows
# the character budget in six events, and a fast exchange of one-word turns
# fills the event budget with 200 characters. Whichever runs out first wins.
#
# `context` is read by a model that also has a task written for it, so this is
# background and not the question — a paragraph, not a transcript. It is also
# rebuilt per call and sent every time, so it is paid for on every plan.
RECENT = 14
RECENT_CHARS = 1800

INSTRUCTION = """\
You work a question out in steps, on behalf of a voice assistant. The assistant
reads your answer out loud and shows it on the screen beside it, so the person
is standing there waiting while you work.

Your message is a JSON object. `task` is what they want, in plain language.
`context` is what has already been said in this conversation — read it before
you plan, because the task on its own often does not say which thing "the other
one" is. `found` is what earlier steps and earlier questions worked out, and it
is the one place a *result* survives; read it before you run anything, because
a lookup it already answers is a lookup you do not have to spend a turn on.
`earlier` is your own note about the conversation before this one, from before
a gap long enough to have ended it — background, not something anybody has just
said, and never fresh enough to answer a question out of. `shown` is what is on
the screen in front of them right now, newest last: one line per card, with the
things on it numbered as the card numbers them. It is how "the second one" and
"that place you showed me" turn into a name, and a name is something to look up
rather than an answer about it. `lang` is the BCP-47 tag of the language they
are speaking.

## How to work
You are here because this question needs more than one lookup. Work in a loop:

1. **Plan.** Decide the steps, and for each one, which earlier step you need
   the answer of before you can run it. Call `show_plan` with them straight
   away, before you run anything — it is the only thing the person can see
   while you work.
2. **Run everything that is ready, together.** A step is ready when you already
   have every input it needs. Call *all* the ready steps in the same turn, as
   several tool calls in one message. Three lookups that do not need each other
   take the time of one that way, and the person is standing there.
3. **Wait for a step that is not ready.** Step two is not ready when its input
   is step one's answer. Leave it out of this turn and run it in the next one,
   once you have what it wants. Guessing at the input to save a turn is worse
   than the turn.
4. **Review.** Did those results answer their steps, and is the rest of the
   plan still right? A restaurant that turns out to be shut on a Monday does
   not make "check the weather there" the next step — it makes "go back to the
   list and take the next one".
5. **Change the plan when a result changes it.** Call `show_plan` again with
   the new steps. Revising is not failing; it is the reason this tool exists.
6. **Answer** as soon as you can. Not when the plan is finished — when the
   question is.

Call `show_plan` again whenever what is running changes, with `running` set to
every step going out this turn and `done` to the ones that have come back. It
never comes out of your eight — call it alongside the tools going out and it
costs nothing at all.

**You have eight turns, and the eighth is enforced** — past it the tools stop
running and you answer from what you have. What costs a turn is going back to
think; the tools you call in one turn are one turn between them, however many
they are. So the shape to aim for is few turns, wide: three turns of three
lookups beats nine turns of one, and answers in a third of the time.

Six tools in one turn is the most that will run. Past that the extra ones come
back refused and you have wasted the turn on them.

## Writing things down
`note_finding` keeps one fact where it will still be there afterwards. **It is
not a step** — it never comes out of your eight — and it is the only thing here
that outlives this run: everything else you learn is gone the moment you
answer. Write it in the *same turn* as your next lookup and it is free
outright; a turn spent on nothing but `note_finding` is still one of the eight.

Write one the moment a step settles something:

- the answer you narrowed to — "shortlist: Hiiragiya, Tawaraya, Yoshida-sanso",
  not the twenty the search returned;
- a fact a slow tool went and got — "free nights next month: 14, 15 and 21 Nov";
- the one number a long document turned out to hold.

The fact, in a sentence, the way you would say it out loud. Not the tool's
response pasted in: anything past two hundred characters is cut off, and a wall
of JSON is no use to the person reading it next time anyway.

Two things it buys, both of them things that go wrong today. If this plan runs
out of time, what you wrote down is what the person still gets. And when they
ask the obvious follow-up — "what about the second one?" — your findings arrive
in `found` and you can answer it without running the search again.

## Tools
- `place_info`: one named place — what it is, its hours, where it is, photos.
  Name it in full, "Kinkaku-ji, Kyoto" rather than "the temple".
- `weather`: any weather question, here or anywhere. It returns five days, so
  one call covers a follow-up about tomorrow.
- `web_search`: anything else that changes or that you are unsure of.
- `image_search`, `youtube_search`: when a picture or a video is the answer.
- `maps_search`: finding places when there are several answers. For one place
  already named, `place_info`.
- `maps_route`: directions, distance and travel time.
- `maps_search_along_route`: stops to make on the way somewhere.
- `gws`: the person's own Google Workspace — their calendar, their mail, their
  Drive, their notes. Anything phrased as "my" or "our". It reads and writes,
  it takes its own `context` paragraph, and it is slow: half a minute for a
  mailbox question. Worth a step; not worth two.
- `browse`: opens a real browser and reads a page, for what only a site knows.
  It takes its own `context` paragraph too, and the better part of a minute.
  Try `web_search` first.

`gws` and `browse` are the two expensive ones. A plan with both in it will be
most of a minute before you say anything.

- **A step that writes runs on its own.** Sending mail, making an event,
  changing a document: put it in a turn by itself, after you have read
  everything you need to decide on. Two writes in one turn can land in either
  order and you cannot undo the one that landed first.
- **One `browse` at a time.** Each one opens a browser of its own, and this
  terminal is a small computer.
- **Two `gws` steps at once share the Workspace allowance.** One `gws` step has
  twelve commands to work with; two running together get six each, three get
  four. One step that asks for everything beats two that each ask for half.

Every tool takes a `lang`. Pass the one from your message, on every call: a
tool is a fresh search with no memory, and without it the screen answers in
English while you answer in Japanese.

The assistant sometimes spells out a date and sometimes gets it wrong. Take the
day it means — "tonight", "next Friday" — and work the date out from the clock
below.

## Answering
Lead with the answer in a sentence, then the detail as a short bulleted list.
This is read out loud as well as shown on a small screen, so no raw JSON, no
IDs and no URLs in the prose.

Say what you did not get to. A plan that ran out of steps, a tool that came
back empty, a restaurant whose hours nobody publishes: one line each. An answer
that quietly leaves out the part that failed is the one mistake here that costs
them something.

Two ways to get this wrong, both worse than answering flatly:

- **Do not invent relevance.** `context`, `earlier` and `shown` say what to
  look for. None of them is a source of facts. If one of them says the hotel is
  booked for the 3rd and no tool found a hotel, you found no hotel — the date
  does not become something you can report.
- **Do not over-filter.** Something that contradicts the plan is the most
  useful thing you can find, not an outlier to leave out. When you are unsure
  whether something matters, it matters.

Reply in concise Markdown, in `lang` — not in the language `task` happens to be
written in, which is the assistant's, not the person's. Fall back to the
language of the task when `lang` is empty. Translate what you find as well.

""" + SCREEN_STYLE + """

## Care
The assistant has already asked whoever is standing there, so do what the task
says — but only what it says. Never mail anyone the task did not name, never
delete anything it did not ask you to delete, and never make room for something
by taking something else away.
"""


class PlanRequest(BaseModel):
    """The message the planner is handed, as a JSON document.

    A schema rather than a bare string, for the same reason as
    :class:`~.gws.GwsRequest`: the language of the turn is part of the
    contract, and three named fields say so where one blob of prose leaves the
    planner to work out which sentence was which. It is the agent's
    ``input_schema``, so :func:`_run` has to send text that parses as this —
    the validating is done here, on the way in.

    `context` is not on :func:`plan`'s own signature. It is written here, from
    the live session's events — see :func:`_recent`. The live model does not
    have to compose it and cannot get it wrong.

    `found` is the other half of that, and the two are deliberately separate
    fields rather than one paragraph: `context` is what was *said*, and it is
    what `_recent` can reconstruct from the session's events. `found` is what
    was *learnt*, which `_recent` cannot reconstruct at all — `_readable` reads
    `part.text` and `part.function_call` and never `part.function_response`, so
    a tool call survives into the next plan and the tool's answer does not.

    `earlier` is a third for the same argument taken one step further out. It
    is what was said in a *different* conversation, boiled down by `recap` at
    the rollover, and folding it into `context` would hand the planner a
    paragraph labelled "this conversation" describing one that ended hours ago
    — the mistake `agent._instruction` spends four lines fencing off for the
    live model. Its own field is what lets the instruction say so once.

    `shown` is the fourth and it is the only one that is not speech. This
    terminal answers on a screen as much as out loud, and "the second one" is
    an ordinal into a card. It is read off the same events `context` is, out of
    the `part.function_response` the paragraph above says `_readable` never
    looks at — see :func:`_shown`. Not folded into `found`, which is the pad
    and is written only by a plan that ran (`_plan_run.keep`); a card is on
    the screen whether or not anybody has ever called `plan`, and #80 measured
    that in eight real conversations the pad has never been written once. Not
    folded into `earlier` either: that is a *different* conversation, and
    these cards are the ones in front of them now.
    """

    task: str = Field(description="What the person wants, in a sentence.")
    context: str = Field(
        default="", description="What has already been said in this conversation."
    )
    found: str = Field(
        default="", description="What earlier steps and questions found out."
    )
    earlier: str = Field(
        default="", description="What the conversation before this one was about."
    )
    shown: str = Field(
        default="", description="The cards on the screen in front of them."
    )
    lang: str = Field(
        default="", description="BCP-47 tag of the language to answer in."
    )


def _instruction(_: ReadonlyContext) -> str:
    """Add the clock to the static instruction.

    Only the clock, where `gws` also adds a skill list and the document cache.
    The planner has neither: its tools are the ones the live agent has, and the
    documents belong to the `gws` step, which is told about them by `gws`.
    """
    now = datetime.now().astimezone()
    return f"{INSTRUCTION}\nRight now it is {now:%A %Y-%m-%d %H:%M %Z}.\n"


# The planner, so it can be reached from inside `plan` below. A module global
# for the same reason `gws._agent` is one: ADK calls a tool with the model's
# arguments and nothing else, and the model name comes from .env, which main.py
# loads at startup.
_agent: Agent | None = None

# One line of history, as the planner reads it. Named speakers rather than
# roles, because the planner is neither of them and "user"/"model" invites it
# to think it is the model.
_SPOKE = {"person": "person", "you": "assistant"}

# Tool traffic worth keeping in a paragraph of background. The call, because
# "you already looked this up" is the thing that stops a plan repeating a step;
# not the response, which is a card's worth of JSON and whose useful half is
# already in what the assistant said about it out loud.
_ARGS_CHARS = 60

# How much of the screen the planner is told about. Its own budget rather than
# a share of `RECENT_CHARS`, and small, both for the same measurement: #80
# replayed the real store and found the character cap already doing the cutting
# at 13 of 34 dispatch points, with a median `context` of 11.5 turns and 1074.5
# characters. A third consumer of that budget would bind it more often on
# exactly the conversations where it already binds.
#
# 400 is sized under the cheapest cap change #80 was willing to call "cheap and
# bounded": raising `RECENT` from 14 to 30 measured +11246 characters over the
# 12 dispatch points it changed, about +937 each. This is a flat +400 on every
# call and is not measured, so it is deliberately set below that.
#
# Six cards because `_plan_run.WIDE` is six: one plan turn may run six tools,
# so six cards is one full turn's worth of screen and the oldest thing in the
# window is still something this conversation put there.
SHOWN = 6
SHOWN_CHARS = 400

# Named items on one card. Cut from the end, never the front: the number beside
# a name is the whole point of the field, so dropping the sixth is a name lost
# and dropping the first is every remaining number made wrong.
# `maps.MAX_PLACES` is 5, so this drops nothing a Maps card draws today, and
# bounds one that grows.
SHOWN_ITEMS = 5

# What a card is headed with, in the order the page looks for one. The client
# tells two cards apart on the same five fields (`app.js:494-513`), so the
# field that names a card there names it here.
_TITLE_KEYS = ("query", "place", "location", "destination", "topic", "expression")

# ... and where the heading comes from when the answer carries no handle at
# all. `gws` and `plan` answer `{"answer": ...}` and their cards are headed
# with the argument the call was made with (`app.js:1052`).
_ASK_KEYS = ("task", "request", "query")

# The things on a card somebody can point at, by the key they arrive under, and
# in the order a card that has two of them draws them. Each is a list of dicts
# carrying a `title` — places, videos and grounding sources all are.
# `images` is deliberately absent: an image has no title, and a grid of
# pictures is not a list anybody names an element of.
_ITEM_KEYS = ("places", "videos", "sources")


def _flat(text) -> str:
    """One field of a tool's answer as something that fits on a line."""
    return re.sub(r"\s+", " ", str(text)).strip()


def _named(response: dict) -> list[str]:
    """The things on one card, in the order it draws them.

    Labelled `title` or else the link, which is `linkChips`' own fallback
    (`cards.js:75`) — an untitled chip is on the screen holding a number, so
    dropping it here would make every number under it wrong. One with neither
    is dropped, matching the third fallback, which is a word naming nothing.
    """
    for key in _ITEM_KEYS:
        items = response.get(key)
        if not isinstance(items, list):
            continue
        titles = [_flat(it.get("title") or it.get("uri") or "")
                  for it in items if isinstance(it, dict)]
        titles = [title for title in titles if title]
        if titles:
            return titles[:SHOWN_ITEMS]
    return []


def _card(name: str, args: dict, response) -> str | None:
    """One card as one line, or None when the page drew no card at all.

    The line is a handle and not a copy. It carries what the card is headed
    with and the names of the things on it, and drops everything else the
    answer holds — coordinates, place ids, photo URLs, opening hours, the
    summary paragraph. That is the same ruling `_ARGS_CHARS` makes one comment
    up for the call side, for the same reason: the planner does not need the
    lookup back, it needs to know which thing "the second one" is so it can run
    the lookup with the right subject.

    Two answers the page draws nothing for, and both have to be left out rather
    than listed as an unnamed card. `weather` hands back an `error` and
    `weatherCard` returns null, which `appendCard` drops (`cards.js:581-582`,
    `app.js:321-322`). A repeat of a question already in flight comes back
    `already_running`, and the frame beside it carries `drop`, which takes the
    empty card the repeat put up back off the screen (`plan.py:835-840`,
    `gws.py:951-954`, `app.js:940-945`).
    """
    if not isinstance(response, dict):
        return None
    if response.get("error") or response.get("already_running"):
        return None
    title = next((_flat(response[key]) for key in _TITLE_KEYS
                  if isinstance(response.get(key), str)
                  and _flat(response[key])), "")
    if not title:
        title = next((_flat(args[key]) for key in _ASK_KEYS
                      if isinstance(args.get(key), str)
                      and _flat(args[key])), "")
    line = f'{name} "{title}"' if title else name
    items = _named(response)
    if items:
        body = "; ".join(f"{i}. {title}"
                         for i, title in enumerate(items, start=1))
        line = f"{line}: {body}"
    return line


def _shown(events, limit: int = SHOWN, chars: int = SHOWN_CHARS) -> str:
    """The cards on the screen in front of them, newest last.

    The other half of what `_readable` leaves behind. `context` is what was
    *said*, and on this terminal a good deal of the answer was never said at
    all — it was drawn. Both paths that draw one end at the DOM: a live
    `part.functionResponse` goes straight into `renderToolResult`
    (`app.js:1089-1116`), and `browse`, `gws` and `plan` push their own frames
    down the screen sink (`app.js:881-885`). Nothing wrote either of them
    anywhere the planner can read, so "book the second one" arrived naming
    nothing.

    Read off `session.events` rather than recorded anywhere, which is what
    makes it free and what keeps it honest — a stored copy is a second version
    of the same fact with its own lifetime, which is the whole of what #79 had
    to build `ran()` for. Every card is in there: ADK appends a blocking tool's
    response event in the flow (`base_llm_flow.py:1611-1618`) and a
    NON_BLOCKING tool's from its background task (`functions.py:988-996`, ADK
    2.8.0), so the out-of-band tools are as visible here as the inline ones.

    Ordered by the *call*, not the answer, because that is where the card goes:
    `gws` and `plan` put an empty card up when they are called and fill it in
    place a turn or two later (`app.js:1055-1062`), so ordering by the answer
    would list a slow card below one drawn under it.

    Deduplicated on the client's own key — same tool, same arguments, one card
    (`app.js:1049`) — keeping the newest, because a question asked again draws
    its new card at the bottom.

    Both caps are soft on the last line, the way `_recent`'s are, and the
    oldest card goes over the side first.
    """
    order: list[tuple[str, str]] = []
    asked: dict[str, tuple[str, dict]] = {}
    drawn: dict[str, str] = {}
    for event in events or []:
        for part in (event.content.parts if event.content else None) or []:
            call = getattr(part, "function_call", None)
            if call is not None:
                name, args = call.name or "tool", call.args or {}
                asked[call.id] = (name, args)
                order.append((call.id, f"{name}:{sorted(args.items())!r}"))
                continue
            answer = getattr(part, "function_response", None)
            if answer is None:
                continue
            name, args = asked.get(
                answer.id, (answer.name or "tool", {}))
            # An answer whose call is off the front of the window still drew a
            # card, so it takes a place of its own here rather than vanishing.
            if answer.id not in asked:
                order.append((answer.id, f"{name}:{answer.id!r}"))
            line = _card(name, args, answer.response)
            if line is not None:
                drawn[answer.id] = line
    # Built over the ids that drew rather than over every call, so that a
    # repeat with nothing on the screen cannot evict the one that has.
    newest: dict[str, str] = {}
    for ident, key in order:
        if ident in drawn:
            newest[key] = ident
    kept = [drawn[ident] for ident, key in order if newest.get(key) == ident]

    lines: list[str] = []
    total = 0
    for line in reversed(kept):
        lines.append(line)
        total += len(line)
        if len(lines) >= limit or total >= chars:
            break
    return "\n".join(reversed(lines))


def _unless_ours(who: str, text: str) -> tuple[str, str] | None:
    """`(who, text)`, or None if nobody said it or the terminal did."""
    text = text.strip()
    if not text or text.startswith(recap.INJECTED):
        return None
    return who, text


def _readable(event) -> tuple[str, str] | None:
    """One session event as (speaker, line), or None if it says nothing.

    Audio never reaches here — the live runner drops the blobs before it
    appends (`runners.py`), and what survives of a spoken turn is its
    transcription. Everything else is either text from a half-cascade model or
    tool traffic.

    The terminal's own notes are not turns and are dropped, on both branches
    and whichever authorship they carry: `_progress`'s "I am still looking"
    arrives as the person, `_settle`'s UNANSWERED arrives as the model. The
    reason the guard matters more here than in `recap` is that `_recent` joins
    consecutive lines from one speaker, so an unfiltered note is glued onto
    the end of the question it interrupted — the planner then reads one
    sentence that starts as the user asking something and ends as an
    instruction not to call a tool. #192.
    """
    if getattr(event, "input_transcription", None) and event.input_transcription.text:
        return _unless_ours("person", event.input_transcription.text)
    if getattr(event, "output_transcription", None) and event.output_transcription.text:
        return _unless_ours("you", event.output_transcription.text)

    said, called = [], []
    for part in (event.content.parts if event.content else None) or []:
        if part.text and not part.thought:
            said.append(part.text)
        elif part.function_call:
            args = ", ".join(
                f"{k}={v}" for k, v in (part.function_call.args or {}).items()
                if k not in ("lang", "context") and isinstance(v, str) and v
            )
            called.append(f"[called {part.function_call.name}: "
                          f"{args[:_ARGS_CHARS]}]")
    line = " ".join(said + called).strip()
    return _unless_ours("person" if event.content
                        and event.content.role == "user" else "you", line)


def _recent(events, limit: int = RECENT, chars: int = RECENT_CHARS) -> str:
    """The conversation so far, as a paragraph the planner can read.

    Newest first while collecting, so that when a budget runs out it is the
    oldest turn that is dropped, then reversed back into reading order.

    Consecutive lines from the same speaker are joined. A spoken turn arrives
    as several events — the transcription comes in pieces, and the live runner
    appends each finished piece — so a sentence otherwise reads as six people
    taking one word each.

    Both caps are deliberately soft on the last line: a turn that would take
    the total past `chars` is kept whole and ends the collection, rather than
    being cut mid-word into something the planner has to guess at.
    """
    lines: list[tuple[str, str]] = []
    total = 0
    for event in reversed(list(events or [])):
        got = _readable(event)
        if got is None:
            continue
        who, text = got
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if lines and lines[-1][0] == who:
            lines[-1] = (who, f"{text} {lines[-1][1]}")
        else:
            lines.append((who, text))
        total += len(text)
        if len(lines) >= limit or total >= chars:
            break
    return "\n".join(f"{_SPOKE[who]}: {text}" for who, text in reversed(lines))


async def _run(args: dict, tool_context: ToolContext,
               seen: list[str] | None = None) -> str:
    """Run the planner over one task and return the text it ended on.

    A ``Runner`` and a session per call, both for the reasons
    :func:`~.gws._run` gives — and the planner is stateless for the same reason
    the Workspace sub-agent is: `context` is how a conversation reaches it, and
    that is written fresh on every call.

    `seen` collects each turn's text as it arrives. The caller owns the list,
    which is the whole point: :func:`_ask` bounds this with
    ``asyncio.timeout``, and a cancellation takes every local in here with it —
    including the accumulated turns. A list that lives outside the cancelled
    coroutine is what survives to be reported.
    """
    invocation = tool_context._invocation_context
    app_name = invocation.app_name or _agent.name
    sessions = InMemorySessionService()
    # Carries the live session's state across so tools called by the planner
    # read what the live agent reads — `maps.here` sees the browser's pin
    # rather than falling back to HOME_LOCATION from the environment, and
    # any ambient state on the turn is preserved in the sub-run.
    #
    # The turn's language first, and the panel only under it. `args["lang"]`
    # is already floored by `of_turn` before it gets here (`plan.py:873`), so
    # this seeds what the person is being answered in rather than what the
    # panel is set to. Reading the panel alone made one plan bilingual: a step
    # where the planner passed `lang` answered in the turn's language and a
    # step where it left it out answered in the panel's.
    panel = args.get("lang") or panel_lang(tool_context)
    state = dict(getattr(getattr(invocation, "session", None), "state", None) or {})
    if panel:
        state["panel_lang"] = panel
    session = await sessions.create_session(
        app_name=app_name, user_id=invocation.user_id,
        state=state if state else None)
    runner = Runner(app_name=app_name, agent=_agent, session_service=sessions)

    # A bare JSON document and nothing else: `PlanRequest` is the agent's
    # `input_schema`, so ADK hands the text on as the whole message and a word
    # of prose around it is a word the planner has to read past.
    message = types.Content(role="user", parts=[types.Part.from_text(
        text=PlanRequest(**args).model_dump_json(exclude_none=True))])
    config = gws_mod._nested_config(invocation.run_config) or RunConfig()

    last, error = None, None
    try:
        async with Aclosing(runner.run_async(
            user_id=session.user_id, session_id=session.id,
            new_message=message, run_config=config,
        )) as events:
            async for event in events:
                if event.error_message:
                    error = event.error_message
                if event.content:
                    last = event.content
                    if seen is not None:
                        # Thoughts dropped, for the reason `gws._answer` drops
                        # them: the planner thinks in the open, so most of what
                        # arrives is "now I will look up the hours". Reported
                        # as the partial answer that is read out loud.
                        chunk = "".join(p.text for p in (last.parts or [])
                                        if p.text and not p.thought)
                        if chunk.strip():
                            seen.append(chunk)
    finally:
        await runner.close()

    return gws_mod._answer(last, error)


async def _ask(args: dict, tool_context: ToolContext) -> str:
    """Put the task to the planner and return its answer as text.

    Never raises, for the reason :func:`~.gws._ask` never raises: a
    NON_BLOCKING tool that does dies inside ADK's background task with one
    ``logger.exception``, nothing reaches the live request queue, and the card
    waits for an answer that is not coming.
    """
    if _agent is None:
        return UNCONFIGURED
    seen: list[str] = []
    try:
        async with asyncio.timeout(TIMEOUT):
            with _timing.step("planner"):
                answer = await _run(args, tool_context, seen)
    except TimeoutError:
        # Sized, not printed, on all five of this module's task lines: `task`
        # is the household's own sentence, `main.py` sizes it on the way in
        # (`_log_tool_traffic`), and printing it two frames down puts it back
        # in the same persistent journal. See #206.
        logger.warning("plan timed out after %ss for %s, %s turns to report",
                       TIMEOUT, _sized(args.get("task", "")), len(seen))
        # The last turn, not all of them: the planner narrates as it goes, so
        # the earlier ones are mostly "now I will look up X" and the newest is
        # the one carrying settled facts.
        if seen:
            return f"{TOO_SLOW_PARTIAL}\n\n{seen[-1]}"
        return TOO_SLOW
    except Exception:  # not BaseException: a cancelled call has not failed
        # `logger.exception` too: the traceback carries no locals, so the
        # format string is the whole of what reaches the log.
        logger.exception("plan failed for %s", _sized(args.get("task", "")))
        return FAILED
    text = answer if isinstance(answer, str) else str(answer)
    if not text.strip():
        logger.warning("plan returned nothing for %s",
                       _sized(args.get("task", "")))
        text = NO_ANSWER
    return text


async def plan(task: str, tool_context: ToolContext, lang: str = "") -> dict:
    """Work out an answer that takes several tools in a particular order.

    Use this for trip planning, travel itineraries, day schedules, or when
    the answer to one lookup is what the next lookup needs —
    "find somewhere nearby for dinner tonight and tell me if I need an
    umbrella" is a search, then that place's hours, then the weather where it
    is. It runs the tools itself, reads each result, and changes the remaining
    steps when one of them says something unexpected.

    Do not use it for a question one tool answers. Call that tool directly: it
    draws its own card and comes back in a second, where this adds a model turn
    in front of the same lookup.

    It answers late — half a minute upwards — and says where it has got to on
    the way. Those updates are not the answer: relay one and keep waiting.

    It reads the conversation itself, so there is no context to write.

    Args:
        task: What the person wants, in a sentence, in their own terms, e.g.
            "somewhere nearby for dinner tonight, and whether they need an
            umbrella getting there".
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. The answer is shown on screen as well as read out, so it is
            written in this language.

    Returns:
        The answer, in Markdown.
    """
    lang = of_turn(lang, tool_context)
    session_id = tool_context.session.id
    call_id = tool_context.function_call_id
    asking = _ASKING.setdefault(session_id, set())
    if task in asking:
        logger.info("plan already working on: %s", _sized(task))
        await screen.push(session_id, {
            "type": "plan", "id": call_id, "drop": True,
        })
        return {"already_running": True, "note": ALREADY}

    asking.add(task)
    work: asyncio.Task | None = None
    # Held rather than looked up twice, for the reason `gws` holds its own: the
    # `finally` has to take the task out of the set it went into, and by then a
    # connection leaving can have made the lookup answer with a different one.
    running: set[asyncio.Task] | None = None
    with _timing.question(call_id), _timing.step("total"):
        try:
            # Not on screen: the card is showing the steps and which of them
            # are running, which `_plan_run.show_plan` draws for itself and
            # which says more than a state name would. Same call as `browse`.
            notes = _progress.Notes(tool_context, "plan", on_screen=False)
            # The pad, in and out. The planner's own `Runner` mints a fresh
            # `InMemorySessionService` per call, so nothing crosses between two
            # plans down there; this session's ADK state is where a finding can
            # be put that outlives both that and the timeout below. Read here,
            # written back after the answer — see `_plan_run.keep`.
            found: list[str] = []
            args = {
                "task": task,
                "context": _recent(tool_context.session.events),
                "found": _plan_run.as_text(_plan_run.held(tool_context.state)),
                # Off the same state, and the only thing here that outlived a
                # rollover on its own — the events above start again with the
                # thread and the pad has to be carried by hand.
                "earlier": recap.recalled(tool_context),
                # And off the same events as `context`, reading the half of
                # them `_readable` throws away. Nothing records a card, so this
                # is the one place "the second one" has anything to be an
                # ordinal into — see `_shown`.
                "shown": _shown(tool_context.session.events),
                "lang": lang,
            }
            # Four bindings, all snapshotted into the task by `create_task`
            # and all needed one or two levels below it — see `_plan_run` and
            # `screen.live`.
            #
            # `_plan_run.pad(found)` is entered here so the list belongs to this
            # frame rather than to `_ask`: the timeout cancels `_ask`, and a
            # finding written before the clock ran out is the one thing worth
            # keeping out of a plan that did not finish.
            #
            # `_gws_run.budget()` is entered here rather than left to `gws`,
            # which enters its own one call later. That one is reentrant and
            # becomes a no-op under this, so a plan spending four `gws` steps
            # gets twelve CLI calls between them instead of twelve each.
            #
            # `plan`'s own `TIMEOUT` on the deadline, because that is the clock
            # `_ask` wraps the whole plan in. It is the ceiling, not the
            # answer: a `gws` step inside this one tightens the deadline to its
            # own 160 s for as long as it runs, because that is what actually
            # cancels it. Nearest wins — see `_gws_run.budget`.
            with _plan_run.budget(), _gws_run.budget(seconds=TIMEOUT), \
                    _plan_run.pad(found), screen.live(session_id, call_id):
                work = notes.start(_ask(args, tool_context))
            # This connection's set where the socket has named itself, the
            # newest otherwise, and None only when every connection is gone.
            running = running_of(session_id)
            if running is not None:
                running.add(work)
            notes.opening()
            await notes.watch(work)
            text = await work
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("plan failed to run %s", _sized(task))
            text = FAILED
        finally:
            if work is not None:
                work.cancel()
                if running is not None:
                    running.discard(work)
            asking.discard(task)
            if not asking and _ASKING.get(session_id) is asking:
                _ASKING.pop(session_id, None)

        # Past the `try`, so the two paths that lose the answer keep their
        # findings anyway — those are the paths the pad is worth the most on.
        # The timeout is covered by `found` belonging to this frame rather than
        # to the cancelled one; landing here rather than beside `text = await
        # work` is what covers a plan that raised.
        #
        # The write is a state delta the framework applies when it appends this
        # call's event, so a `StaleSessionError` cannot surface here to be
        # caught; `keep` swallows what can.
        _plan_run.keep(tool_context.state, found)

        # A run that was cut short reports the facts it settled, not whichever
        # line it happened to be narrating. `found` belongs to this frame and
        # survived the cancellation; `_ask` only ever saw `seen[-1]`, so
        # without this the findings are on the pad and none of them reach the
        # person until the *next* question.
        #
        # `_kind` is a prefix test, so the kind stays "slow" and the `partial`
        # split below picks the findings up unchanged.
        if found and _kind(text) == "slow":
            body = _plan_run.as_text(found)
            text = (f"{text}\n\n{body}" if text.startswith(TOO_SLOW_PARTIAL)
                    else f"{TOO_SLOW_PARTIAL}\n\n{body}")

        frame = {"type": "plan", "id": call_id, "result": text}
        kind = _kind(text)
        if kind:
            frame["error"] = kind
            # Where the answer starts, found here rather than in the client.
            # The other three kinds are wholly instructions to the model and
            # the card is right to throw them away; this one has a real answer
            # after them, and only the side that owns the wording knows where
            # the seam is. Sending the offset as a second field is what keeps
            # `cards.js` from carrying a copy of the English to split on.
            if text.startswith(TOO_SLOW_PARTIAL):
                frame["partial"] = text[len(TOO_SLOW_PARTIAL):].strip()
        await screen.push(session_id, frame)
    return {"answer": text}


def open_session(session_id: str,
                 conn_id: str | None = None) -> set[asyncio.Task]:
    """Track this connection's plans, and hand back the set as a token."""
    return conn.opened(_RUNNING, session_id, set(), conn_id)


def running_of(session_id: str) -> set[asyncio.Task] | None:
    """The plans of the connection asking, or the newest on this session."""
    return conn.own(_RUNNING, session_id)


def close_session(session_id: str,
                  running: set[asyncio.Task] | None = None) -> None:
    """Drop every plan the connection that is leaving had in flight.

    Same two-part rule as `gws.close_session`: its own plans always, its own
    registry entry and no other, and `_ASKING` — which is keyed by id alone and
    shared by every connection on it — only once the last of them has gone.
    """
    gone = conn.taken(_RUNNING, session_id, running)
    tasks: set[asyncio.Task] = set()
    for entry in (gone if running is None else [running]):
        tasks |= set(entry)
    if session_id not in _RUNNING:
        _ASKING.pop(session_id, None)
    logger.info("plan close_session %s, cancelling %s", session_id, len(tasks))
    for task in tasks:
        task.cancel()


def _tools() -> list[FunctionTool]:
    """The planner's tools: the live agent's, each charged one step.

    The same eight grounding tools the live agent has, plus `gws` and `browse`.
    Nothing is duplicated by sharing them — the tool objects are stateless, and
    :func:`~._plan_run.counted` builds a second tool over the same function
    rather than a second copy of the work behind it.

    `gws` and `browse` lose their `response_scheduling` on the way, which costs
    nothing: ADK only honours NON_BLOCKING at the top of a live run, so under a
    planner they were going to run to completion in place either way. It is not
    what makes the planner serial, either — several tools named in one model
    turn run together whatever their scheduling says, one `asyncio.create_task`
    each, which is what the instruction now asks for.

    Two are not charged. `show_plan` draws the card, and `note_finding` writes
    a line to the scratchpad — neither goes anywhere or waits on anything, and
    charging a step for either would buy a planner that stops narrating and
    stops writing things down exactly when the plan is long enough to need it.
    """
    shared = (
        place_info_tool,
        weather_tool,
        web_search_tool,
        image_search_tool,
        youtube_search_tool,
        maps_search_tool,
        maps_route_tool,
        maps_search_along_route_tool,
        browse_tool,
        calculate_tool,
    )
    # Every one of them `Guarded`, charged or not: a raise from any tool here
    # ends the sub-run and costs the plan its whole answer, and the two that
    # cost no step are no likelier to be well-behaved than the ten that do.
    # `show_plan` in particular is called with whatever the model put in
    # `steps`, and `_within` sanitises `running` and `done` but not that.
    return [
        _plan_run.Guarded(_plan_run.show_plan),
        _plan_run.Guarded(_plan_run.note_finding),
        *(_plan_run.counted(tool) for tool in shared),
        # Built from the function rather than from `build_gws_tool()`, which
        # also assigns the Workspace sub-agent to a module global. That global
        # belongs to whoever built the live agent, and building it twice would
        # leave two sub-agents where the second silently wins.
        _plan_run.Counted(gws_mod.gws),
    ]


def build_plan_agent() -> Agent:
    """Build the planner.

    A function rather than a module-level singleton for the same reason
    ``build_agent`` is one: the model name comes from .env, which main.py loads
    at startup. Separate from :func:`build_plan_tool` so it can be run on its
    own — `tools/plan_check.py` builds it without a live agent in front of it.
    """
    return Agent(
        name="plan",
        description=DESCRIPTION,
        input_schema=PlanRequest,
        # A thinking model, not a live one. The audio path is not in its way,
        # so it can take the seconds that deciding what to do next costs.
        # Pinned to the global endpoint, which is the only one serving these.
        model=Gemini(
            model=os.getenv("PLAN_MODEL", "gemini-3.8-flash"),
            client_kwargs={"location": os.getenv("PLAN_LOCATION", "global")},
        ),
        instruction=_instruction,
        # Where a turn is counted. ADK calls this once per model round trip,
        # inside the task `plan` creates, so `_plan_run._BUDGET` is in context.
        # The budget is in turns because that is what the person waiting is
        # spending — see `_plan_run.WARN_AT`.
        before_model_callback=_plan_run.on_model,
        tools=_tools(),
    )


def build_plan_tool() -> FunctionTool:
    """Wrap the planner as a tool the live agent can call."""
    global _agent
    _agent = build_plan_agent()

    # NON_BLOCKING, so the conversation stays open while it works, and
    # INTERRUPT rather than browse's WHEN_IDLE for the same reason `gws` picks
    # INTERRUPT: this is the answer to the question the person asked and is
    # standing there waiting for, not a train time worth holding for a gap.
    tool = FunctionTool(plan)
    tool.response_scheduling = types.FunctionResponseScheduling.INTERRUPT
    return tool
