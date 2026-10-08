"""The `gws` command the Workspace sub-agent runs.

Its own module because the name is wanted twice: this is the tool the sub-agent
calls to run a command line, and `gws.py` has the tool the live agent calls to
ask Workspace a question. ADK takes a tool's name from the function's, so the
two cannot share a namespace.
"""

import asyncio
import contextlib
import contextvars
import itertools
import json
import logging
import shlex
import subprocess

from . import _gws_cli, _progress, _sources, _timing, gws_cache

logger = logging.getLogger(__name__)

# Full API responses run to tens of kilobytes of JSON. The sub-agent only needs
# enough to answer, and the rest is latency.
MAX_OUTPUT = 8000

# How many calls one question gets, and when to start saying so. The
# instruction asks for about six and is not always listened to: "is there
# anything in my mail that needs a reply?" ran 21 commands in 90 seconds — five
# searches for the same thing in five dialects, a read that failed and was
# retried two more ways, and a chase after a reference number nobody had asked
# about. Six is still what a good answer costs; twelve is the wall, because an
# instruction the model can talk itself out of is not one.
#
# Calls and not commands: `spend` is what counts, and `_agenda.agenda` charges
# against it without starting a subprocess, on purpose. A question answered
# entirely out of the calendar spends one and runs no shell command at all.
WARN_AT = 6
STOP_AT = 12

# "Past", not "at": all twelve run, and the thirteenth is refused. On the
# twelfth this is attached to a result the tool has just handed back, and "at
# 12 the tool stops running them" reads there as a contradiction.
LEFT = (
    "{spent} of {total} gws calls used on this question. Past {total} the tool "
    "stops running them and you answer from what you have, so start writing it."
)

# The sentence every refusal has in common, and how :func:`refused` tells one
# from a warning. The sub-agent needs it said out loud anyway: with no result
# to look at, "nothing ran" is otherwise something it has to infer.
STOPPED = "Nothing was run."

OUT_OF_CALLS = (
    f"Out of gws calls for this question. {STOPPED} Write the answer now "
    "from what you have already found, and say what you did not get to."
)

# How much wall clock to hold back for the answer that `LEFT` and
# `OUT_OF_CALLS` both demand. Without it the two limits do not know about each
# other: the count's escape hatch fires, the model is told to start writing,
# and `gws.TIMEOUT` cancels the turn 2.5 s in — a question that found four
# emails and read three comes back as "there is no answer". A model turn on
# this path measured 5.9 s mean over the fifteen turns of the question that
# prompted this, so a reserve under about 15 s is the same bug with a smaller
# number: the instruction to write lands with no room to write in.
RESERVE = 20.0

OUT_OF_TIME = (
    f"Out of time for this question. {STOPPED} Write the answer now from "
    "what you have already found, and say what you did not get to."
)

# Different advice, because this is a different situation. The other two
# refusals end the question; this one ends one step of it while its siblings
# are still going, so "write the answer" would be wrong — this sub-agent is
# writing one part of an answer somebody else assembles.
OUT_OF_SHARE = (
    "This step has used its {spent} of the question's gws calls; the other "
    f"steps running alongside it have the rest. {STOPPED} Answer this step "
    "from what you already found, and say what you did not get to."
)

# How many times one question may fetch the same thing. Three and not two,
# deliberately: a narrow read followed by a wider one is how you go from "is
# this the right mail" to "what does it say", and refusing that would break an
# ordinary read. It is the third fetch of one object that stops being a read
# and starts being a search of the same JSON for something never in it — the
# question in #57 spent four of its twelve calls on one message, in four
# projections, none of which held the number it was looking for.
REPEAT_AT = 3

REPEATED = (
    "You have already run this {n} times: {target}. It will not return "
    f"anything new. {STOPPED} Use what the earlier calls gave you, or ask a "
    "different question."
)

# How much of the key to quote back. The key is the whole parameter blob now,
# so a repeated write echoes its whole body: ten rows of `values append` turned
# a 208-character refusal into a 709-character one, and a 40x12 body passes six
# thousand. Cut in the message and never in the key, so the tally stays exact
# — a truncated key would make two long bodies sharing a prefix one request.
# 200 is enough to recognise which command it was, which is the message's job.
TARGET_SHOWN = 200


def refused(note: str | None) -> bool:
    """Whether what :func:`spend` said means the caller must run nothing.

    A predicate rather than a comparison at the call site, because two of the
    four refusals are formatted with a number or a command in them and
    `note in (A, B)` would have quietly stopped covering them. Keyed on the
    sentence rather than on a prefix so that a refusal added later is covered
    by writing the truth in it, which is the only thing a new one must do.
    """
    return bool(note) and STOPPED in note


def stood(note: str) -> str:
    """A refusal, said to the one caller that ran anyway.

    Every refusal claims nothing was run, and for every caller but one that is
    true. `_agenda.agenda` is the exception: it is never refused, on the
    grounds that a question eleven calls deep is exactly the one the calendar
    answers. So it reaches a wall holding the events and a sentence denying it
    has them, and repeats that sentence on every remaining `agenda` call.

    Dropping :data:`STOPPED` leaves the half still true — which wall this is
    and what to do about it — and leaves it for whichever refusal it was. The
    bug being fixed here was a call site that named the one refusal existing
    when it was written; naming two would have been the same bug with a longer
    tuple.
    """
    return " ".join(note.replace(STOPPED, "").split())

# What to say about a command while it runs, by the service it names — which is
# the word after `gws` for everything the skills document. A command the table
# does not cover says nothing rather than something vague; the next one along
# usually names a service, and a note is only worth making when it is specific.
_DOING = {
    "calendar": ("calendar", "Reading their calendar."),
    "gmail": ("mail", "Searching their mail."),
    "drive": ("drive", "Looking through their Drive."),
    "docs": ("docs", "Reading the document."),
    "sheets": ("docs", "Reading the spreadsheet."),
    "slides": ("docs", "Reading the slides."),
    "keep": ("keep", "Reading their notes."),
    "tasks": ("tasks", "Checking their tasks."),
}

# A command that writes a file is fetching a document, whichever service it
# came out of, and that is the more useful thing to say about it.
_EXPORT = ("export", "Fetching the document.")


class _Budget:
    """What one question has spent so far, or one step of one.

    Both, and which one it is depends on :attr:`pool`. An outer entry is its own
    pool and holds the question's whole allowance; a nested entry is a *share*
    of an outer pool, and holds only what that step has spent. The pool is where
    the question's real numbers live — :attr:`spent`, :attr:`seen` and
    :attr:`live` are only meaningful there.
    """

    def __init__(self, pool: "_Budget | None" = None) -> None:
        self.spent = 0
        self.pool = pool or self
        # How many shares are open on this pool. Shares only — the pool itself
        # is not one of them, because a question with nothing nested under it
        # is not a step of anything and spends the lot. Only the pool's copy of
        # this is ever read.
        self.live = 0
        # What has already been fetched, and how often. On the budget rather
        # than a module global so it lives exactly as long as one question and
        # composes under `plan` the same way the count does. Read through
        # `pool`, so two steps fetching the same message is the repeat it looks
        # like — the second one is no less wasted for having a sibling make it.
        self.seen: dict[str, int] = {}

    @property
    def fair(self) -> int:
        """How many calls this share may spend while its siblings are running.

        A property and not a number set when the share opened, which is what
        hands the remainder back for free: a step that finishes having spent two
        of its four stops being counted in `pool.live`, and the shares still
        running see a bigger `fair` on their very next call. A reservation fixed
        at open time would strand what the early finisher did not use, and
        undoing that needs a wakeup on every branch close.

        :data:`STOP_AT` when nothing is running alongside this — one step of a
        plan, running on its own, is the question. Never zero: a step given no
        calls at all cannot say anything about what it was asked, and one call
        is enough to come back with something.
        """
        if self.pool.live <= 1:
            return STOP_AT
        return max(1, STOP_AT // self.pool.live)


_BUDGET: contextvars.ContextVar[_Budget | None] = contextvars.ContextVar(
    "ai_terminal_gws_budget", default=None
)

# When this question runs out of clock, as a `loop.time()` reading. Held apart
# from the tally because the two compose differently: the count is shared
# across a plan's `gws` steps and the clock is not. A plan running 250 s can
# hold a step that gives up at 160, and it is the step's cancellation that
# lands on the sub-agent, so the deadline the sub-agent has to write inside of
# is the nearest one in force, not the outermost. See :func:`budget`.
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "ai_terminal_gws_deadline", default=None
)


@contextlib.contextmanager
def budget(seconds: float | None = None):
    """Give everything started inside this block one shared command budget.

    A ContextVar rather than an argument because nothing between the tool and
    here can carry one: ADK calls a tool with the model's arguments and nothing
    else. `asyncio.create_task` snapshots the context, so the sub-agent's run
    keeps the budget as long as the task is created inside the block — the same
    trick, and the same reason, as :meth:`._progress.Notes.start`.

    Yields the tally, which the caller is free to ignore. `tools/gws_bench.py`
    reads it: call count is the number this budget exists to hold down, and
    reading it off the same object the enforcement uses beats counting log
    lines and hoping the two agree.

    Reentrant, and that is what makes the budget compose under `plan`. The
    planner can spend four `gws` steps on one question, and `gws` opening a
    fresh tally on each of them would be 4 x 12 = 48 CLI calls for a ceiling
    that says twelve. So **one question is twelve `gws` calls whether or not a
    planner is in the middle** — and `plan` gets the outer entry by opening the
    block one call earlier than `gws` does.

    What a nested entry yields is a *share* of the pool, not the pool. It used
    to be the pool itself, which is right for a plan running one step at a time
    and wrong the moment two run together: twelve calls handed out
    first-come-first-served means the branch that starts first takes them all.
    Measured before this changed — three branches wanting six each got 6/3/3,
    and one greedy branch wanting fourteen against two wanting three got
    12/0/0, which is a fast half-answer where the serial version was a slow
    whole one. Each share may spend :attr:`_Budget.fair` of the pool, and the
    pool's own ceiling still ends the question. See :func:`spend`.

    The tally a caller reads out of this block is therefore its own share, and
    `share.pool` is the question. `plan` and `gws_bench.py` both take the outer
    entry, where the two are the same object.

    `seconds` is how long the caller is going to allow the whole question. The
    deadline does **not** follow the tally, and that is the one place the two
    part company. Nearest wins: every entry, nested or not, tightens
    :data:`_DEADLINE` to `min(what is already in force, now + seconds)` and
    undoes that on the way out. A nested entry can only bring the deadline in,
    never push it out — a step cannot award itself more clock than the plan
    holding it has, and one that tried would be cancelled by the plan's own
    `asyncio.timeout` at exactly the moment its own bookkeeping said there was
    room. So `min`, and never `max`.

    Tightening is what the count deliberately does not do, because the two
    limits are enforced by different things. Nothing cancels a question for
    spending a thirteenth call — `spend` refuses it and the sub-agent carries
    on — so one shared count across a plan's `gws` steps is a real ceiling.
    The clock is not like that: `gws._ask` wraps each step in its own
    `asyncio.timeout(gws.TIMEOUT)` from *step* start, and that is what actually
    kills it. A deadline further out than that cancellation is a deadline the
    reserve cannot protect.

    `seconds` left out means this entry has no clock of its own to contribute
    and whatever is already in force stays; with nothing in force either,
    `spend` counts exactly as it did.

    `loop.time()` rather than `time.monotonic()`, because that is the clock
    `asyncio.timeout` measures its own deadline against and the two must not
    drift.
    """
    until = (None if seconds is None
             else asyncio.get_running_loop().time() + seconds)
    running = _DEADLINE.get()
    if until is None:
        until = running
    elif running is not None:
        until = min(running, until)
    clock = _DEADLINE.set(until)

    running = _BUDGET.get()
    tally = _Budget() if running is None else _Budget(pool=running.pool)
    if tally is not tally.pool:
        tally.pool.live += 1
    token = _BUDGET.set(tally)
    try:
        yield tally
    finally:
        if tally is not tally.pool:
            tally.pool.live -= 1
        _BUDGET.reset(token)
        _DEADLINE.reset(clock)


def hold(share: "_Budget", work: asyncio.Task) -> None:
    """Keep `share` counted in `pool.live` until `work` finishes.

    :func:`budget` is entered and left around ``asyncio.create_task``, and that
    block is a *context snapshot*, not the step's lifetime — there is no await
    inside it, so `live` would be back to zero before the step ran its first
    call and :attr:`_Budget.fair` would hand every step the whole pool.

    The deadline does not need this because it is a value on the snapshot the
    task took. `live` is a mutable counter read at spend time, on an object the
    branches share, so it has to be held open by something that outlives the
    block. There is no await between the block's `finally` and this call, so
    the moment `live` spends back at zero is never observed.
    """
    if share is share.pool or work.done():
        return
    pool = share.pool
    pool.live += 1

    def _drop(_task: asyncio.Task) -> None:
        pool.live -= 1

    work.add_done_callback(_drop)


def spend(target: str = "") -> str | None:
    """Charge one call to this question's budget and say how it stands.

    Returns one of the four refusals — :data:`OUT_OF_CALLS` when the question
    has spent them all, :data:`OUT_OF_SHARE` when this step has spent its share
    of them and its siblings hold the rest, :data:`OUT_OF_TIME` when there is no
    longer enough clock left to write the answer in, :data:`REPEATED` when
    `target` has already been fetched :data:`REPEAT_AT` times — and a caller
    that can refuse should return it and run nothing. Otherwise a warning once
    the count is high enough to be worth saying, or None while there is room.
    Anything running outside :func:`budget` has nothing to charge and gets None,
    which is what a unit test wants. Use :func:`refused` to tell the two kinds
    of string apart.

    The two counts are checked **before** charging, where everything else here
    is charged first and checked after. That was harmless with one pot: a
    refused thirteenth call left the tally reading 13 and nothing read it again.
    With several shares it is wrong — a branch already over its share would go
    on eating the calls the other branches were being held back for, one per
    refusal. Twelve still run and the thirteenth is still refused; the tally
    afterwards now says 12.

    `target` is what the call is going to fetch — see :func:`_target`. Left
    out, nothing is tallied against it and the repeat check cannot fire, which
    is what `_agenda.agenda` wants: it charges the count without being a `gws`
    command at all, so it has no request to be a repeat of.

    The clock is checked after the count so that a question that has spent both
    is told about the count, which is the more actionable of the two: `RESERVE`
    is the tool's own bookkeeping, and "out of calls" is a thing the sub-agent
    has been warned about since the sixth one. The repeat comes last of the
    three because it is the only one the sub-agent can do something about: the
    other two end the question, and this one says to ask a different one.

    A refused repeat is still charged against the count. Twelve refusals have
    to end a question — a refusal the model can retry for free is a loop — and
    the tally of repeats is not bumped by one, so the number in the message
    stays the number of times the command actually ran.
    """
    share = _BUDGET.get()
    if share is None:
        return None
    pool = share.pool
    if pool.spent >= STOP_AT:
        return OUT_OF_CALLS
    if share is not pool and share.spent >= share.fair:
        return OUT_OF_SHARE.format(spent=share.spent)
    share.spent += 1
    if share is not pool:
        pool.spent += 1
    deadline = _DEADLINE.get()
    if (deadline is not None
            and deadline - asyncio.get_running_loop().time() < RESERVE):
        return OUT_OF_TIME
    if target:
        ran = pool.seen.get(target, 0)
        if ran >= REPEAT_AT - 1:
            shown = (target if len(target) <= TARGET_SHOWN
                     else target[:TARGET_SHOWN] + "…")
            return REPEATED.format(n=ran, target=shown)
        pool.seen[target] = ran + 1
    if pool.spent >= WARN_AT:
        return LEFT.format(spent=pool.spent, total=STOP_AT)
    return None


def _parse(command: str) -> tuple[list[str], dict | None]:
    """The arguments after `gws`, or the reason there is nothing to run.

    One reading, used by both the announcement and the run, because the thing
    that decides whether a command happens has to be the thing that decides
    whether to say it is happening. Two readings is how "checking your
    calendar" came to be said out loud about a command that was then refused
    for not starting with `gws`.

    shlex, not a shell: `|`, `>` and `&&` end up as literal argv tokens instead
    of running anything, but the quoting the skills use for `--json '{...}'`
    still parses the way the docs say it does. A `--json` body with an
    apostrophe in it is the one thing that trips it, and an unbalanced quote is
    worth one corrected retry, not a stack trace.
    """
    try:
        argv = shlex.split(command)
    except ValueError as err:
        return [], {"command": command,
                    "error": f"Could not parse the command: {err}"}
    if not argv:
        return [], {"command": command, "error": "Empty command."}
    if argv[0] != "gws":
        return [], {
            "command": command,
            "error": "Only `gws` commands can be run. Start the command with `gws`.",
        }
    return argv[1:], None


def _announce(argv: list[str]) -> None:
    """Say what this command is about to go and do, if it is clear enough."""
    # Prefix, not equality: `--output=notes.md` is a single token and is as
    # much an export as `-o notes.md` is.
    if any(arg == "-o" or arg.startswith("--output") for arg in argv):
        return _progress.note(*_EXPORT)
    doing = _DOING.get(argv[0] if argv else "")
    if doing:
        _progress.note(*doing)


def cli_label(argv: list[str]) -> str:
    """What to call this command in its `cli` step, for someone reading the log.

    The leading subcommand words, plus `--help` when it is there. Those are the
    three waits worth telling apart: `drive files export --help` is the model
    asking how to spell a command, `schema drive.files.export` is it asking
    again, and `drive files export` is the fetch. They are seconds each and
    they are not the same seconds.

    A label, not a key — `latency_report.py` totals the `cli` row by step name
    and never reads this. `--help` is the one flag in it because it is the one
    flag that changes what the command *is* rather than what it returns; the
    rest are dropped so a `--params` blob does not arrive in the log twice.
    """
    named = list(itertools.takewhile(lambda a: not a.startswith("-"), argv))
    if "--help" in argv:
        named.append("--help")
    return " ".join(named)


# What a gws command calls the thing it is fetching. Two spellings because the
# CLI has two: the `+read` shortcuts take `--id` on the command line, the
# generic subcommands take it inside `--params`, and a command written either
# way is the same request. Order only breaks ties — a command names one of
# these, not four.
_NAMES = ("id", "message-id", "message", "document", "spreadsheet",
          "presentation", "q", "query", *gws_cache.ID_KEYS)

# What narrows a request that already names its object. The same `q` over three
# months is three questions, not one asked three times, and `gws.py:341-343`
# tells the sub-agent to hunt an event by name in exactly that shape. Without
# these, the second and third windows key alike and the third is refused for a
# range nobody has read yet.
_NARROWERS = ("timeMin", "timeMax", "startTime", "endTime", "range")

# Parameters that change how the answer is printed rather than which answer it
# is. Two commands differing only by one of these are one API request made
# twice — one message asked for with three `fields` projections is exactly the
# pathology #57 counts — so they are dropped before the key is built.
#
# A closed set, and that is the point: it is Google's standard query parameters
# plus the two projections every service spells for itself. Methods arrive with
# every CLI release and this list does not grow with them, which is why the key
# is built by dropping these rather than by naming the parameters that matter.
# `alt` is deliberately absent: `alt=media` is the difference between a file's
# metadata and its bytes, which is two requests, not one printed two ways.
#
# `pageSize` and `maxResults` are the one deliberate stretch. They change how
# much answer you get, not only how it is printed, so three `files list`
# differing only by `pageSize` are one key and the third bump is refused —
# with :data:`REPEATED`'s "It will not return anything new", which for 5 -> 100
# is false. Kept in because a page-size bump is not a new search: the real
# pagination mechanism is `pageToken`, which is correctly absent here, and
# taking these two out would let one query spend three calls on itself. The
# refusal's sentence is an approximation either way — it is equally loose for a
# `fields` projection asking for a field the earlier call did not return.
_OUTPUT = frozenset((
    "fields", "format", "pageSize", "maxResults", "prettyPrint", "quotaUser",
    "key", "access_token", "oauth_token", "userIp", "callback", "$.xgafv",
))

# The verbs that change the account rather than reading it. A write is never
# the same fetch twice: three `+insert` in one question are three events, and
# refusing the third with "you have already run this 2 times" is both false and
# one event short. Writing is an intended path here — `gws.py:98`, `gws.py:319`
# — and none of `summary`, `start` or `end` is in `_NAMES`, so all three of
# those commands key on `cli_label` alone.
#
# Read off the leading subcommand words, the way :func:`cli_label` reads them,
# rather than off the whole argv: a search for the word "send" is not a send.
_WRITES = frozenset((
    "+send", "+reply", "+reply-all", "+forward", "+insert", "+upload",
    "+write", "create", "insert", "update", "patch", "delete", "send",
    "batchUpdate", "batchDelete", "trash", "untrash", "copy", "modify",
))


def _writes(argv: list[str]) -> bool:
    """Whether this command changes the account rather than reading it."""
    return any(word in _WRITES for word in
               itertools.takewhile(lambda a: not a.startswith("-"), argv))


def _flag(argv: list[str], name: str) -> str:
    """The value of `--name`, written either way, or "" when it is absent."""
    for i, token in enumerate(argv):
        if token == f"--{name}" and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(f"--{name}="):
            return token.split("=", 1)[1]
    return ""


def _target(argv: list[str]) -> str:
    """What this command fetches, ignoring how it is going to be printed.

    `--format json` is an output flag: two commands differing only by it are
    one API request made twice, so keying on the argv would count them as two
    different calls. The identifying argument is sharper still — one message
    asked for with three `fields` projections is the pathology :func:`spend`
    counts, and all three name the same id while no two of them share an argv.

    Naming the object is not always enough to tell two calls apart, and naming
    *one* object is not either: `drive comments get` names a file and a
    comment, `accessproposals resolve` names a file and a proposal, and keying
    on the first id found made every comment on one file the same read and
    every proposal on one file the same write. So the whole parameter blob goes
    in, minus :data:`_OUTPUT` — the parameters *are* the question, and dropping
    only the ones that change the printing is what keeps three projections of
    one message a single key while leaving three comments on one file three.

    `--json` is in that blob too, via :func:`gws_cache.params`, which is what
    makes a write with a body tell itself apart without :data:`_WRITES` having
    to name its verb. `sort_keys` so two spellings of one body are one key.

    `ensure_ascii=False` because this string is not only a key:
    :data:`REPEATED` hands it back to the sub-agent as the tool result, to say
    *which* request was already made. On this terminal that request is usually
    in Japanese, and `\\u30ab\\u30fc\\u30c9` is not a query anyone can
    recognise. The rest of the tree already writes reader-facing JSON this way
    — `gws.py:819`, `_timing.py:112`, `browser.py:1404`.

    :data:`_NAMES` and :data:`_NARROWERS` are read off the command line only.
    Written inside `--params` they are already in the blob; written as flags —
    `+read --id`, `events list --timeMin` — they are the whole question and
    nothing else would carry them.
    """
    fields = {name: value for name, value in gws_cache.params(argv).items()
              if name not in _OUTPUT}
    named = next((value for name in _NAMES if (value := _flag(argv, name))), "")
    span = " ".join(f"{name}={value}" for name in _NARROWERS
                    if (value := _flag(argv, name)))
    rest = (json.dumps(fields, sort_keys=True, ensure_ascii=False)
            if fields else "")
    return " ".join(part for part in (cli_label(argv), named, span, rest)
                    if part)


def _run_sync(argv: list[str], command: str) -> tuple[dict, list[tuple]]:
    # The result, and the summaries `record` deferred. The second is a return
    # value and not a key of the first because the first is the tool's output:
    # every key of it is read by the sub-agent, and a list of file paths and
    # document bodies is neither something to show it nor something it could do
    # anything with. See `gws_cache.record`.
    #
    # Anything the command writes belongs in the cache, so it runs from there
    # and its `-o` path is flattened into it.
    argv = gws_cache.rewrite_output(argv)

    # Snapshot, run and diff are one operation, and `gws_cache.writing` is what
    # keeps a second command from writing inside the diff and having its file
    # attributed to this one. Only those three: putting the headers on is two
    # network calls a file and runs after the lock is released. See there.
    run = f"gws {shlex.join(argv)}"

    # Nothing returns from inside this block, because a command that failed can
    # still have written a file — a timeout kills gws mid-export and leaves
    # whatever it had got. Returning early skipped `record`, and a cached file
    # with no header is worse than a partial one with a header saying which
    # command left it there: it is invisible to the index, the listing and the
    # search, so nobody finds it and nothing clears it.
    with gws_cache.writing():
        before = gws_cache.snapshot()

        try:
            # The subprocess on its own: process start, whatever auth the CLI
            # does and the API call, with nothing of this repo's inside it.
            with _timing.step("cli", cmd=cli_label(argv)):
                proc = subprocess.run(
                    [_gws_cli.binary(), *argv], cwd=gws_cache.cache_dir(),
                    capture_output=True, text=True, timeout=_gws_cli.TIMEOUT,
                    check=False,
                )
        except FileNotFoundError:
            result = {"command": command,
                      "error": "The gws CLI is not installed."}
        except subprocess.TimeoutExpired:
            result = {"command": command,
                      "error": f"Timed out after {_gws_cli.TIMEOUT}s."}
        else:
            # The label and not `run`: the command line carries the question
            # itself — a `--query`, a `--params` blob — and this line is INFO
            # on the success branch, so it reaches a journal kept for a month
            # that the *Forget* button cannot clear. What the line is read for
            # is which subcommand ran and how it came back, and `cli_label`
            # keeps both. `run` still names the command in the cache header.
            logger.info("gws %s -> exit %s", cli_label(argv), proc.returncode)
            # A Gmail attachment arrives as base64 on stdout instead of as the
            # file `-o` asked for. Writing it out has to happen inside the
            # diff, and showing the base64 instead would spend the sub-agent's
            # whole context on a document it now has a filename for.
            unpacked = gws_cache.unpack(argv, proc.stdout)
            # Off the whole of stdout and not off `result`, which is capped at
            # `MAX_OUTPUT` — a listing long enough to be cut is a listing whose
            # JSON no longer parses, and the file the answer came out of is as
            # likely to be past the cut as before it.
            #
            # A command that failed is still handed over, with nothing to read.
            # What it printed is a stack trace or an API error and names no
            # file, but it looked, and the question whose only Drive call
            # failed and got answered anyway is precisely the card that has to
            # say it cannot name a source rather than draw no row and read as
            # though it had none to draw. `ok` is the other half of that: it
            # looked, so it counts as having looked, and the id it asked for
            # is not a source because nothing came back under it.
            _sources.record(argv, proc.stdout if not proc.returncode else "",
                            ok=not proc.returncode)
            result = {
                "command": command,
                "exit_code": proc.returncode,
                "stdout": (gws_cache.UNPACKED.format(name=unpacked) if unpacked
                           else proc.stdout[:MAX_OUTPUT]),
                "stderr": proc.stderr[:MAX_OUTPUT],
            }
        written = gws_cache.written_by(before)

    if "proc" in locals() and _gws_cli.is_auth_expired(proc.stdout, proc.stderr):
        msg = proc.stderr.strip()[:400] or proc.stdout.strip()[:400] or "auth expired"
        raise _gws_cli.GwsAuthExpiredError(msg)

    # What `record` kept, not what the diff saw. A file another command
    # replaced while this one was being indexed belongs to that command, and
    # `record` leaves its header alone — so naming it here would hand the
    # sub-agent a document it does not have.
    cached, later = gws_cache.record(written, run)
    if cached:
        result["cached"] = cached
    return result, later


async def gws(command: str) -> dict:
    """Run one `gws` command and return what it printed.

    Args:
        command: The whole command line as you would type it, starting with
            `gws` — for example
            `gws calendar events list --params '{"maxResults": 5}'`.
            It is not run through a shell, so pipes and redirection do
            nothing, but quoting works as the skill documents show.

    Returns:
        A dict with the exit code, stdout and stderr, or an error message when
        the command could not be run at all.
    """
    # Before the budget: a command that cannot be run has not been run, and
    # spending one of twelve on a missing `gws` prefix shortens the question
    # for nothing.
    argv, bad = _parse(command)
    if bad is not None:
        return bad

    # A write gets no target, which is the same path `_agenda.agenda` takes:
    # charged against the count and the clock, never tallied as a fetch.
    budget = spend("" if _writes(argv) else _target(argv))
    if refused(budget):
        why = ("count" if budget == OUT_OF_CALLS else
               "clock" if budget == OUT_OF_TIME else "repeat")
        # `cli_label(argv)` rather than `command`, for the reason the success
        # branch takes it: a budget refusal is ordinary rate-limiting, not a
        # failure, so this fires on a normal day and the raw command line would
        # put the question in the journal. `why` is the whole diagnosis anyway.
        logger.warning("gws refusing (%s): %s", why, cli_label(argv))
        return {"command": command, "error": budget}

    # Before the work rather than after it: the point of a note is to fill the
    # silence the command is about to cause.
    _announce(argv)
    result, later = await asyncio.to_thread(_run_sync, argv, command)
    # Started here and not inside `_run_sync`, which runs on a worker thread
    # with no loop of its own to create a task on. Before the return rather
    # than after it, obviously, but it costs nothing: `create_task` schedules
    # and does not run, so the summaries start when this coroutine next yields,
    # which is after the sub-agent has this result in hand.
    gws_cache.defer(later)
    if budget:
        result["budget"] = budget
    return result
