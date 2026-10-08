"""A browser the agent can drive, for the pages no API covers.

The grounding tools handle anything with an API behind it. This is the rest:
a train timetable, a shop's opening hours, a status page that publishes no
JSON. A headless Chromium takes a screenshot, a small model looks at it and
says what to click, and the loop repeats until it can answer.

The spike behind the numbers is in ``spikes/browser-use/``; the README's
*Browser use* section has them. Two of its findings shape this module:

- Perception is the screenshot **plus** a numbered list of the interactive
  elements. Screenshot alone reads fine but cannot be acted on — the model
  returns the placeholder text it sees, Playwright fails to match it, and the
  loop spins. Indices come from the live DOM, so they always resolve.
- ``--password-store=basic`` is mandatory. Without it Chromium waits on the
  GNOME keyring, which puts a dialog on a display nobody is watching, and its
  *network stack* blocks behind that. Page loads hang with nothing logged.

A run takes tens of seconds, which is far too long to hold a spoken turn open,
so nothing here is called inline. ``tools/browse.py`` starts it in the
background and the answer comes back as its own turn.

Which Chromium it drives is :func:`_cdp`'s decision and there are two answers.
Unset, a headless throwaway that is logged out of everything, which is the
default and everything above is about. Set, the browser already on the panel:
signed in, visible, and the only one there is, because a Chromium profile
directory takes one process at a time. :meth:`_Session._attach` is that path
and carries what it costs.
"""

import asyncio
import base64
import contextlib
import functools
import json
import logging
import os
import re
import time
from urllib.parse import urlparse

from google.genai import types

from ._log import _sized
from .tools._client import global_client
from .tools._lang import SCREEN_STYLE, answer_in

logger = logging.getLogger(__name__)

VIEWPORT = {"width": 1280, "height": 800}
MAX_ELEMENTS = 60

# How long a step may spend waiting for the page to finish reacting, and how
# long it always spends. This used to be one number, spent unconditionally: 2 s
# after every action, including a `press` that changed nothing. The spike's
# seven-step run was 42 s and 14 s of it was this line sleeping.
#
# The floor is not zero because `wait_for_load_state` needs the navigation to
# have been committed before it is called — Playwright says so in its own
# docstring — and a click that navigates has not committed the instant it
# returns. `networkidle` stays in the frame's `_load_states` until a new
# document commits, so a wait made too early answers about the outgoing page,
# returns at once, and the screenshot catches the page the step was supposed to
# leave.
#
# The floor makes that window 200 ms narrower; it does not close it, and a
# server slower than that still slips through. Closing it takes an event rather
# than a duration, which is what `act` does for `press` — the one action that
# can navigate without Playwright waiting for it. The floor stays as the cheap
# backstop for everything else.
#
# The ceiling is what the two together may cost, so the worst case is what the
# unconditional sleep used to cost rather than a tenth more than it. That worst
# case is not rare: `networkidle` is flagged DISCOURAGED and never fires on a
# page holding a socket open or polling a beacon, which a train timetable does.
# So the saving is real but page-dependent, and `tools/browse_bench.py` is how
# you find out which pages it lands on.
MIN_SETTLE_MS = 200
SETTLE_MS = 2000

# What a click may spend getting itself dispatched — the actionability retry
# and the dispatch, and nothing after them. That is the whole of the window in
# which a tab that opens is attributed to the run, so it wants to be small.
#
# It used to be the page's response budget as well, because playwright waits a
# click-caused navigation out inside the click's own timeout: against a loopback
# server answering in 1,500 ms, `click(timeout=1000)` raises `TimeoutError` at
# 1,002 ms with the page still on the old URL. One number could not be right for
# both — 2 s is generous for a dispatch and short for a page, so a link that
# answered in 2.5 s came back as `failed: click 0 did not land in time`, skipped
# the settle, and left the next screenshot on the pre-click page while the
# navigation completed a moment later. `no_wait_after=True` takes the wait out
# of the click and `CLICK_NAV_MS` below carries it instead.
#
# 2 s, which is `SETTLE_MS` and not a coincidence: `press` above already gives a
# dispatched action's navigation exactly that, for the same reason. Measured on
# pi2 (Chromium 152, aarch64), the dispatch alone is 135-236 ms over five runs.
# Nothing measured says what the actionability retry needs on that machine — a
# cookie banner that animates in over a second is inside this and a slower one
# is not — so it stays where it was rather than following the dispatch down.
CLICK_MS = SETTLE_MS

# And what the page it navigated to may spend answering. Only ever paid by a
# click that actually issued a navigation request: an in-place click leaves the
# waiter early, so the ordinary case still costs a dispatch and the settle
# floor. 8 s restores what a click had before `CLICK_MS` was made to cover both,
# and is a ceiling rather than a cost.
CLICK_NAV_MS = 8000

# What the screenshot a run signs off with may spend. Playwright's default is
# 30 s, and a screenshot auto-waits for a pending commit, so a page that
# navigated into a server which accepted the connection and then went quiet
# holds one open for the whole of it. `act` bounds its own reads at 8 s rather
# than leaving them at the default for exactly that reason; this is the same
# number, for the same wait.
SHOT_MS = 8000

# And what the two page reads that follow it may spend, in seconds because
# `asyncio.wait_for` counts in those where playwright counts in milliseconds.
# The same number, because they are waiting on the same page.
READ_S = SHOT_MS / 1000

# How many steps in a row may come back as something other than "ok" before the
# screenshot and the element list are treated as not enough. Two, because one
# failure is an index that moved under a re-render and the next screenshot
# usually fixes it; a second in a row means the model is looking at something
# it cannot see. Then it gets the accessibility tree as well.
STUCK_AFTER = 2

# What that tree is allowed to cost. It has to be cut, and by a lot: the
# Raspberry Pi article on Wikipedia is 326,496 characters of tree over 6,904
# lines, measured. 12,000 of it is 324 lines, which on that page is the whole
# of the chrome — the nav ends at character 2,243 — plus the top of the article
# body, down into the History section. That is the part a stuck run needs.
SNAPSHOT_CHARS = 12000

# How deep to walk it, as a ceiling for a deeply nested SPA rather than
# something that does work on an ordinary page: the same article measures
# 326,496 characters at depth 12 and 326,496 at no limit at all, so this is
# inert there, and even depth 4 only takes it to 261,005. The cut above is what
# actually bounds the cost. 12 is the depth that keeps the structure worth
# having — which region a control is in, what a widget is called.
SNAPSHOT_DEPTH = 12

_SYSTEM_BODY = """\
You drive a web browser for the voice assistant of a small AI terminal. The
person asked something no API answers, so you go and read the page.

Each turn you get a screenshot of the current page and a numbered list of the
elements you can act on. Reply with one JSON object:

  {"act": "goto",   "url": "https://..."}
  {"act": "click",  "idx": <number from the list>}
  {"act": "type",   "idx": <number>, "text": "..."}
  {"act": "press",  "key": "Enter"}
  {"act": "answer", "text": "<the answer>"}

Rules:
- Only use an index that appears in the list. If an action failed last turn,
  do not repeat it — pick a different element or a different route.
- Answer as soon as the screenshot shows what was asked. Do not keep browsing
  to confirm something you can already read.
- If the page is a bot check, a cookie wall or a login you cannot get past,
  answer saying so rather than fighting it.
- This is read-only work. Never buy, book, send, post or delete anything, and
  never enter a password. If the task needs any of that, say so and stop.

The answer is read out loud as well as shown. Say plainly when you could not
find it."""

# Folded into the constant rather than joined at the call site: `_decide_sync`
# puts `_why(context)` and `answer_in(lang)` after the whole of `SYSTEM`
# (see the call below), and `browse_check.py` asserts that the instruction it
# sends starts with `SYSTEM` and, with no context, equals
# `SYSTEM + " " + answer_in(lang)`. Both stay true this way.
SYSTEM = _SYSTEM_BODY + "\n\n" + SCREEN_STYLE

# What the live agent knows about the person and the page does not: what they
# are doing, and what earlier turns already found out for it. Appended to
# :data:`SYSTEM` rather than sent as a turn, because it is standing background
# for the whole run and not something that happened on a page.
#
# Both guards are `gws.py`'s, learnt on a mailbox, and a browser has the same
# two failure modes on a page: reading the context's dates off a timetable that
# does not show them, and skipping the cancellation notice because a
# cancellation is not what the person said they were planning.
WHY = """

Why they are asking:
{context}

That is background — where to look, and which of several answers on a page is
the one they want. Two ways to get it wrong:
- It is never a source of facts. If it says the hotel is booked for Oct 3-6 and
  the page does not show those dates, the page does not show them.
- Something that contradicts it is the most valuable thing on the page, not an
  outlier to leave out. A cancellation does not match "planning a trip" and is
  exactly what they need to hear."""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "act": {"type": "STRING",
                "enum": ["goto", "click", "type", "press", "answer"]},
        "url": {"type": "STRING"},
        "idx": {"type": "INTEGER"},
        "key": {"type": "STRING"},
        "text": {"type": "STRING"},
    },
    "required": ["act"],
}

# Tag every interactive element that is visible and on-screen, and report what
# it is called. Anything inside a form that holds a password field is left out
# entirely: not offering a login is a cheaper guarantee than refusing one.
TAG_JS = """(limit) => {
  // Last turn's numbering first, or this turn's lands on top of it. Which
  // elements are visible changes as the page scrolls and re-renders, so the
  // second pass numbers a different subset — and whatever dropped out of it
  // keeps the attribute it was given. Two elements then answer to the same
  // index, `[data-agent-idx="1"]` matches both, and Playwright's strict mode
  // refuses to act on either. That is an `Error` and not a `TimeoutError`, so
  // it goes straight past the catch in `act` and ends the whole run.
  for (const el of document.querySelectorAll('[data-agent-idx]')) {
    el.removeAttribute('data-agent-idx');
  }
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link]';
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (el.type === 'password') continue;
    if (el.closest('form')?.querySelector('input[type=password]')) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom < 0 || r.top > innerHeight) continue;
    if (r.right < 0 || r.left > innerWidth) continue;
    if (getComputedStyle(el).visibility === 'hidden') continue;
    el.setAttribute('data-agent-idx', String(i));
    const name = (el.getAttribute('aria-label') || el.placeholder ||
                  el.value || el.innerText || el.title || '').trim().slice(0, 60);
    out.push(`${i}. <${el.tagName.toLowerCase()}> ${name.replace(/\\s+/g, ' ')}`);
    if (++i >= limit) break;
  }
  return out.join('\\n');
}"""

# Anything whose label says it spends money, sends something or destroys
# something. The instruction says not to; this is what happens when it does
# anyway, because a model that has been arguing with a cookie banner for four
# steps is not at its most careful.
RISKY = re.compile(
    r"buy|purchase|order|checkout|pay\b|subscribe|book now|reserve|"
    r"delete|remove|send|post|submit order|confirm|sign in|log in|"
    r"購入|注文|決済|支払|予約する|削除|送信|ログイン",
    re.IGNORECASE,
)

# And the labels that only mean anything on a profile that is signed in.
#
# Kept apart from :data:`RISKY` rather than folded into it, because the two
# have different costs. `RISKY` runs on the throwaway as well, where the
# account behind every label is nobody's: "share" on a news page is a click
# that costs nothing to allow and a route lost to refuse. These run only when
# :attr:`_Session._attached` is set, where the mailbox being archived and the
# document being shared are the household's own.
#
# Read as a second pass over the same label the click already reads, so it
# costs no extra round trip — see :meth:`_Session.act`.
SIGNED_IN_RISKY = re.compile(
    r"archive|trash|junk|spam|bin\b|unsubscribe|block\b|"
    r"share|invite|transfer|withdraw|forward|reply all|"
    r"アーカイブ|ゴミ箱|迷惑メール|共有|転送|振込",
    re.IGNORECASE,
)

# What ``press`` may be on the attached path.
#
# An allowlist of names, and not a list of keys to refuse, because the set to
# refuse has no edge. `Enter` is only the obvious one: `Control+Enter` sends a
# Gmail draft and playwright takes that spelling
# (`playwright/async_api/_generated.py:1604`, 1.62.0), and a single character
# is not a key at all — the same docstring at `:1600` says it generates text,
# so `#` arrives as the keystroke Gmail reads as *delete thread* and no label
# check ever sees it.
#
# What is left is the keys that move the viewport or dismiss something, which
# is the whole of what a run that only reads needs a keyboard for: the tree
# hint in :func:`_decide_sync` tells a stuck model to scroll, and ``press`` is
# the only thing here that can. Membership is by exact name, so every modifier
# spelling of these is refused too — `Control+End` is not `End`.
READ_KEYS = frozenset({
    "PageDown", "PageUp", "Home", "End", "Tab", "Escape",
    "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight",
})

# A page that is a bot check rather than a page. Worth spotting: the model will
# happily spend its whole step budget reasoning about a Cloudflare interstitial.
WALLS = (
    "just a moment", "checking your browser", "enable javascript and cookies",
    "verify you are human", "access denied", "unusual traffic",
)


def _model() -> str:
    return os.getenv("BROWSE_MODEL", "gemini-3.8-flash")


# The major version out of a model name, for the one config field whose
# spelling depends on it. `gemini-3.8-flash` is 3; `gemini-2.5-flash-lite` is 2.
_VERSION = re.compile(r"gemini-(\d+)")


def _thinking() -> types.ThinkingConfig:
    """Turn thinking off, in whichever spelling this model accepts.

    The per-step decision is "which of these sixty things do I click", and the
    step budget is what bounds a run — a model that deliberates over each one
    spends the budget on the same page. So: off, and it has been off since the
    spike.

    The spelling moved with 3.x and both halves matter, both measured against
    the live endpoint rather than read off the SDK:

    - ``thinking_level=MINIMAL`` is **rejected** by 3.8 Flash — *"Thinking
      level is unsupported: THINKING_LEVEL_MINIMAL"*, a 400. `LOW` is the
      lowest one it takes.
    - ``thinking_budget=0`` is *accepted* by 3.8 Flash but not honoured: three
      identical calls came back with 0, 189 and 0 thought tokens. It is not an
      error, which is worse than one — it silently does nothing every third
      call.

    On 2.5 the two swap: ``thinking_budget=0`` gives 0 thought tokens every
    time and ``thinking_level`` is a 400. Hence the version test rather than a
    single answer; ``BROWSE_MODEL`` is a knob and pointing it at 2.5 should not
    be a way to break the tool.
    """
    version = _VERSION.search(_model())
    if version and int(version.group(1)) >= 3:
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
    return types.ThinkingConfig(thinking_budget=0)


def _max_steps() -> int:
    return int(os.getenv("BROWSE_MAX_STEPS", "10"))


def _budget_s() -> float:
    return float(os.getenv("BROWSE_BUDGET_S", "120"))


def _cdp() -> str:
    """A devtools endpoint to attach to, or empty to launch a browser.

    Empty everywhere by default, which is a headless throwaway. Set on the
    panel, where the browser worth driving is the one already there: it is
    signed into what a person signed it into, and a Chromium profile directory
    takes one process at a time, so a second browser on that profile is not
    on the menu. The kiosk unit opens ``127.0.0.1:9222`` at every boot for
    this (#144), and `deploy/ai-terminal-kiosk.service` is a ``copy=False``
    row in `tools/pi_deploy.py` — the installer reads that unit and never
    writes it, so on a Pi the unit has not been put on by hand, this address
    refuses the connection.

    Read on every run rather than at startup, like the rest of ``BROWSE_*``,
    so a Pi that has just had its kiosk restarted does not also need the
    server restarted.
    """
    return os.getenv("BROWSE_CDP", "").strip()


def _allowlist() -> list[str]:
    """Domain suffixes the agent may visit, or an empty list for anywhere.

    Empty is the default because a terminal that can only read six websites is
    not worth the code. What made that defensible was everything else: a
    throwaway profile with no cookies to steal, no logins, and nothing that
    spends money.

    That argument is about the browser and not about this function, and
    :func:`_cdp` is how it stops being true — an attached run holds whatever
    sessions the panel's profile holds, so the pages it reads are reading them
    too.

    So the attached path does not get the default. :meth:`_Session.__aenter__`
    refuses to connect at all when ``BROWSE_CDP`` is set and this list is
    empty, which makes "anywhere" a thing somebody has to type rather than a
    thing they inherit (#208). Unattached, empty still means anywhere, or a
    guard on the list alone would stop every machine that is not the panel
    from browsing.

    One thing an allowlist does not cover, and it is known: a tab the run
    adopts is never asked about, so a page on an allowed host can open one on
    a host that is not. The adoption happens at ``about:blank``, so the check
    has to hang off ``framenavigated`` rather than off the adoption.

    And it raises rather than accepting an entry no host could ever equal
    (#229). :func:`_allowed` matches ``parsed.hostname`` against these by
    equality or dotted suffix and does nothing else, so ``*``, ``*.gmail.com``
    and ``https://mail.google.com`` are not three patterns — they are three
    hostnames that no page has, and taking them silently is a browse that
    attaches, refuses every ``goto`` and ends on ``about:blank`` with the
    deploy row green throughout. The strip below is what makes that plausible:
    it normalises ``.gmail.com`` for you, so it reads as though it normalises
    the rest too. ``*`` is not given a meaning here on purpose — an empty list
    is how "anywhere" is spelled, and the attached path refuses that on
    purpose, so a second spelling of it would be a way round #208's guard that
    the guard cannot see.

    An entry the strip empties raises with them, and it is the one the strip
    itself makes: ``BROWSE_ALLOWLIST=.`` keeps the field (it is not blank) and
    then normalises it to ``""``, which no host equals — and worse, ``_allowed``
    tests ``host.endswith("." + d)``, so an empty entry allows every host
    written as an FQDN with a trailing dot. The list is non-empty either way,
    so #208's attached guard waves it through.

    Separators and nothing else raises for the same reason and is the same
    mistake one keystroke further on: ``BROWSE_ALLOWLIST=" , , "`` parses to
    the empty list, which reads as the default rather than as the typo it is,
    and `tools/pi_deploy.py`'s probe calls the row set because the value is
    not empty on the Pi. Unset and empty stay silent — that is the default and
    a great many machines run on it.
    """
    raw = os.getenv("BROWSE_ALLOWLIST", "")
    fields = [d.strip() for d in raw.split(",") if d.strip()]
    entries = [d.lower().lstrip(".") for d in fields]
    bad = [f for f, d in zip(fields, entries)
           if not d or set(d) & set("*/:")]
    if bad:
        raise ValueError(
            f"BROWSE_ALLOWLIST entry {bad[0]!r} is not a hostname: entries "
            "are matched against a page's host by equality or dotted suffix, "
            "so a wildcard, a scheme and a path each match nothing at all. "
            "Write the suffix on its own, and leave the variable empty to "
            "mean anywhere.")
    if raw.strip() and not entries:
        raise ValueError(
            f"BROWSE_ALLOWLIST is {raw!r}, which is separators and nothing "
            "else: write a domain suffix, or leave the variable empty to "
            "mean anywhere.")
    return entries


def _allowed(url: str) -> bool:
    """Is this somewhere the agent may go?

    Checked before every ``goto``, again after every navigation, a third time
    in :func:`run` on the address each step's screenshot came from, and a
    fourth on the address the run ends on, because a redirect is how you leave
    the allowlist without asking. None of the last three is redundant: the
    second runs the instant the action returns, and a navigation slower than
    the settle's ceiling has not committed by then; the third runs at the top
    of a turn, and a run can end on a step that has no next turn.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    allowed = _allowlist()
    if not allowed:
        return True
    host = (parsed.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


def _walled(text: str) -> bool:
    """A bot check, rather than the page that was asked for."""
    head = text[:400].lower()
    return len(text) < 600 and any(wall in head for wall in WALLS)


@functools.cache
def _timeout_error() -> type[BaseException]:
    """Playwright's own timeout class, imported as late as the driver is.

    The import in :meth:`_Session.__aenter__` is lazy on purpose — playwright
    starts a driver process and this module is imported on every server start,
    browse or no browse. Same reason here, and cached so a per-step lookup does
    not re-enter the import machinery.

    Named rather than caught as ``Exception``: a closed page and a crashed
    context raise too, and treating those as a slow load would have the model
    carry on acting on a browser that is no longer there.
    """
    from playwright.async_api import TimeoutError as PWTimeout

    return PWTimeout


class _WentNowhere(Exception):
    """Leaves a navigation waiter early, when the click issued no navigation.

    `expect_navigation` waits out its whole timeout when nothing navigates —
    `act`'s `press` says so about itself — and most clicks navigate nowhere: a
    disclosure triangle, a checkbox, a link that opens its own tab. Paying
    :data:`CLICK_NAV_MS` for every one of those would cost more than the slow
    page it exists for. Raising inside the `async with` cancels the waiter
    instead of awaiting it (`playwright/_impl/_async_base.py:57-58`), which is
    the only way out of that block that does not wait.

    Never seen by a caller: `act` catches it beside the timeout.
    """


@functools.cache
def _page_error() -> type[BaseException]:
    """Playwright's base error, for the mistakes that are not slowness.

    A key name the model invented, a URL the network stack refuses, two
    elements answering one selector: the model's error, not a broken browser,
    and the same kind of thing a timeout is — a note it can act on. Caught
    after :func:`_timeout_error`, since ``TimeoutError`` is a subclass of this
    one and the timeout note is the more specific of the two, and only while
    the page is still alive, so a closed page and a crashed context still end
    the run.

    Lazily imported and cached for the reason :func:`_timeout_error` is.
    """
    from playwright.async_api import Error as PWError

    return PWError


class _Session:
    """One browser, one task. Closed and deleted when the task ends.

    Or attached to and let go of, which is the other half: see :func:`_cdp`.
    Whose browser it is decides the teardown as much as the setup, so the two
    paths are two methods and :meth:`_teardown` asks which one ran.
    """

    def __init__(self):
        self._pw = None
        self._browser = None
        self.page = None
        # Whether the browser is ours to close, and the pages that were on it
        # before we arrived. Empty on the launch path, where there are none.
        self._attached = False
        self._theirs = ()
        # The other half of that: tabs that were not there before we arrived
        # and are ours because *we* opened them. See :meth:`_popped` for why
        # this is recorded as it happens instead of worked out at the end.
        self._ours = []
        self._acting = False

    async def __aenter__(self):
        from playwright.async_api import async_playwright

        try:
            self._pw = await async_playwright().start()
            # Read once, before either path, so an entry no host could equal
            # is one error at the top of the run instead of a `goto` refused
            # at every step with nothing in the log naming the cause (#229).
            # It has to be here and not only in the branch below, because the
            # unattached path never asks — it would have launched a browser,
            # spent the budget and answered "I could not find that".
            allowed = _allowlist()
            endpoint = _cdp()
            if endpoint:
                if not allowed:
                    # Fail closed, and only on this path. Empty means anywhere
                    # (:func:`_allowed`), which was defensible while the
                    # profile was a throwaway with nothing in it. Attached, the
                    # run holds whatever sessions the panel's profile holds and
                    # its tab is the whole of the screen, so "anywhere" would
                    # be a policy nobody typed. What the kiosk profile is
                    # actually signed into is #222's question, and this refuses
                    # either way round: the answer can change without anybody
                    # revisiting a default.
                    raise RuntimeError(
                        f"BROWSE_CDP is set ({endpoint}) but "
                        "BROWSE_ALLOWLIST is empty: an attached run drives "
                        "the panel's own profile and anywhere is not a "
                        "default it may take")
                self._attached = True
                await self._attach(endpoint)
            else:
                await self._launch()
        except BaseException:
            # `__aexit__` runs for a `with` body that was entered, and this is
            # the one path where none was. By the time `launch` can fail,
            # `start()` has already spawned the node driver — and `launch` is
            # exactly what fails on a Pi with no `BROWSE_CHROMIUM`, since
            # Playwright ships no arm64 Linux build. `connect_over_cdp` fails
            # the same way and rather more often: `ECONNREFUSED` in 0.02 s,
            # measured, on every Pi whose kiosk unit predates #144 — which the
            # installer will not copy, so that is most of them. Without this,
            # every failed
            # browse leaves a node process behind for the life of the server.
            await self._teardown()
            raise
        return self

    async def _launch(self) -> None:
        """A browser of our own, thrown away when the run ends.

        `launch` gives every run its own profile directory and deletes it on
        close, so nothing persists: no cookie jar to carry into the next task,
        and no way to reach the kiosk's own session.
        """
        launch = {
            "headless": True,
            "args": [
                # See the module docstring: without this Chromium's
                # network stack blocks on the desktop keyring.
                "--password-store=basic",
                # Measured on the Pi (Chromium 152, aarch64): without
                # this, the first `Page.captureScreenshot` after a launch
                # fails 9 times in 10 with "Unable to capture screenshot"
                # — the GPU process is not ready and Chromium errors
                # instead of waiting. Every step opens with the screenshot
                # in `look`, so this was browse never working on the Pi at
                # all. Harmless on the Mac, where headless captures
                # already go through the software path.
                "--disable-gpu",
            ],
        }
        chromium = os.getenv("BROWSE_CHROMIUM")
        if chromium:
            launch["executable_path"] = chromium
        self._browser = await self._pw.chromium.launch(**launch)
        self.page = await self._browser.new_page(viewport=VIEWPORT)

    async def _attach(self, endpoint: str) -> None:
        """The browser already on the panel, driven where it stands.

        ``contexts[0]`` and never ``new_context()``, which is the whole reason
        this is a method and not two swapped words. Both work over CDP —
        measured against playwright 1.62.0 and Chromium 151, `new_context()`
        returns in 0.02 s and raises nothing — but a context made over CDP
        gets a cookie jar of its own, and it is empty. The run is then logged
        out of everything while looking exactly like a run that worked, which
        is the one outcome this path exists to avoid.

        `Browser.new_page()` is that same trap with no `new_context()` in
        sight: over CDP it makes an implicit context, measured as a second
        entry in ``browser.contexts`` whose `cookies()` is `[]` beside the
        profile's. That is the line `_launch` uses, so swapping `launch` for
        `connect_over_cdp` and changing nothing else attaches, runs, answers
        and is signed into nothing.

        The tab is ours, and so is any tab our own click opens — which is why
        the popup handler goes on here and nowhere else; :meth:`_popped` is
        the whole of that. The page under it is not. It belongs to whoever is
        standing at the panel, so it is never navigated, never closed, and
        brought back to the front on the way out — on a `--kiosk` window the
        panel *is* that page. Whether our tab covers it while the run goes on
        is the window manager's answer and not one this can give: #162 records
        it as needing the device.

        There is no viewport argument on this path. `BrowserContext.new_page`
        takes none, and an attached page reports `viewport_size` as `None`, so
        the size the model has been calibrated against — 1,848 tokens a
        screenshot, flat — is set on the page instead. That is
        `Emulation.setDeviceMetricsOverride`, which changes what the tab
        renders, which is why it lands on ours and not on theirs.
        """
        self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        contexts = self._browser.contexts
        if not contexts:
            # A browser answering CDP with no context at all is not something
            # a kiosk does, and attaching to `contexts[0]` blind would be an
            # IndexError three frames from anything that names the endpoint.
            raise RuntimeError(f"{endpoint} has no browser context to drive")
        context = contexts[0]
        self._theirs = tuple(context.pages)
        self.page = await context.new_page()
        self.page.on("popup", self._popped)
        await self.page.set_viewport_size(VIEWPORT)

    def _popped(self, page) -> None:
        """A tab our page opened, kept if we were the one who opened it.

        `page.on("popup")` fires for every tab our page opens, whoever caused
        it: playwright emits it from `BrowserContext._on_page` whenever the
        new page's opener is this one (1.62.0,
        `_impl/_browser_context.py:240-244`). So the event says *from* and
        never *by*, and :attr:`_acting` is the only thing here that says *by*.

        It has to be recorded now, because nothing readable afterwards can
        reconstruct it. `page.opener()` names the frame the tab came from, and
        measured against Chromium 151 an agent click and a person's tap on the
        same `target="_blank"` link give the identical answer — our page. A
        rule written off `opener()` at teardown closes the tab a person at the
        panel opened and was reading, which is the one thing :attr:`_theirs`
        exists to forbid. The distinction exists only while we are the one
        dispatching input, and that is this flag.

        The handler goes on the new tab too, because the event only ever
        fires on the page that opened it. A tab our click opened can open one
        of its own — `window.open` and then `document.write` of a script into
        the result is a live shape on real pages, and Chromium's user
        activation carries far enough for it to succeed. Without this line
        that grandchild has no handler anywhere, is never recorded, and is
        left on the panel's signed-in profile: measured against Chromium 151,
        a chained `window.open` leaves one `about:blank` behind per run
        without it and none with it. It is also why :meth:`_release` closes
        newest first.

        Not registered on the launch path: there the whole browser goes, and
        every tab on it with it.
        """
        if self._acting:
            self._ours.append(page)
            page.on("popup", self._popped)

    async def __aexit__(self, *_):
        await self._teardown()

    async def _release(self) -> None:
        """Give the panel back, then let go of the browser.

        Three things and the order is the point.

        Our tabs first, because `Browser.close()` does not take them: measured
        over CDP against playwright 1.62.0, the browser process stays up and
        `/json/list` still lists the page we opened, which on a kiosk window
        is the agent's last page left in front of whoever is standing there.
        Tabs, plural: a click on `<a target="_blank">` — the commonest link on
        a timetable — opens one more, and measured against Chromium 151 three
        runs each clicking one such link left the profile holding 2, 3 and
        then 4 pages. The leak is per tab-opening click, it is a live page on
        the panel's signed-in profile running an arbitrary site's JavaScript
        after the connection is gone, and only the kiosk Chromium exiting
        clears it — the next attach folds it into its own :attr:`_theirs` and
        so protects it for good. Ours are closed newest first, since a tab can
        open a tab.

        Then the page that was there before us, brought to the front. First
        and not all of them, because on the panel there is one; a second page
        on that profile is somebody debugging, and putting *that* in front of
        the household is worse than leaving it where it was.

        Then the connection, which is what `close()` means on this path.
        Playwright has no `disconnect()` in Python and `close()` is the
        documented way off a CDP-connected browser; measured, it leaves the
        process and its own pages running. It must, because that browser is
        the terminal.
        """
        for page in reversed(self._ours):
            with contextlib.suppress(Exception):
                await page.close()
        if self.page:
            with contextlib.suppress(Exception):
                await self.page.close()
        for page in self._theirs[:1]:
            with contextlib.suppress(Exception):
                await page.bring_to_front()
        if self._browser:
            await self._browser.close()

    async def _teardown(self) -> None:
        """Close whatever got as far as being opened.

        Or hand it back, on the attached path — :meth:`_release` is the whole
        of that difference, and it goes here rather than beside the close so
        the two guarantees below cover it as well. They are what an attached
        run needs most: the browser it did not close is the panel's, and the
        tab it did not manage to shut is in front of somebody.

        Two blocks and not one, because they used to be one and a `close()`
        that raised took `stop()` with it. `close()` raising is not exotic:
        Chromium crashing under memory pressure is how the Pi fails, and that
        is precisely when the node driver is worth reaping. So the close is
        best-effort and the stop is not.

        Nested, and not merely one after the other, because there are two ways
        the close can skip the stop and `except Exception` only covers one.
        `CancelledError` is a `BaseException`, so a cancel landing inside
        `close()` — the socket dropped mid-browse and `close_session` cancelled
        the job, with a real Chromium taking hundreds of ms to shut down —
        walks straight past that clause and out of the function, and the driver
        started in `__aenter__` is orphaned for the life of the server. One per
        cancel that lands in the window, on a terminal that never reboots.

        The cancel is re-raised rather than swallowed: an outer `finally` runs
        the stop and lets it carry on. Not shielded — that would make a
        cancelled browse wait for Chromium, and whoever cancelled it has
        usually gone. A cancel landing inside `stop()` itself is still a leak;
        `stop()` is fast next to `close()`, so this narrows the window rather
        than closing it.
        """
        try:
            try:
                if self._attached:
                    await self._release()
                elif self._browser:
                    await self._browser.close()
            except Exception:
                logger.debug("browser close failed", exc_info=True)
            finally:
                self._browser = None
                self._theirs = ()
                self._ours = []
        finally:
            try:
                if self._pw:
                    await self._pw.stop()
            finally:
                self._pw = None
                self.page = None

    async def look(self) -> tuple[bytes | None, str, str]:
        """The screenshot, the element list and the page text.

        The shot is ``None`` when the page would not give one up inside
        :data:`SHOT_MS`. That is not the same as having nothing to look at:
        `evaluate` runs on the main thread and a page whose compositor is
        wedged still answers it, so the element list and the text below are
        usually intact and the step can go ahead on those alone. Raising
        instead throws away every step the run has taken — the caller has no
        `except` around the loop and `browse` turns it into an error string.

        The two reads are bounded as well, and by our own clock, because
        `evaluate` takes no timeout and playwright applies none. Measured
        against a real Chromium on a navigation the far end accepted and never
        finished: the screenshot gives up at :data:`SHOT_MS` and `evaluate` was
        still waiting at 90 s. `_settle` swallows its own `networkidle`
        timeout, so the loop arrives here in exactly that state, and the run
        budget is only consulted between steps — nothing above would cut it
        short. A page that answers nothing at all is the one case with nothing
        to go on, so it raises rather than pretending to a reading.
        """
        try:
            shot = await self.page.screenshot(
                type="jpeg", quality=70, timeout=SHOT_MS)
        except Exception:  # an unphotographable page is still a readable one
            shot = None
            logger.info("browse: the page would not give up a screenshot in "
                        "%s ms; reading it without one", SHOT_MS,
                        exc_info=True)
        try:
            elements = await asyncio.wait_for(
                self.page.evaluate(TAG_JS, MAX_ELEMENTS), READ_S)
            text = await asyncio.wait_for(
                self.page.evaluate("document.body?.innerText || ''"), READ_S)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"the page did not answer in {READ_S}s") from None
        return shot, elements, text

    async def tree(self) -> str:
        """The accessibility tree, for when the screenshot is not enough.

        Not part of every step: it is thousands of characters of YAML next to
        an element list that is usually the same information in sixty lines.
        It earns its place only when the model is stuck, because it carries
        the two things the other two views drop — a control that is scrolled
        out of frame, and the role and name of one whose label renders as an
        icon.

        Playwright's own ``mode="ai"`` is the wrong format for this despite
        the name. It renumbers every node with a `ref` the model would then
        answer with instead of the `data-agent-idx` `act` can act on, and it
        is nearly twice the size — 624,419 characters against 326,496 on the
        same page.
        """
        try:
            snap = await self.page.aria_snapshot(depth=SNAPSHOT_DEPTH,
                                                 timeout=5000)
        except Exception:
            # A tree that will not render is not worth ending a run over; the
            # step goes ahead on the screenshot, as every step before it did.
            logger.debug("aria snapshot failed", exc_info=True)
            return ""
        return snap[:SNAPSHOT_CHARS]

    async def act(self, step: dict) -> tuple[str, int]:
        """Carry out one step.

        Returns a short note on what happened and the milliseconds spent
        settling afterwards — the note goes to the model, the number to the
        bench, because the settle wait is the one cost per step that is a
        choice rather than a fact about the page.
        """
        kind = step.get("act")
        # What a failure note calls the step. Hoisted out of the two `except`
        # clauses below rather than written twice: they name the step the same
        # way and only differ in what went wrong with it.
        named = {"goto": step.get("url"),
                 "press": step.get("key")}.get(kind, step.get("idx"))
        # What the action already spent waiting for its own navigation, which
        # the settle then charges against its ceiling rather than adding to it.
        # Only `press` ever sets it; every other action either waits for its own
        # navigation inside Playwright or does not navigate at all.
        spent = 0

        try:
            if kind == "goto":
                url = step.get("url", "")
                if not _allowed(url):
                    return f"refused: {url} is not on the allowlist", 0
                await self.page.goto(url, wait_until="domcontentloaded",
                                     timeout=30000)

            elif kind == "press":
                key = step.get("key", "Enter")
                # The allowlist says where a run may go; this says what it may
                # do once it is there, and only on the path where that is a
                # different question. `press` is the submit no other guard can
                # see coming: `RISKY` reads a label and a keystroke has none,
                # and the element list `TAG_JS` builds is not consulted at all.
                # On a signed-in profile that is Enter in a compose window and
                # `Control+Enter` in Gmail, either of which sends the
                # household's mail on the model's say-so.
                #
                # Unattached this does not run. The throwaway is signed into
                # nothing, so a key that submits commits nothing, and taking
                # Enter away from every machine that is not the panel would
                # cost every search box for no account to protect.
                if self._attached and key not in READ_KEYS:
                    return (f"refused: {key!r} is not a key this profile may "
                            "press — it is signed in, so a key that submits "
                            "is a key that commits. Scroll with PageDown, or "
                            "put the query in the URL and goto it"), 0
                # The key is not an auto-waiting API, so a press that submits a
                # form returns the moment the key is delivered and nothing below
                # waits for the new document. `_settle` would then answer about
                # the old one: `networkidle` stays in the frame's `_load_states`
                # until a new document *commits*, so `wait_for_load_state`
                # short-circuits and the next step screenshots the form that was
                # just submitted.
                #
                # Budgeted at `SETTLE_MS` and not `MIN_SETTLE_MS`, because a
                # 200 ms budget is the bug rather than the fix for it: the case
                # this exists to catch is a server that answers in 400 ms, and a
                # wait that gives up first leaves the window exactly as wide as
                # it found it. What the wait costs is subtracted from the settle
                # below rather than added to it, so a step still cannot cost
                # more than the ceiling.
                #
                # The timeout is not a failure. A press that navigates nowhere —
                # Enter in a search box that filters in place, Escape closing a
                # dialog — is a perfectly ordinary step, and it is only when
                # nothing navigates that this waits out the whole budget.
                pressed = time.monotonic()
                try:
                    async with self.page.expect_navigation(
                            wait_until="commit", timeout=SETTLE_MS):
                        self._acting = True
                        await self.page.keyboard.press(key)
                except _timeout_error():
                    pass
                except _page_error() as err:
                    if self.page.is_closed() or "ERR_ABORTED" not in str(err):
                        raise
                    await self.page.evaluate("1")
                spent = round((time.monotonic() - pressed) * 1000)

            elif kind in ("click", "type"):
                raw_idx = step.get("idx")
                if isinstance(raw_idx, (bool, float)) or raw_idx is None:
                    return f"failed: {kind} {named!r} is not a valid index", 0
                try:
                    idx = int(raw_idx)
                    if idx < 0:
                        raise ValueError
                except (ValueError, TypeError):
                    return f"failed: {kind} {named!r} is not a valid index", 0
                named = idx
                target = self.page.locator(f'[data-agent-idx="{idx}"]')
                if kind == "click":
                    # `inner_text` is empty for `<input type=submit
                    # value="購入">`, which is exactly the case worth catching,
                    # so read the same name the model was shown.
                    #
                    # Bounded by the same 8 s the click below asks for.
                    # `evaluate` on a locator auto-waits like every other
                    # locator method, so without this it takes playwright's 30 s
                    # default: an index the DOM will not resolve spends 30 s
                    # here and never reaches the click that was supposed to
                    # bound it. Four of those is the whole 120 s budget.
                    label = await target.evaluate(
                        "el => el.getAttribute('aria-label') || el.value || "
                        "el.innerText || el.title || ''",
                        timeout=8000,
                    )
                    if RISKY.search(label or ""):
                        return (f"refused: {label.strip()[:40]!r} looks like "
                                "it commits to something"), 0
                    # The second pass, on the label already in hand. Archive,
                    # trash, spam, share: on a shop these are nothing and
                    # `RISKY` is right to let them through, and on the
                    # household's own mailbox each of them is data the run
                    # would be moving or giving away. A separate note from the
                    # one above, because the reason is a different one and the
                    # model is being told which route to stop taking.
                    if self._attached and SIGNED_IN_RISKY.search(label or ""):
                        return (f"refused: {label.strip()[:40]!r} acts on the "
                                "account this browser is signed into, and "
                                "this run only reads it"), 0
                    # Set here and not at the top of the step: reading the
                    # label above is a locator call that can wait 8 s on an
                    # index the DOM will not resolve, and a tab that opens
                    # while nothing has been dispatched is somebody else's.
                    #
                    # And the actionability retry waits ahead of it for the
                    # same reason, which is the whole of this line's job.
                    # `click` retries until the element is clickable and that
                    # retry used to be inside the flag, so a cookie banner
                    # landing over the target held the flag up for eight
                    # seconds — and every tab that opened in those eight
                    # seconds, including one the person at the panel opened
                    # by tapping a link in our own tab, was recorded as ours
                    # and closed with it. `wait_for` is not a dispatch: it
                    # settles nothing and clicks nothing.
                    #
                    # It narrows the window rather than closing it. The four
                    # states `wait_for` offers are attached, detached, visible
                    # and hidden, and an occluded element is visible, so the
                    # banner case is still resolved inside the click below —
                    # now bounded by :data:`CLICK_MS` instead of by 8 s.
                    await target.wait_for(state="visible", timeout=8000)
                    clicked = time.monotonic()
                    dispatched = navigated = False

                    def issued(request) -> None:
                        """Whether this click asked the network for a page.

                        The one thing that separates a click which went
                        nowhere from a click whose server has not answered
                        yet. `expect_navigation` cannot: both of them are a
                        commit that never arrives, and it would wait out the
                        full budget for either. Main frame only — a click that
                        loads an iframe has not navigated the page.
                        """
                        nonlocal navigated
                        if (request.is_navigation_request()
                                and request.frame == self.page.main_frame):
                            navigated = True

                    self.page.on("request", issued)
                    try:
                        async with self.page.expect_navigation(
                                wait_until="commit", timeout=CLICK_NAV_MS):
                            self._acting = True
                            await target.click(timeout=CLICK_MS,
                                               no_wait_after=True)
                            dispatched = True
                            # The popup grace, and the whole of what the flag
                            # is up for now. Measured against Chromium 151,
                            # `click()` returns at 31-71 ms and the tab arrives
                            # at 66-111 ms, so the flag has to outlive the
                            # dispatch — but only by that much, and not by the
                            # navigation wait below it.
                            await self.page.wait_for_timeout(MIN_SETTLE_MS)
                            self._acting = False
                            if not navigated:
                                raise _WentNowhere
                    except _WentNowhere:
                        pass
                    except _timeout_error():
                        # Three ways in, and they are not the same event. A
                        # click that never dispatched really failed. A page
                        # that was asked for and never committed is the
                        # hanging server, and the step has to say so or the
                        # next screenshot answers about the old page. What is
                        # left — dispatched, asked for nothing — is an
                        # ordinary step and cannot reach here.
                        if not dispatched or navigated:
                            raise
                    except _page_error() as err:
                        # A fourth way out, and the one the split above created:
                        # a link that opens no document at all. `mailto:`,
                        # `tel:`, a scheme no handler claims, anything
                        # answering 204, and anything answering
                        # `Content-Disposition: attachment` — a download the
                        # browser takes over, whether it finishes or dies
                        # mid-transfer — are all navigation requests that
                        # Chromium then aborts, so `issued` sets
                        # `navigated` and the waiter dies of `ERR_ABORTED`
                        # rather than of the timeout. Measured, all five arrive
                        # as one line of `net::ERR_ABORTED; maybe frame was
                        # detached?` with nothing to tell them apart: `mailto:`
                        # and the unclaimed scheme raise no `response` and no
                        # `download` event either, so *the OS took it* and
                        # *nobody did* are the same reading and both are
                        # reported as a step that worked.
                        #
                        # A plain `download` attribute is not on this list and
                        # not this path: it issues no main-frame navigation
                        # request at all, so it leaves through `_WentNowhere`
                        # at `:917` instead. Two shapes called "download", two
                        # doors.
                        #
                        # That is an `Error` and
                        # not a `TimeoutError` — `TimeoutError` is its subclass
                        # and not its parent
                        # (`playwright/_impl/_errors.py:48`), so the clause
                        # above cannot see it, and before this one existed the
                        # abort fell all the way to `:1010` and reported
                        # `failed:` on a click that had worked. `failed:` skips
                        # the settle, which leaves the next `look` reading the
                        # page as it was: the same defect this issue was filed
                        # about, arriving through a different door.
                        #
                        # Hung off `dispatched` because that is already the flag
                        # separating a click that never happened from a click
                        # the page declined to act on, and an abort with nothing
                        # dispatched is still somebody's real failure.
                        if not (dispatched and "ERR_ABORTED" in str(err)):
                            raise
                        # And the sixth shape, which wears the same string and
                        # is not a step that worked: the page closed while its
                        # navigation was in flight. Chromium aborts the pending
                        # request on the way out, so a closed page reaches this
                        # clause as `ERR_ABORTED` after a dispatch and the
                        # guard above waves it through — and `_settle` then
                        # short-circuits, because an aborted navigation never
                        # commits and `networkidle` is therefore still in the
                        # frame's load states from the step before. The step
                        # comes back `ok`, on a browser that is gone.
                        #
                        # `if self.page.is_closed(): raise`, the guard `:1022`
                        # uses, cannot catch it here: the abort is delivered
                        # ahead of the close event that caused it, measured
                        # `False` on 12 of 12 trials at four different close
                        # timings. So ask the page rather than the flag. An
                        # evaluate sends CDP into the context — 0.9-2.5 ms
                        # live, `TargetClosedError` dead — and that lands in
                        # the outer handler, which ends the run on a closed
                        # page exactly as it does for every other way one
                        # can die.
                        await self.page.evaluate("1")
                    finally:
                        self.page.remove_listener("request", issued)
                    spent = round((time.monotonic() - clicked) * 1000)
                else:
                    await target.fill(step.get("text", ""), timeout=8000)

            else:
                return f"unknown action {kind!r}", 0

        except _timeout_error():
            # An index the DOM will not resolve, or a page that never commits.
            # Nothing used to catch this: it propagated out of the loop and the
            # whole task ended as an error, so the card froze on the last step
            # with no answer and the model never got to try another route —
            # which the prompt has been telling it to do all along. A note is
            # the only thing it can act on.
            note, settled = f"failed: {kind} {named!r} did not land in time", 0

        except _page_error() as err:
            # The mistakes that are not slowness, which used to end the run:
            # `press` with an unknown key, `goto` to unresolved DNS, selector
            # collisions. All are model errors with another route, so a note.
            #
            # Second, because `TimeoutError` is a subclass of this: first,
            # and every timeout would come out with this wording instead.
            # And only while the page is alive — a closed page or crashed
            # context cannot carry out the next step, so still ends the run.
            if (self.page.is_closed()
                    or type(err).__name__ == "TargetClosedError"
                    or "Target page, context or browser has been closed" in str(err)
                    or "Target closed" in str(err)):
                raise
            # First line only: Playwright follows the message with the call
            # log, dozens of lines of retry timings that say nothing the model
            # can act on and cost it the context to think with. Indexed off a
            # list that is never empty, because an IndexError raised in here
            # ends the run in exactly the way this clause exists to stop.
            first = (str(err).splitlines() or [""])[0]
            note, settled = f"failed: {kind} {named!r} — {first[:120]}", 0
        else:
            note, settled = "ok", await self._settle(spent)

        finally:
            # The backstop, and for `click` usually a no-op: the click path
            # brings the flag down itself, `MIN_SETTLE_MS` after the dispatch
            # and before the navigation wait. What this catches is every way
            # out that never got there — a click that timed out on
            # actionability, a page error, a `press` or a `type`.
            #
            # Why the grace and not the next line after the dispatch:
            # Chromium's user activation outlives the call, so a handler that
            # opens its tab from a `setTimeout` still counts as gestured and
            # the tab arrives after `click()` has answered. Measured against
            # Chromium 151 over four runs of five, `click()` on a plain
            # `target="_blank"` link returns at 31-71 ms and the popup arrives
            # at 66-111 ms. The two spreads overlap across runs, but the popup
            # came second in all 20 trials. Clearing the flag immediately
            # therefore records nothing at all.
            #
            # And why not longer: what the window costs is a person tapping a
            # link *in our own tab* having it closed with the run's. That is
            # the price of having any answer at all — `opener()` gives their
            # tab away as ours for the whole of the run instead — but it is a
            # price worth keeping to the dispatch rather than letting it grow
            # with how slow the site is.
            self._acting = False

        # Re-check after the fact: a click can navigate, and a redirect is not
        # something the pre-check ever saw.
        #
        # Below the `except` and not inside the success path, because a
        # navigation can commit and only *then* run out of time: `goto` waits
        # for `domcontentloaded` and a 302 has already moved `page.url` by the
        # time the wait gives up. Returning early from the `except` left the
        # browser sitting on wherever it had got to, and the next step of the
        # loop screenshots that page, reads its text and sends both to the
        # model. It is the path most likely to be somewhere unintended, so it
        # is the last one that should skip the check.
        landed = self.page.url
        if landed != "about:blank" and not _allowed(landed):
            await self.page.goto("about:blank")
            sent_back = f"left the allowlist and was sent back ({landed})"
            # Both notes when the action also timed out. The model needs to
            # know it is back at blank, and it needs to know the address it
            # asked for is not the address that answered.
            return (sent_back if note == "ok" else f"{note}; {sent_back}",
                    settled)
        return note, settled

    async def _settle(self, spent_ms: int = 0) -> int:
        """Wait for the page to finish reacting. Returns the ms spent.

        A short floor to let a navigation commit, then the network going quiet
        — or :data:`SETTLE_MS` in total, whichever comes first. A page that
        never goes quiet costs what the old unconditional sleep cost and not a
        tenth more.

        `spent_ms` is what the action itself already spent waiting for its own
        navigation to commit, and it comes off both halves. Off the floor
        because the floor is only a guess at what that wait now knows: a press
        that watched the document commit does not need 200 ms to let it. Off
        the ceiling because :data:`SETTLE_MS` is what a *step* may spend, not
        what this function may — a press that waited 1.9 s for a slow server has
        100 ms of budget left, and adding a fresh 2 s to it would hand back the
        bound the caller is relying on.
        """
        started = time.monotonic()
        floor = max(0, MIN_SETTLE_MS - spent_ms)
        if floor:
            await self.page.wait_for_timeout(floor)
        budget = SETTLE_MS - spent_ms - floor
        if budget > 0:
            try:
                await self.page.wait_for_load_state("networkidle",
                                                    timeout=budget)
            except _timeout_error():
                pass
        return spent_ms + round((time.monotonic() - started) * 1000)


def _why(context: str) -> str:
    """:data:`WHY` filled in, or nothing at all when there is no context.

    Nothing rather than an empty heading: a "Why they are asking:" with a blank
    under it is an invitation to invent one, and the guards below it only make
    sense next to something to guard.
    """
    context = context.strip()
    return WHY.format(context=context) if context else ""


def _decide_sync(task: str, lang: str, shot: bytes | None, elements: str,
                 history: list[str], context: str = "", tree: str = "",
                 blank: bool = False) -> dict:
    seen: list = [
        f"Task: {task}\nDone so far: " + (" then ".join(history) or "nothing")
    ]
    # `blank` and not `shot is None`, because those stopped being the same
    # thing when `look` was allowed to come back without a picture. Telling a
    # model reading a page that it is on a blank one sends it navigating away
    # from the page it was reading, which is a worse answer than no picture.
    if blank:
        seen.append("The browser is on a blank page. Navigate somewhere first.")
    elif shot is None:
        seen.append(
            "The page is loaded but would not give up a screenshot, so this "
            "step has no picture. The element list below is still the page "
            "you are on — act on it, or scroll. Do not navigate away just "
            "because you cannot see it."
        )
        seen.append(f"Elements you can act on:\n{elements}")
    else:
        seen.append(types.Part.from_bytes(data=shot, mime_type="image/jpeg"))
        seen.append(f"Elements you can act on:\n{elements}")
    if tree:
        seen.append(
            "The last two steps did not work. Here is the accessibility tree "
            "of the page as well — it reaches things the screenshot does not "
            "show, including controls below the fold. It is not a list of "
            "indices: pick from the elements above, or scroll or navigate to "
            "reach what the tree says is there.\n" + tree
        )

    response = global_client().models.generate_content(
        model=_model(),
        contents=seen,
        config=types.GenerateContentConfig(
            system_instruction=" ".join(
                filter(None, [SYSTEM + _why(context), answer_in(lang)])),
            thinking_config=_thinking(),
            response_mime_type="application/json",
            response_schema=SCHEMA,
        ),
    )
    try:
        return json.loads(response.text)
    except (TypeError, ValueError):
        return {"act": "answer", "text": ""}


# The keys of a decision worth printing whole. `act` is one of five words,
# `idx` is a position in the element list and `key` is a keyboard key — a
# closed vocabulary each, and between them they are what makes the step line
# readable. Everything else in the schema at :data:`SYSTEM` is a string chosen
# to answer the household's question: `text` is what was typed into the page's
# search box or the whole of the answer, and `url` is the page somebody was
# reading. `_log_tool_traffic` sizes `task` and `context` at the entrance and
# two frames down they were raw again. See #235.
_STEP_PLAIN = frozenset({"act", "idx", "key"})


def _shape(step) -> dict | str:
    """One decision, as it goes to the log: the verb, and the size of the rest.

    A dict rather than `_sized(step)`, which gives `<dict>` and destroys the
    line's reason for existing — the same per-key split `main._log_tool_traffic`
    does, for the same reason.
    """
    if not isinstance(step, dict):
        return _sized(step)
    return {k: v if k in _STEP_PLAIN else _sized(v) for k, v in step.items()}


async def run(task: str, lang: str = "", context: str = "", sink=None) -> dict:
    """Drive a browser until the task is answered, or the budget runs out.

    Args:
        task: What to find out, in a sentence.
        lang: BCP-47 tag of the language to answer in.
        context: What the person is doing, in a short paragraph. See
            :data:`WHY`; empty is fine and means the run is on its own.
        sink: Optional async callable, given a frame per step so the screen can
            show what the browser is doing while it does it.

    Returns:
        A dict with the answer, the steps taken, and the last screenshot as a
        data URI.
    """
    started = time.monotonic()
    history: list[str] = []
    steps: list[dict] = []
    shot = None
    answer = ""
    stuck = 0

    async def emit(frame: dict) -> None:
        if sink:
            try:
                await sink(frame)
            except Exception:  # a dead websocket must not kill the job
                logger.debug("browse sink failed", exc_info=True)

    async with _Session() as session:
        for number in range(1, _max_steps() + 1):
            if time.monotonic() - started > _budget_s():
                answer = "I ran out of time looking that up."
                break

            blank = session.page.url == "about:blank"
            elements, text = "", ""
            if blank:
                shot = None
            else:
                try:
                    shot, elements, text = await session.look()
                except Exception:
                    # A page that will not photograph is read from its element
                    # list; a page that is gone has no element list either, and
                    # `look` raises rather than pretending to a reading. There
                    # is no next step to take against a dead tab, so the run
                    # ends here with the steps it has. Left to propagate this
                    # leaves `run` entirely and `browse` turns the whole job
                    # into an error string, throwing away every step so far.
                    logger.info("browse: the page could not be read at all; "
                                "ending the run with what it has",
                                exc_info=True)
                    break

                # The allowlist, on the address the screenshot actually came
                # from. `act` checks it too and checks it two seconds earlier,
                # which is the whole problem: a navigation slower than the
                # settle's ceiling has not committed when `act` returns, so
                # `page.url` is still the outgoing document and a redirect off
                # the allowlist gets judged against the address it is leaving.
                # Measured on a 3 s server — `act` returns at 2003 ms reading
                # the old URL, and the new one becomes readable at 3063 ms,
                # inside `look`, whose screenshot auto-waits for the pending
                # commit.
                #
                # So the check goes after the read rather than before it.
                # Waiting for the commit first is the other way round and #42
                # ruled on it: the settle is bounded on purpose and a page that
                # never commits must not hold the step open. `look` is bounded
                # by its own timeouts and has to be waited for either way.
                #
                # The page is read and then thrown away, which is the price of
                # keeping that bound. It costs only the bytes: nothing off the
                # allowlist reaches the model, the history or the screen.
                landed = session.page.url
                if landed != "about:blank" and not _allowed(landed):
                    await session.page.goto("about:blank")
                    # Same wording as `act`'s note, because it is the same
                    # event caught later. The model is about to be shown a
                    # blank page in place of the one it asked for, and this
                    # line is the only thing that says why.
                    history.append(
                        f"left the allowlist and was sent back ({landed})")
                    blank, shot, elements, text = True, None, "", ""
                elif shot is None:
                    # In the history as well as in the prompt, because the
                    # next step reads the history and not this one's prompt,
                    # and a step that acted on an element list it could not
                    # see is worth knowing about when the run is read back.
                    #
                    # Below the allowlist check and not up at the read, because
                    # the branch above throws the page away and this line would
                    # then describe a read of an element list nobody was ever
                    # shown. Written once the read is known to have survived,
                    # rather than written and then taken back: there is no
                    # second place that has to remember to undo it.
                    history.append("the page would not give up a screenshot; "
                                   "read it from the element list instead")

                # A full list is a list that was cut, and what got cut is
                # whatever sorts after the sixtieth element in document
                # order — on a page with a long nav, that is the entire
                # article body. Worth a line in the log, because from the
                # outside it looks like the model ignoring an obvious link.
                if elements.count("\n") + 1 >= MAX_ELEMENTS:
                    logger.info("browse step %s: element list is full at %s; "
                                "anything past it was cut", number,
                                MAX_ELEMENTS)

            if text and _walled(text):
                answer = ("That site is showing a bot check rather than the "
                          "page, so I could not read it.")
                break

            tree = ""
            if stuck >= STUCK_AFTER and not blank:
                tree = await session.tree()
                logger.info("browse step %s: stuck for %s steps, adding %s "
                            "chars of accessibility tree", number, stuck,
                            len(tree))

            step = await asyncio.to_thread(
                _decide_sync, task, lang, shot, elements, history, context,
                tree, blank
            )
            logger.info("browse step %s: %s", number, _shape(step))

            if step.get("act") == "answer":
                answer = (step.get("text") or "").strip()
                break

            note, settled = await session.act(step)
            # In a row, not in total: one failure is usually an index that
            # moved under a re-render, and the next screenshot fixes it on its
            # own. Two in a row is the model asking for something it cannot
            # see, and it will keep asking until it is shown something else.
            stuck = 0 if note == "ok" else stuck + 1
            history.append(
                json.dumps(step, ensure_ascii=False)
                + ("" if note == "ok" else f" -> {note}")
            )
            record = {"step": number, "act": step.get("act"),
                      "url": session.page.url, "note": note, "ms": settled}
            steps.append(record)
            await emit({
                "type": "browse", "phase": "step", **record,
                "shot": _data_uri(shot),
            })

        if not answer:
            answer = "I could not find that."
        # The last screenshot is the one worth keeping: it is the page the
        # answer was read off, which is the whole audit trail in one image.
        #
        # Bounded, and best-effort. By here the answer is in hand and this
        # image is not the answer, but the call auto-waits for the pending
        # commit the block below is written around — and left unbounded and
        # uncaught, a page that navigated into a stalled request while the
        # model was deciding raises out of `run` thirty seconds later.
        # `tools/browse.py` turns that into "The browser could not read
        # that", so the person is told the page was unreadable while the
        # sentence the model wrote off it is thrown away. Failing here costs
        # the fresh image and nothing else: what stays on the card is the last
        # screenshot that did come back, which is already what a run ending
        # off the allowlist falls back to.
        fresh = None
        if session.page.url != "about:blank":
            try:
                fresh = await session.page.screenshot(
                    type="jpeg", quality=70, timeout=SHOT_MS)
            except Exception:  # a dead page must not cost the answer
                logger.info("browse: the page the run ended on would not "
                            "give up a screenshot; keeping the last one "
                            "that did", exc_info=True)
        final_url = session.page.url

        # And the allowlist a last time, on the address that screenshot came
        # from. The loop's check runs at the top of a turn, and a run can end
        # on a step that has no next turn for it to run in: the step cap simply
        # stops after the last `act`, and the budget breaks before `look`. The
        # screenshot above auto-waits for the pending commit either way, so
        # without this the audit image is a page the browser was not allowed to
        # be on and `url` hands the model its address.
        #
        # Only the last read is dropped. The answer was taken off a page that
        # passed the check while it was being read, so it is in bounds, and the
        # screenshot kept in its place is the last one that was — a card with
        # the page the answer came from on it is worth more than a blank one.
        # Nothing is added to `history`: there is no turn left to show it to.
        #
        # The test is on the address alone, because the screenshot above is
        # now allowed to fail: a run that ended off the allowlist has to lose
        # that address whether or not an image came back with it.
        if _allowed(final_url):
            if fresh:
                shot = fresh
        elif final_url != "about:blank":
            logger.info("browse: the run ended on %s, off the allowlist; "
                        "keeping neither its address nor its screenshot",
                        final_url)
            final_url = "about:blank"

    result = {
        "task": task,
        "answer": answer,
        "url": "" if final_url == "about:blank" else final_url,
        "steps": steps,
        "shot": _data_uri(shot),
        "seconds": round(time.monotonic() - started, 1),
    }
    await emit({"type": "browse", "phase": "done", **result})
    return result


def _data_uri(shot: bytes | None) -> str:
    if not shot:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(shot).decode()
