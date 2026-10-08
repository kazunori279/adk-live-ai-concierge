#!/usr/bin/env python3
"""Pin what the browser does with a step, without opening one.

`browser.act` is the only place in the repo that acts on the world on the
model's say-so, and every guard it has — the allowlist before a `goto` and
again after a navigation, the label check before a click, the refusal of an
action nobody defined — is a branch that returns a string. Strings are cheap to
pin, and a real Chromium is not: the live pages these run against move, and a
guard that stops working is not something a moving page would show.

So the page is a stand-in here. Everything above it is the real thing —
`_Session.act` unmodified, the real `_allowed`, the real `RISKY`, and both real
playwright error classes — which matters more than it sounds, because
`TimeoutError` is a subclass of `Error` and the two `except` clauses only tell
themselves apart by being in the right order.

    uv run python tools/browse_check.py

Runs every case and exits non-zero if any of them did not hold.

Three sections are the exception, and each gets a real Chromium — two against
`data:` pages and one against an inlined `style.css`. Still no network, and
skipped rather than failed on a machine with no browser to launch. `TAG_JS` is
nothing but geometry and DOM. The one after it is a clock: how long a locator
waits when nobody tells it is not something a stand-in can be wrong about
convincingly, so that one is timed against the real thing. The third counts
the rules the CSS parser kept, which is the only place a selector it threw
away leaves a mark. One more section runs the *tool* rather than the
browser, over a `browser.run` that raises: what a crashed job leaves on the
screen is decided in `tools/browse.py`'s `except` and nowhere else. What is
*not* here is anything that needs a page the repo does not write:
`tools/browse_bench.py` drives those against real sites.
"""

import asyncio
import contextlib
import inspect
import logging
import os
import shutil
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
# And `tools/`, for `js_check.css_text()`. The stylesheet section at the
# bottom borrows that scan rather than keeping a second copy of it: two
# copies of the same strip drift, and a drift here would read as a
# stylesheet the browser broke rather than as a scan that went stale.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import js_check  # noqa: E402
from ai_terminal.live import browser  # noqa: E402
# For the day names the forecast card can print, at the bottom. Taken from the
# tool that writes them rather than copied, so a language added there is
# measured here without anyone remembering to.
from ai_terminal.live.tools import weather  # noqa: E402

failed = False
# Counted rather than written down, for the same reason `match_check.py` counts
# its own: the number goes into a commit message and a typed one drifts.
ran = 0


def check(name: str, got, want) -> None:
    global failed, ran
    ran += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        failed = True


class FakeLocator:
    """One element, or one that will not resolve.

    Every method here takes a `timeout` and records it on the page, because
    every one of them auto-waits and the number it waits for is not visible
    anywhere else. `evaluate` is the one that matters: it is keyword-only on
    the real `Locator.evaluate` too, so a timeout passed positionally lands in
    `arg` and is silently dropped in both.
    """

    def __init__(self, page, label="", raises=False):
        self._page = page
        self._label = label
        # "click after landing" is the click that navigates and only then runs
        # out of time. Reading the label has to work for it to get that far.
        self._late = raises == "click after landing"
        self._raises = raises in (True, "locator")

    async def evaluate(self, _js, arg=None, *, timeout=None):
        self._page.bounded.append(("evaluate", timeout))
        if self._page._raises == "locator error":
            raise browser._page_error()(
                "Error: strict mode violation: locator resolved to 2 elements"
                "\n    1) <button …>\n    2) <button …>")
        if self._raises:
            raise browser._timeout_error()("locator.evaluate timed out")
        return self._label

    async def wait_for(self, *, state=None, timeout=None):
        # Recorded like the rest, because what this bounds is now half the
        # answer to "how long is a tab attributed to the run": the retry the
        # flag used to cover waits here instead. `raises` sends it down the
        # same path `evaluate` takes -- an index the DOM will not resolve does
        # not become visible either.
        self._page.bounded.append((f"wait_for {state}", timeout))
        if self._raises:
            raise browser._timeout_error()("locator.wait_for timed out")

    async def click(self, timeout=None, no_wait_after=None):
        # `no_wait_after` recorded beside the timeout because the two are one
        # decision: the flag is what takes the navigation wait out of the
        # click, and the timeout is only a dispatch budget once it is passed.
        self._page.bounded.append(("click", timeout, no_wait_after))
        if self._page._raises == "click aborted":
            # The same error string, arriving from the click instead of from
            # the navigation it caused. Nothing was dispatched, so the clause
            # that forgives an abort has to not forgive this one — which is
            # what `dispatched` is doing in that condition.
            raise browser._page_error()(
                "Error: net::ERR_ABORTED; maybe frame was detached?")
        if self._late:
            self._page._requested()
            self._page.url = self._page._lands
            raise browser._timeout_error()("locator.click timed out")
        if self._raises:
            # Nothing dispatched, so nothing was asked of the network. A
            # click stuck on actionability is the case that must still fail.
            raise browser._timeout_error()("locator.click timed out")
        self._page.did.append("click")
        if self._page._navigates:
            self._page._requested()

    async def fill(self, text, timeout=None):
        self._page.bounded.append(("fill", timeout))
        if self._raises:
            raise browser._timeout_error()("locator.fill timed out")
        self._page.did.append(f"fill {text}")


class FakeKeyboard:
    def __init__(self, page):
        self._page = page

    async def press(self, key):
        if self._page._raises == "press error":
            # What Playwright says to a key name the model invented. Not a
            # timeout: nothing was waited for, the name was simply not a key.
            raise browser._page_error()(
                f"Error: Unknown key: {key!r}\nCall log:\n  - waiting for …")
        self._page.did.append(f"press {key}")
        # In the timeline as well as the action log, because *when* the key is
        # delivered is the whole of #42: inside the wait for the commit, or
        # before it with the commit free to beat the wait being opened.
        self._page.waited.append(("press", key))


class FakeRequest:
    """One `request` event, as far as `act` reads one.

    Two questions and no more: whether it is a navigation, and whose frame it
    belongs to. The frame is compared by identity in `act`, so it has to be
    the same object the page hands back as `main_frame` — a stand-in that
    returned a fresh one each time would answer *not ours* to every request
    and the discrimination would silently never fire.
    """

    def __init__(self, frame, navigation=True):
        self.frame = frame
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class FakeNavigation:
    """`page.expect_navigation`, as far as `act` uses one.

    The commit lands in `__aexit__` — after the key was delivered, before `act`
    is allowed to move on — which is the property the whole thing exists for.
    It goes in the page's `waited` list next to the settle's own waits so a case
    can read the order off one list instead of inferring it from two.

    The sleep is real, because `spent_ms` is measured rather than declared: a
    settle that failed to subtract it would still look right against a stand-in
    whose waits are all instantaneous.
    """

    def __init__(self, page, wait_until, timeout):
        self._page, self._until, self._timeout = page, wait_until, timeout

    async def __aenter__(self):
        self._page.waited.append(("navigation", self._until, self._timeout))
        return self

    async def __aexit__(self, _kind, err, _tb):
        if err is not None:
            # What the real one does when the body raises: cancel the wait
            # rather than sit it out. A key name that is not a key must cost
            # nothing, not the whole ceiling.
            self._page.waited.append(("nav cancelled", None))
            return False
        await asyncio.sleep(self._page._nav_ms / 1000)
        if not self._page._commits:
            raise browser._timeout_error()("navigation never committed")
        self._page.url = self._page._commits
        self._page.waited.append(("committed", self._page.url))
        return False


# Two different JPEGs, so a case can say *which* screenshot ended up on the
# card: the one `look` took inside the loop, or the one `run` takes at the end.
LOOK_SHOT = b"\xff\xd8\xffL"
FINAL_SHOT = b"\xff\xd8\xffF"


class FakePage:
    """Enough of a Playwright page for `act` to run against.

    `waited` is the interesting one: the step's timeline, in order. Every wait
    `act` asked for, so a case can say what the settle actually cost rather
    than only that it happened, and the two moments that have to fall between
    them — the key going in and the new document committing. `bounded` is its
    counterpart for the waits nobody asked for out loud — the ones a locator
    call does on its own, where the failure is a call that took playwright's
    30 s default because no timeout was passed.

    `commits` is where a press navigates to, or `None` for a press that
    navigates nowhere, and `nav_ms` is how long the browser takes to get there.
    """

    def __init__(self, url="about:blank", label="", raises=False,
                 lands=None, idle_timeout=False, snapshot="", closed=False,
                 commits=None, nav_ms=0, ends_on=None, shot_raises=None,
                 navigates=None):
        self.url = url
        # Whether a dispatched click asks the network for a page. Inferred
        # from `commits` rather than declared, so every fixture written before
        # `act` could tell the two apart still means what it meant: a page
        # that commits somewhere is one that navigated to get there. Passed
        # explicitly only for the case the inference cannot reach — a click
        # that asks and never arrives.
        self._navigates = bool(commits) if navigates is None else navigates
        self.main_frame = object()
        self._handlers: dict[str, list] = {}
        self._ends_on = ends_on
        self._shot_raises = shot_raises
        self._closed = closed
        self.did: list[str] = []
        self.waited: list = []
        self.bounded: list = []
        self.shots: list = []
        self.snapshots: list[dict] = []
        self.keyboard = FakeKeyboard(self)
        self._label = label
        self._raises = raises
        self._lands = lands
        self._idle_timeout = idle_timeout
        self._snapshot = snapshot
        self._commits = commits
        self._nav_ms = nav_ms

    def expect_navigation(self, *, wait_until=None, timeout=None):
        return FakeNavigation(self, wait_until, timeout)

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        # Not `discard`-shaped: a listener `act` removed twice, or removed
        # without adding, is a leak on the real page too and should be loud.
        self._handlers[event].remove(handler)

    def _requested(self):
        """The navigation request a dispatched click issues, delivered."""
        for handler in list(self._handlers.get("request", ())):
            handler(FakeRequest(self.main_frame))

    def is_closed(self) -> bool:
        # The one liveness test that does not itself raise on a dead page,
        # which is why `act` asks this and not, say, `page.title()`.
        return self._closed

    async def goto(self, url, wait_until=None, timeout=None):
        if self._raises == "goto error":
            raise browser._page_error()(
                "Error: net::ERR_NAME_NOT_RESOLVED at " + url
                + "\nCall log:\n  - navigating to …")
        if self._raises == "goto":
            raise browser._timeout_error()("goto timed out")
        self.did.append(f"goto {url}")
        # Where a navigation actually ends up, which is not always where it was
        # pointed — that is the whole reason for the check after the fact.
        self.url = self._lands if self._lands and url != "about:blank" else url
        # A redirect commits and *then* the wait for `domcontentloaded` runs
        # out. Playwright raises with `page.url` already moved, which is the
        # state no case could express while `goto` raised before assigning it.
        if self._raises == "goto after landing":
            # Once: the real thing only times out on the slow navigation, not
            # on the `about:blank` the allowlist check sends it back to.
            self._raises = False
            raise browser._timeout_error()("goto timed out after the redirect")

    def locator(self, _selector):
        return FakeLocator(self, self._label, self._raises)

    async def wait_for_timeout(self, ms):
        self.waited.append(("timeout", ms))

    async def wait_for_load_state(self, state, timeout=None):
        self.waited.append(("load_state", state, timeout))
        if self._idle_timeout:
            raise browser._timeout_error()("networkidle never settled")

    async def screenshot(self, **kwargs):
        # A commit landing here rather than inside `look`, which is where the
        # one from a run's last step lands: no turn is left to read it, and
        # this screenshot is what waits for it.
        #
        # `shots` records the timeout each one was given, because a screenshot
        # auto-waits for that commit and the number it waits for is not
        # visible anywhere else — the same reason `bounded` exists for the
        # locator calls. `shot_raises` is what happens when the wait runs out:
        # the commit lands first and then the call gives up, which is the
        # order the real one fails in.
        self.shots.append(kwargs.get("timeout"))
        if self._ends_on:
            self.url, self._ends_on = self._ends_on, None
        if self._shot_raises == "timeout":
            raise browser._timeout_error()(
                "Page.screenshot: Timeout 8000ms exceeded.")
        if self._shot_raises:
            # A closed or crashed page is the plain `Error`, not a timeout.
            raise browser._page_error()(
                "Error: Target page, context or browser has been closed")
        return FINAL_SHOT

    async def aria_snapshot(self, **kwargs):
        self.snapshots.append(kwargs)
        if self._raises == "snapshot":
            raise browser._timeout_error()("aria snapshot timed out")
        return self._snapshot


def act(page, step):
    """Run one step against a fake page and hand back its note."""
    session = browser._Session()
    session.page = page
    return asyncio.run(session.act(step))


def acted(page, step) -> tuple:
    """What `act` came back with, or the name of what it raised instead.

    A step that raises out of `act` is the exact failure these cases exist to
    catch, so it comes back as a string. Left to propagate it would end the run
    in a traceback from the middle of the file, which reads as a case that was
    never reached rather than one that failed.
    """
    try:
        return act(page, step)
    except Exception as exc:
        return f"raised {type(exc).__name__}", -1


def raised(call) -> str:
    """What `call` raised, as a string, or what it returned instead.

    A case whose subject is a raise has to be able to fail without ending the
    file, and `str(exc)` rather than the type keeps the message in the report
    where a reader can recognise it.
    """
    try:
        return f"returned {call()!r}"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def env(**values):
    """Set BROWSE_* for one case and put the environment back after."""
    before = {k: os.environ.get(k) for k in values}

    class _Scope:
        def __enter__(self):
            for key, value in values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        def __exit__(self, *_):
            for key, value in before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return _Scope()


class FakeModels:
    """A stand-in for `client.models`, keeping every call it is given.

    A list of replies rather than one, so a case can script a whole run: the
    loop asks once per step and what it asks with is what the last step did.
    """

    def __init__(self, text):
        self._replies = list(text) if isinstance(text, list) else None
        self.text = text
        self.calls: list[dict] = []

    @property
    def sent(self):
        return self.calls[-1] if self.calls else None

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self._replies is None:
            return SimpleNamespace(text=self.text)
        return SimpleNamespace(text=self._replies.pop(0))


class FakeSession:
    """A `_Session` that opens nothing, for driving `run` without a browser.

    `notes` scripts what `act` reports, one per step, so a case can put the
    loop into the state where every step fails and watch what it does about
    it. `asked` counts the trees handed out, which is the only outward sign
    that the fallback fired at all. `commits` is a redirect that only becomes
    readable inside `look`, `ends_on` one that waits until the screenshot the
    run signs off with, and `looked` counts the reads.
    """

    def __init__(self, url="https://example.com/", notes=None,
                 elements="0. <a> next", tree="- heading \"Departures\"",
                 commits=None, ends_on=None, shot_raises=None,
                 shotless=False, dies_on=None, text="a page"):
        self.page = FakePage(url=url, ends_on=ends_on,
                             shot_raises=shot_raises)
        # A `look` that came back without a picture, which is a state and not
        # a failure: the page answered `evaluate` and only the screenshot
        # timed out. `shot_raises` is the page's version of this and cannot
        # reach the loop, because this class replaces `look` outright.
        self._shotless = shotless
        # The read this session's tab does not survive. `shot_raises` cannot
        # reach the loop, because this class replaces `look` outright, so a
        # tab dying between one step and the next is expressed here instead.
        self._dies_on = dies_on
        self._commits = commits
        self.acted: list[dict] = []
        self.asked = 0
        self.looked = 0
        self._notes = list(notes or [])
        self._elements = elements
        self._text = text
        self._tree = tree

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def look(self):
        self.looked += 1
        if self._dies_on is not None and self.looked >= self._dies_on:
            raise browser._page_error()(
                "Error: Target page, context or browser has been closed")
        # The commit landing inside the read, which is where the real one
        # lands: the screenshot auto-waits for a navigation the settle already
        # gave up on. Measured at 3063 ms against an `act` that returned at
        # 2003 — so the URL is one page before this call and another after it.
        if self._commits:
            self.page.url, self._commits = self._commits, None
        shot = None if self._shotless else LOOK_SHOT
        return shot, self._elements, self._text

    async def tree(self):
        self.asked += 1
        return self._tree

    async def act(self, step):
        self.acted.append(step)
        return (self._notes.pop(0) if self._notes else "ok"), 0


def logged(name="ai_terminal.live.browser"):
    """Collect what the module logs for one case."""
    lines: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    class _Scope:
        def __enter__(self):
            self.log = logging.getLogger(name)
            self.handler = _Handler()
            self.before = self.log.level
            self.log.setLevel(logging.INFO)
            self.log.addHandler(self.handler)
            return lines

        def __exit__(self, *_):
            self.log.removeHandler(self.handler)
            self.log.setLevel(self.before)

    return _Scope()


def sessions(session):
    """Have `run` open `session` instead of a real browser."""
    before = browser._Session

    class _Scope:
        def __enter__(self):
            browser._Session = lambda: session
            return session

        def __exit__(self, *_):
            browser._Session = before

    return _Scope()


def decide(text='{"act": "answer", "text": "42"}'):
    """Point `_decide_sync` at a client that answers `text`, and hand it back.

    Patching the accessor rather than the client it returns, because *which*
    accessor is part of what is being pinned: 3.8 Flash is served from the
    global endpoint only, so a `_decide_sync` that went back to the regional
    `genai_client` would 404 on every step. Here that shows up as a `NameError`
    — `browser` does not import the regional one at all any more.
    """
    models = FakeModels(text)
    before = browser.global_client

    class _Scope:
        def __enter__(self):
            browser.global_client = lambda: SimpleNamespace(models=models)
            return models

        def __exit__(self, *_):
            browser.global_client = before

    return _Scope()


print("== _allowed: scheme and suffix ==")
check("an ordinary https page is fine with no list",
      browser._allowed("https://www.jorudan.co.jp/"), True)
check("and http too", browser._allowed("http://example.com/"), True)
# `file://` is the one that matters. The Pi runs the kiosk as a user with a
# home directory full of `.env` and ADC, and `goto` is a string from a model.
check("but a file: URL is not a web page", browser._allowed("file:///etc/passwd"),
      False)
check("nor is data:", browser._allowed("data:text/html,<h1>hi"), False)
check("nor is a bare hostname with no scheme", browser._allowed("example.com"),
      False)
with env(BROWSE_ALLOWLIST="jorudan.co.jp, example.com"):
    check("on a list, a listed host passes",
          browser._allowed("https://jorudan.co.jp/x"), True)
    check("and so does a subdomain of one",
          browser._allowed("https://www.jorudan.co.jp/x"), True)
    check("an unlisted host does not",
          browser._allowed("https://www.google.com/"), False)
    # The suffix test is on a dot boundary, not on the string: without it
    # `evil-jorudan.co.jp` and `notexample.com` are both on the list.
    check("nor does one that merely ends with the same letters",
          browser._allowed("https://evil-jorudan.co.jp/"), False)
    check("nor a listed host as somebody else's subdomain",
          browser._allowed("https://jorudan.co.jp.evil.test/"), False)

print("\n== _allowlist: entries no host could equal (#229) ==")
# `_allowed` above matches `parsed.hostname` by equality or dotted suffix and
# does nothing else, so none of these five is a pattern — each is a hostname
# no page has. Taken silently, each is a browse that starts, refuses every
# `goto` and ends on `about:blank`, with `pi_deploy.py`'s row green throughout
# because the key is set. The mistake is plausible precisely because one of
# the obvious spellings is normalised for you: `.jorudan.co.jp` works, so the
# rest read as though they would.
for spelling in ("*", "*.jorudan.co.jp", "https://jorudan.co.jp",
                 "jorudan.co.jp/timetable", "jorudan.co.jp:443"):
    with env(BROWSE_ALLOWLIST=spelling):
        said = raised(browser._allowlist)
    check(f"{spelling} is refused rather than taken as a hostname",
          said.startswith("ValueError: "), True)
    # Named, because the person reading this has one line to edit and the
    # variable holds several entries. A message that says only "invalid" sends
    # them through the whole list by eye.
    check(f"and the message quotes {spelling} back",
          said.startswith("ValueError: ") and repr(spelling) in said, True)
# One bad entry among good ones is still bad. Without this the cheap
# implementation is a filter, which leaves a list that looks configured and
# quietly reaches only its good half.
with env(BROWSE_ALLOWLIST="jorudan.co.jp, *.example.com"):
    check("a bad entry beside a good one is refused, not dropped",
          raised(browser._allowlist).startswith("ValueError: "), True)
# Separators and nothing else parses to `[]`, which is how the default is
# spelled — so unattached it would read as "anywhere" and attached it would
# hit #208's guard, while the deploy probe called the key set either way.
with env(BROWSE_ALLOWLIST=" , , "):
    check("a value that is only separators is refused, not read as unset",
          raised(browser._allowlist).startswith("ValueError: "), True)
# The same nothing written with a dot, which gets past both rules above:
# `if d.strip()` keeps the field because it is not blank, `lstrip(".")` then
# empties it, and `''` is an entry no host equals. The list is non-empty, so
# #208's guard attaches; and `_allowed` compares `host.endswith("." + d)`, so
# an empty entry is not "nothing matches" but "every host spelled with a
# trailing dot matches" — the allowlist inverted by one character.
with env(BROWSE_ALLOWLIST="."):
    check("an entry the strip empties is refused too",
          raised(browser._allowlist).startswith("ValueError: "), True)
    check("and the message quotes the field as it was written",
          "'.'" in raised(browser._allowlist), True)
with env(BROWSE_ALLOWLIST="jorudan.co.jp, ."):
    check("beside a good entry it is still refused, not dropped",
          raised(browser._allowlist).startswith("ValueError: "), True)
# And the two spellings that have to stay silent, because every machine that
# is not the panel runs on them and a raise here would stop all of them
# browsing at all.
with env(BROWSE_ALLOWLIST=None):
    check("unset is still the empty list, and still means anywhere",
          (browser._allowlist(), browser._allowed("https://www.google.com/")),
          ([], True))
with env(BROWSE_ALLOWLIST="  "):
    check("and so is whitespace, which is what `BROWSE_ALLOWLIST=` gives",
          browser._allowlist(), [])
# The normalisation that does work has to keep working: the raise is written
# over the same comprehension, and the cheap way to add it loses the strip.
with env(BROWSE_ALLOWLIST=" .Jorudan.CO.JP , example.com "):
    check("a leading dot, upper case and padding are still normalised away",
          browser._allowlist(), ["jorudan.co.jp", "example.com"])

print("\n== _walled: a bot check is not a page ==")
check("Cloudflare's interstitial", browser._walled("Just a moment..."), True)
check("and it is case-insensitive",
      browser._walled("JUST A MOMENT while we check your browser"), True)
check("an ordinary short page is not a wall",
      browser._walled("Example Domain. This domain is for use in examples."),
      False)
# The length bound is what stops a real article about Cloudflare being read as
# a Cloudflare page. Without it any page quoting the phrase is a dead end.
check("nor is a long page that happens to say the words",
      browser._walled("Just a moment. " + "x" * 700), False)

print("\n== act: the guards ==")
page = FakePage()
check("a goto off the allowlist is refused before it is made",
      act(page, {"act": "goto", "url": "file:///etc/passwd"}),
      ("refused: file:///etc/passwd is not on the allowlist", 0))
check("and nothing was navigated", page.did, [])
check("an action nobody defined is a note, not a crash",
      act(FakePage(), {"act": "screenshot"}),
      ("unknown action 'screenshot'", 0))
check("a click on a label that spends money is refused",
      act(FakePage(url="https://shop.test/", label="今すぐ購入"),
          {"act": "click", "idx": 3})[0],
      "refused: '今すぐ購入' looks like it commits to something")
check("and in English too",
      act(FakePage(url="https://shop.test/", label="Buy now"),
          {"act": "click", "idx": 3})[0],
      "refused: 'Buy now' looks like it commits to something")
check("an ordinary link is not",
      act(FakePage(url="https://shop.test/", label="Opening hours"),
          {"act": "click", "idx": 3})[0], "ok")
check("a click with an injection selector is refused before any locator resolves",
      act(FakePage(url="https://x.test/"),
          {"act": "click", "idx": '0"] , input[type="password"]'}),
      ("failed: click '0\"] , input[type=\"password\"]' is not a valid index", 0))
check("a type with an injection selector is refused too",
      act(FakePage(url="https://x.test/"),
          {"act": "type", "idx": '0\"] , input[type=\"password\"]', "text": "secret"}),
      ("failed: type '0\"] , input[type=\"password\"]' is not a valid index", 0))
check("a boolean index is not accepted as an integer",
      act(FakePage(url="https://x.test/"), {"act": "click", "idx": True}),
      ("failed: click True is not a valid index", 0))
check("a negative index is rejected",
      act(FakePage(url="https://x.test/"), {"act": "click", "idx": -1}),
      ("failed: click -1 is not a valid index", 0))
check("a missing index is rejected",
      act(FakePage(url="https://x.test/"), {"act": "click"}),
      ("failed: click None is not a valid index", 0))
check("a string integer index is coerced to integer",
      act(FakePage(url="https://shop.test/", label="Opening hours"),
          {"act": "click", "idx": "3"})[0], "ok")

print("\n== act: what an attached run may do, not only where it may go ==")


def attached_act(page, step):
    """One step against a fake page, as an attached run takes it.

    `_attached` set directly and not `env(BROWSE_CDP=…)`, because the flag is
    what `act` reads: `__aenter__` sets it once the connection is up, and
    these cases are about the step rather than about the connection. The
    attach that sets it is pinned in its own section further down.
    """
    session = browser._Session()
    session.page = page
    session._attached = True
    return asyncio.run(session.act(step))


# The gap this section exists for. `BROWSE_ALLOWLIST` gates the address and
# `RISKY` gates a label; a keystroke has neither, so before this the one
# action that submits a form was the one action nothing looked at. On the
# panel's profile that is Enter in a compose window.
entered = FakePage(url="https://mail.google.test/")
note, settled = attached_act(entered, {"act": "press", "key": "Enter"})
check("Enter is refused on the profile that is signed in", note,
      "refused: 'Enter' is not a key this profile may press — it is signed "
      "in, so a key that submits is a key that commits. Scroll with PageDown, "
      "or put the query in the URL and goto it")
check("and the key never reached the page", entered.did, [])
check("and nothing was waited for on its behalf", entered.waited,
      [])
check("and the step is not charged a settle it never opened", settled, 0)
# The default is what a step with no key at all presses, so the guard has to
# be on the resolved key and not on `step["key"]`.
check("a press that names no key is refused too, because Enter is the default",
      attached_act(FakePage(), {"act": "press"})[0].startswith(
          "refused: 'Enter'"), True)
# The three spellings a denylist of `Enter` would have missed, which is why
# this is an allowlist of names.
check("and so is the modifier spelling that sends a Gmail draft",
      attached_act(FakePage(), {"act": "press", "key": "Control+Enter"})[0],
      "refused: 'Control+Enter' is not a key this profile may press — it is "
      "signed in, so a key that submits is a key that commits. Scroll with "
      "PageDown, or put the query in the URL and goto it")
# A single character is not a key: playwright types it
# (`async_api/_generated.py:1600`, 1.62.0), so this is the keystroke Gmail
# reads as *delete thread* arriving with no label for `RISKY` to read.
check("a single character is a keystroke, not a key, and is refused",
      attached_act(FakePage(), {"act": "press", "key": "#"})[0].startswith(
          "refused: '#'"), True)
check("and a modifier on an allowed key is not that key",
      attached_act(FakePage(), {"act": "press", "key": "Control+End"})[0]
      .startswith("refused: 'Control+End'"), True)

scrolled = FakePage(url="https://mail.google.test/")
check("the keys that scroll still work, because a stuck run is told to scroll",
      attached_act(scrolled, {"act": "press", "key": "PageDown"})[0], "ok")
check("and the key that went through is the one that was asked for",
      scrolled.did, ["press PageDown"])
# The whole set, rather than the one member above: a key added to `READ_KEYS`
# that submits is the way this guard stops working, and nothing else would say.
check("every key on the list is accepted, and none of them is Enter",
      sorted(k for k in browser.READ_KEYS
             if attached_act(FakePage(), {"act": "press", "key": k})[0]
             != "ok"),
      [])
check("Enter is not on it, and neither is any spelling of it",
      sorted(k for k in browser.READ_KEYS if "enter" in k.lower()), [])
# The control, and the case the pair above is worthless without: a guard
# written on the key alone reads the same in every case here and takes Enter
# away from every machine that is not the panel. The throwaway is signed into
# nothing, so a key that submits commits nothing.
loose = FakePage(url="https://x.test/")
check("but a throwaway may still press Enter, because it is nobody",
      act(loose, {"act": "press", "key": "Enter"})[0], "ok")
check("and the key really went through on that path", loose.did,
      ["press Enter"])

# The second pattern. These are free on a shop and are the household's own
# data on the panel's profile, which is why they are not in `RISKY`.
check("a click that archives the household's mail is refused",
      attached_act(FakePage(url="https://mail.google.test/", label="Archive"),
                   {"act": "click", "idx": 3})[0],
      "refused: 'Archive' acts on the account this browser is signed into, "
      "and this run only reads it")
check("and in Japanese too",
      attached_act(FakePage(url="https://mail.google.test/",
                            label="アーカイブ"),
                   {"act": "click", "idx": 3})[0].startswith(
          "refused: 'アーカイブ' acts on the account"), True)
check("and a share, which gives a document away rather than destroying it",
      attached_act(FakePage(url="https://docs.google.test/", label="Share"),
                   {"act": "click", "idx": 3})[0].startswith(
          "refused: 'Share' acts on the account"), True)
shared = FakePage(url="https://docs.google.test/", label="Share")
attached_act(shared, {"act": "click", "idx": 3})
check("and the click was never dispatched", shared.did, [])
# The control again: the same label on the throwaway is an ordinary click, and
# on a news page it is the commonest control there is.
check("but a throwaway may share, because there is no account behind it",
      act(FakePage(url="https://news.test/", label="Share"),
          {"act": "click", "idx": 3})[0], "ok")
# Order, because the two patterns overlap and the reason the model is given
# has to be the one that is true everywhere. `RISKY` is: it holds on the
# throwaway too, so a run reading its note has learnt something that does not
# stop being true when `BROWSE_CDP` is unset. "Unsubscribe" is the overlap
# without contriving one — `RISKY`'s `subscribe` is a substring of it and
# `SIGNED_IN_RISKY` names it outright.
check("a label both patterns match reports the one that holds off the panel "
      "too",
      attached_act(FakePage(url="https://mail.google.test/",
                            label="Unsubscribe"),
                   {"act": "click", "idx": 3})[0],
      "refused: 'Unsubscribe' looks like it commits to something")
check("and it is genuinely an overlap, not a label only one of them reads",
      (bool(browser.RISKY.search("Unsubscribe")),
       bool(browser.SIGNED_IN_RISKY.search("Unsubscribe"))), (True, True))
check("a RISKY label on the panel still reports the RISKY reason",
      attached_act(FakePage(url="https://shop.test/", label="Buy now"),
                   {"act": "click", "idx": 3})[0],
      "refused: 'Buy now' looks like it commits to something")
check("an ordinary link is still an ordinary link on the panel",
      attached_act(FakePage(url="https://jorudan.test/",
                            label="Opening hours"),
                   {"act": "click", "idx": 3})[0], "ok")
# Deliberately not refused, and pinned so that stays a decision rather than an
# oversight: refusing it takes the search box away from the one browser that
# can use the household's session, which is why it is attached.
#
# It is not "typing commits nothing", and that phrasing was wrong in an earlier
# draft of this. On a page with nothing to submit the keystrokes are the write:
# a Google Doc has no send button, and a Gmail compose autosaves into their
# Drafts with nobody pressing anything. Both are the account's own properties
# rather than an action against a third party, which is where the line was
# drawn — #238, and the owner's to move.
typed = FakePage(url="https://mail.google.test/")
check("typing is still allowed on an attached run, on purpose",
      attached_act(typed, {"act": "type", "idx": 2, "text": "sendai"})[0],
      "ok")
check("and it is the text the model asked for", typed.did, ["fill sendai"])

# The check after the fact. A click that redirects somewhere off the list is
# the only way past the pre-check, and it is the likelier one: the model never
# named the destination, the page did.
with env(BROWSE_ALLOWLIST="jorudan.co.jp"):
    landed = FakePage(url="https://jorudan.co.jp/",
                      lands="https://ads.example.com/x")
    note, _ = act(landed, {"act": "goto", "url": "https://jorudan.co.jp/go"})
    check("a redirect off the list is caught after the navigation",
          note, "left the allowlist and was sent back "
                "(https://ads.example.com/x)")
    check("and the page was sent back to blank",
          landed.did[-1], "goto about:blank")

    # The same redirect, but the wait for `domcontentloaded` runs out after it
    # has committed. The check used to sit below an early `return` on this
    # path, so the browser was left on the ad domain and the next step of the
    # loop screenshotted it, read its text and sent both to the model.
    late = FakePage(url="https://jorudan.co.jp/",
                    lands="https://ads.example.com/x",
                    raises="goto after landing")
    note, _ = act(late, {"act": "goto", "url": "https://jorudan.co.jp/go"})
    check("a goto that lands off the list and only then times out is caught too",
          note, "failed: goto 'https://jorudan.co.jp/go' did not land in time; "
                "left the allowlist and was sent back "
                "(https://ads.example.com/x)")
    check("and that page is sent back to blank as well",
          late.did[-1], "goto about:blank")

    # A click is the other way to get there, and it never named a destination
    # at all — the page did.
    clicked = FakePage(url="https://jorudan.co.jp/", label="Departures",
                       lands="https://ads.example.com/x",
                       raises="click after landing")
    note, _ = act(clicked, {"act": "click", "idx": 4})
    check("so is a click that navigates off it and then times out",
          note, "failed: click 4 did not land in time; left the allowlist and "
                "was sent back (https://ads.example.com/x)")
    check("and it is sent back too", clicked.did[-1], "goto about:blank")

    # The note still has to reach the model. Losing the timeout half would
    # leave it thinking the address it asked for is the one that answered.
    check("both halves are reported, not just the second",
          note.startswith("failed: click 4"), True)

    # An action that times out without leaving the list is unchanged: one note,
    # and the browser stays where it is.
    stayed = FakePage(url="https://jorudan.co.jp/",
                      lands="https://www.jorudan.co.jp/x",
                      raises="goto after landing")
    note, _ = act(stayed, {"act": "goto", "url": "https://jorudan.co.jp/go"})
    check("a timeout that stayed on the list reports only the timeout",
          note, "failed: goto 'https://jorudan.co.jp/go' did not land in time")
    check("and nothing sent it anywhere", stayed.did, ["goto https://jorudan.co.jp/go"])

print("\n== act: a failure is a note, not the end of the run ==")
# Before this, nothing caught the timeout. It propagated out of `run`, past the
# `async with`, and the task ended as `{"error": ...}` — so the card froze on
# the previous step, no `done` frame was ever emitted, and the model never got
# the chance to pick a different element that the prompt keeps telling it to
# take.
check("a click on an index the DOM will not resolve",
      act(FakePage(url="https://x.test/", raises=True),
          {"act": "click", "idx": 42}),
      ("failed: click 42 did not land in time", 0))
check("a type into one, the same",
      act(FakePage(url="https://x.test/", raises=True),
          {"act": "type", "idx": 7, "text": "恵比寿"})[0],
      "failed: type 7 did not land in time")
# `goto` and `press` carry no index, so naming one would name `None`. Each is
# reported by the thing it actually had.
check("a goto that never commits names the url",
      act(FakePage(raises="goto"),
          {"act": "goto", "url": "https://slow.test/"})[0],
      "failed: goto 'https://slow.test/' did not land in time")
unsettled = FakePage(url="https://x.test/", raises=True)
check("and the settle is not charged for a step that never happened",
      act(unsettled, {"act": "click", "idx": 42})[1], 0)
# The number alone is not enough: `_settle` against a stand-in page returns 0 ms
# whether it was called or not, so a step that pays for it in the real browser
# still reads as free here. What is checkable is that it was never asked for.
check("and it was not waited for either", unsettled.waited, [])

print("\n== act: a mistake is a note too, not only slowness ==")
# The timeout clause above only ever covered the steps that ran out of time.
# Everything else Playwright raises — a key name the model invented, a host DNND
# will not resolve, a selector two elements answer — went straight out of the
# loop and ended the whole task, which is the failure the clause above exists to
# stop. Same treatment, different wording.
check("TimeoutError is a subclass of the base error",
      issubclass(browser._timeout_error(), browser._page_error()), True)
check("so the timeout clause has to come first",
      [type(exc).__name__ for exc in
       (browser._timeout_error()("x"), browser._page_error()("x"))],
      ["TimeoutError", "Error"])
check("a key name the model invented is a note",
      acted(FakePage(url="https://x.test/", raises="press error"),
            {"act": "press", "key": "Ctrl+葉"})[0],
      "failed: press 'Ctrl+葉' — Error: Unknown key: 'Ctrl+葉'")
check("a host that will not resolve is a note",
      acted(FakePage(raises="goto error"),
            {"act": "goto", "url": "https://jorudan.co.jp/nope"})[0],
      "failed: goto 'https://jorudan.co.jp/nope' — Error: "
      "net::ERR_NAME_NOT_RESOLVED at https://jorudan.co.jp/nope")
# And the half of the abort clause the loopback fixtures cannot reach. There
# the abort always arrives from a navigation a click had already dispatched;
# here it arrives from the click itself, and the step really did not happen.
check("an abort with nothing dispatched is still the click's own failure",
      acted(FakePage(url="https://x.test/", raises="click aborted"),
            {"act": "click", "idx": 3})[0],
      "failed: click 3 — Error: net::ERR_ABORTED; maybe frame was detached?")
check("and a selector two elements answer names the index",
      acted(FakePage(url="https://x.test/", raises="locator error"),
            {"act": "click", "idx": 4})[0],
      "failed: click 4 — Error: strict mode violation: locator resolved to "
      "2 elements")
# Playwright puts a call log under the message: retry timings, dozens of lines
# of them, saying nothing the model can act on.
check("the call log under the message is dropped",
      "Call log" in acted(FakePage(url="https://x.test/",
                                   raises="press error"),
                          {"act": "press", "key": "F13"})[0], False)
long_key = "K" * 400
check("and a message longer than the cap is cut to it",
      len(acted(FakePage(url="https://x.test/", raises="press error"),
                {"act": "press", "key": long_key})[0]),
      len(f"failed: press {long_key!r} — ") + 120)
unhurt = FakePage(url="https://x.test/", raises="press error")
check("the settle is not charged for a step that never happened",
      acted(unhurt, {"act": "press", "key": "F13"})[1], 0)
# The navigation wait is the one thing that did happen, because the key is
# delivered inside it. It is cancelled rather than sat out — `F13` is not a key
# and no amount of waiting will make it one, so an invented key name must cost
# nothing rather than the whole ceiling.
check("and the only wait it opened was cancelled, not sat out", unhurt.waited,
      [("navigation", "commit", browser.SETTLE_MS), ("nav cancelled", None)])
# A timeout is one of these errors by inheritance, so the wording is the only
# thing keeping the two apart. Reading it back is how the clause order is
# checked at all: swap them and every case above still passes, while every
# timeout starts reporting a call-log line instead of "did not land in time".
check("a timeout still gets the timeout wording, not this one",
      act(FakePage(url="https://x.test/", raises=True),
          {"act": "click", "idx": 42})[0],
      "failed: click 42 did not land in time")

# The line between the two. A mistake made *on* a page is the model's and has
# another route; a page that is gone is a browser that cannot carry out the
# next step either, and pretending otherwise would have the loop screenshot a
# corpse for the rest of its budget.
dead = FakePage(url="https://x.test/", raises="press error", closed=True)
try:
    act(dead, {"act": "press", "key": "F13"})
    check("a closed page still ends the run", "returned a note", "raised")
except Exception as exc:
    check("a closed page still ends the run", type(exc).__name__, "Error")
    check("and the error is the one Playwright raised",
          str(exc).splitlines()[0], "Error: Unknown key: 'F13'")

# #41's shared exit, on the new path. The clause assigns `note, settled` and
# falls through rather than returning, so a click that navigates off the
# allowlist and *then* errors is still caught and still sent back.
with env(BROWSE_ALLOWLIST="jorudan.co.jp"):
    onlist = FakePage(url="https://jorudan.co.jp/", raises="goto error")
    check("a mistake on the list reports only itself",
          acted(onlist, {"act": "goto", "url": "https://jorudan.co.jp/x"})[0],
          "failed: goto 'https://jorudan.co.jp/x' — Error: "
          "net::ERR_NAME_NOT_RESOLVED at https://jorudan.co.jp/x")
    check("and nothing sent it anywhere", onlist.did, [])
    # The click that navigated off the list and only then hit a bad selector.
    # `settled` is assigned rather than returned, so the check below the
    # `except` runs on this path exactly as it does on the timeout one.
    off = FakePage(url="https://ads.example.com/x", raises="locator error")
    note, _ = acted(off, {"act": "click", "idx": 4})
    check("a mistake off the list reports both", note,
          "failed: click 4 — Error: strict mode violation: locator resolved "
          "to 2 elements; left the allowlist and was sent back "
          "(https://ads.example.com/x)")
    check("and the page was sent back to blank", off.did, ["goto about:blank"])

print("\n== act: every locator call says how long it will wait ==")
# The label read was the one that did not. `evaluate` auto-waits like every
# other locator method, so on an index the DOM will not resolve it sat for
# playwright's 30 s default and the note came back four times later than the
# 8 s the click on the next line asks for. Four of those and `browse` has spent
# its whole 120 s budget on misses.
risky = FakePage(url="https://shop.test/", label="今すぐ購入")
act(risky, {"act": "click", "idx": 3})
check("the label read is bounded, not left to the default",
      risky.bounded, [("evaluate", 8000)])
check("and it still refused, so the read gave back the same string",
      act(FakePage(url="https://shop.test/", label="今すぐ購入"),
          {"act": "click", "idx": 3})[0],
      "refused: '今すぐ購入' looks like it commits to something")
hit = FakePage(url="https://x.test/", label="Departures")
act(hit, {"act": "click", "idx": 4})
check("a click bounds all three of the calls it makes",
      hit.bounded,
      [("evaluate", 8000), ("wait_for visible", 8000), ("click", 2000, True)])
# And the third is deliberately not the same number as the other two. Those
# happen with the acting flag down and cost only time; this one happens with it
# up, and everything it covers is a tab attributed to the run.
check("and the one under the acting flag is the short one",
      (hit.bounded[-1][1], min(ms for _, ms in hit.bounded[:-1])),
      (browser.CLICK_MS, 8000))
# What makes that number a dispatch budget rather than two budgets in one
# name. Without the flag playwright waits the click's own navigation out
# inside the click's timeout, so `CLICK_MS` would be the page's response
# budget too — and a page slower than a dispatch is generous comes back as a
# click that never landed.
check("and it is a dispatch budget, because the click does not wait after "
      "itself", hit.bounded[-1][2], True)
check("what waits instead is a longer one, spent outside the flag",
      (browser.CLICK_NAV_MS > browser.CLICK_MS,
       [wait for wait in hit.waited if wait[0] == "navigation"]),
      (True, [("navigation", "commit", browser.CLICK_NAV_MS)]))
typed = FakePage(url="https://x.test/")
act(typed, {"act": "type", "idx": 4, "text": "恵比寿"})
check("a type reads no label, so it bounds the one call it makes",
      typed.bounded, [("fill", 8000)])
# What the miss costs now. The read is where the 8 s is spent and the click is
# never reached, so the run gets its note one call in rather than after two
# waits — which is also why the number above has to be on `evaluate` and not
# only on `click`.
missed = FakePage(url="https://x.test/", raises=True)
check("a miss stops at the read", acted(missed, {"act": "click", "idx": 42})[0],
      "failed: click 42 did not land in time")
check("and never reaches the click it was supposed to bound",
      missed.bounded, [("evaluate", 8000)])
# The fake takes `timeout` keyword-only because the real one does. Passed
# positionally it would bind to `arg` — playwright would take it as the value
# to hand the JS, raise nothing, and wait the full 30 s anyway.
from playwright.async_api import Locator as PWLocator  # noqa: E402

check("timeout is keyword-only on the real Locator.evaluate",
      inspect.signature(PWLocator.evaluate).parameters["timeout"].kind.name,
      "KEYWORD_ONLY")
check("as it is on the stand-in",
      inspect.signature(FakeLocator.evaluate)
      .parameters["timeout"].kind.name, "KEYWORD_ONLY")

print("\n== act: the settle ==")
page = FakePage(url="https://x.test/")
note, settled = act(page, {"act": "press", "key": "Enter"})
check("a press settles like anything else", note, "ok")
check("and it is reported in milliseconds", isinstance(settled, int), True)
# The order is the whole of it. `wait_for_load_state` needs the navigation
# committed before it is called, so the short floor comes first; put them the
# other way round and a click that navigates settles against the page it just
# left. The waits are asked of the page rather than slept in Python, which is
# what makes them visible here at all.
#
# This page navigates nowhere, so the navigation wait runs out and the floor is
# still paid in full — the cheap backstop, unchanged, for a press that changed
# nothing.
check("the floor is waited first, then the network",
      page.waited,
      [("navigation", "commit", browser.SETTLE_MS),
       ("press", "Enter"),
       ("timeout", browser.MIN_SETTLE_MS),
       ("load_state", "networkidle",
        browser.SETTLE_MS - browser.MIN_SETTLE_MS)])
# `SETTLE_MS` is a ceiling on the pair, not on the second half. The obvious
# spelling passes `SETTLE_MS` straight through, which makes the worst case
# 2.2 s — a tenth *worse* than the unconditional sleep it replaces, on exactly
# the pages where `networkidle` never fires and the worst case is the case.
check("and the two together may not exceed the ceiling",
      browser.MIN_SETTLE_MS + (browser.SETTLE_MS - browser.MIN_SETTLE_MS),
      browser.SETTLE_MS)
# A page that never goes quiet must not take the step down with it. This is the
# common case, not the edge: `networkidle` is DISCOURAGED and a page with an
# open socket never reaches it.
quiet = FakePage(url="https://x.test/", idle_timeout=True)
check("a page that never goes quiet still settles, and says so",
      act(quiet, {"act": "press", "key": "Enter"})[0], "ok")
check("and it waited for both", len(quiet.waited), 4)

print("\n== act: a press waits for the document it commits ==")
# The floor is a duration where what is wanted is an event. `press` is not an
# auto-waiting API, so a form submitted with Enter has not committed anything by
# the time the key returns — and `networkidle` is still in the frame's
# `_load_states` from the *outgoing* document, so the settle's second half
# short-circuits and the next step screenshots the form that was just sent.
# 400 ms is the server in the report: slower than the floor, faster than the
# ceiling.
submit = FakePage(url="https://x.test/form", commits="https://x.test/results",
                  nav_ms=400)
note, settled = act(submit, {"act": "press", "key": "Enter"})
check("a press that submits a form is still an ordinary ok", note, "ok")
check("the key is delivered inside a wait for the commit, not before one",
      submit.waited[0], ("navigation", "commit", browser.SETTLE_MS))
# The one that says it. Everything the settle waits for comes *after* the
# commit, so there is no longer a moment where the settle can answer about the
# page the press left.
check("and nothing is settled until the new document is there",
      [w[0] for w in submit.waited],
      ["navigation", "press", "committed", "load_state"])
check("so the page the next step screenshots is the one that answered",
      submit.url, "https://x.test/results")
check("the key itself still went through exactly once", submit.did,
      ["press Enter"])
# What is left of the ceiling, not a fresh one. The commit already spent 400 ms
# of the step's budget; handing the network wait the old constant would make the
# step cost 2.2 s.
network = [w for w in submit.waited if w[0] == "load_state"][0]
check("the network wait gets what is left of the ceiling",
      0 < network[2] <= browser.SETTLE_MS - 400, True)
check("which is less than the fixed budget it used to get",
      network[2] < browser.SETTLE_MS - browser.MIN_SETTLE_MS, True)
# The floor is a guess at how long a commit takes; this press watched one
# happen, so there is nothing left for it to guess at.
check("and the floor is skipped, having already been more than paid",
      [w for w in submit.waited if w[0] == "timeout"], [])
# The returned number is what the bench reads, and until now nothing pinned it:
# `return 0` left every case green because a stand-in page's waits are all
# instantaneous. This one sleeps, so the number has to be real.
check("the milliseconds it reports are the ones it actually spent",
      f"{400 <= settled <= 900} at {settled // 100 * 100} ms",
      "True at 400 ms")

print("\n== _settle: the ceiling is the step's, not the function's ==")
# Straight at the arithmetic, because the paths above only reach two of its
# corners. `spent_ms` is what the action already spent watching its own
# navigation commit.


def settle_waits(spent_ms: int) -> list:
    """What `_settle` asks the page to wait for, given that much already gone."""
    page = FakePage(url="https://x.test/", idle_timeout=True)
    session = browser._Session()
    session.page = page
    asyncio.run(session._settle(spent_ms))
    return page.waited


check("nothing spent, so the floor and the rest of the ceiling", settle_waits(0),
      [("timeout", 200), ("load_state", "networkidle", 1800)])
check("part of the floor spent, so the rest of it", settle_waits(50),
      [("timeout", 150), ("load_state", "networkidle", 1800)])
check("more than the floor, so none of it", settle_waits(400),
      [("load_state", "networkidle", 1600)])
check("nearly all of the ceiling, so a tenth of a second is what is left",
      settle_waits(1900), [("load_state", "networkidle", 100)])
check("all of it, so nothing at all", settle_waits(2000), [])
check("and past it, without asking to wait a negative number of ms",
      settle_waits(3000), [])
# The property the two halves exist to keep, over every split above at once. A
# page that never goes quiet is the case that has to hold: it is the common one,
# `networkidle` being DISCOURAGED, and it is the one where the worst case is the
# case.
totals = {ms: ms + sum(w[-1] for w in settle_waits(ms))
          for ms in (0, 50, 400, 1900, 2000, 3000)}
check("however the split falls, a step still costs the ceiling at worst", totals,
      {0: 2000, 50: 2000, 400: 2000, 1900: 2000, 2000: 2000, 3000: 3000})

print("\n== _thinking: off, in the spelling the model takes ==")
# Both spellings are a 400 on the wrong model family, and the wrong one is a
# 400 on every step of every run. Measured against the live endpoint: 3.8 Flash
# rejects `MINIMAL` outright and accepts `thinking_budget=0` without honouring
# it (0, 189, 0 thought tokens over three identical calls); 2.5 rejects
# `thinking_level` and honours the budget every time.
with env(BROWSE_MODEL=None):
    check("the default is 3.8 Flash", browser._model(), "gemini-3.8-flash")
    check("and 3.x asks for a level, not a budget",
          browser._thinking().thinking_level, browser.types.ThinkingLevel.LOW)
    check("with no budget beside it",
          browser._thinking().thinking_budget, None)
with env(BROWSE_MODEL="gemini-2.5-flash"):
    check("2.x asks for a budget", browser._thinking().thinking_budget, 0)
    check("with no level beside it",
          browser._thinking().thinking_level, None)
with env(BROWSE_MODEL="gemini-2.5-flash-lite"):
    check("and the minor version is not the one being read",
          browser._thinking().thinking_budget, 0)
# The test is on the major version rather than on a list of names, so a model
# that does not exist yet does not need this file edited to work.
with env(BROWSE_MODEL="gemini-4-flash"):
    check("a later major version reads as 3.x does",
          browser._thinking().thinking_level, browser.types.ThinkingLevel.LOW)
with env(BROWSE_MODEL="some-other-model"):
    check("and something that is not a Gemini falls back to the budget",
          browser._thinking().thinking_budget, 0)

print("\n== _decide_sync: what the request is actually built out of ==")
with env(BROWSE_MODEL=None), decide() as call:
    check("the reply comes back parsed",
          browser._decide_sync("find the price", "ja", b"\xff\xd8\xff",
                               "1 a", []),
          {"act": "answer", "text": "42"})
    check("and it named the configured model",
          call.sent["model"], "gemini-3.8-flash")
    check("thinking is off in the request, not only in the helper",
          call.sent["config"].thinking_config.thinking_level,
          browser.types.ThinkingLevel.LOW)
    check("the reply is asked for as JSON",
          call.sent["config"].response_mime_type, "application/json")
    check("against the step schema",
          call.sent["config"].response_schema, browser.SCHEMA)
    check("and the language is folded into the instruction",
          browser.answer_in("ja") in call.sent["config"].system_instruction,
          True)
    check("a screenshot goes as an image part",
          call.sent["contents"][1].inline_data.mime_type, "image/jpeg")
    check("with the element list beside it",
          call.sent["contents"][2], "Elements you can act on:\n1 a")
    check("and an empty history reads as nothing",
          call.sent["contents"][0],
          "Task: find the price\nDone so far: nothing")

with env(BROWSE_MODEL="gemini-2.5-flash"), decide() as call:
    browser._decide_sync("t", "", b"\xff\xd8\xff", "", [])
    check("on 2.x the request carries a budget instead",
          call.sent["config"].thinking_config.thinking_budget, 0)

with decide() as call:
    browser._decide_sync("t", "", None, "1 a", ["clicked a", "typed b"],
                         blank=True)
    check("a blank page is described rather than shown",
          call.sent["contents"][1],
          "The browser is on a blank page. Navigate somewhere first.")
    check("and the element list is left off with it",
          len(call.sent["contents"]), 2)
    check("history is replayed in order",
          call.sent["contents"][0], "Task: t\nDone so far: clicked a then typed b")

# The model is asked for JSON against a schema and still gets to answer with
# something else. That used to end the run with a `JSONDecodeError` out of the
# executor; an empty answer at least gets the card its "I could not find that".
with decide("sorry, no") as call:
    check("a reply that is not JSON is an empty answer, not a crash",
          browser._decide_sync("t", "", None, "", [], blank=True),
          {"act": "answer", "text": ""})
with decide(None) as call:
    check("and neither is no reply at all",
          browser._decide_sync("t", "", None, "", [], blank=True),
          {"act": "answer", "text": ""})

with decide() as call:
    browser._decide_sync("t", "", b"\xff\xd8\xff", "1 a", [], "",
                         "- heading \"Departures\"")
    check("a tree is a fourth part, after the elements",
          len(call.sent["contents"]), 4)
    check("carrying what the snapshot said",
          "- heading \"Departures\"" in call.sent["contents"][3], True)
    # Without this the model answers `{"act": "click", "idx": 3}` off the
    # tree's third line, and clicks whatever the element list numbered 3.
    check("and warning that its lines are not indices",
          "not a list of indices" in call.sent["contents"][3], True)
with decide() as call:
    browser._decide_sync("t", "", b"\xff\xd8\xff", "1 a", [])
    check("no tree is no extra part", len(call.sent["contents"]), 3)

print("\n== _decide_sync: no picture is not a blank page ==")
# The two used to be one state — `shot is None` meant blank, because the only
# way to get here without bytes was a page nobody had navigated to yet. Now
# `look` can come back without a picture from a page that is fully loaded, and
# the difference decides what the model does next: told it is blank, it
# navigates away from the page it was in the middle of reading.
with decide() as call:
    browser._decide_sync("t", "", None, "1 a", [])
    check("a page that would not photograph is still the page you are on",
          "would not give up a screenshot" in call.sent["contents"][1], True)
    check("and the element list goes with it, unlike a blank page's",
          call.sent["contents"][2], "Elements you can act on:\n1 a")
    check("and the model is told in as many words not to leave over it",
          "Do not navigate away" in call.sent["contents"][1], True)
with decide() as call:
    browser._decide_sync("t", "", None, "1 a", [], blank=True)
    check("while a blank page says so, and still says only that",
          [call.sent["contents"][1], len(call.sent["contents"])],
          ["The browser is on a blank page. Navigate somewhere first.", 2])

print("\n== _Session.look: a page that will not photograph ==")


class TextPage(FakePage):
    """A `FakePage` that answers `evaluate` too, which `look` needs.

    Two calls with one method: `look` asks for the element list with an
    argument and the page text without one, so the argument is what tells
    them apart.

    `shot_raises` reaches this the way it reaches `screenshot`, with one
    difference that is the whole point of the shotless branch: a page that
    will not photograph still answers JavaScript, so `"timeout"` is deaf to
    the camera alone. A page that is *gone* answers nothing, so `"closed"`
    raises here too. A fake that runs JavaScript on a closed page cannot tell
    anybody whether a closed page is handled.
    """

    async def evaluate(self, _js, arg=None):
        if self._shot_raises and self._shot_raises != "timeout":
            raise browser._page_error()(
                "Error: Target page, context or browser has been closed")
        return "0. <a> next" if arg is not None else "a page"


page = TextPage(url="https://example.com/")
check("an ordinary page comes back with all three",
      asyncio.run(browser._Session.look(SimpleNamespace(page=page))),
      (FINAL_SHOT, "0. <a> next", "a page"))
# Playwright's default is 30 s and the budget is 120, so four steps on a page
# like this is the whole run spent waiting.
check("and its screenshot is bounded rather than left at the default",
      page.shots, [browser.SHOT_MS])

page = TextPage(url="https://example.com/", shot_raises="timeout")
with logged() as lines:
    out = asyncio.run(browser._Session.look(SimpleNamespace(page=page)))
check("one that will not give the picture up costs the picture and no more",
      out, (None, "0. <a> next", "a page"))
check("and it is in the log, because nothing else on screen will say it",
      any("would not give up a screenshot" in line for line in lines), True)
# Not only the timeout, and not the same answer either. A page that crashed
# answers nothing at all — `evaluate` raises the same `Error` the screenshot
# did — so there is no reading to come back with and `look` says so. What
# stops that being a traceback out of the whole job is the guard in the loop,
# two groups below.
page = TextPage(url="https://example.com/", shot_raises="closed")
check("a page that is gone has nothing to read, so `look` raises rather "
      "than pretending to a reading",
      raised(lambda: asyncio.run(
          browser._Session.look(SimpleNamespace(page=page)))),
      "Error: Error: Target page, context or browser has been closed")

# And the reads after it, which is the half a bounded screenshot does not
# cover. `evaluate` takes no timeout and playwright applies none, so a page
# that has started a navigation the far end never finished does not answer it
# at all: measured against a real Chromium, the screenshot gave up at SHOT_MS
# and `evaluate` was still waiting at 90 s. `_settle` swallows its own
# `networkidle` timeout, so the loop reaches `look` in that state, and the run
# budget is read between steps and never during one — so an unbounded read
# here is not a slow step, it is a browse that never comes back.


class DeafPage(TextPage):
    """Gives up the screenshot and then stops answering altogether."""

    async def evaluate(self, _js, arg=None):
        await asyncio.sleep(3600)


def looked(page, cap=2.0):
    """What `look` gave up with, under a cap of the case's own.

    The cap is the harness and not the thing being measured: without a bound
    inside `look` this call does not return, and a case that hangs reports
    nothing at all. `asyncio.TimeoutError` is the builtin `TimeoutError` on
    3.11 and up, so an outer cap that fires arrives here as an empty message
    and the case fails on the difference rather than on the wait.
    """
    async def under_cap():
        return await asyncio.wait_for(
            browser._Session.look(SimpleNamespace(page=page)), cap)

    try:
        asyncio.run(under_cap())
        return "came back"
    except TimeoutError as err:
        return str(err)


was, browser.READ_S = browser.READ_S, 0.2
began = time.monotonic()
check("a page that answers nothing gives up rather than waiting for ever",
      looked(DeafPage(url="https://example.com/")),
      "the page did not answer in 0.2s")
check("and on its own clock, since playwright brings none to this call",
      time.monotonic() - began < 1, True)


class HalfDeafPage(TextPage):
    """Answers the element list and then stops, which is the second read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.answered = 0

    async def evaluate(self, _js, arg=None):
        self.answered += 1
        if self.answered > 1:
            await asyncio.sleep(3600)
        return "0. <a> next"


page = HalfDeafPage(url="https://example.com/")
check("and the text read is bounded too, not only the element list",
      (looked(page), page.answered), ("the page did not answer in 0.2s", 2))
browser.READ_S = was
check("the shipped bound is the screenshot's, counted in seconds",
      browser.READ_S, browser.SHOT_MS / 1000)

print("\n== _why: the context, and the two ways to misread one ==")
check("no context is no heading at all", browser._why(""), "")
check("and neither is one the model padded with whitespace",
      browser._why("  \n "), "")
filled = browser._why("They are packing for Portland.")
check("a real one is quoted under a heading",
      "Why they are asking:\nThey are packing for Portland." in filled, True)
# Both guards travel with the context rather than living in SYSTEM, so a run
# with no context does not spend tokens warning about one.
check("the do-not-invent guard rides with it",
      "never a source of facts" in filled, True)
check("and the do-not-over-filter guard too",
      "most valuable thing on the page" in filled, True)
check("neither is in the standing instruction on its own",
      "never a source of facts" in browser.SYSTEM, False)

with decide() as call:
    browser._decide_sync("t", "", None, "", [],
                         "They are packing for Portland.", blank=True)
    check("the context reaches the request",
          "They are packing for Portland."
          in call.sent["config"].system_instruction, True)
    check("appended to the standing instruction, not instead of it",
          call.sent["config"].system_instruction.startswith(browser.SYSTEM),
          True)
with decide() as call:
    browser._decide_sync("t", "ja", None, "", [], blank=True)
    check("and no context leaves the instruction as it was",
          call.sent["config"].system_instruction,
          browser.SYSTEM + " " + browser.answer_in("ja"))

print("\n== run: the loop, without a browser under it ==")
session = FakeSession()
frames: list[dict] = []


async def collect(frame):
    frames.append(frame)


with decide(['{"act": "click", "idx": 0}',
             '{"act": "answer", "text": "2023"}']) as call, sessions(session):
    out = asyncio.run(browser.run("when was it released?", "en",
                                  context="They are setting up a Pi 5.",
                                  sink=collect))
check("the answer comes back out of the loop", out["answer"], "2023")
check("with the step it took on the way", len(out["steps"]), 1)
check("and the page it was read off", out["url"], "https://example.com/")
check("the step the model asked for is the step that was acted on",
      session.acted, [{"act": "click", "idx": 0}])
# The threading `_decide_sync`'s own cases cannot see: they call it directly,
# and `run` is what has to hand the context down on every step.
check("the context reaches every decision, not only the first",
      ["They are setting up a Pi 5." in c["config"].system_instruction
       for c in call.calls], [True, True])
check("and what the last step did reaches the next one",
      '{"act": "click", "idx": 0}' in call.calls[1]["contents"][0], True)
check("the screen was told about the step and the answer",
      [f.get("phase") for f in frames], ["step", "done"])

with env(BROWSE_MAX_STEPS="3"), decide(['{"act": "press", "key": "Enter"}'] * 3):
    with sessions(FakeSession()):
        out = asyncio.run(browser.run("t"))
check("a run that never answers stops at the step cap", len(out["steps"]), 3)
check("and says so rather than returning nothing",
      out["answer"], "I could not find that.")

# A page that will not photograph, all the way through the loop. This used to
# end the run: `look` raised, nothing in `run` caught it, and `browse` turned
# the whole thing into "The browser could not read that: Page.screenshot:
# Timeout 30000ms exceeded." — every step taken so far thrown away with it.
shotless = FakeSession(shotless=True)
blind: list[dict] = []


async def collect_blind(frame):
    blind.append(frame)


with decide(['{"act": "click", "idx": 0}',
             '{"act": "answer", "text": "2023"}']) as call, sessions(shotless):
    out = asyncio.run(browser.run("when was it released?",
                                  sink=collect_blind))
check("a page with no picture in it is read anyway", out["answer"], "2023")
check("and it is a step like any other, not a run that ended",
      [len(out["steps"]), shotless.looked], [1, 2])
check("the model is told what it is missing, on the step it is missing it",
      "would not give up a screenshot" in call.calls[0]["contents"][1], True)
check("and the next step is told it happened, which reads the history",
      "would not give up a screenshot" in call.calls[1]["contents"][0], True)
# `_data_uri(None)` is the empty string, so the card carries no picture for
# that step rather than the one before it — the frame says what was seen.
check("the card is honest about having nothing to show for it",
      [f.get("shot") for f in blind if f.get("phase") == "step"], [""])

print("\n== run: a tab that dies between one step and the next ==")
# The other half of the same failure, and the likelier one on a Pi. A page
# that will not photograph is read anyway; a page that is gone answers
# nothing, `look` raises, and until the guard landed that raise left `run`
# entirely and `browse.py` turned the whole job into "The browser could not
# read that: Target page, context or browser has been closed" — with the step
# it had already taken thrown away.
dead = FakeSession(dies_on=2)
with decide(['{"act": "click", "idx": 0}',
             '{"act": "answer", "text": "2023"}']), sessions(dead):
    out = asyncio.run(browser.run("when was it released?"))
check("a tab that dies mid-run ends the run rather than raising out of it",
      [len(out["steps"]), dead.looked], [1, 2])
check("and the step it took before that is still in the answer",
      [out["steps"][0]["act"], out["answer"]],
      ["click", "I could not find that."])
# `shot` is bound before the loop and only ever reassigned, so the step that
# could not be read leaves the previous one's picture standing. That is what
# the card wants — the last thing anybody actually saw — and it is only true
# because nothing resets `shot` at the top of an iteration.
check("and the card keeps the last picture that did come back",
      out["shot"].startswith("data:image/jpeg;base64,"), True)

print("\n== run: the allowlist, on the address the screenshot came from ==")


def shown(call) -> str:
    """What the model was given as the page: a screenshot, or a sentence.

    Reached for rather than indexed into, because a check that fired when it
    should not have leaves a string here, and `.inline_data` on a string is an
    AttributeError out of the middle of the file rather than a named case.
    """
    part = call.calls[0]["contents"][1]
    return getattr(getattr(part, "inline_data", None), "mime_type", part)


# A redirect slower than the settle's ceiling. `act` returns while `page.url`
# is still the outgoing document, so its own check passes on an address the
# browser is about to leave, and the new one does not become readable until
# something waits for the commit — which is `look`, and only `look`.
slow = FakeSession(url="https://shop.test/", commits="https://tracker.example/x")
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_MAX_STEPS="1"), \
        decide(['{"act": "answer", "text": "no"}']) as call:
    with sessions(slow):
        asyncio.run(browser.run("t"))
check("the page is read, because reading it is what settles the address",
      slow.looked, 1)
check("and then thrown away rather than handed to the model", shown(call),
      "The browser is on a blank page. Navigate somewhere first.")
check("with none of it left beside", len(call.calls[0]["contents"]), 2)
check("the browser is sent back to blank", slow.page.did, ["goto about:blank"])
check("and the model is told why, in act's own words",
      "left the allowlist and was sent back (https://tracker.example/x)"
      in call.calls[0]["contents"][0], True)

# The same redirect landing somewhere the allowlist names. The check has to be
# quiet then, or a site that answers on its www host becomes unreadable.
inside = FakeSession(url="https://shop.test/",
                     commits="https://www.shop.test/hours")
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_MAX_STEPS="1"), \
        decide(['{"act": "answer", "text": "no"}']) as call:
    with sessions(inside):
        asyncio.run(browser.run("t"))
check("a redirect that stays inside the allowlist is shown as usual",
      shown(call), "image/jpeg")
check("with the browser left where it landed", inside.page.did, [])
check("and nothing said about being sent back",
      "sent back" in call.calls[0]["contents"][0], False)

# The shipped configuration, where `_allowlist` is empty and everything is in
# bounds. A check that fired here would take the browser off the one page it
# was sent to find.
anywhere = FakeSession(url="https://shop.test/",
                       commits="https://tracker.example/x")
with env(BROWSE_ALLOWLIST=None, BROWSE_MAX_STEPS="1"), \
        decide(['{"act": "answer", "text": "no"}']) as call:
    with sessions(anywhere):
        asyncio.run(browser.run("t"))
check("and with no allowlist configured it never fires at all",
      (anywhere.page.did, shown(call)), ([], "image/jpeg"))

# `act` catching a fast redirect one step sooner leaves the page on
# `about:blank`, which is never read at all — so the two cannot both report the
# same redirect, and no deduplication is needed between them.
returned = FakeSession(url="about:blank")
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_MAX_STEPS="1"), \
        decide(['{"act": "answer", "text": "no"}']):
    with sessions(returned):
        asyncio.run(browser.run("t"))
check("a page act already sent back is not read or sent back again",
      (returned.page.did, returned.looked), ([], 0))

# A page that navigates itself to `about:blank` while it is being read.
# `_allowed` says no to it, having no http scheme, but it is not somewhere the
# browser needs sending back from and calling it an escape would be a lie.
away = FakeSession(url="https://shop.test/", commits="about:blank")
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_MAX_STEPS="1"), \
        decide(['{"act": "answer", "text": "no"}']) as call:
    with sessions(away):
        asyncio.run(browser.run("t"))
check("and a page that goes blank on its own is not reported as an escape",
      (away.page.did, "sent back" in call.calls[0]["contents"][0]),
      ([], False))

# The two states the suite covered one at a time: a page that will not
# photograph, whose redirect off the allowlist commits inside the same `look`.
# Both of them write to `history`, and the second throws away the page the
# first says was read — so the history has to describe one event, not two.
thrown = FakeSession(url="https://shop.test/", commits="https://evil.test/",
                     shotless=True)
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_MAX_STEPS="2"), \
        decide(['{"act": "press", "key": "Enter"}',
                '{"act": "answer", "text": "no"}']) as call:
    with sessions(thrown):
        asyncio.run(browser.run("t"))
# Whole line rather than a substring, because the failure is two true-sounding
# halves next to each other and a substring test passes on either one alone.
check("a page thrown away for leaving the allowlist is not also reported read",
      call.calls[0]["contents"][0].split("Done so far: ")[1],
      "left the allowlist and was sent back (https://evil.test/)")
check("and the step after it is not told it either, which reads the history",
      call.calls[1]["contents"][0].split("Done so far: ")[1],
      "left the allowlist and was sent back (https://evil.test/) then "
      '{"act": "press", "key": "Enter"}')
check("the half that did happen is kept, and the page still withheld",
      (thrown.page.did, shown(call)),
      (["goto about:blank"],
       "The browser is on a blank page. Navigate somewhere first."))

# And the same unphotographable page redirecting somewhere the allowlist names,
# which is the state the note exists for. Dropping it here would cost the next
# step the one thing it needs to know about the step before.
kept = FakeSession(url="https://shop.test/", commits="https://www.shop.test/x",
                   shotless=True)
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_MAX_STEPS="2"), \
        decide(['{"act": "press", "key": "Enter"}',
                '{"act": "answer", "text": "no"}']) as call:
    with sessions(kept):
        asyncio.run(browser.run("t"))
check("a page that was read and kept still says it had no picture",
      (call.calls[1]["contents"][0].split("Done so far: ")[1], kept.page.did),
      ("the page would not give up a screenshot; read it from the element "
       'list instead then {"act": "press", "key": "Enter"} then the page '
       "would not give up a screenshot; read it from the element list "
       "instead", []))

# The rest of what the thrown-away read is still holding. The prompt ignores
# `elements` once `blank` is set, so the reset looks redundant there — but
# `text` is read one branch below by the bot-check guard, which ends the run
# and writes the answer. A wall on a page already sent back would answer for a
# page the model was never shown.
walled = FakeSession(url="https://shop.test/", commits="https://evil.test/",
                     text="Just a moment...")
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_MAX_STEPS="1"), \
        decide(['{"act": "answer", "text": "open till six"}']):
    with sessions(walled):
        out = asyncio.run(browser.run("t"))
check("and the text of a page sent back cannot end the run as a bot check",
      out["answer"], "open till six")

print("\n== run: the allowlist, on the address the run ends on ==")

# The exit the loop's check cannot cover. It runs at the top of a turn, and the
# model answering on the last step leaves no turn for it to run in — so the
# redirect that step kicked off commits on the screenshot `run` signs off with,
# which is the one that goes on the card and whose address goes to the model.
late = FakeSession(url="https://shop.test/", ends_on="https://tracker.example/x")
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_MAX_STEPS="2"), \
        logged() as lines, \
        decide(['{"act": "press", "key": "Enter"}',
                '{"act": "answer", "text": "open till six"}']):
    with sessions(late):
        out = asyncio.run(browser.run("t", sink=collect))
check("the address the run ended on is not handed to the model",
      out["url"], "")
# The last page that was on the allowlist, rather than the one it left for and
# rather than nothing: the answer was read off it, and it is the whole reason
# there is an image on the card.
check("and the card keeps the page the answer came from", out["shot"],
      browser._data_uri(LOOK_SHOT))
check("the answer, read off the page that was in bounds, is kept",
      out["answer"], "open till six")
check("and the escape is logged, since no turn is left to say it in",
      any("ended on https://tracker.example/x" in line for line in lines), True)
check("the card is told the same as the model",
      (frames[-1]["url"], frames[-1]["shot"]), (out["url"], out["shot"]))

# The other exit without a next turn. The budget breaks at the top of a turn
# and before `look`, so nothing was read on this one and there is no earlier
# screenshot to fall back on — the card gets none rather than one of a page the
# browser was not allowed to be on.
spent = FakeSession(url="https://shop.test/", ends_on="https://tracker.example/x")
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_BUDGET_S="0"), \
        decide(['{"act": "answer", "text": "no"}']):
    with sessions(spent):
        out = asyncio.run(browser.run("t"))
check("a run that ran out of time drops it too",
      (out["url"], out["shot"]), ("", ""))
check("and still says what became of it",
      out["answer"], "I ran out of time looking that up.")

# A run ending on a redirect the allowlist names. The fresh screenshot is the
# point of taking one at all — it is the page the answer was read off — so a
# check that fired here would cost every run its audit image.
home = FakeSession(url="https://shop.test/", ends_on="https://www.shop.test/x")
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_MAX_STEPS="1"), \
        decide(['{"act": "press", "key": "Enter"}']):
    with sessions(home):
        out = asyncio.run(browser.run("t"))
check("one that stays inside the allowlist is signed off as usual",
      (out["url"], out["shot"]),
      ("https://www.shop.test/x", browser._data_uri(FINAL_SHOT)))

# And the shipped configuration, where the allowlist is empty and the check
# has no business firing at all.
open_ = FakeSession(url="https://shop.test/", ends_on="https://tracker.example/x")
with env(BROWSE_ALLOWLIST=None, BROWSE_MAX_STEPS="1"), \
        decide(['{"act": "press", "key": "Enter"}']):
    with sessions(open_):
        out = asyncio.run(browser.run("t"))
check("and with no allowlist configured it never fires",
      (out["url"], out["shot"]),
      ("https://tracker.example/x", browser._data_uri(FINAL_SHOT)))

print("\n== run: the screenshot it signs off with is not the answer ==")
# That screenshot auto-waits for the pending commit the section above is
# written around, which is the whole reason it can be slow. Left at
# playwright's default it waits 30 s for a page that navigated into a request
# the far end accepted and never answered, and then raises — out of the loop,
# out of `run`, into `tools/browse.py`'s `except`, which says "The browser
# could not read that". The answer the model had already written is thrown
# away with it, and the person is told the page was unreadable when what
# actually failed was the photograph of it. So: bounded, and caught.
paid = FakeSession(url="https://shop.test/")
with env(BROWSE_ALLOWLIST=None, BROWSE_MAX_STEPS="1"), \
        decide(['{"act": "press", "key": "Enter"}']):
    with sessions(paid):
        asyncio.run(browser.run("t"))
check("the sign-off screenshot is bounded, like act's own reads",
      paid.page.shots, [browser.SHOT_MS])
check("and by act's 8 s rather than playwright's 30",
      browser.SHOT_MS, 8000)

lost = FakeSession(url="https://shop.test/", shot_raises="timeout")
with env(BROWSE_ALLOWLIST=None, BROWSE_MAX_STEPS="2"), \
        logged() as lines, \
        decide(['{"act": "press", "key": "Enter"}',
                '{"act": "answer", "text": "open till six"}']):
    with sessions(lost):
        out = asyncio.run(browser.run("t", sink=collect))
check("one that runs out of time does not cost the run its answer",
      out["answer"], "open till six")
# The last screenshot that did come back, which is the page the answer was
# read off — the same fallback a run ending off the allowlist already takes.
check("the card keeps the last screenshot that did come back",
      out["shot"], browser._data_uri(LOOK_SHOT))
check("and the address, which was in bounds when it was read",
      out["url"], "https://shop.test/")
check("and the image nobody got is logged rather than raised",
      any("would not give up a screenshot" in line for line in lines), True)
check("the card is told the same as the caller",
      (frames[-1]["answer"], frames[-1]["shot"]),
      (out["answer"], out["shot"]))

# The other way it fails, and the commoner one: a page that closed or crashed
# under the run raises playwright's plain `Error`, not a `TimeoutError`.
# Catching only the timeout would leave this one going straight back out.
dead = FakeSession(url="https://shop.test/", shot_raises="closed")
with env(BROWSE_ALLOWLIST=None, BROWSE_MAX_STEPS="1"), \
        decide(['{"act": "answer", "text": "open till six"}']):
    with sessions(dead):
        out = asyncio.run(browser.run("t"))
check("nor does a page that closed under it",
      (out["answer"], out["shot"]),
      ("open till six", browser._data_uri(LOOK_SHOT)))

# And the allowlist above still has to fire. Now that the screenshot may fail,
# a check asking whether there is an image rather than where the run ended
# would hand the model the address of a page it was never allowed on, every
# time that page was too broken to photograph.
gone = FakeSession(url="https://shop.test/", shot_raises="timeout",
                   ends_on="https://tracker.example/x")
with env(BROWSE_ALLOWLIST="shop.test", BROWSE_MAX_STEPS="2"), \
        decide(['{"act": "press", "key": "Enter"}',
                '{"act": "answer", "text": "open till six"}']):
    with sessions(gone):
        out = asyncio.run(browser.run("t"))
check("an escape is still dropped when the screenshot of it failed",
      (out["url"], out["answer"]), ("", "open till six"))

print("\n== run: a run that is getting nowhere is shown more ==")
# Four steps that all fail. Steps 1 and 2 are on their own; the tree arrives
# for step 3 and stays for step 4, because nothing has worked yet.
stuck = FakeSession(notes=["refused: no", "not found", "not found", "ok"])
with env(BROWSE_MAX_STEPS="4"), decide(['{"act": "click", "idx": 0}'] * 4) as call:
    with sessions(stuck):
        asyncio.run(browser.run("t"))
check("two failures in a row is what it takes", stuck.asked, 2)
check("nothing extra goes with the first two steps",
      [len(c["contents"]) for c in call.calls[:2]], [3, 3])
check("the third and fourth get the tree",
      [len(c["contents"]) for c in call.calls[2:]], [4, 4])

# The same four steps with the third one working. The counter is consecutive,
# so the success clears it and the fourth is back to a screenshot.
recovers = FakeSession(notes=["not found", "not found", "ok", "not found"])
with env(BROWSE_MAX_STEPS="4"), decide(['{"act": "click", "idx": 0}'] * 4) as call:
    with sessions(recovers):
        asyncio.run(browser.run("t"))
check("a step that works resets the count", recovers.asked, 1)
check("and the step after it is back to the screenshot alone",
      len(call.calls[3]["contents"]), 3)

fine = FakeSession()
with env(BROWSE_MAX_STEPS="3"), decide(['{"act": "click", "idx": 0}'] * 3):
    with sessions(fine):
        asyncio.run(browser.run("t"))
check("a run where everything works never asks for one", fine.asked, 0)

# A run that has not navigated anywhere is stuck on nothing: there is no page
# to take a tree of, and the model is already being told to go somewhere.
blank = FakeSession(url="about:blank", notes=["refused: no"] * 3)
with env(BROWSE_MAX_STEPS="3"), decide(['{"act": "goto", "url": "x"}'] * 3):
    with sessions(blank):
        asyncio.run(browser.run("t"))
check("and neither does one still on a blank page", blank.asked, 0)

full = FakeSession(elements="\n".join(f"{i}. <a> link"
                                      for i in range(browser.MAX_ELEMENTS)))
with env(BROWSE_MAX_STEPS="2"), decide(['{"act": "click", "idx": 0}'] * 2):
    with sessions(full), logged() as lines:
        asyncio.run(browser.run("t"))
check("a full element list says so in the log",
      any("element list is full" in line for line in lines), True)
with env(BROWSE_MAX_STEPS="2"), decide(['{"act": "click", "idx": 0}'] * 2):
    with sessions(FakeSession()), logged() as lines:
        asyncio.run(browser.run("t"))
check("a short one does not", any("element list is full" in line
                                  for line in lines), False)

print("\n== _Session.tree: the fallback view, bounded ==")


def tree_of(page):
    session = browser._Session()
    session.page = page
    return asyncio.run(session.tree())


page = FakePage(snapshot="- heading \"Departures\"")
check("the snapshot comes back as it is", tree_of(page),
      "- heading \"Departures\"")
check("asked for at the configured depth",
      page.snapshots[0].get("depth"), browser.SNAPSHOT_DEPTH)
check("and with a timeout, so a wedged page is not the end of the run",
      page.snapshots[0].get("timeout"), 5000)
# Not `mode="ai"`: that spelling is Playwright's own agent format, which
# renumbers refs the model would then use instead of `data-agent-idx`, and
# measures nearly twice the characters on the same page.
check("in the default format, not the ai one",
      "mode" in page.snapshots[0], False)
check("a tree longer than the budget is cut to it",
      len(tree_of(FakePage(snapshot="x" * 40000))), browser.SNAPSHOT_CHARS)
check("a snapshot that will not render is no tree, not a crash",
      tree_of(FakePage(raises="snapshot")), "")

print("\n== _Session: nothing is left running ==")


class FakeDriver:
    """`async_playwright()`, with a Chromium that may refuse to launch."""

    def __init__(self, log, launches=True):
        self.log = log
        self._launches = launches
        # What `launch` was called with, kept rather than dropped: the flags
        # are the whole of the browser's configuration and nothing else in the
        # tree can be asked what they were.
        self.launched: dict | None = None
        self.chromium = SimpleNamespace(launch=self._launch)

    def __call__(self):
        return self

    async def start(self):
        self.log.append("start")
        return self

    async def stop(self):
        self.log.append("stop")

    async def _launch(self, **kwargs):
        self.launched = kwargs
        if not self._launches:
            raise RuntimeError("no arm64 build; set BROWSE_CHROMIUM")
        self.log.append("launch")
        return SimpleNamespace(
            close=self._close,
            new_page=lambda viewport=None: _resolved(FakePage()))

    async def _close(self):
        self.log.append("close")
        if self._launches == "close raises":
            raise RuntimeError("chromium went away")
        if self._launches == "close cancelled":
            # What the socket dropping mid-browse does: `close_session`
            # cancels the job, and a real Chromium is hundreds of ms inside
            # this call when the cancel lands.
            raise asyncio.CancelledError


async def _resolved(value):
    return value


def driven(launches=True):
    """Point `_Session` at a fake driver and hand back the driver.

    The driver rather than its log, because there are two things worth asking
    it: the order it was called in, which is `log`, and what `launch` was
    handed, which is `launched`.
    """
    import playwright.async_api as pw_api

    log: list[str] = []
    before = pw_api.async_playwright

    class _Scope:
        def __enter__(self):
            driver = FakeDriver(log, launches)
            pw_api.async_playwright = driver
            return driver

        def __exit__(self, *_):
            pw_api.async_playwright = before

    return _Scope()


async def _open_and_close(launches):
    # BROWSE_CDP cleared rather than left alone: it is the one variable that
    # sends `__aenter__` down the other branch entirely, and on the Pi it is
    # set in `.env`. Without this, every case in this section reads as a
    # broken launch on the machine the tool actually runs on.
    with driven(launches) as driver, env(BROWSE_CDP=None):
        log = driver.log
        session = browser._Session()
        try:
            async with session:
                pass
        except asyncio.CancelledError:
            # Recorded and stopped here so a case can say it got this far. In
            # the real thing it carries on up to whoever cancelled the job,
            # which is the point — the teardown runs on the way past, it does
            # not swallow the cancel.
            log.append("cancelled")
        except RuntimeError as err:
            log.append(f"raised {err.args[0][:8]}")
        # Appended to every path, not only the new one: a teardown that runs
        # the stop but keeps the handles has reaped the process and left the
        # session looking open.
        if (session._browser, session._pw, session.page) != (None,) * 3:
            log.append("handles left behind")
    return log


check("an ordinary run starts, launches, closes and stops",
      asyncio.run(_open_and_close(True)),
      ["start", "launch", "close", "stop"])
# The Pi's failure, and the one nothing used to clean up after: `__aexit__` is
# not called for a `with` that never opened, and by then `start()` has already
# spawned the node driver.
check("a launch that fails still stops the driver it started",
      asyncio.run(_open_and_close(False)),
      ["start", "stop", "raised no arm64"])
# And the other half: `close()` and `stop()` used to be one block, so a crashed
# Chromium meant the node process was never reaped.
check("a close that fails does not take the stop with it",
      asyncio.run(_open_and_close("close raises")),
      ["start", "launch", "close", "stop"])
# The other way past the stop, and the one `except Exception` cannot see:
# `CancelledError` is a `BaseException`. The window is the few hundred ms a
# real `close()` takes, and what is left behind is a node process that lives as
# long as the server — one per cancel that lands in it, on a terminal nobody
# reboots.
check("a cancel landing inside the close does not take it either",
      asyncio.run(_open_and_close("close cancelled")),
      ["start", "launch", "close", "stop", "cancelled"])

print("\n== _Session: what Chromium is launched with ==")
# Flags, and not much else, are the whole configuration of the browser this
# tool drives, and both of the ones here were paid for in a failure nobody
# would have guessed from reading the code. Pinned off the device because the
# device is not what can be wrong about them: whether the flag is *passed* is
# this file's business, whether it *helps* is the Pi's.


async def _launched(**environ):
    """Open and close a session; hand back the kwargs `launch` was given."""
    environ.setdefault("BROWSE_CDP", None)
    with driven() as driver, env(**environ):
        async with browser._Session():
            pass
    return driver.launched


args = asyncio.run(_launched(BROWSE_CHROMIUM=None))["args"]
# The keyring flag, still: without it Chromium's network stack blocks on the
# GNOME keyring and page loads hang with nothing logged.
check("the keyring flag is passed", "--password-store=basic" in args, True)
# And the one this section was added for. Measured on the Pi: without it the
# first `Page.captureScreenshot` after a launch fails nine times in ten, and
# every step opens with that screenshot, so browse never got past step one.
check("and the flag the Pi's first screenshot needs",
      "--disable-gpu" in args, True)
# Playwright ships no arm64 Linux build, so on the Pi the executable has to
# come from the environment — and on the Mac it must not, or a machine with no
# BROWSE_CHROMIUM would be told to launch nothing.
check("BROWSE_CHROMIUM is what gets launched",
      asyncio.run(_launched(BROWSE_CHROMIUM="/usr/bin/chromium"))
      .get("executable_path"), "/usr/bin/chromium")
check("and unset leaves Playwright to find its own",
      "executable_path" in asyncio.run(_launched(BROWSE_CHROMIUM=None)), False)

print("\n== _Session: attaching to the browser on the panel ==")
# The other browser this tool can drive, and the only one that is signed into
# anything: #162, over the port #144 opened. What is worth pinning here is not
# that `connect_over_cdp` gets called — it is which of the three tabs playwright
# will hand you it takes, because the two wrong ones raise nothing.
#
# `browser.new_context()` and `browser.new_page()` both work over CDP and both
# make a context with an empty cookie jar. `new_page` is the line the launch
# path uses, so the smallest possible version of this change — swap the two
# method names, touch nothing else — attaches to the panel's signed-in browser
# and then browses as a stranger. Green everywhere, no error, and the paywall
# still says no. So the cases below are about the context and about the tab,
# and there is a real Chromium behind them further down.


class FakeCdpLocator(FakeLocator):
    """A `FakeLocator` whose element can open a tab when it is used.

    Which of the two calls opens it is the point. `act` reads the label off
    the element before it clicks it, and that read is a locator call that
    waits up to 8 s on an index the DOM will not resolve — so a tab arriving
    during it belongs to whoever was at the panel, and one arriving on the
    click does not.
    """

    async def evaluate(self, js, arg=None, *, timeout=None):
        label = await super().evaluate(js, arg, timeout=timeout)
        if self._page.pops == "on the label read":
            self._page.pop()
        return label

    async def click(self, timeout=None, no_wait_after=None):
        await super().click(timeout=timeout, no_wait_after=no_wait_after)
        if self._page.pops == "on the click":
            self._page.pop()


class FakeCdpKeyboard(FakeKeyboard):
    """The same, for the one other action that can open a tab.

    Enter on a focused `target="_blank"` link opens one exactly as a click
    does, and `act` holds its window open across the press for that reason.
    Enter is no longer a key an attached run may send — `READ_KEYS` — so the
    case below drives a key that is. What is pinned is the window, not which
    key opens the tab: `READ_KEYS` is a list somebody can add to, and the day
    it grows a key that navigates, the flag has to already cover it.
    """

    async def press(self, key):
        await super().press(key)
        if self._page.pops == "on the press":
            self._page.pop()


class FakeCdpPage(FakePage):
    """A tab, writing what is done to it into the shared timeline.

    Three of these matter now and no two are interchangeable: the one this run
    opened, which it has to clean up; the one that was already there, which
    belongs to whoever is standing at the panel; and the one that appears
    mid-run, which is the run's only if the run was the one acting.

    A `FakePage` underneath, so `act` can be driven against it. That is not
    decoration: which tabs are the run's is decided inside a step and read
    back at teardown, so a stand-in that cannot take a step cannot express
    the question.
    """

    def __init__(self, log, name, pops=None, **kwargs):
        super().__init__(**kwargs)
        self.log = log
        self.name = name
        self.pops = pops
        self.viewport = None
        self.popups: list = []
        self.keyboard = FakeCdpKeyboard(self)
        self._handlers: dict = {}

    def locator(self, _selector):
        return FakeCdpLocator(self, self._label, self._raises)

    async def close(self):
        self.log.append(f"close {self.name}")
        self._closed = True

    async def bring_to_front(self):
        self.log.append(f"front {self.name}")

    async def set_viewport_size(self, viewport):
        self.viewport = viewport
        self.log.append(f"viewport {self.name}")

    async def goto(self, url, **_):
        self.log.append(f"goto {self.name}")

    async def wait_for_timeout(self, ms):
        await super().wait_for_timeout(ms)
        # Where the real one turns up. Measured against Chromium 151 over CDP,
        # four runs of five: `locator.click()` returns at 31-71 ms and the
        # popup event lands at 66-111 ms. The two spreads overlap across
        # runs, but in all 20 trials the popup landed *after* the call that
        # caused it, which is the half that matters. A window
        # that shuts when the click returns records nothing at all, which is
        # why this is the arrival the shipped cases use and "on the click" is
        # only the easy half.
        if self.pops == "in the settle":
            self.pops = None
            self.pop()
        elif self.pops == "and that tab opens one":
            # A tab our click opened opening one of its own, which is what
            # `window.open` followed by a `document.write`n script does on a
            # real page and which Chromium's user activation reaches far
            # enough to allow. The event fires on the opener and on nothing
            # else, so the second tab is seen only if the handler followed
            # onto the first.
            self.pops = None
            self.pop().pop()

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def pop(self):
        """A tab opening on this page, delivered the way playwright does.

        `page.on("popup")` and not `context.on("page")`: playwright emits the
        first only when the new page's opener is this one (1.62.0,
        ``_impl/_browser_context.py:240-244``), so the *from* half of the
        question is answered by the library and only the *by* half is left.
        """
        name = f"{self.name}-popup{len(self.popups) + 1}"
        page = FakeCdpPage(self.log, name)
        self.popups.append(page)
        self.log.append(f"opened {page.name}")
        for handler in self._handlers.get("popup", ()):
            handler(page)
        return page


class FakeContext:
    """A browser context and the pages on it."""

    def __init__(self, log, name, pages=(), pops=None):
        self.log = log
        self.name = name
        self.pages = list(pages)
        self._pops = pops

    async def new_page(self):
        self.log.append(f"new_page {self.name}")
        page = FakeCdpPage(self.log, "ours", pops=self._pops)
        self.pages.append(page)
        return page


class FakeCdpBrowser:
    """The browser on the other end of the port, which is not ours to close.

    `new_context` and `new_page` are here to be counted rather than used. In
    the real thing they succeed, which is the problem; here their being called
    at all is the failure.
    """

    def __init__(self, log, contexts):
        self.log = log
        self.contexts = contexts
        self.fresh = 0

    async def new_context(self, **_):
        self.fresh += 1
        self.log.append("new_context")
        return FakeContext(self.log, "fresh")

    async def new_page(self, **_):
        self.fresh += 1
        self.log.append("browser.new_page")
        return FakeCdpPage(self.log, "implicit")

    async def close(self):
        self.log.append("close")


class FakeCdpDriver(FakeDriver):
    """`async_playwright()`, with a browser to attach to rather than launch."""

    def __init__(self, log, panel=1, contexts=1, refuses=False, pops=None):
        super().__init__(log)
        self.endpoint = None
        self.browser = None
        self.page = None
        self._panel = panel
        self._contexts = contexts
        self._refuses = refuses
        self._pops = pops
        self.chromium = SimpleNamespace(launch=self._launch,
                                        connect_over_cdp=self._connect)

    async def _connect(self, endpoint, **_):
        self.endpoint = endpoint
        if self._refuses:
            # What a Pi whose kiosk unit predates #144 answers, which is most
            # of them: the installer will not copy that unit.
            raise RuntimeError("connect ECONNREFUSED 127.0.0.1:9222")
        self.log.append("connect")
        names = ["panel"] + [f"panel-{n}" for n in range(2, self._panel + 1)]
        first = [FakeCdpPage(self.log, n) for n in names[:self._panel]]
        self.browser = FakeCdpBrowser(self.log, [
            FakeContext(self.log, f"ctx{n}", first if n == 0 else [],
                        pops=self._pops)
            for n in range(self._contexts)])
        return self.browser


def cdp(**kwargs):
    """Point `_Session` at a fake panel browser and hand back the driver."""
    import playwright.async_api as pw_api

    log: list[str] = []
    before = pw_api.async_playwright

    class _Scope:
        def __enter__(self):
            driver = FakeCdpDriver(log, **kwargs)
            pw_api.async_playwright = driver
            return driver

        def __exit__(self, *_):
            pw_api.async_playwright = before

    return _Scope()


async def _attached(endpoint="http://127.0.0.1:9222", steps=(), taps=0,
                    allowlist="example.test", **kwargs):
    """Attach, take `steps`, take `taps`, let go. Hand back the driver.

    `taps` is the other hand on the panel: a tab opening on our page with no
    step in flight, which is what a person tapping a `target="_blank"` link
    in the tab the agent left in front of them produces. It is fired outside
    `act` on purpose — that is the whole of the difference the run is allowed
    to see.

    `allowlist` has a value because the attached path refuses an empty one
    (#208), and none of the sixteen cases below is about that. Pass `None` for
    the one that is.
    """
    with cdp(**kwargs) as driver, env(BROWSE_CDP=endpoint,
                                      BROWSE_ALLOWLIST=allowlist):
        session = browser._Session()
        try:
            async with session:
                driver.page = session.page
                for step in steps:
                    await session.act(step)
                for _ in range(taps):
                    session.page.pop()
        except RuntimeError as err:
            driver.log.append(f"raised {err.args[0]}")
        if (session._browser, session._pw, session.page) != (None,) * 3:
            driver.log.append("handles left behind")
    return driver


run_ = asyncio.run(_attached())
# The whole shape in one list, because the order is half of it: the tab we
# opened goes before the panel's is restored, and both go before the socket is
# dropped. `close` with no page name is the browser's, and it is a disconnect
# — measured over CDP, the process stays up and so do its own pages.
check("BROWSE_CDP attaches, works in a tab of its own, and hands the "
      "panel back",
      run_.log, ["start", "connect", "new_page ctx0", "viewport ours",
                 "close ours", "front panel", "close", "stop"])
check("and nothing is launched on that path", run_.launched, None)
check("the endpoint is passed through as it was written",
      run_.endpoint, "http://127.0.0.1:9222")
# The trap. Both of these succeed against a real browser and both hand back an
# empty cookie jar, so the only thing that can be asserted is that neither was
# reached for.
check("the tab is made on the profile's own context, not a fresh one",
      run_.browser.fresh if run_.browser else "never attached", 0)
check("and it is that context's page the run drives",
      run_.page is run_.browser.contexts[0].pages[-1] if run_.browser
      else "never attached", True)
# `BrowserContext.new_page` takes no viewport and an attached page reports
# none, so the size the model is calibrated against — 1,848 tokens a
# screenshot, flat — has to be set afterwards.
check("with the viewport the model has been calibrated against",
      run_.page.viewport if run_.page else None, browser.VIEWPORT)
# The panel is a product surface. Being brought to the front is the only thing
# this run may do to the page somebody is standing in front of.
check("the panel's own page is not navigated and not closed",
      [e for e in run_.log if e.endswith("panel")], ["front panel"])

two = asyncio.run(_attached(panel=2))
# A second page on that profile is somebody debugging, and putting theirs in
# front of the kitchen is worse than leaving the front tab where it was.
check("a second page on the profile is left where it is",
      [e for e in two.log if "panel" in e], ["front panel"])

refused = asyncio.run(_attached(refuses=True))
# The same hole `launch` has on a Pi with no BROWSE_CHROMIUM, and it is wider:
# this one is what every Pi answered until #183 had the deploy carry the kiosk
# unit, and what one still answers between a deploy and the restart after it.
check("an endpoint that refuses still stops the driver it started",
      refused.log,
      ["start", "stop", "raised connect ECONNREFUSED 127.0.0.1:9222"])

empty = asyncio.run(_attached(contexts=0))
# `contexts[0]` on a browser with none is an IndexError three frames from
# anything that names the endpoint, which is a bad thing to read in a journal.
# The `close` in the middle is the socket going away: the attach got as far as
# connecting, so the teardown has a connection to drop even though the session
# never opened.
check("a browser with no context to drive says so, and says which endpoint",
      empty.log, ["start", "connect", "close", "stop",
                  "raised http://127.0.0.1:9222 has no browser context "
                  "to drive"])

# Empty means anywhere everywhere else in this file, and on the attached path
# that is a policy nobody typed: the tab is on the panel's own profile and
# fills the screen. So the endpoint alone is not enough to connect on, and the
# refusal comes before `connect_over_cdp` rather than after — a run that got as
# far as the socket has already opened a tab on the profile it was not allowed
# to drive. The log is the only place that difference shows.
shut = asyncio.run(_attached(allowlist=None))
check("BROWSE_CDP with an empty allowlist never reaches the browser",
      [e for e in shut.log if not e.startswith("raised ")],
      ["start", "stop"])
# Named, both of them, because the person reading this in a journal has one
# file to edit and two lines to write in it, and a message that says only
# "allowlist" sends them looking for the wrong half.
check("and it says which two variables disagree",
      [w for w in ("BROWSE_CDP", "BROWSE_ALLOWLIST")
       if any(w in e for e in shut.log)],
      ["BROWSE_CDP", "BROWSE_ALLOWLIST"])
# The control, and the case this pair is worthless without: a guard written on
# the allowlist alone reads the same in every test above and stops every
# machine that is not the panel from browsing at all. Unattached, empty still
# means anywhere.
with env(BROWSE_ALLOWLIST=None):
    open_ = asyncio.run(_open_and_close(True))
check("but an empty allowlist with no endpoint is still a throwaway, not a "
      "refusal",
      open_, ["start", "launch", "close", "stop"])
# And the default, which is every machine that is not the panel. Green before
# this change as well as after: what it watches is that the attach path stayed
# opt-in, since it is the one that puts live sessions behind a page the model
# picked.
check("unset BROWSE_CDP still launches a throwaway",
      asyncio.run(_open_and_close(True)),
      ["start", "launch", "close", "stop"])
with env(BROWSE_CDP="  "):
    blank_cdp = browser._cdp()
check("and whitespace is unset, not an endpoint to attach to", blank_cdp, "")

# #229, and why the list is read before `__aenter__` picks a path rather than
# only inside the branch above. The refusal has to land on both paths and it
# has to land before a browser exists: past `launch`, the same value is a run
# that spends its whole budget refusing its own `goto`s and answers "I could
# not find that", which is indistinguishable from a page that did not say.
with env(BROWSE_CDP=None, BROWSE_ALLOWLIST="*"), driven(True) as star_driver:
    star_session = browser._Session()

    async def _enter_with_star():
        async with star_session:
            pass

    star = raised(lambda: asyncio.run(_enter_with_star()))
    star_log = list(star_driver.log)
check("an unattached run with an entry no host could equal never launches",
      star_log, ["start", "stop"])
check("and the driver it started is stopped and let go of",
      (star_session._browser, star_session._pw, star_session.page), (None,) * 3)
check("and what it raised names the entry",
      star.startswith("ValueError: ") and "'*'" in star, True)
# The attached path takes the same refusal, and takes it before the socket:
# a run that got as far as `connect_over_cdp` has already opened a tab on the
# panel's own profile, which is the thing #208's guard exists to prevent.
with env(BROWSE_CDP="http://127.0.0.1:9222", BROWSE_ALLOWLIST="*"), \
        cdp() as star_cdp:
    attached_session = browser._Session()

    async def _attach_with_star():
        async with attached_session:
            pass

    attached_star = raised(lambda: asyncio.run(_attach_with_star()))
    attached_log = list(star_cdp.log)
check("and an attached one never reaches the browser either",
      attached_log, ["start", "stop"])
check("with the same message, since it is the same mistake",
      attached_star.startswith("ValueError: ") and "'*'" in attached_star,
      True)
# The template is where this variable is met, and an undocumented one on a
# machine nobody logs into is a feature that exists and is never switched on.
template = (Path(__file__).resolve().parent.parent
            / ".env.example").read_text(encoding="utf-8")
check("the template carries the endpoint the Pi would use",
      "# BROWSE_CDP=http://127.0.0.1:9222" in template, True)
# And says the two go together, because the template is what a person reads
# while writing the file the guard above refuses. Learning the rule from a
# RuntimeError after the panel has stopped answering is the wrong order.
check("and says the allowlist goes with it rather than leaving it to be found",
      "set together or not at all" in template, True)

print("\n== _Session: the tabs a run opens, and the ones it does not ==")
# The half of "give the panel back" that `_release` used to miss. A click on
# `<a target="_blank">` — the commonest link on a timetable — opens a second
# tab, and nothing closed it: measured against Chromium 151, three runs each
# clicking one such link left the profile holding 2, 3 and then 4 pages, live,
# on the signed-in profile, after the connection was dropped.
#
# Why it is recorded as it happens rather than worked out at the end: there is
# nothing left to work it out from. `page.opener()` names the frame a tab came
# from, and measured against Chromium 151 an agent's click and a person's tap
# on the same link give the identical answer — our page. A teardown-time rule
# written off `opener()` therefore closes the tab somebody at the panel opened
# and is reading, which is the one thing `_theirs` exists to forbid. The only
# moment the two are distinguishable is while we are the one dispatching
# input, so that moment is where the answer is written down.

opened = asyncio.run(_attached(pops="in the settle",
                               steps=[{"act": "click", "idx": 0}]))
check("a tab the run's own click opened is closed with the run's own tab",
      [e for e in opened.log if e.startswith("close ")],
      ["close ours-popup1", "close ours"])
check("and the panel is still handed back afterwards",
      [e for e in opened.log if e.endswith("panel")], ["front panel"])

two_tabs = asyncio.run(_attached(pops="on the click",
                                 steps=[{"act": "click", "idx": 0},
                                        {"act": "click", "idx": 1}]))
# Per tab-opening click and not per run, which is how the leak grew: two
# clicks in one run left two. Newest first, because a tab can open a tab.
check("two of them in one run leave nothing behind, newest first",
      [e for e in two_tabs.log if e.startswith("close ")],
      ["close ours-popup2", "close ours-popup1", "close ours"])

tapped = asyncio.run(_attached(taps=1))
# The measurement that sank the `opener()` shape. This tab has our page as its
# opener and is not ours, and no reading taken at teardown can tell.
check("a tab that opened with no step in flight is somebody else's",
      [e for e in tapped.log if e.startswith("close ")], ["close ours"])

early = asyncio.run(_attached(pops="on the label read",
                              steps=[{"act": "click", "idx": 0}]))
# The window opens after the label is read and not at the top of the step:
# that read is a locator call that waits up to 8 s on an index the DOM will
# not resolve, and eight seconds of somebody else's taps is not ours.
check("nor is one that arrives before the step dispatches anything",
      [e for e in early.log if e.startswith("close ")], ["close ours"])

keyed = asyncio.run(_attached(pops="on the press",
                              steps=[{"act": "press", "key": "PageDown"}]))
check("a press opens the same window a click does",
      [e for e in keyed.log if e.startswith("close ")],
      ["close ours-popup1", "close ours"])

nested = asyncio.run(_attached(pops="and that tab opens one",
                               steps=[{"act": "click", "idx": 0}]))
# The reason `_popped` puts the handler on the tab it keeps as well as on
# ours: playwright fires `popup` on the page that opened the tab and on no
# other, so a tab opened by a tab we opened has no handler anywhere unless the
# record follows it down. Measured against Chromium 151, a chained
# `window.open` leaves that grandchild on the profile without this line.
check("and so is a tab opened by a tab the run opened",
      [e for e in nested.log if e.startswith("close ")],
      ["close ours-popup1-popup1", "close ours-popup1", "close ours"])

print("\n== _Session: attached to a real Chromium ==")
# The section above is a stand-in agreeing with itself. These five run against
# a browser, because every one of them is a claim about playwright and
# Chromium rather than about this repo — and they are the claims `_attach` is
# built on, so an upgrade that changes one of them should turn this red rather
# than turn a Pi silently logged out.
#
# Hermetic: a persistent context of its own under a temp directory, with
# `--remote-debugging-port=0` so no fixed port can collide with anything and
# `DevToolsActivePort` to read back the one Chromium chose. Loopback only, and
# the only cookie in it is one this file put there. Skipped rather than failed
# where there is no browser to launch, like the sections below.


async def real_attach():
    """Raise a signed-in browser, attach to it the way `_Session` does."""
    from playwright.async_api import async_playwright

    out: dict = {}
    profile = tempfile.mkdtemp(prefix="browse-check-cdp-")
    pw = await async_playwright().start()
    panel = None
    try:
        launch = {"headless": True,
                  "args": ["--password-store=basic", "--disable-gpu",
                           "--remote-debugging-port=0"]}
        chromium = os.getenv("BROWSE_CHROMIUM")
        if chromium:
            launch["executable_path"] = chromium
        panel = await pw.chromium.launch_persistent_context(profile, **launch)
        port = (Path(profile) / "DevToolsActivePort").read_text().split()[0]
        # The stand-in for being signed into something. Loopback, so no site
        # is contacted and nothing real is held.
        await panel.add_cookies([{"name": "signed_in", "value": "yes",
                                  "url": "http://127.0.0.1/",
                                  "expires": time.time() + 3600}])
        endpoint = f"http://127.0.0.1:{port}"

        attached = await pw.chromium.connect_over_cdp(endpoint)
        out["profile_jar"] = [c["name"]
                              for c in await attached.contexts[0].cookies()]
        fresh = await attached.new_context()
        out["fresh_context_jar"] = [c["name"] for c in await fresh.cookies()]
        await fresh.close()
        implicit = await attached.new_page()
        out["implicit_context_jar"] = [c["name"]
                                       for c in await implicit.context.cookies()]
        await implicit.close()
        await attached.close()

        # And the whole of `_Session` over the same endpoint.
        before = len(panel.pages)
        # Spelled out rather than inherited: this block and `real_popup` are
        # the two that do not go red when they break. An empty allowlist would
        # make `__aenter__` refuse (#208), the `except Exception` below would
        # read that as no Chromium, and the run would stay green having tested
        # nothing. Loopback and not a placeholder host, because everything
        # either block reaches is served from this machine — an allowlist that
        # excluded it would send half of `real_popup` back to `about:blank`.
        with env(BROWSE_CDP=endpoint, BROWSE_CHROMIUM=chromium,
                 BROWSE_ALLOWLIST="127.0.0.1"):
            async with browser._Session() as session:
                out["session_jar"] = [
                    c["name"] for c in await session.page.context.cookies()]
                out["pages_during"] = len(panel.pages) - before
        out["pages_after"] = len(panel.pages) - before
        out["panel_alive"] = not panel.pages[0].is_closed()
    finally:
        for closing in (panel,):
            if closing:
                with contextlib.suppress(Exception):
                    await closing.close()
        await pw.stop()
        shutil.rmtree(profile, ignore_errors=True)
    return out


try:
    real = asyncio.run(real_attach())
except Exception as err:  # no Chromium to attach to is not this file's news
    print(f"   skipped: no Chromium to attach to ({type(err).__name__})")
else:
    check("the profile's own context is the one holding its cookies",
          real["profile_jar"], ["signed_in"])
    # The two that look like they would work and are the reason `_attach`
    # exists as a method. Neither raises; both are logged out.
    check("a context made over CDP gets an empty jar of its own",
          real["fresh_context_jar"], [])
    check("and so does the one Browser.new_page makes without being asked",
          real["implicit_context_jar"], [])
    # End to end: this is `_Session`, not a stand-in, and the cookie is the
    # whole point of #162.
    check("so a session over BROWSE_CDP reads with the profile's cookies",
          real["session_jar"], ["signed_in"])
    # Disconnect, not close. The tab is ours to clean up and the panel's is
    # not ours to touch.
    check("its tab is on the panel's browser during the run and gone after",
          (real["pages_during"], real["pages_after"], real["panel_alive"]),
          (1, 0, True))

# The link a timetable is made of, and the one action a person at the panel
# can take on it. Nothing is fetched: `set_content` writes this into a page
# that stays on `about:blank`, and the tab it opens goes to `about:blank` too.
LINK = '<a data-agent-idx="0" target="_blank" href="about:blank#opened">go</a>'
# And the same tab opened by the site's own script instead of by an attribute.
# Here so that "stop the tab from opening" -- rewriting `target` to `_self`
# before the click, which is the cheaper-looking fix and which passes every
# case above -- is a red line and not a preference: measured, that rewrite
# leaves this one behind.
SCRIPTED = ('<button data-agent-idx="0" '
            'onclick="window.open(\'about:blank#opened\')">go</button>')
# And a tab that opens a tab, which is the shape `page.on("popup")` alone
# cannot see: the event fires on the opener and on no other page, so the
# second one is recorded only because `_popped` puts the handler on the first.
# `document.write` into the window `window.open` handed back is same-origin
# `about:blank`, and Chromium's user activation reaches the script it writes.
CHAINED = ('<button data-agent-idx="0" onclick="'
           "var w = window.open('about:blank#opened');"
           "w.document.write("
           "'<script>window.open(\\'about:blank#nested\\')</script>')"
           '">go</button>')
# The element that resolves and then will not be clicked: a cookie banner that
# lands after the page was tagged, here as a transparent veil so the anchor
# under it is still *visible* by every definition playwright has. `click`
# retries actionability until its timeout and never dispatches. The second
# link is outside the veil and is the person's, and it has to survive: the
# whole regression this fixture exists for is the run adopting it because the
# run happened to be stuck at the time.
VEILED = ('<a data-agent-idx="0" target="_blank" href="about:blank#opened" '
          'style="position:absolute;top:40px;left:0">go</a>'
          '<div style="position:absolute;top:0;left:0;width:300px;'
          'height:120px;background:rgba(0,0,0,0.002);z-index:9"></div>'
          '<a id="theirs" target="_blank" href="about:blank#theirs" '
          'style="position:absolute;top:200px;left:0">theirs</a>')
# And the other end of the same trigger: a click that dispatches, opens its
# tab, and *then* runs out of time waiting for the navigation it caused —
# playwright waits that out inside the click's own timeout. The tab is the
# run's and has to go, which is what stops "on a timeout, disown whatever
# arrived" from being the fix.
HANGING = ('<a data-agent-idx="0" target="_blank" href="about:blank#opened" '
           "onclick=\"location.href='/hang'\">go</a>")
# And the one in between, which is the ordinary web rather than a fixture: a
# link whose server is simply slow. It has to come back as a step that worked,
# on the page it asked for, because everything downstream of the note reads it
# as gospel — a `failed` skips the settle, leaves the next `look` photographing
# the page the click was supposed to leave, and counts against `stuck`.
SLOW = '<a data-agent-idx="0" href="/slow">go</a>'
# And the ordinary links that open no document at all. A `mailto:` hands the
# address to the system and a 204 answers without a page, and Chromium aborts
# the navigation either way: `issued` sees the request, the waiter dies of
# `ERR_ABORTED` rather than of the timeout, and the split that gave the
# navigation its own budget turned both of them into a reported failure. A
# contact page with an email address on it is not an exotic input. `tel:` is
# the same door as the `mailto:` and gets no case of its own.
# The third link is the line the clause must not cross: a dispatched click
# whose navigation fails for a reason that is not an abort. It has to stay a
# `failed:`, because the page really did not go where it was asked, and it is
# what stops the guard from being "swallow anything after a dispatch".
#
# The fourth and fifth are the shapes the clause was measured to swallow
# without ever naming them, and they are pinned here as accepted rather than
# fixed: a scheme no handler claims and a `Content-Disposition: attachment`
# both come back as the same one-line `net::ERR_ABORTED`, with no `response`
# and no `download` event to separate the unclaimed scheme from the `mailto:`
# above it. There is no material to tell them apart, so `ok` is the answer for
# all of them and these two cases exist to say that on purpose.
NO_DOCUMENT = ('<a data-agent-idx="0" href="mailto:nobody@example.test">'
               'mail</a>'
               '<a data-agent-idx="1" href="/empty">nothing</a>'
               '<a data-agent-idx="2" href="http://127.0.0.1:{gone}/x">'
               'refused</a>'
               '<a data-agent-idx="3" href="weirdscheme://nowhere/x">'
               'unclaimed</a>'
               '<a data-agent-idx="4" href="/attach">attachment</a>')
# And the page that dies under its own click: a link into `/hang`, which the
# loopback accepts and never answers, so the navigation is still in flight
# when the page goes.
CLOSING = '<a data-agent-idx="0" href="/hang">go</a>'


def _closed_port() -> int:
    """A loopback port with nothing listening on it.

    Bound and released, so the number was real a moment ago and is free now —
    which is a connection refused rather than a name that will not resolve.
    Nothing here leaves 127.0.0.1, and a DNS lookup would.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _loopback(html: str):
    """A server on 127.0.0.1 with one page on it and two URLs that take their time.

    A real socket rather than `page.route`, because the claim under test is
    what Chromium does with a navigation that has been accepted and never
    answered, and an intercepted route is a different thing wearing the same
    URL. Nothing leaves the loopback interface.
    """
    async def handle(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
        except Exception:
            writer.close()
            return
        if b"GET /hang " in request:
            # Accepted, never answered and never closed. What ends it is the
            # server being torn down at the end of the run.
            await asyncio.sleep(30)
            return
        if b"GET /empty " in request:
            # Answered, and with no document. The connection is honoured and
            # the navigation is still aborted, which is the half of `mailto:`
            # that has nothing to do with an external scheme.
            writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        if b"GET /attach " in request:
            # Answered, and with a document the browser keeps for itself. A
            # download is the third way a dispatched navigation aborts, and
            # the one the clause never named.
            csv = b"col1,col2\r\n1,2\r\n"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\n"
                         b'Content-Disposition: attachment; filename="a.csv"'
                         b"\r\nContent-Length: %d\r\n\r\n" % len(csv) + csv)
            await writer.drain()
            writer.close()
            return
        if b"GET /slow " in request:
            # Slow and not broken, which is the difference the click budget
            # has to be able to tell. Three seconds: outside the two a click
            # may spend dispatching and inside the eight its page may spend
            # answering, so a run that conflates the two cannot pass this.
            await asyncio.sleep(3.0)
        body = html.encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                     b"Content-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _tapped(page, selector='[data-agent-idx="0"]'):
    """A finger on the Touch Display 2, delivered over CDP.

    `Input.dispatchTouchEvent` and deliberately not `locator.tap()`: the whole
    claim is that this is *not* one of the run's own actions, so it must not
    go through anything `act` touches. The sleep is the tab arriving on the
    websocket a beat after the finger does — measured at 66-111 ms.

    The selector is a parameter because the veiled fixture below needs the
    person to tap a *different* link from the one the agent is failing to
    click; everywhere else they are the same link, which is the harder case.
    """
    cdp = await page.context.new_cdp_session(page)
    box = await page.locator(selector).bounding_box()
    await cdp.send("Input.dispatchTouchEvent",
                   {"type": "touchStart",
                    "touchPoints": [{"x": box["x"] + box["width"] / 2,
                                     "y": box["y"] + box["height"] / 2,
                                     "id": 1}]})
    await cdp.send("Input.dispatchTouchEvent",
                   {"type": "touchEnd", "touchPoints": []})
    await asyncio.sleep(0.5)


async def real_popup():
    """Runs that open a second tab, on one profile, against a real browser.

    Cumulative on purpose: the defect was one leaked page per tab-opening
    click on a browser that never restarts, so a single run cannot show it and
    three can. The fourth run is the other half — a person tapping the same
    link in the tab the agent left in front of them, which `opener()` reports
    identically and which must survive. The fifth takes the attribute away and
    leaves the script, which is the case the cheap fix cannot reach.
    """
    from playwright.async_api import async_playwright

    out: dict = {}
    profile = tempfile.mkdtemp(prefix="browse-check-popup-")
    pw = await async_playwright().start()
    panel = None
    try:
        launch = {"headless": True,
                  "args": ["--password-store=basic", "--disable-gpu",
                           "--remote-debugging-port=0"]}
        chromium = os.getenv("BROWSE_CHROMIUM")
        if chromium:
            launch["executable_path"] = chromium
        panel = await pw.chromium.launch_persistent_context(profile, **launch)
        port = (Path(profile) / "DevToolsActivePort").read_text().split()[0]
        with env(BROWSE_CDP=f"http://127.0.0.1:{port}",
                 BROWSE_CHROMIUM=chromium,
                 BROWSE_ALLOWLIST="127.0.0.1"):
            out["left after each run"] = []
            for _ in range(3):
                async with browser._Session() as session:
                    await session.page.set_content(LINK)
                    await session.act({"act": "click", "idx": 0})
                out["left after each run"].append(len(panel.pages))
            # Counted as a delta and not a total, so this half says only
            # whether the run over-reached. A total would also move with the
            # three runs above and read as red for their reason, not its own.
            before = len(panel.pages)
            async with browser._Session() as session:
                await session.page.set_content(LINK)
                await _tapped(session.page)
                out["theirs, during"] = len(panel.pages) - before
            out["theirs, after"] = len(panel.pages) - before
            for page in list(panel.pages)[1:]:
                await page.close()
            before = len(panel.pages)
            async with browser._Session() as session:
                await session.page.set_content(SCRIPTED)
                await session.act({"act": "click", "idx": 0})
            out["scripted"] = len(panel.pages) - before
            for page in list(panel.pages)[1:]:
                await page.close()
            before = len(panel.pages)
            async with browser._Session() as session:
                await session.page.set_content(CHAINED)
                await session.act({"act": "click", "idx": 0})
                # Both halves, so the case cannot pass by the chain never
                # having happened: three tabs up during the run — ours, the
                # one our click opened, and the one that one opened — and
                # none of them left after it.
                out["chained, during"] = len(panel.pages) - before
            out["chained, after"] = len(panel.pages) - before
            for page in list(panel.pages)[1:]:
                await page.close()

            # The two ends of one trigger, and they fail in opposite
            # directions, which is why neither can be handled by a rule that
            # looks at the timeout. The click is stuck on actionability and
            # dispatched nothing, so the tab is the person's; or the click
            # dispatched, opened its tab and then ran out of time waiting for
            # its own navigation, so the tab is the run's. Nothing observable
            # at the moment of the timeout tells them apart — same opener,
            # same timing, and CDP does not say which gesture caused a popup.
            before = len(panel.pages)
            async with browser._Session() as session:
                await session.page.set_content(VEILED)
                # Three seconds in: comfortably inside the eight the retry
                # used to hold the flag up for, and comfortably outside the
                # two it holds it up for now. A tap timed to land inside both
                # would pass either way and pin nothing.
                async def person():
                    await asyncio.sleep(3.0)
                    await _tapped(session.page, "#theirs")

                tapping = asyncio.create_task(person())
                out["veiled note"] = (await session.act(
                    {"act": "click", "idx": 0}))[0].split(" ")[0]
                await tapping
            out["veiled"] = len(panel.pages) - before
            for page in list(panel.pages)[1:]:
                await page.close()

            before = len(panel.pages)
            server, port = await _loopback(HANGING)
            try:
                async with browser._Session() as session:
                    await session.page.goto(f"http://127.0.0.1:{port}/")
                    out["hanging note"] = (await session.act(
                        {"act": "click", "idx": 0}))[0].split(" ")[0]
                out["hanging"] = len(panel.pages) - before
            finally:
                server.close()

            server, port = await _loopback(SLOW)
            try:
                async with browser._Session() as session:
                    await session.page.goto(f"http://127.0.0.1:{port}/")
                    out["slow note"] = (await session.act(
                        {"act": "click", "idx": 0}))[0]
                    out["slow url"] = session.page.url.endswith("/slow")
            finally:
                server.close()

            server, port = await _loopback(
                NO_DOCUMENT.format(gone=_closed_port()))
            try:
                async with browser._Session() as session:
                    await session.page.goto(f"http://127.0.0.1:{port}/")
                    out["mailto note"] = (await session.act(
                        {"act": "click", "idx": 0}))[0]
                    session._attached = False
                    await session.page.focus('[data-agent-idx="0"]')
                    out["press mailto note"] = (await session.act(
                        {"act": "press", "key": "Enter"}))[0]
                    session._attached = True
                    out["empty note"] = (await session.act(
                        {"act": "click", "idx": 1}))[0]
                    out["unclaimed note"] = (await session.act(
                        {"act": "click", "idx": 3}))[0]
                    out["attach note"] = (await session.act(
                        {"act": "click", "idx": 4}))[0]
                    # The other half of both: an abort leaves the browser where
                    # it was, so a step reported as worked has to be a step
                    # after which `look` still answers about this page.
                    out["still here"] = session.page.url.endswith(f":{port}/")
                    # Last, because this one does move the page — the error
                    # document is off the allowlist and the session bounces
                    # itself back to blank.
                    out["refused note"] = (await session.act(
                        {"act": "click", "idx": 2}))[0]
            finally:
                server.close()

            # And the shape that wears `ERR_ABORTED` and is not a step that
            # worked. The page is closed 300 ms into a navigation the server
            # accepted and will never answer, which aborts the request on the
            # way out, so the clause sees exactly what a `mailto:` gives it.
            server, port = await _loopback(CLOSING)
            try:
                async with browser._Session() as session:
                    dying = session.page
                    await dying.goto(f"http://127.0.0.1:{port}/",
                                     wait_until="domcontentloaded")
                    # The state every step after the first is in, and the
                    # reason the swallow is silent rather than loud: `_settle`
                    # waits for `networkidle`, an aborted navigation never
                    # commits, so the load state is still there afterwards and
                    # `_settle` short-circuits instead of raising on the dead
                    # page. Without this line the case would pass at the
                    # parent too, for the wrong reason.
                    await session._settle(0)

                    async def shutter():
                        await asyncio.sleep(0.3)
                        with contextlib.suppress(Exception):
                            await dying.close()

                    closing = asyncio.create_task(shutter())
                    try:
                        out["closed note"] = (await session.act(
                            {"act": "click", "idx": 0}))[0]
                        out["closed via"] = "reported a note"
                    except Exception as err:
                        out["closed note"] = type(err).__name__
                        # Which door it left by, not just that it left. The
                        # module says a closed page ends the run at `:1100`;
                        # ending it by tripping over the corpse in `_settle`
                        # is the same outcome reached by accident.
                        out["closed via"] = ("_settle" if any(
                            frame.name == "_settle" for frame in
                            traceback.extract_tb(err.__traceback__))
                            else "the re-raise")
                    await closing
            finally:
                server.close()

            # And the same for press: closing the page mid-navigation aborts
            # the request, which must re-raise rather than return 'ok'.
            server, port = await _loopback(CLOSING)
            try:
                async with browser._Session() as session:
                    session._attached = False
                    dying = session.page
                    await dying.goto(f"http://127.0.0.1:{port}/",
                                     wait_until="domcontentloaded")
                    await dying.focus('[data-agent-idx="0"]')
                    await session._settle(0)

                    async def shutter():
                        await asyncio.sleep(0.3)
                        with contextlib.suppress(Exception):
                            await dying.close()

                    closing = asyncio.create_task(shutter())
                    try:
                        out["press closed note"] = (await session.act(
                            {"act": "press", "key": "Enter"}))[0]
                        out["press closed via"] = "reported a note"
                    except Exception as err:
                        out["press closed note"] = type(err).__name__
                        out["press closed via"] = ("_settle" if any(
                            frame.name == "_settle" for frame in
                            traceback.extract_tb(err.__traceback__))
                            else "the re-raise")
                    await closing
            finally:
                server.close()
            for leftover in list(panel.pages)[1:]:
                await leftover.close()

            # And what the navigation budget must not cost the clicks that
            # never use it. `LINK` opens a tab and leaves the main frame where
            # it was, so nothing navigates and nothing should be waited for.
            # Timed rather than asserted on a note, because the failure this
            # watches is silent: every case above still passes at eight
            # seconds a click.
            async with browser._Session() as session:
                await session.page.set_content(LINK)
                nowhere = time.monotonic()
                await session.act({"act": "click", "idx": 0})
                out["nowhere ms"] = round(
                    (time.monotonic() - nowhere) * 1000)
            for page in list(panel.pages)[1:]:
                await page.close()
    finally:
        for closing in (panel,):
            if closing:
                with contextlib.suppress(Exception):
                    await closing.close()
        await pw.stop()
        shutil.rmtree(profile, ignore_errors=True)
    return out


try:
    leaked = asyncio.run(real_popup())
except Exception as err:  # no Chromium to attach to is not this file's news
    print(f"   skipped: no Chromium to attach to ({type(err).__name__})")
else:
    # One page on the profile before, one after, three times over. Before the
    # fix this read [2, 3, 4], measured, and only the kiosk Chromium exiting
    # would ever have cleared them.
    check("a run that clicks a tab-opening link leaves the profile as it "
          "found it, run after run",
          leaked["left after each run"], [1, 1, 1])
    # And the line the fix is not allowed to cross. Two tabs more than the
    # profile started that run with while the run is up -- ours and theirs --
    # and one more after it, because ours goes and theirs does not.
    check("a tab a person opened in our own tab outlives the run",
          (leaked["theirs, during"], leaked["theirs, after"]), (2, 1))
    # No attribute to rewrite and no link to avoid: the click runs the site's
    # script, the script opens the tab, and the record still has to name it.
    check("and one the site's own script opened under our click goes too",
          leaked["scripted"], 0)
    # The generation below that. `page.on("popup")` fires on the opener and on
    # nothing else, so this one is reachable only because the record follows
    # each kept tab down. Without that line this reads (3, 1), measured: one
    # `about:blank#nested` left live on the signed-in profile per run.
    check("and so does the tab that tab opened",
          (leaked["chained, during"], leaked["chained, after"]), (3, 0))
    # The gate on the regression. Both halves, because a run that never got
    # stuck would leave the tab alone for the wrong reason and read green: the
    # click has to have failed *and* the person's tab has to still be there.
    check("a click stuck on actionability does not take a tab the person "
          "opened while it waited",
          (leaked["veiled note"], leaked["veiled"]), ("failed:", 1))
    # And the direction the obvious repair breaks. Green here and green at the
    # parent -- it is a control, and it is the one that says why "on a
    # timeout, disown whatever arrived" is not the fix: this tab arrived
    # during a timeout and is the run's.
    check("but a click that opens a tab and then times out on its own "
          "navigation still takes it",
          (leaked["hanging note"], leaked["hanging"]), ("failed:", 0))
    # The regression #162 was reopened for. Both halves, because a note alone
    # would pass on a click that never went anywhere: the step has to read as
    # worked *and* the browser has to be on the page it asked for. Red before
    # at ('failed: click 0 did not land in time', False).
    check("a click whose page answers slower than the click budget is not a "
          "failed click",
          (leaked["slow note"], leaked["slow url"]), ("ok", True))
    # And the cost of fixing it, which is the half that has no note to read.
    # An in-place click waits for a commit that is never coming, so a
    # navigation budget spent unconditionally shows up here as `CLICK_NAV_MS`
    # and nowhere else. Generous against a cold profile on a loaded machine —
    # what it has to separate is a settle from eight seconds, not 300 ms from
    # 600 ms.
    print(f"        ({leaked['nowhere ms']} ms, against the "
          f"{browser.CLICK_NAV_MS} ms it must not have paid)")
    check("and a click that navigates nowhere does not wait for a page",
          leaked["nowhere ms"] < 2000, True)
    # The cost of that budget on the other side. A link that opens no document
    # asks for a page and is then aborted, which is an `Error` and not a
    # `TimeoutError`, so before the clause at `browser.py:1047-1109` it fell past
    # both `except`s and came back
    # 'failed: click 0 — net::ERR_ABORTED; maybe frame was detached?'. A wrong
    # word is the small half: `failed:` skips the settle and counts against
    # `stuck`, so a working click made the run look like it was getting
    # nowhere.
    check("a click on a link that opens no document is not a failed click",
          leaked["mailto note"], "ok")
    check("nor is one whose server answers without a document",
          leaked["empty note"], "ok")
    check("nor is a press on a link that opens no document",
          leaked["press mailto note"], "ok")
    check("and either way the page it was clicked on is still in front of it",
          leaked["still here"], True)
    # And the line that keeps the clause from being "swallow anything after a
    # dispatch". A link into a closed port is dispatched, asks for a page and
    # never gets one, and the model has to be told: the next `look` is about
    # the error document, or about blank once the allowlist has bounced it.
    check("but a dispatched click that fails for another reason still says so",
          (leaked["refused note"].split(" ")[0],
           "ERR_CONNECTION_REFUSED" in leaked["refused note"]),
          ("failed:", True))
    # The two the clause was already swallowing and never named. Pinned as
    # accepted, not as fixed: measured, a scheme no handler claims raises no
    # `response` and no `download` event, which is the same nothing a
    # `mailto:` raises, so there is no material to report one of them
    # differently from the other. Green at `cab2e18` too — these say the
    # behaviour is on purpose, and they go red the day somebody narrows the
    # guard without deciding about them.
    check("a click on a scheme nobody claims reads as a click that worked, "
          "because nothing can tell it from a mailto:",
          leaked["unclaimed note"], "ok")
    check("and so does one whose answer the browser downloads instead of "
          "showing",
          leaked["attach note"], "ok")
    # And the shape that must not be forgiven. A page closed mid-navigation
    # aborts its own request, so it reaches `browser.py:1047` wearing the same
    # `net::ERR_ABORTED` — and `is_closed()` is still False there, measured on
    # 12 of 12 trials, so the guard `:1100` uses cannot simply be copied up.
    # Red at the parent as ('ok', 'reported a note'): the step said the click
    # worked, `stuck` reset, and the card was sent a green step, on a browser
    # that was already gone.
    check("a page closed under its own click ends the run",
          leaked["closed note"], "TargetClosedError")
    check("and ends it through the re-raise rather than by tripping over the "
          "dead page in `_settle`",
          leaked["closed via"], "the re-raise")
    check("and a page closed under its own press ends the run too",
          leaked["press closed note"], "TargetClosedError")
    check("and ends it through the re-raise rather than by tripping over the "
          "dead page in `_settle`",
          leaked["press closed via"], "the re-raise")

print("\n== TAG_JS: numbering a page ==")
# The one part of this file that needs a renderer, because the whole of TAG_JS
# is geometry and DOM. No network: every page here is a `data:` URI, so this
# stays as offline as the rest. Skipped rather than failed when Chromium will
# not start, since a missing browser is a machine that cannot run `browse`
# either and this file is not where you would want to find that out.
async def tag(html: str, limit=browser.MAX_ELEMENTS, then=None):
    """Number `html`, optionally change it and number it again."""
    async with browser._Session() as session:
        await session.page.goto("data:text/html;charset=utf-8," + quote(html))
        listed = await session.page.evaluate(browser.TAG_JS, limit)
        if then is None:
            return listed, session.page
        await session.page.evaluate(then)
        listed = await session.page.evaluate(browser.TAG_JS, limit)
        return listed, await session.page.evaluate(
            "() => [...document.querySelectorAll('[data-agent-idx]')]"
            ".map(el => el.dataset.agentIdx)")


LINKS = "<a href=#>one</a><a href=#>two</a><a href=#>three</a>"
LOGIN = ("<form><input name=user placeholder=Username>"
         "<input type=password placeholder=Password></form>"
         "<form><input name=q placeholder=Search></form>")

try:
    _, indices = asyncio.run(tag(
        LINKS,
        then="() => document.querySelectorAll('a').forEach((el, n) => "
             "{ if (n < 2) el.style.display = 'none' })"))
except Exception as err:  # noqa: BLE001 - any launch failure is a skip
    print(f"   skipped: no Chromium to render with ({type(err).__name__})")
else:
    # The bug this exists for. A second pass numbers whatever is visible now,
    # and what scrolled or collapsed out of view kept the number it had — so
    # `[data-agent-idx="0"]` matched two elements and the run ended on a strict
    # mode violation rather than on an answer.
    check("a second pass renumbers rather than numbering on top",
          indices, ["0"])

    listed, _ = asyncio.run(tag(LOGIN))
    check("a password field is not offered", "Password" in listed, False)
    check("nor is the username beside it", "Username" in listed, False)
    check("but a form without one is", "Search" in listed, True)
    check("so the login form contributes nothing at all",
          listed, "0. <input> Search")
    # The two guards overlap for a password inside a form, so only this pins
    # the first one: plenty of login widgets are a div and a fetch, with no
    # `<form>` for the second guard to find.
    listed, _ = asyncio.run(tag("<input type=password placeholder=Password>"
                                "<input name=q placeholder=Search>"))
    check("and a password field outside any form is left out too",
          listed, "0. <input> Search")

    listed, _ = asyncio.run(tag(LINKS, limit=2))
    check("the list stops at the limit", listed.count("\n") + 1, 2)
    check("and it is the first two", listed.startswith("0. <a> one"), True)

    listed, _ = asyncio.run(tag(
        '<button aria-label=Buy value=v>Cheap</button>'
        '<input type=submit value="購入">'))
    check("aria-label wins over the text under it",
          listed.splitlines()[0], "0. <button> Buy")
    check("and value is read for a submit with no text",
          listed.splitlines()[1], "1. <input> 購入")

print("\n== act: what a miss actually costs, on a real locator ==")
# The stand-in above can say which number was passed; only playwright can say
# what happens when it is not. The default is 30 s, so before the `timeout=` on
# the label read this exact step took half a minute to come back with a note the
# model could have had in eight seconds — and `browse` gets 120 s in total.
async def missed_click():
    """Click an index nothing on the page answers, and time the note."""
    async with browser._Session() as session:
        await session.page.goto("data:text/html;charset=utf-8,"
                                + quote("<a href=#>one</a>"))
        start = time.monotonic()
        note, _ = await session.act({"act": "click", "idx": 99})
        return note, time.monotonic() - start


try:
    # Bounded from the outside as well, because `timeout=0` is not an error to
    # playwright — it means wait forever. Without this, getting that number
    # wrong does not fail this file, it wedges it.
    note, took = asyncio.run(asyncio.wait_for(missed_click(), 45))
except TimeoutError:
    check("an index nothing answers comes back as a note",
          "still waiting after 45 s", "a note")
except Exception as err:  # noqa: BLE001 - any launch failure is a skip
    print(f"   skipped: no Chromium to click in ({type(err).__name__})")
else:
    # Split, because a `data:` page is off every allowlist by scheme, so #41's
    # after-the-fact check appends its own half to the note. That half has its
    # own cases above; what this section is about is the first one.
    check("an index nothing answers comes back as a note", note.split(";")[0],
          "failed: click 99 did not land in time")
    # 10, not 8: the 8 s is what the locator waits, and the launch and the
    # `goto` are outside the clock but the teardown of the wait is not. A pass
    # at 30 s is the bug; a pass at 8.2 s is the fix.
    check(f"and it took {took:.1f} s, not playwright's 30", took < 10, True)

print("\n== style.css: the rules the browser kept ==")
# The brace invariants in `js_check.py` catch a block left open or closed
# twice. They cannot catch a selector the parser rejects: break `.map` to
# `.map:::` and the braces still balance, the tokens still resolve, no new
# at-rule appears, and the whole suite stays green while Chromium drops that
# one rule and renders the rest. Only a real parse can say how many rules
# came through, and this file already pays for the launch.
#
# Every `{` in the stripped stylesheet opens exactly one rule, at any depth,
# because a declaration block holds no braces of its own. So what the file
# writes is `css_text().count("{")` and what the browser kept is the same
# count over the CSSOM. `@media` and `@keyframes` each arrive as one rule
# with their children nested under it, which is why the walk recurses — and
# why the two agree outright rather than by a constant. Measured here: 276
# and 276. A braceless at-rule would not agree — `@import` is one
# `CSSImportRule` with no `{` to count — and what keeps that out is
# `js_check.py`, which already refuses every at-rule but those two.
#
# The sheet is injected inline rather than loaded the way the server serves
# it. A stylesheet linked from a `file://` page is cross-origin to the CSSOM
# and reading `cssRules` raises SecurityError, and `index.html` pulls Google
# Fonts, which nothing in this file goes on the network for.
#
# What this deliberately does not ask is whether a selector matches
# anything. Measured at `77e84fc`, 179 of them match nothing on the
# `index.html` beside it — `.wx-rain`, `.mic-btn.on`, `.place-card .map` —
# because the cards are built by `cards.js` at runtime and the state classes
# are put on by `app.js`. A gate that went red on those would be red the day
# it landed and gone the week after.
RULE_COUNT_JS = """() => {
  const walk = (rules) => [...rules].reduce(
      (n, rule) => n + 1 + (rule.cssRules ? walk(rule.cssRules) : 0), 0);
  return walk(document.styleSheets[0].cssRules);
}"""

STYLE = (Path(__file__).resolve().parent.parent
         / "src/ai_terminal/live/static/css/style.css")


async def kept_rules(*sheets):
    """Parse each stylesheet in one Chromium; count what survived each."""
    async with browser._Session() as session:
        counts = []
        for sheet in sheets:
            await session.page.set_content(f"<style>{sheet}</style>")
            counts.append(await session.page.evaluate(RULE_COUNT_JS))
        return counts


written = js_check.css_text().count("{")
style = STYLE.read_text(encoding="utf-8")
try:
    kept, plus, minus = asyncio.run(kept_rules(
        style,
        style + "\n.map { color: red; }\n",
        style + "\n.map::: { color: red; }\n"))
except Exception as err:  # noqa: BLE001 - any launch failure is a skip
    print(f"   skipped: no Chromium to parse with ({type(err).__name__})")
else:
    check(f"the browser kept all {written} rules style.css writes",
          kept, written)
    # The pair below is the control, and it is not a formality: a walk that
    # had quietly stopped counting would sit at whatever number the case
    # above wants and never move again. Both rules are appended rather than
    # edited into place, so neither depends on anything already in the file.
    check("a rule it can parse is one more than that", plus, kept + 1)
    check("and one whose selector it cannot is not", minus, kept)

print("\n== the forecast row's day label fits the track it is given ==")
# Here and not in `js_check.py`, where the rest of the front end is checked,
# because this one needs a layout engine: `js_check` runs the scripts under
# node against hand-written DOM stubs, and a stub has no text to measure. The
# Chromium above is the only thing in the repo that has already parsed
# `style.css`, so this borrows it.
#
# What went wrong without it (#171): naming the first row *Tomorrow* put an
# eight-character word into a track sized for *Today*, and `.wx-d-label` has
# no rule of its own while `.wx-day` sets no `overflow`, `white-space` or
# `min-width` — so the label drew over the icon beside it instead of
# clipping. Japanese fits either way, which is why the panel this runs on did
# not show it and the answer and the card disagreed in English only.
#
# What is pinned is the property rather than the number: every day name the
# server can put in that slot, measured, inside track 1 as the browser
# resolved it. A language added to `_WEEKDAYS` with a longer word turns this
# red on its own, which is the case that would not exist if the track width
# were asserted directly.
WX_LABELS = sorted({name
                    for table in (weather._NAMED, weather._WEEKDAYS)
                    for names in table.values()
                    for name in names})
# One row of the real markup, from `wxDays` in `cards.js`. The label is the
# first of the four children, which is what makes track 1 its track, and the
# other three are here because a grid with one item resolves differently.
WX_ROW = """<div class="wx-days"><div class="wx-day">
  <span class="wx-d-label">%s</span>
  <span class="material-symbols-outlined">wb_sunny</span>
  <span class="wx-d-pop">10%%</span>
  <span class="wx-d-range"><b>28°</b> / 19°</span>
</div></div>"""
# A `Range` over the text and not the span's own box: a span that is a direct
# child of a grid container is blockified into a grid item and stretched to
# its track, so its bounding box reports 5.2em whatever it holds and the
# overflow is invisible to it. The range measures the glyphs.
WX_FIT_JS = """() => {
  const label = document.querySelector('.wx-d-label');
  const range = document.createRange();
  range.selectNodeContents(label);
  const track = getComputedStyle(document.querySelector('.wx-day'))
      .gridTemplateColumns.split(' ')[0];
  return [range.getBoundingClientRect().width, parseFloat(track)];
}"""
# The width this shipped with. Appended rather than edited in, so it does not
# have to know where the rule it overrides lives.
WX_WAS = "\n.wx-day { grid-template-columns: 3.5em 24px 3em 1fr; }\n"


async def overflowing(*sheets):
    """Under each stylesheet, name the day labels wider than track 1."""
    session = browser._Session()
    try:
        await session.__aenter__()
    except Exception as err:  # noqa: BLE001 - any launch failure is a skip
        print(f"   skipped: no Chromium to measure in ({type(err).__name__})")
        return None
    try:
        out = []
        for sheet in sheets:
            over = []
            for name in WX_LABELS:
                await session.page.set_content(
                    f"<style>{sheet}</style>" + WX_ROW % name)
                text, track = await session.page.evaluate(WX_FIT_JS)
                # 1.07 covers font-substitution margin for DejaVu Sans on Pi
                # (resolved system-ui is SF on macOS; DejaVu Sans runs up to
                # +6.9% wider on English weekday labels).
                if text * 1.07 > track:
                    over.append(name)
            out.append(over)
        return out
    finally:
        await session.__aexit__(None, None, None)


measured = asyncio.run(overflowing(style, style + WX_WAS))
if measured is not None:
    fits, was = measured
    check(f"all {len(WX_LABELS)} day names the server can send fit the label "
          "track", fits, [])
    # The control proves Chromium is measuring at all rather than comparing
    # two numbers that are both wrong: shrink the track back and the one word
    # that started this comes back out. `js_check.py:7518` pins the measurement
    # to the shipped rule.
    check("and at the 3.5em it shipped with, one of them did not", was,
          ["Tomorrow"])

print("\n== a job that crashed still ends its card ==")
# The one section here that runs the tool rather than the browser, because the
# frame it is about is sent from `tools/browse.py`'s `except` and nowhere else.
# The card has exactly one terminal frame and until now only the last line of
# the happy path sent it, so a job that raised after its first screenshot left
# the spinner running for the rest of the session — beside a voice that had
# already said the page could not be read.
from ai_terminal.live import conn, screen  # noqa: E402
from ai_terminal.live.tools import browse as browse_tool  # noqa: E402
from ai_terminal.live.tools import _progress  # noqa: E402


class FakeContext:
    """What a `ToolContext` is, as far as `browse` reads one."""

    def __init__(self, session_id="live-1", call_id="call-browse"):
        self.session = type("S", (), {"id": session_id, "events": []})()
        self.function_call_id = call_id
        self._invocation_context = type("I", (), {"live_request_queue": None})()


async def browsed(crash: bool, callers=("call-browse",), deaf=False) -> tuple:
    """What the tool answered, and every frame it pushed getting there.

    `callers` are the call ids asking for the same task at the same time. Only
    the first starts the browser; the rest ride its job and have no card, so
    every frame in the list should carry the first one's id.

    `deaf` is a screen that has gone away — the socket dropped and every write
    to it raises.
    """
    frames: list[dict] = []

    async def to_screen(frame):
        if deaf:
            raise RuntimeError("Cannot call send once a close message has been "
                               "sent")
        frames.append(frame)

    async def stub(task, lang, context="", sink=None):
        # Sink errors swallowed the way `browser.run`'s own `emit` swallows
        # them, so a dead screen fails the tool's write and not the browser's.
        with contextlib.suppress(Exception):
            await sink({"type": "browse", "phase": "step", "step": 1,
                        "url": "https://shop.test/", "shot": "data:x"})
        if crash:
            raise RuntimeError("Target page, context or browser has been closed")
        # The last line of the happy path, which is where the card's only
        # terminal frame used to come from. Copied rather than skipped: without
        # it the case below cannot tell "one done frame" from "none at all".
        result = {"answer": "Open 10:00-18:00.",
                  "url": "https://shop.test/hours", "seconds": 21}
        with contextlib.suppress(Exception):
            await sink({"type": "browse", "phase": "done", **result})
        return result

    real = browser.run
    browser.run = stub
    screen.register("live-1", to_screen)
    browse_tool.open_session("live-1")
    # The tool logs the crash with `logger.exception`, which prints the stack to
    # stderr. Expected here, and unhelpful: a check run that ends in `all N
    # cases hold` should not also look like it fell over, and the mutation
    # driver reads any traceback as a case that never ran.
    quiet = logging.getLogger(browse_tool.__name__)
    quiet.disabled = True
    try:
        # Started in order, so the first id in `callers` is the one that finds
        # `_JOBS` empty and becomes the owner.
        out = await asyncio.gather(
            *(browse_tool.browse("opening hours", FakeContext(call_id=call))
              for call in callers))
        return (out[0] if len(out) == 1 else out), frames
    finally:
        quiet.disabled = False
        browse_tool.close_session("live-1")
        screen.unregister("live-1")
        browser.run = real


out, frames = asyncio.run(browsed(crash=True))
check("the crash still reaches the model as an error",
      out.get("error", "").startswith("The browser could not read that:"), True)
check("and the screen got a terminal frame after the step",
      [f.get("phase") for f in frames], ["step", "done"])
check("stamped with the call id, so it lands in the right card",
      frames[-1].get("id"), "call-browse")
# Thin on purpose: no `shot`, so the last screenshot stays on the card, and no
# English — the label is the client's, in the language of the turn.
check("carrying nothing that would overwrite what is already there",
      {k: v for k, v in frames[-1].items() if k != "id"},
      {"type": "browse", "phase": "done", "url": "", "answer": ""})

out, frames = asyncio.run(browsed(crash=False))
check("a run that worked answers as it always did",
      out, {"answer": "Open 10:00-18:00.", "url": "https://shop.test/hours",
            "seconds": 21})
check("and sends the one done frame it always did, not two",
      [f.get("phase") for f in frames].count("done"), 1)

# Only the call that started the job has a card: the others ride the same
# `asyncio.Task` and their own `watch` is never handed to the browser. So a
# frame sent from the tool rather than from `browser.run` has to ask whether it
# is the owner — otherwise each duplicate's crash frame arrives under an id the
# client has never seen and `handleBrowse` draws it a card of its own, with no
# screenshot and nothing on it but the failure line.
out, frames = asyncio.run(
    browsed(crash=True, callers=("call-A", "call-B", "call-C")))
check("every duplicate is told the browser failed",
      [o.get("error", "").startswith("The browser could not read that:")
       for o in out], [True] * 3)
check("but the crash frame comes from the owner alone, once",
      [f.get("id") for f in frames], ["call-A", "call-A"])
check("and it is still the terminal frame, not a second step",
      [f.get("phase") for f in frames], ["step", "done"])


async def cancelled(deaf: bool = False, early: bool = False,
                    drop: bool = True, twice: bool = False,
                    crash: bool = False, cancel_job: bool = False) -> tuple:
    """Cancel a browse mid-flight; report what the screen was left holding.

    The other way a job ends without an answer, and the one that outlives the
    page: the socket drops, the turn is cancelled, and the card has no deadline
    of its own to fall back on — `app.js`'s `LATE_TOOLS` gives `gws` and `plan`
    a number and can give browse none, since a run may overrun
    `BROWSE_BUDGET_S` by a whole step. So the `done` frame is the only thing
    that ends it, and the `except CancelledError` used to re-raise without one.

    There are two of those, because there are two awaits a cancel can land on.
    `notes.watch` is the one it lands on in life — it runs for the whole job —
    and `asyncio.shield` is the sliver between the notes stopping and the
    answer arriving. `early` returns from the notes at once, which is the only
    way to get a cancel to the second one on purpose.

    `deaf` is a screen that has gone away, which is what a cancel usually means
    in the first place: the frame must be attempted and its failure swallowed.

    `drop` is which of the two cancellations this is, and they do not have the
    same answer. A socket dropping goes through `close_session` first, so the
    browser job is cancelled and the card is the tool's to end — that is the
    default, and it is what `main.py` does on the way out. A `plan` timeout
    cancels only the call: `_JOBS` is keyed on the live session, the job runs
    on, and it ends its own card. A frame from the tool there is a second card.

    `twice` is the cancel that arrives while the frame is still going out — the
    runner tearing the tool task down behind `close_session`. It parks the send
    on its way to the screen, cancels a second time, and only then lets it go,
    so a frame that is not detached from this task never arrives.
    """
    frames = []
    stepped, release = asyncio.Event(), asyncio.Event()
    sending, held = asyncio.Event(), asyncio.Event()

    async def to_screen(frame):
        if deaf and frame.get("phase") == "done":
            raise RuntimeError("Cannot call send once a close message has been "
                               "sent")
        if twice and frame.get("phase") == "done":
            # Where a real one waits: `main.py` takes a lock before
            # `send_text`, so the second cancel has a point to land on.
            sending.set()
            await held.wait()
        frames.append(frame)

    async def stub(task, lang, context="", sink=None):
        await sink({"type": "browse", "phase": "step", "step": 1,
                    "url": "https://shop.test/", "shot": "data:x"})
        stepped.set()
        # Still reading when the cancel arrives, which is the whole point: a
        # job that had already returned would never reach the `except`.
        await release.wait()
        if crash:
            raise RuntimeError("Target page, context or browser has "
                               "been closed")
        return {"answer": "", "url": "", "seconds": 0}

    async def no_notes(self, work):
        return

    real, watching = browser.run, _progress.Notes.watch
    browser.run = stub
    if early:
        _progress.Notes.watch = no_notes
    screen.register("live-1", to_screen)
    browse_tool.open_session("live-1")
    try:
        job = asyncio.create_task(
            browse_tool.browse("opening hours", FakeContext()))
        await stepped.wait()
        running = browse_tool.jobs_of("live-1")["opening hours"]
        # A turn of the loop, so `browse` is parked on the await this run is
        # about rather than still on its way there.
        await asyncio.sleep(0)
        if drop:
            # In `main.py`'s order: the connection leaving cancels the browser
            # job, and the call goes down behind it. Cancelling the call alone
            # is a different event with a different right answer — see `drop`.
            browse_tool.close_session("live-1")
        job.cancel()
        if twice:
            await sending.wait()
            job.cancel()
            held.set()
        try:
            await job
            raised = "<nothing>"
        except asyncio.CancelledError:
            raised = "CancelledError"
        except Exception as err:  # noqa: BLE001 - reported, not fatal
            raised = type(err).__name__
        if crash:
            # The call is gone; the job is not. Let it get to the crash and
            # take its exception, which is what the awaiting caller would have
            # done had there still been one.
            release.set()
            with contextlib.suppress(Exception):
                await running
        if cancel_job:
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running
        if twice:
            # The shielded send is a task of its own now, so the frame lands a
            # turn or two after the call it was sent from has finished dying.
            for _ in range(3):
                await asyncio.sleep(0)
        return raised, frames
    finally:
        # The browser is shielded, so it is still sitting on `release` — let it
        # finish rather than leaving a task to be reaped at interpreter exit.
        release.set()
        browse_tool.close_session("live-1")
        screen.unregister("live-1")
        browser.run = real
        _progress.Notes.watch = watching


raised, frames = asyncio.run(cancelled())
# Cancellation has to keep propagating: it is how the turn ends, and swallowing
# it here would leave the live agent waiting on a call that is not coming.
check("a cancelled job still cancels", raised, "CancelledError")
# A browse card has no deadline of its own, so this frame is all there is.
check("but the card is ended on the way out",
      [f.get("phase") for f in frames], ["step", "done"])
check("with the same thin frame the crash sends",
      {k: v for k, v in frames[-1].items() if k != "id"},
      {"type": "browse", "phase": "done", "url": "", "answer": ""})

# The other await, reached only when the notes are already done. Two handlers
# rather than one `try` around both, because a progress panel that raised is
# not a browser that failed and must not be reported as one — so each has to
# be shown to work on its own.
raised, frames = asyncio.run(cancelled(early=True))
check("cancelled in the gap after the notes, the card is ended too",
      (raised, [f.get("phase") for f in frames]),
      ("CancelledError", ["step", "done"]))

# And the usual reason for a cancel is the screen that would receive the frame
# having gone away. The send has to be tried and its failure swallowed, or the
# cancellation the caller is waiting on is replaced by a socket error.
raised, frames = asyncio.run(cancelled(deaf=True))
check("a screen that has gone away does not change how the turn ends",
      (raised, [f.get("phase") for f in frames]),
      ("CancelledError", ["step"]))

# The cancellation that is not a socket drop. `plan`'s 250 s timeout cancels
# the planner's *call*; `_JOBS` is keyed on the live session, so the job is not
# in the timeout's reach and goes on reading. It sends its own `done` at the
# end of `browser.run`, under the same id — so a frame from the tool here is
# not a card ended, it is a card ended early and a second, screenshotless one
# built beside it when the real answer lands. `handleBrowse` gives up the id on
# whichever `done` arrives first.
raised, frames = asyncio.run(cancelled(drop=False))
check("a job that outlives the call ends its own card, so the tool sends none",
      (raised, [f.get("phase") for f in frames]),
      ("CancelledError", ["step"]))
# And in the sliver too, which is where a `plan` timeout is most likely to
# land: the notes stop when the job's own progress does, and the timeout is
# counted from before the call. Each handler decides for itself, so each has to
# be shown to.
raised, frames = asyncio.run(cancelled(early=True, drop=False))
check("in the gap after the notes it is still the job's card, not the tool's",
      (raised, [f.get("phase") for f in frames]),
      ("CancelledError", ["step"]))

# The same job, crashing instead of answering. `browser.run` sends its
# `done` on the last line of the happy path, so a crash never reaches one,
# and the call that would have sent one from the tool was cancelled a
# minute ago. Nobody is left to end the card unless the job ends its own.
raised, frames = asyncio.run(cancelled(drop=False, crash=True))
check("a job that outlives its call and then crashes still ends the card",
      (raised, [f.get("phase") for f in frames]),
      ("CancelledError", ["step", "done"]))

# The same job, cancelled instead of answering. Task cancellation in
# `read()` must end the card on the way out too.
raised, frames = asyncio.run(cancelled(drop=False, cancel_job=True))
check("a job that outlives its call and is cancelled still ends the card",
      (raised, [f.get("phase") for f in frames]),
      ("CancelledError", ["step", "done"]))

# One cancel is what the tool is written around; two is what it gets. The
# runner tears the tool task down behind `close_session`, and the frame is
# mid-flight when it does — `main.py` takes a lock before `send_text`, which is
# a point to be cancelled at. `suppress(Exception)` cannot help: a
# `CancelledError` is a `BaseException`.
raised, frames = asyncio.run(cancelled(twice=True))
check("a second cancel does not take the frame with it",
      (raised, [f.get("phase") for f in frames]),
      ("CancelledError", ["step", "done"]))

# `watch` reaches `websocket.send_text` with nothing between, and this is the
# one caller inside an `except`: a raise here replaces the error the model was
# about to be handed with a socket error nobody catches, and the
# FunctionResponse for a NON_BLOCKING call never arrives at all. The turn ends
# with the person waiting on an answer that is not coming.
try:
    out, frames = asyncio.run(browsed(crash=True, deaf=True))
except Exception as escaped:  # noqa: BLE001 - which is the failure, not a crash
    # Caught so the failure reads as a named case rather than as this file
    # falling over. What escapes here is what would escape `browse` in the
    # server, where nothing is waiting to catch it either.
    out, frames = {"error": f"<{type(escaped).__name__} escaped>"}, []
check("a screen that has gone away does not cost the model its answer",
      out.get("error", "").startswith("The browser could not read that:"), True)
check("and nothing reached it, there being nothing there", frames, [])

# The same page, asked for again later. `_JOBS` is what decides whether a call
# starts a browser or rides one, and a finished `asyncio.Task` is still a
# perfectly good value to find in it: without the done-callback that takes it
# back out, the second call reads `mine = False`, never runs the browser, and
# `await asyncio.shield(job)` hands back the first answer at once. The person
# gets an hour-old page, in silence, with no card. See #55.
async def asked_twice() -> tuple[list[str], int, list[str]]:
    runs: list[str] = []
    frames: list[dict] = []

    async def to_screen(frame):
        frames.append(frame)

    async def stub(task, lang, context="", sink=None):
        runs.append(task)
        result = {"answer": f"answer {len(runs)}",
                  "url": "https://shop.test/hours", "seconds": 1}
        await sink({"type": "browse", "phase": "done", **result})
        return result

    real = browser.run
    browser.run = stub
    screen.register("live-1", to_screen)
    browse_tool.open_session("live-1")
    try:
        first = await browse_tool.browse(
            "opening hours", FakeContext(call_id="call-1"))
        second = await browse_tool.browse(
            "opening hours", FakeContext(call_id="call-2"))
        return ([first.get("answer"), second.get("answer")],
                len(browse_tool.jobs_of("live-1")),
                [f.get("id") for f in frames])
    finally:
        browse_tool.close_session("live-1")
        screen.unregister("live-1")
        browser.run = real


answers, left, drawn = asyncio.run(asked_twice())
check("asking for the same page twice reads it twice",
      answers, ["answer 1", "answer 2"])
check("and the second has a card of its own", drawn, ["call-1", "call-2"])
check("with nothing of either left behind to be ridden", left, 0)

# Two pages on one profile — the kiosk in the kitchen and a tab on the Mac
# serving it, which is the ordinary development topology and the one that
# produced #127. They share a session id (#19), and the tab is closed while the
# kiosk stays up. Keyed on the id alone, the tab's `open_session` replaced the
# kiosk's entry and its `close_session` then popped that entry as its own, so
# `_JOBS.get` answered None on a terminal that was still connected — and the
# guard turned that into "No screen is connected", which #127 had just made
# false. A reload is the only way out and nobody reloads a kiosk.
print("\n== a second page on the profile closes, and the kiosk browses ==")


async def after_the_other_page_left() -> tuple:
    frames: list[dict] = []

    async def to_screen(frame):
        frames.append(frame)

    async def stub(task, lang, context="", sink=None):
        result = {"answer": "Open 10:00-18:00.",
                  "url": "https://shop.test/hours", "seconds": 1}
        await sink({"type": "browse", "phase": "done", **result})
        return result

    real = browser.run
    browser.run = stub
    kiosk = browse_tool.open_session("live-1", "c-kiosk")
    laptop = browse_tool.open_session("live-1", "c-laptop")
    screen.register("live-1", to_screen)
    try:
        browse_tool.close_session("live-1", laptop)
        answered = await browse_tool.browse(
            "opening hours", FakeContext(call_id="call-1"))
        # And the job it starts is the kiosk's own, so the *next* page to open
        # and close cannot cancel a browser the kiosk is waiting on.
        with conn.speaking("c-kiosk"):
            during = browse_tool.jobs_of("live-1") is kiosk
        return answered, during
    finally:
        browse_tool.close_session("live-1", kiosk)
        screen.unregister("live-1", to_screen)
        browser.run = real


answered, during = asyncio.run(after_the_other_page_left())
# Not the error: `_JOBS` answering None here is a terminal being told "No
# screen is connected" for the rest of its life by a guard whose other half —
# the screen — #127 already fixed.
check("the kiosk can still browse after the other page closes",
      answered.get("answer"), "Open 10:00-18:00.")
check("and its jobs are filed under the kiosk, not under whoever is newest",
      during, True)

print("\n== _data_uri ==")
check("no screenshot is no data URI", browser._data_uri(None), "")
check("and an empty one is not a URI either", browser._data_uri(b""), "")
check("a real one is base64 with the jpeg prefix",
      browser._data_uri(b"\xff\xd8\xff"), "data:image/jpeg;base64,/9j/")

print(f"\n{'SOME CASES FAILED' if failed else f'all {ran} cases hold'}")
sys.exit(1 if failed else 0)
