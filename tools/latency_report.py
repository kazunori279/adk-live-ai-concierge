#!/usr/bin/env python3
"""Where a Workspace document question spends its seconds.

Companion to `tools/gws_bench.py`, which counts calls. Calls are the number
that moves when the instruction changes; this is the number the person standing
in front of the terminal actually experiences, and the two do not track each
other — the sub-agent's own thinking makes no calls at all and is most of a
cold answer.

Every step logs itself through :mod:`ai_terminal.live.tools._timing`, so the
work here is arithmetic: run the questions, collect the lines, and print a
percentile per step. Nesting is handled by interval union rather than by
summing, because `read` and `record` both contain a `drive.meta`; adding
those up would charge the same second twice and report more time than the
question took.

**Model** is the row that is not a step. No context manager can wrap a model
turn — ADK owns that side — so it is measured by subtraction: the question's
wall clock, minus every interval a step covered. What is left is the sub-agent
thinking, and it is usually most of the answer.

    uv run python tools/latency_report.py              # 2 cold + 2 warm, 3 docs
    uv run python tools/latency_report.py -n 1 -q trip
    uv run python tools/latency_report.py -n 1 -q trip --trace
    uv run python tools/latency_report.py --log /tmp/live-server.log

`--trace` names each sub-agent turn as it happens, which is the other half of
the same question: the table says how many turns a question cost, and the trace
says what they were spent on.

Cold runs get their own cache directory, so nothing in `gws_cache/` is moved or
deleted to make a document uncached. Warm runs reuse whatever the cold run left
there, which is what a warm question is.

`--log` totals a real session instead of running anything: the same table off a
server log, including the two steps this script cannot reach — the live model
deciding to call `gws` at all, and the answer going back into the live queue.
Those live inside the `total` step, which only `gws.py` opens.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
load_dotenv(REPO_ROOT / ".env", override=True)

from google.adk.agents.run_config import RunConfig  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402

from ai_terminal.live.tools import _gws_run, _timing, gws_cache  # noqa: E402

APP = "latency_report"
USER = "latency"

# Three real documents in the owner's Drive, each asked twice: once with an
# empty cache and once with the copy the first question left behind. Two
# questions rather than the same one twice, because a repeated question is
# answered from a warm cache *and* a shorter search — the second phrasing keeps
# the search honest without needing a different document.
QUESTIONS = {
    "trip": {
        "cold": "In their US trip itinerary document in Drive, what time does "
                "the flight to San Francisco leave, and where from?",
        "warm": "In their US trip itinerary document in Drive, which hotel are "
                "they in in New York, and for which nights?",
    },
    "llm": {
        "cold": "In their LLM notes document in Drive, what does it say about "
                "Mixture of Experts?",
        "warm": "In their LLM notes document in Drive, what does it say about "
                "KV cache memory?",
    },
    "paper": {
        "cold": "In their paper notes document in Drive, what does it say "
                "about token compression?",
        "warm": "In their paper notes document in Drive, which robotics "
                "papers does it cover?",
    },
}

# Printed in this order, coarse first, so the table reads as a breakdown rather
# than as an alphabetical list. A step that never ran is left out.
ORDER = ["subagent", "resolve", "instruction", "listing", "search", "read",
         "cli", "drive.meta", "record"]

# What a step is, in one line, for the reader of the table who has not read
# `gws.py`. Same keys as ORDER.
MEANS = {
    "subagent": "the whole sub-agent run, everything below plus its thinking",
    "resolve": "which cached document the question names, before turn one",
    "instruction": "rebuilding the prompt, once per model turn",
    "listing": "the cached-documents list inside that prompt",
    "search": "search_cache: score every record, read every body",
    "read": "read_cache: the file, plus the freshness check",
    "cli": "one `gws` subprocess: start, auth, API call",
    "drive.meta": "`gws drive files get` for three fields",
    "record": "headering what was fetched: drive.meta, then two local writes",
}

# `summarize.bg` is deliberately not in either table. It runs after the answer,
# outside any question — `_timing.detached` — so it carries no `q`, and the
# grouping below never sees it. It is still logged, and `grep summarize.bg` on
# the server log is how you check the work is happening at all.

# `[^}]*` rather than `.*`, and not anchored to the end of the line. A step
# record has no nested object in it, and a server log does have spliced lines:
# uvicorn's stderr and the logging module write the same fd unsynchronised, so
# a step line arrives with another line's text welded onto its tail often
# enough that anchoring loses real measurements.
_LINE = re.compile(re.escape(_timing.FIELD) + r" (\{[^}]*\})")


def parsed(line: str) -> dict | None:
    """The step record in this line, or None if there is not a whole one.

    The splice cuts both ways: a line welded together mid-record leaves a
    fragment that matches the pattern and is not JSON, and dropping it is the
    only thing to do with it — the numbers it was carrying went with the weld.
    """
    found = _LINE.search(line)
    if not found:
        return None
    try:
        return json.loads(found.group(1))
    except json.JSONDecodeError:
        return None


class Collector(logging.Handler):
    """Keep the step lines belonging to a question instead of printing them.

    A step with no `q` was logged by work that outlives the question that
    queued it — `summarize.bg`, and see `_timing.detached`. It runs while this
    handler happens to be installed, and the table it would land in is a table
    of what a question costs. Dropped here so this path groups the way
    :func:`from_log` does, which is the whole point of having two.
    """

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.steps: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        step = parsed(record.getMessage())
        if step is not None and "q" in step:
            self.steps.append(step)


def covered(steps: list[dict]) -> float:
    """Milliseconds at least one step was running, counting each once.

    The union of `[at, at + ms)` over every step. `total` and `subagent` are
    left to the caller to exclude: both span nearly the whole question, and
    including either would say every millisecond was accounted for.
    """
    spans = sorted((s["at"], s["at"] + s["ms"]) for s in steps if "at" in s)
    total, end = 0.0, None
    for start, stop in spans:
        if end is None or start > end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total


def pct(values: list[float], share: float) -> float:
    """Nearest-rank percentile. Small samples, so no interpolation."""
    ranked = sorted(values)
    return ranked[min(len(ranked) - 1, int(share * len(ranked)))]


def dur(ms: float) -> str:
    """Milliseconds under a second, seconds over it.

    Two units in one column, because the rows this table exists to compare are
    four orders of magnitude apart: `listing` is under a millisecond and the
    question is half a minute. Printed as seconds throughout, every cheap step
    reads `0.00s` and the table says nothing about which of them is cheapest.
    """
    return f"{ms:.0f}ms" if ms < 1000 else f"{ms / 1000:.2f}s"


def summarise(runs: list[dict]) -> None:
    """Print one table: a row per step, p50 and p90 across these runs."""
    if not runs:
        print("   (no runs)")
        return

    names = [n for n in ORDER if any(
        s["step"] == n for run in runs for s in run["steps"])]
    names += sorted({s["step"] for run in runs for s in run["steps"]}
                    - set(names) - {"total"})

    print(f"   {'step':<12} {'per q':>5} {'p50':>7} {'p90':>7}   what it is")
    for name in names:
        per = [sum(s["ms"] for s in run["steps"] if s["step"] == name)
               for run in runs]
        count = statistics.median(
            len([s for s in run["steps"] if s["step"] == name])
            for run in runs)
        print(f"   {name:<12} {count:>5g} {dur(pct(per, 0.5)):>7} "
              f"{dur(pct(per, 0.9)):>7}   {MEANS.get(name, '')}")

    # Subtraction, not a step: see the module docstring.
    thinking = [run["total"] - covered(
        [s for s in run["steps"] if s["step"] not in ("total", "subagent")])
        for run in runs]
    turns = [len([s for s in run["steps"] if s["step"] == "instruction"])
             for run in runs]
    print(f"   {'model':<12} {statistics.median(turns):>5g} "
          f"{dur(pct(thinking, 0.5)):>7} {dur(pct(thinking, 0.9)):>7}"
          "   what is left over: sub-agent turns, `per q` is how many")
    totals = [run["total"] for run in runs]
    print(f"   {'TOTAL':<12} {len(runs):>5g} {dur(pct(totals, 0.5)):>7} "
          f"{dur(pct(totals, 0.9)):>7}   `per q` is the sample size")


async def ask(agent, question: str, name: str, trace: bool = False) -> dict:
    """Run one question through the sub-agent and return its timings.

    `trace` names every turn as it happens. The table below says a cold
    question costs nine or ten model turns; it cannot say what they were, and
    the answer to that turned out to be "five of them learn nothing about the
    document". The server log is no help either — `_log_tool_traffic` only sees
    the live agent, so a sub-agent's `load_skill` never reaches it. This loop
    is already iterating the events the trace is made of.
    """
    from ai_terminal.live.tools.gws import first_turn

    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name=APP, user_id=USER)
    runner = Runner(app_name=APP, agent=agent, session_service=sessions)

    collector = Collector()
    logging.getLogger(_timing.__name__).addHandler(collector)
    answer = ""
    started = time.monotonic()
    try:
        # The same three the server's tool entry opens, and all three for the
        # same reason: a run missing one of them is not the run being measured.
        # `question` because `at` has to be measured from the same place
        # here as it is there; `budget` because without it `spend()` has
        # nothing to charge and refuses nothing, and an unbudgeted run is not
        # a question anyone will ever ask — the first one measured spent 37
        # `gws` calls and five minutes; `remembering` because without it a
        # cold question asks Drive about the document twice and the
        # `drive.meta` row reads 2.
        with _timing.question(name), _gws_run.budget(), \
                gws_cache.remembering():
            # Inside the question, so the resolver's own step is charged to it
            # and lands in the table like every other step.
            message, config = await first_turn(
                {"request": question, "context": "", "lang": "en"},
                RunConfig())
            async for event in runner.run_async(
                user_id=USER, session_id=session.id, new_message=message,
                run_config=config,
            ):
                if trace:
                    for part in (event.content and event.content.parts) or []:
                        call = part.function_call
                        if call is not None:
                            print(f"    {time.monotonic() - started:6.2f}s  "
                                  f"{call.name} {str(call.args)[:80]}")
                if (event.is_final_response() and event.content
                        and event.content.parts):
                    answer = "".join(p.text or "" for p in event.content.parts)
    finally:
        logging.getLogger(_timing.__name__).removeHandler(collector)
    return {
        "steps": collector.steps,
        "total": (time.monotonic() - started) * 1000,
        "answer": answer.strip(),
    }


async def measure(args) -> int:
    """Run the questions against a scratch cache and print both tables."""
    with tempfile.TemporaryDirectory(prefix="latency-cache-") as scratch:
        os.environ["GWS_CACHE_DIR"] = scratch
        # After the environment, not before: `build_gws_agent` reads .env, and
        # `gws_cache.cache_dir` is memoised on first use.
        from ai_terminal.live.tools.gws import build_gws_agent
        agent = build_gws_agent()
        print(f"model {os.getenv('GWS_MODEL', 'gemini-3.8-flash')}, "
              f"cache {scratch}, {args.runs} run(s) per question\n")

        runs = {"cold": [], "warm": []}
        for name in args.question or QUESTIONS:
            for run in range(1, args.runs + 1):
                # Emptied per run, not per script: the warm question below
                # fills the cache, and without this the second run's "cold"
                # question would find the first run's copy still sitting there.
                for stale in Path(scratch).iterdir():
                    stale.unlink()
                for kind in ("cold", "warm"):
                    question = QUESTIONS[name][kind]
                    if args.trace:
                        print(f"  {name} {kind} {run}: {question}")
                    result = await ask(agent, question,
                                       f"{name}-{kind}-{run}", args.trace)
                    print(f"  {name} {kind} {run}: "
                          f"{result['total']/1000:>5.1f}s  {question}")
                    if args.verbose:
                        print(f"    {result['answer']}")
                    runs[kind].append(result)

        for kind in ("cold", "warm"):
            print(f"\n== {kind} ==")
            summarise(runs[kind])
    return 0


def from_log(path: Path) -> int:
    """Total a server log instead of running anything."""
    steps: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        record = parsed(line)
        if record is not None:
            steps.setdefault(record.get("q", "?"), []).append(record)

    runs = {"cold": [], "warm": []}
    for question in steps.values():
        whole = [s for s in question if s["step"] == "total"]
        if not whole:
            # A question the log caught the middle of — the server was started
            # or stopped inside it — has no wall clock to subtract from.
            continue
        # On `record` and not on `summarize`: a cold question is one that
        # fetched a document, and since #32 the summarising happens after the
        # question is over and is filed under no question at all. `record` is
        # the step that means a file was written, and it is still inside.
        kind = ("cold" if any(s["step"] == "record" for s in question)
                else "warm")
        runs[kind].append({"steps": question, "total": whole[0]["ms"]})

    for kind in ("cold", "warm"):
        print(f"\n== {kind}, from {path} ==")
        summarise(runs[kind])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("-n", "--runs", type=int, default=2,
                        help="runs per question per state (default 2)")
    parser.add_argument("-q", "--question", action="append", choices=QUESTIONS,
                        help="only this document; repeatable")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print each answer as well as its cost")
    parser.add_argument("--trace", action="store_true",
                        help="name every sub-agent turn as it happens")
    parser.add_argument("--log", type=Path,
                        help="total this server log instead of running")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    # The step lines are wanted by the collector and by nothing else. INFO so
    # they are emitted at all with the root logger at WARNING, and no
    # propagation so they do not also scroll past on stderr.
    steps = logging.getLogger(_timing.__name__)
    steps.setLevel(logging.INFO)
    steps.propagate = False

    if args.log:
        return from_log(args.log)
    return asyncio.run(measure(args))


if __name__ == "__main__":
    raise SystemExit(main())
