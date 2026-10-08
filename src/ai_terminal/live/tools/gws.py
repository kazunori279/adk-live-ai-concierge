"""Google Workspace as an agent tool, driven by the `gws` CLI.

The live agent cannot drive a CLI itself: reading `gws --help`, picking a
subcommand and turning JSON into a sentence takes several model turns, and the
Live API has one turn to spend. So this is a sub-agent on its own ``Runner``.
It runs on a small, fast text model, works through however many round trips it
needs, and hands back one short Markdown answer.

The tool the live agent sees is not that sub-agent but :func:`gws`, which drives
it. The sub-agent cannot say anything until it is finished, and half a minute of
silence is long enough that saying where the work has got to is worth the
indirection — see :mod:`._progress`.

:func:`_ask` runs the ``Runner`` itself rather than going through ADK's
``AgentTool``, which builds one inside its own ``run_async`` and so leaves no
way to pass a ``RunConfig`` per call. ``model_input_context`` lives there, and
it is the only place a document can be put in front of the sub-agent's first
turn without also putting it in the session history and the prompt prefix. The
run is otherwise what ``AgentTool`` did: a fresh session per call, the request
marshalled through :class:`GwsRequest`, and the last content's text merged.

What it knows about `gws` comes from ADK Skills: the `SKILL.md` files under
``live/skills/``, written by `gws generate-skills` and vendored so the Pi does
not need a `~/.claude` to work from. Only their one-line descriptions sit in
the prompt; the body of a skill is fetched with `load_skill` when the
sub-agent decides it needs it.

Gmail, Drive and Docs are the exception, and ``## Gmail`` and ``## Running
gws`` in the instruction are where they live instead. A document question used
to open with three `load_skill` calls and then two more asking the CLI to
describe an export — five turns and around fifteen seconds before anything was
read. The skills are also the wrong shape for it: `gws-drive` is 16 KB of API
reference for resources this sub-agent never touches, and both it and
`gws-docs` end by telling the model to inspect every method before calling it,
which is exactly the two turns worth deleting.

Gmail is the same shape and was left on the skill path until #249 measured it.
Four mail questions on 2026-09-11 each spent two or three turns and 5.0 to
6.7 s before their first Gmail request, and none of the six in that log reached
`+triage` on its own: `gws-gmail` links the helper commands out to six separate
skill files and then documents the raw `users.messages` API in their place, so
one `load_skill` buys the API that returns ids and not the helper that returns
subjects. That is the cost this section exists to delete, and it is the same
saving `agenda` made for the calendar.

Anything gws exports or downloads lands in ``gws_cache/``. See ``_cache_name``
and ``write_index`` below for what that involves.
"""

import asyncio
import functools
import json
import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.skills import load_skill_from_dir, prompt
from google.adk.tools import FunctionTool, ToolContext
from google.adk.tools.skill_toolset import SkillToolset
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from pydantic import BaseModel, Field

from .. import conn, screen
from .._log import _sized
from . import (_agenda, _gws_cli, _gws_run, _plan_run, _progress, _sources,
               _timing, gws_cache)
from ._gws_run import gws as gws_command
from ._lang import SCREEN_STYLE, of_turn

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parents[1] / "skills"

NO_ANSWER = (
    "Workspace searched but did not manage to put an answer together. Say so, "
    "and offer to try again with a narrower question."
)

# And when it raised rather than gave up. Both of these are read by the model
# as the answer, so both have to be unambiguous: the agent is told never to
# claim a write happened until Workspace confirms it, and after "add that to my
# calendar" a vague failure and a slow success sound identical.
FAILED = (
    "The Workspace request failed and nothing was done — no answer, and no "
    "change to their account if you asked for one. Say that plainly, say "
    "nothing about what the answer might have been, and offer to try again."
)

DESCRIPTION = (
    "Google Workspace: the person's own calendar, Gmail, Drive, Docs and "
    "Keep notes. Use for their schedule, their mail, their files and their "
    "notes — anything that lives in their account rather than on the public "
    "web. Ask for what you want in a sentence; reading and writing both work."
)

# And when it was still going and had to be stopped. The budget caps the
# commands the sub-agent can run, not the model turns it can spend rephrasing
# between them — and a refused command comes back in milliseconds, so the
# budget does nothing to slow that loop down.
#
# Sized on the mailbox question, which is the dearest one the bench knows and
# the one the model choice rests on. Thirteen runs at HEAD on 2026-09-09 spread
# from 49.3 s to 130.5 s around a 58 s median, and the median is not what a
# timeout is for. The slowest run traced wanted its eleventh call 79.7 s in,
# which with `_gws_run.RESERVE` held back to write the answer in needs 99.7 s
# against the 100.0 this used to be — a third of a second of margin, and the
# 130.5 s run had none. 160 leaves the slowest run measured inside it with the
# reserve intact. See "What Flash bought, and what it cost" in the README.
#
# Nothing about this number is local. `LATE_TOOLS.gws.deadline` in app.js sits
# 1.2x above it so the screen hears the tool give up rather than giving up on
# it, `plan.TIMEOUT` is derived from it, and `_agenda`'s own 75 s worst case
# (45 s for `_calendars`, then `MAX_SECONDS`) is measured against it.
TIMEOUT = 160.0

TOO_SLOW = (
    "Workspace ran too long and was stopped, so there is no answer and nothing "
    "was changed. Say that plainly and offer to try again with a narrower "
    "question."
)

# And when there is nothing to drive: no `gws` on the machine, or a sub-agent
# that was never built. Said before the first subprocess rather than after the
# CLI has failed a few times, because a missing binary comes back as an
# ordinary tool result and the sub-agent retries ordinary tool results.
UNCONFIGURED = (
    "Workspace is not set up on this terminal, so there is nothing to ask. "
    "Tell them plainly that you cannot reach their account here."
)

EXPIRED = (
    "Google Workspace access has expired. Tell the person plainly that "
    "their Google Workspace access has expired, and ask them to open Settings "
    "on the screen and reconnect their account."
)

# The five ways a call ends with no answer, as the card knows them.
_NOT_AN_ANSWER = {
    FAILED: "failed",
    NO_ANSWER: "empty",
    UNCONFIGURED: "unset",
    TOO_SLOW: "slow",
    EXPIRED: "expired",
}

# What a repeat call is told.
ALREADY = (
    "This exact question is already running and its answer is coming. Do not "
    "call gws for it again, and say nothing — the person has been told you are "
    "looking. Wait."
)

# And what a repeat made from inside a plan is told, which is a different
# situation with the same cause. The planner shares the live session's
# `_ASKING`, so a question the live agent is already waiting on is refused here
# too — but that answer is going to the live agent and not to the planner, so
# "wait, it is coming" is not true from where the planner is standing. A
# planner that re-asks after a revision is not refused at all: the entry is
# cleared as each call ends, and its steps run one after another.
ALREADY_NESTED = (
    "That exact question is already running elsewhere and its answer will not "
    "come back to you. Do not ask it again. Carry on with the rest of the plan "
    "and say in your answer that you did not get this part."
)

# The Workspace questions each live session has in flight, by session id. The
# native-audio model re-dispatches a call it is still waiting on — one progress
# note is enough to set it off, and it does not stop at one — and every copy
# would be another sub-agent running the same mailbox search. Ten of them in
# thirty seconds is what this is for.
_ASKING: dict[str, set[str]] = {}

# And the sub-agent runs themselves, so a socket that closes takes them with
# it. Same shape and same reason as `browse._JOBS`: the task outlives the tool
# call that started it, and nothing else holds a handle on it. Leaving one
# going matters more here than it looks — the sub-agent writes as well as
# reads, so an abandoned run can still create a calendar event for a
# conversation that ended, with nobody left to be told about it.
# Keyed by session id and then by connection, for the reason `browse._JOBS`
# is: a session id is one per profile, so two pages hold it at once and one
# entry between them means whichever page leaves first takes the other's work
# out of reach. Here that is a sub-agent run nobody can cancel any more — and
# this one writes as well as reads, so an abandoned run can still put an event
# in a calendar for a conversation that ended.
_RUNNING: dict[str, dict[str, set[asyncio.Task]]] = {}


def open_session(session_id: str,
                 conn_id: str | None = None) -> set[asyncio.Task]:
    """Start tracking this connection's work, and hand the set back as a token.

    The set itself is the token — see :func:`close_session`, which tells "the
    connection I opened" from any other on the same id by identity, with no
    second dictionary to keep in step.
    """
    return conn.opened(_RUNNING, session_id, set(), conn_id)


def running_of(session_id: str) -> set[asyncio.Task] | None:
    """The work of the connection asking, or the newest on this session."""
    return conn.own(_RUNNING, session_id)


def close_session(session_id: str,
                  running: set[asyncio.Task] | None = None) -> None:
    """Drop everything the connection that is leaving had in flight.

    Its own work, always, and its own registry entry and no other: session ids
    outlive a reload now and two pages on one profile share one indefinitely,
    so clearing more than this connection's would leave somebody else's work
    untracked and uncancellable.
    """
    gone = conn.taken(_RUNNING, session_id, running)
    tasks: set[asyncio.Task] = set()
    for entry in (gone if running is None else [running]):
        tasks |= set(entry)
    # Questions in flight go with the last connection. `gws` clears its own
    # entry as each call ends, but a call that never ends — cancelled here, or
    # lost with the event loop — leaves one behind, and a stranded entry
    # silently refuses that question for the life of the process. Hung on the
    # session being empty rather than on owning the entry: `_ASKING` is keyed
    # by id alone and shared by every connection on it, so it must not go while
    # one of them is still asking, and it must not be left when the last one
    # leaves. Owning the entry answered the first and not the second, because
    # the connection that arrived second had already taken the first's place in
    # the dictionary and the first then matched nothing on its way out.
    if session_id not in _RUNNING:
        _ASKING.pop(session_id, None)
    logger.info("gws close_session %s, cancelling %s", session_id, len(tasks))
    for task in tasks:
        task.cancel()


class GwsRequest(BaseModel):
    """The message the sub-agent is handed, as a JSON document.

    A schema rather than a bare request string, so the language of the turn is
    part of the contract here as it is for the grounding tools. It is also the
    agent's ``input_schema``, which on this code path declares nothing to
    anyone: ADK reads ``input_schema`` only on the agent-as-a-tool path, and
    :func:`_run` drives a plain ``Runner``. So the bare-JSON shape
    below is a convention the sub-agent's instruction relies on rather than one
    anything enforces, and what each field is for is spelled out twice over: in
    the sub-agent's instruction, which reads them, and in the docstring of
    :func:`gws`, which is what the live agent is shown.

    The message carries one field this schema does not: `now`, the clock, added
    by :func:`first_turn` after validation. It is left undeclared because this
    describes what the sub-agent receives from whoever asked, and nothing
    upstream supplies a clock — :func:`gws` has no such parameter, and the
    `args` it builds are the three fields that follow and nothing else.
    """

    request: str = Field(description="What the assistant wants, in a sentence.")
    context: str = Field(
        default="", description="What the person is doing and why they asked."
    )
    lang: str = Field(
        default="", description="BCP-47 tag of the language to answer in."
    )


BEFORE_DOCUMENTS = """\
You operate the `gws` command-line tool, which is a thin wrapper over the
Google Workspace REST APIs, on behalf of a voice assistant. The assistant reads
your answer out loud and shows it on the screen beside it.

Your message is a JSON object. `request` is what the assistant wants, in plain
language. `context` is a paragraph on what the person is doing and what earlier
turns already found — why they are asking, which the request itself never says.
`lang` is the BCP-47 tag of the language they are speaking. `now` is the clock,
read when the question arrived.

## How to work
1. Gmail, Drive and Docs are written out below — under `## Gmail` and
   `## Running gws` — so start there, not with a skill. For Calendar or Keep,
   load that service's skill before your first `gws` call; it carries the
   exact syntax.
2. Run `gws` with the `gws` tool. If a command fails, read the error: a bad
   flag or a missing required parameter is worth one corrected retry, and a `+`
   shortcut that will not read one particular item is worth one attempt through
   the plain API. Neither is worth a third. An auth error is worth none —
   report it and stop.
3. `gws <service> --help` is for a case neither the section below nor a skill
   covers. It is not for the commands written out below.
4. Stop as soon as you can answer. A command that returned what was asked for
   is the answer: do not confirm it by fetching the same thing another way, and
   do not take apart something one command already covers. The `agenda` tool
   below is the calendar in one call — listing the calendars and querying each
   one in turn is seven round trips for a result you already had.
5. One search is the search. A query that came back with results has done its
   job. Narrowing it, widening it, or asking the same thing through
   `users messages list` instead of `+triage` is the same question in another
   dialect, and it will hand you the same answer. Refine once when the first
   result genuinely missed; never twice.

**You have twelve `gws` calls, and the twelfth is enforced** — past it the tool
stops running commands and you answer from what you have. `agenda` spends one
of them and is the exception to the enforcement: it still runs when the budget
is gone, because a question that has spent eleven calls getting nowhere is
exactly the one where reading the calendar is the answer. That is not licence
to call it twice. Six is what a good
answer usually costs. Someone is standing in front of the terminal waiting for
you and every call is another three seconds of silence, so a good answer from
three messages beats a complete one from thirty. When a search returns more
than you can read, read the few that `context` says matter and answer from
those, saying how many there were in total. Running out with nothing to say is
the only real failure here — spend the calls on the answer, not on being
thorough.

The assistant sometimes spells out a date in its request, and sometimes gets
it wrong. Take the day it means — "tomorrow", "next Friday" — and work the
date out from `now` in your message.

## Calendar
For anything about what is on, call the `agenda` tool. It reads their own
calendar and hands back the whole answer: titles, times already in
their timezone, places, meeting links, how many people are invited, and the
pairs of events that run over each other. It is one call and it is enough — do
not follow it with `gws calendar +agenda` or `events list`, which cost another
round trip to return less than you already have.

Four things about what comes back:

1. **It is their day, not the household's.** Only their own calendar is read.
   A family member's, the shared family one, a work one, a holiday feed — none
   of them, whatever rights the account holds over them — so nothing that comes
   back needs attributing to anybody. Do not go looking for the rest with
   `calendarList list`; there is no rest, and a week's worth of chasing is how
   a one-call question turns into six.
2. **Say the overlaps out loud.** `overlaps` is the most useful thing on the
   page and the one a person reading a list of times never notices. Lead with
   it if there is one.
3. **`your_rsvp` means they have not answered yet.** Worth mentioning
   unprompted — it is a thing they can still do something about.
4. **Say when the answer is short.** `overlaps_total` or `back_to_back_total`
   larger than its list, `truncated`, `unreachable`: one line each. A calendar
   that could not be read is not a calendar with nothing on it, and a day
   reported as clear when it was only unreadable is the one mistake here that
   costs them something.

`agenda` reads. Everything else about the calendar — creating an event, moving
one, replying to an invitation, hunting for one event by name across months —
is still `gws calendar` through `gws_command`. Pass `--format json` when you
do: its help says JSON is the default and it is not, and asking twice is a
wasted call.

## Gmail
Two commands cover nearly every mail question. **Run them as written — no
`load_skill` for them, no `--help` and no `gws schema` first.** `gws-gmail`
links its helper commands out to six separate skill files and documents the raw
`users.messages` API in their place, so loading it costs a round trip and hands
back the more expensive way to ask.

Search the mailbox and read what matched, in one call. `--query` takes Gmail's
own search syntax, and this comes back with each match's sender, subject and
date rather than with a list of ids:
`gws gmail +triage --query 'from:amazon newer_than:2d' --max 20 --format json`

**That is the search.** `users messages list` runs the same query and returns
`id` and nothing else — no subject, no sender, no date, and no `fields`
projection can add them — so every match then costs a `+read` of its own. Five
round trips to learn what one `+triage` already said. Reach for it only when
you need something `+triage` does not carry.

Read one message in full, when the subject and the date are not the answer:
`gws gmail +read --id MESSAGE_ID --headers --format json`

Four things, each of which costs a call when it is got wrong:

1. **One query.** Put the whole constraint in the first time — `from:`,
   `subject:`, `newer_than:2d`, `has:attachment`, `is:unread`. A second search
   of the same mailbox in another dialect returns the same mail.
2. **Read the messages you need in one turn.** Three `+read`s for three ids
   out of one search do not depend on each other, and issued one at a time
   they are three model turns of somebody standing in front of the terminal
   waiting. Ask for them together.
3. **`+read` and `users messages get` are the same read.** Pick one. A message
   read through both is two calls and two turns for one body.
4. **`--format json` on both of them.** They print a table otherwise, and
   asking again for JSON is a wasted call.

Labels, drafts, threads, settings and anything that sends are not here: load
the matching `gws-gmail-*` skill for those. The two commands that pull a PDF
off a message are under `## Running gws` below.

## Running gws
`gws <service> <resource> [sub-resource] <method> [flags]`. No shell runs it, so
piping and redirection do nothing — but the command line is still split on shell
quoting rules, and a single quote inside a single-quoted `--params` or `--json`
is eaten rather than passed on. Where the value itself needs quotes, as a Drive
`q` does, put the JSON in double quotes and escape the ones inside it — the
search command below is the pattern.

| flag | takes |
|---|---|
| `--params '{"key": "val"}'` | path and query parameters |
| `--json '{"key": "val"}'` | the request body |
| `-o <name>` | write the response to a file instead of returning it |
| `--upload <path>` | send a local file |
| `--format json` | JSON out; the help calls this the default and it is not |
| `--page-all`, `--page-limit N` | paginate, NDJSON out |

Three commands cover nearly every document question. **Run them as written —
no `--help` and no `gws schema` first.** The skills tell you to inspect a
method before calling it; these three are the exception, and inspecting them
costs two round trips for a command already in front of you.

Search Drive. Drive's `q` grammar needs the search term in quotes of its own,
so this one is double-quoted outside and escaped inside:
`gws drive files list --params "{\\"q\\": \\"name contains 'itinerary'\\", \\"pageSize\\": 10, \\"fields\\": \\"files(id,name,mimeType,modifiedTime)\\"}"`

An apostrophe in the search term closes that inner quote early and Drive
answers 400, so `name contains 'Alex's trip'` is rejected outright. Search whole
words of the name that have no apostrophe in them — `name contains` is prefix
matching, word by word, so `trip` finds "Alex's trip" and needs no escaping at
all, while `rip` finds nothing. If `files` is empty (`[]`), do not call `files get`
or `files export`; state plainly that no matching file was found in Google Drive. Quoting needs `\\\\\\"`:
`gws drive files list --params "{\\"q\\": \\"name contains \\\\\\"Alex's trip\\\\\\"\\", \\"pageSize\\": 10, \\"fields\\": \\"files(id,name,mimeType,modifiedTime)\\"}"`

Metadata for one file, when you have the id and want its type or its title:
`gws drive files get --params '{"fileId": "FILE_ID", "fields": "id,name,mimeType,modifiedTime"}'`

Export a Google document as text:
`gws drive files export -o trip-itinerary.md --params '{"fileId": "FILE_ID", "mimeType": "text/markdown"}'`

The export `mimeType` depends on what the file is, and the wrong one comes back
as a 400 rather than as a file:

| the file is | export as |
|---|---|
| a Google Doc | `text/markdown` |
| a Google Slides deck | `text/plain`. There is no markdown export for slides. |
| a Google Sheet | `text/csv`, which gives you the first tab and no others. Say so if the answer could be on another tab. |
| not a Google format at all — a PDF, a `.docx`, an image | `files export` refuses it outright. `gws drive files get -o paper.pdf --params '{"fileId": "FILE_ID", "alt": "media"}'` downloads the bytes. |

Download a PDF when the answer is inside it. The cache reads the text out of
one as it lands and writes it beside the bytes as `paper.pdf.md`, so
`read_cache` and `search_cache` reach it like any other document — and
`read_cache('paper.pdf')` reaches it too, so either name works. A scan has no
text in it to read; that copy says so rather than coming back empty, and the
thing to do then is ask them for the figure itself.

Any other binary — an `.xlsx`, a `.docx`, an image — is still bytes nobody can
read back: listed as a bare filename, no title, no summary, and `read_cache`
returns an error for it. Download one of those only when they asked for the
file itself rather than for something in it.

A PDF attached to an email is two calls, and no Gmail skill covers them. First
the attachment's id, off the message:
`gws gmail users messages get --params '{"userId": "me", "id": "MESSAGE_ID", "format": "full", "fields": "payload/parts(filename,mimeType,body/attachmentId)"}'`
then the file itself, named after the `filename` that came back:
`gws gmail users messages attachments get -o invoice.pdf --params '{"userId": "me", "messageId": "MESSAGE_ID", "id": "ATTACHMENT_ID"}'`

Always pass `-o` on the second one. The API answers with the document inside a
JSON field rather than as a file, and `-o` is what tells the cache where to put
it; without one there is nothing to read afterwards.

For a service this section does not cover — Calendar, Keep — and for the
Gmail, Drive and Docs methods it leaves out, such as labels, drafts,
permissions, revisions, comments and document batch updates, load that skill.
Do not load `gws-shared`, `gws-drive`, `gws-docs` or `gws-gmail` to re-read
what is written above.

"""

# The paragraph that only holds when something is cached. Kept apart from the
# rest so `_instruction` can swap the whole of it, not a clause of it: with an
# empty cache every one of steps 1 to 3 names something that is not there, and
# a prompt that says look in the cache first and then says the cache is empty
# is asking the model to pick which half to believe.
DOCUMENTS = """\
## Documents
Exporting or downloading a document writes a file, and every file you write is
kept. The recent ones are listed below, each with the Drive id it came from and
a couple of sentences on what is in it.

1. **When a document is already in your message, that is the document.** A
   request that names one arrives with that file read, checked against Drive
   and quoted to you under a line saying so — or, when the Drive original has
   moved on since the copy was taken, with the command that fetches it again.
   Either way the looking up is done, and doing it a second time costs a round
   trip to arrive back where you already are. Most questions do not come this
   way, and steps 2 to 5 are what the rest of them do.
2. When the request is about a document, the cache is the first place you look
   — before any `gws` call at all, not merely before an export. A matching file
   id is the same document, whatever the copy is called; with no id in hand,
   match the request against the summaries, and use `search_cache` for the
   older ones the list below leaves out. Match on `context` as well as on
   `request`: "the itinerary" names no file, but a paragraph about a Portland
   trip tells you which cached one it is. Searching Drive for something you
   already have costs the same round trip as fetching it.
3. Read a match with `read_cache`. It checks with Drive that the copy is still
   current and tells you when it is not — that answer, and only that answer, is
   your reason to export the document again.
4. When you do export, name the file for what is in it: `-o trip-itinerary.md`,
   not `-o content.txt`. Re-fetching a document you already have? Write it to
   the name it already has, so the cache holds one copy of it and not two.
5. Do not paste a whole document back to the assistant. Answer the question it
   asked and say the file is cached.

"""

# The same paragraph with nothing cached: what is still true, and no more. The
# five steps become two, because the first three are all about finding a copy
# and there is no copy to find. The cache is still worth naming — it is what
# the export is for — but as something the next question reaches, not this one.
DOCUMENTS_EMPTY = """\
## Documents
Exporting or downloading a document writes a file, and every file you write is
kept, so a later question can read it back without fetching it again. Nothing
is cached yet: this one starts at Drive.

1. Name the file for what is in it: `-o trip-itinerary.md`, not
   `-o content.txt`. That name is what the question after this one has to
   recognise it by.
2. Do not paste a whole document back to the assistant. Answer the question it
   asked and say the file is cached.

"""

AFTER_DOCUMENTS = """\
Output paths are relative to the cache; a leading directory is dropped. An
`--upload` path is not, so give that one in full.

## Answering
A search returns what matched. `context` is what tells you which of it is worth
saying out loud, so read the results against it before you write anything.

It tells you what to notice in what you found. It does not tell you what else
to go and look for: answer the `request`, and do not open a second search
because the context mentioned a name. Asked about tomorrow's calendar, the
calendar is the answer, however interesting the trip sounds.

Lead with what the person is doing something about. Keep a detail the context makes load-bearing even when the
request never named it — a gate change, a delay, a check-in window about to close, a booking moved since an earlier
turn. Drop what the same intent makes noise: the airline's newsletter matched "flight" too, nobody needs it.

Ways to get this wrong, each worse than answering flatly:
- **Do not invent relevance.** `context` says what to look for, never a fact source: report missing if not found.
- **Do not over-filter.** Something contradicting context matters most: cancellations are what they need to hear.
- **Payload over envelope.** Notification messages (tickets, invoices, receipts, shipping confirmations,
  contracts, statements) often have brief or empty boilerplate bodies with substantive details in attachments
  (PDFs, spreadsheets). If `+read` returns a boilerplate notice, inspect `payload/parts` via `messages get`
  and fetch the attachment before concluding information is missing or falling back to older files.
- **Transactions supersede baselines.** Static documents in Drive or files are historical baselines; recent emails,
  invoices, and receipts are transactions that modify or add to that baseline. A newer transaction always supersedes
  an older baseline document. Never assert the non-existence of an option or current state from an older document if newer transactional messages exist in the timeline.
- **Dynamic fan-out over serial search.** For queries concerning current status, options, bookings, reservations,
  itineraries, or orders where information may span multiple services: on Turn 1, issue lightweight discovery calls
  (e.g. `gmail +triage` and `drive files list`) concurrently in parallel. Use the combined metadata timeline to target
  the latest transaction in Turn 2. For focused single-source asks (calendar, single list), use single-tool ReAct.

Answer about their day, their mail, their files — not the structure underneath. Nobody has six calendars, they have
a Tuesday. Do not sort by which calendar or account it came from; never lead with what an empty one lacks.

Reply in concise Markdown, in `lang` — not in the language `request` happens to
be written in, which is the assistant's, not the person's. Fall back to the
language of the request when `lang` is empty. Translate what you find as well:
a calendar entry titled in English belongs in a Japanese answer as Japanese.

This is spoken aloud and shown on screen. Never dump raw JSON, IDs or URLs.
Say plainly when nothing was found: an empty search is never an error or failure.

""" + SCREEN_STYLE + """

## Care
The assistant has already asked whoever is standing there, so do what the
request says — but only what it says. Never delete something to make room for
what you were asked to create, and never mail anyone the request did not name.
Never run a delete the request did not ask for.
"""

INSTRUCTION = BEFORE_DOCUMENTS + DOCUMENTS + AFTER_DOCUMENTS

# The half of step 2 that only means anything when the listing below it stops
# short. Held here as well as in the text so `_instruction` can take it back
# out; a `.replace` that no longer matches would fail silently, so
# `timing_check.py` holds this and the paragraph together.
SEARCH_TAIL = (
    ", and use `search_cache` for the\n   older ones the list below leaves out"
)

# What step 2 says instead when the listing leaves nothing out. The tool is
# still worth reaching for there: `_scored` weights the document body as well
# as the row the listing shows, so a question in a language the English
# summaries do not answer still has somewhere to go.
SEARCH_SHOWN = (
    ", and use `search_cache` when the\n"
    "   summaries do not name it — it searches the documents themselves,\n"
    "   not just these rows"
)


@functools.cache
def _skills() -> tuple:
    """The vendored gws skills, in a stable order.

    Cached: the instruction is rebuilt on every model turn, and 16 files off
    the Pi's SD card each time buys nothing — they cannot change without a
    restart anyway.
    """
    return tuple(
        load_skill_from_dir(path)
        for path in sorted(SKILLS_DIR.iterdir())
        if (path / "SKILL.md").is_file()
    )


def _instruction(_: ReadonlyContext) -> str:
    """Add the skill list and the cache to the static instruction.

    The skill list because ADK only injects it when `list_skills` is exposed,
    and spending a round trip on a fixed list of sixteen is a second of silence
    for nothing. The cache for the same reason: knowing what has already been
    fetched is what stops it being fetched twice.

    Not the clock, which used to be appended here at minute resolution and is
    now a field of the message :func:`first_turn` builds. ADK re-runs this on
    every model turn and hashes what it returns as one blob for the context
    cache, so a minute hand splits one question's turns across two entries
    every time they straddle a boundary. A cold document question runs this
    seven times over 24.5 s, which is four questions in ten. See #34.
    """
    # Timed as one step because it is rebuilt on every model turn, which makes
    # a cheap function called four times a question and a wasteful one called
    # four times a question look identical in a total.
    with _timing.step("instruction"):
        cached = gws_cache.listing()
        # Step 2 sends the model to `search_cache` for the documents the
        # listing leaves out, and under `LISTING_LIMIT` it leaves out none.
        # Told to search a tail that is not there, the model is being asked to
        # resolve a contradiction, and the way it resolves it is a round trip.
        #
        # Three states, not two. An empty cache has nothing to search and the
        # whole paragraph goes; a cache the listing shows all of still has the
        # document bodies, which the rows do not carry, so the clause is
        # replaced by one that is true rather than dropped.
        nothing_cached = cached == gws_cache.EMPTY
        if gws_cache.TAIL in cached:
            documents = DOCUMENTS
        elif nothing_cached:
            documents = DOCUMENTS_EMPTY
        else:
            documents = DOCUMENTS.replace(SEARCH_TAIL, SEARCH_SHOWN)
        # And no heading over nothing: a section whose entire content is a
        # sentence saying it has no content reads as a section that failed to
        # load. `DOCUMENTS_EMPTY` already says it, in the place the model is
        # being told what to do about it.
        listing = "" if nothing_cached else f"## Cached documents\n{cached}\n\n"
        body = BEFORE_DOCUMENTS + documents + AFTER_DOCUMENTS
        skills = prompt.format_skills_as_xml(list(_skills()))
    return f"{body}\n{listing}{skills}"


# The sub-agent, so it can be reached from inside `gws` below. A module global
# rather than an argument because ADK calls a tool with the model's arguments
# and nothing else, and it cannot be built at import time: the model name comes
# from .env, which main.py loads at startup.
_agent: Agent | None = None


def _nested_config(parent: RunConfig | None) -> RunConfig | None:
    """The caller's run settings, adjusted for a run nested inside them.

    Forwarded rather than replaced with a default ``RunConfig`` because the
    sub-agent runs as part of the caller's invocation and should be bounded by
    the same settings — `max_llm_calls` above all, which defaults to 500 and is
    the only ceiling on a sub-agent that has talked itself into a loop.

    Two fields cannot be forwarded as they stand, and both are lifted from
    ``AgentTool``'s own version of this:

    - `support_cfc` describes how the *caller's* model executes its code. Given
      to another agent it replaces that agent's code executor and then refuses
      to run at all unless the model is a Gemini 2 one.
    - the caller is BIDI, and only the last event's content becomes the answer
      here. That is the whole answer in unary mode and a partial chunk in a
      streaming one, so the nested run is always unary.
    """
    if parent is None:
        return None
    changed = {}
    if parent.support_cfc:
        changed["support_cfc"] = False
    if parent.streaming_mode != StreamingMode.NONE:
        changed["streaming_mode"] = StreamingMode.NONE
    return parent.model_copy(update=changed) if changed else parent


def _answer(last: types.Content | None, error: str | None) -> str:
    """The sub-agent's answer, out of the last content it produced.

    Thought parts are dropped. The sub-agent thinks in the open — half a dozen
    turns of picking a search and reading a document — and a thought merged into
    the answer is read out loud and drawn on the card as though it were one.

    Text parts only, where ``AgentTool`` also merged code-execution output and
    executable code. `run_skill_script` is deliberately not among the
    sub-agent's tools and it has no code executor, so those parts cannot arrive.

    `error` is the fallback rather than the answer: a run can log an error on
    one event and recover on the next, and the recovered answer is the one to
    return. It is only reached when there is no text at all, which is also when
    it is the only thing left to say.
    """
    parts = (last.parts or []) if last is not None else []
    merged = "\n".join(p.text for p in parts if p.text and not p.thought)
    return merged or error or ""


# `gws_cache.named` matches on the phrase alone, deliberately, so this block
# goes in front of every request that happens to name a cached document — the
# Gmail one and the sharing one as much as the read one. It therefore says what
# the text is, a copy provided in case the question is about it, and not what to
# do with it. An earlier wording ended "answer from what is here", which read as
# an instruction to answer a question about mail out of a Drive document.
ALREADY_READ = """\
The request names `{name}`, which is in the cached documents. It has been read \
for you and checked against Drive, and its text is below. Do not call \
`read_cache` or `search_cache` for it and do not export it again. If the \
request is about what the document says, answer from the text below. If it \
needs another service (mail about it, an event from it, sharing it), do that, \
and use this text as the document's contents.\
"""

# Only the first MAX_READ characters of a long document are cached at all, so
# this says nothing `read_cache` would not have said; it is here because a
# document that stops mid-sentence otherwise reads as one that ends there.
CUT_OFF = " The document is longer than this and is cut off at the end."

ALREADY_STALE = """\
The request names `{name}`, which is in the cached documents, but the Drive \
original has been edited since that copy was taken, so the copy cannot be \
answered from. Fetch it again with `{source}` — writing to the same filename \
replaces the copy. Do not call `read_cache` for it first; that is the check \
that has already been made.\
"""


async def _prefetch(request: str) -> types.Content | None:
    """The document this request names, read and ready for the first turn.

    A warm document question is two turns, and the first of them produces one
    `read_cache` call and nothing else — three seconds to type a filename that
    is already in the prompt. This is that turn, done here instead.

    Through :func:`~gws_cache.read_cache` rather than reading the file, because
    the freshness check lives inside it. A resolver that opened the file
    directly would answer fluently out of a copy Drive has moved past, which is
    worse than the turn it saves. The stale branch is inlined too: the sub-agent
    needs the export command, and it would otherwise spend the same turn being
    told to fetch the document again.

    Returns None to leave the question exactly as it was. Nothing is injected to
    say the lookup failed — a note that the cache was searched and came up empty
    invites the model to trust a matcher that has just said it cannot judge.
    """
    with _timing.step("resolve") as timed:
        name = gws_cache.named(request)
        # Whether, not which. `named` returns a name only when a phrase out of
        # it was said out loud, so the name is a private document somebody in
        # the house asked about, and `mark` puts every field of this record
        # into the journal with `ensure_ascii=False`. A boolean answers what
        # the field is asked — did the resolver fire — and `_sized` would not:
        # a filename's length says nothing a reader can use and still narrows
        # which of the cached documents it was. See #234, and #206 for why the
        # journal is the store that matters.
        timed["hit"] = name is not None
        if name is None:
            return None
        found = await gws_cache.read_cache(name)
        text = found.get("text")
        if text:
            note = ALREADY_READ.format(name=name)
            if found.get("truncated"):
                note += CUT_OFF
            body = f"{note}\n\n{text}"
        elif found.get("source"):
            body = ALREADY_STALE.format(name=name, source=found["source"])
        else:
            # `read_cache` refused for a reason the sub-agent cannot act on —
            # the file went between `named` reading the directory and this line,
            # or it stopped being readable. Hand the question over untouched.
            #
            # `why` and not `error`: three of the four refusals that reach here
            # spell a filename inside the advice they give the sub-agent — the
            # path in an `OSError`, and the sidecar's name in each of the two
            # PDF branches — so printing the string undoes the sizing one
            # argument along. The tags are a closed set (`gws_cache.py:2180`
            # and below) and say more about why the resolve was dropped than
            # the prose did. #234.
            logger.info("gws prefetch: %s unusable, %s", _sized(name),
                        found.get("why", "?"))
            return None
        timed["chars"] = len(body)
    return types.Content(role="user", parts=[types.Part.from_text(text=body)])


async def first_turn(
    args: dict, config: RunConfig
) -> tuple[types.Content, RunConfig]:
    """One question, as the message and settings to start a run with.

    Split out of :func:`_run` so `tools/latency_report.py` can measure the same
    first turn the server takes. That script builds its own ``Runner`` around
    :func:`build_gws_agent` — it has no live agent and so no `ToolContext` to
    reach `_run` through — and a resolver it did not call is a resolver its
    tables would report as having cost nothing and saved nothing.
    """
    # A bare JSON document and nothing else. Nothing enforces this — ADK
    # validates `input_schema` only on the agent-as-a-tool path
    # (`tools/agent_tool.py:234` in 2.8.0), and this drives a plain `Runner` —
    # but the instruction tells the sub-agent to read fields out of a JSON
    # object, and a trailing sentence of prose is readable as part of
    # `request`.
    #
    # The clock is added here rather than being a field of `GwsRequest`,
    # because that schema is the sub-agent's half of the contract and not the
    # caller's: the live agent is shown `gws()`'s own signature
    # (`FunctionTool(gws)`), and `args` is built from it. A `now` declared on
    # `GwsRequest` would be a field only the server ever fills in, written
    # where the sub-agent reads it as though the caller had sent it. Here, and
    # not in the instruction where it used to be, because the instruction is
    # hashed whole for the context cache and a minute hand in it stops a
    # question's own turns matching each other. The message is outside that
    # hash, and it is read
    # once per question rather than once per turn, so the sub-agent's clock
    # also stops moving underneath it mid-question.
    fields = GwsRequest(**args).model_dump(exclude_none=True)
    fields["now"] = f"{datetime.now().astimezone():%A %Y-%m-%d %H:%M %Z}"
    message = types.Content(role="user", parts=[types.Part.from_text(
        text=json.dumps(fields, ensure_ascii=False))])

    # `model_input_context` and not a fourth field on `GwsRequest`, which is
    # where the document would otherwise go: the Runner does not persist it to
    # the session, so a 20 000-character document is in the one request that
    # needs it and not in the history behind every turn after it. It is also
    # not the instruction, which is rebuilt per turn and hashed verbatim for
    # the context cache — splicing a per-question document in there makes a
    # prefix that was byte-identical across a question's turns unique to each.
    document = await _prefetch(args.get("request", ""))
    if document is not None:
        config = config.model_copy(update={"model_input_context": [document]})
    return message, config


async def _run(args: dict, tool_context: ToolContext) -> str:
    """Run the sub-agent over one question and return the text it ended on.

    A ``Runner`` per call, built here rather than hoisted, because a hoisted one
    would pin the first call's `tool_context` for the life of the process. This
    repo passes no artifact or credential service today, so nothing is pinned in
    practice — but a `Runner` costs 0.006 ms to build and a session 0.010 ms, so
    the right lifetime is free and the wrong one is a trap for whoever adds the
    first one.

    A fresh session per call, which is also what ``AgentTool`` did. The
    sub-agent is stateless by design and `context` is how the live agent carries
    a conversation into it; see the `context` argument of :func:`gws`.

    Parent session state is not seeded in. ``AgentTool`` did seed it and forward
    the deltas back, but the sub-agent's four tools read no state and write
    none, so all that plumbing ever carried was `home_location` and
    `home_coords` — which `maps` and `weather` read on the live agent's side,
    where they still are.
    """
    invocation = tool_context._invocation_context
    app_name = invocation.app_name or _agent.name
    sessions = InMemorySessionService()
    session = await sessions.create_session(
        app_name=app_name, user_id=invocation.user_id)
    runner = Runner(app_name=app_name, agent=_agent, session_service=sessions)
    message, config = await first_turn(
        args, _nested_config(invocation.run_config) or RunConfig())

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
    finally:
        # MCP sessions and anything else the Runner opened. Skipping it raises
        # "attempted to exit cancel scope in a different task" out of whichever
        # task happens to collect it later, which is not this one.
        await runner.close()

    return _answer(last, error)


async def _ask(args: dict, tool_context: ToolContext) -> str:
    """Put the question to the sub-agent and return its answer as text.

    Never raises. A NON_BLOCKING tool that does runs inside ADK's background
    task, whose entire recovery is ``logger.exception`` — nothing reaches the
    live request queue, so the model is never told the call is over and the
    card on screen waits for an answer that is not coming. Turning the failure
    into text keeps all three channels working: the model has something to say,
    the screen has something to draw, and the person is told.

    The sub-agent shells out to a CLI and talks to a model over the network, so
    this is the ordinary case rather than the exotic one: an expired token, a
    non-zero exit, a 429, the Pi losing Wi-Fi mid-call.
    """
    if _agent is None or not _gws_cli.available():
        logger.warning("gws unconfigured: no CLI at %r", _gws_cli.binary())
        return UNCONFIGURED
    try:
        async with asyncio.timeout(TIMEOUT):
            with _timing.step("subagent"):
                answer = await _run(args, tool_context)
    except TimeoutError:
        # Sized, not printed, on all five of this module's request lines:
        # `request` is what the person asked, in the model's words, `main.py`
        # sizes it on the way in (`_log_tool_traffic`), and printing it two
        # frames down puts it back in the same persistent journal. See #206.
        logger.warning("gws timed out after %ss for %s",
                       TIMEOUT, _sized(args.get("request", "")))
        return TOO_SLOW
    except _gws_cli.GwsAuthExpiredError:
        logger.warning("gws auth expired for %s", _sized(args.get("request", "")))
        return EXPIRED
    except Exception:  # not BaseException: a cancelled call has not failed
        # `logger.exception` too: the traceback carries no locals, so the
        # format string is the whole of what reaches the log.
        logger.exception("gws failed for %s", _sized(args.get("request", "")))
        return FAILED
    text = answer if isinstance(answer, str) else str(answer)
    # Seen once in a dozen runs: the sub-agent spends its whole budget
    # searching and stops without writing anything. An empty string reads to
    # the model as a successful answer with nothing in it, and to the screen as
    # an empty box. Say plainly that it failed instead.
    if not text.strip():
        logger.warning("gws returned nothing for %s",
                       _sized(args.get("request", "")))
        text = NO_ANSWER
    return text


async def gws(
    request: str,
    tool_context: ToolContext,
    context: str = "",
    lang: str = "",
) -> dict:
    """Ask the person's own Google Workspace — calendar, Gmail, Drive, Docs, Keep.

    Use for their schedule, their mail, their files and their notes: anything
    that lives in their account rather than on the public web. Ask for what you
    want in a sentence; reading and writing both work.

    It answers late — a few seconds for a calendar question, half a minute for
    a mailbox one — and says where it has got to on the way. Those updates are
    not the answer: relay one and keep waiting.

    Args:
        request: What you want, in a sentence, e.g. "what is on my calendar
            tomorrow afternoon?".
        context: A short paragraph — three or four sentences at most — on what
            the person is doing and what earlier turns already found out for
            it, e.g. "They have been planning a trip to Portland. Earlier turns
            pulled the hotel confirmation (Oct 3-6) and the rental car booking.
            They are packing now and working through the departure day." You
            are the only thing in the system that knows this: Workspace is
            stateless and sees nothing but this one message, so without it
            every question is asked from a standing start. Write what they are
            doing, not what to answer.
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. The answer is shown on screen as well as read out, so it is
            written in this language.

    Returns:
        The answer, in Markdown.
    """
    lang = of_turn(lang, tool_context)
    # The live session, which under `plan` is not the one this context is in —
    # see `screen.session_of`. The call id stays this call's own, so a nested
    # answer lands in a card of its own rather than in the plan's.
    session_id = screen.session_of(tool_context)
    call_id = tool_context.function_call_id
    asking = _ASKING.setdefault(session_id, set())
    if request in asking:
        logger.info("gws already asking: %s", _sized(request))
        # Take the empty card the repeat put up back off the screen, so the
        # page shows one question waiting rather than a column of them. A
        # nested call never put one up and this is a no-op for it.
        await screen.push(session_id, {
            "type": "gws", "id": call_id, "drop": True,
        })
        return {"already_running": True,
                "note": ALREADY_NESTED if _plan_run.nested() else ALREADY}

    asking.add(request)
    work: asyncio.Task | None = None
    # The set this call's task goes into, held rather than looked up twice: the
    # `finally` has to take the task out of the same set it went into, and the
    # lookup can answer differently by then — a connection leaving takes its
    # entry with it, and `own` would fall through to somebody else's.
    running: set[asyncio.Task] | None = None
    # Bound before the `try`, because a raise on the way in still reaches the
    # frame at the bottom and an unbound name there would lose the answer as
    # well as the calendar.
    drawn: dict = {}
    found: _sources.Ledger | None = None
    # Everything below hangs off one question, named by the function-call id
    # because that is the identifier the card, the log and the model already
    # share. `question` is the grouping and `total` is the wait the person
    # actually sits through, so it closes after the card is pushed rather than
    # when the sub-agent stops talking.
    with _timing.question(call_id), _timing.step("total"):
        try:
            notes = _progress.Notes(tool_context, "gws")
            args = {"request": request, "context": context, "lang": lang}
            # The budget is per question, and the task has to be created inside
            # it to inherit it — see `_gws_run.budget`. The memo the same way,
            # and here rather than beside the budget in `plan.py`: a budget is
            # shared across a plan's gws steps on purpose and a memo must not
            # be — see `gws_cache.remembering`.
            #
            # The same `TIMEOUT` that `_ask` wraps the run in, so the budget
            # stops spending calls with `RESERVE` seconds still on the clock
            # rather than being cancelled partway through the answer it just
            # asked for. Under a plan this entry is nested and contributes no
            # count, but it is still the one that puts the *step's* clock in
            # force — and that clock, not the plan's, is what cancels at the
            # `asyncio.timeout` in `_ask`.
            #
            # `showing` is scoped the same way and for the same reason: the
            # card it fills belongs to this question, and a plan's four gws
            # steps each draw one of their own. The source ledger too — and it
            # is held in `found` because the frame is built after the block has
            # closed, see `_sources.collecting`.
            with _gws_run.budget(seconds=TIMEOUT) as share, \
                    gws_cache.remembering(), _agenda.showing() as drawn, \
                    _sources.collecting() as found:
                work = notes.start(_ask(args, tool_context))
            # The share has to outlive the block that opened it — see
            # `_gws_run.hold`. Nothing awaits between the two, so the count
            # never reads low.
            _gws_run.hold(share, work)
            # A lookup and never a create: `open_session` is the only thing
            # that should put an entry here. This runs in a background task,
            # and the socket can close between the two — an entry put back
            # afterwards has nothing left to ever cancel what goes into it. No
            # entry means every connection on the session is gone, and the
            # `finally` below cancels the task either way. This connection's
            # set where it can be found, so the work dies with the socket that
            # asked for it rather than with whichever page closes first.
            running = running_of(session_id)
            if running is not None:
                running.add(work)
            notes.opening()
            await notes.watch(work)
            text = await work
        except asyncio.CancelledError:
            raise
        except _gws_cli.GwsAuthExpiredError:
            logger.warning("gws auth expired to run %s", _sized(request))
            text = EXPIRED
        except Exception:
            # Not only the sub-agent — `_ask` guards itself. This covers the
            # plumbing around it: building the channel, sending the opening
            # turn, the watch loop. A raise in any of those reaches ADK's
            # background task, whose entire recovery is one log line, and the
            # call goes silent on all three channels. That is this tool's
            # oldest bug; the fix only holds if every path out of here produces
            # an answer.
            logger.exception("gws failed to run %s", _sized(request))
            text = FAILED
        finally:
            if work is not None:
                # A no-op once it has answered. The point is the paths where it
                # has not: a raise above, or the socket closing under us.
                work.cancel()
                if running is not None:
                    running.discard(work)
            asking.discard(request)
            # `is`, not just emptiness: `close_session` may have popped this
            # set and a later call put a fresh one under the same id. Popping
            # that one because this one is empty loses the repeat-suppression
            # for the question now in flight, which is the one thing `_ASKING`
            # is for.
            if not asking and _ASKING.get(session_id) is asking:
                _ASKING.pop(session_id, None)

        # A NON_BLOCKING tool's response goes into the live request queue, not
        # out of `run_live`, so the page sees the call and never the answer.
        # The card is drawn from this frame instead, tagged with the
        # function-call id so it lands in the empty card the call put up rather
        # than in a new one.
        frame = {"type": "gws", "id": call_id, "result": text}
        # The events, when the question was a calendar one. Markdown is what
        # the sub-agent writes and what gets read out loud, and it is the wrong
        # thing to put on a panel read from three metres: a day comes out as a
        # bullet list of times. This is the same answer in the shape a card can
        # draw — see `_agenda.showing`. Attached whatever `kind` says below,
        # because a run that read the calendar and then ran out of clock has
        # the whole of what was asked for and only lacks the sentence about it.
        if drawn.get("agenda"):
            frame["agenda"] = drawn["agenda"]
        # FAILED and NO_ANSWER are written for the model, which is told to
        # relay them — as card text they read as instructions to nobody. Name
        # the state instead and let the card say it in the language the page is
        # in.
        kind = _NOT_AN_ANSWER.get(text)
        if kind:
            frame["error"] = kind
        else:
            # Which file and which thread the answer came out of, so the card
            # can say so — the sub-agent's prose cannot, and is told not to.
            # Only under an answer: a card that failed has no provenance to
            # report, and a source row under "the request failed" reads as a
            # claim that something was nonetheless read. Absent rather than
            # empty when the question touched nothing of the kind; the card
            # tells the three apart. See `_sources.collected`.
            sources = _sources.collected(found)
            if sources is not None:
                frame["sources"] = sources
        await screen.push(session_id, frame)
    return {"answer": text}


def build_gws_agent() -> Agent:
    """Build the Workspace sub-agent.

    A function rather than a module-level singleton for the same reason
    ``build_agent`` is one: the model name comes from .env, which main.py loads
    at startup.

    Separate from :func:`build_gws_tool` so it can be run on its own.
    `tools/gws_bench.py` drives this agent through a plain ADK ``Runner``: the
    thing worth measuring is how many commands one question costs, and the live
    agent, the audio, and the card add a minute of setup and a lot of variance
    to a number that does not depend on any of them.
    """
    return Agent(
        name="gws",
        description=DESCRIPTION,
        input_schema=GwsRequest,
        # Flash rather than Flash-Lite: Lite was sized for driving a CLI, and
        # deciding which of forty matching messages is the one worth saying is
        # a different job. Re-measured against 3.5 Flash-Lite on 2026-09-09,
        # after the instruction fixes that were meant to rescue it: Lite is
        # 2.5x faster and spends fewer calls, and still answers the mailbox
        # question with a count of matches rather than the threads, and
        # answers in Japanese to a request that asked for English.
        #
        # The cost is real and it is not a second, and it is not one number
        # either: re-measured over thirteen runs the same day, Flash's spread
        # on that question is 49.3 s to 130.5 s around a 58 s median. `TIMEOUT`
        # is sized on the tail rather than the middle — see there — which is a
        # budget to hold, not a reason to take the worse answer. "What Flash
        # bought, and what it cost" in the README has both sets of numbers.
        #
        # Pinned to the global endpoint, which is the only one serving these
        # models.
        model=Gemini(
            model=os.getenv("GWS_MODEL", "gemini-3.8-flash"),
            client_kwargs={"location": os.getenv("GWS_LOCATION", "global")},
        ),
        instruction=_instruction,
        tools=[
            # `run_skill_script` is dropped: it would need a code executor or
            # an Environment, and either one is a shell on the Pi that anyone
            # within earshot can reach. `list_skills` is dropped because the
            # instruction already carries the list.
            SkillToolset(
                skills=list(_skills()),
                tool_filter=["load_skill", "load_skill_resource"],
            ),
            FunctionTool(gws_command),
            FunctionTool(_agenda.agenda),
            FunctionTool(gws_cache.read_cache),
            FunctionTool(gws_cache.search_cache),
        ],
    )


def build_gws_tool() -> FunctionTool:
    """Wrap the Workspace sub-agent as a tool the live agent can call."""
    global _agent
    _agent = build_gws_agent()

    # Answering takes 5 s for a calendar question and half a minute for a
    # mailbox one, and a blocking tool call freezes the conversation for all of
    # it. Setting a scheduling declares the tool NON_BLOCKING, which lets the
    # Live API keep talking while the sub-agent works.
    #
    # INTERRUPT rather than browse's WHEN_IDLE, because these are different
    # waits. A train time is worth holding until there is a gap; the answer to
    # "what's on my calendar tomorrow?" is what the person is standing there
    # waiting for, and delivering it whenever they next happen to pause is how
    # you end up answering a question they gave up on.
    tool = FunctionTool(gws)
    tool.response_scheduling = types.FunctionResponseScheduling.INTERRUPT
    return tool


def launch_ephemeral_auth_browser(consent_url: str) -> tuple[subprocess.Popen, Path]:
    """Launch an ephemeral Chromium instance for OAuth sign-in on Wayland display.

    Uses an isolated /tmp profile to ensure no credentials or session cookies
    leak into the kiosk browser profile.
    """
    profile_dir = Path(f"/tmp/gws-auth-{uuid.uuid4().hex[:8]}")
    profile_dir.mkdir(parents=True, exist_ok=True)
    browser_bin = (
        os.getenv("BROWSE_CHROMIUM")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or "chromium"
    )
    env = os.environ.copy()
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    env.setdefault("DISPLAY", ":0")
    cmd = [
        browser_bin,
        f"--user-data-dir={profile_dir}",
        "--ozone-platform=wayland",
        "--password-store=basic",
        "--no-first-run",
        "--no-default-browser-check",
        "--touch-events=enabled",
        f"--app={consent_url}",
    ]
    proc = subprocess.Popen(cmd, env=env)
    return proc, profile_dir


def cleanup_ephemeral_auth(proc: subprocess.Popen | None, profile_dir: Path | str | None) -> None:
    """Terminate ephemeral browser process and wipe its temporary profile directory."""
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass
    if profile_dir is not None:
        p = Path(profile_dir)
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)

