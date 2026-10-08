"""Check that what the household said does not leave the module it was said to.

Five rounds of hand-enumeration — #206, #231, #226, #234, #233 — each took the
household's words out of a list of log lines, and each was followed by a round
that found lines the last one had missed. The lists were made by reading for
`logger.` in the file somebody was already looking at, and two of the sites
found that way are not `logger.` calls: one is a keyword argument to
`_timing.step`, which becomes a log line one module away, and one is a dict
literal that becomes a file on the SD card. A search for the token cannot find
those, so the search does not terminate.

This asks at the sink instead of at the call site. A sentinel goes in at an
entry point as the household's words; every record reaching the root logger is
captured, rendered, at DEBUG; every byte under the cache directory is read
back; and the sentinel has to be in neither. What carried it there — a
format argument, a `_timing` field, a YAML header, a traceback — is not
something this has to know, which is the point: it sees the class rather than
the instances somebody remembered.

Five things it does deliberately.

- **Captures at the root logger, not at `ai_terminal`.** Every module here is
  `logging.getLogger(__name__)`, so attaching at `ai_terminal` already sees a
  line emitted three modules below the one being driven — which is where four
  of the five sites the last round found were, and why
  `tools/history_check.py`'s `journalled` no longer attaches to one named
  module with `propagate = False` on it. But `ai_terminal` is not the boundary
  the journal has. `main.py:73` calls `basicConfig`, so the handler that feeds
  the journal sits on the root, and the loggers that reach it include ADK's:
  `base_llm_flow.py:897` and `:900-902` log the Live flow's `APIError` — with
  `exc_info=True` — before re-raising it, under
  `google_adk.google.adk.flows.llm_flows.base_llm_flow`. That is the one route
  by which the Live transcript could reach the journal at all, and a sink under
  `ai_terminal` is structurally blind to it.
- **Renders the record.** `logger.info("%s", said)` keeps the value in
  `record.args` until a handler formats it. Reading `record.msg` would pass
  every site below, and pass a run in which nothing had been captured at all —
  which is why :func:`sink_cases` exists before any of the drives.
- **Reads the directory as well as the log.** #233 is a header on the SD card
  and no amount of log capture sees it.
- **Fails on a module it has never heard of.** The list of modules is not
  written down here. :func:`manifest_cases` walks `src/` for anything that can
  emit — a logging call, a `print`, a write — and requires every module that
  has one either to have *been observed emitting* during the drives above, or
  to be named in :data:`UNDRIVEN` with a reason. A new module lands red the day
  it lands, and a drive that stops reaching a module it used to reach lands red
  too. That is the part the four rounds did not have, and it is the only part
  of this file that does not go stale on its own.
- **And fails on a line it has never heard of.** Module coverage is not line
  coverage, and every one of the six rounds — #206, #226, #231, #233, #234,
  #248 — was a *line* inside a module somebody was already reading. #248's
  four sites are the standing example: `gws_cache` is driven by
  :func:`cache_cases`, so the module manifest is satisfied while
  `gws_cache.py:434`, `:442`, `:449` and `:1031` are on error branches no
  drive enters. So :data:`SITES` records how many emitting calls each module
  holds, and :func:`manifest_cases` fails when that count moves. Adding a log
  line to an already-driven module is then a decision somebody makes in the
  diff — drive it, or bump the number — rather than something that happens
  quietly.

What this cannot reach is in :data:`UNDRIVEN`, with the reason and with the
check script whose fixtures the case belongs in. That list is the honest cost
of the design: it is a hand-written list like the ones that failed, and the
only thing that makes it different is that a module cannot fall off it silently.

    uv run python tools/leak_check.py

Exits non-zero on the first case that does not hold.
"""

import ast
import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "ai_terminal"

# The cache directory has to be somewhere disposable before anything imports
# `gws_cache`, because `cache_dir()` creates it on first call and several of the
# drives below write into it. `mkdtemp` and not `TemporaryDirectory`, because
# this outlives every case.
CACHE = Path(tempfile.mkdtemp(prefix="leak-check-")).resolve()
os.environ["GWS_CACHE_DIR"] = str(CACHE)

from ai_terminal.live import browser, conn, memory, screen  # noqa: E402
from ai_terminal.live.tools import (  # noqa: E402
    _gws_cli, _gws_run, _progress, _timing, browse as browse_mod, gws_cache,
    weather,
)

# One lower-case alphanumeric word. `gws_cache._spoken` casefolds and splits on
# `[^\W_]+` before matching, so a sentinel carrying punctuation would be torn
# up before it reached the code being driven and the case would pass by never
# having asked. Nothing else in this tree spells it.
SENTINEL = "zqxvwood"

# Counted rather than written down, so the closing line cannot drift.
RAN = 0

# Every logger any drive was seen to use. Collected rather than declared — see
# :func:`manifest_cases` for why that is the whole design.
SEEN: set[str] = set()

# The cases that are red because an issue that owns them is still open. Printed
# and not fatal, so this can land and start guarding everything else while
# those are being written — the same call `tools/citations_check.py` makes for
# its drift rule, and for the same reason: a gate that is red on the day it
# lands is a gate somebody turns off.
#
# It is not a suppression list. A name here that goes *green* fails
# :func:`known_cases`, so closing the issue forces the entry out and the case
# becomes an ordinary gate. Delete the line, do not edit it. That has already
# happened twice: this list held three #226 entries — the cache search line and
# the two `_gws_run` lines — and `f55e216` sized all three; it held #233's
# header line for one commit, and the redaction closed that. Every one of them
# came out because `known_cases` went red on it, which is the design. They are
# gates now, and the list being empty is the state to want.
KNOWN: dict[str, str] = {}

# Every case above that turned out red, in the order they ran.
OPEN: list[str] = []

# The modules that can emit and that nothing here drives, each with the reason
# and with where the case belongs instead. Anything absent from this and never
# observed emitting turns `manifest_cases` red.
UNDRIVEN = {
    "ai_terminal.live.tools.plan": (
        "the planner, same shape as `gws`. `tools/plan_check.py` and "
        "`tools/replay_check.py` have the fixtures."),
    "ai_terminal.live.tools._plan_run": (
        "reached only from `plan`. `_plan_run.py:599-603` composes the "
        "sentence `_progress.py:252` speaks — the one `:251` no longer logs — "
        "so the case belongs with `plan`'s."),
    "ai_terminal.live.tools._agenda": (
        "runs the `gws` CLI. `tools/agenda_check.py` stubs it already."),
    "ai_terminal.live.tools._gws_cli": (
        "its one line is `_gws_cli.py:104`, what `json_output` warns when the "
        "CLI fails, and its only caller is `_drive_meta`, whose argv is a "
        "`fileId` (`gws_cache.py:988-991`). Not nothing, though: "
        "`_gws_cli.py:79` raises `str(TimeoutExpired)`, which spells the whole "
        "command out and so goes round that line's `argv[:3]`. "
        "`tools/agenda_check.py` drives the module."),
    "ai_terminal.live.tools._sources": (
        "two DEBUG lines on unread headers. `tools/sources_check.py`."),
    "ai_terminal.live.tools.place": (
        "two lines naming a branch and a timeout, no argument from the turn. "
        "`tools/wiring_check.py` builds the tool."),
    "ai_terminal.live.recap": (
        "reached from `main` with a model call in it. `tools/history_check.py` "
        "has the recap cases."),
    "ai_terminal.live.screen": (
        "the frame router. `tools/wiring_check.py` and `tools/browse_check.py` "
        "drive it; a frame carries the household's words and this should be "
        "driven — it is here because a screen needs a live socket."),
    "ai_terminal.live.screensaver": (
        "the generated place name at `screensaver.py:1024-1025`, plus a state "
        "file it writes. `tools/screensaver_check.py` has the fixtures."),
    "ai_terminal.live.tools._sdm": (
        "camera stream lifecycle logs (generate, extend, stop). SDM API calls."),
    "ai_terminal.live.tools.maps": (
        "one warning on grounding error. tools/wiring_check.py has the fallback fixtures."),
    "ai_terminal.wake": (
        "a stdout CLI probe, not the server. Its `print`s are the operator's."),
}

# How many emitting calls each module above holds, as :func:`emitters` counts
# them. The line-level half of :data:`UNDRIVEN`: that one asks whether a module
# is accounted for, this one asks whether every line in it is, and a number
# that moves is a line somebody added or deleted.
#
# **Counts and not `file.py:123` coordinates, and that is the whole design
# choice.** A ledger of line numbers is a ledger that rots: every insertion
# above a cited line walks the entry off its subject, and
# `tools/citations_check.py:46-51` measured what gating on that costs — a
# single line inserted at the top of `main.py`'s body turns one to four
# citations red, and a green restored by bumping a number is indistinguishable
# from a green restored by fixing one. A count does not move when a line is
# inserted above it. It moves when a line is *added*, which is the only event
# this has to see.
#
# What it buys is not proof that the new line is safe — it is that nobody can
# add one without saying so. Drive it and the drive covers it; leave it undriven
# and the bump is in the diff, next to the line, where a reviewer reads it.
# That is the bargain `UNDRIVEN` already strikes, one granularity down.
#
# Re-derived and gated a second time by `tools/cache_check.py`'s
# :func:`emitter_cases`, which reads this literal without importing this file —
# because the reviewer's do-not-run list has this script on it, and a gate that
# only ever runs inside a script nobody may run is a gate nobody can verify.
SITES = {
    "ai_terminal.live.browser": 10,
    "ai_terminal.live.main": 52,
    "ai_terminal.live.memory": 2,
    "ai_terminal.live.recap": 2,
    "ai_terminal.live.screen": 3,
    "ai_terminal.live.screensaver": 16,
    "ai_terminal.live.tools._agenda": 3,
    "ai_terminal.live.tools._gws_cli": 1,
    "ai_terminal.live.tools._gws_run": 2,
    "ai_terminal.live.tools._plan_run": 4,
    "ai_terminal.live.tools._progress": 1,
    "ai_terminal.live.tools._sdm": 3,
    "ai_terminal.live.tools._sources": 2,
    "ai_terminal.live.tools._timing": 1,
    "ai_terminal.live.tools.browse": 2,
    "ai_terminal.live.tools.gws": 10,
    "ai_terminal.live.tools.gws_cache": 21,
    "ai_terminal.live.tools.maps": 1,
    "ai_terminal.live.tools.place": 2,
    "ai_terminal.live.tools.plan": 6,
    "ai_terminal.live.tools.weather": 5,
    "ai_terminal.wake": 7,
}


@contextlib.contextmanager
def quiet():
    """Swallow the tree's logging for the duration, recording nothing."""
    root = logging.getLogger("ai_terminal")
    was, null = root.propagate, logging.NullHandler()
    root.propagate = False
    root.addHandler(null)
    try:
        yield
    finally:
        root.removeHandler(null)
        root.propagate = was


def check(name: str, ok: bool, detail: str, got=None, want=None) -> None:
    global RAN
    RAN += 1
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        if want is not None:
            print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


class Sink:
    """Everything this process can say, for as long as the block runs.

    Two sinks, because the household's words have been found in both: the
    logging tree and the bytes under the cache directory. `watch` is a
    directory to read back at the end; `None` for a case that writes no files.

    The handler goes on the root logger, not on the module being driven and
    not on `ai_terminal` either — so a record from any module in the tree is
    captured, one from a module the case did not expect is captured rather
    than missed, and so is one from a library.

    **The root and not `ai_terminal`, because the journal is not this tree's.**
    ADK logs the Live flow's own failures before re-raising them —
    `base_llm_flow.py:897` is `'APIError in live flow: %s', e` and `:900-902`
    logs the same with `exc_info=True` — under
    `google_adk.google.adk.flows.llm_flows.base_llm_flow`, which is nothing to
    do with `ai_terminal`. `main.py:82-88` caps that logger at WARNING, which
    does not reach ERROR. A handler under `ai_terminal` is structurally blind
    to it, and so was every one of the four rounds; the fifth would have been
    too. What goes into `journalctl --user -u ai-terminal` is the process's
    whole stderr, so the root is the boundary the journal actually has.

    Swapping `root.handlers` rather than appending to it also keeps the run
    quiet: `main.py:73` calls `logging.basicConfig`, so importing `main` in
    :func:`entrance_cases` puts a `StreamHandler` there, and a drive whose
    whole point is to emit the household's words should not print them.
    """

    def __init__(self, watch: Path | None = None):
        self.watch = watch
        self.lines: list[str] = []

    def __enter__(self):
        sink = self

        class Held(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                # `format`, not `record.msg`: `logger.info("%s", said)` holds
                # the value in `record.args` until something renders it, which
                # is the shape of every site the four rounds found. The
                # formatter renders `exc_info` too, so a value that reaches the
                # journal only inside a traceback is seen.
                SEEN.add(record.name)
                sink.lines.append(logging.Formatter().format(record))

        self._held = Held()
        self._root = logging.getLogger()
        # The tree as well as the root: a level set anywhere between the two
        # drops the record before any handler is asked, and `propagate = False`
        # on the tree would keep it from arriving at all.
        self._tree = logging.getLogger("ai_terminal")
        self._was = (self._root.level, list(self._root.handlers),
                     self._tree.level, self._tree.propagate)
        self._root.setLevel(logging.DEBUG)
        self._root.handlers = [self._held]
        self._tree.setLevel(logging.DEBUG)
        self._tree.propagate = True
        return self

    def __exit__(self, *exc):
        self._root.setLevel(self._was[0])
        self._root.handlers = self._was[1]
        self._tree.setLevel(self._was[2])
        self._tree.propagate = self._was[3]
        return False

    def logged(self) -> str:
        return "\n".join(self.lines)

    def written(self) -> str:
        """Every byte under the watched directory, and every name.

        `errors="replace"` rather than skipping what will not decode: a cached
        PDF is bytes and a sentinel inside one is still on the SD card.
        The names as well as the contents, because `_cache_name` builds a
        filename out of the command line, so the directory listing is itself a
        copy of what was asked for.
        """
        if self.watch is None:
            return ""
        out = []
        for path in sorted(self.watch.rglob("*")):
            if path.is_file():
                out.append(str(path.relative_to(self.watch)))
                out.append(path.read_bytes().decode("utf-8", errors="replace"))
        return "\n".join(out)

    def leaked(self) -> str:
        """Where the sentinel got to, named, or the empty string."""
        where = []
        if SENTINEL in self.logged().casefold():
            where.append("the log")
        if SENTINEL in self.written().casefold():
            where.append("a file on disk")
        return " and ".join(where)


def clear(name: str, sink: Sink) -> None:
    """One case: the sentinel reached neither sink."""
    global RAN
    got = sink.leaked()
    said = [line.splitlines()[0] for line in sink.lines
            if SENTINEL in line.casefold()]
    if got and name in KNOWN:
        RAN += 1
        OPEN.append(name)
        print(f"   open {name}: reached {got} — {KNOWN[name]}")
        for line in said[:2]:
            print(f"        {line}")
        return
    check(name, not got,
          f"{len(sink.lines)} log lines, nothing said it" if not got
          else f"reached {got}",
          said[:3] or "a file under the cache directory",
          "the sentinel nowhere")


def known_cases() -> None:
    print("\n== the still-open list has not gone stale ==")
    landed = sorted(set(KNOWN) - set(OPEN))
    check("nothing on the still-open list has quietly started passing",
          not landed,
          f"{len(OPEN)} of {len(KNOWN)} still leaking"
          + (f" ({', '.join(sorted({KNOWN[n] for n in OPEN}))})" if OPEN
             else ""),
          landed, "delete the KNOWN entry; the case is a gate now")


# ---------------------------------------------------------------------------
# The watcher itself. Without these, a green run below means only that nothing
# was captured — which is what a run with no handler attached also looks like.

def sink_cases() -> None:
    print("\n== the sink watcher sees what it claims to ==")
    said = logging.getLogger("ai_terminal.live.tools.nowhere")

    with Sink() as sink:
        said.info("plain %s", SENTINEL)
    check("a grandchild logger's record reaches the handler",
          sink.leaked() == "the log", f"{len(sink.lines)} captured")

    with Sink() as sink:
        said.info("%s", {"said": SENTINEL})
    check("a value inside a container is rendered, not left in args",
          sink.leaked() == "the log", sink.lines[0][-32:])

    with Sink() as sink:
        try:
            raise ValueError(SENTINEL)
        except ValueError:
            said.exception("failed")
    check("a value that reaches the log only in a traceback is seen",
          sink.leaked() == "the log", "exc_info is rendered")

    with Sink() as sink:
        said.debug("%s", SENTINEL)
    check("DEBUG is captured, not only INFO and above",
          sink.leaked() == "the log", "the tree is set to DEBUG")

    # And still captured with the tree turned down, which is why `Sink` raises
    # the level in two places rather than one. A level set anywhere between
    # the record and the root drops it before any handler is asked, and
    # `main.py:73`'s `basicConfig` is one `logging.getLogger("ai_terminal")
    # .setLevel` away from being that level. Without this the second raise
    # reads as redundant, because nothing in the tree sets it today.
    tree = logging.getLogger("ai_terminal")
    was = tree.level
    tree.setLevel(logging.WARNING)
    try:
        with Sink() as sink:
            said.debug("%s", SENTINEL)
        check("and still captured when the tree itself is turned down",
              sink.leaked() == "the log", "the level is raised at both ends")
    finally:
        tree.setLevel(was)

    # The shape of `gws.py:762`: a value that becomes a log line in a module
    # the caller never named. This is what makes the entry-point drives able to
    # catch #234 without knowing that `_timing` exists.
    with Sink() as sink:
        with _timing.step("resolve") as timed:
            timed["hit"] = SENTINEL
    check("a `_timing` field reaches the log from a module away",
          sink.leaked() == "the log" and
          "ai_terminal.live.tools._timing" in SEEN,
          "_timing.py renders it as JSON")

    with tempfile.TemporaryDirectory() as tmp:
        with Sink(Path(tmp)) as sink:
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "sub" / "doc.md").write_text(f"source: {SENTINEL}\n")
        check("a byte written anywhere under the directory is read back",
              sink.leaked() == "a file on disk", "found in sub/doc.md")

    with tempfile.TemporaryDirectory() as tmp:
        with Sink(Path(tmp)) as sink:
            (Path(tmp) / f"{SENTINEL}.md").write_text("nothing\n")
        check("and so is a filename that spells it",
              sink.leaked() == "a file on disk", "found in the listing")

    # The line ADK writes on the way out of a failed Live turn, at the logger
    # it writes it under. `base_llm_flow.py:897` and `:900-902` both log the
    # exception before re-raising, and an `APIError` out of the Live socket can
    # quote what was being sent. Nothing in this repo owns that logger, and
    # `main.py:82-88` caps it at WARNING rather than silencing it — so the
    # journal gets it and a handler under `ai_terminal` does not. This is the
    # case that says the capture is the journal's boundary and not the
    # package's; #235 named it and no round before it could have.
    adk = logging.getLogger(
        "google_adk.google.adk.flows.llm_flows.base_llm_flow")
    with Sink() as sink:
        try:
            raise RuntimeError(f"APIError in live flow: {SENTINEL}")
        except RuntimeError:
            adk.exception("APIError in live flow")
    check("a library's logger is captured too, not only this tree's",
          sink.leaked() == "the log", f"{len(sink.lines)} captured")

    root, tree = logging.getLogger(), logging.getLogger("ai_terminal")
    before = (root.level, list(root.handlers), tree.level, tree.propagate)
    with Sink() as sink:
        said.info("nothing here")
    clear("and a run that leaks nothing reads as clean", sink)
    check("the handler comes off again, from both loggers it touched",
          not any(type(handler).__name__ == "Held"
                  for handler in root.handlers + tree.handlers),
          "nothing left attached")
    # Restoring matters more here than it did when the handler sat on
    # `ai_terminal`: this swaps the root's handler list out from under whatever
    # configured it, and a sweep runs the scripts in one process each but a
    # future harness may not.
    now = (root.level, list(root.handlers), tree.level, tree.propagate)
    check("and both loggers are left saying what they said before",
          now == before, "level, handlers and propagate all back", now, before)


# ---------------------------------------------------------------------------
# The drives. One per entry point, each putting the sentinel where the
# household's words go and asserting it is nowhere on the way out.

class FakeQueue:
    """The one method `_progress` calls on a live request queue."""

    def send_content(self, *args, **kwargs) -> None:
        pass


class FakeContext:
    """The three attributes a `ToolContext` is read for here."""

    def __init__(self, queue=None):
        self.state: dict = {}
        self.session = type("S", (), {"id": "leak"})()
        self.function_call_id = "call-1"
        self._invocation_context = type(
            "I", (), {"live_request_queue": queue or FakeQueue()})()


class FakePage:
    url = "about:blank"

    async def goto(self, url: str) -> None:
        pass


class FakeBrowser:
    """`browser._Session`, with no browser under it.

    `about:blank` throughout, so the loop takes the blank branch and never
    looks at a page — the decision and the logging of it are what is being
    driven, and a real page would only add ways for this to be slow.
    """

    page = FakePage()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def look(self):
        return None, "", ""

    async def act(self, step):
        return "acted", True

    async def tree(self):
        return ""


def memory_cases() -> None:
    print("\n== remember: the fact, not the topic ==")
    with Sink() as sink:
        memory.remember("home address", f"{SENTINEL} street 14",
                        FakeContext())
    clear("a standing fact does not reach the log", sink)
    check("and the topic still does, which is the point of the line",
          any("home address" in line for line in sink.lines),
          "`_PLAIN_ARGS` keeps `topic` printable")


class FakeCall:
    """One `types.FunctionCall`, as `_log_tool_traffic` reads it."""

    def __init__(self, name: str, args: dict):
        self.name, self.args = name, args


class FakePart:
    def __init__(self, call=None, response=None):
        self.function_call = call
        self.function_response = response


class FakeEvent:
    def __init__(self, *parts):
        self.content = type("C", (), {"parts": list(parts)})()


def entrance_cases() -> None:
    """#206's site and #231's, driven through the modules that hold them.

    Only `main` is deferred, and only the `main` line buys anything: the
    top-of-file `from ai_terminal.live.tools import …` already loads `gws`,
    because `live/tools/__init__.py:4` is `from .gws import build_gws_tool` and
    importing any member of that package runs it. The name is bound here for
    readability, not to keep the module out. `main` is genuinely not imported
    until this runs, and it is inert when it is —
    `tools/deploy_check.py:106` and `tools/screensaver_check.py:370` already
    import it, and neither reaches an account.

    The import is silenced because `main`'s module body reports the environment
    it did not find, and a sweep line is not the place for three ERRORs about a
    `.env` nobody was asking it to read. `quiet` and not `Sink`: a `Sink` would
    put `ai_terminal.live.main` in `SEEN` for having been imported, and
    :func:`manifest_cases` would then count a module as driven that no case had
    driven.
    """
    with quiet():
        from ai_terminal.live import main  # noqa: PLC0415
        from ai_terminal.live.tools import gws  # noqa: PLC0415
        os.environ["GWS_CACHE_DIR"] = str(CACHE)

    print("\n== the two entrances the journal used to hold whole ==")
    with Sink() as sink:
        main._log_tool_traffic(FakeEvent(FakePart(
            call=FakeCall("remember", {"topic": "home address",
                                       "fact": f"{SENTINEL} street 14"}))))
    clear("a tool call's arguments do not reach the log", sink)
    check("and the tool's name still does",
          any("remember" in line for line in sink.lines),
          "the trace is still a trace")

    # `gws._ask`, with the sub-agent replaced by something that falls over.
    # Three of the module's five sites are in here; the other two are in `gws`
    # itself and need a screen, so they stay with #231's cases in
    # `tools/history_check.py`.
    real = gws._run, gws._agent, _gws_cli.available
    gws._agent = object()
    _gws_cli.available = lambda: True

    async def fell_over(args, tool_context):
        raise RuntimeError("the sub-agent fell over")

    gws._run = fell_over
    try:
        with Sink() as sink:
            asyncio.run(gws._ask(
                {"request": f"find the letter about {SENTINEL}"}, None))
        clear("nor does the question put to the sub-agent", sink)
    finally:
        gws._run, gws._agent, _gws_cli.available = real


def cache_cases() -> None:
    print("\n== the gws cache: the log and the SD card ==")
    check("the cache under test is still the temp one",
          gws_cache.cache_dir() == CACHE, str(CACHE))
    with Sink(CACHE) as sink:
        asyncio.run(gws_cache.search_cache(f"the {SENTINEL} letter"))
    clear("searching the cache does not log what was searched for", sink)

    was = gws_cache._drive_meta
    gws_cache._drive_meta = lambda file_id: {}
    try:
        with Sink(CACHE) as sink:
            out = CACHE / "letter.md"
            out.write_text("the body of the document\n")
            gws_cache.record(
                {out.name: out.stat().st_mtime},
                "gws drive files list --query "
                f"\"fullText contains '{SENTINEL}'\" -o letter.md")
        clear("and caching a document does not write it into the header", sink)
    finally:
        gws_cache._drive_meta = was
        for path in CACHE.iterdir():
            if path.is_file():
                path.unlink()

    # The two drives above are the happy path, and every site #248 found is on
    # an error branch off it. They are also the first drives here to put the
    # sentinel in a *filename* rather than in a query: `record`'s document is
    # `letter.md`, so a run that forced these branches with the drive above
    # would have printed `letter.md` and stayed green. A bare filename is the
    # shape #226, #231 and #234 were all about, and it was the one shape this
    # function could not express.
    print("\n== the gws cache: the misses, and the summary that failed ==")
    attach = gws_cache.rewrite_output(
        ["gmail", "users", "messages", "attachments", "get",
         "-o", f"{SENTINEL}.pdf", "--params",
         '{"userId": "me", "messageId": "M1", "id": "A1"}'])
    # One per branch of `unpack`, in the order they sit in the function. Each
    # returns "" and writes no file, so the name reaches the log and nothing
    # else; a fix that moved the line instead of sizing it would leave the
    # first case red.
    for stdout, what in (
            ("gws: no such command", "no JSON at all"),
            ('{"error": {"code": 404}}', "no attachment in the JSON"),
            (json.dumps({"data": "!!!!"}), "a `data` that is not base64")):
        with Sink(CACHE) as sink:
            wrote = gws_cache.unpack(attach, stdout)
        check(f"unpack says so when stdout has {what}",
              wrote == "" and len(sink.lines) == 1,
              f"{len(sink.lines)} log lines, and nothing written")
        clear(f"and the -o filename is not in the line saying {what}", sink)

    # `finish`, which is the frame above the leak: #234 sized the
    # `summarize.bg` step and left the `except` under it printing `path.name`
    # raw. `_summarize` itself is deliberately not stubbed — every other check
    # of this module stubs it, and stubbing it is exactly what skips the line.
    # The model client is broken instead, the way `weather_cases` breaks it.
    # No file is created at that path: the summary comes back empty, `finish`
    # takes the `continue`, and nothing is written.
    was = gws_cache.grounding_client

    def broken():
        raise RuntimeError("no credentials here")

    gws_cache.grounding_client = broken
    try:
        with Sink(CACHE) as sink:
            gws_cache.finish([(CACHE / f"{SENTINEL}-letter.md", {},
                               "the body of the document", 0.0)])
        check("a summary the model refused is still reported",
              any("could not summarise" in line for line in sink.lines),
              f"{len(sink.lines)} log lines")
        clear("and the document is not named in the line reporting it", sink)
        # The other half, as `progress_cases` has it: a fix that logged nothing
        # would pass `clear` and leave nobody able to tell a document that
        # failed to summarise from one that was never queued.
        check("and the line still says what went wrong",
              any("no credentials here" in line for line in sink.lines),
              "the API's own status, which is not the household's words")
    finally:
        gws_cache.grounding_client = was


def gws_run_cases() -> None:
    print("\n== the gws CLI wrapper ==")
    real = _gws_run.subprocess, _gws_cli.binary
    _gws_cli.binary = lambda: "/bin/true"
    _gws_run.subprocess = types.SimpleNamespace(
        run=lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="ok", stderr=""),
        TimeoutExpired=subprocess.TimeoutExpired)
    asked = f'gws gmail messages list --query "{SENTINEL}"'
    try:
        with Sink(CACHE) as sink:
            async def one():
                with _gws_run.budget(60):
                    return await _gws_run.gws(asked)
            asyncio.run(one())
        clear("running a command does not log the command line", sink)

        with Sink(CACHE) as sink:
            async def past_the_cap():
                with _gws_run.budget(60):
                    for number in range(_gws_run.STOP_AT + 2):
                        await _gws_run.gws(
                            'gws gmail messages list --query '
                            f'"{SENTINEL}-{number}"')
            asyncio.run(past_the_cap())
        clear("and neither does refusing one", sink)
    finally:
        _gws_run.subprocess, _gws_cli.binary = real


def weather_cases() -> None:
    print("\n== weather: the place somebody named ==")
    was = weather.grounding_client

    def broken():
        raise RuntimeError("no credentials here")

    weather.grounding_client = broken
    try:
        with Sink() as sink:
            weather._geocode(SENTINEL, "en")
        clear("a place that will not geocode is not named in the log", sink)
    finally:
        weather.grounding_client = was


def progress_cases() -> None:
    print("\n== the progress channel ==")
    real = _progress.QUIET, _progress.GAP, _progress.OPENING
    _progress.QUIET, _progress.GAP, _progress.OPENING = 0.02, 0.02, 0.02
    try:
        with Sink() as sink:
            async def run():
                notes = _progress.Notes(FakeContext(), "gws")
                notes.opening()

                async def work():
                    await asyncio.sleep(0.1)
                    notes.note("step", f"Step 1 of 2: find the {SENTINEL}")
                    await asyncio.sleep(0.1)
                    return "done"

                job = notes.start(work())
                await notes.watch(job)
                await job
            asyncio.run(run())
        clear("a progress note's sentence does not reach the log", sink)
        # The other half of every fix in this file, and the reason none of
        # them is `del logger.info`. `state` is the short handle the card is
        # drawn from and is a closed vocabulary, so the line still says which
        # note went out. A patch that satisfied `clear` by logging nothing, or
        # by sizing the whole note, would leave an operator unable to tell a
        # stalled tool from a quiet one.
        check("and the note still says which one it was",
              any("gws progress: step" in line for line in sink.lines),
              "the state, which is a closed vocabulary")
    finally:
        _progress.QUIET, _progress.GAP, _progress.OPENING = real


def browse_cases() -> None:
    print("\n== the browser loop, and the tool around it ==")
    real = browser._Session, browser._decide_sync
    browser._Session = FakeBrowser
    # Two steps: what was typed into the page, and the answer. Both are strings
    # the decision dict carries and both are what the household asked about.
    steps = iter([{"act": "type", "idx": 0, "text": f"{SENTINEL} opening hours"},
                  {"act": "answer", "text": f"the {SENTINEL} shuts at six"}])
    browser._decide_sync = lambda *a, **k: next(steps)
    try:
        with Sink() as sink:
            asyncio.run(browser.run(f"when does the {SENTINEL} shut?", "en"))
        clear("the decision dict does not reach the log", sink)
        # `_sized(step)` would give `<dict>` and pass the case above while
        # destroying the line: which of the five verbs the model chose, and
        # which element it chose it on, is the whole of what makes a browse
        # readable after the fact. The per-key split is what keeps both.
        check("and the step still says which verb, on which element",
              any("'act': 'type'" in line and "'idx': 0" in line
                  for line in sink.lines),
              "act and idx are printed whole")
        check("while the string that went into the page is a size",
              any("'text': '<22 chars>'" in line for line in sink.lines),
              "everything outside the closed vocabulary is `_sized`")
    finally:
        browser._Session, browser._decide_sync = real

    real = browser._Session, browser._decide_sync
    browser._Session = FakeBrowser
    browser._decide_sync = lambda *a, **k: {
        "act": "answer", "text": f"the {SENTINEL} shuts at six"}
    lowered = _progress.QUIET, _progress.GAP, _progress.OPENING
    _progress.QUIET, _progress.GAP, _progress.OPENING = 0.02, 0.02, 0.02

    async def nowhere(text):
        pass

    try:
        with Sink() as sink:
            async def one():
                screen.register("leak", nowhere)
                browse_mod._JOBS["leak"] = {None: {}}
                try:
                    return await browse_mod.browse(
                        f"when does the {SENTINEL} shut?",
                        tool_context=FakeContext())
                finally:
                    screen.unregister("leak", nowhere)
                    browse_mod._JOBS.pop("leak", None)
            got = asyncio.run(one())
        check("the tool answered, so the drive reached the line",
              "answer" in got, str(got)[:60])
        clear("and the answer is not logged on the way out", sink)
    finally:
        browser._Session, browser._decide_sync = real
        _progress.QUIET, _progress.GAP, _progress.OPENING = lowered


def gws_status_cases() -> None:
    print("\n== gws status: no credentials or tokens exposed ==")
    with quiet():
        from ai_terminal.live import main  # noqa: PLC0415
        from ai_terminal.live.tools import _gws_cli  # noqa: PLC0415

    # Test status endpoint without leaking tokens
    status = asyncio.run(main.api_gws_status())
    check("the status response is a dictionary", isinstance(status, dict),
          "api_gws_status returns dict")
    check("status only has the five expected public keys",
          sorted(status) == ["account", "available", "connected", "expired", "scopes"],
          f"keys: {sorted(status)}")
    token_keys = [k for k in status if "token" in k.lower() or "secret" in k.lower() or "cred" in k.lower()]
    check("/api/gws/status returns no token-shaped keys", not token_keys,
          "clean keys", token_keys, [])
    for k, v in status.items():
        if isinstance(v, str):
            check(f"field {k} does not contain credentials or tokens",
                  "ya29." not in v and "secret" not in v.lower(),
                  f"{k} value safe")

    # Even if CLI status had token-shaped fields, api_gws_status never returns them
    real_run = _gws_cli.run_json
    real_avail = _gws_cli.available
    try:
        _gws_cli.available = lambda: True
        _gws_cli.run_json = lambda *a, **k: {
            "email": "user@example.com",
            "token": "ya29.secret_token_12345",
            "access_token": "ya29.access_token_12345",
            "refresh_token": "refresh_token_12345",
            "client_secret": "my_client_secret",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
        }
        mocked = asyncio.run(main.api_gws_status())
        check("status with tokens in CLI output still exposes only public keys",
              sorted(mocked) == ["account", "available", "connected", "expired", "scopes"],
              f"keys: {sorted(mocked)}")
        check("status with tokens in CLI output exposes no token values",
              not any("ya29." in str(v) or "secret" in str(v).lower() for v in mocked.values()),
              "no tokens in values")
    finally:
        _gws_cli.run_json = real_run
        _gws_cli.available = real_avail

    # Consent URL from login does not reach the log
    real_popen = subprocess.Popen

    class FakeProc:
        def __init__(self, *args, **kwargs):
            import io
            self.stderr = io.StringIO(
                "Open URL: https://accounts.google.com/o/oauth2/auth?"
                f"client_id={SENTINEL}&secret={SENTINEL}\n"
            )
            self.stdout = io.StringIO()
            self.returncode = 0

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

    try:
        subprocess.Popen = FakeProc
        with Sink() as sink:
            from starlette.requests import Request
            req = Request({
                "type": "http", "http_version": "1.1", "method": "POST",
                "scheme": "http", "path": "/api/gws/login", "root_path": "",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
            })
            resp = asyncio.run(main.api_gws_login(req))
            check("login returns URL", "url" in resp, "consent URL returned")
        clear("consent URL does not reach the log", sink)
    finally:
        subprocess.Popen = real_popen


# ---------------------------------------------------------------------------
# The gate that keeps the list above from being the thing that failed four
# times: it is not a list. It is the tree, minus what was observed, minus what
# is written off.

SINKS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
WRITES = {"write", "writelines", "write_text", "write_bytes", "writerow",
          "writerows"}


def emitters(root: Path = SRC) -> dict[str, int]:
    """Every module under `src/` that can put text outside this process.

    Counted by AST rather than by grep, because two of the five sites the last
    round found are not spelled `logger.` — this counts the calls, and
    :func:`manifest_cases` checks both whether the count is zero and whether
    it matches :data:`SITES`.
    """
    found: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        name = ".".join(("ai_terminal",)
                        + path.relative_to(root).with_suffix("").parts)
        name = name.removesuffix(".__init__")
        count = 0
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                count += 1
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr in SINKS | WRITES:
                    count += 1
        if count:
            found[name] = count
    return found


def manifest_cases() -> None:
    print("\n== every module that can emit is driven or written off ==")
    can = emitters()
    check("the tree has emitters to account for at all", len(can) > 10,
          f"{len(can)} modules with a sink, {sum(can.values())} calls")

    missing = sorted(set(can) - SEEN - set(UNDRIVEN))
    check("no module can emit without being driven or named",
          not missing,
          "every emitter is accounted for" if not missing
          else f"{len(missing)} unaccounted",
          missing, "each one driven above, or in UNDRIVEN with a reason")

    gone = sorted(set(UNDRIVEN) - set(can))
    check("and nothing is written off that no longer emits", not gone,
          f"{len(UNDRIVEN)} written off, all still real", gone, [])

    reached = sorted(set(UNDRIVEN) & SEEN)
    check("nor written off after a drive started reaching it", not reached,
          "the excuses are all still true", reached,
          "delete the UNDRIVEN entry and add a case")

    # The line-level half. Everything above is satisfied by a module having
    # emitted once, and six rounds running the defect has been one line inside
    # a module that had. `moved` is every module whose emitting-call count is
    # not the one recorded, in either direction, plus every module the ledger
    # has never heard of.
    moved = sorted({name for name in set(can) | set(SITES)
                    if can.get(name, 0) != SITES.get(name, 0)})
    check("no module can gain or lose an emitting line uncounted", not moved,
          f"{sum(SITES.values())} calls across {len(SITES)} modules, all "
          "still there" if not moved
          else f"{len(moved)} moved — drive it, or update SITES in the "
               "same commit",
          {name: can.get(name, 0) for name in moved},
          {name: SITES.get(name, 0) for name in moved})

    driven = sorted(set(can) & SEEN)
    print(f"\n   driven: {len(driven)} of {len(can)} modules that can emit "
          f"({len(UNDRIVEN)} written off)")
    for name in driven:
        print(f"     {name}")


def main() -> None:
    sink_cases()
    memory_cases()
    entrance_cases()
    cache_cases()
    gws_run_cases()
    weather_cases()
    progress_cases()
    browse_cases()
    known_cases()
    gws_status_cases()
    manifest_cases()
    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()
