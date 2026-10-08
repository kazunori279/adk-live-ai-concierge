"""What a Workspace answer was actually read out of.

The sub-agent answers in prose. It is told to, and told twice over: "Reply in
concise Markdown" and "Never dump raw JSON, IDs or URLs into the prose" both
sit in :data:`~.gws.AFTER_DOCUMENTS`. So by the time an answer reaches
:func:`~..screen.push` it is one Markdown string, and which file or thread it
came out of is not in it and cannot be recovered from it.

Asking the model for the provenance instead was the other way to do this, and
it is the wrong one twice. The instruction it would have to contradict is there
for a reason — an id read out loud on a kitchen speaker is noise — and worse, a
source line the model wrote is a source line the model can invent. The whole
value of the line is that it can be trusted; one hallucinated filename under a
plausible summary is a worse card than no line at all, because it is the same
card the owner already has plus a false claim.

So this watches instead, on the two paths an answer can arrive by. A cold one
went through :func:`~._gws_run.gws` as an argv and came back as JSON on stdout,
and :func:`record` reads that as it goes past; a warm one was read out of the
cache and ran no command at all, and :func:`cached` reads the header the copy
carries. Either way it is a fact about what the process did rather than a claim
about it, which is the whole point. :func:`collected` says what the card may
name.

Three answers, not two, and the third is the point:

- a list of sources — these were read;
- ``[]`` — a file or a thread was looked for and none could be named, which the
  card says out loud rather than leaving a gap that looks like a card whose
  source row was never written;
- ``None`` — nothing that has files or threads in it was touched at all. A
  calendar question has no document behind it, and a terminal that reports "no
  source" for every "what's on today?" has taught the household to ignore the
  line by the end of the week.

The ledger is a ContextVar for the same reason :func:`~._gws_run.budget` and
:func:`~.gws_cache.remembering` are: nothing between the tool and the CLI call
can carry an argument, because ADK calls a tool with the model's arguments and
nothing else. `asyncio.to_thread` copies the context and the ledger is mutable,
so :func:`record` reaches it from the worker thread `_run_sync` runs on.
"""

import contextlib
import contextvars
import json
import logging

from . import gws_cache

logger = logging.getLogger(__name__)

# How many sources one card shows. Six pills is already two rows on the panel,
# and a card that needs a third row to say where it got something is no longer
# answering the question it was asked.
MAX_SOURCES = 6

# And how long a search can be and still be its own answer. A Drive search that
# matched three files is three files the answer weighs up; one that matched
# forty is a haystack, and naming six of the forty tells the person the answer
# came from those six when it did not. A long listing is remembered anyway —
# see :class:`Ledger` — because the fetch that follows it is what says which
# one mattered, and the name is only in the listing.
LIST_SOURCES = 3

# How much of one string goes on the wire. The card clips harder than this for
# the panel; this is only here so a pathological subject cannot make the frame
# the largest thing on the socket.
MAX_TEXT = 200

# Google's own mime types for the three editor formats, which are three
# different objects and want three different glyphs. Everything else Drive
# holds is a file, which is all the card can usefully say about it.
_KINDS = {
    "application/vnd.google-apps.document": "doc",
    "application/vnd.google-apps.spreadsheet": "sheet",
    "application/vnd.google-apps.presentation": "slides",
}

# A folder is a place to look, not a thing an answer comes out of.
_FOLDER = "application/vnd.google-apps.folder"

# The services whose objects a card can name, and which of the two shapes each
# one is. Calendar, Keep and Tasks are deliberately absent: an event is not a
# source, it is the answer, and a calendar card is its own piece of work.
_FILES = frozenset(("drive", "docs", "sheets", "slides"))
_MAIL = "gmail"

# Where a list of objects hides in a JSON response, by the name each service
# gives it. Read in order and the first hit wins; no response carries two.
_LISTS = ("files", "messages", "threads", "documents", "items")

# Where an id hides, on the command line and inside `--params`. Both spellings,
# because the `+read` shortcuts take `--id` on the command line and the generic
# subcommands take it in the blob — the same request written two ways.
_ID_FLAGS = ("id", "message-id")
_ID_KEYS = (*gws_cache.ID_KEYS, "id", "messageId")

# What a Drive object calls itself, in the order worth trying.
_FILE_IDS = ("id", "fileId", "documentId", "spreadsheetId", "presentationId")
_FILE_NAMES = ("name", "title")
_FILE_WHEN = ("modifiedTime", "modifiedDate", "createdTime")

# And a Gmail one. `threadId` last: it identifies the conversation rather than
# the message, which is the coarser and less useful of the two here.
_MAIL_IDS = ("id", "messageId", "threadId")
_MAIL_FROM = ("from", "sender")


class Ledger:
    """Everything one question saw, and what it went back for.

    Two collections and not one, because they answer different questions.
    :attr:`seen` is every nameable object that went past, keyed by its id, and
    it is the only place a name exists — a `files export` prints the document,
    not its title, so the name of the file that was exported is in the listing
    that found it and nowhere else. :attr:`fetched` is the ids the question
    named on a command line, which is the record of what it went and got.

    So the answer to "what is this card's source" is `fetched` resolved through
    `seen`, and the listing is a lookup table rather than a claim.
    """

    def __init__(self) -> None:
        self.seen: dict[str, dict] = {}
        self.order: list[str] = []
        self.fetched: list[str] = []
        # Whether anything with files or threads in it ran at all. This is what
        # tells "we looked and could name nothing" from "there was nothing of
        # this kind to look for" — see the module docstring.
        self.touched = False

    def add(self, source: dict, broad: bool) -> None:
        """Remember one object, the first sighting of it winning.

        First and not last, because the first sighting is the search result and
        the later ones are projections of it: `messages get` asked with a
        `fields` mask returns a thinner copy of a message `+triage` already
        described in full, and overwriting would trade a subject for nothing.
        """
        key = source.pop("id")
        if key in self.seen:
            return
        source["broad"] = broad
        self.seen[key] = source
        self.order.append(key)

    def fetch(self, key: str) -> None:
        if key and key not in self.fetched:
            self.fetched.append(key)


_LEDGER: contextvars.ContextVar[Ledger | None] = contextvars.ContextVar(
    "ai_terminal_gws_sources", default=None
)


@contextlib.contextmanager
def collecting():
    """Give everything started inside this block one shared ledger.

    Yields it, and the caller has to keep it: the block closes around
    ``create_task`` the way :func:`~._gws_run.budget` does, so the task keeps
    the binding through its context snapshot and the caller does not. The frame
    is built after the answer arrives, by which time :data:`_LEDGER` is back to
    what it was — so :func:`collected` takes the ledger rather than reading it.
    """
    ledger = Ledger()
    token = _LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _LEDGER.reset(token)


def _flag(argv: list[str], name: str) -> str:
    """The value of `--name`, written either way, or "" when it is absent.

    A second copy of :func:`~._gws_run._flag`, deliberately. That one is half
    of a budget key and this one is half of a source line, and a shared helper
    would be one function two features have a reason to change.
    """
    for i, token in enumerate(argv):
        if token == f"--{name}" and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(f"--{name}="):
            return token.split("=", 1)[1]
    return ""


def _named(argv: list[str]) -> str:
    """The one object this command line names, or "" when it names none."""
    for name in _ID_FLAGS:
        value = _flag(argv, name)
        if value:
            return value
    fields = gws_cache.params(argv)
    for key in _ID_KEYS:
        value = fields.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _objects(stdout: str) -> tuple[list[dict], bool]:
    """The candidate objects in one response, and whether they came in a list.

    The distinction is the whole of :data:`LIST_SOURCES`: an object returned on
    its own was asked for by name, and one in a list turned up in a search.
    Output that is not JSON at all — a table, a document, an empty string —
    yields nothing, which is right. A command whose answer nobody parsed is a
    command this cannot honestly attribute anything to.

    The parse starts at the first brace or bracket rather than at the first
    byte, for the reason `_gws_cli.run_json` gives: the CLI writes a line about
    its keyring backend ahead of the payload, and a warning has been seen on
    stdout too. `_run_sync` hands this the same raw stdout it hands that one,
    so parsing from the first byte attributes nothing on exactly the machines
    that print the line — and attributes it silently, because unparseable
    output and a genuinely sourceless answer are the same `[]` here. Whichever
    of the two characters comes first, because unlike `run_json` this takes a
    top-level list as readily as an object.
    """
    opens = [at for at in (stdout.find("{"), stdout.find("[")) if at >= 0]
    if not opens:
        return [], False
    try:
        data = json.loads(stdout[min(opens):])
    except ValueError:
        return [], False
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)], True
    if not isinstance(data, dict):
        return [], False
    for key in _LISTS:
        found = data.get(key)
        if isinstance(found, list):
            return [item for item in found if isinstance(item, dict)], True
    return [data], False


def _text(value) -> str:
    """One field, as a bounded string, or "" for anything that is not one."""
    return value.strip()[:MAX_TEXT] if isinstance(value, str) else ""


def _first(obj: dict, keys) -> str:
    """The first of `keys` this object has a non-empty string under."""
    return next((found for key in keys if (found := _text(obj.get(key)))), "")


def _day(value: str) -> str:
    """The date out of an RFC 3339 timestamp, or "" if it is not one.

    The date and not the time, and printed as the API spells it rather than
    turned into "3 days ago". A relative phrase needs a clock and a language,
    and every other card here draws its data as the server sent it and reserves
    the translating for its own chrome — `wxHours` prints `h.time` untouched.
    """
    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]
    return ""


def _file(obj: dict) -> dict | None:
    """One Drive object as a source, or None if it is not one to name."""
    mime = _text(obj.get("mimeType"))
    name = _first(obj, _FILE_NAMES)
    if not name or mime == _FOLDER:
        return None
    key = _first(obj, _FILE_IDS)
    return {
        "id": key or name,
        "kind": _KINDS.get(mime, "file"),
        "name": name,
        "detail": _day(_first(obj, _FILE_WHEN)),
    }


def _headers(obj: dict) -> dict:
    """A Gmail `payload.headers` list, as a dict keyed in lower case."""
    payload = obj.get("payload")
    rows = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = obj.get("headers")
    if not isinstance(rows, list):
        return {}
    return {name.lower(): _text(row.get("value"))
            for row in rows if isinstance(row, dict)
            and (name := _text(row.get("name")))}


def _mail(obj: dict) -> dict | None:
    """One Gmail message as a source, or None if it is not one to name.

    A sender is required and a subject is not, which is the way round it looks
    least like. The sender is the half of a thread that is safe to put on a
    wall and readable across a room; the subject is the most disclosive string
    this whole system handles, and on its own it identifies the thread to
    nobody but the person who owns it — so a message that came back with a
    subject and no sender is not named. That case is `users messages list`,
    whose whole response is ids: a command that found messages without reading
    any of them, which is exactly what should contribute no source.
    """
    flat = {key.lower(): value for key, value in obj.items()}
    head = _headers(obj)
    who = _first(flat, _MAIL_FROM) or head.get("from", "")
    if not who:
        return None
    return {
        "id": _first(obj, _MAIL_IDS) or who,
        "kind": "mail",
        "name": who,
        "detail": _text(flat.get("subject")) or head.get("subject", ""),
    }


def record(argv: list[str], stdout: str, ok: bool = True) -> None:
    """Note what one `gws` command named and what it brought back.

    `ok` is whether the command exited zero. A failed one still counts as
    having looked — that is what `touched` is for — but the id on its
    command line is not a source, because `fetched` resolved through
    `seen` would then name a file out of the listing that *preceded* the
    failure and present it as what the answer came out of.

    Called from the worker thread the subprocess ran on, and a no-op outside a
    :func:`collecting` block — the sub-agent's tools are reachable from a test
    and from `tools/gws_bench.py`, and neither has a card to draw on.

    Never raises. This is bookkeeping hung off the side of a command that has
    already succeeded, and a malformed response is the ordinary case rather
    than the exotic one: an exception here would take out the answer itself.
    """
    ledger = _LEDGER.get()
    service = argv[0] if argv else ""
    if ledger is None or not (service in _FILES or service == _MAIL):
        return
    ledger.touched = True
    try:
        # Only a command that came back may claim an id. An export that
        # exits non-zero after a search has looked and read nothing, and
        # promoting its id would put the one file the answer demonstrably
        # did not come out of on the card — and displace the honest `[]`
        # while doing it, since a fetch outranks the listing.
        if ok:
            ledger.fetch(_named(argv))
        objects, listed = _objects(stdout)
        broad = listed and len(objects) > LIST_SOURCES
        read = _mail if service == _MAIL else _file
        for obj in objects[:MAX_SOURCES * 4]:
            source = read(obj)
            if source is not None:
                ledger.add(source, broad)
    except Exception:  # pylint: disable=broad-except
        logger.debug("gws sources: %s went unread", service, exc_info=True)


def cached(header: dict) -> None:
    """Note a Drive document that was read out of the cache, not off the CLI.

    :func:`record` watches `gws` argv and stdout, and the warm path has
    neither. `_prefetch` splices a cached document into the sub-agent's first
    turn and `read_cache` is one of its four tools, and both answer out of the
    copy on the SD card — no subprocess, so nothing goes past `_run_sync` and
    the ledger stays empty. That is the frequent path rather than the exotic
    one: `_prefetch` exists because a question naming a document already in the
    cache is the common shape, and it is also the quiet failure, because an
    empty ledger reads as a question that touched no files at all.

    Nothing here is parsed. Every cached file carries the header
    :func:`~.gws_cache._meta_fields` wrote out of Drive's own answer, and it
    holds exactly the four fields a source line needs. That also beats reading
    the cold path's stdout, which is the document itself: a `files export`
    prints the text and never the title.

    A cached document is a fetched document rather than a listing — it was
    named, fetched and read — so it goes into both halves of the ledger.
    """
    ledger = _LEDGER.get()
    if ledger is None:
        return
    # Before the parse, and unconditionally: a document was read either way,
    # and a header too thin to name it is the "looked and could name nothing"
    # answer rather than the "nothing of the kind was touched" one.
    ledger.touched = True
    try:
        source = _file({
            "id": header.get("file_id", ""),
            "name": header.get("title", ""),
            "mimeType": header.get("mime_type", ""),
            "modifiedTime": header.get("modified", ""),
        })
        if source is None:
            return
        # Read before `add`, which takes the id out of the dict it stores.
        key = source["id"]
        ledger.add(source, False)
        ledger.fetch(key)
    except Exception:  # pylint: disable=broad-except
        logger.debug("gws sources: a cached header went unread", exc_info=True)


def collected(ledger: Ledger | None) -> list[dict] | None:
    """What the card may name, or None when there was nothing of the kind.

    The ids the question fetched come first and alone. Those are the objects it
    went back for by name, and once there is one of them the listing that found
    it is scaffolding rather than provenance — the answer came out of the file
    that was exported, not out of the nine others whose names went past on the
    way. Only with nothing fetched does the search itself stand in, and then
    only a search short enough to be its own answer; see :data:`LIST_SOURCES`.
    """
    if ledger is None or not ledger.touched:
        return None
    hits = [ledger.seen[key] for key in ledger.fetched if key in ledger.seen]
    if not hits:
        hits = [ledger.seen[key] for key in ledger.order
                if not ledger.seen[key]["broad"]]
    return [{"kind": hit["kind"], "name": hit["name"], "detail": hit["detail"]}
            for hit in hits[:MAX_SOURCES]]
