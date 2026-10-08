"""Check the step log and the arithmetic done on it.

A measurement nobody has checked is a number people then argue about. Two
things here can be quietly wrong and produce a table that still looks like a
table:

- **The step lines themselves.** A step that logs nothing when its block
  raises hides the slow failures, which are the ones worth finding. A step
  outside a `question()` that logs `at` anyway would be reporting an offset
  from nothing.
- **The name on a `cli` step.** Every `gws` subprocess totals into one row, so
  the label is the only thing separating the export from the two turns spent
  asking the CLI how to spell the export. A label that collapses them says a
  document question ran three subprocesses and nothing about which.
- **The union.** `read` contains `drive.meta` and `record` contains both
  `drive.meta` and `summarize`. Summing the step rows and subtracting would
  charge those seconds twice and report a negative amount of model time on a
  question that was mostly subprocess; :func:`~latency_report.covered` unions
  the intervals instead, and every way of getting an interval union wrong —
  nesting, touching, unsorted input — is here.

Runs in a fraction of a second, talks to nothing.

    uv run python tools/timing_check.py

Exits non-zero on the first case that does not hold.
"""

import asyncio
import json
import logging
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from google.adk.tools import FunctionTool  # noqa: E402

from ai_terminal.live.tools import (  # noqa: E402
    _gws_run, _timing, gws, gws_cache,
)

import latency_report  # noqa: E402

# Long enough that a clock reading whole milliseconds cannot round it to zero,
# short enough that the whole file still runs in under a second.
SLOW = 0.05

# Counted rather than written down, so the closing line cannot drift from the
# number of cases the way a figure typed into a commit message does.
RAN = 0



def contains_term(q: str) -> str | None:
    """The quoted operand on the right of `contains`, or None if it is broken.

    Drive's `q` grammar wants a string literal there, quoted with `\'` or
    `"`, and an apostrophe inside a `\'`-quoted one has to be escaped or it
    ends the literal early. That is a 400 from Drive and nothing local objects
    to it: the payload is still valid JSON, and `shlex` passes the apostrophe
    through untouched because it sits inside double quotes. So the check has
    to read the grammar rather than the syntax around it — this walks the
    literal the way Drive's parser would and returns it only when it is closed
    and nothing is left dangling after it.
    """
    _, _, operand = q.partition("contains ")
    if not operand or operand[0] not in "'\"":
        return None
    quote, i = operand[0], 1
    while i < len(operand):
        if operand[i] == "\\":
            i += 2
            continue
        if operand[i] == quote:
            # Closed — but only well formed if the literal is the whole
            # operand. `'Alex's trip'` closes after `Alex` and leaves the rest.
            return operand[1:i] if i == len(operand) - 1 else None
        i += 1
    return None

def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


class Kept(logging.Handler):
    """Every step line the block inside logs, parsed."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.steps: list[dict] = []
        self.logger = logging.getLogger(_timing.__name__)

    def __enter__(self) -> "Kept":
        self.was = self.logger.level, self.logger.propagate
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(self)
        return self

    def __exit__(self, *_) -> None:
        self.logger.removeHandler(self)
        self.logger.level, self.logger.propagate = self.was

    def emit(self, record: logging.LogRecord) -> None:
        self.steps.append(latency_report.parsed(record.getMessage()))

    def named(self, name: str) -> dict:
        return next(s for s in self.steps if s["step"] == name)


def spans(*pairs) -> list[dict]:
    """Steps with nothing on them but the interval, for `covered`."""
    return [{"step": "x", "at": at, "ms": ms} for at, ms in pairs]


def main() -> None:
    print("== one step, one line ==")
    with Kept() as kept:
        with _timing.step("solo"):
            time.sleep(SLOW)
    check("a step logs exactly one line", len(kept.steps), 1)
    check("under its own name", kept.steps[0]["step"], "solo")
    check("with a duration that is roughly the sleep",
          40 <= kept.steps[0]["ms"] <= 400, True,
          f"{kept.steps[0]['ms']}ms for a {SLOW * 1000:.0f}ms sleep")
    check("and no offset, because there was no question to offset from",
          "at" in kept.steps[0], False)
    check("nor a question name", "q" in kept.steps[0], False)

    print("\n== extra fields ==")
    with Kept() as kept:
        with _timing.step("sized", chars=1200):
            pass
    check("a keyword argument goes on the line", kept.steps[0].get("chars"),
          1200)
    with Kept() as kept:
        with _timing.step("late") as timed:
            timed["files"] = 3
    check("and so does one added on the way out",
          kept.steps[0].get("files"), 3)

    print("\n== a field is a measurement, not the document ==")
    # `mark` writes every field of the record into the journal with
    # `ensure_ascii=False`, and `resolve`'s `hit` used to be the cached
    # document's filename — a name `gws_cache.named` returns only when a
    # phrase out of it was said out loud. See #234, and #206 for why the
    # journal is the store that matters.
    DOC = "sentinel-tax-return-2025.md"

    def resolved(found: dict | None) -> list[dict]:
        """The `resolve` step for a question that named `DOC`, as logged.

        Both of `_prefetch`'s outward calls are replaced, so nothing here
        reads the cache directory and nothing starts a `gws` subprocess.
        `found` of None is the miss: `named` matched no document at all.
        """
        was = gws_cache.named, gws_cache.read_cache

        async def read_cache(name: str) -> dict:
            return found or {}

        gws_cache.named = lambda request: None if found is None else DOC
        gws_cache.read_cache = read_cache
        try:
            with Kept() as kept:
                asyncio.run(gws._prefetch(f"what does the {DOC} say"))
            return kept.steps
        finally:
            gws_cache.named, gws_cache.read_cache = was

    steps = resolved({"text": "the body"})
    check("a resolve that found a document logs one line", len(steps), 1)
    check("and no field of it names the document",
          DOC in json.dumps(steps[0], ensure_ascii=False), False,
          json.dumps(steps[0], ensure_ascii=False))
    check("only that the resolver fired", steps[0].get("hit"), True)
    check("nor does the line for a copy that could not be read",
          DOC in json.dumps(resolved({})[0], ensure_ascii=False), False)
    check("and a question that named nothing is still told apart from one "
          "that did", resolved(None)[0].get("hit"), False)

    print("\n== naming a cli step ==")
    # The `cli` row totals every `gws` subprocess a question ran, and on a
    # document question those are three different things: one fetch and two
    # turns spent asking the CLI to describe the fetch. `cmd` is the only place
    # the log says which is which.
    for argv, want in (
        (["drive", "files", "export", "-o", "notes.md",
          "--params", '{"fileId": "X"}'], "drive files export"),
        (["drive", "files", "export", "--help"], "drive files export --help"),
        (["schema", "drive.files.export"], "schema drive.files.export"),
        (["drive", "files", "list", "--params", '{"q": "x"}'],
         "drive files list"),
        (["drive", "files", "get", "--params", '{"fileId": "X"}'],
         "drive files get"),
    ):
        check(f"{' '.join(argv[:4])}…", _gws_run.cli_label(argv), want)
    check("a --params blob is not repeated into the label",
          "fileId" in _gws_run.cli_label(
              ["drive", "files", "get", "--params", '{"fileId": "X"}']),
          False)
    check("--help after a flag is still found",
          _gws_run.cli_label(["drive", "--format", "json", "--help"]),
          "drive --help")
    check("and a command that is nothing but flags names nothing",
          _gws_run.cli_label(["--version"]), "")

    print("\n== the gws commands written into the instruction ==")
    # `INSTRUCTION` tells the sub-agent to run three Drive commands as written,
    # with no `--help` and no `gws schema` first. That is the whole saving in
    # #30 and it means nothing checks them any more except this: the model
    # copies them, `_parse` splits them with `shlex`, and a quote lost in
    # between produces a request that is still valid JSON and an invalid Drive
    # query. Nothing local rejects it — the 400 comes back from Drive.
    commands = [line for line in re.findall(r"`(gws [^`]+)`", gws.INSTRUCTION)
                if "--params" in line or "--json" in line]
    check("the instruction still carries commands to check", len(commands), 7)
    for n, command in enumerate(commands, 1):
        argv, refused = _gws_run._parse(command)
        # Numbered as well as named: two of the seven are `drive files get`,
        # one for metadata and one to download bytes, and a failing case has
        # to say which.
        label = f"{n}. {_gws_run.cli_label(argv) or command[:40]}"
        check(f"{label}: parses", refused, None)
        flag = "--params" if "--params" in argv else "--json"
        payload = argv[argv.index(flag) + 1]
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as err:
            body = {"__error__": str(err)}
        check(f"{label}: {flag} is JSON", body.get("__error__"), None)
        # Drive's `q` grammar wants a quoted string literal on the right of
        # `contains`, and a bare word there is a 400. Single-quoting the whole
        # `--params` eats those inner quotes, so the search command is
        # double-quoted outside with the JSON's own quotes escaped. Read as
        # the grammar rather than as "there is a quote in there somewhere":
        # `contains 'Alex's trip'` has quotes in it and is still a 400.
        if "q" in body:
            check(f"{label}: the search term is one closed literal",
                  contains_term(body["q"]) is not None, True, repr(body["q"]))

    # The form that shipped in 9762e69 and was reopened for. Here so the three
    # cases above cannot pass by never having been able to fail.
    broken = ("""gws drive files list --params '{"q": "name contains """
              """'itinerary'", "pageSize": 10}'""")
    argv, _ = _gws_run._parse(broken)
    check("single-quoting the whole thing loses the inner quotes",
          json.loads(argv[argv.index("--params") + 1])["q"],
          "name contains itinerary")

    print("\n== the Gmail commands written into the instruction ==")
    # #249. The two Gmail commands are in the prompt for the reason the Drive
    # ones are: a `load_skill` before the first search cost two or three turns
    # and 5.0 to 6.7 s of every mail question measured on 2026-09-11, and what
    # the skill hands back is the raw `users.messages` API — which returns ids
    # and costs a `+read` each — rather than the `+triage` that answers in one
    # call. The loop above cannot see these two: they carry their arguments as
    # flags, not as a `--params` blob, so they are matched here instead. A
    # command the prompt says to run as written and that does not parse spends
    # a call finding that out.
    gmail = [line for line in re.findall(r"`(gws gmail [^`]+)`",
                                         gws.INSTRUCTION)
             if "--params" not in line and "--json" not in line]
    check("the instruction carries the two helper commands", len(gmail), 2)
    labels = []
    for command in gmail:
        argv, refused = _gws_run._parse(command)
        label = _gws_run.cli_label(argv)
        labels.append(label)
        check(f"{label}: parses", refused, None)
        # `--format json` on both, because both print a table without it and
        # asking a second time is the wasted call the section is about.
        check(f"{label}: asks for JSON up front",
              argv[argv.index("--format") + 1] if "--format" in argv else None,
              "json")
    check("one of them is the search that answers in one call",
          "gmail +triage" in labels, True, str(labels))
    # The two sentences that would put the round trips straight back, and that
    # nothing else holds down: step 1 sends the model to a skill for every
    # service the instruction does not write out, and the do-not-load list is
    # what stops it re-reading one it has been given.
    # Joined on whitespace because both sentences wrap, and where they wrap is
    # not what is being pinned.
    prose = " ".join(gws.INSTRUCTION.split())
    check("step 1 no longer sends a mail question to a skill first",
          "For Calendar or Keep, load that service's skill" in prose, True)
    check("and the skills not to load name gws-gmail",
          "`gws-gmail`" in prose.split("Do not load", 1)[1][:140], True)

    print("\n== an apostrophe inside the search term ==")
    # Four spellings of the same search, all measured against Drive. Only the
    # last two are answerable, and the instruction teaches those two.
    def q_of(command: str):
        argv, refused = _gws_run._parse(command)
        if refused:
            return refused
        payload = argv[argv.index("--params") + 1]
        try:
            return json.loads(payload)["q"]
        except json.JSONDecodeError as err:
            return f"invalid JSON: {err.msg}"

    bare = q_of(
        'gws drive files list --params "{\\"q\\": '
        '\\"name contains \'Alex\'s trip\'\\"}"')
    check("the obvious spelling survives shlex and json untouched",
          bare, "name contains 'Alex's trip'")
    # Drive answered this one 400 Invalid Value; the local layers did not.
    check("and is a broken literal, which is what Drive rejects",
          contains_term(bare), None)
    # Why the case above reads the grammar rather than looking for a quote:
    # the broken spelling has three of them.
    check("a quote somewhere in the q would not have caught it",
          "'" in bare, True)

    escaped = q_of(
        'gws drive files list --params "{\\"q\\": '
        '\\"name contains \'Alex\\\'s trip\'\\"}"')
    check("escaping the apostrophe for Drive alone breaks the JSON first",
          escaped, "invalid JSON: Invalid \\escape")

    doubled = q_of(
        'gws drive files list --params "{\\"q\\": '
        '\\"name contains \\\\\\"Alex\'s trip\\\\\\"\\"}"')
    check("quoting the term with a double quote instead needs no escape",
          doubled, 'name contains "Alex\'s trip"')
    check("and gives one closed literal, apostrophe and all",
          contains_term(doubled), "Alex's trip")

    dropped = q_of(
        'gws drive files list --params "{\\"q\\": \\"name contains \'trip\'\\"}"')
    check("so does leaving the apostrophe out, which is the advice given first",
          contains_term(dropped), "trip")
    # The third form Drive accepts, and the one no model will spell right:
    # four backslashes on the command line to put one in front of the
    # apostrophe. Here because it is what `contains_term` skips escapes for.
    check("a backslash-escaped apostrophe is a closed literal too",
          contains_term("name contains 'Alex\\'s trip'"), "Alex\\'s trip")
    check("and a bare word on the right of contains is not a literal at all",
          contains_term("name contains trip"), None)

    print("\n== what the instruction says contains does ==")
    # Everything above reads the commands, so the sentence next to them is
    # pinned by nothing: delete the whole apostrophe advice and the file still
    # passes. It is also the half of the paragraph a model reaches for first,
    # because it is the half that needs no escaping.
    #
    # Measured against Drive on 2026-09-08, on a file named "Pandora's AI Model
    # Routing Box - Efficient Allocation…": `Routing` finds it and `outing`
    # does not, `AI Model Rou` does and `andora` does not, `s AI Model` does,
    # and `Pandora's` comes back 400 Invalid Value. So `contains` matches a
    # prefix of a word, not a substring and not a prefix of the whole name, and
    # the apostrophe is one of the boundaries.
    # Read with the line breaks flattened, both here and on the README: the
    # phrase is two words long and prose gets rewrapped, so matching the file
    # as written would fail on a paragraph that still says the right thing.
    def flat(text: str) -> str:
        return " ".join(text.split())

    instruction = flat(gws.INSTRUCTION)
    check("the instruction says how far into a word contains looks",
          "prefix matching" in instruction, True)
    check("and not that it matches anywhere, which Drive disagrees with",
          "matches substrings" in instruction, False)
    readme = flat((ROOT / "README.md").read_text(encoding="utf-8"))
    check("the README describes it the same way",
          ("prefix matching" in readme, "matches substrings" in readme),
          (True, False))

    print("\n== the document paragraph the cache has to bear out ==")
    # `## Documents` is written for a cache with something in it. Steps 1 to 3
    # name a copy to find, and step 2 sends the model after "the older ones the
    # list below leaves out", which the list only leaves any of past
    # `LISTING_LIMIT`. Every one of those is false over an empty cache, and the
    # prompt then says look in the cache first and, further down, that the
    # cache is empty. It resolves that the harmless way today; the turn it
    # costs when it stops is the reason the paragraph comes out.
    #
    # Out, but not always to nothing, and three states rather than two. The
    # middle one is the easy mistake: a cache the listing shows whole still has
    # the document bodies, which `_scored` weights and no listing row carries,
    # so dropping the clause there hides the one tool that can answer a
    # question the English summaries cannot.
    #
    # The clause is lifted out with `str.replace`, which does nothing at all if
    # the paragraph is ever rewrapped, so the first case here is the one that
    # holds the constant and the text together.
    check("the clause is in the instruction exactly once",
          gws.INSTRUCTION.count(gws.SEARCH_TAIL), 1)

    def with_cache(files: int) -> tuple[str, str]:
        """`listing()` and the instruction built on it, over N cached files."""
        kept = gws_cache.cache_dir
        with tempfile.TemporaryDirectory() as scratch:
            gws_cache.cache_dir = lambda: Path(scratch)
            try:
                for n in range(files):
                    (Path(scratch) / f"doc-{n}.md").write_text(
                        f"Cached document {n}.\n", encoding="utf-8")
                return gws_cache.listing(), gws._instruction(None)
            finally:
                gws_cache.cache_dir = kept

    def clauses(text: str) -> tuple[bool, bool]:
        """Which of the two versions of step 2's second half the prompt got."""
        return gws.SEARCH_TAIL in text, gws.SEARCH_SHOWN in text

    def promises(text: str) -> tuple[bool, bool, bool]:
        """The three sentences of `## Documents` that need a cached copy.

        One per step 1 to 3, read off the built prompt rather than the
        constant, because the constant being right is no use if the branch
        picks the other one.
        """
        return (
            "The recent ones are listed below" in text,
            "the cache is the first place you look" in text,
            "Read a match with `read_cache`" in text,
        )

    empty, cold = with_cache(0)
    check("an empty cache says so and names no tail",
          (empty, gws_cache.TAIL in empty), (gws_cache.EMPTY, False))
    check("and it is the sentence the instruction is keyed on",
          gws_cache.EMPTY, "The cache is empty.")
    check("so the instruction sends the model nowhere to search",
          clauses(cold), (False, False))
    check("and promises nothing the empty cache cannot keep",
          promises(flat(cold)), (False, False, False))
    # The paragraph is swapped, not deleted: what to call an export and what
    # not to do with it are as true over an empty cache as over a full one.
    check("what is left still says what to do with what it exports",
          "name the file for what is in it" in flat(cold).lower(), True)
    check("and there is no heading over the nothing that is cached",
          "## Cached documents" in cold, False)
    # Swapping the paragraph means rebuilding the instruction around it, and a
    # concatenation that drops one of its three parts loses a whole section of
    # the prompt without changing anything this section otherwise measures.
    check("the paragraph is still joined to the ones on either side of it",
          (gws.BEFORE_DOCUMENTS in cold, gws.AFTER_DOCUMENTS in cold),
          (True, True))

    # A cache the listing shows all of has no tail either, so the sentence
    # about older documents is as untrue there as it is over nothing. It is
    # replaced rather than dropped: `_scored` weights the document body, which
    # no listing row carries, so the tool still reaches something the prompt
    # does not — and the summaries are in English whatever the document is in.
    one_row, one = with_cache(1)
    check("a single cached document is shown whole, so no tail there",
          gws_cache.TAIL in one_row, False)
    check("but the clause is replaced by one that is true, not dropped",
          clauses(one), (False, True))
    check("and one document is enough to make the whole paragraph true again",
          promises(flat(one)), (True, True, True))
    check("shown under a heading, now that something is under it",
          f"## Cached documents\n{one_row}" in one, True)
    check("and the tool it points at is the one that reads the bodies",
          "Look through the text of every cached document."
          in gws_cache.search_cache.__doc__, True)

    short, warm = with_cache(gws_cache.LISTING_LIMIT)
    check("a cache the listing shows all of names no tail either",
          gws_cache.TAIL in short, False)
    check("so the clause is replaced there too, right up to the limit",
          clauses(warm), (False, True))

    long, full = with_cache(gws_cache.LISTING_LIMIT + 1)
    check("one document more than fits does leave a tail",
          gws_cache.TAIL in long, True)
    check("and there the clause about older ones is the truth, so it stays",
          clauses(full), (True, False))
    # Two subtractions, because the three states differ in two different ways
    # and a prompt that grew a sentence somewhere else would otherwise hide in
    # either one. Between the two cached states only the clause and the listing
    # move; the empty one drops the heading and its blank line as well.
    check("the rest of the instruction is untouched by any of this",
          len(full) - len(warm),
          len(gws.SEARCH_TAIL) - len(gws.SEARCH_SHOWN)
          + len(long) - len(short))
    check("and an empty cache differs by the paragraph and the heading",
          len(warm) - len(cold),
          len(gws.DOCUMENTS.replace(gws.SEARCH_TAIL, gws.SEARCH_SHOWN))
          - len(gws.DOCUMENTS_EMPTY)
          + len("## Cached documents\n") + len(short) + 2)

    print("\n== the constant the cases here read, against the real prompt ==")
    # Five cases in this file read `gws.INSTRUCTION` — the seven commands, the
    # `contains` sentence, the `SEARCH_TAIL` count, the absent clock and the
    # `now` pointer — and nothing in the running app reads it at all.
    # `build_gws_agent` hands ADK the callable (`gws.py:1139`), which rebuilds
    # the same three pieces itself on every model turn (`gws.py:637`) so it can
    # swap the middle one for the cache it has. The constant is therefore a
    # stand-in, and an unpinned stand-in drifts: a paragraph appended to
    # `INSTRUCTION` reaches no model and leaves all five of them green. That is
    # not hypothetical. A draft of #136 appended its new house style there,
    # passed every script in `tools/`, and shipped nothing to the sub-agent.
    #
    # Pinned over `full`, the one cache state where the two are meant to be
    # equal: past `LISTING_LIMIT` the listing has a tail, which is the branch
    # that takes `DOCUMENTS` unmodified, so the prompt is the constant and then
    # the listing and the skills. Two cases rather than one, because a prefix
    # comparison on its own reads a constant with text *deleted* from it as a
    # pass, and half a stand-in drifts as quietly as a whole one.
    check("the constant is the front of what the sub-agent is really run with",
          full.startswith(gws.INSTRUCTION), True,
          f"{len(gws.INSTRUCTION)} characters pinned")
    check("and it ends where the prompt turns to the cache, not before",
          full[len(gws.INSTRUCTION):].startswith(
              f"\n## Cached documents\n{long}"), True,
          repr(full[len(gws.INSTRUCTION):][:40]))

    print("\n== the clock, and which half of the prompt carries it ==")
    # ADK hashes the whole system instruction into one fingerprint for an
    # explicit context cache (`_generate_cache_fingerprint`, google-adk 2.8.0),
    # and `instruction=` here is a callable, so it is re-run and re-hashed on
    # every model turn. A clock in it has to hold still for the length of a
    # question or the question's own turns stop matching each other, and a
    # minute hand does not: a cold document question renders this seven times
    # over 24.5 s, so it crosses a boundary about four times in ten. The clock
    # is in the message instead, which is outside the hash and read once.
    #
    # Both readings are taken either side of midnight, the one instant every
    # resolution crosses at once: rounded to the minute, to the hour or to the
    # day, a clock still in the instruction reads differently across it.
    local = datetime.now().astimezone().tzinfo
    TICKS = (datetime(2026, 9, 8, 23, 59, 30, tzinfo=local),
             datetime(2026, 9, 9, 0, 0, 30, tzinfo=local))

    class Ticks:
        """`datetime` with a `now` that hands back each reading in turn."""

        def __init__(self, *times):
            self._times = iter(times)

        def now(self, tz=None):
            return next(self._times)

    def twice(build):
        """`build()` at each tick, with nothing else about the run moving."""
        kept = gws.datetime
        gws.datetime = Ticks(*TICKS)
        try:
            return build(), build()
        finally:
            gws.datetime = kept

    was_cache_dir = gws_cache.cache_dir
    with tempfile.TemporaryDirectory() as scratch:
        gws_cache.cache_dir = lambda: Path(scratch)
        try:
            before, after = twice(lambda: gws._instruction(None))
        finally:
            gws_cache.cache_dir = was_cache_dir
    check("the instruction is the same on both sides of midnight",
          before == after, True)
    # On the static text, not on the built prompt: the cache listing carries a
    # timestamp per row, which reads as a clock to any pattern loose enough to
    # find one and moves for reasons that have nothing to do with this.
    check("because there is no reading of a clock left in it",
          re.search(r"\d\d:\d\d", gws.INSTRUCTION), None)
    check("and nothing in it pointing at one that is no longer there",
          "Right now it is" in before, False)

    def pointing_at_a_clock() -> list[str]:
        """The sub-agent's tools whose description sends it looking for one.

        Off the built agent rather than off a list written here: the thing
        that goes wrong is a tool docstring left behind by a change to the
        instruction, and a roster typed into a check is one more copy to leave
        behind. ADK puts the whole docstring, `Args:` section included, in the
        declaration's `description` — so a sentence written for a developer is
        a sentence the model reads.
        """
        return sorted(
            tool.name for tool in gws.build_gws_agent().tools
            if isinstance(tool, FunctionTool)
            and re.search(r"clock\s+above|instructions?\s+above",
                          tool._get_declaration().description or "")
        )

    # The instruction is not the whole prompt. `agenda`'s `start` parameter
    # told the model to work the date out from "the clock above" for as long
    # as the instruction ended with one, and went on saying it afterwards.
    check("nor any tool description the sub-agent is shown",
          pointing_at_a_clock(), [])

    def message(request: str, **rest) -> str:
        """The JSON document :func:`~gws.first_turn` hands the sub-agent.

        As the text that goes on the wire, not as a dict: whether the Japanese
        in it survives as Japanese is a property of the serialiser, and
        `json.loads` would put it back either way.
        """
        with tempfile.TemporaryDirectory() as scratch:
            kept = gws_cache.cache_dir
            gws_cache.cache_dir = lambda: Path(scratch)
            try:
                sent, _ = asyncio.run(gws.first_turn(
                    {"request": request, **rest}, gws.RunConfig()))
            finally:
                gws_cache.cache_dir = kept
        return sent.parts[0].text

    def parsed(raw: str) -> dict:
        """The message as fields, or nothing if it is not a JSON object.

        Nothing rather than a `JSONDecodeError`: a message with a sentence of
        prose welded onto the end is one of the things being checked for here,
        and a case that raises reports a broken script instead of it.
        """
        try:
            got = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return got if isinstance(got, dict) else {}

    asked = "what is on tomorrow"
    raw = message(asked, context="", lang="ja")
    # First of the message cases, because the ones below read fields out of it
    # and `parsed` hands back nothing at all when this is what went wrong.
    check("the message is a bare JSON object with no prose around it",
          (raw[:1], raw[-1:], raw.strip() == raw), ("{", "}", True))
    first, second = twice(
        lambda: parsed(message(asked, context="", lang="ja")))
    check("and it carries the clock the instruction gave up",
          (first.get("now"), second.get("now")),
          tuple(f"{tick:%A %Y-%m-%d %H:%M %Z}" for tick in TICKS))
    check("with the fields it always carried still all there",
          {k: v for k, v in first.items() if k != "now"},
          {"request": asked, "context": "", "lang": "ja"})
    check("written as text, not as escapes, the way pydantic wrote it",
          "明日" in message("明日の予定"), True)
    # `GwsRequest` describes what the caller sends. Declaring the clock there
    # would put a field the server fills in on the half of the contract the
    # sub-agent reads as having come from whoever asked.
    check("and not declared on the schema, which is the caller's half",
          "now" in gws.GwsRequest.model_fields, False)
    check("and the sub-agent is told where to read it",
          "`now` in your message" in flat(gws.INSTRUCTION), True)

    print("\n== a step that raised ==")
    with Kept() as kept:
        try:
            with _timing.step("doomed"):
                time.sleep(SLOW)
                raise RuntimeError("the network went")
        except RuntimeError:
            pass
    check("a step whose block raised is still logged",
          [s["step"] for s in kept.steps], ["doomed"])
    check("with the time it spent failing", kept.steps[0]["ms"] >= 40, True,
          f"{kept.steps[0]['ms']}ms")

    print("\n== inside a question ==")
    with Kept() as kept:
        with _timing.question("q1"):
            time.sleep(SLOW)
            with _timing.step("inner"):
                time.sleep(SLOW)
    inner = kept.named("inner")
    check("the step carries the question's name", inner["q"], "q1")
    check("and starts where it started, not at zero",
          40 <= inner["at"] <= 400, True, f"at {inner['at']}ms")
    with Kept() as kept:
        with _timing.question("q1"):
            pass
        with _timing.step("after"):
            pass
    check("and a step after the block is outside it again",
          "q" in kept.named("after"), False)

    print("\n== nesting ==")
    with Kept() as kept:
        with _timing.question("q2"):
            with _timing.step("outer"):
                with _timing.step("nested"):
                    time.sleep(SLOW)
                time.sleep(SLOW)
    outer, nested = kept.named("outer"), kept.named("nested")
    check("the inner step is logged first, because it finished first",
          [s["step"] for s in kept.steps], ["nested", "outer"])
    check("and sits inside the outer one",
          outer["at"] <= nested["at"]
          and nested["at"] + nested["ms"] <= outer["at"] + outer["ms"] + 2,
          True, f"outer {outer['at']}+{outer['ms']}, "
                f"nested {nested['at']}+{nested['ms']}")

    print("\n== the union, which is what nesting costs ==")
    check("two disjoint spans add up",
          latency_report.covered(spans((0, 100), (200, 50))), 150)
    check("a nested span is not charged twice",
          latency_report.covered(spans((0, 100), (10, 20))), 100)
    check("nor is an identical one",
          latency_report.covered(spans((0, 100), (0, 100))), 100)
    check("an overlap is charged once for the overlap",
          latency_report.covered(spans((0, 100), (50, 100))), 150)
    check("spans that touch are one span",
          latency_report.covered(spans((0, 100), (100, 100))), 200)
    check("and the input does not have to be in order",
          latency_report.covered(spans((200, 50), (0, 100))), 150)
    check("nothing covers nothing", latency_report.covered([]), 0)
    check("a step with no offset is not an interval",
          latency_report.covered([{"step": "x", "ms": 100}]), 0)

    print("\n== percentiles and units ==")
    check("p50 of one value is that value", latency_report.pct([7.0], 0.5), 7.0)
    check("p90 of one value is too", latency_report.pct([7.0], 0.9), 7.0)
    ten = [float(n) for n in range(1, 11)]
    check("p50 is nearest-rank, not interpolated",
          latency_report.pct(ten, 0.5), 6.0)
    check("and p90 is the ninth of ten", latency_report.pct(ten, 0.9), 10.0)
    check("under a second reads in milliseconds",
          latency_report.dur(999.4), "999ms")
    check("a second reads in seconds", latency_report.dur(1000), "1.00s")
    check("and so does half a minute", latency_report.dur(35150), "35.15s")

    print("\n== reading a log back ==")
    good = ('2026-09-08 06:08 INFO ai_terminal.live.tools._timing: gws-step '
            '{"step": "read", "ms": 623, "q": "abc", "at": 3346}')
    check("a whole line parses", latency_report.parsed(good),
          {"step": "read", "ms": 623, "q": "abc", "at": 3346})
    check("a line with no step in it is not one",
          latency_report.parsed("INFO tool call gws {'request': '<2 chars>'}"), None)
    # What uvicorn's stderr and the logging module do to each other when they
    # share a file descriptor. Seen in `/tmp/live-server.log`, and the reason
    # the pattern stops at the first `}` instead of running to end of line.
    spliced = (good[:good.index('"at"')]
               + "2026-09-08 06:08 INFO ai_terminal.live.main: "
               "tool call gws {'request': '<2 chars>'}")
    check("half a line welded to another line is not a measurement",
          latency_report.parsed(spliced), None)
    check("but a whole one with something welded after it still is",
          latency_report.parsed(good + "2026-09-08 INFO uvicorn: GET /"),
          json.loads(good.split("gws-step ")[1]))

    page_load_timing_cases()

    print(f"\nall {RAN} cases hold")


def page_load_timing_cases() -> None:
    print("\n== page load latency ceiling (#468) ==")
    # #443 spawned gws CLI for an auth check on page load, increasing load time
    # from 1.1s to 4.6s. Serving / and /api/config must be purely deterministic
    # in-memory lookups without any blocking subprocess or hook on the hot path.
    from unittest.mock import patch
    from starlette.testclient import TestClient
    from ai_terminal.live import main

    PAGE_LOAD_MAX_MS = 100.0

    client = TestClient(main.app, base_url="http://localhost")
    try:
        # Time GET /
        t0 = time.perf_counter()
        resp_root = client.get("/")
        root_ms = (time.perf_counter() - t0) * 1000.0
        check("GET / responds with 200 OK", resp_root.status_code, 200)
        check(f"GET / response time is strictly under {PAGE_LOAD_MAX_MS:.0f}ms",
              root_ms < PAGE_LOAD_MAX_MS, True, f"{root_ms:.2f}ms")

        # Time GET /api/config
        t0 = time.perf_counter()
        resp_cfg = client.get("/api/config")
        cfg_ms = (time.perf_counter() - t0) * 1000.0
        check("GET /api/config responds with 200 OK", resp_cfg.status_code, 200)
        check(f"GET /api/config response time is strictly under {PAGE_LOAD_MAX_MS:.0f}ms",
              cfg_ms < PAGE_LOAD_MAX_MS, True, f"{cfg_ms:.2f}ms")

        # Verify that a simulated blocking hook or slow subprocess on the load path exceeds ceiling
        orig_profiles = main._profiles

        def slow_profiles():
            time.sleep(0.12)  # 120ms simulated blocking hook / slow subprocess
            return orig_profiles()

        with patch.object(main, "_profiles", side_effect=slow_profiles):
            t0 = time.perf_counter()
            resp_slow = client.get("/api/config")
            slow_ms = (time.perf_counter() - t0) * 1000.0
            check("simulated slow hook still responds with 200 OK", resp_slow.status_code, 200)
            check("simulated slow hook on load path exceeds latency ceiling",
                  slow_ms >= PAGE_LOAD_MAX_MS, True, f"{slow_ms:.2f}ms >= {PAGE_LOAD_MAX_MS:.0f}ms")
    finally:
        client.close()


if __name__ == "__main__":
    main()

