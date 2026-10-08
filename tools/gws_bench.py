#!/usr/bin/env python3
"""Count what one Workspace question costs, over and over.

The number worth watching is `gws` calls per question. Seconds move with the
network and with how busy the model is; calls move only when the instruction or
the tools change, which is the thing being worked on. A calendar question that
used to cost four commands and now costs one is the whole point of `agenda`,
and there was no way to show that without re-running it by hand and reading the
log.

This drives the sub-agent on its own — `build_gws_agent()` through a plain ADK
``Runner``, no live agent, no audio, no card. Those add a minute of setup and a
lot of variance to a number that does not depend on any of them.

The account is a real personal one and the answers change daily, so nothing
here asserts on what comes back. Read the call counts against the ceilings in
:data:`QUESTIONS`; the answers are printed with `-v` when you want to see
whether a cheap run was also a good one.

    uv run python tools/gws_bench.py            # every question, 3 runs each
    uv run python tools/gws_bench.py -n 5 -q mail
    GWS_MODEL=gemini-3.5-flash-lite uv run python tools/gws_bench.py -v

Two things skew a run. The document cache is shared with the live server, so a
question whose answer is already sitting in `gws_cache/` costs less than it
would cold. And the sub-agent's budget refuses the thirteenth chargeable call,
so a run reporting 13 or more was cut off rather than finished — `spend` counts
the call it refuses, and it keeps counting the ones after it, so 12 is a
complete run and not a number a cut-off can produce.

Chargeable, not "command": what the budget counts is calls to the sub-agent's
tools, and `agenda` answers from the Calendar API without starting a `gws`
subprocess at all. The two ceiling-1 questions below are exactly that call, so
their whole cost is a number no shell would show.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
load_dotenv(REPO_ROOT / ".env", override=True)

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from ai_terminal.live.tools import _gws_run  # noqa: E402
from ai_terminal.live.tools.gws import build_gws_agent  # noqa: E402

APP = "gws_bench"
USER = "bench"

# Three questions, picked because they cost three very different amounts.
# `ceiling` is the median a run may not exceed, not the median it should hit:
# the typical cost is in the comment beside it, and the gap between the two is
# the room a bad day has before anyone needs to look.
QUESTIONS = {
    "calendar": {
        # One `agenda` call, and nothing about the question needs a second.
        "ceiling": 1,
        "request": "What is on their calendar tomorrow?",
        "context": "",
    },
    "week": {
        # Also one: `agenda` takes the window, so a week costs what a day does.
        "ceiling": 1,
        "request": "What is on their calendar this week?",
        "context": "",
    },
    "mail": {
        # The question that started all of this: 21 commands in 90 seconds,
        # five searches for the same thing in five dialects. Eleven was where
        # it landed after the instruction was rewritten, close enough to the
        # budget that 12 is still a clean run. So the line goes at the budget's
        # own ceiling, where a median above it means calls were refused.
        "ceiling": _gws_run.STOP_AT,
        "request": "Is there anything in their mail that needs a reply?",
        "context": "",
    },
    "next": {
        # The question the model default was chosen on, and the only one here
        # whose answer is worth reading rather than counting: a search with
        # about a hundred matches, where the cheap answer is a result count
        # and the useful one names the threads. Kept so that changing the
        # model can be argued against the same question it was argued on. See
        # "What Flash bought, and what it cost" in the README.
        "ceiling": _gws_run.STOP_AT,
        "request": ("Search their gmail for anything about Google Cloud "
                    "Next."),
        "context": "Their session at Next is BRK2-109.",
    },
}


async def ask(agent, question: dict) -> tuple[int, float, str]:
    """Run one question and return its command count, seconds and answer."""
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name=APP, user_id=USER)
    runner = Runner(app_name=APP, agent=agent, session_service=sessions)

    message = types.Content(role="user", parts=[types.Part(text=json.dumps({
        "request": question["request"],
        "context": question["context"],
        "lang": "en",
    }))])

    answer = ""
    started = time.monotonic()
    # The count is read off the budget rather than counted from the log: it is
    # the same object the stop enforces, so the two cannot disagree.
    with _gws_run.budget() as tally:
        async for event in runner.run_async(
            user_id=USER, session_id=session.id, new_message=message
        ):
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text or "" for p in event.content.parts)
    return tally.spent, time.monotonic() - started, answer.strip()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("-n", "--runs", type=int, default=3,
                        help="runs per question (default 3)")
    parser.add_argument("-q", "--question", action="append", choices=QUESTIONS,
                        help="only this question; repeatable")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print each answer as well as its cost")
    args = parser.parse_args()

    agent = build_gws_agent()
    print(f"model {os.getenv('GWS_MODEL', 'gemini-3.8-flash')}, "
          f"{args.runs} run(s) per question\n")

    worse = False
    for name in args.question or QUESTIONS:
        question = QUESTIONS[name]
        ceiling = question["ceiling"]
        print(f"{name}: {question['request']}  "
              f"(over {ceiling} call{'' if ceiling == 1 else 's'} "
              "is a regression)")
        calls, seconds = [], []
        for run in range(1, args.runs + 1):
            spent, elapsed, answer = await ask(agent, question)
            calls.append(spent)
            seconds.append(elapsed)
            print(f"  run {run}: {spent:>2} calls  {elapsed:>5.1f}s")
            if args.verbose:
                print(f"    {answer}")
        median = statistics.median(calls)
        print(f"  median: {median:g} calls  "
              f"{statistics.median(seconds):.1f}s\n")
        worse = worse or median > question["ceiling"]

    return 1 if worse else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
