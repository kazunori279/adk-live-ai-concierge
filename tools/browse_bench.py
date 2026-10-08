#!/usr/bin/env python3
"""Count what one browse task costs, over and over.

The numbers worth watching are **steps per task** and **seconds per step**. A
better per-step model can finish in fewer steps than it costs in latency, and
waiting for the page instead of sleeping a flat two seconds moves the second
number without touching the first — neither trade is visible unless both are
measured, and neither was, because there was no way to run a task twice without
driving the whole live agent by voice.

This drives :func:`ai_terminal.live.browser.run` directly. No live agent, no
audio, no card: those add a minute of setup and a lot of variance to a number
that does not depend on any of them.

    uv run python tools/browse_bench.py             # every task, 3 runs each
    uv run python tools/browse_bench.py -n 5 -t train
    BROWSE_MODEL=gemini-2.5-flash uv run python tools/browse_bench.py

Nothing here asserts on the answer. The pages are live and their contents move
— a timetable changes with the timetable — so the answers are printed with `-v`
when you want to see whether a cheap run was also a right one. What is asserted
is the step count, against the ceiling beside each task: a run over it either
took a wrong route or gave up, and both are regressions however fast they were.

Two things skew a run. The first task after a cold start pays Chromium's launch
— 2.5 s on the Pi, less on a Mac — once per run, since every task launches its
own. And these are real sites: one of them redesigning its front page is a real
step-count change that has nothing to do with the code.
"""

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
load_dotenv(REPO_ROOT / ".env", override=True)

from ai_terminal.live import browser  # noqa: E402

# Three tasks, picked because they cost three very different amounts and
# because none of them needs a login, spends anything or changes a page.
# `ceiling` is the step count a run may not exceed, not the one it should hit;
# the typical cost is in the comment beside it.
TASKS = {
    "page": {
        # One `goto` and the answer is on the screenshot. This is the control:
        # it holds the launch cost and the per-step model cost and nothing
        # else, so a change that only moves the settle wait cannot move it.
        "ceiling": 2,
        "task": "on example.com, what does the page say?",
    },
    "wiki": {
        # Two or three: navigate, maybe scroll or search, read.
        "ceiling": 4,
        "task": "on en.wikipedia.org, in what year was the Raspberry Pi 5 "
                "released?",
    },
    "train": {
        # The spike's task, and the one the settle wait is really paid on: a
        # form to fill in, two fields and a submit, then a results page.
        # Typically six steps.
        "ceiling": 9,
        "task": "on jorudan.co.jp, how long does the train from 恵比寿 to "
                "金沢 take?",
    },
}


async def run_once(task: str) -> dict:
    """One task, with the per-step timings the run already records."""
    started = time.monotonic()
    result = await browser.run(task, "en")
    result["wall"] = time.monotonic() - started
    return result


def summarise(name: str, runs: list[dict], ceiling: int, verbose: bool) -> bool:
    steps = [len(r["steps"]) for r in runs]
    seconds = [r["seconds"] for r in runs]
    # Per step and not per run, because the step count is the other axis and
    # a change that halves the wait while doubling the steps must not read as
    # an improvement.
    per_step = [r["seconds"] / max(len(r["steps"]), 1) for r in runs]
    print(f"  median: {statistics.median(steps):g} steps  "
          f"{statistics.median(seconds):.1f}s  "
          f"{statistics.median(per_step):.1f}s/step")
    if verbose:
        for run in runs:
            print(f"    {run['answer'][:100]}")
    return statistics.median(steps) > ceiling


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("-n", "--runs", type=int, default=3,
                        help="runs per task (default 3)")
    parser.add_argument("-t", "--task", action="append", choices=TASKS,
                        help="only this task; repeatable")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print each answer as well as its cost")
    args = parser.parse_args()

    print(f"model {browser._model()}, {args.runs} run(s) per task\n")

    worse = False
    for name in args.task or TASKS:
        entry = TASKS[name]
        print(f"{name}: {entry['task']}  "
              f"(over {entry['ceiling']} steps is a regression)")
        runs = []
        for number in range(1, args.runs + 1):
            result = await run_once(entry["task"])
            runs.append(result)
            waits = sum(step.get("ms", 0) for step in result["steps"]) / 1000
            print(f"  run {number}: {len(result['steps']):>2} steps  "
                  f"{result['seconds']:>5.1f}s  {waits:>4.1f}s settling")
        worse = summarise(name, runs, entry["ceiling"], args.verbose) or worse
        print()

    return 1 if worse else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
