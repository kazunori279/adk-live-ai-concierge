"""Where the seconds went, one log line per step.

A Workspace question is a live model turn, a sub-agent with its own model and
four tools, one or two subprocesses, and a summariser — and until this module
existed the only thing anyone could say about a slow one was that it was slow.
Every step wraps itself in :func:`step`, which writes one INFO line under the
same prefix, so `grep gws-step` over a server log is a timeline of the run and
`tools/latency_report.py` turns a pile of them into percentiles.

Two numbers per line, not one. `ms` is how long the step took; `at` is how far
into the question it started. `at` is the one that finds the *gaps* — a model
turn is not a step anything here can wrap, so the second a question spends
waiting for the sub-agent to decide what to do shows up as space between two
steps and nowhere else.

Always on, at INFO. A question logs a dozen lines, which is small beside what
`_log_tool_traffic` already writes per turn, and a measurement you have to
switch on is one nobody has running when the slow question happens.
"""

import contextlib
import contextvars
import json
import logging
import time

logger = logging.getLogger(__name__)

# The prefix every line carries, so one grep finds them all and nothing else.
FIELD = "gws-step"


class _Question:
    """One question's identity and the moment it started."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.at = time.perf_counter()


_QUESTION: contextvars.ContextVar[_Question | None] = contextvars.ContextVar(
    "ai_terminal_timing_question", default=None
)


@contextlib.contextmanager
def question(name: str):
    """Group every step started inside this block under one question.

    A ContextVar for the same reason `_gws_run.budget` is one: nothing between
    the tool and a cache read can carry an argument, because ADK calls a tool
    with the model's arguments and nothing else. `asyncio.create_task` and
    `asyncio.to_thread` both copy the context, so the sub-agent's task and the
    thread `_run_sync` runs in stay inside the question they belong to.
    """
    token = _QUESTION.set(_Question(name))
    try:
        yield
    finally:
        _QUESTION.reset(token)


@contextlib.contextmanager
def detached():
    """Log the steps started inside this block against no question at all.

    For work that outlives the question that queued it — `gws_cache.finish`, so
    far. The context was copied when the task was created, so the question is
    still in it and every step in there would be filed under a question that
    had already been answered, inflating its total by however long the deferred
    work took and moving its `at` offsets out past the answer.

    The line is still logged, with a `step` and an `ms` and no `q`. That is
    what makes the work measurable at all — `tools/latency_report.py` groups on
    `q` and so ignores these, and a `grep` for the step name still finds them.
    """
    token = _QUESTION.set(None)
    try:
        yield
    finally:
        _QUESTION.reset(token)


@contextlib.contextmanager
def step(name: str, **fields):
    """Time this block and log what it cost.

    `finally`, so a step that raised is still reported. A failure that takes
    nine seconds is the interesting kind, and a timing module that only
    measures the happy path would drop exactly those.

    Extra keyword arguments go on the line as they are, for whatever makes the
    step's cost readable afterwards: how many characters were summarised, how
    many files a command wrote. Yields that same dict, so a step whose size is
    only known once it has run can put it there on the way out.
    """
    at = time.perf_counter()
    try:
        yield fields
    finally:
        mark(name, (time.perf_counter() - at) * 1000, at, **fields)


def mark(name: str, ms: float, at: float | None = None, **fields) -> None:
    """Log one step directly, for a duration that was measured elsewhere."""
    record = {"step": name, "ms": round(ms)}
    asked = _QUESTION.get()
    if asked is not None:
        record["q"] = asked.name
        record["at"] = round(((at or time.perf_counter()) - asked.at) * 1000)
    record.update(fields)
    logger.info("%s %s", FIELD, json.dumps(record, ensure_ascii=False,
                                           default=str))
