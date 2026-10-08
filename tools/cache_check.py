"""Check that a cached document keeps its own contents and its own header.

Everything here is a way of getting one of those wrong, and all of them are
quiet and permanent: nothing shows in the answer to either question, and the
file left on disk is what `listing()` shows the sub-agent and what it matches
the next request against.

- **Two commands meeting on one name.** `record` annotates one file at a time
  and each file costs two network calls, so a two-document export holds a
  window open for seconds. `gws` names a download it was given no name for
  `download.html`, so two of them meeting is the ordinary case, not a corner.
- **Two commands not meeting on a name at all**, when `writing()` is not
  holding them apart: the diff windows overlap and the slower command claims
  the faster one's file.
- **A document that opens with `---`.** Jekyll front matter does, and so does a
  Doc whose first line is a horizontal rule. Taking everything down to the next
  `---` for a header deleted real text.
- **A PDF.** The bytes cache fine and nothing can read them back, so a number
  printed on an invoice was unanswerable at any budget. It gets a text copy
  beside it now, and the two have to stay one document: same identity in the
  header, no header in the bytes, and the copy has to be reached by whichever
  of the two the sub-agent names.
- **A cache with no bound on it.** Every read here is linear in the number of
  cached files, so an uncapped directory is a turn that gets slower every week
  and an SD card that gets written for nothing. What the cap must not do is
  take the document somebody is waiting on, or take half of a PDF and its
  text copy and leave bytes nothing lists and nothing will ever delete.

And one that is not about the file on disk at all: **`read_cache` waiting for
Drive on the event loop.** Its freshness check ends in a `gws` subprocess, and
the loop it runs on is the one streaming audio to the person who asked. A
blocking call inside a coroutine raises nothing and logs nothing, so it is
checked the only way it shows: a heartbeat running beside it, counted.

And two that are about the check scripts rather than the cache, and are here
because this is the script that walks them with `ast` and because `uv run
python tools/cache_check.py` is a thing the reviewer may run:
:func:`guard_cases`, which is #233's, and :func:`emitter_cases`, which is
#250's — `tools/leak_check.py` drives real entry points and so is never run
in a review, and its line-level ledger would otherwise be gated only from
inside a script nobody may execute.

The network calls are stubbed to sleep, so this runs against a temporary
directory in a few seconds and needs no account.

    uv run python tools/cache_check.py

Exits non-zero on the first case that does not hold.
"""

import ast
import asyncio
import base64
import fcntl
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ai_terminal.live.tools import (  # noqa: E402
    _gws_cli, _gws_run, _timing, gws, gws_cache,
)

# What a person said, in a shape no document, filename or Drive id could be
# mistaken for, so a case can ask the only question that matters about the
# bytes on the card: is it in there. Read back off the file rather than off the
# parsed header — a field the parser skips is still a field somebody can read.
SENTINEL = "ZZ-CLINIC-ABOUT-YUKI-ZZ"

# The instructed export, in full. Written out once rather than built, because
# what the idempotence case is about is a literal string surviving a round trip
# and a string assembled from the same parts would survive one by construction.
EXPORT = ('gws drive files export -o kept.md '
          '--params \'{"fileId": "ID-1", "mimeType": "text/markdown"}\'')

# What the build before #233 wrote into a header, verbatim. This is the string
# on every cached file the household already has.
OLD_STYLE = ('gws drive files list -o older.md --params '
             f'\'{{"q": "fullText contains \'"\'"\'{SENTINEL}\'"\'"\'"}}\'')

# `main` replaces `_drive_meta` with a stub that sleeps, which is the right
# thing for every case about locking and is the wrong thing for the cases about
# the memo: the memo lives *inside* `_drive_meta`, so a stub of it is a stub of
# the thing being checked. Those cases put this back and stub the subprocess
# underneath it instead.
REAL_DRIVE_META = gws_cache._drive_meta

# Long enough that the two commands genuinely overlap, short enough to wait
# for. This stands in for a Drive `files get` plus a summarisation.
SLOW = 0.4

# How often the heartbeat beside `read_cache` tries to run. Small against
# `SLOW`, so a loop that is free ticks tens of times and a loop that is blocked
# ticks once at most.
TICK = 0.01

# A hundredth of the real waits, for the one case that is about the waits
# themselves. The three constants are scaled together and none of them is typed
# in, because what that case checks is the ratio between them.
LOCK_SCALE = 0.01


# Counted rather than written down, so the closing line cannot drift from the
# number of cases the way a figure typed into a commit message does.
RAN = 0


def check(name: str, ok: bool, detail: str, got=None, want=None) -> None:
    global RAN
    RAN += 1
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        if want is not None:
            print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


def make_pdf(lines: list[str], ascii_only: bool = False) -> bytes:
    """A one-page PDF showing `lines`, or a page with nothing on it.

    Hand-rolled because nothing in the dependency tree writes a PDF — pypdf
    reads them and copies pages between them. Five objects and an xref table is
    the whole format at this size: a catalog, a page tree, the page, a font,
    and a content stream of `Tj` calls. No `lines` gives a valid page with an
    empty text layer, which is what a scan is as far as anything here can tell.

    Line two is the binary comment every real writer puts there — four bytes
    over 127, so that anything treating the file as text gives up early. That
    is what makes a downloaded PDF fail to decode, and `ascii_only` leaves it
    off: a PDF written without one decodes as UTF-8 perfectly, which is the
    case that made dispatching on a `UnicodeDecodeError` the wrong idea.
    """
    show = "".join(f"({t}) Tj T*\n" for t in lines)
    stream = f"BT /F1 12 Tf 14 TL 72 720 Td\n{show}ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    out = bytearray(b"%PDF-1.4\n" if ascii_only
                    else b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, start))
    return bytes(out)


def make_empty_pdf() -> bytes:
    """A valid PDF with no pages at all — a catalog and an empty page tree.

    pypdf opens it and reports zero pages, which is not the same thing as a
    scan: `NO_TEXT` says the pages are images, and there are none. A truncated
    file will not do here, because that raises instead and takes the earlier
    branch.
    """
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [] /Count 0 >>"]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, start))
    return bytes(out)


def annotate(path: Path, file_id: str = "ID-F") -> bool:
    """`_annotate` on one file, as `record` calls it: `seen` is its own mtime.

    Deferred summaries written before returning, so this stands for the whole
    of what annotating a file used to be. The cases that care where the two
    halves split call `_annotate` and `finish` themselves.
    """
    later: list[tuple] = []
    kept = gws_cache._annotate(
        path, f'gws drive files export --params \'{{"fileId": "{file_id}"}}\'',
        datetime.now().astimezone(), path.stat().st_mtime, later)
    gws_cache.finish(later)
    return kept


def record(written: dict[str, float], command: str) -> list[str]:
    """`record` and then its deferred half, the way `_gws_run.gws` runs them.

    In one call and on this thread rather than through `defer`: what the split
    is for is measured in the server, and what the cases below are about is
    that the same headers and the same index come out of it either way.
    """
    kept, later = gws_cache.record(written, command)
    gws_cache.finish(later)
    return kept


def export(names: list[str], file_id: str, body: str,
           work: float = 0.0) -> list[str]:
    """One gws command that writes `names`, cached the way `_gws_run` does it.

    The snapshot, the write and the diff under one `writing()`, and `record`
    outside it — which is the whole shape being checked. `work` is how long the
    command takes between the snapshot and the write, which is what makes two
    of these overlap when the lock is not holding them apart. Returns what
    `record` says the command ended up owning.
    """
    command = (f"gws drive files export {' '.join('-o ' + n for n in names)} "
               f'--params \'{{"fileId": "{file_id}"}}\'')
    with gws_cache.writing():
        before = gws_cache.snapshot()
        time.sleep(work)
        for name in names:
            (gws_cache.cache_dir() / name).write_text(
                f"{body} in {name}\n", encoding="utf-8")
        written = gws_cache.written_by(before)
    return record(written, command)


def hold_for(seconds: float) -> None:
    """Sit inside `writing()` for `seconds`, holding both locks."""
    with gws_cache.writing():
        time.sleep(seconds)


def queue_up(count: int, work: float,
             foreign: bool) -> tuple[list[tuple[int, int]], list]:
    """Start `count` threads at `writing()` at once and see who met whom.

    Returns the pairs that were inside the block at the same time — the whole
    question, since one thread inside it at a time is the invariant — and the
    timeline they came from, which is what makes a failure readable.

    The constants are scaled here rather than by the caller because they have
    to be back to their real values before `check` runs: a detail string
    reporting the scaled deadline is the point of scaling it.
    """
    real = (gws_cache.LOCAL_TIMEOUT, gws_cache.LOCK_TIMEOUT)
    gws_cache.LOCAL_TIMEOUT = real[0] * LOCK_SCALE
    gws_cache.LOCK_TIMEOUT = real[1] * LOCK_SCALE
    timeline: list[tuple[int, str, float]] = []
    # Appended to from every thread, and the two edges of one hold have to land
    # in the order they happened.
    edges = threading.Lock()
    started = time.monotonic()

    def hold(which: int) -> None:
        with gws_cache.writing():
            with edges:
                timeline.append((which, "in", time.monotonic() - started))
            time.sleep(work)
            with edges:
                timeline.append((which, "out", time.monotonic() - started))

    handle = None
    if foreign:
        handle = (gws_cache.cache_dir() / gws_cache._LOCK).open("w")
        fcntl.flock(handle, fcntl.LOCK_EX)
    try:
        threads = [threading.Thread(target=hold, args=(i,))
                   for i in range(1, count + 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        gws_cache.LOCAL_TIMEOUT, gws_cache.LOCK_TIMEOUT = real

    spans: dict[int, dict[str, float]] = {}
    for who, edge, at in timeline:
        spans.setdefault(who, {})[edge] = at
    overlaps = [(a, b) for a in spans for b in spans
                if a < b and spans[a]["in"] < spans[b]["out"]
                and spans[b]["in"] < spans[a]["out"]]
    return overlaps, timeline


class Said(logging.Handler):
    """Whatever the module logged inside the block, formatted."""

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level)
        self.lines: list[str] = []
        self.logger = logging.getLogger(gws_cache.__name__)

    def __enter__(self) -> "Said":
        self.was = self.logger.level, self.logger.propagate
        self.logger.setLevel(self.level)
        self.logger.propagate = False
        self.logger.addHandler(self)
        return self

    def __exit__(self, *_) -> None:
        self.logger.removeHandler(self)
        self.logger.level, self.logger.propagate = self.was

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class Steps(logging.Handler):
    """The step lines logged inside the block, parsed."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.seen: list[dict] = []
        self.logger = logging.getLogger(_timing.__name__)

    def __enter__(self) -> "Steps":
        self.was = self.logger.level, self.logger.propagate
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(self)
        return self

    def __exit__(self, *_) -> None:
        self.logger.removeHandler(self)
        self.logger.level, self.logger.propagate = self.was

    def emit(self, record: logging.LogRecord) -> None:
        self.seen.append(
            json.loads(record.getMessage().split(_timing.FIELD + " ", 1)[1]))

    def named(self, name: str) -> dict:
        return next(s for s in self.seen if s["step"] == name)


async def beside(name: str) -> tuple[dict, int]:
    """`read_cache`, and how many times a heartbeat got to run beside it.

    The heartbeat stands in for everything else the live server's loop is doing
    while somebody waits for an answer, the websocket streaming audio most of
    all. A blocking call inside a coroutine does not show up as an error
    anywhere; it shows up as the count below going to nothing.
    """
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(TICK)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    try:
        return await gws_cache.read_cache(name), ticks
    finally:
        beat.cancel()


class Journaled(logging.Handler):
    """What one module logged while this was installed, formatted.

    Formatted, not kept as records: the arguments reach the journal through
    `%s`, so a record still holds the live string and a case reading
    `record.args` would pass on a line that goes on to print the question.
    `history_check.Journaled` is the same class, for the same reason.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def journalled(source_name: str, run) -> str:
    """What one module logged while `run` ran, as the journal would hold it.

    The level is raised for the duration because a case that measured nothing
    because nothing was logged would pass on any code at all.
    """
    held = Journaled()
    source = logging.getLogger(source_name)
    was, quiet = source.level, source.propagate
    source.setLevel(logging.INFO)
    source.propagate = False
    source.addHandler(held)
    try:
        run()
    finally:
        source.removeHandler(held)
        source.setLevel(was)
        source.propagate = quiet
    assert held.lines, "nothing was logged, so nothing was measured"
    return "\n".join(held.lines)


# What a check script cannot call without first pointing `cache_dir` somewhere
# disposable. All of them delete or rewrite: `forget` unlinks every file under
# the directory, `migrate` rewrites the `source` line of every header,
# `api_forget` is the endpoint that calls the first, and `_lifespan` awaits the
# second on startup. A `with TestClient(...)` is the fifth way in — it runs the
# lifespan, which is the whole reason `screensaver_check.py` needs a stub —
# and it is matched on the `with` rather than on the call, because the same
# constructor without one runs no lifespan and is used that way twice here.
DESTRUCTIVE = frozenset(("api_forget", "forget", "migrate", "_lifespan"))


def _reaches(node: ast.AST) -> bool:
    """Whether this function can delete or rewrite what is in `cache_dir()`."""
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in DESTRUCTIVE):
            return True
        if isinstance(sub, (ast.With, ast.AsyncWith)) and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "TestClient"
                for item in sub.items):
            return True
    return False


def _enclosing(node: ast.AST) -> list[ast.AST]:
    """The functions `node` sits inside, innermost first."""
    out, here = [], getattr(node, "parent", None)
    while here is not None:
        if isinstance(here, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(here)
        here = getattr(here, "parent", None)
    return out


def guard_cases() -> None:
    print("\n== no check script deletes the developer's own cache ==")

    # #233 made three functions destructive, and `cache_dir()` resolves against
    # the working directory — for a sweep run from the repo root, that is the
    # developer's real `gws_cache/`, which is gitignored and so not
    # recoverable. Four call sites had to be stubbed and the patch stubbed
    # three. The fourth took out 18 documents on the first run, and nothing
    # said so: the script printed `all 240 cases hold` while the directory
    # emptied.
    #
    # So the rule is enumerated rather than listed. Every `*_check.py` is
    # parsed, and a function calling one of :data:`DESTRUCTIVE` has to have a
    # `cache_dir` stub in itself or in a function it is nested in. A new call
    # site lands red the day somebody writes it, which is the only version of
    # this that keeps working.
    #
    # A *sibling's* stub does not count, or one function's guard would excuse
    # the function beside it — which is the exact mistake, one file over.
    # Nesting does count: `wiring_check.py`'s `forget` helper is stubbed by the
    # function around it, and that is the right place for it.
    #
    # Matched on an attribute (`gws_cache.forget`, `main.api_forget`) and not a
    # bare name, so a local helper somebody happens to call `forget` is not the
    # subject. That is also why this cannot be a grep.
    bare: list[str] = []
    scripts = sorted(Path(__file__).parent.glob("*_check.py"))
    for script in scripts:
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.parent = parent  # type: ignore[attr-defined]

        stubbed = set()
        for node in ast.walk(tree):
            # `= lambda: ...` and not `= was_cache_dir`. Both assign the
            # attribute, and the second is the *restore* in the `finally` —
            # counting it made all four mutants below survive, because
            # deleting a stub left its own restore standing in for it.
            if (isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Lambda)
                    and any(isinstance(target, ast.Attribute)
                            and target.attr == "cache_dir"
                            for target in node.targets)):
                stubbed.update(map(id, _enclosing(node)[:1]))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _reaches(node):
                continue
            if not ({id(node), *map(id, _enclosing(node))} & stubbed):
                bare.append(f"{script.name}:{node.lineno} {node.name}")

    check("every check function that can empty the cache points it somewhere "
          "disposable first", not bare,
          f"{len(scripts)} scripts, {len(bare) or 'no'} unguarded", bare, [])


def _literals(script: Path) -> dict:
    """Every module-level `NAME = <literal>` in `script`, without importing it.

    Reading rather than importing, because `tools/leak_check.py`'s module body
    makes a temporary cache directory and imports half the tree on the way
    past. `ast.literal_eval` of the value and not of the file, so a name whose
    value is a call or a comprehension is skipped rather than fatal.
    """
    out: dict = {}
    for node in ast.parse(script.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        try:
            out[target.id] = ast.literal_eval(value)
        except ValueError:
            continue
    return out


def emitter_cases() -> None:
    print("\n== every line in src/ that can emit is still counted ==")

    # `tools/leak_check.py` asks whether every module that can emit was driven
    # or written off. Six rounds of the same bug — #206, #226, #231, #233,
    # #234, #248 — were a *line* inside a module that was already driven, so a
    # module-level manifest cannot see the seventh. #250. Its `SITES` ledger is
    # the line-level half, and this re-derives it.
    #
    # Here and not only there, because `leak_check.py` drives real entry points
    # and is on the reviewer's do-not-run list: a gate that lives only inside
    # it is a gate that never runs in the sweep the reviewer can actually
    # execute. This one is pure `ast` over `src/` and costs nothing.
    #
    # The rule is read out of `leak_check.py` rather than restated — `SINKS`,
    # `WRITES` and the walk all come from there — so widening what counts as an
    # emitter in one file cannot leave the two disagreeing about the number
    # they are both gating.
    leak = Path(__file__).resolve().parent / "leak_check.py"
    held = _literals(leak)
    for name in ("SINKS", "WRITES", "SITES", "UNDRIVEN"):
        check(f"leak_check.py still declares {name} as a plain literal",
              name in held, "read without importing it", sorted(held), name)

    sinks, writes = set(held["SINKS"]), set(held["WRITES"])
    src = Path(__file__).resolve().parent.parent / "src" / "ai_terminal"
    can: dict[str, int] = {}
    for path in sorted(src.rglob("*.py")):
        module = ".".join(
            ("ai_terminal",) + path.relative_to(src).with_suffix("").parts)
        module = module.removesuffix(".__init__")
        count = 0
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"),
                                       filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                count += 1
            elif (isinstance(node.func, ast.Attribute)
                    and node.func.attr in sinks | writes):
                count += 1
        if count:
            can[module] = count

    moved = sorted({name for name in set(can) | set(held["SITES"])
                    if can.get(name, 0) != held["SITES"].get(name, 0)})
    check("leak_check.py's SITES ledger still matches what src/ can emit",
          not moved,
          f"{sum(can.values())} calls across {len(can)} modules"
          if not moved
          else f"{len(moved)} moved — drive it in leak_check.py, or update "
               "SITES in the same commit",
          {name: can.get(name, 0) for name in moved},
          {name: held["SITES"].get(name, 0) for name in moved})

    # `manifest_cases`'s third case, hoisted so it runs somewhere runnable:
    # a module written off for not being driven, that no longer emits at all,
    # is an excuse outliving its subject.
    gone = sorted(set(held["UNDRIVEN"]) - set(can))
    check("and nothing is written off in UNDRIVEN that no longer emits",
          not gone, f"{len(held['UNDRIVEN'])} written off, all still real",
          gone, [])


def journal_cases() -> None:
    print("\n== and the log the *Forget* button cannot reach ==")

    # Not a real question: what these three lines used to print is whatever
    # somebody asked to find in their own mail, calendar or Drive, and it went
    # into a journal that is `Storage=persistent` for a month
    # (`deploy/50-ai-terminal-journal.conf`) and that `api_forget` does not
    # reach. #226, after #206 did the live runner's arguments and #231 did
    # `gws.py`'s.
    asked = "ZZQQ-SENTINEL-7391"

    # Both branches, because `gws_cache.py`'s line sits *above* the empty
    # return under it: a miss writes the query as readily as a hit does, and a
    # case that drove only the hit would pass on a fix that moved the line down
    # instead of sizing it.
    hit = [{"name": "note.md", "mtime": 0, "title": "", "summary": ""}]
    for found, what in ((hit, "found something"), ([], "found nothing")):
        was = gws_cache._scored
        gws_cache._scored = lambda query, found=found: found
        try:
            searched = journalled(
                "ai_terminal.live.tools.gws_cache",
                lambda: asyncio.run(gws_cache.search_cache(asked)))
        finally:
            gws_cache._scored = was
        check(f"what the household asked the cache to find does not reach "
              f"the log when it {what}",
              asked not in searched, searched)
        # Green before this and after it, and here because a line that said
        # nothing at all would pass the case above.
        check("only how long the asking was, and how much came back",
              f"{len(asked)} chars" in searched
              and f"-> {len(found)}" in searched, searched)

    class Answered:
        """`subprocess` for one call, answering as the CLI does."""

        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "{}", "")

    argv = ["drive", "files", "list", "--query", f"name contains '{asked}'"]
    command = "gws " + " ".join(argv)

    # The success branch, which is INFO: the command worked, and the whole
    # command line — `--query` payload included — used to be the message.
    with tempfile.TemporaryDirectory() as scratch:
        was_dir, was_sub = gws_cache.cache_dir, _gws_run.subprocess
        gws_cache.cache_dir = lambda: Path(scratch)
        _gws_run.subprocess = Answered
        try:
            ran = journalled("ai_terminal.live.tools._gws_run",
                             lambda: _gws_run._run_sync(argv, command))
        finally:
            gws_cache.cache_dir, _gws_run.subprocess = was_dir, was_sub
    check("nor does the command line of a run that worked",
          asked not in ran, ran)
    check("only which subcommand it was, and how it came back",
          "drive files list" in ran and "exit 0" in ran, ran)

    # And the refusal, which is a WARNING but is ordinary rate-limiting rather
    # than something having gone wrong — so it fires on a normal day.
    def refuse():
        with _gws_run.budget():
            for _ in range(_gws_run.STOP_AT):
                _gws_run.spend()
            asyncio.run(_gws_run.gws(command))

    refused = journalled("ai_terminal.live.tools._gws_run", refuse)
    check("nor the command line of one refused for being over the budget",
          asked not in refused, refused)
    check("only why it was refused, and which subcommand it was",
          "(count)" in refused and "drive files list" in refused, refused)


def startup_cases() -> None:
    print("\n== startup does not block on the cache lock ==")

    # AST: _lifespan does not await migrate or writing before yield
    main_path = (Path(__file__).resolve().parent.parent / "src" / "ai_terminal"
                 / "live" / "main.py")
    tree = ast.parse(main_path.read_text(encoding="utf-8"),
                     filename=str(main_path))
    lifespan = next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "_lifespan")
    yield_node = next(n for n in ast.walk(lifespan) if isinstance(n, ast.Yield))

    awaits_before_yield = [
        n for n in ast.walk(lifespan)
        if isinstance(n, ast.Await) and n.lineno < yield_node.lineno
    ]
    awaits_migrate = any(
        (isinstance(sub, ast.Attribute) and sub.attr == "migrate")
        or (isinstance(sub, ast.Name) and sub.id == "migrate")
        for aw in awaits_before_yield for sub in ast.walk(aw)
    )
    check("_lifespan does not await migrate before yield",
          not awaits_migrate,
          "cache migration must not block the server from yielding",
          awaits_migrate, False)

    awaits_writing = any(
        (isinstance(sub, ast.Attribute) and sub.attr == "writing")
        or (isinstance(sub, ast.Name) and sub.id == "writing")
        for aw in awaits_before_yield for sub in ast.walk(aw)
    )
    check("_lifespan does not await writing before yield",
          not awaits_writing,
          "taking the cache lock before yielding delays request handling",
          awaits_writing, False)

    from ai_terminal.live import main as live_main
    from ai_terminal.live import screensaver

    was_cache_dir = gws_cache.cache_dir
    was_refresh = screensaver.refresh_loop
    scratch = tempfile.TemporaryDirectory()
    gws_cache.cache_dir = lambda: Path(scratch.name)

    async def noop_refresh(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    screensaver.refresh_loop = noop_refresh

    stale = Path(scratch.name) / "stale.md"
    stale.write_text(
        gws_cache._header({
            "source": 'gws drive files list --query "name contains \'zqxvwood\'"',
            "fetched": "2026-09-11T00:00:00+09:00",
        }) + "body\n",
        encoding="utf-8",
    )

    lock_file = Path(scratch.name) / gws_cache._LOCK
    lock_file.touch()
    lock_handle = lock_file.open("w")
    fcntl.flock(lock_handle, fcntl.LOCK_EX)

    yield_time = 0.0
    header_during = ""
    header_after = ""

    async def drive():
        nonlocal yield_time, header_during, header_after
        t0 = time.monotonic()
        async with live_main._lifespan(live_main.app):
            yield_time = time.monotonic() - t0
            header_during = stale.read_text(encoding="utf-8")
            # Release foreign lock so background migration can proceed
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()
            for _ in range(50):
                if "zqxvwood" not in stale.read_text(encoding="utf-8"):
                    break
                await asyncio.sleep(0.01)
            header_after = stale.read_text(encoding="utf-8")

    try:
        asyncio.run(drive())
    finally:
        screensaver.refresh_loop = was_refresh
        if not lock_handle.closed:
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_handle.close()
        gws_cache.cache_dir = was_cache_dir
        scratch.cleanup()

    check("startup yields in < 0.5 s with the lock held",
          yield_time < 0.5,
          f"yielded in {yield_time:.3f} s",
          yield_time < 0.5, True)
    check("the header is untouched while the lock is held",
          "zqxvwood" in header_during,
          "unmigrated header kept while lock held",
          "zqxvwood" in header_during, True)
    check("and migrating completes after the lock is released",
          "zqxvwood" not in header_after,
          "migrated header after lock release",
          "zqxvwood" not in header_after, True)


def main() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        gws_cache.cache_dir = lambda: Path(scratch)
        # Both network calls, slow enough to overlap and answering from the
        # command rather than from Drive, so each header is checkably its own.
        gws_cache._drive_meta = lambda file_id: (
            time.sleep(SLOW) or {"name": f"title of {file_id}"})
        gws_cache._summarize = lambda name, text: (
            time.sleep(SLOW) or f"summary from {text[:30]}")

        print("== two exports meeting on one name ==")
        # A writes x.md and y.md; C writes y.md while A is still annotating
        # x.md. A's diff saw its own y.md, C's write replaces it, and A must
        # not put its header on C's document.
        kept: list[str] = []
        first = threading.Thread(target=lambda: kept.extend(
            export(["x.md", "y.md"], "ID-A", "body of A")))
        first.start()
        time.sleep(SLOW * 0.5)
        export(["y.md"], "ID-C", "body of C")
        first.join()

        y = (Path(scratch) / "y.md").read_text(encoding="utf-8")
        header = gws_cache._read_header(Path(scratch) / "y.md")
        print(f"   y.md is {len(y)} bytes, header names "
              f"{header.get('file_id')!r}")

        check("the surviving body keeps its own file_id",
              header.get("file_id") == "ID-C" and "body of C" in y,
              f"file_id={header.get('file_id')}", header.get("file_id"),
              "ID-C")
        check("no second header underneath the first",
              y.count(gws_cache._HEADER) == 2,
              f"{y.count(gws_cache._HEADER)} `---` lines, so one block",
              y.count(gws_cache._HEADER), 2)
        check("and the summary describes the document, not the header",
              "---" not in (header.get("summary") or ""),
              f"summary={header.get('summary')!r}")
        # The same fact one layer up. A's diff saw y.md, so it is in `written`
        # and `record` used to name it — in the log, and in the `cached` list
        # `_gws_run` hands the sub-agent. Telling it that it has a copy of
        # ID-A's document when the file on disk is ID-C's is the wrong header
        # arriving by another route.
        check("and A is not told it wrote the file it lost",
              kept == ["x.md"], f"record kept {kept}", kept, ["x.md"])
        # x.md was never contended, so it is the control: the same run has to
        # annotate it normally. It is not what the `seen` check is caught by —
        # backing that out fails case 1 on `file_id=ID-A` long before x.md is
        # looked at. What this one rules out is the opposite mistake, a fix
        # that buys case 1 by declining to annotate anything.
        x = gws_cache._read_header(Path(scratch) / "x.md")
        check("the uncontended file is annotated as usual",
              x.get("file_id") == "ID-A" and x.get("title") == "title of ID-A",
              f"file_id={x.get('file_id')} title={x.get('title')!r}")

        # What `writing()` is for, which nothing here defended: with it gone,
        # `if True: yield; return` at the re-entrancy guard, the two commands
        # take overlapping diff windows and the slower one claims the faster
        # one's file. A takes its snapshot, writes download.html and diffs
        # inside a tenth of a second; B's snapshot is older than A's write and
        # its diff is a second later, so B sees download.html as its own.
        #
        # `record` reports what it kept, so B naming download.html in its
        # answer is a second, separate wrong — the sub-agent is told it has a
        # copy of a document it never fetched.
        print("\n== two commands that do not meet on a name ==")
        slow = threading.Thread(
            target=export, args=(["download.html"], "ID-A", "body of A", 0.1))
        slow.start()
        time.sleep(0.02)
        claimed = export(["report.md"], "ID-B", "body of B", SLOW * 3)
        slow.join()

        early = gws_cache._read_header(Path(scratch) / "download.html")
        check("the earlier command keeps the file it wrote",
              early.get("file_id") == "ID-A",
              f"file_id={early.get('file_id')}", early.get("file_id"), "ID-A")
        check("and the later one claims only its own",
              claimed == ["report.md"], f"record kept {claimed}",
              claimed, ["report.md"])

        # Three shapes at the degrade the timeouts are set to keep out of
        # reach, and one shape at the degrade they are set to allow. Scaled
        # together and derived, not typed: all three are about ratios.
        real = (gws_cache.LOCAL_TIMEOUT, gws_cache.LOCK_TIMEOUT)
        work = _gws_cli.TIMEOUT * LOCK_SCALE

        # `writing()` takes `_local` *before* it starts polling for the `flock`
        # and holds it through that wait and through the unlocked yield
        # afterwards, so this server's own longest hold is `TIMEOUT +
        # LOCK_TIMEOUT`. With `LOCAL_TIMEOUT` clearing only `TIMEOUT`, the
        # second thread times out on `_local` while the first is still inside
        # the block and walks in beside it — two overlapping diff windows,
        # which is the one thing `written_by`'s mtimes cannot survive.
        #
        # A foreign process on the `flock` is what makes two threads enough,
        # and a second `open` of the lock file is one: `flock` is per open file
        # description, so this contends with `writing()` from inside this same
        # process.
        print("\n== two threads at a lock somebody else is holding ==")
        overlaps, timeline = queue_up(2, work, foreign=True)
        print(f"   timeline: {[(w, e, round(a, 2)) for w, e, a in timeline]}")
        check("neither thread is let in beside the other", not overlaps,
              f"{work:.2f}s of work each behind a "
              f"{real[1] * LOCK_SCALE:.2f}s flock wait, "
              f"overlapping pairs {overlaps}", overlaps, [])

        # The same degrade with no foreign process anywhere near it, reached by
        # depth instead. `LOCAL_TIMEOUT` is sized as one longest hold, so a
        # waiter timing itself counts the queue: 105 / 45 is 2.33, and the
        # fourth thread would give up on three that were each making progress.
        # The deadline belongs to the holder and a handover restarts it, so
        # five hold in a row here and no arrival order among them puts two
        # inside at once.
        #
        # Five and not four because four is only the first depth that reaches
        # it. The arithmetic says every depth above that too, and a case pinned
        # at the threshold says nothing about the shape past it.
        print("\n== a queue five deep, every hold at the ceiling ==")
        overlaps, timeline = queue_up(5, work, foreign=False)
        print(f"   timeline: {[(w, e, round(a, 2)) for w, e, a in timeline]}")
        check("no two of five are inside the block at once", not overlaps,
              f"{len(timeline) // 2} holds of {work:.2f}s behind a "
              f"{real[0] * LOCK_SCALE:.2f}s deadline, "
              f"overlapping pairs {overlaps}", overlaps, [])

        # And the other direction, which is what makes the case above a fix
        # rather than a hang. The degrade exists for a thread that has stopped:
        # one holding `_local` past `LOCAL_TIMEOUT` without handing it on has
        # to still be walked around, or the terminal loses the `to_thread`
        # worker of every command behind it. Held for half as long again as the
        # deadline, so the second thread is let through while the first is
        # demonstrably still inside.
        print("\n== a thread wedged inside the block ==")
        gws_cache.LOCAL_TIMEOUT = real[0] * LOCK_SCALE
        gws_cache.LOCK_TIMEOUT = real[1] * LOCK_SCALE
        deadline = gws_cache.LOCAL_TIMEOUT
        wedged = deadline * 1.5
        stuck = threading.Thread(target=hold_for, args=(wedged,))
        started = time.monotonic()
        stuck.start()
        time.sleep(deadline * 0.1)
        with gws_cache.writing():
            through = time.monotonic() - started
        stuck.join()
        gws_cache.LOCAL_TIMEOUT, gws_cache.LOCK_TIMEOUT = real

        check("the wedged holder is walked around, not waited out",
              through < wedged,
              f"let through at {through:.2f}s, on a {deadline:.2f}s deadline, "
              f"against a hold that ran {wedged:.2f}s", through, f"< {wedged}")
        # Which only works while the clock says something true. A `_since` left
        # behind by a holder that has gone is a deadline already expired, and
        # the next waiter reads it and walks in beside a thread that has been
        # inside for a millisecond. Nobody is in the block by now, so it is 0.0
        # or the clear is not happening.
        check("and no deadline is left running with nobody inside",
              gws_cache._since == 0.0,
              f"_since is {gws_cache._since!r} after every holder has left",
              gws_cache._since, 0.0)

        print("\n== abandoned temporaries ==")
        fresh = Path(scratch) / f"{gws_cache._TMP}fresh"
        stale = Path(scratch) / f"{gws_cache._TMP}stale"
        fresh.write_text("half a document", encoding="utf-8")
        stale.write_text("half a document", encoding="utf-8")
        old = time.time() - gws_cache._TMP_TTL - 1
        os.utime(stale, (old, old))
        gws_cache.write_index()
        check("a crashed write is swept", not stale.exists(),
              f"{gws_cache._TMP}stale is gone")
        check("one in progress is left alone", fresh.exists(),
              f"{gws_cache._TMP}fresh survives")
        fresh.unlink()

        # In a directory of its own, because everything here is about how many
        # files there are and what the oldest of them is — and the cases above
        # left eight documents lying in `scratch` with mtimes they care about.
        print("\n== the cap, and what goes when the cache reaches it ==")
        with tempfile.TemporaryDirectory() as pen:
            caps = gws_cache.CACHE_MAX_FILES, gws_cache.CACHE_MAX_BYTES
            gws_cache.cache_dir = lambda: Path(pen)

            def put(name: str, body, age: float,
                    fields: dict | None = None) -> Path:
                """One cached file, `age` seconds old. `body` may be bytes.

                Written straight rather than through `record`, because what is
                being checked is what the directory holds and not how it got
                there — and `record`'s own path needs a Drive call per file.
                """
                path = Path(pen) / name
                if isinstance(body, bytes):
                    path.write_bytes(body)
                else:
                    head = gws_cache._header(
                        {"source": f"gws drive files export -o {name}",
                         "fetched": "2026-09-01T09:00:00+09:00",
                         **(fields or {})})
                    path.write_text(head + body, encoding="utf-8")
                when = time.time() - age
                os.utime(path, (when, when))
                return path

            def left() -> list[str]:
                """What is still cached, oldest last, the index excluded."""
                return [item["name"] for item in gws_cache._records()]

            def wipe() -> None:
                for path in Path(pen).iterdir():
                    path.unlink()

            try:
                # The count cap, which is the one the read costs care about:
                # `_records` stats every file and `_scored` opens every file,
                # both once per turn, so it is the number of them that makes a
                # turn slow and not their size.
                gws_cache.CACHE_MAX_FILES = 8
                gws_cache.CACHE_MAX_BYTES = 1 << 30
                for i in range(12):
                    put(f"doc-{i:02d}.md", "a hundred bytes " * 6, 120 - i)
                gws_cache.write_index()
                check("the file cap drops what is over it",
                      len(left()) == 8, f"12 written, {len(left())} left",
                      len(left()), 8)
                # Oldest first, and by mtime: `fetched` is identical on all
                # twelve here and missing altogether from the files below, so
                # an eviction ordered by the header has nothing to sort on.
                check("and it is the oldest that go",
                      left() == [f"doc-{i:02d}.md"
                                 for i in range(11, 3, -1)],
                      f"{left()}", left(),
                      [f"doc-{i:02d}.md" for i in range(11, 3, -1)])
                published = (Path(pen) / gws_cache.INDEX).read_text(
                    encoding="utf-8")
                check("and the index published is the directory as it now is",
                      "doc-00.md" not in published
                      and "doc-11.md" in published,
                      f"{len(published)} bytes, "
                      f"{published.count('| [')} rows")

                # The byte cap, on files the count cap is nowhere near. Equal
                # sizes so the cut is arithmetic and not a guess: three fit
                # inside three and a half, the fourth does not.
                wipe()
                each = put("size-00.md", "x" * 1000, 60).stat().st_size
                for i in range(1, 8):
                    put(f"size-{i:02d}.md", "x" * 1000, 60 - i)
                gws_cache.CACHE_MAX_FILES = 200
                gws_cache.CACHE_MAX_BYTES = each * 3 + each // 2
                gws_cache.write_index()
                check("the byte cap fires on size with the count nowhere near",
                      len(left()) == 3,
                      f"8 files of {each} B under a "
                      f"{gws_cache.CACHE_MAX_BYTES} B cap, {len(left())} left",
                      len(left()), 3)

                # One export bigger than the whole cap. Evicting it would mean
                # fetching a document and deleting it inside the one command,
                # and answering that the cache has nothing in it.
                wipe()
                put("whale.md", "x" * 5000, 1)
                gws_cache.CACHE_MAX_BYTES = 10
                gws_cache.write_index()
                check("but the newest document is never the one that goes",
                      left() == ["whale.md"], f"{left()}", left(),
                      ["whale.md"])

                # A PDF and the text read out of it are one record — the
                # sidecar's — so evicting the record has to take the bytes too.
                # Left behind, the PDF is a file no listing shows, no search
                # reaches and nothing will ever delete.
                wipe()
                pdf = make_pdf(["Folio total 42 EUR"])
                put("invoice.pdf", pdf, 60)
                put("invoice.pdf.md", "Folio total 42 EUR\n", 60,
                    {"pdf": "invoice.pdf"})
                put("note.md", "the newest thing here\n", 1)
                gws_cache.CACHE_MAX_FILES = 1
                gws_cache.CACHE_MAX_BYTES = 1 << 30
                gws_cache.write_index()
                check("a sidecar taken by the cap takes its PDF with it",
                      sorted(p.name for p in Path(pen).iterdir()
                             if gws_cache._cached(p)) == ["note.md"],
                      f"{sorted(p.name for p in Path(pen).iterdir())}")

                # And the same pair the other way round: the sidecar is three
                # hundred bytes of text in front of however many megabytes of
                # PDF, so a byte cap weighing only the record it can see never
                # fires on the part that is actually on the card.
                wipe()
                put("invoice.pdf", b"%PDF-1.4\n" + b"\x00" * 40000, 60)
                put("invoice.pdf.md", "Folio total 42 EUR\n", 60,
                    {"pdf": "invoice.pdf"})
                put("note.md", "the newest thing here\n", 1)
                gws_cache.CACHE_MAX_FILES = 200
                gws_cache.CACHE_MAX_BYTES = 5000
                gws_cache.write_index()
                check("and the byte cap weighs the PDF, not just its sidecar",
                      left() == ["note.md"], f"{left()}", left(), ["note.md"])

                # Ours. The header points at a PDF that is not there any more,
                # which is what `index.md` has been telling the household to do
                # since the first version: delete a file to drop it. Weighing
                # that record stats a path nothing is at and evicting it
                # unlinks one, and this runs under the lock inside
                # `write_index` — so an eviction that raises here does not lose
                # one document, it takes down the fetch that triggered it and
                # every fetch after, until somebody with a keyboard notices.
                wipe()
                put("invoice.pdf.md", "Folio total 42 EUR\n", 60,
                    {"pdf": "invoice.pdf"})
                put("note.md", "the newest thing here\n", 1)
                gws_cache.CACHE_MAX_FILES = 1
                gws_cache.CACHE_MAX_BYTES = 1 << 30
                try:
                    gws_cache.write_index()
                    raised = None
                except OSError as exc:
                    raised = exc
                check("a record whose PDF was deleted by hand still evicts",
                      raised is None and left() == ["note.md"],
                      f"{raised!r}, {left()} left",
                      [raised, left()], [None, ["note.md"]])

                # Everything that is not a document this module wrote. A
                # spreadsheet download has no header at all, a truncated one
                # has an opening `---` and no close, and the lock and the index
                # are the cache's own bookkeeping — the first two are cached
                # documents and count, the last two are not and must not.
                wipe()
                put("sheet.xlsx", b"PK\x03\x04" + b"\x00" * 500, 90)
                put("cut-off.md", b'---\nsource: "gws"\n', 80)
                put("keep.md", "the newest thing here\n", 1)
                (Path(pen) / gws_cache._LOCK).write_text("", encoding="utf-8")
                gws_cache.CACHE_MAX_FILES = 3
                gws_cache.write_index()
                check("a headerless or truncated file is a document and "
                      "counts", sorted(left()) ==
                      ["cut-off.md", "keep.md", "sheet.xlsx"],
                      f"{sorted(left())}", sorted(left()),
                      ["cut-off.md", "keep.md", "sheet.xlsx"])
                gws_cache.CACHE_MAX_FILES = 1
                gws_cache.write_index()
                check("and the lock and the index are neither counted nor "
                      "evicted",
                      left() == ["keep.md"]
                      and (Path(pen) / gws_cache._LOCK).exists()
                      and (Path(pen) / gws_cache.INDEX).exists(),
                      f"{sorted(p.name for p in Path(pen).iterdir())}")

                # An empty cache is not an error and not an eviction.
                wipe()
                gws_cache.write_index()
                check("and an empty cache is left empty rather than raised at",
                      not left(), f"{left()} cached, index rebuilt anyway")

                # What a cap of zero would do, which is the reason the import
                # refuses one. Green before the guard as well as after it —
                # the caps are set by hand here, so this is the premise the
                # refusal below rests on rather than a check of it. #115.
                for name, files, byts in (("count", 0, 1 << 30),
                                          ("count", -1, 1 << 30),
                                          ("byte", 200, 0),
                                          ("byte", 200, -1)):
                    wipe()
                    for i in range(5):
                        put(f"keep-{i}.md", "a document worth keeping\n",
                            50 - i)
                    gws_cache.CACHE_MAX_FILES = files
                    gws_cache.CACHE_MAX_BYTES = byts
                    gws_cache.write_index()
                    cap = files if name == "count" else byts
                    check(f"a {name} cap of {cap} would leave one of five",
                          left() == ["keep-4.md"],
                          "only `index` zero is spared, so the next fetch "
                          "would delete every document but itself",
                          left(), ["keep-4.md"])
            finally:
                gws_cache.CACHE_MAX_FILES, gws_cache.CACHE_MAX_BYTES = caps
                gws_cache.cache_dir = lambda: Path(scratch)

        # The one thing the cases above cannot see, because they set the caps
        # by hand: whether the caps are read at all, or typed into the source
        # where nobody can change them on a machine with a different card in
        # it. A subprocess because the constants are read once, at import.
        source = str(Path(__file__).resolve().parent.parent / "src")
        asked = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {source!r})\n"
             "from ai_terminal.live.tools import gws_cache\n"
             "print(gws_cache.CACHE_MAX_FILES, gws_cache.CACHE_MAX_BYTES)"],
            env={**os.environ, "GWS_CACHE_MAX_FILES": "17",
                 "GWS_CACHE_MAX_BYTES": "1234"},
            capture_output=True, text=True)
        check("the caps come off the environment and not off the source",
              asked.stdout.split() == ["17", "1234"],
              f"{asked.stdout.strip()!r}{asked.stderr[-200:]}",
              asked.stdout.split(), ["17", "1234"])

        # And the values a cap must not take, refused at import for the same
        # reason `SESSION_RETAIN_S` is: zero reads as "no cap" and the block
        # above measured what it does instead. `nan` and `inf` are not in
        # this list because `int()` has already refused them — that is the
        # whole difference between these two and the float settings #114
        # guarded. #115.
        def imported(**names) -> subprocess.CompletedProcess:
            """`gws_cache` imported with `names` in the environment."""
            return subprocess.run(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, {source!r})\n"
                 "from ai_terminal.live.tools import gws_cache"],
                env={**os.environ, **names}, capture_output=True, text=True)

        for key, value in (("GWS_CACHE_MAX_FILES", "0"),
                           ("GWS_CACHE_MAX_FILES", "-1"),
                           ("GWS_CACHE_MAX_BYTES", "0"),
                           ("GWS_CACHE_MAX_BYTES", "-1")):
            out = imported(**{key: value})
            check(f"{key}={value} is refused at import",
                  out.returncode != 0 and key in out.stderr,
                  "a cache holding one document is not a cache",
                  (out.returncode, out.stderr[-160:]), (1, key))

        print("\n== a header is never stacked, and a document is never cut ==")
        ours = gws_cache._header({"source": "first", "fetched": "now"})
        check("stripping leaves the body",
              gws_cache._without_header(ours + "the document\n")
              == "the document\n",
              repr(gws_cache._without_header(ours + "the document\n")))
        # The three shapes that open with `---` and are not ours. The first has
        # no closing rule at all; the other two have one, and cutting to it was
        # deleting a real document. `_ours` is what tells them apart: a Jekyll
        # `title: Q3 plan` is not JSON, and a horizontal rule has no fields at
        # all, while every header this module writes has `source` and
        # `fetched`.
        jekyll = ('---\ntitle: Q3 plan\nauthor: Alex\n---\n\n# Q3 plan\n\n'
                  'Budget is 12,000,000 JPY.\n')
        rule = ('---\n\n# Minutes, 2026-09-01\n\nPresent: Alex, Rina\n\n'
                '---\n\n# Minutes, 2026-09-02\n\nPresent: Alex\n')
        for name, text in (("no closing rule", "---\nnot a header\n"),
                           ("Jekyll front matter", jekyll),
                           ("a document that opens on a rule", rule)):
            check(f"{name} is left alone",
                  gws_cache._without_header(text) == text,
                  f"{len(text)} bytes in, "
                  f"{len(gws_cache._without_header(text))} out",
                  gws_cache._without_header(text), text)

        # `_read_header` needs the same test, and for the same reason with the
        # sign flipped: a YAML value that happens to be a quoted string parses
        # as JSON, so without `_ours` the index would publish the document's
        # own `title` as if the cache had written it.
        quoted = Path(scratch) / "quoted.md"
        quoted.write_text('---\ntitle: "Q3 plan"\n---\n\n# Q3 plan\n',
                          encoding="utf-8")
        check("a foreign block is not read as a cache header",
              gws_cache._read_header(quoted) == {},
              f"{gws_cache._read_header(quoted)}",
              gws_cache._read_header(quoted), {})
        quoted.unlink()

        # The other two ways a foreign block can carry `source` and `fetched`
        # and still not be ours. Both used to read as a header, and
        # `_without_header` then deleted the document down to the second rule:
        # the first went 71 bytes in and 23 out, the second 104 in and 19 out.
        unquoted = ('---\nsource: my own note\nfetched: yesterday\n---\n\n'
                    '# Real document\n\nBody.\n')
        nested = ('---\ndocument:\n  source: "internal memo"\n'
                  '  fetched: "2026-01-01"\ntitle: "Q3 plan"\n---\n\n'
                  '# Real body\n')
        # And the shape that needs *both* fields to be required rather than
        # either: a quoted `source` and nothing else parses cleanly, so `_ours`
        # asking for one name would take this document's own front matter for a
        # header and `_without_header` would delete it.
        one_field = ('---\nsource: "a note to myself"\n---\n\n'
                     '# Real document\n\nBudget is 12,000,000 JPY.\n')
        for name, text in (("an unquoted value is not JSON", unquoted),
                           ("a nested key is not a field", nested),
                           ("one field of the two is not a header", one_field)):
            foreign_path = Path(scratch) / "foreign.md"
            foreign_path.write_text(text, encoding="utf-8")
            head = gws_cache._read_header(foreign_path)
            stripped = gws_cache._without_header(text)
            check(f"{name}, so the block is not ours",
                  head == {} and stripped == text,
                  f"{len(text)} bytes in, {len(stripped)} out, header {head}",
                  head, {})
            foreign_path.unlink()

        # The bound on how much of a file that costs. `_records()` runs
        # `_read_header` over the whole cache on every turn, and a document
        # that opens with `---` and never closes it used to have every line of
        # itself appended to `block` before the loop ran out.
        big = Path(scratch) / "big.md"
        big.write_text("---\n" + "filler line\n" * 400_000, encoding="utf-8")
        tracemalloc.start()
        head = gws_cache._read_header(big)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        check("a 5 MB file that opens with `---` is not buffered whole",
              head == {} and peak < 1_000_000,
              f"{big.stat().st_size:,} bytes on disk, {peak / 1e6:.1f} MB "
              f"of peak traced memory, header {head}")
        big.unlink()

        # The line ceiling is what the case above rests on, and it is only half
        # the bound: `readline` takes its cap per call, so one enormous line
        # arrives whole unless `_HEADER_BYTES` cuts it into pieces the line
        # count can then run out on. A minified export is exactly that shape.
        single = Path(scratch) / "single.md"
        single.write_text("---\n" + "x" * 5_000_000, encoding="utf-8")
        tracemalloc.start()
        head = gws_cache._read_header(single)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        check("and neither is one 5 MB line",
              head == {} and peak < 1_000_000,
              f"{single.stat().st_size:,} bytes on one line, "
              f"{peak / 1e6:.1f} MB of peak traced memory, header {head}")
        single.unlink()

        # And the same through `_annotate`, because the call was unpinned:
        # replacing `text = _without_header(text)` with a no-op left every case
        # above passing. Two shapes, one each way — the document's own front
        # matter has to survive, and our own header has to not stack.
        print("\n== the same, through _annotate ==")
        for name, body, want_rules in (("front-matter.md", jekyll, 4),
                                       ("ours.md", ours + "under it\n", 2)):
            path = Path(scratch) / name
            path.write_text(body, encoding="utf-8")
            annotate(path)
            out = path.read_text(encoding="utf-8")
            kept = gws_cache._without_header(out)
            want_kept = body if name == "front-matter.md" else "under it\n"
            check(f"{name}: our header on top and the body intact",
                  out.count(gws_cache._HEADER) == want_rules
                  and kept == want_kept,
                  f"{len(out)} bytes, {out.count(gws_cache._HEADER)} rules",
                  kept, want_kept)

        # What `cached` means. `_annotate` returns False for three different
        # things and `record` used to read all three as "not this command's":
        # a PDF is not editable text, and the sub-agent has it all the same.
        # Reporting only what got a header dropped every binary download out of
        # the answer, and a file the tool never mentions is one nobody asks for
        # again.
        print("\n== a download that is not text ==")
        pdf = Path(scratch) / "report.pdf"
        raw = b"%PDF-1.4\n\xff\xfe not utf-8\n"
        pdf.write_bytes(raw)
        got = record(
            {"report.pdf": pdf.stat().st_mtime},
            'gws drive files get -o report.pdf --params \'{"fileId": "ID-P"}\'')
        check("a binary download is named as cached", got == ["report.pdf"],
              f"record kept {got}", got, ["report.pdf"])
        check("and is left exactly as it was downloaded",
              pdf.read_bytes() == raw and gws_cache._read_header(pdf) == {},
              f"{pdf.stat().st_size} bytes, no header")

        # The other two returns, which are the ones that do mean "not this
        # command's". `_still_ours` is where both live now, so they can be
        # asked directly instead of raced for.
        check("a file that has gone is nobody's",
              not gws_cache._still_ours(Path(scratch) / "never.md", 0.0),
              "an OSError on the stat is not an owner")
        check("nor is one whose mtime moved",
              not gws_cache._still_ours(pdf, pdf.stat().st_mtime - 1),
              "the diff saw a different body")

        # And the same three through `record`, because `_still_ours` being
        # right is only half of it — `_annotate` has to ask it, on both
        # branches, and hand it the mtime the diff saw rather than one of its
        # own. Every path here returns True from somewhere if the asking is
        # dropped, and `record` then names a file this command does not have.
        replaced = 'gws drive files get -o report.pdf --params \'{"fileId": "ID-P"}\''
        got = record({"report.pdf": pdf.stat().st_mtime - 1}, replaced)
        check("a binary another command replaced is given up",
              got == [], f"record kept {got}", got, [])
        got = record({"x.md": (Path(scratch) / "x.md").stat().st_mtime - 1},
                               "gws drive files export -o x.md")
        check("and so is a document another command replaced",
              got == [], f"record kept {got}", got, [])
        got = record({"vanished.md": 0.0},
                               "gws drive files export -o vanished.md")
        check("a file that went between the diff and the read is not claimed",
              got == [], f"record kept {got}", got, [])
        pdf.unlink()

        # `read_cache` is a coroutine and its freshness check ends in a `gws`
        # subprocess. Run bare, that subprocess blocks the live server's loop
        # for as long as Drive takes to answer — 0.55 s median, and
        # `_gws_cli.TIMEOUT` seconds if the CLI wedges — with the audio stream
        # on the same loop.
        print("\n== the freshness check does not stall the event loop ==")
        stamp = "2026-09-01T00:00:00.000Z"
        loop_doc = Path(scratch) / "loop.md"
        loop_doc.write_text(
            gws_cache._header({
                "source": 'gws drive files export -o loop.md --params '
                          '\'{"fileId": "ID-L"}\'',
                "fetched": "2026-09-01T09:00:00+09:00",
                "file_id": "ID-L", "modified": stamp,
            }) + "body of the cached document\n", encoding="utf-8")

        # Whatever Drive says `modifiedTime` is, after a wait the length of a
        # real one. Same shape as the stub at the top of `main`, plus the field
        # the staleness check reads and the step the real `_drive_meta` logs —
        # it is logged from wherever the check ends up running, which is the
        # point of the third case below.
        def drive_says(modified: str):
            def meta(file_id: str) -> dict:
                with _timing.step("drive.meta"):
                    time.sleep(SLOW)
                return {"name": "loop", "modifiedTime": modified}
            return meta

        # First that the check is still made at all, because the cheapest way
        # to take a wait off the loop is to stop waiting, and every case below
        # passes trivially for a copy nobody checks.
        gws_cache._drive_meta = drive_says("2026-09-05T00:00:00.000Z")
        held: list = []
        aged = journalled("ai_terminal.live.tools.gws_cache",
                          lambda: held.append(asyncio.run(beside("loop.md"))))
        stale_doc = held[0][0]
        check("an edited original is caught",
              "out of date" in stale_doc.get("error", ""),
              f"{stale_doc.get('error', stale_doc)!r}")
        # This line and the one for a copy that reads back fire for the
        # sub-agent's own `read_cache` calls as well as for a resolve, so the
        # name did not have to be said out loud to get here — it is a document
        # in the household's Drive either way, and the journal holds it for a
        # month. #234, folded in from #226's map.
        check("and the line saying so does not name the document",
              "loop.md" not in aged, aged)
        check("only how long its name was", "<7 chars>" in aged, aged)

        gws_cache._drive_meta = drive_says(stamp)
        with Steps() as steps:
            with _timing.question("loop"):
                fresh, ticks = asyncio.run(beside("loop.md"))
        check("and an untouched one is served",
              fresh.get("text", "").endswith("body of the cached document\n"),
              f"{len(fresh.get('text', ''))} bytes back, keys {sorted(fresh)}")
        want = int(SLOW / TICK / 2)
        check("with the loop still running while Drive is asked",
              ticks >= want,
              f"{ticks} heartbeats in the {SLOW:.1f}s the check took, "
              f"wanted at least {want}", ticks, f">= {want}")
        # `asyncio.to_thread` copies the context and `run_in_executor` does not,
        # so getting this off the loop the other way would leave `drive.meta`
        # with no question on it and the timing tables missing their slowest
        # nested step.
        check("and the step inside the thread still the question's",
              steps.named("drive.meta").get("q") == "loop"
              and steps.named("drive.meta")["at"] >= 0,
              f"drive.meta {steps.named('drive.meta')}")
        loop_doc.unlink()

        # `_annotate` asks Drive for a document's `modifiedTime` to write the
        # header, and seconds later `_is_stale` asks about the same file to
        # check the copy it just wrote — 1.16 s of a cold question for one
        # answer. `remembering()` opens a memo per question so the second call
        # is answered from the first. Stubbed at the subprocess, because the
        # memo is inside `_drive_meta` and a stub of it checks nothing.
        print("\n== Drive asked once a question, not once a call ==")
        gws_cache._drive_meta = REAL_DRIVE_META
        asked: list[str] = []

        def drive_answers(argv, cwd, timeout=None) -> dict:
            params = json.loads(argv[argv.index("--params") + 1])
            asked.append(params["fileId"])
            return {"id": params["fileId"], "name": "memo",
                    "mimeType": "text/plain", "modifiedTime": stamp}

        _gws_cli.json_output = drive_answers

        with gws_cache.remembering():
            once = gws_cache._drive_meta("ID-M")
            twice = gws_cache._drive_meta("ID-M")
        check("one question asks Drive about a file once",
              asked == ["ID-M"], f"asked {asked}", asked, ["ID-M"])
        check("and the second caller gets what the first was told",
              twice == once and once.get("modifiedTime") == stamp,
              f"{twice}")

        asked.clear()
        with gws_cache.remembering():
            gws_cache._drive_meta("ID-M")
            gws_cache._drive_meta("ID-N")
        check("a different file is a different thing to ask",
              asked == ["ID-M", "ID-N"], f"asked {asked}",
              asked, ["ID-M", "ID-N"])

        # The one place this must not copy `_gws_run.budget`, which passes
        # through to a hold already open so a plan's four gws steps share one
        # allowance. A memo doing that would span several exports and answer
        # one step's freshness check out of another step's reading of Drive.
        asked.clear()
        with gws_cache.remembering():
            gws_cache._drive_meta("ID-M")
            with gws_cache.remembering():
                gws_cache._drive_meta("ID-M")
        check("and a memo inside another does not inherit it, unlike a budget",
              asked == ["ID-M", "ID-M"], f"asked {asked}",
              asked, ["ID-M", "ID-M"])

        asked.clear()
        gws_cache._drive_meta("ID-M")
        gws_cache._drive_meta("ID-M")
        check("outside a question nothing is remembered at all",
              asked == ["ID-M", "ID-M"], f"asked {asked}",
              asked, ["ID-M", "ID-M"])

        # `{}` is what an unreachable Drive returns, and `_is_stale` reads that
        # as fresh. Remembered, one hiccup would serve every stale copy for the
        # rest of the question without asking again.
        asked.clear()
        _gws_cli.json_output = lambda argv, cwd, timeout=None: (
            asked.append("failed") or None)
        with gws_cache.remembering():
            gws_cache._drive_meta("ID-M")
            gws_cache._drive_meta("ID-M")
        check("a lookup that failed is not an answer worth keeping",
              asked == ["failed", "failed"], f"asked {asked}",
              asked, ["failed", "failed"])

        # The two calls #32 is about, in the order a cold question makes them:
        # the export's header, then the sub-agent reading back what it fetched.
        asked.clear()
        _gws_cli.json_output = drive_answers
        with gws_cache.remembering():
            kept_memo = export(["memo.md"], "ID-M", "body of M")
            served = asyncio.run(gws_cache.read_cache("memo.md"))
        check("so a cold fetch and the read after it ask Drive once between "
              "them", asked == ["ID-M"], f"asked {asked}", asked, ["ID-M"])
        check("and the copy just written is served rather than called stale",
              served.get("text", "").endswith("body of M in memo.md\n"),
              f"kept {kept_memo}, keys {sorted(served)}")

        # The other half of #32. Summarising a document is a model call, and
        # nothing downstream of the fetch reads the summary — only the *next*
        # question's listing does — so the question that paid for the fetch was
        # waiting on work it would never see the result of.
        print("\n== the summary written after the answer, not before it ==")
        # Slower than `_drive_meta`, and it has to be: what the timing case
        # below asks is which of the two steps is the longer one, and with both
        # at `SLOW` a `record` that still called this would look much as it
        # does now. Named per file, so two of them cannot be confused.
        gws_cache._summarize = lambda name, text: (
            time.sleep(SLOW * 3) or f"summary of {name}")

        def fetched(name: str, file_id: str,
                    body: str) -> tuple[list[str], list[tuple]]:
            """One export, run only as far as the point the answer goes out.

            `export` above runs the deferred half too, which is right for every
            case that is about the file on disk. These are about where the two
            halves split, so they hold the second one and run it themselves.
            """
            with gws_cache.writing():
                before = gws_cache.snapshot()
                (Path(scratch) / name).write_text(body, encoding="utf-8")
                written = gws_cache.written_by(before)
            return gws_cache.record(
                written, f"gws drive files export -o {name} "
                f'--params \'{{"fileId": "{file_id}"}}\'')

        def indexed(name: str) -> str:
            """`name`'s row of `index.md`, or "" when it has none."""
            rows = (Path(scratch) / gws_cache.INDEX).read_text(
                encoding="utf-8").splitlines()
            return next((r for r in rows if r.startswith(f"| [{name}]")), "")

        note = Path(scratch) / "note.md"
        with Steps() as steps, _timing.question("cold"):
            held_record: list = []
            wrote = journalled(
                "ai_terminal.live.tools.gws_cache",
                lambda: held_record.append(
                    fetched("note.md", "ID-Q", "body of the note\n")))
            kept_note, pending = held_record[0]
            # Everything the sub-agent is given, read at the moment it is given
            # it. This is also the state a process killed here would leave
            # behind, since nothing runs the deferred half on shutdown: a
            # header that parses, an index row, and no summary in either.
            headed = gws_cache._read_header(note)
            stamped = note.stat().st_mtime
            row_before = indexed("note.md")
            gws_cache.finish(pending)

        check("the answer goes out with a full header and no summary in it",
              kept_note == ["note.md"] and headed.get("file_id") == "ID-Q"
              and headed.get("title") == "memo" and "summary" not in headed,
              f"kept {kept_note}, header {sorted(headed)}")
        check("and the write path does not name the document either",
              "note.md" not in wrote, wrote)
        check("only how long its name was", "<7 chars>" in wrote, wrote)
        check("and an index row that says so rather than one that is missing",
              row_before.endswith("| — |") and "note.md" in row_before,
              f"row {row_before!r}")

        done = gws_cache._read_header(note)
        body = gws_cache._without_header(note.read_text(encoding="utf-8"))
        check("then the summary lands on the same file, body untouched",
              done.get("summary") == "summary of note.md"
              and body == "body of the note\n",
              f"summary={done.get('summary')!r}, {len(body)} bytes of body")
        check("with the mtime still where the diff left it",
              note.stat().st_mtime == stamped,
              f"{note.stat().st_mtime} vs {stamped}")
        check("and the index rebuilt around it",
              indexed("note.md").endswith("| summary of note.md |"),
              f"row {indexed('note.md')!r}")

        # What the split is worth, and where the cost went. Both steps are
        # logged; only one of them is charged to the question. This case first,
        # because the one below reads a duration out of `deferred` and a step
        # logged under the wrong name would take it off the end of an empty
        # list — true, and a traceback rather than a named failure.
        deferred = [s for s in steps.seen if s["step"] == "summarize.bg"]
        check("the model call is still timed, and against no question",
              len(deferred) == 1 and "q" not in deferred[0]
              and steps.named("record").get("q") == "cold",
              f"{deferred} against record q="
              f"{steps.named('record').get('q')!r}")
        check("so the question is charged for the header, not for the model",
              steps.named("record")["ms"] < deferred[0]["ms"],
              f"record {steps.named('record')['ms']} ms, "
              f"summarize.bg {deferred[0]['ms']} ms")

        # `mark` writes every field of the record to the journal, so the step
        # that says a summary is running must not say which of the
        # household's documents it is running on. See #234.
        line = json.dumps(deferred[0], ensure_ascii=False)
        check("and no field of that line names the file it summarised",
              "note.md" not in line, line)
        check("only how long its name was, which still tells one line of a "
              "batch from the next alongside `chars`",
              deferred[0].get("file") == "<7 chars>",
              f"file={deferred[0].get('file')!r}")

        # `defer` is the piece `_gws_run.gws` calls and none of the above
        # touches: `record` runs on a `to_thread` worker with no loop to create
        # a task on, so the work has to be handed back out to the async side.
        # asyncio holds a task by weak reference, so a `defer` that does not
        # keep one can have its summary collected mid-await.
        async def deferred_run() -> tuple[int, int]:
            _, pending = fetched("task.md", "ID-T", "body of the task\n")
            gws_cache.defer(pending)
            held = len(gws_cache._SUMMARIES)
            while gws_cache._SUMMARIES:
                await asyncio.sleep(0.05)
            return held, len(gws_cache._SUMMARIES)

        in_flight, after = asyncio.run(deferred_run())
        check("the work handed to `defer` is held onto and does run",
              (in_flight, after) == (1, 0)
              and gws_cache._read_header(Path(scratch) / "task.md").get(
                  "summary") == "summary of task.md",
              f"{in_flight} task in flight, {after} after")

        # And that anything hands it over. Every case above reaches `defer` or
        # `finish` by calling it, so the one line that connects them to a real
        # export — `gws_cache.defer(later)` at the end of `_gws_run.gws` — was
        # the only part of this that nothing watched. Delete it and no document
        # is ever summarised, no check goes red, and the index's summary column
        # reads `| — |` for the life of the cache. See #55.
        async def through_the_tool() -> tuple[int, int]:
            _, pending = fetched("wired.md", "ID-W", "body of the wire\n")
            # `record` has already run, above, so this stands in for the whole
            # of `_run_sync` — the subprocess and the diff included. What is
            # being watched is only what `gws` does with its second return
            # value.
            _gws_run._run_sync = lambda argv, command: ({"exit_code": 0},
                                                        pending)
            await _gws_run.gws("gws drive files export -o wired.md")
            handed = len(gws_cache._SUMMARIES)
            while gws_cache._SUMMARIES:
                await asyncio.sleep(0.05)
            return handed, len(gws_cache._SUMMARIES)

        real_run_sync = _gws_run._run_sync
        try:
            passed_on, drained = asyncio.run(through_the_tool())
        finally:
            _gws_run._run_sync = real_run_sync
        check("and the tool that ran the export is what hands it over",
              (passed_on, drained) == (1, 0)
              and gws_cache._read_header(Path(scratch) / "wired.md").get(
                  "summary") == "summary of wired.md",
              f"{passed_on} deferred by `gws`, {drained} after")

        # `finish` writes the index once per file, so two of them in flight are
        # two rebuilds racing, and a rebuild reads every header in the
        # directory. Unlocked, whichever reads first can be whichever writes
        # last, and it publishes an index without the other's summary in it.
        print("\n== two deferred summaries landing at once ==")
        held = [fetched(name, file_id, f"body of {file_id}\n")[1]
                for name, file_id in (("one.md", "ID-1"), ("two.md", "ID-2"))]
        jobs = [threading.Thread(target=gws_cache.finish, args=(p,))
                for p in held]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join()
        check("neither rebuild loses the other's row",
              indexed("one.md").endswith("| summary of one.md |")
              and indexed("two.md").endswith("| summary of two.md |"),
              f"{indexed('one.md')!r} / {indexed('two.md')!r}")

        # The `seen` check, on the half of the write that now happens seconds
        # later. A deferred summary is a header rewrite like any other and the
        # rule for those does not change: a file another command has replaced
        # is not this one's to write.
        stale = fetched("gone.md", "ID-3", "body of ID-3\n")[1]
        fetched("gone.md", "ID-4", "body of ID-4\n")
        gws_cache.finish(stale)
        replaced_head = gws_cache._read_header(Path(scratch) / "gone.md")
        check("a file replaced before its summary lands keeps the new header",
              replaced_head.get("file_id") == "ID-4"
              and "summary" not in replaced_head
              and "body of ID-4" in (Path(scratch) / "gone.md").read_text(
                  encoding="utf-8"),
              f"header {replaced_head}")

        # A number printed on an invoice was unanswerable at any budget: the
        # bytes cached fine and `read_cache` handed back "'utf-8' codec can't
        # decode", so the only honest thing the sub-agent could say was that it
        # had the file and could not open it. Summarising put back to instant —
        # nothing below is about when the model call happens, and `_summarize`
        # is still on the `SLOW * 3` stub from the section above.
        print("\n== a PDF is a document, not an unreadable download ==")
        gws_cache._summarize = lambda name, text: f"summary of {name}"
        invoice = Path(scratch) / "invoice.pdf"
        invoice.write_bytes(make_pdf(["ZIPAIR Tokyo", "Total JPY 128,400"]))
        got = record({"invoice.pdf": invoice.stat().st_mtime},
                     "gws drive files get -o invoice.pdf "
                     '--params \'{"fileId": "ID-PDF"}\'')
        check("one PDF written is two files named",
              got == ["invoice.pdf", "invoice.pdf.md"], f"record kept {got}",
              got, ["invoice.pdf", "invoice.pdf.md"])
        check("and the bytes are left exactly as they were downloaded",
              invoice.read_bytes() == make_pdf(["ZIPAIR Tokyo",
                                                "Total JPY 128,400"])
              and gws_cache._read_header(invoice) == {},
              f"{invoice.stat().st_size} bytes, no header")

        side = asyncio.run(gws_cache.read_cache("invoice.pdf.md"))
        check("the figure on it reads back through read_cache",
              "Total JPY 128,400" in side.get("text", ""),
              f"{len(side.get('text', ''))} chars, keys {sorted(side)}")
        # And by the PDF's own name, which is the name the sub-agent just typed
        # into the download command and so the one it is most likely to try.
        # This used to come back `Could not read it: 'utf-8' codec can't decode`
        # naming no alternative, which is a dead end at exactly the point the
        # answer is one file away.
        pdf_back = asyncio.run(gws_cache.read_cache("invoice.pdf"))
        check("and by the PDF's own name, which redirects to the copy",
              pdf_back.get("name") == "invoice.pdf.md"
              and "Total JPY 128,400" in pdf_back.get("text", "")
              and "error" not in pdf_back,
              f"{pdf_back.get('name')!r}, keys {sorted(pdf_back)}")
        found = asyncio.run(gws_cache.search_cache("128,400"))["found"]
        check("and a phrase that exists only inside the PDF is searchable",
              "invoice.pdf.md" in found, f"search found {found!r}")

        # The two are one document, so the copy has to carry the original's
        # identity: without `file_id` and `modified` on it `_is_stale` cannot
        # check it, and the text of a document edited in Drive last week goes
        # out as current. `pdf` is what says the two are a pair rather than two
        # unrelated files that happen to share a stem.
        head = gws_cache._read_header(Path(scratch) / "invoice.pdf.md")
        check("the text copy names the PDF it was read out of",
              head.get("pdf") == "invoice.pdf", f"header {head}",
              head.get("pdf"), "invoice.pdf")
        check("and carries the same identity, so staleness guards both",
              (head.get("file_id"), head.get("modified")) == ("ID-PDF", stamp),
              f"file_id={head.get('file_id')} modified={head.get('modified')}")
        row = indexed("invoice.pdf.md")
        check("the listing gets a title and a summary, not a bare filename",
              "memo" in row and row.endswith("| summary of invoice.pdf.md |"),
              f"index row {row!r}")

        # OCR is a different dependency and a much worse latency story on a Pi,
        # so a page of pixels is answered rather than read. Coming back empty
        # is the failure mode this replaces: the sub-agent reported that the
        # document was blank, which is a different thing from a scan and sent
        # the person looking for a file they had already sent.
        print("\n== a PDF with nothing selectable on it ==")
        scan = Path(scratch) / "scan.pdf"
        scan.write_bytes(make_pdf([]))
        got = record({"scan.pdf": scan.stat().st_mtime},
                     "gws drive files get -o scan.pdf "
                     '--params \'{"fileId": "ID-SCAN"}\'')
        scan_head = gws_cache._read_header(Path(scratch) / "scan.pdf.md")
        check("a scan still gets a copy, and one that says it is a scan",
              got == ["scan.pdf", "scan.pdf.md"]
              and scan_head.get("text_layer") == "none",
              f"record kept {got}, header {scan_head}")
        check("with something to do in it instead of an empty body",
              gws_cache.NO_TEXT in asyncio.run(
                  gws_cache.read_cache("scan.pdf.md")).get("text", ""),
              "the body tells them to ask for the figure")

        # A PDF with no pages is not a scan. `NO_TEXT` says the pages are
        # images, and there are no pages — so this lands where every other
        # unreadable binary lands, named and left as bytes.
        empty = Path(scratch) / "empty.pdf"
        empty.write_bytes(make_empty_pdf())
        got = record({"empty.pdf": empty.stat().st_mtime},
                     "gws drive files get -o empty.pdf "
                     '--params \'{"fileId": "ID-E"}\'')
        check("a PDF with no pages is not told to be a scan",
              got == ["empty.pdf"]
              and not (Path(scratch) / "empty.pdf.md").exists(),
              f"record kept {got}")

        # `-o` names the file and the model is what types the name, so the
        # extension is a guess and the first five bytes are not. The direction
        # that actually bit: an uncompressed PDF decodes as UTF-8 perfectly, so
        # dispatching on a `UnicodeDecodeError` filed one as a text document
        # whose body happened to begin `%PDF-1.4` — header written into the
        # bytes, no text copy, and the file corrupted on the way past.
        print("\n== which files are PDFs is decided by the bytes ==")
        odd = Path(scratch) / "statement.txt"
        body = make_pdf(["Balance 12 EUR"], ascii_only=True)
        odd.write_bytes(body)
        got = record({"statement.txt": odd.stat().st_mtime},
                     "gws drive files get -o statement.txt "
                     '--params \'{"fileId": "ID-ODD"}\'')
        check("a PDF saved under a text name is read as a PDF",
              got == ["statement.txt", "statement.txt.md"]
              and "Balance 12 EUR" in (Path(scratch) / "statement.txt.md")
              .read_text(encoding="utf-8"),
              f"record kept {got}")
        check("and is never headered as though it were the text",
              odd.read_bytes() == body,
              f"{odd.stat().st_size} bytes, unchanged")

        # And the read has to decide the same way the write did. `read_cache`
        # used to reach for the copy only from the `UnicodeDecodeError` branch
        # of its own read, so the one PDF shape that decodes — this one — never
        # got there: `read_text` succeeded on the bytes and the sub-agent was
        # handed `%PDF-1.4 / 1 0 obj / << /Type /Catalog` as the document's
        # text, with the copy holding the answer sitting beside it unread. The
        # model cannot tell that apart from a document that really does open
        # that way, so it answers out of PDF syntax.
        ascii_back = asyncio.run(gws_cache.read_cache("statement.txt"))
        check("and reading it back by that name gets the copy, not the bytes",
              ascii_back.get("name") == "statement.txt.md",
              f"{ascii_back.get('name')!r}, keys {sorted(ascii_back)}",
              ascii_back.get("name"), "statement.txt.md")
        check("so what comes back is the text and not the PDF's own source",
              "Balance 12 EUR" in ascii_back.get("text", "")
              and "/Type /Catalog" not in ascii_back.get("text", ""),
              f"text opens {ascii_back.get('text', '')[:30]!r}")

        # A second download landing on a name a PDF already holds. `paper.pdf`
        # is the instruction's own example filename, so this is the expected
        # case and not the exotic one — and the sidecar is the file the
        # instruction sends the sub-agent to. Left behind, it answers about the
        # previous document with the previous document's title and file_id, and
        # `_is_stale` cannot see it: the file it names really is unchanged.
        print("\n== a name that used to hold a different PDF ==")
        paper = Path(scratch) / "paper.pdf"
        paper.write_bytes(make_pdf(["ZIPAIR Tokyo", "Total JPY 128,400"]))
        record({"paper.pdf": paper.stat().st_mtime},
               'gws drive files get -o paper.pdf --params \'{"fileId": "ID-A"}\'')
        side = Path(scratch) / "paper.pdf.md"
        check("the first PDF gets its text copy as usual",
              "Total JPY 128,400" in side.read_text(encoding="utf-8"),
              f"{gws_cache._read_header(side).get('file_id')} on the copy")

        # Truncated past the xref: pypdf will not open it, so there is no new
        # text to overwrite the old copy with. An encrypted or a `.docx` one
        # lands the same way — `_from_pdf` returns None for all three.
        paper.write_bytes(make_pdf(["Nothing to see"])[:120])
        got = record({"paper.pdf": paper.stat().st_mtime},
                     'gws drive files get -o paper.pdf '
                     '--params \'{"fileId": "ID-B"}\'')
        check("a PDF that will not open takes the old copy with it",
              got == ["paper.pdf"] and not side.exists(),
              f"record kept {got}, copy exists {side.exists()}")

        paper.write_bytes(make_pdf(["Total JPY 999"]))
        record({"paper.pdf": paper.stat().st_mtime},
               'gws drive files get -o paper.pdf --params \'{"fileId": "ID-C"}\'')
        check("and the name reads back as whatever holds it now",
              "Total JPY 999" in asyncio.run(
                  gws_cache.read_cache("paper.pdf")).get("text", ""),
              "not 128,400, which is the wrong-document answer")

        # The other way in: a text file overwriting a name a PDF held. Nothing
        # in `_annotate`'s text branch would otherwise touch the copy.
        paper.write_bytes(b"an ordinary export now\n")
        got = record({"paper.pdf": paper.stat().st_mtime},
                     'gws drive files export -o paper.pdf '
                     '--params \'{"fileId": "ID-D"}\'')
        check("a text file at the name drops the copy too",
              got == ["paper.pdf"] and not side.exists(),
              f"record kept {got}, copy exists {side.exists()}")

        # And what it must not delete: an `invoice.md` exported from a Doc has
        # no `pdf:` in its header and is a document in its own right.
        keep = Path(scratch) / "keep.pdf"
        keep.write_bytes(make_pdf(["kept"]))
        keep_me = Path(scratch) / "keep.pdf.md"
        keep_me.write_text("---\ntitle: \"unrelated\"\n---\n\nmine\n",
                           encoding="utf-8")
        # A real `seen`, so this is the header test doing the work and not the
        # ownership guard below declining on a file that is not there.
        gws_cache._drop_sidecar(keep, keep.stat().st_mtime)
        check("an .md with no `pdf` header is left where it is",
              keep_me.exists(), "the header test is what separates the two")

        # The decision to call `_drop_sidecar` comes from bytes read outside
        # the lock, and `_from_pdf` is 101 ms on 60 pages. In that window
        # another command can land a good PDF at the name and write it a
        # correct copy; a late drop then destroys a valid document's only
        # readable form, invisible to `listing` and `search_cache` and healed
        # by nothing. Every other write in the module re-checks `seen` under
        # the lock — `_sidecar` does it one line after taking `writing()`.
        late = Path(scratch) / "late.pdf"
        late.write_bytes(b"%PDF-1.4 truncated, will not open\n")
        stale_seen = late.stat().st_mtime
        # The other command, in full: a real PDF at the name with a real copy.
        time.sleep(0.01)
        late.write_bytes(make_pdf(["Total JPY 128,400"]))
        record({"late.pdf": late.stat().st_mtime},
               "gws drive files get -o late.pdf "
               '--params \'{"fileId": "ID-LATE"}\'')
        late_side = Path(scratch) / "late.pdf.md"
        check("the second command's copy is written and holds the figure",
              late_side.exists()
              and "128,400" in late_side.read_text(encoding="utf-8"),
              f"copy exists {late_side.exists()}")
        gws_cache._drop_sidecar(late, stale_seen)
        check("and a drop arriving late for the file it replaced spares it",
              late_side.exists(),
              "the mtime it was called about is not the one on disk")
        back = asyncio.run(gws_cache.read_cache("late.pdf"))
        check("so the valid PDF still reads back through its copy",
              "128,400" in back.get("text", ""),
              f"{back.get('name')!r}, keys {sorted(back)}")

        # The same hazard on the write path, which is where it was missing.
        # `_still_ours` guards the PDF and says nothing about the file the
        # text goes into, so whatever was at the copy's name was overwritten.
        # A document really is there to overwrite: `_cache_name` rewrites only
        # `TEXT_SUFFIXES`, so a Doc exported with `-o minutes.pdf.md` lands on
        # exactly that name, and `-o minutes.pdf.txt` is flattened onto it too.
        # Silent in every direction — nothing logged, nothing raised, and the
        # row in the listing stayed where it was, carrying the PDF's identity.
        print("\n== the copy's name held by another document ==")
        minutes = Path(scratch) / "minutes.pdf"
        theirs = Path(scratch) / "minutes.pdf.md"
        exported = gws_cache._header({
            "source": "gws drive files export -o minutes.pdf.md",
            "fetched": "2026-09-08T09:00:00+09:00",
            "file_id": "ID-DOC", "modified": stamp,
            "title": "Board minutes", "summary": "the 2019 board minutes",
        }) + "BOARD MINUTES: the vote was 4-1.\n"
        theirs.write_text(exported, encoding="utf-8")
        minutes.write_bytes(make_pdf(["Invoice 7781", "Total JPY 42,000"],
                                     ascii_only=True))
        with Said() as said:
            got = record({"minutes.pdf": minutes.stat().st_mtime},
                         "gws drive files get -o minutes.pdf "
                         '--params \'{"fileId": "ID-INV"}\'')
        held = gws_cache._read_header(theirs)
        check("a fetch does not write its text over another document",
              theirs.read_text(encoding="utf-8") == exported,
              f"file_id {held.get('file_id')!r}, last line "
              f"{theirs.read_text(encoding='utf-8').splitlines()[-1]!r}")
        check("and names only the file it wrote, not one it left alone",
              got == ["minutes.pdf"], f"record kept {got}",
              got, ["minutes.pdf"])
        # The fetch succeeds and the bytes are cached, so a log line is the
        # only place the dropped text is visible to anyone but the sub-agent
        # that later asks for it.
        check("and says out loud that it threw the text away",
              said.lines == ["gws cache: minutes.pdf.md already holds another "
                             "document, so the text of minutes.pdf was not "
                             "written"],
              f"logged {said.lines}")

        # Intact on disk is not the whole of it. What the sub-agent matches a
        # question against is the row, and what it reads is the file under the
        # name in that row; after the overwrite both were the invoice.
        rows = [(r["name"], r.get("summary")) for r in gws_cache._records()]
        served = asyncio.run(gws_cache.read_cache("minutes.pdf.md"))
        check("so it keeps its own row, and answers under its own name",
              ("minutes.pdf.md", "the 2019 board minutes") in rows
              and "vote was 4-1" in served.get("text", ""),
              f"{[r for r in rows if r[0].startswith('minutes')]}")

        # And what the PDF's own name says now. The advice `read_cache` gives
        # on the `%PDF` fall-through is to fetch again and read the copy it
        # names — which is the stranger, and the fetch writes nothing to it, so
        # it is wrong twice over. `ascii_only` because that is the shape which
        # reaches this branch: a PDF with the usual binary comment on line two
        # fails the decode above and comes back as `Could not read it`.
        taken = asyncio.run(gws_cache.read_cache("minutes.pdf"))
        check("and the PDF's own name reports the copy's name as taken",
              "another document is already at `minutes.pdf.md`"
              in taken.get("error", "")
              and "vote was 4-1" not in str(taken),
              f"{taken.get('error', sorted(taken))!r}")

        # The control on the guard, and the thing it must not cost: a re-fetch
        # of the same PDF still replaces its own copy. The `pdf:` header is
        # what makes that a different question from the one above, and a guard
        # that asked only whether the name is free would refuse every refresh
        # and leave the cache answering out of the first download for ever.
        theirs.unlink()
        record({"minutes.pdf": minutes.stat().st_mtime},
               "gws drive files get -o minutes.pdf "
               '--params \'{"fileId": "ID-INV"}\'')
        minutes.write_bytes(make_pdf(["Invoice 7781", "Total JPY 55,000"],
                                     ascii_only=True))
        got = record({"minutes.pdf": minutes.stat().st_mtime},
                     "gws drive files get -o minutes.pdf "
                     '--params \'{"fileId": "ID-INV"}\'')
        check("a re-fetch of the same PDF still replaces its own copy",
              got == ["minutes.pdf", "minutes.pdf.md"]
              and "55,000" in theirs.read_text(encoding="utf-8"),
              f"record kept {got}")

        # And the two paths still answer the question the same way, which is
        # the whole of what was wrong: the delete path refused to touch a
        # document with no `pdf:` header of its own and the write path wrote
        # straight over one.
        theirs.write_text(exported, encoding="utf-8")
        gws_cache._drop_sidecar(minutes, minutes.stat().st_mtime)
        # `exists` first, so a delete path that stopped reading the header
        # fails this case rather than raising out of the script.
        spared = theirs.exists() and theirs.read_text(encoding="utf-8")
        check("and the delete path still spares what the write path spares",
              spared == exported,
              f"the document is {'where it was' if spared else 'gone'}")

        # Two records for one document spends one of LISTING_LIMIT on a bare
        # unreadable filename, and makes `named` ambiguous on every multi-word
        # PDF name — so the question that names the invoice out loud costs an
        # extra sub-agent turn to call `read_cache` by hand.
        print("\n== a PDF and its copy are one document ==")
        gws_cache._summarize = lambda name, text: "ZIPAIR flight invoice"
        for stale in Path(scratch).iterdir():
            stale.unlink()
        zip_pdf = Path(scratch) / "zipair-invoice.pdf"
        zip_pdf.write_bytes(make_pdf(["ZIPAIR Tokyo", "Total JPY 128,400"]))
        record({"zipair-invoice.pdf": zip_pdf.stat().st_mtime},
               "gws drive files get -o zipair-invoice.pdf "
               '--params \'{"fileId": "ID-Z"}\'')
        rows = [r["name"] for r in gws_cache._records()]
        check("one row, and it is the one with the title on it",
              rows == ["zipair-invoice.pdf.md"], f"records {rows}",
              rows, ["zipair-invoice.pdf.md"])
        hit = gws_cache.named("what's the total on the zipair invoice")
        check("the question that names it out loud finds it",
              hit == "zipair-invoice.pdf.md",
              "the copy's own stem is `zipair-invoice.pdf`, and nobody says "
              '"pdf" out loud', hit, "zipair-invoice.pdf.md")

        # Everything that is not a PDF lands where it always did. `.xlsx` and
        # `.docx` are the next ones worth reading and this is the seam they go
        # through; guessing at them now would be a header on a zip file.
        sheet = Path(scratch) / "book.xlsx"
        sheet.write_bytes(b"PK\x03\x04\xff\xfe not a pdf\n")
        got = record({"book.xlsx": sheet.stat().st_mtime},
                     "gws drive files get -o book.xlsx "
                     '--params \'{"fileId": "ID-XL"}\'')
        check("another binary is still named, and still gets no copy",
              got == ["book.xlsx"]
              and not (Path(scratch) / "book.xlsx.md").exists(),
              f"record kept {got}")
        # And the redirect only fires where there is something to redirect to.
        # A binary with no copy beside it is the error it always was, not a
        # `FileNotFoundError` out of the fall-through.
        try:
            xl_back = asyncio.run(gws_cache.read_cache("book.xlsx"))
        except OSError as exc:
            # `read_cache` is a tool the model calls; raising out of one is a
            # dead turn, not an error message. Reported rather than allowed to
            # take this script out.
            xl_back = {"error": f"raised {exc!r}"}
        check("and reading it back is still the error it always was",
              "codec can't decode" in xl_back.get("error", ""),
              f"{xl_back.get('error', xl_back)!r}")

        # The redirect has to apply the same `pdf:` test the write path does,
        # or *any* `.md` at that name answers for the PDF. `_cache_name`
        # rewrites only `TEXT_SUFFIXES`, so a Doc exported with `-o memo.pdf.md`
        # lands on exactly the sidecar's name and `_annotate` — looking for
        # `memo.pdf.md.md` — never touches it. The sub-agent asked by the PDF's
        # name and would be handed the other document's text with `name`
        # changed underneath it.
        memo = Path(scratch) / "memo.pdf"
        memo_side = Path(scratch) / "memo.pdf.md"
        memo.write_bytes(make_pdf(["Total JPY 128,400"]))
        memo_side.write_text(gws_cache._header({
            "source": "gws drive files export -o memo.pdf.md",
            "fetched": "2026-09-09T00:00:00+09:00",
            "file_id": "ID-DOC",
        }) + "Minutes of the 2019 board meeting. No dividend.\n",
            encoding="utf-8")
        wrong = asyncio.run(gws_cache.read_cache("memo.pdf"))
        check("an unrelated .md at the copy's name does not answer for the PDF",
              "error" in wrong and "dividend" not in str(wrong),
              f"{wrong.get('name')!r}, keys {sorted(wrong)}")

        # And the same branch when the copy will not decode. `read_cache` is a
        # tool the model calls, and `except OSError` did not catch a
        # `UnicodeDecodeError`, so this was a traceback out of one.
        #
        # Padded past `open()`'s 8 KB read-ahead on purpose: below that the
        # invalid byte arrives in the same buffered chunk as the header,
        # `_read_header` gives up first and the case above is the one that
        # fires. A copy whose tail is corrupt is what reaches the read.
        memo_side.write_bytes(gws_cache._header({
            "source": "gws drive files get -o memo.pdf",
            "fetched": "2026-09-09T00:00:00+09:00",
            "pdf": "memo.pdf",
        }).encode("utf-8") + b"padding.\n" * 2000 + b"\xff\xfe\x00\n")
        check("a copy corrupt past the header still passes the header test",
              gws_cache._read_header(memo_side).get("pdf") == "memo.pdf",
              "or the case below would be the test above again")
        try:
            binary = asyncio.run(gws_cache.read_cache("memo.pdf"))
        except Exception as exc:  # pylint: disable=broad-except
            binary = {"error": f"raised {exc!r}"}
        check("nor does a copy that will not decode, and it does not raise",
              "codec can't decode" in binary.get("error", "")
              and "raised" not in binary.get("error", ""),
              f"{binary.get('error', binary)!r}")

        # The decodable twin of the two above, which is the state the header
        # test cannot rescue: it correctly refuses the `.md` — that document is
        # not this PDF's copy — and then the PDF's own bytes read as text
        # because they are ASCII. `ascii_only` is the whole difference from
        # `:1247`, and without the magic-byte gate this comes back as `text`
        # with no `error` key at all, which is the one shape the sub-agent acts
        # on.
        memo_side.write_text(gws_cache._header({
            "source": "gws drive files export -o memo.pdf.md",
            "fetched": "2026-09-09T00:00:00+09:00",
            "file_id": "ID-DOC",
        }) + "Minutes of the 2019 board meeting. No dividend.\n",
            encoding="utf-8")
        memo.write_bytes(make_pdf(["Total JPY 128,400"], ascii_only=True))
        plain = asyncio.run(gws_cache.read_cache("memo.pdf"))
        check("nor one whose bytes happen to decode, unrelated .md and all",
              "error" in plain and "%PDF" not in str(plain),
              f"{plain.get('name')!r}, keys {sorted(plain)}")

        # And with no `.md` at that name at all, which is the same fall-through
        # by the shorter route: nothing to redirect to, bytes that decode.
        solo = Path(scratch) / "solo.pdf"
        solo.write_bytes(make_pdf(["Total JPY 128,400"], ascii_only=True))
        alone = asyncio.run(gws_cache.read_cache("solo.pdf"))
        check("and a decodable PDF with no copy beside it is an error too",
              "error" in alone and "%PDF" not in str(alone),
              f"{alone.get('name')!r}, keys {sorted(alone)}")
        check("which names the copy the sub-agent should go and fetch",
              "solo.pdf.md" in alone.get("error", ""),
              f"{alone.get('error', alone)!r}")
        solo.unlink()

        # The same two shapes again with the bytes a real writer produces,
        # because every case above them passes `ascii_only=True` and that is
        # not an incidental choice. Line two of a PDF is a binary comment, put
        # there so that anything treating the file as text gives up early, so
        # the bytes Drive actually returns do not decode — and a `read_cache`
        # that decodes before it dispatches answers `Could not read it` to all
        # of them. Both messages above would then be unreachable on the only
        # input there is, and a suite that only ever feeds them ASCII would
        # never say so.
        memo.write_bytes(make_pdf(["Total JPY 128,400"]))
        real = asyncio.run(gws_cache.read_cache("memo.pdf"))
        check("a PDF whose bytes do not decode gets that answer too",
              "already at `memo.pdf.md`" in real.get("error", ""),
              f"{real.get('error', real)!r}")

        solo.write_bytes(make_pdf(["Total JPY 128,400"]))
        undecodable = asyncio.run(gws_cache.read_cache("solo.pdf"))
        check("and so does one with no copy beside it, by its own name",
              "solo.pdf.md" in undecodable.get("error", "")
              and "codec" not in undecodable.get("error", ""),
              f"{undecodable.get('error', undecodable)!r}")
        solo.unlink()

        # Which is the reason `gws.py:794` cannot log `error`: the advice
        # names a file on three of the four refusals that reach it — the two
        # above, and the whole path off the SD card inside `Could not read
        # it`. `why` is the part of a refusal that may be logged, and every
        # refusal carries one, so the line has something to say. #234.
        absent = asyncio.run(gws_cache.read_cache("no-such-copy.md"))
        tagged = [real.get("why"), undecodable.get("why"),
                  binary.get("why"), absent.get("why")]
        check("every refusal says why in a word that names no file",
              tagged == ["pdf-name-taken", "pdf-no-copy", "unreadable",
                         "missing"], f"tags {tagged}")
        # And the advice keeps the filename, because that is what the
        # sub-agent acts on: the split is between what the model is told and
        # what the journal keeps, not between saying the name and not.
        check("while the advice each one gives still names the copy to fetch",
              "memo.pdf.md" in real.get("error", "")
              and "solo.pdf.md" in undecodable.get("error", ""),
              f"{real.get('error', '')!r} / "
              f"{undecodable.get('error', '')!r}")

        # The other side of that gate, twice, because the question it asks is
        # "are these bytes a PDF" and there are two cheaper questions it must
        # not be asking instead. A document that *writes about* the format
        # carries the magic somewhere in its body — this cache holds the
        # terminal's own notes, so `%PDF-` in a paragraph is a document that
        # exists — and matching anywhere would refuse it.
        formats = Path(scratch) / "formats.md"
        formats.write_text(gws_cache._header({
            "source": "gws drive files export -o formats.md",
            "fetched": "2026-09-09T00:00:00+09:00",
            "file_id": "ID-NOTE",
        }) + "Every PDF opens with the five bytes %PDF- and a version.\n",
            encoding="utf-8")
        read = []
        told = journalled(
            "ai_terminal.live.tools.gws_cache",
            lambda: read.append(asyncio.run(gws_cache.read_cache(
                "formats.md"))))
        note = read[0]
        check("a document that writes about the magic is not one of them",
              "%PDF- and a version" in note.get("text", ""),
              f"{note.get('error', sorted(note))!r}")
        # The other of the pair, on the path that succeeds — which is the one
        # that fires on an ordinary day. #234.
        check("and a copy that reads back is not named on the way out either",
              "formats.md" not in told, told)
        check("that one being a size too", "<10 chars>" in told, told)

        # And the magic is five bytes, not one. A Pandoc title block opens on
        # `%`, and a copy written by hand has no header for the reader to tell
        # it apart by, so the first byte alone would refuse it.
        title = Path(scratch) / "talk.md"
        title.write_text("% Grounding at Next\n% Alex\n\nSlide one.\n",
                         encoding="utf-8")
        pandoc = asyncio.run(gws_cache.read_cache("talk.md"))
        check("nor is a title block, which opens on the first byte only",
              "Slide one." in pandoc.get("text", ""),
              f"{pandoc.get('error', sorted(pandoc))!r}")
        for gone in (formats, title):
            gone.unlink()

        # The control for both: a real sidecar at that name still answers. The
        # header test must not have closed the door it was added behind.
        for stale in (memo, memo_side):
            stale.unlink()
        memo.write_bytes(make_pdf(["Total JPY 128,400"]))
        record({"memo.pdf": memo.stat().st_mtime},
               "gws drive files get -o memo.pdf "
               '--params \'{"fileId": "ID-MEMO"}\'')
        right = asyncio.run(gws_cache.read_cache("memo.pdf"))
        check("while the copy this PDF was read out of still answers",
              right.get("name") == "memo.pdf.md"
              and "128,400" in right.get("text", ""),
              f"{right.get('name')!r}, keys {sorted(right)}")

        # Measured against the real account on 2026-09-09: this one method
        # ignores `-o`, writes no file at all, and prints 168 047 characters of
        # base64url JSON for a 126 KB PDF. Nothing downstream can cache a file
        # that was never written, so the attachment path was blocked before any
        # of the above could run — and showing the sub-agent the base64 instead
        # would spend its whole context on a document it cannot decode.
        print("\n== a Gmail attachment arrives as base64, not as a file ==")
        raw = make_pdf(["Folio total 42 EUR"])
        blob = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        attach = gws_cache.rewrite_output(
            ["gmail", "users", "messages", "attachments", "get",
             "-o", "folio.pdf", "--params",
             '{"userId": "me", "messageId": "M1", "id": "A1"}'])
        with gws_cache.writing():
            before = gws_cache.snapshot()
            wrote = gws_cache.unpack(attach, json.dumps({"data": blob}))
            written = gws_cache.written_by(before)
        check("the attachment is written out under the name -o asked for",
              wrote == "folio.pdf"
              and (Path(scratch) / "folio.pdf").read_bytes() == raw,
              f"unpack wrote {wrote!r}")
        check("inside the diff, so record sees it like any other download",
              sorted(written) == ["folio.pdf"],
              f"the diff saw {sorted(written)}")

        check("a command that is not an attachment get is left alone",
              gws_cache.unpack(["drive", "files", "get", "-o", "folio.pdf"],
                               json.dumps({"data": blob})) == "",
              "only the one method answers with the document inline")
        check("nor is one the caller passed no -o on",
              gws_cache.unpack(
                  ["gmail", "users", "messages", "attachments", "get",
                   "--params", "{}"], json.dumps({"data": blob})) == "",
              "there is no name to write it under")
        check("and an error on stdout costs the name, not the turn",
              gws_cache.unpack(attach, '{"error": {"code": 404}}') == "",
              "no attachment in the JSON, and nothing raised")
        # `urlsafe_b64decode` validates nothing, so a `data` that is not base64
        # comes back as zero bytes rather than raising. Reported as saved, that
        # sends the sub-agent to read a document that is not there.
        check("nor does a `data` that is not base64 at all count as saved",
              gws_cache.unpack(attach, json.dumps({"data": "!!!!"})) == ""
              and not (Path(scratch) / "folio.pdf").read_bytes() == b"",
              "zero bytes decoded is not an attachment")

        # The CLI writes a line about its keyring backend ahead of the payload
        # — `_gws_cli.run_json` starts its parse at the first brace for exactly
        # that reason, and `_run_sync` hands this function the same stdout. A
        # whole-string parse raises `ValueError` on it, which reads here as "no
        # attachment": the file is never written, the base64 goes to the
        # sub-agent instead, and the one document most likely to hold the
        # number being asked for is lost on whichever machines print the line
        # and on no others.
        second = gws_cache.rewrite_output(
            ["gmail", "users", "messages", "attachments", "get",
             "-o", "ticket.pdf", "--params",
             '{"userId": "me", "messageId": "M2", "id": "A2"}'])
        noisy = ("Using keyring backend: SecretService\n"
                 + json.dumps({"data": blob}))
        check("a line of the CLI's own ahead of the payload is skipped, not "
              "read as a missing attachment",
              gws_cache.unpack(second, noisy) == "ticket.pdf"
              and (Path(scratch) / "ticket.pdf").read_bytes() == raw,
              "the same first-brace rule `run_json` parses by")
        check("and stdout with no JSON on it at all is still only a miss",
              gws_cache.unpack(second, "gws: no such command") == "",
              "nothing to decode, and nothing raised")

        # A global flag written before the subcommand is a line the CLI takes,
        # and `takewhile` from the front returned an empty tuple for it — so
        # the attachment went unrecognised and the base64 stayed on stdout.
        check("a global flag before the subcommand does not hide it",
              gws_cache._cli_words(
                  ["--format", "json", "gmail", "users", "messages",
                   "attachments", "get", "-o", "f.pdf"])
              == gws_cache._ATTACHMENT,
              "the subcommand words are the same wherever they start")
        check("and a flag's value is not read as a subcommand word",
              gws_cache._cli_words(["--format", "json", "drive", "files"])
              == ("drive", "files"),
              "`json` belongs to --format, not to the command")
        check("and a flag with no value does not eat the next word",
              gws_cache._cli_words(["--page-all", "drive", "files"])
              == ("drive", "files"),
              "--page-all is not in VALUED, so `drive` is not its value")
        check("a flag's value after the command is not a command word",
              gws_cache._cli_words(
                  ["drive", "files", "get", "-o", "invoice.pdf"])
              == ("drive", "files", "get"), "the run ends at the first flag")

        # The whole path, through the function the tool actually calls. What
        # the sub-agent is handed has to be the filename and not the base64:
        # 168 047 characters is past `MAX_OUTPUT`, so it would arrive truncated
        # as well as unusable.
        class Answered:
            """`subprocess` for one call, answering as the Gmail API does."""

            TimeoutExpired = subprocess.TimeoutExpired

            @staticmethod
            def run(*args, **kwargs):
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"data": blob}), "")

        real_subprocess = _gws_run.subprocess
        _gws_run.subprocess = Answered
        try:
            result, held = _gws_run._run_sync(
                ["gmail", "users", "messages", "attachments", "get",
                 "-o", "note.pdf", "--params", '{"userId": "me"}'],
                "gws gmail users messages attachments get -o note.pdf")
        finally:
            _gws_run.subprocess = real_subprocess
        gws_cache.finish(held)
        check("the tool result names the file instead of quoting the base64",
              result["stdout"] == gws_cache.UNPACKED.format(name="note.pdf"),
              f"{len(result['stdout'])} chars of stdout, not {len(blob)}")
        check("and cached names both the PDF and the text read out of it",
              result.get("cached") == ["note.pdf", "note.pdf.md"],
              f"cached {result.get('cached')}",
              result.get("cached"), ["note.pdf", "note.pdf.md"])

        # ---------------------------------------------------------------
        # #233. The header's first field used to be the whole command line,
        # `--query` payload and all, so the sentence somebody said to the
        # terminal ended up on the SD card in a store neither *Forget* nor the
        # journal's retention reaches. These are checked on the bytes and not
        # on the parsed header: the point is what is on the card, and a field
        # the parser skips is still a field somebody can read.
        print("\n== the household's words are not written to the card ==")

        for path in Path(scratch).iterdir():
            path.unlink()

        # One per path that writes a header. There is no fifth: `_meta_fields`
        # has exactly two callers, `_annotate` for a text export and `_sidecar`
        # for the copy read out of a PDF, and `record` reaches both.
        paths = {
            "a drive export": (
                "kept.md", b"the letter\n",
                f'gws drive files export -o kept.md --params \'{{"fileId": '
                f'"ID-1", "mimeType": "text/markdown", "q": "{SENTINEL}"}}\''),
            "a drive search written to a file": (
                "found.md", b"[]\n",
                f'gws drive files list -o found.md --params "{{\\"q\\": '
                f'\\"fullText contains \'{SENTINEL}\'\\"}}"'),
            "a gmail attachment": (
                "bill.md", b"the invoice\n",
                f'gws gmail users messages attachments get -o bill.md '
                f'--params \'{{"userId": "me", "messageId": "M1", "id": "A1"}}\''
                f' --query \'{SENTINEL}\''),
            "the copy read out of a PDF": (
                "scan.pdf", make_pdf(["the invoice"]),
                f'gws drive files get -o scan.pdf --params \'{{"fileId": '
                f'"ID-2", "alt": "media"}}\' --query \'{SENTINEL}\''),
        }
        for label, (name, body, command) in paths.items():
            written = Path(scratch) / name
            written.write_bytes(body)
            record({name: written.stat().st_mtime}, command)
            # The PDF's header is on the copy beside it, which is the file the
            # listing and the search read.
            headed = (written if not name.endswith(".pdf")
                      else written.with_name(name + ".md"))
            check(f"{label}: nothing on disk holds what they said",
                  SENTINEL.encode() not in headed.read_bytes(),
                  f"{headed.name} is {headed.stat().st_size} bytes")
            check(f"{label}: and the header still says what fetched it",
                  gws_cache._read_header(headed).get("source", "")
                  .startswith("gws "),
                  gws_cache._read_header(headed).get("source", ""))

        check("and the index rebuilt from those headers holds none of it "
              "either",
              SENTINEL.encode()
              not in (Path(scratch) / gws_cache.INDEX).read_bytes(),
              "index.md is written from the same fields")

        # A redaction that fails open is worse than none, because it is the one
        # you stop watching. Anything not named is elided, so a flag or a
        # `--params` key nobody thought of costs fidelity and never silence.
        check("a flag nobody has named keeps its name and loses its value",
              gws_cache.redacted(f"gws gmail +triage --subject '{SENTINEL}'")
              == "gws gmail +triage --subject '<elided>'",
              gws_cache.redacted(f"gws gmail +triage --subject '{SENTINEL}'"))
        check("and so does a --params key nobody has named",
              SENTINEL not in gws_cache.redacted(
                  f'gws drive files list --params \'{{"newKey": "{SENTINEL}"}}\''),
              "the allowlist is the whole rule")
        check("a value that is also a subcommand word is still a value",
              gws_cache.redacted("gws gmail +search --query drive")
              == "gws gmail +search --query '<elided>'",
              "`_word_indices` keeps the words in place, not by name")

        # The spelling that has to work, and the one the rest of the redaction
        # very nearly got wrong. A Gmail query is a run of space-free operators
        # — `is:unread from:boss subject:invoice` — so the sub-agent writes it
        # without quotes and `shlex` hands back one token per operator. Both
        # halves of `redacted` were sized for a value of one token: `--query`
        # was not in `VALUED`, so nothing skipped the first operator, and
        # `_word_indices` took the *longest* run of plain words, so from four
        # operators on the query outran `gmail +triage` and became the
        # subcommand. The whole query stayed on the card and the words the
        # re-fetch needs went out — one bug printing both of #233's failures.
        for spelling, lead, want in (
            ("gws gmail +triage --query is:unread",
             ["gmail", "+triage"],
             "gws gmail +triage --query '<elided>'"),
            ("gws gmail +search --query is:unread from:boss subject:invoice",
             ["gmail", "+search"],
             "gws gmail +search --query '<elided>' '<elided>' '<elided>'"),
            ("gws gmail +triage --query is:unread from:boss subject:invoice "
             "has:attachment after:2026/01/01",
             ["gmail", "+triage"],
             "gws gmail +triage --query '<elided>' '<elided>' '<elided>' "
             "'<elided>' '<elided>'"),
            ("gws drive files list -o out.md --query trip a b c",
             ["drive", "files", "list"],
             "gws drive files list -o out.md --query '<elided>' '<elided>' "
             "'<elided>' '<elided>'"),
        ):
            got = gws_cache.redacted(spelling)
            argv = shlex.split(spelling)
            operators = argv[argv.index("--query") + 1:]
            check(f"an unquoted query of {len(operators)} operators loses "
                  f"every one of them", got == want, got, got, want)
            # The second failure the same bug caused, and the one eliding more
            # would never have fixed: what the longest-run rule elided *was*
            # the command, so `ALREADY_STALE` handed the sub-agent a line it
            # could not run. Read off the front of the redaction, which is
            # where `gws <service> <resource|+shortcut>` always is.
            check("  and the words the re-fetch is written from survive",
                  shlex.split(got)[1:1 + len(lead)] == lead,
                  " ".join(shlex.split(got)[1:1 + len(lead)]),
                  shlex.split(got)[1:1 + len(lead)], lead)

        # `--max` is the one `_SHOWN_FLAGS` entry that is also a `+shortcut`
        # flag, so it is the one that needs `VALUED` to be reachable: without
        # it the `5` is read as a stray word and elided, and the allowlist
        # entry is dead for every spelling but `--max=5`.
        spaced = gws_cache.redacted("gws gmail +triage --max 5")
        joined = gws_cache.redacted("gws gmail +triage --max=5")
        check("a count is kept, written either way",
              [spaced, joined] == ["gws gmail +triage --max 5",
                                   "gws gmail +triage --max=5"],
              f"{spaced!r} and {joined!r}", [spaced, joined],
              ["gws gmail +triage --max 5", "gws gmail +triage --max=5"])

        # Fails closed, and these are the lines that say so. Every flag that
        # can legally precede the service is a documented global and every one
        # of those is in `VALUED`, so the first run of plain words is the
        # command in any line gws will take — including one that opens on a
        # global, which is why the run cannot simply be taken from the front.
        LEADING = ("gws --format json drive files export -o kept.md --params "
                   '\'{"fileId": "ID-1", "mimeType": "text/markdown"}\'')
        check("a leading global still leaves the command readable",
              gws_cache.redacted(LEADING) == LEADING,
              gws_cache.redacted(LEADING), gws_cache.redacted(LEADING),
              LEADING)
        nothing = gws_cache.redacted(
            f'gws --page-all drive files list -o out.md '
            f'--params \'{{"q": "{SENTINEL}"}}\'')
        check("and a flag that takes nothing does not eat the service",
              nothing == ('gws --page-all drive files list -o out.md '
                          '--params \'{"q": "<elided>"}\''),
              nothing, nothing,
              'gws --page-all drive files list -o out.md '
              '--params \'{"q": "<elided>"}\'')

        torn = gws_cache.redacted(
            f"gws drive files list --params '{SENTINEL}")
        check("and a command line that will not even split says nothing",
              torn == "gws '<elided>'",
              "shlex cannot read it, so nothing here can vouch for a part")
        check("redacting twice changes nothing the second time",
              gws_cache.redacted(gws_cache.redacted(EXPORT))
              == gws_cache.redacted(EXPORT),
              "which is what `migrate` decides on")
        # The same property on the branch that does not go through the loop.
        # Unquoted, `gws <elided>` splits into a word the second pass quotes,
        # so `migrate` finds a difference and rewrites the header on every
        # start — a file that changes forever, on the one input nobody has a
        # fixed line to compare against.
        check("including the line that would not split",
              gws_cache.redacted(torn) == torn, torn)

        # `gws.py:780-781` is the reader that is not a diagnostic: the stored
        # command goes into a prompt as the way to fetch the document again.
        # It is reachable only for a copy `_is_stale` refused, and `_is_stale`
        # returns False without a `file_id` — so every command that reaches it
        # names an id, and an id is the one thing the redaction keeps.
        print("\n== a stale copy can still say how to fetch itself ==")
        check("_is_stale needs a file_id, so a header without one never gets "
              "as far as the prompt",
              gws_cache._is_stale({"source": "gws drive files export",
                                   "fetched": "2026-09-11T00:00:00+09:00"})
              is False, "no id, no freshness check, no ALREADY_STALE")

        gws_cache._drive_meta = lambda file_id: {
            "name": f"title of {file_id}", "mimeType": "text/markdown",
            "modifiedTime": "2099-01-01T00:00:00.000Z"}
        stale = asyncio.run(gws_cache.read_cache("kept.md"))
        told = gws.ALREADY_STALE.format(name="kept.md",
                                        source=stale.get("source", ""))
        again = shlex.split(stale.get("source", ""))
        check("the prompt the sub-agent is handed carries a command",
              bool(stale.get("source")) and "kept.md" in told,
              f"source {stale.get('source')!r}")
        check("and that command still names the document and the export it "
              "wants",
              gws_cache._identity(stale["source"]) == "ID-1"
              and "text/markdown" in stale["source"]
              and "-o" in again and "kept.md" in again,
              stale["source"])
        check("while the words that fetched it are gone from the prompt too",
              SENTINEL not in told, f"{len(told)} characters of prompt")

        # ---------------------------------------------------------------
        # A cache carried over from the build before this one still holds the
        # whole command line, and `_ours` reads it straight back into that
        # prompt. On read would leave the bytes on the card, which is the half
        # `Forget` cannot reach, so the rewrite has to be the file.
        print("\n== headers an older build wrote ==")
        old = Path(scratch) / "older.md"
        legacy = gws_cache._header({
            "source": OLD_STYLE,
            "fetched": "2026-09-01T00:00:00+09:00",
            "file_id": "ID-9",
            "summary": "a letter",
        }) + "the letter\n"
        old.write_text(legacy, encoding="utf-8")
        os.utime(old, (1_700_000_000, 1_700_000_000))
        check("before: the old header holds what they said",
              SENTINEL.encode() in old.read_bytes(), "which is the state today")

        moved = gws_cache.migrate()
        after = gws_cache._read_header(old)
        check("migrate rewrites it", moved == 1, f"{moved} file(s) rewritten",
              moved, 1)
        check("and the words are off the card", SENTINEL.encode()
              not in old.read_bytes(), "the file, not the read")
        check("with the rest of the header kept",
              (after.get("file_id"), after.get("summary"))
              == ("ID-9", "a letter"),
              f"file_id={after.get('file_id')} summary={after.get('summary')!r}")
        check("and the document itself untouched",
              old.read_text(encoding="utf-8").endswith("the letter\n"),
              "a migration that ate the body would be the worse bug")
        check("and the mtime put back, so eviction order does not move",
              old.stat().st_mtime == 1_700_000_000,
              f"mtime {old.stat().st_mtime}", old.stat().st_mtime,
              1_700_000_000)
        check("a second start rewrites nothing", gws_cache.migrate() == 0,
              "redacting twice is a no-op, so there is nothing to do")

        # ---------------------------------------------------------------
        # The case above builds its "old" header with `_header`, so every value
        # in it is JSON and `_read_header` can read it. The headers actually on
        # the card are not that shape: they quote `source` and leave `fetched`
        # bare, `_fields` drops the unquoted one, `_ours` then says the block is
        # not ours, and `migrate` walks past it. Measured on pi2 at `24330ff`,
        # where all six headers were this shape and the deploy migrated none of
        # them — a fix green in twenty scripts and inert on the device. What
        # those six held was luck; the fixture is what decides whether the next
        # six are.
        print("\n== and headers from before every value was JSON ==")
        older = Path(scratch) / "unquoted.md"
        older.write_text(
            '---\n'
            f'source: {json.dumps(OLD_STYLE)}\n'
            'fetched: 2026-09-01T00:00:00+09:00\n'
            '---\n\nthe older letter\n', encoding="utf-8")
        os.utime(older, (1_700_000_001, 1_700_000_001))
        check("the reader the rest of the module uses cannot see it",
              gws_cache._read_header(older) == {},
              "which is why nothing else may read a header this way")
        moved = gws_cache.migrate()
        check("migrate reads it anyway", moved == 1,
              f"{moved} file(s) rewritten", moved, 1)
        check("and the words are off the card",
              SENTINEL.encode() not in older.read_bytes(),
              "the one that was still true on pi2 after the first deploy")
        check("with the header left in a shape the module can read",
              gws_cache._read_header(older).get("fetched")
              == "2026-09-01T00:00:00+09:00",
              "normalising it is what stops this repeating every start")
        check("the document itself kept",
              older.read_text(encoding="utf-8").endswith("the older letter\n"),
              "the body is what the sub-agent reads")
        check("the mtime put back",
              older.stat().st_mtime == 1_700_000_001,
              f"mtime {older.stat().st_mtime}")
        check("and a second start rewrites nothing",
              gws_cache.migrate() == 0,
              "normalised once, ordinary from then on")

        # An old header whose command line holds nothing to elide is still an
        # old header: until it is normalised `_read_header` returns nothing for
        # it, so `_is_stale` never sees its `fetched` and the index never sees
        # its title. Rewriting only the ones that change would leave those
        # unreadable forever, and no run would ever say so.
        plain = Path(scratch) / "plain.md"
        # Written by `redacted` rather than by hand, because the case is about
        # a source it would leave alone and a line that merely looks redacted
        # still comes back differently quoted.
        innocuous = gws_cache.redacted(
            'gws drive files export -o plain.md --params \'{"fileId": "ID-7"}\'')
        check("a command line with nothing in it to elide survives redaction",
              gws_cache.redacted(innocuous) == innocuous, innocuous)
        plain.write_text(
            f'---\nsource: {json.dumps(innocuous)}\n'
            'fetched: 2026-09-02T00:00:00+09:00\n'
            '---\n\nnothing to hide\n', encoding="utf-8")
        check("an older header with nothing to redact is normalised anyway",
              gws_cache.migrate() == 1,
              "the shape is the thing that is wrong with it")
        check("and the module can read it from then on",
              gws_cache._read_header(plain).get("fetched")
              == "2026-09-02T00:00:00+09:00",
              "which is what `_is_stale` and the index were missing")

        # The leniency is why the next three matter. Dropping the JSON test is
        # dropping most of what tells a header of ours from a document's own
        # front matter, and `_without_header` deletes what it decides is ours —
        # so `_was_ours` asks for two things instead, and one case per clause,
        # each fixture failing only that clause.
        theirs = Path(scratch) / "jekyll.md"
        front = ('---\nsource: my own note\n'
                 'fetched: 2026-09-01T00:00:00+09:00\n---\n\ntheir post\n')
        theirs.write_text(front, encoding="utf-8")
        check("a source that is not a gws command line is somebody else's",
              gws_cache._legacy_header(theirs) == {},
              "unquoted is not enough on its own")

        dated = Path(scratch) / "dated.md"
        undated = ('---\nsource: "gws drive files list"\n'
                   'fetched: last Tuesday\n---\n\ntheir other post\n')
        dated.write_text(undated, encoding="utf-8")
        check("and neither is one whose fetched is not a time",
              gws_cache._legacy_header(dated) == {},
              "both clauses carry a fixture of their own")

        check("so migrate leaves both alone",
              gws_cache.migrate() == 0
              and theirs.read_text(encoding="utf-8") == front
              and dated.read_text(encoding="utf-8") == undated,
              "a migration that rewrote people's documents would be worse")
        theirs.unlink()
        dated.unlink()

        # A document whose own front matter satisfies both clauses: column-one
        # `source` beginning with `gws ` (unquoted, pinning that quotes are not
        # required) and column-one ISO-shaped `fetched`. It also carries
        # non-string values and nested list items. `migrate` must rewrite it to
        # redact the command line while carrying every unrecognised key and
        # list item through verbatim.
        both = Path(scratch) / "both.md"
        both_content = (
            "---\n"
            f"source: {OLD_STYLE}\n"
            "fetched: 2026-01-02T09:00:00\n"
            "draft: true\n"
            "order: 3\n"
            "tags:\n"
            "  - drive\n"
            "  - backup\n"
            "---\n\n"
            "their document with its own front matter\n"
        )
        both.write_text(both_content, encoding="utf-8")
        os.utime(both, (1_700_000_002, 1_700_000_002))

        check("an unquoted source beginning with gws satisfies clause 1 without quotes",
              gws_cache._was_ours(gws_cache._loose_fields(gws_cache._header_block(both))),
              "quotes are not required to match _was_ours")

        check("the reader the rest of the module uses cannot see it before migration",
              gws_cache._read_header(both) == {},
              "unquoted fetched keeps _read_header away")

        moved = gws_cache.migrate()
        check("migrate rewrites the legacy header", moved == 1,
              f"{moved} file(s) rewritten", moved, 1)

        check("and the words are off the card",
              SENTINEL.encode() not in both.read_bytes(),
              "the command line was redacted")

        body_text = both.read_text(encoding="utf-8")
        check("every non-string key and list item survives migrate",
              "draft: true" in body_text
              and "order: 3" in body_text
              and "tags:" in body_text
              and "  - drive" in body_text
              and "  - backup" in body_text,
              "unrecognised front matter lines carried through verbatim")

        check("with the cache keys normalised so the module can read them",
              gws_cache._read_header(both).get("fetched") == "2026-01-02T09:00:00"
              and "gws " in gws_cache._read_header(both).get("source", ""),
              "normalised to JSON strings")

        check("and the document itself untouched",
              body_text.endswith("their document with its own front matter\n"),
              "the body is preserved")

        check("and a second migrate rewrites nothing",
              gws_cache.migrate() == 0,
              "idempotent: zero files rewritten on second pass")

        both.unlink()

        # ---------------------------------------------------------------
        # The other half, and #206 decided it: a button that says the terminal
        # has forgotten, over a store that goes on remembering, is the defect
        # wherever the store is. Nothing else here ever deletes a document —
        # `_evict` waits for 200 newer ones, deliberately not for a clock.
        print("\n== Forget reaches the cache ==")
        (Path(scratch) / gws_cache._LOCK).touch()
        before_forget = sorted(p.name for p in Path(scratch).iterdir()
                               if gws_cache._cached(p))
        dropped = gws_cache.forget()
        left = sorted(p.name for p in Path(scratch).iterdir())
        # Equality and not "nothing left that looks like a document": the
        # index has to still be *there*, empty, and a check that only counts
        # what is gone passes just as happily when `forget` deletes it and
        # nothing writes it back.
        check("every cached document goes", dropped == len(before_forget)
              and left == sorted((gws_cache._LOCK, gws_cache.INDEX)),
              f"{dropped} of {len(before_forget)} gone, {left} left",
              left, sorted((gws_cache._LOCK, gws_cache.INDEX)))
        check("the index is rewritten rather than left naming them",
              gws_cache.listing() == gws_cache.EMPTY,
              "a listing of files that are gone is the next wrong answer")
        check("and the lock survives it",
              (Path(scratch) / gws_cache._LOCK).exists(),
              "unlinking it mid-hold leaves the next command on a new inode")


    guard_cases()
    emitter_cases()
    journal_cases()
    startup_cases()

    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()
