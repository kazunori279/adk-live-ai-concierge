"""Where documents pulled out of Google Workspace are kept.

`gws` writes a file whenever it exports or downloads something, and the path is
whatever the sub-agent typed after ``-o``. Left alone that scatters
`content.txt`, `doc_content.txt` and `download.txt` through whatever directory
the server happened to start in, each one an anonymous copy of a document
nobody can identify a day later.

So every gws command runs with ``gws_cache/`` as its working directory, output
paths are flattened into it, and each text file gets a header: the *shape* of
the command that produced it with every value elided out of it — see
:func:`redacted`, and #233 for why the values cannot stay — the Drive id and
title behind it, and two or three sentences of
what is in it, written once by the small model just after the fetch — after the
answer that fetched it has gone out, because only a later question ever reads a
summary and there is no reason to make this one wait. That header is
the index. ``index.md`` is rebuilt from it for anyone reading the directory,
``listing()`` puts the recent part of it in the sub-agent's prompt, and
``search_cache`` reaches the rest.

Nothing here expires on a clock, because these are copies of documents that
still live in Drive and the mtime of the copy says nothing about the original.
``read_cache`` asks Drive for the document's ``modifiedTime`` instead and
refuses to serve a copy that lost the race — one cheap call in place of the
export it is standing in for. What does go is the oldest, and only once the
directory runs past :data:`CACHE_MAX_FILES` or :data:`CACHE_MAX_BYTES`: every
read here is linear in the number of cached files, so an uncapped cache is a
turn that gets slower every week. See :func:`_evict`.
"""

import asyncio
import base64
import binascii
import contextlib
import contextvars
import fcntl
import functools
import json
import logging
import os
import re
import shlex
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import pypdf
from google.genai.types import GenerateContentConfig

from .._log import _sized
from . import _gws_cli, _match, _progress, _timing
from ._client import grounding_client, grounding_model

logger = logging.getLogger(__name__)


@functools.cache
def _sources():
    """The source ledger, imported late because it imports this module.

    `_sources` reads :data:`ID_KEYS` at import to build its own, so it cannot
    be imported from the top of this file: whichever of the two loaded first
    would hand the other a half-built module. It is a leaf otherwise, so the
    cycle is this one line and the cache makes it a dict lookup after the first
    call.
    """
    from . import _sources as module  # pylint: disable=import-outside-toplevel
    return module


INDEX = "index.md"

# What gws exports as text, whatever extension the sub-agent asked for. A doc
# is a document; calling the file .md is what makes the cache readable.
TEXT_SUFFIXES = {"", ".txt", ".text"}

# One cached file, read back into a prompt. Long enough for a meeting note or
# an itinerary, short enough that a 300-page export cannot blow the turn.
MAX_READ = 20000

# How much of a document the summariser is shown. The opening of a document is
# what says what it is; the tail of a long one rarely changes the answer.
SUMMARY_INPUT = 12000

# How a PDF is recognised. The first five bytes of the file, not the extension:
# the extension is one the model typed, and `-o invoice` is an invoice.
PDF_MAGIC = b"%PDF-"

# What a PDF turned into text is called: the whole filename plus `.md`, not the
# stem. An `invoice.pdf` and an `invoice.md` exported from a Doc are two
# different documents and the stem would collide them, silently and in the
# direction that loses the older one.
PDF_TEXT = "{name}.md"

# How many pages of a PDF are worth reading. Two reasons and both hold. Sixty
# dense pages already extract to 110 000 characters where `MAX_READ` hands back
# 20 000, so pages past this one cost a write nobody reads back — and it is
# also the time. MEASURED on this Mac, 400 dense pages: 110 ms capped against
# 674 ms uncapped, and pypdf runs three to five times slower on a Pi 5, which
# makes the uncapped parse two or three seconds of somebody standing there.
# `record` runs this inside the question. Do not widen it on the belief that
# extraction is free; the earlier note here said so and had measured the capped
# path both times.
PDF_PAGES = 60

# What the sub-agent is shown in place of an attachment's base64. The bytes
# are in the cache under a name it can read back; the base64 is 170 KB of
# nothing it can do anything with.
UNPACKED = "Saved the attachment as {name}. `cached` names what to read."

# What a scanned PDF gets instead of text. OCR is a different dependency and a
# much worse latency story on a Pi, so the useful thing to do with a page of
# pixels is say it is one and stop — the sub-agent can then ask for the number
# rather than reporting that it looked and the document was empty.
NO_TEXT = (
    "This PDF has no text layer: the pages are images, so nothing here can "
    "read what is printed on them. Tell them the file is a scan and ask for "
    "the figure itself, or for a copy with selectable text."
)

# Cached documents shown in the sub-agent's prompt, newest first. A row of
# filename, title and summary measures about 400 characters, so eight of them
# is roughly 3 KB of instruction — and the instruction is rebuilt on every one
# of the several turns a request takes. Twenty would be a third of what the
# whole prompt costs. Everything older is a `search_cache` away.
LISTING_LIMIT = 8

# The line `listing()` ends on when the cache runs past that limit, named so
# the instruction can ask the listing whether it left anything out instead of
# counting the cache a second time — and so the two cannot drift apart.
TAIL = "`search_cache` finds them."

# The other thing `listing()` can return on its own, named for the same reason:
# an empty cache and a cache the listing shows all of are different states, and
# the instruction says something different in each.
EMPTY = "The cache is empty."

# The parameter a gws command names a document with. Drive, Docs, Sheets and
# Slides each call it something different, and any of them identifies the same
# thing: this exact document, whatever the local copy ended up being called.
ID_KEYS = ("fileId", "documentId", "spreadsheetId", "presentationId")

_HEADER = "---\n"

# How much of a file :func:`_read_header` will look at before deciding it has
# no header. Ours is at most eight short fields, and one of them — the summary
# — is two or three sentences on one line; a document's own front matter can be
# any length at all, and the whole point of the bound is not to hold it. Lines
# rather than a byte total because `readline` takes the byte cap per call, so
# an enormous single line arrives in `_HEADER_BYTES` pieces and runs the line
# count out instead of the memory.
_HEADER_LINES = 32
_HEADER_BYTES = 8192

# The cache's own bookkeeping, kept in the directory it describes and hidden so
# that one rule keeps both out of every listing of it.
_LOCK = ".lock"
_TMP = ".tmp-"

# How long to wait for the file lock, which is always another process — the
# bench, a shell left open. Nothing here bounds how long that holds it, so
# waiting past the sub-agent's own deadline only turns a wrong header into a
# question that went unanswered. What is lost by carrying on is attribution;
# what is lost by waiting is the answer.
LOCK_TIMEOUT = 45.0

# How long to wait for the thread lock before going ahead without it. It has to
# clear the longest hold this server takes itself, or the degrade fires against
# its own work.
#
# That hold is all three of these and not one. `_gws_run` takes the lock across
# a whole gws subprocess, which `_gws_cli.TIMEOUT` bounds, plus a directory scan
# either side — and `writing` takes `_local` *before* it starts polling for the
# `flock`, and goes on holding it for the whole of that wait and through the
# unlocked yield afterwards. So the longest hold is `TIMEOUT + LOCK_TIMEOUT`,
# and every version of this number that counted only `TIMEOUT` left a thread
# able to time out on `_local` and walk in beside a thread already inside the
# block. That is the one failure `written_by`'s mtimes cannot survive: two
# overlapping diff windows, and the later command attributes the earlier one's
# file with an mtime that `_annotate` then reads back as unchanged.
#
# One hold, and it has to stay one hold: this is the *holder's* deadline, not
# the waiter's. Timed from a waiter's arrival it would be a number about the
# queue instead, because `_local` is a plain lock and the k-th waiter waits
# (k−1) holds in front of it. Three ordinary ones at `TIMEOUT` already clear
# 105 s, so the fourth command in flight would give up on three threads that
# were each making progress and walk in beside the third — the same failure by
# a longer route. :func:`_acquire_local` restarts the clock on every handover,
# so a queue does not add up and this stays the one number the paragraph above
# argues for.
LOCAL_TIMEOUT = _gws_cli.TIMEOUT + LOCK_TIMEOUT + 15.0

# How often a waiter wakes to ask whether the holder has been in too long, and
# how often :func:`_flock` retries. One interval for both: it is the resolution
# of a deadline measured in tens of seconds, and neither wait is the sort where
# 50 ms matters.
_POLL = 0.05

# One acquisition is up to the sum of the two, because `writing` waits for
# `_local` and then for the `flock`, serially: 150 s behind one wedged holder.
# `record` takes the lock once per file and `write_index` once more, and
# `_gws_run` has already taken it once for the command itself, so a
# two-document export is four acquisitions and 600 s worst case — on an
# `asyncio.to_thread` worker that the sub-agent's 160 s deadline does not
# reclaim. That is the real number and it is not comfortable. Reaching it needs
# a foreign process sitting on the directory for the whole of it, which is the
# part that has never happened.
#
# Behind a queue of *healthy* holders the wait is not bounded by this number at
# all; it is bounded by the queue, and the queue by the thread pool rather than
# by anything here. All three call sites run inside an `asyncio.to_thread`
# worker, so at most `min(32, cpu_count + 4)` of them — eight on the Pi — can
# be at this lock at once. Eight holds at their ceiling is 720 s, and it is
# 720 s of work that is actually happening. Giving up on that is the trade this
# module refuses: what is lost by waiting is one worker, what is lost by
# carrying on is the attribution of every file written in the window.

# How old a `.tmp-` file has to be before :func:`_sweep_temporaries` takes it
# for abandoned. Long enough that a live `_replace` mid-write is never in
# range, short enough that a crashed one goes on the next command.
_TMP_TTL = 300.0

# How much the cache is allowed to hold before :func:`_evict` starts dropping
# the oldest documents. Having no cap was the same decision as "somebody will
# delete files", and on a terminal with no keyboard nobody will — the README
# has said `Delete a file to drop it from the cache` since the first version of
# this module and the household has never read it.
#
# Two caps because the two costs are different. `_records` stats every file and
# `_scored` opens every file, both on every turn, so the read cost is a
# *count*: five thousand tiny files make every turn slow at no size at all.
# The SD card wears by *bytes*, so a hundred 60-page exports do the damage a
# count cap never sees.
#
# Two hundred files holds `listing()` to about 6 ms and `index.md` to about
# 36 KB, both read off the table in #72 — a judgement about where a per-turn
# cost stops being free, not a measurement. Oldest first rather than a TTL: a
# document fetched a year ago and asked about weekly is exactly the one to
# keep, and an eviction that turns out wrong costs one gws call to undo.
#
# Neither may be zero or less, and the refusal is at import for the same
# reason `SESSION_RETAIN_S`'s is: this is the #99 shape again, a cap where
# nothing has ever said that zero is not "no cap". It is not. `_evict` walks
# the records newest first and spares only `index` zero, so either cap at
# zero or below breaks at `index == 1` and the directory is left holding
# exactly the one document that was just fetched. Measured on a scratch
# directory: five cached files go to one, on both caps and at both 0 and -1.
#
# That is a deletion of the household's cached Workspace documents, and the
# next read of any of them is a `gws` subprocess and a fresh summary call —
# 1.9 s of a cold question, per document, forever. `int()` has already
# refused `nan` and `inf` by the time either name is bound, so there is
# nothing to check for finiteness here.
CACHE_MAX_FILES = int(os.getenv("GWS_CACHE_MAX_FILES", "200"))
if CACHE_MAX_FILES <= 0:
    raise ValueError(
        "GWS_CACHE_MAX_FILES is how many documents the cache may hold "
        "before the oldest are dropped: it wants a whole number above zero. "
        "0 is not 'no cap' — the next fetch would evict every other "
        "document in the cache and leave the one file it just wrote. Set a "
        "count, or leave it unset for 200.")
CACHE_MAX_BYTES = int(os.getenv("GWS_CACHE_MAX_BYTES", str(64 << 20)))
if CACHE_MAX_BYTES <= 0:
    raise ValueError(
        "GWS_CACHE_MAX_BYTES is how much the cache may hold on the card "
        "before the oldest documents are dropped, in bytes: it wants a "
        "whole number above zero. 0 is not 'no cap' — the next fetch would "
        "evict everything but the file it just wrote. Set a size, or leave "
        "it unset for 64 MB.")

_SUMMARY_PROMPT = """\
You are indexing a document so that it can be found again later, by an
assistant matching a spoken question against a list of short descriptions.

Write two or three sentences about the document below: what kind of document it
is, what it is about, and the names, dates, places and numbers someone would
say when asking for it. Dense and concrete, the register of a catalogue entry.

Every detail must come from the text in front of you. Take no hint from the
wording of these instructions, and write nothing you cannot point at. If the
text is markup, boilerplate or all but empty, say that instead.

Describe the document; do not address anyone or explain how to search for it.
Write in English, whatever language the document is in, but keep names, titles
and places spelled exactly as it spells them — those are what a question about
this document will have in common with it. No preamble, no Markdown.
"""


@functools.cache
def cache_dir() -> Path:
    """The cache directory, created if it is not there yet.

    Relative to the process's working directory, which is the repo root under
    `uv run live-server` and the systemd unit's ``WorkingDirectory`` on the Pi.
    Cached because .env is loaded before the first call and the path cannot
    change after that.
    """
    path = Path(os.getenv("GWS_CACHE_DIR", "gws_cache")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_name(value: str) -> str:
    """Flatten an output path to a filename inside the cache.

    Only the basename survives, so neither an absolute path nor a `..` can put
    a document somewhere nobody thinks to look for it. Leading dots go too: a
    hidden name is how this directory marks its own bookkeeping, so `-o
    .notes.txt` would land a real document that `_cached` then hides from the
    index, the listing and the diff alike — fetched, and gone. The model picks
    these names, so it is a name that can be asked for.
    """
    name = Path(value).name.lstrip(".")
    stem, suffix = os.path.splitext(name)
    if suffix.lower() in TEXT_SUFFIXES:
        name = f"{stem or 'content'}.md"
    # The index is written from the directory listing, so a document saved
    # over it would be lost on the next write anyway.
    return f"cached-{name}" if name == INDEX else name


def rewrite_output(argv: list[str]) -> list[str]:
    """Point any `-o` / `--output` in a gws command at the cache."""
    out = list(argv)
    for i, token in enumerate(out):
        if token in ("-o", "--output") and i + 1 < len(out):
            out[i + 1] = _cache_name(out[i + 1])
        elif token.startswith("--output="):
            out[i] = "--output=" + _cache_name(token.split("=", 1)[1])
    return out


# The one gws command that hands back a document instead of writing it out.
_ATTACHMENT = ("gmail", "users", "messages", "attachments", "get")

# gws flags that take a value as the next word: the CLI's documented globals,
# plus the `+shortcut` flags whose value is a sentence somebody said.
#
# Adding a flag that takes nothing — `--dry-run`, `--page-all` — reads the
# following word as a value and eats the first word of the command, so this
# stays an allowlist. But it is *not* the full set of flags that take one:
# every `+shortcut` has its own — `--to`, `--body`, `--summary`, `--days` —
# and only the three below are named, because only they change an answer.
# `--query` and `--q` are what :func:`redacted` has to see (a Gmail query is
# written unquoted, `--query is:unread from:boss`, so its value is several
# tokens and #233 leaked all but the first), and `--max` is what makes the
# `--max` in :data:`_SHOWN_FLAGS` reachable at all for the spaced spelling.
# The rest are unnamed on purpose: :func:`_word_indices` fails closed, so an
# unnamed flag's value is elided rather than kept, and `_cli_words` only ever
# compares against :data:`_ATTACHMENT`, which none of them appears on.
VALUED = {"--format", "--params", "--json", "-o", "--output", "--upload",
          "--page-limit", "--page-delay", "--sanitize",
          "--query", "--q", "--max"}


def _cli_words(argv: list[str]) -> tuple[str, ...]:
    """The subcommand words, wherever in the line they start.

    Not `takewhile` from the front: a global flag written first — `--format
    json gmail users messages attachments get` is a line the CLI accepts —
    emptied the tuple, and :func:`unpack` compares it for equality, so the
    attachment went unrecognised and the base64 stayed on stdout.

    A flag's value has to be skipped with it, or `--format json` contributes
    `json` and the equality fails the other way — and which flags take one
    cannot be guessed from the line: `--format json drive` and `--page-all
    drive` look identical from here. Hence :data:`VALUED`. The longest run of
    the words that are left is the command, because a value is one token after
    `shlex` and `gws <service> <resource> [sub] <method>` is at least two.
    """
    runs: list[list[str]] = [[]]
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            skip = token in VALUED
            runs.append([])
        else:
            runs[-1].append(token)
    return tuple(max(runs, key=len))


def _output_name(argv: list[str]) -> str:
    """The filename an already-rewritten `-o` names, or "" for none."""
    for i, token in enumerate(argv):
        if token in ("-o", "--output") and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith("--output="):
            return token.split("=", 1)[1]
    return ""


def unpack(argv: list[str], stdout: str) -> str:
    """Write out a Gmail attachment the CLI answered with instead of saving.

    Every other download lands as a file: `-o` names it, the bytes go there,
    and the diff around the command picks it up. `gmail users messages
    attachments get` is the exception. The API answers with base64url in a JSON
    body rather than with the bytes, the CLI has no case for that and ignores
    `-o`, and 170 KB of base64 arrives on stdout with nothing on disk —
    measured against this account on 2026-09-09, on a 126 KB PDF.

    So the one document most likely to hold the number being asked for, the
    invoice attached to the email, was the one the cache could never see.
    Honouring the `-o` the CLI dropped puts it back on the ordinary path:
    :func:`record` headers it and :func:`_sidecar` reads the text out of it.

    Returns the name written, or "" for every command this is not — which is
    almost all of them, and is why it can be called on all of them. No `-o`,
    no `data` in the answer, or an answer that is not JSON, and nothing
    happens. Call it inside :func:`writing`, before :func:`written_by`: the
    file has to land inside the diff to be attributed to the command.
    """
    if _cli_words(argv) != _ATTACHMENT:
        return ""
    name = _output_name(argv)
    if not name:
        return ""
    # From the first brace and not from the first byte, for the reason
    # `_gws_cli.run_json` gives: the CLI prints a line about its keyring
    # backend ahead of the payload, and a warning has been seen on stdout too.
    # This reads the same stdout that one does — `_gws_run._run_sync` hands it
    # over raw — so a whole-string parse throws the attachment away on exactly
    # the machines that print the line, and throws it away silently: the base64
    # goes to the sub-agent instead of the file, which is the state this
    # function exists to end.
    # `_sized` on all three misses below, for the reason #234 gives at
    # `finish`: `name` is the `-o` the model wrote, which is a document in the
    # household's mail, and the journal is `Storage=persistent` for a month
    # with *Forget* unable to reach it. Which of the three misses it was is
    # still in the message, so nothing diagnostic goes with the name. See #248.
    start = stdout.find("{")
    if start < 0:
        logger.info("gws cache: %s answered with no JSON at all", _sized(name))
        return ""
    try:
        blob = json.loads(stdout[start:])["data"]
        # urlsafe, because that is the alphabet Gmail answers in, and the
        # padding the API leaves off.
        raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
    except (ValueError, TypeError, KeyError, binascii.Error) as err:
        logger.info("gws cache: %s answered with no attachment: %s",
                    _sized(name), err)
        return ""
    if not raw:
        # `urlsafe_b64decode` validates nothing: a `data` field that is not
        # base64 at all decodes to zero bytes without raising, and reporting
        # "Saved the attachment" over an empty file sends the sub-agent to read
        # a document that is not there.
        logger.info("gws cache: %s decoded to nothing", _sized(name))
        return ""
    path = cache_dir() / _cache_name(name)
    _replace(path, raw)
    return path.name


_local = threading.Lock()
_held = threading.local()

# When the thread currently inside `writing()` took `_local`, on the monotonic
# clock, and 0.0 while nobody is inside. Written under `_local` and read
# without it, which is safe in the one direction that matters: it is cleared
# *before* the release, so a waiter reading between the two sees 0.0 and keeps
# waiting rather than a stale timestamp and a deadline that has already passed.
# A float assignment is atomic under the GIL and nothing here needs more than
# the last one written.
_since = 0.0


def _acquire_local() -> bool:
    """Take `_local`, or give up on a holder that has been in too long.

    The deadline belongs to whoever is inside, not to whoever is waiting.
    :data:`LOCAL_TIMEOUT` is sized as one longest hold, and a waiter's own
    clock measures something else: the sum of every hold in front of it.
    Against that sum the number would have to be the ceiling times the depth of
    the queue, and the queue is as deep as the thread pool — 8 × 90 s on the
    Pi, which is not a timeout. Timing the holder keeps the constant honest and
    takes queue depth out of the question, which is what it was always meant to
    be: the degrade is for a thread that has stopped making progress, and a
    thread that hands the lock on has not.

    So every handover restarts the clock, and any number of threads can queue
    without one of them giving up while the front of the queue is moving. What
    still fires is the case it exists for, one thread wedged inside the block,
    because that is the only way `_since` stays put for the whole deadline.

    Returns whether the lock was taken. False is the caller's cue to go on
    without it — see :func:`writing` for what that costs.
    """
    while True:
        if _local.acquire(timeout=_POLL):
            return True
        since = _since
        if since and time.monotonic() - since > LOCAL_TIMEOUT:
            return False


@contextlib.contextmanager
def writing():
    """Held from a command's snapshot to its diff, because those are one thing.

    :func:`written_by` works out what a command wrote by diffing the directory,
    and a second command writing inside that window is indistinguishable from
    this one writing twice — `gws_cache/download.html` is stamped `source: "gws
    calendar events delete …"` for exactly that reason, an HTML download
    wearing a calendar delete's name. `source`, and the `file_id` and `title`
    read out of it, are what the sub-agent matches a request against, so a
    wrong one sends it to the wrong document or makes it fetch one it already
    has.

    Only the snapshot, the command and the diff. Putting the header on is two
    network calls and a summarisation per file, and a two-document export would
    hold the lock past the sub-agent's own 160 s deadline — the second question
    would be answered "that took too long" having run nothing, waiting on
    somebody else's summary.

    :func:`_annotate` takes it back for the write alone, which is the part that
    needed it. It is invisible to the next command's diff, because `_replace`
    puts the mtime back; what it is not is safe against a second command having
    replaced the file during those network calls, and `_annotate` settles that
    by comparing against the mtime :func:`written_by` read while this lock was
    held, rather than against one of its own. :func:`write_index` holds it
    across both its read and its write, for the reason given there.

    A file lock as well as a thread lock, because the cache is shared between
    processes and documented as such — `tools/gws_bench.py` runs against the
    same directory as the live server. `flock` is advisory and per-open-file,
    so the thread lock is what makes it hold within this process too.

    Neither wait is unbounded, and the two are serial, so one acquisition
    behind one wedged holder is up to :data:`LOCAL_TIMEOUT` plus
    :data:`LOCK_TIMEOUT` — and behind a queue of live ones, up to the queue,
    which the thread pool bounds. See :data:`LOCAL_TIMEOUT` for both, and for
    the arithmetic across a whole `record`. Waiting forever would be the worse
    failure, because this runs under `asyncio.to_thread` and the sub-agent's
    160 s deadline does not reclaim the thread.

    Running out degrades in two ways and not one. `flock` running out leaves
    `_local` held, so this process is still serialised internally and only
    another process can overlap. `_local` running out returns without
    attempting the `flock` at all, so there is no lock of any kind and two
    threads here can overlap their diff windows — the case `written_by`'s
    mtimes cannot survive, because the later command reads its mtime after the
    earlier one's header has already landed.

    Which is why :data:`LOCAL_TIMEOUT` has to clear `_gws_cli.TIMEOUT` *plus*
    :data:`LOCK_TIMEOUT`, and not the subprocess timeout alone. `_local` is
    taken before the poll for the `flock` below and is still held through it,
    and still held through the unlocked `yield` when that poll gives up — so
    this server's own longest hold is the sum, and anything less lets one of
    its own threads reach the second degrade.

    And why the wait is :func:`_acquire_local` rather than a plain
    `acquire(timeout=...)`. One hold is what the number is sized against, and a
    waiter's own clock counts every hold in front of it: three ordinary ones
    clear 105 s between them, so the fourth command in flight used to time out
    against three threads that were all making progress. The deadline is the
    holder's, and a handover restarts it.

    `tools/cache_check.py` scales both constants down and runs three shapes at
    it: two threads behind a `flock` a foreign process is sitting on, a queue
    five deep with every hold at the subprocess ceiling, and one thread wedged
    inside the block — the last of which is what the degrade is for and has to
    still fire.

    Reentrant, and a pass-through the second time down. Neither lock is: a
    thread that took `_local` cannot take it again, and `flock` is per open
    file description, so a second `open` of the same path blocks against this
    process's own hold. Nobody nests today, but `record` now takes the lock
    once per file and again for the index, and the failure mode for calling it
    from inside a block is a minute's wait against yourself followed by
    carrying on unlocked — too quiet for what it is.

    `_held.depth` is reset in a `finally`, so it survives an exception. What it
    would not survive is this generator being driven outside a `with`: the
    depth would stay 1 on a pooled `asyncio.to_thread` worker and every later
    `writing()` on that thread would silently do nothing. All three production
    call sites use `with` — `_gws_run.py:596`, :func:`_still_ours` and
    :func:`write_index` — so this is a note and not a bug.
    """
    global _since
    if getattr(_held, "depth", 0):
        yield
        return
    _held.depth = 1
    try:
        if not _acquire_local():
            logger.warning("gws cache: one thread has held the lock for "
                           "%.0f s; continuing without it", LOCAL_TIMEOUT)
            yield
            return
        _since = time.monotonic()
        try:
            with (cache_dir() / _LOCK).open("w") as handle:
                if not _flock(handle, time.monotonic() + LOCK_TIMEOUT):
                    yield
                    return
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            # Cleared before the release, not after: a waiter that reads it in
            # between sees 0.0 and waits, instead of a stale timestamp whose
            # deadline has long since passed.
            _since = 0.0
            _local.release()
    finally:
        _held.depth = 0


def _flock(handle, deadline: float) -> bool:
    """Take the file lock, or give up at `deadline` and say we did.

    Polled rather than a blocking `flock`, because a blocking one has no
    deadline to give up at.
    """
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                logger.warning(
                    "gws cache: another process has held the lock for %.0f s; "
                    "continuing without it", LOCK_TIMEOUT,
                )
                return False
            time.sleep(_POLL)


def _cached(path: Path) -> bool:
    """Is this a document the cache holds, rather than its own bookkeeping?

    Hidden files are the lock and the half-written temporaries of
    :func:`_replace`; `index.md` is written from the listing and so cannot be
    part of it.
    """
    return (path.is_file() and not path.name.startswith(".")
            and path.name != INDEX)


def _replace(path: Path, body: str | bytes,
             mtime: float | None = None) -> None:
    """Write the file in one step, so no reader ever sees half of it.

    `listing()` runs from the sub-agent's instruction on every turn and reads
    the header of every file here, and `_annotate` rewrites files while it
    does. A truncate-and-write leaves a window in which the header comes back
    empty and a document loses its title in the prompt; a write to a temporary
    and an `os.replace` has no window at all.

    The temporary has a name nobody else can pick. Deriving it from `path`
    instead gave two overlapping commands the same one, and they need not have
    shared a document to collide: `record` rebuilds `index.md` whenever a
    command wrote anything, so any two of them met on `.tmp-index.md`. Whoever
    renamed it second got `FileNotFoundError` out of `os.replace`, from a
    command that had already run and already been charged.

    `mtime` puts the timestamp back where the caller found it. Diffing mtimes
    is how :func:`written_by` decides what a command wrote, and rewriting a
    file to put a header on it is not the document being fetched again —
    left alone it looks like one to whichever command runs next.

    `bytes` for :func:`unpack`, which has a PDF to write and no text to write
    it as. Everything the atomic write buys is wanted there too: `listing()`
    reads the header of every file in this directory on every turn.
    """
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=_TMP)
    tmp = Path(name)
    binary = isinstance(body, bytes)
    try:
        with os.fdopen(fd, "wb" if binary else "w",
                       **({} if binary else {"encoding": "utf-8"})) as handle:
            handle.write(body)
        # mkstemp opens at 0600. This is a copy of a document sitting in a
        # directory of them, not a secret the ones beside it are not.
        os.chmod(tmp, 0o644)
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def snapshot() -> dict[str, float]:
    """Filenames and modification times, to diff against after a command.

    A file that goes away between the listing and the stat is left out rather
    than raised about. `_cached` stats too, so there are two chances to miss it
    and nothing to gain by minding: it is not in the cache either way.
    """
    out = {}
    for path in cache_dir().iterdir():
        try:
            if _cached(path):
                out[path.name] = path.stat().st_mtime
        except OSError:
            continue
    return out


def _fields(lines) -> dict[str, str]:
    """Top-level `key: <json string>` lines, as a dict. Anything else skipped.

    Top-level is the load-bearing word. Everything this module writes starts in
    column one, and an indented line is a nested YAML key — so without the
    first test a document whose own front matter has a `document:` mapping with
    `source` and `fetched` under it reads as one of ours, and `_without_header`
    then deletes it.
    """
    fields: dict[str, str] = {}
    for line in lines:
        if line[:1].isspace():
            continue
        key, _, raw = line.partition(":")
        try:
            value = json.loads(raw.strip())
        except ValueError:
            continue
        if isinstance(value, str):
            fields[key.strip()] = value
    return fields


def _loose_fields(lines) -> dict[str, str]:
    """The same, keeping a value an older build wrote without quoting it.

    Only :func:`migrate` reads a header this way, and :func:`_was_ours` is what
    makes that safe. Nothing else may: the JSON test in :func:`_fields` is most
    of what tells a header of ours from a document's own front matter, and a
    reader that drops it starts deleting people's Jekyll blocks.
    """
    fields: dict[str, str] = {}
    for line in lines:
        if line[:1].isspace():
            continue
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        try:
            value = json.loads(raw.strip())
        except ValueError:
            value = raw.strip()
        if isinstance(value, str):
            fields[key.strip()] = value
    return fields


def _ours(fields: dict[str, str]) -> bool:
    """Whether a parsed block is a header this module wrote.

    A leading `---` says nothing: a Drive markdown file with Jekyll front
    matter opens with one, and so does a Doc whose first line is a horizontal
    rule. Both have a second `---` further down, so a block delimited by eye is
    the document's own text as often as it is this module's header.

    :func:`_annotate` writes `source` and `fetched` on every file it touches
    and writes them as JSON, so requiring both is the narrowest test that never
    rejects one of ours. What it cannot tell apart is a document whose own
    front matter opens with both of those names, in column one, with
    double-quoted values — `source: my own note`, unquoted, is not JSON and
    does not count, and neither does a `source` nested under something else.

    `tools/cache_check.py` runs the three shapes through `_without_header`, the
    Jekyll one through :func:`_annotate` as well, and the quoted-value and
    nested ones through :func:`_read_header`.
    """
    return all(name in fields for name in ("source", "fetched"))


# A `fetched` an older build wrote: an ISO timestamp with no quotes round it.
_LEGACY_FETCHED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _was_ours(fields: dict[str, str]) -> bool:
    """Whether a leniently parsed block is a header an *older build* wrote.

    :func:`_ours` cannot answer this, because the thing that dates the header
    is the very thing it tests: these files quote `source` and leave `fetched`
    bare, so :func:`_fields` drops `fetched` and the block stops being ours.
    Measured on pi2 at `24330ff`, where it was not an edge case — *every* header
    on the device was this shape, all six, so :func:`migrate` read none of them
    and reported nothing to do. The two with a `source` turned out to hold only
    a `fileId` and a `mimeType`, so nothing was lost that day; what had shipped
    was a migration that could not read a single header in the field.

    Lenient about quoting and stricter about everything else, which is the
    trade that keeps a document's own front matter out: our `source` is written
    by :func:`redacted` and so always starts `gws `, and our `fetched` is always
    a timestamp. A column-one `source` beginning with `gws ` plus an ISO-shaped
    `fetched` matches with or without quotes.
    """
    return (_ours(fields)
            and fields["source"].startswith("gws ")
            and _LEGACY_FETCHED.match(fields["fetched"]) is not None)


def _header_block(path: Path) -> list[str]:
    """The lines between a cached file's two `---`s. Empty when it has none.

    A line at a time and no more than :data:`_HEADER_LINES` of them, because
    the index reads every file in the cache on every turn and some of them are
    megabytes. Reading a line at a time is not enough on its own: a document
    that opens with `---` and never closes it used to have every line of itself
    appended to `block` before the loop ran out, so a 5 MB file cost 5 MB of
    buffer to decide it had no header at all.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            if handle.readline(_HEADER_BYTES) != _HEADER:
                return []
            block: list[str] = []
            while len(block) < _HEADER_LINES:
                line = handle.readline(_HEADER_BYTES)
                if not line:
                    return []
                if line == _HEADER:
                    return block
                block.append(line)
    except (OSError, UnicodeDecodeError):
        return []
    return []


def _read_header(path: Path) -> dict[str, str]:
    """The header of a cached file, as a dict. Empty when it has none.

    Every value is JSON, so a summary with a colon or a newline in it survives
    the round trip and the parser stays one line long.
    """
    fields = _fields(_header_block(path))
    return fields if _ours(fields) else {}


def _legacy_header(path: Path) -> dict[str, str]:
    """The header of a cached file an older build wrote. Empty when it is not.

    :func:`migrate` is the only caller and :func:`_was_ours` says why.
    """
    fields = _loose_fields(_header_block(path))
    return fields if _was_ours(fields) else {}


def _split_header(text: str) -> tuple[list[str], str]:
    """A file's header lines and the document under them, both unparsed.

    Where the block ends is arithmetic two callers need and neither should own:
    :func:`_without_header` decides whether to keep the block, and
    :func:`migrate` rewrites it.
    """
    if not text.startswith(_HEADER):
        return [], text
    end = text.find(f"\n{_HEADER}", len(_HEADER) - 1)
    if end < 0:
        return [], text
    return (text[len(_HEADER):end + 1].splitlines(),
            text[end + len(_HEADER) + 1:].lstrip("\n"))


def _without_header(text: str) -> str:
    """`text` with a header this module wrote taken off, if it has one.

    The mirror of :func:`_header`, and it has to be a parse. Cutting from the
    first `---` to the next one deleted a Jekyll file's own front matter, and
    took the whole first section off a Doc that opens on a horizontal rule —
    and the copy this runs on is the one the sub-agent reads. See :func:`_ours`
    for what counts as ours.
    """
    block, body = _split_header(text)
    return body if _ours(_fields(block)) else text


def _header(fields: dict[str, str]) -> str:
    body = "".join(f"{key}: {json.dumps(value)}\n" for key, value in fields.items())
    return f"{_HEADER}{body}{_HEADER}\n"


# The keys this module writes into headers. migrate() normalises only these,
# leaving any document's own front matter lines untouched.
_CACHE_KEYS = {
    "source",
    "fetched",
    "file_id",
    "title",
    "mime_type",
    "modified",
    "summary",
    "pdf",
}


def _rewrite_legacy_header(lines: list[str], header: dict[str, str], clean_source: str) -> str:
    """Rewrite a legacy header, normalising cache keys and keeping other lines verbatim.

    Normalises only the keys the cache owns (:data:`_CACHE_KEYS`), and
    preserves every unrecognised key or line verbatim from the original header
    block so that nested items, booleans, numbers, and list items in a
    document's own front matter are not dropped.
    """
    out: list[str] = []
    seen_keys: set[str] = set()
    for line in lines:
        if line[:1].isspace() or line.startswith(("-", "#")) or ":" not in line:
            out.append(f"{line}\n")
            continue
        key, sep, raw = line.partition(":")
        k = key.strip()
        if k in _CACHE_KEYS:
            if k in seen_keys:
                continue
            seen_keys.add(k)
            val = clean_source if k == "source" else header.get(k)
            if val is not None:
                out.append(f"{k}: {json.dumps(val)}\n")
            else:
                out.append(f"{line}\n")
        else:
            out.append(f"{line}\n")
    if "source" not in seen_keys:
        out.insert(0, f"source: {json.dumps(clean_source)}\n")
        seen_keys.add("source")
    for k, v in header.items():
        if k in _CACHE_KEYS and k not in seen_keys:
            out.append(f"{k}: {json.dumps(v)}\n")
            seen_keys.add(k)
    return f"{_HEADER}{''.join(out)}{_HEADER}\n"


def params(argv: list[str]) -> dict:
    """Everything the `--params` and `--json` blobs on a command line say.

    One reader because two callers want it and the shapes are fiddly: gws takes
    a blob as either `--params '{...}'` or `--params={...}`, one command can
    carry both flags, and a blob that will not parse is not worth an exception
    in either place. Later flags win, which is what the CLI does with them.
    """
    found: dict = {}
    for i, token in enumerate(argv):
        if token in ("--params", "--json") and i + 1 < len(argv):
            blob = argv[i + 1]
        elif token.startswith(("--params=", "--json=")):
            blob = token.split("=", 1)[1]
        else:
            continue
        try:
            parsed = json.loads(blob)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            found.update(parsed)
    return found


def _identity(command: str) -> str:
    """The Drive id a gws command names, or "" when it names none.

    Read out of the `--params` the sub-agent passed, which is where every gws
    subcommand takes it. A Gmail message or a calendar event has an id too, but
    neither is a document anybody exports, so neither is looked for here.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return ""
    fields = params(argv)
    for key in ID_KEYS:
        value = fields.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


# What Drive has already said about a file, for the length of one question.
# `_annotate` asks for a document's `modifiedTime` to write the header, and
# seconds later `_is_stale` asks Drive the same thing about the same file to
# check the copy that was just written is fresh — two subprocesses, 1.16 s of a
# cold question, for one answer. See #32.
#
# A ContextVar because there is no call path between the two to thread an
# argument down: `_annotate` runs on a `to_thread` worker inside `record`, and
# `read_cache` is a separate tool the sub-agent invokes a turn later. Both copy
# the context and the dict is mutable, so it crosses in both directions.
_META: contextvars.ContextVar[dict[str, dict] | None] = contextvars.ContextVar(
    "ai_terminal_gws_drive_meta", default=None
)


@contextlib.contextmanager
def remembering():
    """Ask Drive about a given file once for the length of one question.

    Set and reset unconditionally, which is where this differs from
    :func:`~_gws_run.budget` and has to: that one passes through to a hold
    already open so a plan's four gws steps share one allowance, and a memo
    doing the same would span several exports and answer one step's freshness
    check out of another step's reading of Drive. Each step gets its own.

    Scoped to the question rather than kept on a clock, so there is no TTL to
    tune and nothing survives into the next question to weaken its check.
    Outside one of these, :func:`_drive_meta` behaves as it always did.
    """
    token = _META.set({})
    try:
        yield
    finally:
        _META.reset(token)


def _drive_meta(file_id: str) -> dict:
    """Title, type and last-modified for one Drive file. `{}` if unavailable.

    Answered from :func:`remembering`'s memo when this question has already
    asked about this file. Only a real answer is remembered: `{}` is what a
    failed lookup returns, and a question that could not reach Drive once is
    not a question that should stop trying.

    Run from a scratch directory and not from the cache. It asks for fields and
    writes nothing, but it is a subprocess, and the cache's bookkeeping is a
    diff of that directory taken around somebody else's command — anything the
    CLI decided to drop beside itself would be attributed to whatever was
    running at the time. :func:`_annotate` calls this outside the lock.

    `ignore_cleanup_errors` because the caller sits outside any `try`: a
    scratch directory that will not delete would otherwise raise out of a
    command that has already run and already been charged for.
    """
    memo = _META.get()
    if memo is not None and file_id in memo:
        return memo[file_id]
    # Timed on its own because it is a whole `gws` subprocess spent on three
    # fields. A memo hit logs no step at all, which is the measurement: a cold
    # question used to carry two `drive.meta` lines and now carries one.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch, \
            _timing.step("drive.meta"):
        meta = _gws_cli.json_output(
            [
                "drive", "files", "get", "--params",
                json.dumps({
                    "fileId": file_id,
                    "fields": "id,name,mimeType,modifiedTime",
                }),
            ],
            cwd=scratch,
        ) or {}
    if memo is not None and meta:
        memo[file_id] = meta
    return meta


def _summarize(name: str, text: str) -> str:
    """Two or three sentences about a document, from the small model.

    Once, here, rather than on every model turn: the summary is what the
    sub-agent matches a question against, and a description regenerated per
    turn would cost more than the fetch it exists to avoid.

    Uses the shared grounding client rather than ``GWS_MODEL``: it is already
    built and pointed at a region, and cataloguing a document is the smaller
    job of the two, so it does not want the sub-agent's larger model.

    Untimed here; :func:`finish` times the call. The step has to be outside
    this function because that is the only way it survives a stub of it, and
    every check of where the model call now happens works by stubbing this.
    """
    try:
        response = grounding_client().models.generate_content(
            model=grounding_model(),
            contents=f"Filename: {name}\n\n{text[:SUMMARY_INPUT]}",
            config=GenerateContentConfig(
                system_instruction=_SUMMARY_PROMPT,
                temperature=0,
                max_output_tokens=300,
            ),
        )
        return " ".join((response.text or "").split())
    except Exception as err:  # pylint: disable=broad-except
        # A document with no summary is still a cached document. Whatever went
        # wrong here — quota, a filter, a network blip — losing the copy over
        # it would be the worse outcome.
        #
        # Sized for the same reason `finish` sizes the `summarize.bg` step one
        # frame above this, and #234 sized that: `name` is `path.name`, a
        # document in the household's Drive, and this is the one line in the
        # pair that #234 did not reach. `err` stays whole — it is the API's own
        # status, not the household's words, and it is the only thing here that
        # says why. See #248.
        logger.warning("gws cache: could not summarise %s: %s",
                       _sized(name), err)
        return ""


# What stands in for a value the header will not carry. Deliberately not a
# valid argument to anything: a command re-run with one of these in it fails
# loudly at the CLI, which is the failure to want. A silently plausible
# placeholder would send the sub-agent a command that fetches the wrong
# document.
ELIDED = "<elided>"

# The flags whose value is a number, an enum or a filename this directory
# already holds under that name, so keeping it costs nothing. `-o` is the file
# the header is written into; eliding its own name out of its own header is
# theatre. `--upload` is not here: it is a path somebody typed.
_SHOWN_FLAGS = frozenset((
    "--format", "-o", "--output", "--page-limit", "--page-delay",
    "--sanitize", "--max",
))

# The `--params` and `--json` keys whose value is an identifier, a projection
# or an enum rather than a sentence. Everything else is elided, and that is the
# direction this has to fail in: a key nobody thought of is a key whose value
# is unknown, and unknown means show nothing. `q`, `query`, `name`, `subject`
# and every body field are absent for that reason rather than by being named.
#
# What is in here is what the four commands that write a cached file need to
# run again — `fileId` and `mimeType` for an export, `fileId` and `alt` for a
# download, `documentId` for a Doc, `userId`/`messageId`/`id` for a Gmail
# attachment — plus the parameters that only change how the answer is printed.
_SHOWN_KEYS = frozenset((
    *ID_KEYS, "userId", "messageId", "attachmentId", "id", "revisionId",
    "alt", "mimeType", "fields", "pageSize", "maxResults", "pageToken",
    "prettyPrint", "supportsAllDrives", "includeItemsFromAllDrives",
))


def _shown(flag: str, value: str) -> str:
    """One flag's value, as the header may keep it."""
    if flag in ("--params", "--json"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return ELIDED
        if not isinstance(parsed, dict):
            return ELIDED
        return json.dumps(
            {key: (item if key in _SHOWN_KEYS else ELIDED)
             for key, item in parsed.items()},
            ensure_ascii=False)
    return value if flag in _SHOWN_FLAGS else ELIDED


def _word_indices(argv: list[str]) -> set[int]:
    """Where in `argv` the subcommand words are.

    :func:`_cli_words` answers *what* they are and this answers *where*,
    because a redaction has to keep the words in place rather than re-emit
    them: `gws gmail +search --query drive` has a value that is also a
    subcommand word, and a set-membership test would keep it.

    The *first* run of plain words and not the longest, which is where this
    parts company with `_cli_words`. Both have to skip a leading global —
    `--format json gmail users messages attachments get` is a line the CLI
    accepts — and every flag that can precede the service is in
    :data:`VALUED`, so the first run is the command in every line gws will
    take. The longest run is the same run right up until some flag nobody
    named carries a value of several words, and then it is the value: `gws
    gmail +triage --query is:unread from:boss subject:invoice has:attachment`
    made the query the longest run, kept all five operators verbatim, and
    elided `gmail +triage` instead — #233 twice over, because the words the
    :data:`~.gws.ALREADY_STALE` re-fetch needs went out and the sentence the
    household said stayed in. `_cli_words` keeps the longest run: it is
    identifying a command rather than protecting a value, its one caller
    compares the answer against :data:`_ATTACHMENT`, and a wrong answer there
    means one attachment is not unpacked rather than a leak.

    So: wrong here means too much elided, never too little.
    """
    runs: list[list[int]] = [[]]
    skip = False
    for index, token in enumerate(argv):
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            skip = token in VALUED
            runs.append([])
        else:
            runs[-1].append(index)
    return set(next((run for run in runs if run), []))


def redacted(command: str) -> str:
    """A gws command line with everything somebody could have said taken out.

    The header used to carry the command verbatim, which put the household's
    own search string on the SD card, in a store neither *Forget* nor the
    journal's retention reaches — #233, and #206 for why the button is the bar.
    The command still has to be *there*: :data:`~.gws.ALREADY_STALE` hands it
    back to the sub-agent as the way to fetch a copy Drive has moved past.

    So the shape survives and the values do not. Subcommand words and flag
    names are kept, and a value is kept only when it is named in
    :data:`_SHOWN_FLAGS` or :data:`_SHOWN_KEYS`. That allowlist covers every
    command that writes a cached file, so the one path that reads this back is
    the one path it costs nothing: a stale copy has a `file_id` by
    construction — :func:`_is_stale` returns False without one — and a
    `fileId` is kept, so the re-fetch command comes out whole.

    A line that will not split is replaced rather than repaired. `shlex` is
    what parsed it everywhere else in this module, and a line it cannot read is
    a line nothing here can say anything about the parts of.

    Idempotent, which is what :func:`migrate` leans on: redacting an already
    redacted line changes only JSON key spacing, and only the first time.
    The replacement above is quoted for that reason and not for `shlex`'s —
    `gws <elided>` splits back into a word this function then quotes, so the
    unquoted spelling comes out different on the second pass and `migrate`
    rewrites the file again on the next start, having said it would not.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return f"gws {shlex.quote(ELIDED)}"
    if argv and argv[0] == "gws":
        argv = argv[1:]
    words = _word_indices(argv)
    out: list[str] = []
    flag = ""
    skip = False
    for index, token in enumerate(argv):
        if skip:
            skip = False
            out.append(_shown(flag, token))
        elif token.startswith("-"):
            name, sep, value = token.partition("=")
            flag = name
            skip = token in VALUED
            out.append(f"{name}={_shown(name, value)}" if sep else name)
        else:
            out.append(token if index in words else ELIDED)
    return f"gws {shlex.join(out)}"


def migrate() -> int:
    """Redact command lines out of headers a previous release wrote.

    Every cached file written before #233 still holds a whole gws command line
    in its `source`, and :func:`_ours` reads it straight back into a prompt. On
    read rather than on start would leave the bytes on the card, which is the
    half of this that *Forget* cannot reach, so the rewrite happens once at
    startup — `main._lifespan` — and the file is the thing that changes.

    The mtime is put back. It is what :func:`_evict` orders by and what
    :func:`written_by` diffs against, and a migration that reordered the cache
    or made every file look freshly written would be a worse bug than the one
    it fixes.

    Only files whose header would come out different are rewritten, so a second
    start writes nothing. Returns how many changed, for the log.

    A header from before every value was JSON is rewritten whether or not its
    `source` holds anything — normalising it is what makes it readable, and a
    header :func:`_read_header` cannot read is one this would skip forever.
    See :func:`_was_ours`, which is the whole of why the first pi2 deploy
    migrated nothing at all.
    """
    done = 0
    with writing():
        for path in sorted(cache_dir().iterdir()):
            try:
                if not _cached(path):
                    continue
                header = _read_header(path)
                legacy = False
                if not header:
                    header = _legacy_header(path)
                    legacy = bool(header)
                source = header.get("source", "")
                clean = redacted(source) if source else ""
                if not clean or (clean == source and not legacy):
                    continue
                seen = path.stat().st_mtime
                text = path.read_text(encoding="utf-8")
                if legacy:
                    block, body = _split_header(text)
                    _replace(path, _rewrite_legacy_header(block, header, clean) + body, seen)
                else:
                    body = _without_header(text)
                    _replace(path, _header(header | {"source": clean}) + body, seen)
                done += 1
            except (OSError, UnicodeDecodeError):
                continue
    if done:
        logger.info("gws cache: redacted the command line out of %d header(s) "
                    "written by an older build", done)
    return done


def forget() -> int:
    """Delete every cached document. The Forget button's half of this module.

    `api_forget` clears the sessions, the recap and the household's facts, and
    for as long as it did not clear this the terminal went on holding copies of
    their Workspace documents on the card — the #206 shape in a second store.
    The header redaction above and this one cover different people: one the
    household that never presses the button, the other the household that does.

    Everything, not only the documents. `index.md` is rewritten empty rather
    than left naming files that are gone, and the `.tmp-` files of a write in
    flight go with it. The lock is the one thing kept: unlinking it while
    holding the `flock` leaves the next command taking a lock on a new inode
    and overlapping a holder it cannot see.

    Returns how many cached documents there were, which is what the endpoint
    reports. `index.md` and the bookkeeping are not documents and are not
    counted, so the number matches what the listing showed.
    """
    gone = 0
    with writing():
        for path in cache_dir().iterdir():
            if path.name == _LOCK:
                continue
            try:
                counts = _cached(path)
                path.unlink()
                gone += counts
            except OSError:
                continue
        _write_index([])
    logger.info("gws cache: forgot %d cached document(s)", gone)
    return gone


def _meta_fields(command: str, when: datetime) -> dict[str, str]:
    """The header every cached file gets: where it came from, and when.

    Plus whatever Drive says about it, when the command named a document id.
    A Gmail attachment names none, so it gets the first two and nothing else —
    which also means :func:`_is_stale` cannot check it and will not try.

    `source` is :func:`redacted` and not the command itself. This is the one
    place every header is built — :func:`_annotate` for a text export and
    :func:`_sidecar` for a PDF's copy, and there is no third — so it is the one
    place the redaction has to be. :func:`_identity` below still reads the
    *un*redacted command, because the id it is looking for is the part that is
    kept either way and the header wants it whole.
    """
    fields = {
        "source": redacted(command),
        "fetched": when.isoformat(timespec="seconds"),
    }
    file_id = _identity(command)
    if file_id:
        fields["file_id"] = file_id
        meta = _drive_meta(file_id)
        for key, field in (("name", "title"), ("mimeType", "mime_type"),
                           ("modifiedTime", "modified")):
            if isinstance(meta.get(key), str):
                fields[field] = meta[key]
    return fields


def _kept(path: Path, seen: float) -> list[str]:
    """Name a file this command wrote but could put no header on.

    A spreadsheet download, an image, a PDF too damaged to open. The sub-agent
    has the file whether or not anything here can read it, and leaving it out
    of the answer is how a fetched document becomes one nobody asks for again.
    """
    return [path.name] if _still_ours(path, seen) else []


def _stranger(out: Path, name: str) -> bool:
    """Is a document that did not come out of `name` sitting at `out`?

    The other side of the test :func:`_drop_sidecar` deletes on, and one
    function so that the two cannot drift apart: a copy this module wrote for
    a PDF carries `pdf: <that PDF>` in its header, and anything else at that
    name is a document in its own right. A Doc exported with `-o memo.pdf.md`
    is one, and so is a binary downloaded as `-o memo.pdf.txt`, which
    :func:`_cache_name` also flattens onto `memo.pdf.md` because `.md` is not
    in :data:`TEXT_SUFFIXES`. Neither may be written over, and neither may be
    handed to a sub-agent that asked for the PDF.

    A file that is not there is nobody's, and :func:`_read_header` cannot tell
    that apart from one with no header of ours — hence the `exists`.
    """
    return out.exists() and _read_header(out).get("pdf") != name


def _drop_sidecar(path: Path, seen: float) -> None:
    """Delete a text copy left over from a different document at this name.

    `paper.pdf` is the instruction's own example filename (`gws.py:434`), so a
    second download landing on a name a PDF already holds is the expected case,
    not the exotic one. When that download cannot be read as a PDF — encrypted,
    truncated, a `.docx` written as `-o paper.pdf` — nothing overwrites the old
    sidecar, and it stays on disk carrying the *previous* document's `file_id`,
    title and text. :func:`_is_stale` cannot catch that: the file it names is
    genuinely unchanged. The instruction meanwhile tells the sub-agent the text
    is in `paper.pdf.md`, so the answer comes back about the wrong invoice, in
    full confidence.

    The `pdf:` header is what keeps this from deleting an unrelated `.md`: an
    `invoice.md` exported from a Doc has no such key and is left alone.

    `seen` re-checks under the lock the way every other write in this module
    does, because the decision to call this came from bytes read outside it —
    `_annotate:1034` — and :func:`_from_pdf` is 101 ms on 60 pages, longer on
    a Pi's SD card. In that window another command can land a good PDF at this
    name and write it a correct sidecar, and deleting that one leaves a valid
    PDF with no text copy at all, invisible to `listing` and `search_cache`
    and healed by nothing.
    """
    out = path.with_name(PDF_TEXT.format(name=path.name))
    with writing():
        if not _still_ours(path, seen):
            return          # the file at this name is another command's now
        if not _stranger(out, path.name):
            out.unlink(missing_ok=True)


def _from_pdf(path: Path) -> str | None:
    """The text layer of a PDF, up to :data:`PDF_PAGES` pages.

    `""` when it is a PDF with nothing selectable on it — a scan — which is a
    real answer and not a failure. `None` when it will not open as a PDF at
    all, which is where every other binary lands too: an `.xlsx`, a `.png`, a
    download that turned out to be an HTML error page.

    Pages are joined with a blank line rather than a form feed. What reads this
    back is a model, and a blank line is the separator every other document in
    the cache already uses between blocks.

    A PDF with no pages at all is `None`, not `""`. There is nothing in it to
    have a text layer, and :data:`NO_TEXT` would tell the sub-agent the pages
    are images when there are no pages.

    pypdf logs a warning per embedded CFF font when fontTools is absent — 2 KB
    of `/Widths` noise per fetch on a real Apple PDF, none of it actionable.
    Turned down here rather than adding fontTools, which is 3 MB this does not
    otherwise want.
    """
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        pages = pypdf.PdfReader(path).pages
        if not len(pages):
            logger.info("gws cache: %s is a PDF with no pages", path.name)
            return None
        text = "\n\n".join((page.extract_text() or "").strip()
                           for page in pages[:PDF_PAGES])
    except Exception as err:  # pylint: disable=broad-except
        # pypdf raises its own errors for a damaged file and whatever the
        # underlying codec raises for a truncated one. Neither is worth losing
        # the fetch over: the bytes are cached, and this says only that they
        # are not text.
        logger.info("gws cache: %s does not read as a PDF: %s", path.name, err)
        return None
    return text.strip()


def _sidecar(path: Path, command: str, when: datetime, seen: float,
             later: list[tuple[Path, dict, str, float]]) -> list[str]:
    """Turn a downloaded PDF into a text file the rest of the cache can read.

    A number printed on an invoice was unanswerable at any budget: the bytes
    cached fine, and `read_cache` handed back `'utf-8' codec can't decode`.
    Written beside the PDF rather than over it, so the bytes survive. Nothing
    discovers them by name any more — after the `_records` fold, `index.md`,
    `listing` and `search_cache` mention only `paper.pdf.md` — but the PDF is
    still the name in the download command the sub-agent just ran, and it is
    the name `read_cache` starts from before falling through to here.

    The sidecar carries the same `file_id` and `modified` as the PDF would
    have, so :func:`_is_stale` guards the text and the bytes alike, and `pdf`
    names the file it was read out of — which is what tells a reader of the
    directory that the two are one document and not two.

    Nothing is written when another document already holds that name, and the
    PDF is named on its own — see :func:`_stranger` for what counts as another
    document, and :func:`read_cache` for what the sub-agent is told when it
    asks for the bytes and finds no copy. The text is dropped rather than put
    somewhere else because the name is how the copy is found: four places
    compute it from the PDF's, and a second name would be a directory scan in
    each of them. What the sub-agent can do instead is one command, a re-fetch
    under an `-o` name that is free, and the error names that as the way out.

    This is the seam for `.xlsx` and `.docx` when their turn comes; it is not
    the place to guess at them now.
    """
    with _timing.step("pdf"):
        text = _from_pdf(path)
    if text is None:
        # Whatever is at this name now is not the document the sidecar was read
        # out of, and there is no new text to overwrite it with.
        _drop_sidecar(path, seen)
        return _kept(path, seen)

    fields = _meta_fields(command, when)
    fields["pdf"] = path.name
    if not text:
        fields["text_layer"] = "none"
        text = NO_TEXT

    out = path.with_name(PDF_TEXT.format(name=path.name))
    # One hold for the check and the write. The PDF is not rewritten — only its
    # mtime is read — but a second command landing on that name mid-extraction
    # means this text belongs to their document, and writing it under our name
    # is the bug :func:`_still_ours` exists to stop, one file over.
    #
    # The destination is tested inside the same hold, and it has to be. That
    # check guards the PDF and says nothing about where the text goes, and the
    # window is open at both ends: `_from_pdf` above is 101 ms on 60 pages and
    # longer on a Pi, and an export can land at `out` inside it. Asked before
    # the lock, the answer would be as stale as the mtime `_drop_sidecar`
    # refuses to act on, and the file it cleared would be gone by the write.
    with writing():
        if not _still_ours(path, seen):
            return []
        if _stranger(out, path.name):
            # Logged, because this is the one path here that keeps a document
            # by throwing text away, and nothing else would show it: the fetch
            # succeeded, the bytes are cached, and only the read afterwards
            # says anything is missing.
            logger.warning("gws cache: %s already holds another document, so "
                           "the text of %s was not written",
                           out.name, path.name)
            return [path.name]
        _replace(out, _header(fields) + text + "\n", seen)
    later.append((out, fields, text, seen))
    return [path.name, out.name]


def _annotate(path: Path, command: str, when: datetime, seen: float,
              later: list[tuple[Path, dict, str, float]]) -> list[str]:
    """Give a freshly written text file its header.

    Returns the names this command has to report, which is not the same
    question as which of them got a header. A spreadsheet download is not
    editable text and gets none, and the sub-agent has it all the same — so it
    is named. Leaving it out took every binary download out of the `cached`
    list `_gws_run` hands back, and a file the tool never mentions is one
    nobody asks for again.

    A PDF is named twice: the bytes, and the text :func:`_sidecar` reads out of
    them. Which files are PDFs is decided by the first five bytes and not by
    the extension, because the extension is one the model typed. An invoice
    saved as `-o invoice` is still an invoice, and an uncompressed PDF decodes
    as UTF-8 perfectly well — dispatching on a `UnicodeDecodeError` filed that
    one as a text document whose body happened to start `%PDF-1.4`.

    Empty is for a file this command does not have: one another command
    replaced while this one was being indexed, or one that has gone. `seen` is
    the mtime :func:`written_by` read under the lock — the one that decided
    this file is this command's — and :func:`_still_ours` is where it is
    checked, for the binary and for the text alike.

    The header this writes has no `summary` in it. Writing one costs a model
    call, 1.9 s of a cold question at p50, and the question is waiting on this:
    nothing downstream of here reads the summary, only the *next* question's
    listing does. So the work goes on `later` — everything :func:`finish` needs
    to write the summary in once the answer is out — and this returns. See #32.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if raw.startswith(PDF_MAGIC):
        return _sidecar(path, command, when, seen, later)
    # Past the magic bytes, so whatever is here now is not a PDF. Any sidecar
    # at this name belongs to a document that is gone.
    _drop_sidecar(path, seen)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _kept(path, seen)
    # Google's exports open with a BOM, which shows up as a stray glyph in
    # front of the first heading everywhere it is read.
    text = text.lstrip("﻿")
    # A fresh export has no header of ours. One here means the file has been
    # through this function already, which the mtime check below should have
    # caught — so this is the second guard on one thing and cheap enough to
    # keep. What it stops is a header landing on top of a header:
    # `_read_header` reads the first block, so the listing would name whichever
    # command lost the race, and `_summarize` would be handed our own YAML to
    # describe. What it must not do is touch the document's own front matter,
    # which is why `_without_header` parses rather than cuts.
    text = _without_header(text)

    fields = _meta_fields(command, when)
    # Back under the lock, and only for the write. `_still_ours` is reentrant
    # through `writing`, so the check and the write are one hold and nothing
    # can land between them.
    with writing():
        if not _still_ours(path, seen):
            return []
        _replace(path, _header(fields) + text, seen)
    # Queued after the write, not before it: `finish` writes the same header
    # again with a `summary` line added, and a file this command turned out not
    # to own has nothing to add it to. Still `seen`, because the write above
    # put the mtime back where it found it.
    later.append((path, fields, text, seen))
    return [path.name]


def finish(later: list[tuple[Path, dict, str, float]]) -> None:
    """Write the summaries :func:`_annotate` left behind.

    Off the question's clock — the answer went out while this was still queued
    — so the steps it logs are detached from it. They would otherwise be
    counted against whichever question happened to be in flight when the model
    came back, which is the next one, and blame it for time it did not spend.

    Per file rather than in one hold: the model call is the slow part, it needs
    no lock, and taking one across four documents would stall a concurrent
    export behind three summaries it does not care about.

    A file replaced since :func:`_annotate` wrote it is skipped and keeps the
    newer command's header — the same `seen` check, against the same mtime,
    because :func:`_replace` puts the timestamp back and a header write is
    invisible to it. The summary is lost in that case, and it is the right
    thing to lose: it describes a document that is no longer at that name.

    Nothing calls this on shutdown. A process killed between the two writes
    leaves a file with a valid header and no summary, and an index row reading
    `—`, which is what a document that failed to summarise has always looked
    like.
    """
    with _timing.detached():
        for path, fields, text, seen in later:
            # `summarize.bg` and not `summarize`, so that a log holding both a
            # run from before this change and a run from after it can still be
            # read: the old name means the model call was inside the question.
            # Sized for the same reason as `gws.py`'s `hit`: `mark` writes
            # every field of the record to the journal, and a cached file's
            # name is a document in the household's Drive. `chars` already
            # tells one line of a batch from the next. See #234.
            with _timing.step("summarize.bg", file=_sized(path.name),
                              chars=min(len(text), SUMMARY_INPUT)):
                summary = _summarize(path.name, text)
            if not summary:
                continue
            with writing():
                if not _still_ours(path, seen):
                    continue
                _replace(path, _header(fields | {"summary": summary}) + text,
                         seen)
                # Inside the hold, and reentrant through `writing`: the row for
                # this file is read off the file, so an index written between
                # the two would be an index without the summary in it.
                write_index()


# The summaries in flight. asyncio keeps only a weak reference to a task, so a
# task nobody holds can be collected mid-await; the callback drops each one as
# it lands. Its own set and not `_gws_run._RUNNING` or `browse._JOBS`, both of
# which are cancelled when the socket closes — the point of this work is that
# it outlives the question, and a person who hangs up right after an answer is
# exactly the case that most wants the summary written anyway.
_SUMMARIES: set[asyncio.Task] = set()


def defer(later: list[tuple]) -> None:
    """Run :func:`finish` behind the answer, on a worker of its own.

    Called from the async side because :func:`record` cannot call it: `record`
    runs on a `to_thread` worker, which has no running loop to create a task
    on. Fire and forget — nothing waits on the result, and a failure in there
    must not reach the caller, whose command has already succeeded.
    """
    if not later:
        return
    task = asyncio.create_task(asyncio.to_thread(finish, later))
    _SUMMARIES.add(task)
    task.add_done_callback(_settled)


def _settled(task: asyncio.Task) -> None:
    """Drop a finished summary task, and read its exception if it has one.

    Reading it is the point of having this rather than `_SUMMARIES.discard`:
    an exception nobody retrieves is reported by asyncio when the task is
    collected, out of any context, as an unhandled error in a server that is
    working fine. The header and the copy both landed; only the summary is
    missing, which is a warning.
    """
    _SUMMARIES.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("gws cache: summaries failed: %s", task.exception())


def _still_ours(path: Path, seen: float) -> bool:
    """Whether `path` is still the file the diff attributed to this command.

    A second command exporting to the same name has already replaced it —
    `download.html` is what gws calls a download it was not given a name for,
    so two of them landing on one name is the ordinary case and not a corner —
    and the text read for it would then be their document wearing this
    command's name, which is :func:`writing`'s own bug arriving by a different
    route.

    Against `seen` and not against a stat taken when the file was read. The
    question is *is this the body the diff saw*, and a later stat cannot answer
    it: if the other command's write landed before it, its mtime is already
    what a local stat returns and the check waves the file through.
    :func:`record` annotates one file at a time with two network calls each, so
    a two-file export holds that window open for seconds, and the other command
    need not have started after this one. `seen` was read under the lock at the
    moment the diff attributed the file, which is the only mtime that makes the
    question answerable.
    """
    with writing():
        try:
            if path.stat().st_mtime == seen:
                return True
        except OSError:
            return False
    logger.info("gws cache: %s was rewritten while it was being indexed; "
                "leaving the newer header alone", path.name)
    return False


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count / 1024 / 1024:.1f} MB"


def _records() -> list[dict]:
    """Every cached file with its header, newest first.

    One read of the directory behind the index, the listing and the search, so
    the three of them cannot disagree about what is cached.

    A file deleted mid-listing is skipped, not raised about: `listing()` runs
    from the sub-agent's instruction on every turn, so an exception here loses
    the turn over a document nobody asked for.

    A PDF and its sidecar are one document and get one record — the sidecar's,
    because it is the one with the title, the summary and the text. Two rows
    spends one of :data:`LISTING_LIMIT` on a bare unreadable filename, and it
    is what made :func:`named` ambiguous on every multi-word PDF name, so
    `what's the total on the zipair invoice` cost a whole extra turn.
    """
    records = []
    for path in cache_dir().iterdir():
        try:
            if not _cached(path):
                continue
            side = path.with_name(PDF_TEXT.format(name=path.name))
            if _read_header(side).get("pdf") == path.name:
                continue
            stat = path.stat()
        except OSError:
            continue
        records.append({
            "name": path.name,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            **_read_header(path),
        })
    return sorted(records, key=lambda r: r["mtime"], reverse=True)


def write_index() -> None:
    """Rebuild `index.md` from the headers of whatever is in the directory.

    Rebuilt rather than appended to, so deleting a cached file is all it takes
    to drop it from the index. Nothing reads this — the sub-agent gets
    ``listing()`` — but it is the surface that says what the cache thinks it
    has, which is what you want when it answers from the wrong document.

    Read and write under one lock, because rebuilt-from-scratch means the two
    are one operation: unlocked, the command that reads the directory first can
    still be the one that writes last, and the index it publishes is missing
    whatever landed in between — until something else writes a file. Cheap to
    hold, unlike :func:`_annotate`: no network, just the directory.

    The eviction goes here and not on a timer for the same reason the sweep
    does: this is the one place that runs once per `record`, under the lock,
    and it is about to walk the directory anyway. Before the write rather than
    after it, so the index that gets published is the one the directory now
    has — and :func:`_evict` hands its survivors on, so the walk is paid for
    once instead of once each.
    """
    with writing():
        _sweep_temporaries()
        _write_index(_evict())


def _sweep_temporaries() -> None:
    """Delete `.tmp-` files old enough that nobody is still writing them.

    :func:`_replace` unlinks its own on failure, but a crash between `mkstemp`
    and `os.replace` leaves one behind for good: the name is unique per call so
    nothing overwrites it, and :func:`_cached` hides it from every listing, so
    it accumulates where nobody will look. The old fixed name was self-cleaning
    by accident and this is the cost of having given that up.

    Here because this runs once per `record`, under the lock, and already walks
    the directory. :data:`_TMP_TTL` is well past the longest a live `_replace`
    can take, which is one buffered write of a document.
    """
    cutoff = time.time() - _TMP_TTL
    for path in cache_dir().glob(f"{_TMP}*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _files(item: dict) -> list[Path]:
    """Every file on disk that one record of :func:`_records` stands for.

    A PDF and the text read out of it are one document and one record — the
    sidecar's, see :func:`_records` — so dropping the record has to drop the
    bytes with it. Left behind, the PDF is a file no listing shows, no search
    reaches and nothing will ever delete, and it is the larger half of the two.
    """
    paths = [cache_dir() / item["name"]]
    if item.get("pdf"):
        paths.append(cache_dir() / item["pdf"])
    return paths


def _weight(item: dict) -> int:
    """What one record costs on disk, the PDF behind a sidecar included.

    Not `item["size"]`, which is the sidecar's alone: a 60-page invoice is
    three hundred bytes of extracted text beside two megabytes of PDF, and a
    byte cap counting only the text would never fire on the megabytes.
    """
    total = 0
    for path in _files(item):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _evict() -> list[dict]:
    """Drop the oldest documents until both caps hold. Returns what is left.

    Call under :func:`writing`. Returns the surviving records in
    :func:`_records` order so :func:`_write_index` does not walk the directory
    a second time to find out what this one just changed.

    Oldest first by the mtime :func:`_records` already sorts on, and not by the
    `fetched` header the issue proposed. The two agree on every file this
    module wrote a header for, and `fetched` is *missing* on the ones it could
    not — a spreadsheet download, an image, a PDF too damaged to open — which
    are exactly the files an order keyed on it would either strand at the front
    forever or throw out first, depending on how the absent key happened to
    sort. The mtime is on every file there is.

    The newest document is never evicted, whatever the caps say. It is the one
    somebody is waiting on: a single export larger than :data:`CACHE_MAX_BYTES`
    would otherwise be fetched and deleted inside the one command, and the
    answer would be that there is nothing in the cache.

    A file another command is halfway through annotating can be taken here.
    That command's :func:`_still_ours` then fails and it reports one document
    fewer, which is what it already does for a file a second command replaced.
    """
    records = _records()
    keep, held = len(records), 0
    for index, item in enumerate(records):
        held += _weight(item)
        if index and (index >= CACHE_MAX_FILES or held > CACHE_MAX_BYTES):
            keep = index
            break
    for item in records[keep:]:
        for path in _files(item):
            try:
                path.unlink()
            except OSError:
                continue
    if keep < len(records):
        logger.info("gws cache: %d over the cap, evicted %s",
                    len(records) - keep,
                    ", ".join(item["name"] for item in records[keep:]))
    return records[:keep]


def _write_index(records: list[dict]) -> None:
    rows = []
    for item in records:
        when = datetime.fromtimestamp(item["mtime"]).astimezone()
        rows.append(
            f"| [{item['name']}]({item['name']}) "
            f"| {item.get('title') or '—'} "
            f"| `{item.get('file_id') or '—'}` "
            f"| {when:%Y-%m-%d %H:%M} | {_size(item['size'])} "
            f"| {item.get('summary') or '—'} |"
        )

    body = (
        "\n".join([
            "| File | Title | File id | Fetched | Size | Summary |",
            "|---|---|---|---|---|---|",
            *rows,
        ])
        if rows
        else "Nothing cached yet."
    )
    _replace(
        cache_dir() / INDEX,
        "# gws cache\n\n"
        "Documents the live agent has read out of Google Workspace, one file "
        "per fetch. Written by `ai_terminal.live.tools.gws`; this index is "
        "rebuilt whenever a command writes a file here, so edits to it do not "
        "survive. Delete a file to drop it from the cache; the oldest go on "
        f"their own once there are more than {CACHE_MAX_FILES} of them or "
        f"they run past {_size(CACHE_MAX_BYTES)}.\n\n" + body + "\n",
    )


def written_by(before: dict[str, float]) -> dict[str, float]:
    """What the command wrote, against the snapshot taken before it ran.

    Call this under :func:`writing`, still holding it from that snapshot.

    The mtimes come back with the names because they are the evidence, not a
    detail of how the diff was computed. This mtime is the one that decided
    this file belongs to this command, and it was read while the lock was held.
    :func:`_annotate` runs later and outside the lock and has to know whether
    the body in front of it is still that one; its own read cannot answer that,
    because a second command's write may already have landed before it.
    """
    return {
        name: mtime for name, mtime in sorted(snapshot().items())
        if before.get(name) != mtime
    }


def record(written: dict[str, float],
           command: str) -> tuple[list[str], list[tuple]]:
    """Header and index the files a command wrote.

    Returns the names it kept, and the summaries it did not write. The second
    of those goes to :func:`finish`, which the caller runs after the answer has
    gone out — see :func:`_annotate` for why the summary is not wanted here and
    :func:`~_gws_run.gws` for why the caller has to be the one to start it.

    `written` is :func:`written_by`'s answer, names and the mtimes the diff
    saw. A network call per file, so this runs after :func:`writing` has been
    released — see there for why that is safe, and :func:`_annotate` for what
    carrying the mtimes buys.

    The diff is what the command *appeared* to write; the answer is what it
    turned out to own. A file a second command replaced during those network
    calls is in `written` and is not in this, and telling the sub-agent it has
    a document that is now somebody else's copy is the same wrong answer the
    header check exists to stop, arriving one layer up.

    Owning it and having a header on it are different things — a spreadsheet
    download is kept and named here and never headered — and one file written
    can be two files named, because a PDF gets a text copy beside it. See
    :func:`_annotate`.
    """
    if not written:
        return [], []
    now = datetime.now().astimezone()
    later: list[tuple] = []
    # `_annotate` and the index write together, because what makes this worth
    # measuring is that it happens *inside* the fetch: everything counted here
    # is time the person spends waiting for work that only speeds up a later
    # question. What is left in it after #32 is one `drive.meta` and two local
    # writes; the model call that used to dominate it is now `summarize.bg`.
    with _timing.step("record", files=len(written)):
        kept = [named for name, seen in written.items()
                for named in _annotate(cache_dir() / name, command, now, seen,
                                       later)]
        write_index()
    # After `_annotate` and not before it: the header it just wrote is where
    # the title and the mime type are, and the command's own stdout is the
    # document rather than anything describing it.
    for name in kept:
        _sources().cached(_read_header(cache_dir() / name))
    if kept:
        logger.info("gws cache: wrote %s", ", ".join(_sized(name) for name in kept))
    return kept, later


def _row(item: dict) -> str:
    """One cached document, as the sub-agent sees it."""
    when = datetime.fromtimestamp(item["mtime"])
    head = f"- `{item['name']}` — {when:%Y-%m-%d %H:%M}"
    if item.get("title"):
        head += f" — {item['title']}"
    if item.get("file_id"):
        head += f" (id `{item['file_id']}`)"
    return f"{head}\n  {item['summary']}" if item.get("summary") else head


def listing(limit: int = LISTING_LIMIT) -> str:
    """The recent part of the cache, for the sub-agent's prompt.

    In the instruction rather than behind a tool: the whole point is to stop a
    document being fetched twice, and a round trip spent asking what is cached
    costs about what the second fetch would have. Only the recent part, because
    the summaries that make a match possible are also what makes the list too
    heavy to repeat on every turn — `search_cache` covers the tail.
    """
    with _timing.step("listing"):
        records = _records()
        if not records:
            return EMPTY
        rows = [_row(item) for item in records[:limit]]
        if len(records) > limit:
            rows.append(f"- …and {len(records) - limit} older — {TAIL}")
        return "\n".join(rows)


def _text(name: str) -> str:
    """A cached file's text, or "" for one that is not text at all."""
    try:
        return (cache_dir() / name).read_text(encoding="utf-8")[:MAX_READ]
    except (OSError, UnicodeDecodeError):
        return ""


async def search_cache(query: str) -> dict:
    """Look through the text of every cached document.

    Searches what the summaries above leave out: the documents themselves, and
    the older ones the listing does not show.

    Args:
        query: What you are looking for — a topic, a title, a Drive file id, a
            place or a date that would appear in the document. An empty query
            returns everything.

    Returns:
        A dict with the matching documents, most relevant first, each with the
        filename to pass to `read_cache`.
    """
    with _timing.step("search") as timed:
        records = _scored(query)
        timed["files"] = len(records)

    # The length and not the text. This is INFO and it sits above the empty
    # return below, so a hit and a miss both write it — and `query` is whatever
    # the household asked to find in their own mail, calendar or Drive, going
    # into a journal kept for a month that *Forget* does not reach. The two
    # counts are what the line was ever read for: whether anything was asked
    # and whether anything came back.
    logger.info("gws cache: search of %d chars -> %d", len(query), len(records))
    if not records:
        return {"query": query, "found": "Nothing in the cache matches."}
    return {"query": query, "found": "\n".join(_row(item) for item in records[:10])}


def _scored(query: str) -> list[dict]:
    """Cached documents that match, most relevant first."""
    records = _records()
    terms = _match.terms(query)
    if terms:
        scored = []
        for item in records:
            index = " ".join([
                item["name"], item.get("title", ""), item.get("summary", ""),
            ]).lower()
            # The body is searched as well as the summary, and not only for the
            # detail three sentences had to leave out: summaries are written in
            # English whatever the document is in, so a Japanese question has
            # nothing but the title and the document itself to match against.
            body = _text(item["name"]).lower()
            hits = sum(2 * (term in index) + (term in body) for term in terms)
            # A file id is matched whole, not in pieces: half of one id looking
            # like half of another is not a match.
            if query.strip() and query.strip() == item.get("file_id"):
                hits += 2 * len(terms) + 1
            if hits:
                scored.append((hits, item))
        records = [item for _, item in sorted(scored, key=lambda s: -s[0])]
    return records


# How many words a phrase needs before hearing it counts as naming a document.
# One is a coincidence: `download.csv` and `paper-notes.md` reduce to "download"
# and "notes", and both of those turn up in questions about something else.
NAME_WORDS = 2


def _spoken(text: str) -> str:
    """`text` as the words in it, casefolded, one space apart.

    Three kinds of string have to be compared here and no two arrive alike: a
    filename is hyphenated and has an extension, a Drive title has punctuation
    and capitals, and a request is prose. Flattening all three the same way is
    what lets `paper-notes.md` be looked for inside "in their paper notes
    document in Drive".
    """
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def _names(item: dict) -> list[str]:
    """The phrases that would count as naming this document out loud.

    Its filename without the extension, and its Drive title. Both, because
    neither is reliable on its own: the titles in this cache run from "LLM" to
    a 95-character slide deck name, while the filenames were written by the
    model that fetched them and describe what is in them.

    For a PDF's sidecar that is the *PDF's* name, not the sidecar's:
    `zipair-invoice.pdf.md` has a stem of `zipair-invoice.pdf`, and `_spoken`
    turns the extension into a word, so the phrase to match became "zipair
    invoice pdf" and nobody says that.
    """
    stem = Path(item.get("pdf") or item["name"]).stem
    spoken = [_spoken(stem), _spoken(item.get("title", ""))]
    return [phrase for phrase in spoken
            if len(phrase.split()) >= NAME_WORDS]


def named(request: str) -> str | None:
    """The one cached document this request names, if it names one.

    A phrase gate, not a threshold. `_scored` computes a number, and that number
    cannot carry this: it counts substring hits over terms `_match.terms` does
    not stopword-filter, so it tracks how long the question and the document are
    rather than what either is about. Measured over the six questions in
    `tools/latency_report.py` and nine that name no cached document, the two
    score distributions overlap completely — a Gmail question outscores three of
    the six true hits, and the highest-scoring question in the set is one whose
    document is not cached at all. Any cutoff picks which way to be wrong.

    Hearing the document's own name is a different signal. It is a narrow one:
    "what does it say about token compression?" names nothing, and neither does
    either Japanese question in that set. That is the trade this makes. A miss
    costs exactly what happens today — the sub-agent spends a turn picking the
    file — while a false positive answers a different document fluently, and
    only one of those two is worth guarding against.

    None when nothing matches, and None when more than one record does. Two
    matches is `us-trip-itinerary.md` next to a `trip-itinerary.md`, where the
    longer name contains the shorter, and choosing between them is the judgement
    this function exists to avoid making.
    """
    # Padded, so a phrase has to fall on word boundaries: "us trip" is inside
    # "bus trip" as a substring and is not in it as words.
    heard = f" {_spoken(request)} "
    hits = [item for item in _records()
            if any(f" {phrase} " in heard for phrase in _names(item))]
    if len(hits) != 1:
        return None
    # A PDF is 13 MB of bytes that `_text` reads as nothing, and it is in the
    # listing under its filename alone. Naming it resolves to an empty document,
    # which is worse than not resolving: the sub-agent would be told the thing
    # it needs is already in front of it.
    return hits[0]["name"] if _text(hits[0]["name"]) else None


def _is_stale(header: dict) -> bool:
    """Has the Drive original been edited since this copy was recorded?

    Since it was *recorded*, which is a little later than when it was taken.
    `modified` is read by :func:`_annotate` after the export has already
    written the bytes, so an edit landing in between is written into the header
    as this copy's own — a window neither this check nor any version of it has
    ever closed.

    Within one question this is now answered from :func:`remembering`'s memo:
    the reading `_annotate` took is the reading this compares against, so the
    answer is fresh by construction and no second subprocess is run. What that
    gives up is the few seconds between the two, in which somebody else editing
    the document used to be caught. It costs 0.58 s of every cold question to
    catch, the window either side of it is open regardless, and the next
    question opens a new memo and reports the edit correctly. See #32.

    Only answerable for a copy that recorded which document it came from and
    when that document was last touched; anything else is served as it is,
    which is what the cache did for every file before this check existed.

    Unreachable Drive counts as fresh. Refusing to answer from a good copy
    because the network hiccuped is the worse of the two failures.
    """
    file_id, modified = header.get("file_id"), header.get("modified")
    if not file_id or not modified:
        return False
    current = _drive_meta(file_id).get("modifiedTime")
    return bool(current) and current != modified


async def read_cache(name: str) -> dict:
    """Read a document the cache already holds, instead of fetching it again.

    Checks with Drive first that the original has not been edited since. When
    it has, nothing is returned but the command that made the copy, so it can
    be run again.

    Args:
        name: The filename as it appears in the cached documents list, e.g.
            `trip-itinerary.md`.

    Returns:
        A dict with the file's text, or an error when there is no such file or
        the copy is out of date.
    """
    path = cache_dir() / Path(name).name
    # The step covers the freshness check as well as the read, and the two are
    # not the same order of magnitude: the read is a file off the SD card and
    # the check is a `gws` subprocess, logged under `drive.meta` inside this.
    with _timing.step("read"):
        # Before the read, not inside its `except`. Whether the bytes decode is
        # not the question and never was: a PDF written without the binary
        # comment on line two is valid UTF-8 — which is why :func:`_annotate`
        # dispatches on the magic bytes and says so at `:1032` — so `read_text`
        # succeeds on one and the sub-agent is handed `%PDF-1.4\n1 0 obj\n<<
        # /Type /Catalog` as the document's text, in full confidence and with
        # `paper.pdf.md` sitting beside it holding the answer. Tested on the
        # decode, that shape returned the source; tested here, the copy answers
        # for the bytes whether or not the bytes happen to be readable.
        #
        # The same `pdf:` header test :func:`_drop_sidecar` uses, so both paths
        # agree on what "this PDF's copy" means. Without it any `.md` at that
        # name answers: `_cache_name` rewrites only :data:`TEXT_SUFFIXES`, so a
        # Doc exported with `-o paper.pdf.md` lands on exactly the sidecar's
        # name, and the wrong document's text would go out under the asked-for
        # `name`. The magic bytes will not do instead — they say this file is a
        # PDF, not that the `.md` beside it came out of *this* one.
        #
        # The PDF's own name is the one the sub-agent is most likely to try: it
        # is the name in the download command it just ran, and `gws.py:426`
        # promises that name works.
        side = path.with_name(PDF_TEXT.format(name=path.name))
        if _read_header(side).get("pdf") == path.name:
            path, name = side, side.name
        # Ask the bytes before asking for the text. Line two of a PDF is a
        # binary comment — four bytes over 127, which every real writer puts
        # there precisely so that anything treating the file as text gives up
        # early — so a decode-first read sends every PDF Drive actually
        # returns out through `Could not read it` below, and neither branch
        # further down is ever reached. `make_pdf` in `cache_check.py` says
        # the same thing in its own docstring, which is why its cases have to
        # pass `ascii_only=True` to get here at all.
        try:
            with path.open("rb") as handle:
                magic = handle.read(len(PDF_MAGIC)) == PDF_MAGIC
        except OSError:
            # Including the missing file: the read below raises the same way
            # and answers it, so there is one place that decides that.
            magic = False
        try:
            text = "" if magic else path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"name": name, "why": "missing",
                    "error": "No cached file by that name."}
        except (OSError, UnicodeDecodeError) as err:
            # Tried rather than tested for: this is a tool the model calls, so
            # a copy deleted between the check and the read has to come back as
            # an error, not as a traceback out of a tool. `UnicodeDecodeError`
            # as well as `OSError`, and both are reachable. The bytes of a
            # binary with no copy beside it land here — an `.xlsx`, an image —
            # and so does a copy that is corrupt past its own header:
            # `_cache_name("paper.pdf.txt")` is `paper.pdf.md`, so a binary
            # downloaded that way is at the sidecar's name and passes the test
            # above on a header that reads fine.
            # `why` on every error return, and it is the only part of one that
            # may be logged: `err` here carries the whole path off the SD card,
            # and the two PDF branches below interpolate a filename into their
            # advice. `gws.py:794` used to print that string. #234.
            return {"name": name, "why": "unreadable",
                    "error": f"Could not read it: {err}"}

        # The redirect above did not fire and the bytes decoded, so this is a
        # PDF being served as its own text. Whether the bytes decode is not the
        # question there either: the copy is the answer, and when the copy
        # cannot be claimed there is no answer, only source. Reached whenever
        # the sidecar's name is taken by something else — a Doc exported with
        # `-o memo.pdf.md` lands exactly there — or the copy was never written.
        #
        # Magic bytes are right *here* and wrong at the redirect, and the
        # difference is what is being asked. There the question is "did this
        # `.md` come out of this PDF", which the bytes cannot answer. Here it
        # is "are these bytes a PDF", the only thing they do answer.
        #
        # Two states arrive here and the advice differs. The copy was never
        # written, and fetching the PDF again writes it under the name below;
        # or another document holds that name, and :func:`_sidecar` refuses to
        # write over it, so a re-fetch to the same name would produce nothing
        # and naming the copy would name the stranger. The sub-agent would
        # then answer about a document nobody asked for, which is exactly the
        # failure this branch was added to stop, one file over.
        if magic:
            if _stranger(side, path.name):
                return {
                    "name": name,
                    "why": "pdf-name-taken",
                    "error": "That is the PDF itself, not its text, and no "
                             "copy was made of it: another document is "
                             f"already at `{side.name}`, the one name the "
                             "copy is looked for under. Fetch the PDF again "
                             "with `gws drive files get` under a different "
                             "`-o` name, and read the `.md` written beside "
                             "that one.",
                }
            return {
                "name": name,
                "why": "pdf-no-copy",
                "error": "That is the PDF itself, not its text — there is no "
                         "readable copy beside it. Fetch it again with `gws "
                         "drive files get`, which writes the text copy as "
                         f"`{PDF_TEXT.format(name=path.name)}`.",
            }

        header = _read_header(path)
        # Off the loop, because `_is_stale` ends in a `gws` subprocess: 0.55 s
        # median from a Mac on home Wi-Fi, and `_gws_cli.TIMEOUT` seconds if the
        # CLI wedges. This coroutine runs on the live server's loop, which is
        # also streaming audio to whoever asked the question, so the wait has to
        # happen on a thread the way `_gws_run.gws` and `_agenda.agenda` do it.
        if await asyncio.to_thread(_is_stale, header):
            # Sized, like the read below it. These two fire for the sub-agent's
            # own `read_cache` calls as well as for a resolve, so the name did
            # not have to be said out loud to get here — but it is a document
            # in the household's Drive either way, and the journal keeps it for
            # a month. #234, folded in from #226's map.
            logger.info("gws cache: %s is out of date", _sized(path.name))
            return {
                "name": path.name,
                "why": "stale",
                "error": "This copy is out of date — the document has been "
                         "edited since. Fetch it again with the command below; "
                         "writing to the same filename replaces the copy.",
                "source": header.get("source", ""),
            }

        # Here rather than at the top of the function: above this line are a
        # guessed filename and a copy that has gone stale, both of which return
        # an error. Announcing before them says "found the document, and
        # reading it" out loud and on screen, and then hands back a failure.
        _progress.note("document", "Found the document, and reading it.")
        logger.info("gws cache: read %s", _sized(path.name))
        # Below the errors for the same reason the note is: the card should
        # name the document that was read, not one that was asked for and
        # refused. `header` is already in hand from the freshness check.
        _sources().cached(header)
        return {
            "name": path.name,
            "text": text[:MAX_READ],
            "truncated": len(text) > MAX_READ,
        }
