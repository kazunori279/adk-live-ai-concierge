"""Check what the clock page is served when nobody is talking to the terminal.

#84 put a clock on the panel and wrote it to ask the server what to wear. This
is the server's answer, and almost everything worth getting wrong about it is
a state the household will actually be in:

- **An empty directory.** It is the state of every terminal for the first ten
  minutes after boot, and of one whose WAN never came back for as long as it
  does not. `/api/config`
  has a value for everything it returns; this endpoint does not, and the
  difference has to be a 200 with less in it rather than a 500. The
  photograph's route has the same problem one layer down, where the honest
  answer is a 404 and the accident is a 500 —
  `FileResponse` raises `RuntimeError` from inside the response when the file
  is not there (`starlette/responses.py:353`), which is a stack trace in the
  log every time the clock comes up on a terminal that has no picture.
- **A payload with fields nothing on the page reads.** There are two
  readers since #88: `paintScreensaver` takes an image URL, a place and a
  temperature with a word for the sky, and `paintBarWeather` takes the
  temperature again with the sky as a slug and a day/night flag. js_check
  pins every one of them. Anything else in the JSON is a promise kept to
  nobody, so the keys are checked exactly rather than by containment — and it
  is why the generator's own view of the weather is a second dict rather than
  four more keys in that one.
- **The word for the sky, and which language it is in.** One process serves
  every panel and the language is per page (#96), so the word cannot be
  settled at the fetch: the reading keeps the WMO code, and `payload(lang)`
  turns it into a word per request. The cases that matter are the ones a
  slug-keyed table in the page would pass and this has to as well —
  `Heavy rain` and `Light rain` are both `rain`, and both have to survive the
  round trip. A tag nobody has a table for is English rather than a blank
  line, and so is no tag at all, which is what a page too old to send one
  sends.
- **The slug beside the word.** The top bar has no room for a word, so it
  draws a glyph off `condition` through the same nine-slug table the weather
  card is keyed on. A payload that carries the word and drops the slug is a
  bar with no sky in it and nothing that says so.
- **Which position the reading is for.** Three rungs since #131, and the new
  one is the map pin in the settings sheet, which used to move the agent and
  leave the clock on `HOME_LOCATION`. Kept across the socket that carried it,
  because the refresh loop has no page attached — and only ever replaced by
  another position, never cleared by a second page that has no opinion.
- **Which page that position belongs to.** One process, one clock, and as
  many pages as the household opens. A pin dropped on a phone in another
  city must not move the panel on the kitchen wall, so a connected panel
  owns the position while it is connected — asked of the screen registry on
  every write rather than remembered, which is what makes it lapse when the
  panel is unplugged instead of freezing the clock until a restart.
- **The `?v=` on the image URL.** It has to change when the picture changes,
  or Chromium serves yesterday's out of cache; and it has to *not* change when
  the picture has not, because the face only assigns `src` when the string
  differs and re-assigning it blinks the gradient on the panel. Both
  directions are checked, because only one of them is ever noticed.
- **A path off disk.** The route takes no arguments, which is the whole of the
  defence and is therefore worth a case rather than a sentence: a decoy sits
  one directory up and no request can produce it.
- **What the route calls the file.** The path ends `.jpg` and the model draws
  PNG, and Chromium sniffs an `<img>` and renders it either way — so a case
  that writes JPEG magic and reads back `image/jpeg` passes on a route that
  answers with a constant. Every case about the type writes bytes the old
  answer was wrong about (#110).
- **The next deploy.** The picture is kept under the working directory to
  stay out of `static/`, which the deploy copies over. The working directory
  is where that same `rsync --delete` copies *to*, so the placement is worth
  nothing unless the name is one of its `--exclude`s — and a deploy that took
  the picture and the sidecar leaves a terminal behaving exactly as designed,
  buying a fresh 2K generation for a slot that already had one.
- **A background task that dies.** The face fetches from `sleep()` and nowhere
  else, so a refresh loop that stopped at 04:00 is invisible until somebody
  reads the temperature over breakfast and it is eight hours old. The loop
  must outlive the thing it calls throwing.
- **Where to ask about.** Two rungs, and the failure of both is a designed
  state too — a warning naming the variable to set, not an exception in a
  task nobody is watching.
- **A picture that costs money.** #86 spends a 2K generation seven times a
  local day, forever, on an appliance nobody is watching. So the cases that
  matter are the ones about *not* spending one: a slot that has already had
  its picture, a failure that leaves both `last_slot` and the old bytes
  exactly where they were, and an answer with no bytes in it that must not
  become a 0-byte file the panel then tries to draw.
- **A schedule that bunches.** Six of the seven slots hang off sunrise and
  sunset, and Open-Meteo answers a day the sun does not rise with `00:00` at
  both ends rather than a null. Read one at a time those are times, and the
  six collapse into four pictures inside two hours around midnight with
  three of them duplicates. Read as a pair they are the fact that there is
  no sunrise, and the day gets its `night` picture and nothing else.
- **A picture that is the same one twice.** The place is picked here rather
  than by the model, so "not Kyoto eleven times running" is a property of
  this repo and is checked like one.

`weather._fetch` is stubbed, so no case reaches Open-Meteo,
`weather._geocode` is stubbed, so no case reaches a model, and
`screensaver._generate_sync` is stubbed, so no case draws a picture. That
last one is not only about the reviewer's bill: a check script that costs
money per run is a check script that stops being run. `TestClient` is
in-process: nothing here opens a port.

    uv run python tools/screensaver_check.py

Exits non-zero on the first case that does not hold.
"""

import ast
import asyncio
import contextlib
import importlib.util
import inspect
import json
import logging
import os
import random
import re
import sys
import tempfile
import threading
import time
import warnings
from datetime import date, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Starlette 1.6 wants httpx2 and this tree has httpx 0.28. The client works;
# the warning is one line of somebody else's roadmap in the middle of a suite
# whose output is meant to be read.
warnings.filterwarnings("ignore", message=".*httpx.*")

from google.genai import types  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from ai_terminal.live.tools import gws_cache, weather  # noqa: E402

# Kept here because `main_async` stubs `weather._fetch` for the whole run, and
# `fetch_cases` is the one section that wants the real one — its subject is
# what `_fetch` puts on the wire and what it does with a refusal, which the
# stub is standing in front of. Nothing here ever reaches Open-Meteo:
# `requests.get` under it is stubbed too.
_FETCH = weather._fetch

# And the real `_geocode`, for the same reason: `main_async` replaces it with
# one that always answers Hakone, and `geocode_log_cases` is about the two
# lines it writes when it cannot answer at all.
_GEOCODE = weather._geocode

# Set before `main` is imported, because that is when it says the three true
# things about a machine with no credentials — and after it, `basicConfig`
# has already turned httpx's per-request line on. Half the cases here are
# failures on purpose and every one of them logs; none of the logging is what
# is being checked.
logging.getLogger("ai_terminal").setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.WARNING)

# What the client calls the server. `TestClient`'s own default is
# `http://testserver`, and since #95 the server answers to its own two names
# and refuses every other one with a 400 before the request reaches a route —
# so a client left on the default would measure the host guard here instead of
# the clock. This is the name the kiosk uses under both topologies.
OUR_URL = "http://localhost:8000"

# Counted rather than written down, so the closing line cannot drift from the
# number of cases the way a figure typed into a commit message does.
RAN = 0


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got!r}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


@contextlib.contextmanager
def env(**names):
    """Set or unset environment variables for the block, then put them back."""
    kept = {name: os.environ.get(name) for name in names}
    try:
        for name, value in names.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in kept.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def tz(name: str):
    """Stand the process in another timezone for the block, then put it back.

    For the one case that is about the clocks moving. Everything else here
    is timezone-agnostic on purpose — the slots are local wall times on both
    sides and nothing in the module converts — but that is a claim, and the
    only way to check a claim about local time is to be somewhere it bites.
    """
    kept = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if kept is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = kept
        time.tzset()


# One Open-Meteo forecast, with the fields `_weather_sync` reads and none of
# the ones it only passes through. 24.1 and code 3 are the pair js_check
# already draws the face from, so the two halves of this feature are checked
# against the same numbers and a disagreement shows as a disagreement.
FORECAST = {
    "timezone": "Asia/Tokyo",
    "current_units": {"temperature_2m": "°C"},
    "current": {
        "time": "2026-09-09T15:00",
        "temperature_2m": 24.1,
        "weather_code": 3,
        "is_day": 1,
    },
    "hourly": {},
    # The two the picture's schedule hangs off, and the week behind them the
    # picture is drawn from. Local stamps, because the request carries
    # `timezone=auto` and nothing downstream converts.
    #
    # `time` runs from seven days *before* the current reading, which is what
    # `past_days=7` returns and the shape everything downstream has to cut at
    # today: index 0 of these arrays is 2026-09-02, not this morning.
    "daily": {
        "time": [f"2026-09-{day:02d}" for day in range(2, 14)],
        "weather_code": [3] * 12,
        "precipitation_probability_max": [10] * 12,
        "temperature_2m_max": [29.0, 30.5, 31.2, 33.4, 30.0, 28.8, 31.1,
                               32.0, 31.5, 30.9, 29.8, 30.2],
        "temperature_2m_min": [24.4, 25.1, 25.9, 26.2, 24.8, 24.1, 25.5,
                               25.8, 25.2, 24.9, 24.3, 24.6],
        "sunrise": [f"2026-09-{day:02d}T05:{14 + day:02d}"
                    for day in range(2, 14)],
        "sunset": [f"2026-09-{day:02d}T17:{43 + day:02d}"
                   for day in range(2, 14)],
    },
}

# The week `_recent` makes of the seven days before 2026-09-09 in `FORECAST`:
# 24.1 at the coldest, 33.4 at the warmest, and the midpoints averaged. Early
# September in Tokyo, which is the reading #185 was reported on.
RECENT = {"days": 7, "low": 24.1, "high": 33.4, "mean": 27.9}

# How many of `FORECAST`'s twelve days are behind today. The rest are the
# five the card draws.
PAST = 7


def forecast(past_days: int = 0) -> dict:
    """`FORECAST` with as much history in it as the caller asked for.

    Open-Meteo prepends `past_days` days to every array in `daily` and sends
    none at all when it is zero, and this is the half of that behaviour that
    matters to anything here. A stub that hands the whole week back however
    it is called cannot tell a caller that asked from one that did not — and
    two of the things this file now checks are exactly that distinction: the
    card path's refusal to cut (`weather._weather_sync`) and the once-a-day
    ceiling on how often the screensaver asks. Both would look identical to
    the versions without them.
    """
    keep = PAST - min(max(past_days, 0), PAST)
    return {**FORECAST,
            "daily": {key: value[keep:] if isinstance(value, list) else value
                      for key, value in FORECAST["daily"].items()}}

HAKONE = (35.232, 139.107, "Hakone, Japan")

JPEG = b"\xff\xd8\xff\xe0 not really a jpeg, but it is the bytes on disk"
DECOY = b"the file one directory up that no request may reach"

# The other things that can be under `background.jpg`, because what the route
# calls the file has to come off the file. PNG is not hypothetical — it is
# what the model draws and what is on the Pi right now. WEBP is here for its
# magic, eight bytes in behind a RIFF header, which is the one a `startswith`
# table gets wrong. The last is any of the ways a picture is not one.
PNG = b"\x89PNG\r\n\x1a\n a picture the name on disk calls a jpeg"
WEBP = b"RIFF\x20\x00\x00\x00WEBP and a third thing it is not"
NOT_AN_IMAGE = b"<!doctype html><title>whatever came back instead</title>"

# What the model is pretending to have drawn. Two of them, because half of
# what is checked below is which one is still on disk after something failed.
OLD = b"\x89PNG\r\n\x1a\n the picture the panel is already wearing"
NEW = b"\x89PNG\r\n\x1a\n the one it should be wearing instead"

# One place off the list, named here so the prompt case reads as a sentence.
BLED = "the shore of Lake Bled, Slovenia"

# What `_seen` looks like after a refresh: Hakone, mid-afternoon, the same
# 24.1 and the same overcast sky the payload cases draw the face from, and
# the sunrise and sunset the seven slots are cut from.
SEEN = {
    "is_day": True,
    "temperature": 24.1,
    "description": "Overcast",
    "recent": RECENT,
    "sunrise": "05:23",
    "sunset": "17:52",
}

# The same day with the sun up an hour later, so that `dawn-1` is 05:30 and
# `dawn` is 06:30 and there is a gap between them for a Pi to reboot in.
LATE_SUN = dict(SEEN, sunrise="06:30", sunset="16:40")

# One audio frame: 128 samples at 16 kHz. The recorder's worklet posts every
# render quantum whether the room is talking or not
# (`static/js/pcm-recorder-processor.js:9-13`) at the context's rate
# (`static/js/audio-recorder.js:8`), so this is how often the event loop that
# also runs the screensaver has a frame to move.
FRAME_S = 128 / 16000

# Stands in for a disk slower than the one under this file. Writing 8 MB to
# APFS is a millisecond or two, which is under one frame and would show
# nothing; the Pi's storage after #10, at a plausible microSD figure, is most
# of a second for the same bytes. Long enough here to be unambiguous, short
# enough that the suite does not notice spending it.
SLOW_WRITE_S = 0.15


def at(hour: float) -> float:
    """A timestamp at `hour` on one fixed January day.

    Built from a date rather than written down as an epoch second, because
    `generate_once` reads the month off it for the season and an epoch second
    is a different month in a different timezone. Every case that mentions a
    clock takes one of these: nothing here reads the wall clock, and nothing
    here sleeps for real.
    """
    return datetime(2026, 1, 14).timestamp() + hour * 3600


def place_in(screensaver, prompt: str) -> str:
    """Which place off the list the prompt is about, or "" if none is."""
    for place in screensaver.PLACES:
        if place in prompt:
            return place
    return ""


def answered(*parts) -> types.GenerateContentResponse:
    """A `generate_content` response carrying exactly these parts."""
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(parts=list(parts)))])


# ------------------------------------------------- the pieces exist at all

def existence_cases():
    """That there is anything to check, before anything is imported.

    First, and asked as a spec rather than by importing, because a server
    without a screensaver in it should fail here by name. Import it and the
    run ends in an `ImportError` traceback that names this file and the line
    it happened on, which is the one piece of information nobody needed.

    Hands `main` back so the caller does not import it twice.
    """
    print("== the server has a screensaver ==")

    found = importlib.util.find_spec("ai_terminal.live.screensaver")
    check("live/screensaver.py is a module", found is not None, True)

    # Imported here rather than at the top of the file: importing `main`
    # builds the live agent and reads `.env`, and the case above has to have
    # answered before any of that is worth doing.
    from ai_terminal.live import main

    routed = {route.path for route in main.app.routes}
    # First, and by path rather than by calling anything: a server without
    # these two routes fails here by name, instead of every case below it
    # dying in a 404 that reads like a bug in the case.
    check("GET /api/screensaver is a route",
          "/api/screensaver" in routed, True)
    check("GET /api/screensaver.jpg is a route",
          "/api/screensaver.jpg" in routed, True)
    return main


# --------------------------------------------------- where the picture lives

def directory_cases(screensaver) -> None:
    print("\n== where the picture is kept ==")

    with tempfile.TemporaryDirectory() as tmp:
        # Two levels down and neither of them there, because the Pi's unit
        # sets a `WorkingDirectory` that may itself be new on first boot.
        want = Path(tmp).resolve() / "pictures" / "clock"
        with env(SCREENSAVER_DIR=str(want)):
            screensaver.screensaver_dir.cache_clear()
            check("SCREENSAVER_DIR is where the picture goes",
                  screensaver.screensaver_dir(), want)
            check("and asking is what creates it", want.is_dir(), True)

        # The default is relative, like GWS_CACHE_DIR and SESSIONS_DB: the
        # repo root under `uv run live-server`, the unit's WorkingDirectory
        # on the Pi. Checked from a working directory of its own, because
        # the answer is a function of where the process is standing.
        was = Path.cwd()
        try:
            os.chdir(tmp)
            with env(SCREENSAVER_DIR=None):
                screensaver.screensaver_dir.cache_clear()
                check("unset, it is `screensaver/` under the working dir",
                      screensaver.screensaver_dir(),
                      Path(tmp).resolve() / "screensaver")
        finally:
            os.chdir(was)
            screensaver.screensaver_dir.cache_clear()


# ------------------------------------------ and what the deploy does to it

def deploy_cases(screensaver) -> None:
    """That the copy which reaches the Pi leaves the picture where it is.

    The directory is under the working directory rather than under `static/`
    because the deploy copies files over the package. That is only half of
    it: the working directory is the same copy's *destination* and the copy
    carries `--delete`, so the placement saves nothing on its own — the name
    has to be one of the command's `--exclude`s as well.

    Worth a case rather than a sentence because nothing on the Pi complains.
    A deploy that took `background.json` with it leaves a terminal working
    exactly as designed: `_read_state` reads `{}` (`screensaver.py:857`),
    every slot is due, and the next tick buys a 2K generation. The bill is the
    only place it shows.
    """
    print("\n== and what the next deploy does to it ==")

    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    start = readme.index("\nrsync ") + 1
    words = readme[start:readme.index("pi2:", start)].split()
    excluded = {after.strip("'\"") for flag, after in zip(words, words[1:])
                if flag == "--exclude"}

    # The default, off a working directory of its own, for the reason
    # `directory_cases` uses one: asking creates the directory.
    with tempfile.TemporaryDirectory() as tmp:
        was = Path.cwd()
        try:
            os.chdir(tmp)
            with env(SCREENSAVER_DIR=None):
                screensaver.screensaver_dir.cache_clear()
                kept = screensaver.screensaver_dir().name
        finally:
            os.chdir(was)
            screensaver.screensaver_dir.cache_clear()

    check("the deploy removes whatever the repo does not have",
          "--delete" in words, True, " ".join(words[:2]))
    check("and the picture is kept in the directory it writes into", kept,
          "screensaver", "out of `static/`, but not out of the way")
    check("so the copy is told to leave the picture alone",
          kept in excluded, True,
          "or the panel is a bare gradient and the next tick pays again")
    check("and the conversations kept beside it, for the same reason",
          any(name.startswith("sessions.db") for name in excluded), True,
          "`main.py:215` puts them in that directory too")


# ------------------------------------------- the state everyone is in today

def empty_cases(client, screensaver, scratch: Path) -> None:
    print("\n== an empty directory, which is every terminal until #86 ==")

    screensaver._shown = None
    for stray in scratch.iterdir():
        stray.unlink()

    answered = client.get("/api/screensaver")
    check("asking what to wear with nothing to wear is not an error",
          answered.status_code, 200)
    check("and it invents nothing", answered.json(), {})

    missing = client.get("/api/screensaver.jpg")
    check("the picture that was never generated is a 404",
          missing.status_code, 404,
          "not the 500 FileResponse raises on a missing path")


# ------------------------------------------------ what the clock face reads

async def payload_cases(client, screensaver) -> None:
    print("\n== the payload the face reads ==")

    check("one refresh finds something to show",
          await screensaver.refresh_once(), True)

    body = client.get("/api/screensaver").json()
    check("the payload is exactly what the two faces read",
          sorted(body), ["place", "weather"],
          "no picture yet, so no `image` either")
    check("the weather is exactly what they read",
          sorted(body["weather"]),
          ["condition", "description", "fetched", "is_day", "temperature", "unit"])
    check("the temperature is the one Open-Meteo gave",
          body["weather"]["temperature"], 24.1)
    check("with its unit beside it, because the face prints them joined",
          body["weather"]["unit"], "°C")
    check("and the sky in words", body["weather"]["description"], "Overcast")
    check("the weather carries the fetch timestamp as a float",
          isinstance(body["weather"].get("fetched"), float), True)
    check("and payload() carries it too",
          isinstance(screensaver.payload()["weather"].get("fetched"), float), True)
    # The half of the same sky the toolbar can use. The word does not fit the
    # top bar at any length, in either language, so the slug is what goes
    # there, and it is the key `WX_ICONS` in `cards.js` is indexed by. A
    # payload with the word and no slug leaves the bar with a temperature and
    # no sky beside it.
    check("and the same sky as a slug, which is the half a glyph is drawn "
          "from", body["weather"].get("condition"), "cloudy")
    check("with the day/night flag the glyph needs beside it",
          body["weather"].get("is_day"), True,
          "a clear night drawn with a sun in it")
    check("the place is what the face puts at the end of the line",
          body["place"], "Hakone, Japan")

    # And the half of the refresh the face never sees. The two sun times are
    # here rather than in the payload above because the schedule needs them
    # and `paintScreensaver` does not, which is the whole reason `_seen` is a
    # second dict — no second request, and no second source of truth for
    # where the sun is.
    check("the generator's own view is the rest of the same reading",
          sorted(screensaver._seen),
          ["description", "is_day", "recent", "sunrise", "sunset",
           "temperature"])
    # The week behind today, cut out of the same `daily` arrays the sun times
    # came from. No latitude: since #185 nothing here keys off a hemisphere.
    check("and the week the picture is drawn from",
          screensaver._seen["recent"], RECENT,
          "seven days before the reading, off the one request")
    check("carrying the pair the seven slots are cut from",
          [screensaver._seen["sunrise"], screensaver._seen["sunset"]],
          ["05:23", "17:52"],
          "straight out of the payload, with no second call to get them")

    # The refresh that fails is the ordinary one: a Pi on Wi-Fi, at 4 a.m.
    # Blanking the line would be a worse answer than an hour-old temperature,
    # because the face has no way to say which it is looking at.
    def refused(lat, lng, past_days=0):
        raise OSError("no route to host")

    was = weather._fetch
    weather._fetch = refused
    screensaver._weather_attempted_at = None
    try:
        check("a refresh that cannot reach Open-Meteo says so",
              await screensaver.refresh_once(), False)
    finally:
        weather._fetch = was
    check("and leaves the last good answer on the face",
          client.get("/api/screensaver").json()["weather"]["temperature"],
          24.1)

    # The other way a refresh fails, and the one the `except` above cannot
    # see: a 200 whose body has no reading in it. The face cannot tell an
    # hour-old temperature from a missing one, so the answer has to be the
    # same as the OSError's — keep what is there — or the promise the
    # docstring makes holds for the provider that breaks and breaks for the
    # provider that answers.
    def blank(lat, lng, past_days=0):
        return {}

    weather._fetch = blank
    screensaver._weather_attempted_at = None
    try:
        check("an answer with nothing in it is a failed refresh too",
              await screensaver.refresh_once(), False)
    finally:
        weather._fetch = was
    check("and it does not paint `Unknown` over a good temperature",
          client.get("/api/screensaver").json()["weather"],
          {"temperature": 24.1, "unit": "°C", "description": "Overcast",
           "condition": "cloudy", "is_day": True,
           "fetched": screensaver._shown_at})

    # The night, which is the only thing `is_day` is for and the one value of
    # it a fixed fixture never reaches. Open-Meteo sends 0, and it has to come
    # back as `False` rather than as the 0 — `wxIcon` takes the second slot on
    # a falsy value either way, but the payload is a contract and #86 reads it
    # too. Code 0 with it, so the pair the bar draws is `clear` at night: the
    # one combination where the day and the night glyphs actually differ.
    def midnight(lat, lng, past_days=0):
        raw = forecast(past_days)
        return {**raw, "current": {**raw["current"],
                                   "weather_code": 0, "is_day": 0}}

    weather._fetch = midnight
    screensaver._weather_attempted_at = None
    try:
        check("a reading taken after dark says so",
              await screensaver.refresh_once(), True)
    finally:
        weather._fetch = was
    check("and the slug and the flag are the pair the night glyph needs",
          client.get("/api/screensaver").json()["weather"],
          {"temperature": 24.1, "unit": "°C", "description": "Clear",
           "condition": "clear", "is_day": False,
           "fetched": screensaver._shown_at})


# ------------------------------------- and the language the face reads it in

async def language_cases(client, screensaver) -> None:
    """That one reading serves both panels, in the language each is set to.

    The defect (#131) is a clock face reading `24°C · Overcast · Hakone` on a
    panel set to Japanese. The words were already in the repo — the table in
    `weather._CONDITIONS` carries both — and what was missing was a way for
    the page to say which it wanted, because the server holds one reading for
    every panel and the setting is in each page's own localStorage.
    """
    print("\n== the language the panel asks in ==")

    screensaver._weather_attempted_at = None
    check("a reading to ask about", await screensaver.refresh_once(), True)

    counted = []

    def once(lat, lng, past_days=0):
        counted.append(past_days)
        return forecast(past_days)

    was = weather._fetch
    weather._fetch = once
    try:
        english = client.get("/api/screensaver?lang=en").json()
        japanese = client.get("/api/screensaver?lang=ja").json()
    finally:
        weather._fetch = was

    check("a panel set to English gets the word it always got",
          english["weather"]["description"], "Overcast")
    check("and one set to Japanese gets the same sky in Japanese",
          japanese["weather"]["description"], "曇り")
    check("off the one reading, with no second fetch behind it", counted, [],
          "the refresh loop owns the only poller there is")
    check("and nothing else in the payload moves with the language",
          {key: value for key, value in japanese.items() if key != "weather"},
          {key: value for key, value in english.items() if key != "weather"})
    check("the place at the end of the line least of all",
          japanese["place"], "Hakone, Japan")
    check("the keys are the six the two faces read, in either language",
          sorted(japanese["weather"]),
          ["condition", "description", "fetched", "is_day", "temperature", "unit"],
          "the WMO code is what this is rendered from, not a seventh field")
    check("and the slug the bar draws its glyph off is one of them",
          japanese["weather"]["condition"], "cloudy",
          "a glyph reads the same in both, which is why the bar has no word")

    # A page too old to send one, which is every page until this deploy lands.
    # It has to read exactly what it read before rather than a blank line.
    check("a request with no language at all is still English",
          client.get("/api/screensaver").json()["weather"]["description"],
          "Overcast")
    check("and so is the function's own default",
          screensaver.payload()["weather"]["description"], "Overcast")
    for tag in ("ja-JP", "JA"):
        check(f"{tag!r} is the same language with something on the end",
              client.get(f"/api/screensaver?lang={tag}").json()
              ["weather"]["description"], "曇り")
    # Anything else, including the shapes that are not tags at all. English
    # rather than an exception in a route that is not allowed to have one:
    # the rule `weather._base` already applies one layer down.
    for tag in ("sv", "zz", "", "not a language tag at all, but a sentence"):
        check(f"{tag!r} is a language nobody has a table for",
              client.get("/api/screensaver",
                         params={"lang": tag}).json()
              ["weather"]["description"], "Overcast")

    # The pair a `cond_*` table in `i18n.js` would lose, and the whole reason
    # the code is what is kept: `61` and `65` are both the slug `rain`, so a
    # page translating the slug it already has could not tell them apart in
    # either language. Both directions of the same sky, so a table that had
    # simply been mis-keyed would not pass this by luck.
    for code, english_word, japanese_word in ((61, "Light rain", "小雨"),
                                              (65, "Heavy rain", "大雨")):
        def raining(lat, lng, past_days=0, code=code):
            raw = forecast(past_days)
            return {**raw, "current": {**raw["current"],
                                       "weather_code": code}}

        weather._fetch = raining
        screensaver._weather_attempted_at = None
        try:
            check(f"a reading at WMO {code}",
                  await screensaver.refresh_once(), True)
        finally:
            weather._fetch = was
        body = client.get("/api/screensaver?lang=en").json()["weather"]
        check(f"WMO {code} comes back as {english_word!r}",
              body["description"], english_word)
        check("and in Japanese as well",
              client.get("/api/screensaver?lang=ja").json()
              ["weather"]["description"], japanese_word)
        check("with the same slug as the other one, which is the point",
              body["condition"], "rain",
              "nine slugs cover twenty-eight codes; the word is the finer one")

    # Put the overcast afternoon back for the cases after this one.
    screensaver._weather_attempted_at = None
    check("the fixture is back", await screensaver.refresh_once(), True)


# ------------------------------------------------ the picture, once it exists

def picture_cases(client, screensaver, scratch: Path) -> None:
    print("\n== the picture, once something has written one ==")

    picture = scratch / screensaver.IMAGE_NAME
    picture.write_bytes(JPEG)
    os.utime(picture, (1_757_000_000, 1_757_000_000))

    served = client.get("/api/screensaver.jpg")
    check("the bytes on disk are the bytes served", served.content, JPEG)
    check("labelled as a photograph",
          served.headers["content-type"], "image/jpeg")

    first = client.get("/api/screensaver").json()["image"]
    check("the payload points at it, with the file's own timestamp on the URL",
          first, "/api/screensaver.jpg?v=1757000000")
    check("asking twice gives the same URL", first,
          client.get("/api/screensaver").json()["image"],
          "a URL that changes for nothing blinks the gradient every wake")

    os.utime(picture, (1_757_003_600, 1_757_003_600))
    check("a picture that has changed gives a new one",
          client.get("/api/screensaver").json()["image"],
          "/api/screensaver.jpg?v=1757003600")

    # A `SCREENSAVER_DIR` the process cannot create or write. `image_path`
    # makes the directory on its way to naming the file, so this raises rather
    # than returning a path that is not there — and the two routes have to
    # answer it the same way, because it is one cause. `payload()` already
    # degrades to `{}`; the picture route used to hand back a traceback.
    def refuses():
        raise PermissionError(13, "Permission denied", str(scratch))

    was = screensaver.screensaver_dir
    screensaver.screensaver_dir = refuses
    try:
        check("a directory the process cannot make is a 404, not a 500",
              client.get("/api/screensaver.jpg").status_code, 404,
              "the same answer as a picture that is simply not there yet")
        check("and asking what to wear still answers rather than raising",
              client.get("/api/screensaver").status_code, 200)
    finally:
        screensaver.screensaver_dir = was

    media_type_cases(client, screensaver, picture, scratch)

    # Put back what the cases after this one read, bytes and mtime both.
    picture.write_bytes(JPEG)
    os.utime(picture, (1_757_003_600, 1_757_003_600))


def media_type_cases(client, screensaver, picture: Path,
                     scratch: Path) -> None:
    """What the route calls the file, against what is in the file (#110).

    The case above is the one this issue hid behind: it writes JPEG magic
    and reads back `image/jpeg`, which is equally true of a route that looks
    at the file and a route that answers with a constant. Every case here
    writes bytes the constant was wrong about, which is the only shape of
    case that can tell the two apart.
    """
    print("\n== and what the route calls it ==")

    def served(raw: bytes):
        picture.write_bytes(raw)
        return client.get("/api/screensaver.jpg")

    check("PNG under the .jpg name is served as PNG",
          served(PNG).headers["content-type"], "image/png",
          "what the model actually draws, and what is on the Pi now")
    check("JPEG is still served as JPEG",
          served(JPEG).headers["content-type"], "image/jpeg")
    check("and a WEBP as neither",
          served(WEBP).headers["content-type"], "image/webp",
          "its magic is at byte 8, behind a RIFF header")
    check("bytes nothing recognises are not called a photograph",
          served(NOT_AN_IMAGE).headers["content-type"],
          "application/octet-stream",
          "a name no browser will draw beats one that is wrong")
    check("nor is a file too short to have magic in it",
          served(b"\x89PN").headers["content-type"],
          "application/octet-stream")
    check("nor is an empty one, which `is_file()` lets through",
          served(b"").headers["content-type"], "application/octet-stream")

    # Reading the head must not eat it. The route opens the file once to
    # look and `FileResponse` opens it again to send.
    answer = served(PNG)
    check("looking at the bytes does not consume them",
          answer.content, PNG)
    check("and the length on the wire is the whole file",
          answer.headers["content-length"], str(len(PNG)))

    # Off the file rather than off anything remembered from the generation,
    # which is the property that fixes the terminal already running: it has
    # a PNG under the old name, written by code that never recorded a type,
    # and nothing regenerates until the next slot comes round.
    def typed(path: Path):
        """The type, or whatever was raised instead of one."""
        try:
            return screensaver.image_media_type(path)
        except OSError as err:
            return repr(err)

    check("a picture no process here has ever generated is read anyway",
          typed(picture), "image/png")
    check("and a path with nothing at it answers rather than raising",
          typed(scratch / "not-written-yet.jpg"),
          "application/octet-stream",
          "the 404 for that is `is_file()`'s, one line above the call")
    check("nor does a directory where the picture should be",
          typed(scratch), "application/octet-stream",
          "`SCREENSAVER_DIR` pointed one level too high")


# ---------------------------------------------- nothing can steer the path

def path_cases(client, main, scratch: Path) -> None:
    print("\n== the served path has no part a request can steer ==")

    # The structural half. There is no parameter, so there is no traversal to
    # defend against — this is the case that fails if somebody later adds a
    # `name` to make the route reusable.
    signature = inspect.signature(_endpoint(main))
    check("the picture route takes no arguments",
          list(signature.parameters), [])

    # And the demonstrated half, against a decoy one directory up. The three
    # query strings reach the route and are ignored by it; the three path
    # shapes reach no route at all, two of them because httpx resolves `..`
    # before it sends and one because `%2f` is not a separator. Recorded as
    # statuses as well as contents, so a case that stops reaching the route
    # cannot go on passing by never getting there.
    decoy = scratch.parent / "outside.jpg"
    decoy.write_bytes(DECOY)
    try:
        asked = [client.get(url) for url in (
            "/api/screensaver.jpg?v=../outside.jpg",
            "/api/screensaver.jpg?name=../outside.jpg",
            "/api/screensaver.jpg?name=%2e%2e%2foutside.jpg",
            "/api/screensaver.jpg/../outside.jpg",
            "/api/screensaver.jpg%2f..%2foutside.jpg",
            "/api/screensaver.jpg/..%2foutside.jpg",
        )]
        check("no way of asking reaches the file one directory up",
              [got.content == DECOY for got in asked], [False] * 6)
        check("a query string reaches the route and is ignored by it",
              [got.status_code for got in asked[:3]], [200, 200, 200])
        check("and the picture it serves is still the only one it has",
              [got.content for got in asked[:3]], [JPEG] * 3)
        check("and a path shape the route does not have reaches none",
              [got.status_code for got in asked[3:]], [404, 404, 404])
    finally:
        decoy.unlink()

    # Reads, both of them. #83 is about an unauthenticated write on a server
    # that binds 0.0.0.0 with no CORS policy; this issue must not add a third.
    check("neither route accepts a write",
          [client.post("/api/screensaver").status_code,
           client.post("/api/screensaver.jpg").status_code], [405, 405])


def _endpoint(main):
    """The function behind `/api/screensaver.jpg`, found by path.

    By path rather than by importing the name, because what the case is about
    is the signature of whatever is actually mounted there.
    """
    for route in main.app.routes:
        if getattr(route, "path", None) == "/api/screensaver.jpg":
            return route.endpoint
    raise AssertionError("no /api/screensaver.jpg route")


# -------------------------------------------------- the task behind it all

async def task_cases(screensaver) -> None:
    print("\n== the refresh that keeps the line current ==")

    screensaver._shown = None
    screensaver._weather_attempted_at = None
    task = asyncio.create_task(screensaver.refresh_loop(interval=3600))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if screensaver._shown is not None:
            break
    check("the loop fetches before its first sleep",
          screensaver._shown is not None, True,
          "the clock comes up seconds after boot")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    check("and cancelling it is what stops it", task.cancelled(), True)

    # The one that matters, because the alternative is invisible: a task that
    # dies leaves a server that still answers audio and a clock that still
    # tells the time, showing the temperature it happened to have when the
    # loop stopped.
    tries, enough = [], asyncio.Event()

    async def boom() -> bool:
        tries.append(1)
        if len(tries) >= 3:
            enough.set()
        raise RuntimeError("something nobody thought of")

    was = screensaver.refresh_once
    screensaver.refresh_once = boom
    try:
        task = asyncio.create_task(screensaver.refresh_loop(interval=0))
        await asyncio.wait_for(enough.wait(), timeout=5)
        check("an attempt that raises does not end the loop",
              task.done(), False, f"{len(tries)} attempts made")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        screensaver.refresh_once = was


# --------------------------------------------- and the picture rides with it

async def picture_loop_cases(screensaver) -> None:
    """That the picture is checked on the same tick, and separately.

    One loop rather than two: the lifespan starts and cancels exactly one
    task, and a second one is a second thing to forget to cancel. What that
    buys has to be paid for here, because the two halves now fail for
    unrelated reasons — an image endpoint refusing everything at 03:00 must
    not be able to stop the temperature updating.
    """
    print("\n== the same loop looks at the picture ==")

    looked = []

    async def spy(now: float) -> bool:
        looked.append(now)
        return False

    async def raising(now: float) -> bool:
        looked.append(now)
        raise RuntimeError("the image endpoint is having a day")

    was = screensaver.generate_if_due
    screensaver.generate_if_due = spy
    try:
        task = asyncio.create_task(screensaver.refresh_loop(interval=3600))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if looked:
                break
        check("one turn of the weather loop is one look at the picture",
              len(looked), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        looked.clear()
        screensaver.generate_if_due = raising
        task = asyncio.create_task(screensaver.refresh_loop(interval=0))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if len(looked) >= 3:
                break
        check("and a generation that raises does not end the loop",
              [task.done(), len(looked) >= 3], [False, True],
              f"{len(looked)} attempts made")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        screensaver.generate_if_due = was


# ------------------------------------- and weather and photograph clocks differ

async def interval_cases(screensaver) -> None:
    """That weather is refetched every 30 minutes while the loop ticks every 10."""
    print("\n== weather and photograph intervals are uncoupled ==")

    check("refresh tick default is ten minutes", screensaver.REFRESH_S, 600.0)
    check("weather interval default is thirty minutes", screensaver.WEATHER_S, 1800.0)

    check("weather due on cold boot", screensaver._weather_due(0.0, None), True)
    check("weather not due before interval", screensaver._weather_due(600.0, 0.0), False)
    check("weather not due at 20 min", screensaver._weather_due(1200.0, 0.0), False)
    check("weather due at 30 min", screensaver._weather_due(1800.0, 0.0), True)
    check("weather due after 30 min", screensaver._weather_due(1801.0, 0.0), True)

    # Pin the unpassed interval: refresh_loop() with no arguments ticks at
    # REFRESH_S and weather is fetched every WEATHER_S.
    weather_attempts = []
    photo_looks = []
    fake_mono = 1000.0
    loop_ticks = 0

    was_sleep = asyncio.sleep

    async def fake_sleep(duration):
        nonlocal fake_mono, loop_ticks
        loop_ticks += 1
        fake_mono += duration
        await was_sleep(0)

    def spy_fetch(lat, lng, place, lang, past_days=0):
        weather_attempts.append(fake_mono)
        return {"current": {"temperature": 20.0}}

    async def spy_gen(now: float) -> bool:
        photo_looks.append(fake_mono)
        return False

    was_weather = screensaver.weather._weather_sync
    was_gen = screensaver.generate_if_due
    was_mono = time.monotonic

    screensaver.weather._weather_sync = spy_fetch
    screensaver.generate_if_due = spy_gen
    screensaver._weather_attempted_at = None
    screensaver._shown = {"place": "Hakone", "weather": {"temperature": 20.0, "unit": "°C"}}

    time.monotonic = lambda: fake_mono
    asyncio.sleep = fake_sleep

    try:
        # Start refresh_loop() with NO arguments: interval defaults to REFRESH_S (600s)
        task = asyncio.create_task(screensaver.refresh_loop())
        for _ in range(50):
            if loop_ticks >= 4:
                break
            await was_sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        check("the loop with no arguments ticks at REFRESH_S (600s)",
              len(photo_looks) >= 4, True)
        check("photographs are checked on every 600s tick",
              [photo_looks[0], photo_looks[1], photo_looks[2], photo_looks[3]],
              [1000.0, 1600.0, 2200.0, 2800.0])
        check("weather is refetched only on the 1800s cadence",
              weather_attempts, [1000.0, 2800.0],
              "fetched at t=0 and t=1800; skipped at t=600 and t=1200")
    finally:
        asyncio.sleep = was_sleep
        time.monotonic = was_mono
        screensaver.weather._weather_sync = was_weather
        screensaver.generate_if_due = was_gen
        screensaver._weather_attempted_at = None

    # That consecutive failures do NOT collapse the interval back to the 600s tick.
    weather_attempts.clear()
    photo_looks.clear()
    fake_mono = 5000.0
    loop_ticks = 0

    def failing_fetch(lat, lng, place, lang, past_days=0):
        weather_attempts.append(fake_mono)
        raise OSError("network down")

    screensaver.weather._weather_sync = failing_fetch
    screensaver.generate_if_due = spy_gen
    screensaver._weather_attempted_at = None

    time.monotonic = lambda: fake_mono
    asyncio.sleep = fake_sleep

    try:
        task = asyncio.create_task(screensaver.refresh_loop())
        for _ in range(50):
            if loop_ticks >= 4:
                break
            await was_sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        check("a failed weather fetch records attempt and does not collapse back to 600s",
              weather_attempts, [5000.0, 6800.0],
              "failed at t=0, not retried at t=600 or t=1200, retried at t=1800")
    finally:
        asyncio.sleep = was_sleep
        time.monotonic = was_mono
        screensaver.weather._weather_sync = was_weather
        screensaver.generate_if_due = was_gen
        screensaver._weather_attempted_at = None

    # When no reading has been established yet (_shown is None), a failed fetch
    # retries on every 600s loop tick so cold-boot network outages do not delay
    # the first photograph by 30 minutes.
    weather_attempts.clear()
    photo_looks.clear()
    fake_mono = 5000.0
    loop_ticks = 0

    screensaver.weather._weather_sync = failing_fetch
    screensaver.generate_if_due = spy_gen
    screensaver._weather_attempted_at = None
    screensaver._shown = None

    time.monotonic = lambda: fake_mono
    asyncio.sleep = fake_sleep

    try:
        task = asyncio.create_task(screensaver.refresh_loop())
        for _ in range(50):
            if loop_ticks >= 4:
                break
            await was_sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        check("cold boot retries failed weather fetch on loop cadence when no reading shown",
              weather_attempts, [5000.0, 5600.0, 6200.0, 6800.0],
              "failed at t=0, retried at t=600, t=1200, t=1800 while _shown is None")
    finally:
        asyncio.sleep = was_sleep
        time.monotonic = was_mono
        screensaver.weather._weather_sync = was_weather
        screensaver.generate_if_due = was_gen
        screensaver._weather_attempted_at = None

    # Moving the home pin clears the weather stamp so the next tick asks about the new place.
    was_told = screensaver._told
    was_geocoded = screensaver._geocoded
    was_shown = screensaver._shown
    try:
        screensaver._weather_attempted_at = 1000.0
        screensaver._told = None
        screensaver._geocoded = (35.68, 139.76, "Tokyo")
        # #465: set_home immediately updates _shown["place"] if _shown exists
        screensaver._shown = {"place": "Toyosu", "weather": {"temperature": 18.0}}
        check("set_home returns True when position moves",
              screensaver.set_home({"lat": 34.693, "lng": 135.502}, "Osaka"), True)
        check("set_home clears weather attempted stamp",
              screensaver._weather_attempted_at, None)
        check("set_home clears geocoded cache",
              screensaver._geocoded, None)
        check("set_home immediately updates _shown place",
              screensaver._shown["place"], "Osaka")
        check("set_home persists position to home.json",
              screensaver.home_path().is_file(), True)
        home_saved = json.loads(screensaver.home_path().read_text(encoding="utf-8"))
        check("persisted home has correct place", home_saved.get("place"), "Osaka")
        check("persisted home has correct lat", home_saved.get("lat"), 34.693)
        check("persisted home has correct lng", home_saved.get("lng"), 135.502)

        screensaver._told = None
        check("restore_home restores told from disk",
              screensaver.restore_home(), (34.693, 135.502, "Osaka"))
        check("where() returns restored position",
              screensaver.where(), (34.693, 135.502, "Osaka"))

        screensaver._weather_attempted_at = 2000.0
        screensaver._geocoded = (34.693, 135.502, "Osaka")
        check("same pin does not clear weather attempted stamp",
              screensaver.set_home({"lat": 34.693, "lng": 135.502}, "Osaka"), False)
        check("stamp remains untouched on no-op set_home",
              screensaver._weather_attempted_at, 2000.0)
        check("geocoded cache remains untouched on no-op set_home",
              screensaver._geocoded, (34.693, 135.502, "Osaka"))
    finally:
        screensaver._told = was_told
        screensaver._geocoded = was_geocoded
        screensaver._shown = was_shown
        screensaver.home_path().unlink(missing_ok=True)


# ------------------------------------------------- and it is wired to the app

async def lifespan_cases(main, screensaver) -> None:
    print("\n== the app is what starts it ==")

    started, running, stopped = [], [], []

    async def spy(interval: float = 0.0) -> None:
        started.append(interval)
        running.append(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(1)
            raise

    # These two cases are the only place outside the server that runs the
    # lifespan for real, and since #233 the lifespan calls `gws_cache.migrate`,
    # which rewrites the `source` line of every header in `cache_dir()`. That
    # is the developer's own `gws_cache/` for a sweep run from the repo root,
    # and those files are gitignored, so the rewrite is not recoverable. The
    # redaction is idempotent and this file is not about the cache either way —
    # so it gets a directory of its own, the same as `wiring_check.py:2191` and
    # `history_check.py:885` give the endpoint.
    was_cache_dir = gws_cache.cache_dir
    scratch_cache = tempfile.TemporaryDirectory()
    gws_cache.cache_dir = lambda: Path(scratch_cache.name)

    # A header an older build left behind, for the case below. Written before
    # the lifespan runs, because the migration is a startup step and the whole
    # question is whether starting is what does it.
    # Written by the module's own writer rather than by hand: `_read_header`
    # wants every value quoted, and a header it cannot parse is one `migrate`
    # skips — which would make this case pass for the wrong reason.
    stale_header = Path(scratch_cache.name) / "letter.md"
    stale_header.write_text(
        gws_cache._header({
            "source": 'gws drive files list --query "name contains '
                      "'zqxvwood'\"",
            "fetched": "2026-09-11T00:00:00+09:00"}) + "body\n",
        encoding="utf-8")

    was = screensaver.refresh_loop
    screensaver.refresh_loop = spy
    try:
        with TestClient(main.app, base_url=OUR_URL) as client:
            client.get("/api/screensaver")
            check("starting the server starts the refresh", len(started), 1)
            check("and it is still running while the server is",
                  [running[0].done(), stopped], [False, []])
            # #233. `gws_cache.migrate` is only ever called from here, so this
            # is the only place the wiring can be pinned — and until it was,
            # deleting the call from `_lifespan` left all twenty scripts green
            # while every header on the Pi kept the question that fetched it.
            # The redaction itself is `cache_check.py`'s; this asks only
            # whether starting the server is what runs it.
            for _ in range(50):
                if "zqxvwood" not in stale_header.read_text(encoding="utf-8"):
                    break
                time.sleep(0.01)
            check("and starting it redacts a header an older build wrote",
                  "zqxvwood" in stale_header.read_text(encoding="utf-8"),
                  False,
                  "the migration is a startup step or it is nothing")

        # Shutdown by hand, in this file's own loop, rather than by letting
        # the client go. A `TestClient` takes its whole event loop down on the
        # way out, and a task nobody cancelled dies of that — so through the
        # client, a lifespan that leaks its task and one that stops it look
        # exactly alike, which is how a server ends up with one refresh loop
        # per `--reload` all talking to Open-Meteo.
        started.clear(), running.clear(), stopped.clear()
        async with main._lifespan(main.app):
            await asyncio.sleep(0)
            check("the lifespan on its own starts one too",
                  [len(started), running[0].done()], [1, False])
        check("and stopping the server stops it",
              [stopped, running[0].cancelled()], [[1], True],
              "not left behind for the next one to run alongside")
    finally:
        screensaver.refresh_loop = was
        gws_cache.cache_dir = was_cache_dir
        scratch_cache.cleanup()


# ----------------------------------------------------- where it asks about

def sun_cases() -> None:
    """`_clock` against the shapes a time arrives in.

    The one that matters is the null. `["x"] or [""]` short-circuits on the
    truthy list, so the `or` at the two call sites guards an absent field and
    not a null element, and `len(None)` raised out of the tool with nothing
    above it to catch the exception.

    What a polar day actually sends is the last case, and it is not a null.
    Asked on 2026-09-09, Open-Meteo answers a day the sun does not rise with a
    midnight stamp — Tromsø on 2025-12-15 comes back `sunrise` and `sunset`
    both `2025-12-15T00:00`, and Longyearbyen in June the same way. Pinned
    here so the next person has the API's answer without going back out to
    the network for it.

    One stamp at a time that midnight is indistinguishable from a real
    midnight sunrise, which is why `_clock` is not where the day is decided.
    `polar_cases` below is about `_sun`, which sees both times at once and is
    the only thing that can tell the two apart.
    """
    print("\n== a sunrise that is not a time ==")

    check("an ordinary stamp is the time out of it",
          weather._clock("2026-09-09T05:23"), "05:23")
    check("one too short to hold a time is passed through",
          weather._clock("05:23"), "05:23")
    check("an empty string is an empty answer", weather._clock(""), "")
    check("and so is a null, which nothing sends but everything accepts",
          weather._clock(None), "")
    check("a polar day's real answer is a midnight stamp, not a null",
          weather._clock("2025-12-15T00:00"), "00:00")

    polar = dict(FORECAST, daily={"sunrise": [None], "sunset": [None]})
    was = weather._fetch
    weather._fetch = lambda *a, **k: polar
    try:
        out = weather._weather_sync(69.65, 18.96, "Tromso, Norway", "en")
    finally:
        weather._fetch = was

    check("so the forecast for one comes back rather than raising",
          out["current"]["sunrise"], "")
    check("with the sunset the same way", out["current"]["sunset"], "")
    check("and the rest of the reading untouched by it",
          out["current"]["temperature"], 24.1)


# What pi2 served on 2026-09-10 at 16:03, cut to the fields `_daily` reads.
# `current` is code 2, which the card draws as a 220 px sun; `daily[1]` is
# code 65, 大雨. The owner heard the second and looked at the first.
CONTRADICTION = {
    "timezone": "Asia/Tokyo",
    "current_units": {"temperature_2m": "°C"},
    "current": {
        "time": "2026-09-10T16:00",
        "temperature_2m": 20.9,
        "weather_code": 2,
        "is_day": 1,
    },
    "hourly": {},
    "daily": {
        "time": ["2026-09-10", "2026-09-11", "2026-09-12",
                 "2026-09-13", "2026-09-14"],
        "weather_code": [61, 65, 51, 3, 0],
        "temperature_2m_max": [24.0, 22.0, 25.0, 26.0, 27.0],
        "temperature_2m_min": [18.0, 17.0, 19.0, 20.0, 21.0],
        "precipitation_probability_max": [98, 97, 31, 10, 0],
        "sunrise": ["2026-09-10T05:23"],
        "sunset": ["2026-09-10T17:52"],
    },
}


def day_cases() -> None:
    """Which day a forecast row is about, said on the row.

    One reading feeds two surfaces: `weatherCard` draws `current` at the size
    of an answer and the five `daily` rows underneath it, and the model is
    handed the same dict and speaks out of whichever row the question was
    about. So a row the model can answer from has to be a row the person can
    find on the screen, and `Fri` is not one — nobody in front of a kitchen
    panel converts a weekday into "the day after this one".

    `current` cannot carry that weight instead. `_clock` renders `HH:MM` and
    drops the date (`weather.py:154`), so `current.time` says 16:00 and not
    which 16:00; putting it on the card names an hour, not a day.
    """
    print("\n== which day a forecast row is about ==")

    was = weather._fetch
    weather._fetch = lambda *a, **k: CONTRADICTION
    try:
        ja = weather._weather_sync(35.232, 139.107, "Hakone", "ja")
        en = weather._weather_sync(35.232, 139.107, "Hakone", "en-US")
    finally:
        weather._fetch = was

    check("the sky the card draws big is `current`, and it is a sun",
          [ja["current"]["condition"], ja["current"]["description"]],
          ["partly-cloudy", "晴れときどき曇り"])
    check("大雨 is one row of the reading, and the row names its own day",
          [row["label"] for row in ja["daily"]
           if row["description"] == "大雨"], ["明日"])
    check("every day in the reading says which day it is",
          [row["label"] for row in ja["daily"]],
          ["今日", "明日", "土", "日", "月"],
          "not 木 for today and 金 for tomorrow")
    check("and says it in the language of the turn",
          [row["label"] for row in en["daily"]],
          ["Today", "Tomorrow", "Sat", "Sun", "Mon"])
    check("the date is still on every row for anything that sorts on one",
          [row["date"] for row in ja["daily"]],
          ["2026-09-10", "2026-09-11", "2026-09-12",
           "2026-09-13", "2026-09-14"])
    check("while `current` carries an hour and no date at all",
          ja["current"]["time"], "16:00",
          "which is why the naming has to be on the daily rows")


def reading(sunrise, sunset, is_day: int | None = 1) -> dict:
    """`_weather_sync`'s `current`, over one day's pair of *raw* stamps.

    `_fetch` is stubbed, so this reaches Open-Meteo no more than anything else
    in this file does. Raw rather than rendered because the whole question
    below is what the two stamps have to be compared as, and a helper that
    took `HH:MM` would answer it in the harness instead of in the code.

    `is_day=None` leaves the field out of the answer altogether, which is the
    only way to see what the default is.
    """
    hour = dict(FORECAST["current"])
    if is_day is None:
        hour.pop("is_day", None)
    else:
        hour["is_day"] = is_day
    raw = dict(FORECAST, current=hour,
               daily={"sunrise": [sunrise], "sunset": [sunset]})
    was = weather._fetch
    weather._fetch = lambda *a, **k: raw
    try:
        out = weather._weather_sync(69.65, 18.96, "Tromso, Norway", "en")
    finally:
        weather._fetch = was
    return out["current"]


def polar_cases(screensaver) -> None:
    """A day with no sunrise, told apart from a sunrise near midnight.

    `Sunrise 00:00   Sunset 00:00` is what the card read on such a day: two
    identical times, worn with the confidence of a reading. The fix is a
    state beside the pair, and the state is decided on the rendered times
    rather than on the raw stamps — the two degenerate shapes the API sends
    are not the same shape as each other, and only one of them has equal raw
    stamps. The first two cases are those shapes, measured on 2026-09-09 and
    already pinned in `slot_cases` at the far end of the same path.

    The case that keeps the fix honest is the third: at 83.8°N on that same
    day the sun really does come up at 00:46 and go down at 22:43, and those
    two *times* differ. A fix that ate them would be a second bug wearing the
    first one's clothes.
    """
    print("\n== a day the sun does not cross the horizon ==")

    day = reading("2026-09-09T00:00", "2026-09-10T00:00", is_day=1)
    check("89.9°N, where the sun does not set, is a state not a time",
          day["sun"], weather.SUN_POLAR_DAY,
          "the raw stamps are a day apart; the rendered pair is not")
    check("and the card is given nothing to print for either end",
          [day["sunrise"], day["sunset"]], ["", ""],
          "the `|| \"—\"` fallback, rather than 00:00 twice")
    check("nor a moment for a scheduler to anchor on",
          [day["sunrise_at"], day["sunset_at"]], [None, None],
          "tomorrow's midnight is not this day's sunset")

    night = reading("2026-09-10T00:00", "2026-09-10T00:00", is_day=0)
    check("89.9°S the same day, where it does not rise, is the other one",
          night["sun"], weather.SUN_POLAR_NIGHT,
          "`is_day` is what tells the two apart; the times cannot")
    check("with nothing to print or to anchor on there either",
          [night["sunrise"], night["sunset"],
           night["sunrise_at"], night["sunset_at"]], ["", "", None, None])

    # `is_day` used to pick a palette and nothing else, so its default was a
    # shade of blue. It now picks the word on the card too, and a provider
    # that leaves the field out gets a polar day rather than a polar night.
    silent = reading("2026-09-09T00:00", "2026-09-10T00:00", is_day=None)
    check("a reading with no `is_day` in it at all is taken for daylight",
          [silent["is_day"], silent["sun"]], [True, weather.SUN_POLAR_DAY],
          "the default now names the day as well as painting it")

    shoulder = reading("2026-09-09T00:46", "2026-09-09T22:43", is_day=1)
    check("83.8°N is an ordinary day and stays two times",
          [shoulder["sunrise"], shoulder["sunset"]], ["00:46", "22:43"],
          "a real sunrise three quarters of an hour after midnight")
    check("and keeps both of its anchors", shoulder["sun"],
          weather.SUN_RISES)

    ordinary = reading("2026-09-09T05:23", "2026-09-09T17:52")
    check("a day away from the circle is untouched",
          [ordinary["sunrise"], ordinary["sunset"], ordinary["sun"]],
          ["05:23", "17:52", weather.SUN_RISES])
    check("and the rest of the reading with it",
          ordinary["temperature"], 24.1)

    missing = reading(None, None)
    check("a provider that says nothing says it as `unknown`",
          missing["sun"], weather.SUN_UNKNOWN,
          "which is not the same fact as a polar night")
    check("with both halves absent rather than empty-and-guessed",
          [missing["sunrise_at"], missing["sunset_at"]], [None, None])

    half = reading("2026-09-09T05:23", None)
    check("half an answer keeps the half that arrived",
          [half["sunrise"], half["sunrise_at"], half["sunset_at"]],
          ["05:23", "2026-09-09T05:23", None])
    check("and does not claim to know which kind of day it was",
          half["sun"], weather.SUN_UNKNOWN)

    print("\n== what #86 is handed ==")

    # The point of the `_at` pair: a scheduler wanting sunrise ±1 h needs a
    # moment it can do arithmetic on, and parsing one back out of "05:23"
    # means knowing the local date and the timezone that `HH:MM` was rendered
    # in. The raw stamp is already local — the request asks `timezone=auto`.
    check("the anchor is a stamp `fromisoformat` takes, not a rendered time",
          datetime.fromisoformat(ordinary["sunrise_at"]),
          datetime(2026, 9, 9, 5, 23))
    check("and the sunset the same way",
          datetime.fromisoformat(ordinary["sunset_at"]),
          datetime(2026, 9, 9, 17, 52))
    check("the four states are the whole of the vocabulary",
          sorted({weather.SUN_RISES, weather.SUN_POLAR_DAY,
                  weather.SUN_POLAR_NIGHT, weather.SUN_UNKNOWN}),
          ["polar_day", "polar_night", "rises", "unknown"])
    check("`rises` is the only one that comes with both anchors",
          [(r["sun"], r["sunrise_at"] is not None
            and r["sunset_at"] is not None)
           for r in (ordinary, shoulder, day, night, missing, half)],
          [("rises", True), ("rises", True), ("polar_day", False),
           ("polar_night", False), ("unknown", False), ("unknown", False)],
          "so `sunrise_at and sunset_at` is the one test #86 has to make")

    # And the whole path, because this changes what `_seen` is built from:
    # the rendered pair on a degenerate day is now `""` rather than `"00:00"`,
    # and `_sun` over there reads the pair equal either way. Run rather than
    # reasoned about — the seven slots are what the change could break.
    def slots(current: dict) -> list:
        return named(screensaver._slots(
            dict(SEEN, sunrise=current["sunrise"],
                 sunset=current["sunset"])))

    check("a polar day still leaves only the wall-clock slot", slots(day),
          ["night"], "two empty strings, where it used to be two midnights")
    check("and a polar night the same", slots(night), ["night"])
    check("and a reading with no sun times in it at all",
          slots(missing), ["night"])
    check("but 83.8°N keeps the six its two times can carry",
          slots(shoulder),
          ["dawn", "dawn+1", "night", "dusk-1", "dusk", "dusk+1"],
          "`dawn-1` is 23:46 yesterday and is dropped, not carried")
    check("and an ordinary day all seven",
          slots(ordinary),
          ["dawn-1", "dawn", "dawn+1", "dusk-1", "dusk", "dusk+1", "night"])


async def coordinate_cases(screensaver, main) -> None:
    print("\n== where a task with no browser asks about ==")

    asked = []
    reverse_asked = []

    def geocoder(place, lang):
        asked.append(place)
        return HAKONE

    def reverse_geocoder(lat, lng, lang):
        reverse_asked.append((lat, lng))
        return "Hakone"

    was = weather._geocode
    was_reverse = getattr(weather, "_reverse_geocode", None)
    weather._geocode = geocoder
    weather._reverse_geocode = reverse_geocoder
    try:
        screensaver._geocoded = None
        with env(HOME_COORDS="35.232,139.107", HOME_LOCATION="Hakone"):
            check("HOME_COORDS is the answer when it is set",
                  screensaver.where(), (35.232, 139.107, "Hakone"))
            check("and no model is asked for it", asked + reverse_asked, [])

        screensaver._geocoded = None
        reverse_asked.clear()
        with env(HOME_COORDS="35.232,139.107", HOME_LOCATION=None):
            check("with only HOME_COORDS set, place resolves deterministically",
                  screensaver.where(), (35.232, 139.107, "Hakone"))
            screensaver.where()
            check("and does not invoke reverse-geocode model",
                  reverse_asked, [])

        # #465: where() never returns raw coordinate string as place name,
        # and discontinues Gemini model reverse-geocoding per owner directive
        screensaver._geocoded = None
        reverse_asked.clear()
        with env(HOME_COORDS="35.7138,139.7773", HOME_LOCATION=None):
            res = screensaver.where()
            check("where() resolves Ueno district name without invoking model",
                  res, (35.7138, 139.7773, "Ueno, Tokyo"))
            check("where() never returns raw coordinates",
                  re.match(r"^-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?$", res[2]) is None, True)
            check("reverse-geocode model is not called",
                  reverse_asked, [])

        screensaver._geocoded = None
        reverse_asked.clear()
        with env(HOME_COORDS="35.7138,139.7773", HOME_LOCATION=""):
            res = screensaver.where()
            check("where() resolves Ueno district when HOME_LOCATION is empty",
                  res, (35.7138, 139.7773, "Ueno, Tokyo"))
            check("where() place is not raw coordinates on empty HOME_LOCATION",
                  re.match(r"^-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?$", res[2]) is None, True)
            check("reverse-geocode model is not called on empty HOME_LOCATION",
                  reverse_asked, [])

        # Test Google Maps reverse geocoding API path when GOOGLE_MAPS_API_KEY is present
        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "status": "OK",
                    "results": [{
                        "address_components": [
                            {"long_name": "Shibuya", "types": ["sublocality_level_1", "sublocality"]},
                            {"long_name": "Tokyo", "types": ["administrative_area_level_1"]},
                            {"long_name": "Japan", "types": ["country"]},
                        ],
                        "formatted_address": "Shibuya, Tokyo, Japan",
                    }],
                }

        was_get = requests.get
        requests.get = lambda url, params=None, timeout=None: FakeResponse()
        try:
            screensaver._geocoded = None
            with env(HOME_COORDS="35.6580,139.7016", HOME_LOCATION=None, GOOGLE_MAPS_API_KEY="fake-key"):
                res = screensaver.where()
                check("Google Maps reverse geocoding formats place name",
                      res, (35.6580, 139.7016, "Shibuya, Tokyo"))
        finally:
            requests.get = was_get

        # Test fallback when Google Maps API fails
        def failing_get(url, params=None, timeout=None):
            raise requests.RequestException("offline")

        requests.get = failing_get
        try:
            screensaver._geocoded = None
            with env(HOME_COORDS="35.7138,139.7773", HOME_LOCATION=None, GOOGLE_MAPS_API_KEY="fake-key"):
                res = screensaver.where()
                check("Google Maps failure falls back to known district",
                      res, (35.7138, 139.7773, "Ueno, Tokyo"))
        finally:
            requests.get = was_get

        # Unknown coordinates fallback to Local Area, never raw coordinates
        screensaver._geocoded = None
        with env(HOME_COORDS="10.0,20.0", HOME_LOCATION=None):
            res = screensaver.where()
            check("Unknown coordinates fallback to Local Area, never raw coordinates",
                  res, (10.0, 20.0, "Local Area"))
            check("Unknown coordinates place is not raw coords",
                  re.match(r"^-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?$", res[2]) is None, True)

        weather._reverse_geocode = reverse_geocoder

        geocode_desc = (
            weather._GEOCODE_SCHEMA.get("properties", {})
            .get("name", {})
            .get("description", "")
            .lower()
        )
        check("weather._GEOCODE_SCHEMA specifies district/locality level",
              "district" in geocode_desc and "locality" in geocode_desc, True)
        check("weather._GEOCODE_SCHEMA prioritizes neighborhood/sublocality level",
              "neighborhood" in geocode_desc and "sublocality" in geocode_desc, True)
        check("weather._GEOCODE_SCHEMA excludes wider administrative wards/districts",
              "ward" in geocode_desc and ("broader" in geocode_desc or "administrative" in geocode_desc), True)
        check("weather._GEOCODE_SCHEMA mentions avoiding water body jumps to neighboring areas like Toyosu",
              "toyosu" in geocode_desc and "water" in geocode_desc, True)

        reverse_schema = getattr(weather, "_REVERSE_GEOCODE_SCHEMA", {})
        reverse_desc = (
            reverse_schema.get("properties", {})
            .get("name", {})
            .get("description", "")
            .lower()
        )
        check("weather._REVERSE_GEOCODE_SCHEMA specifies district/locality level",
              "district" in reverse_desc and "locality" in reverse_desc, True)
        check("weather._REVERSE_GEOCODE_SCHEMA prioritizes neighborhood/sublocality level",
              "neighborhood" in reverse_desc and "sublocality" in reverse_desc, True)
        check("weather._REVERSE_GEOCODE_SCHEMA excludes wider administrative wards/districts",
              "ward" in reverse_desc and ("broader" in reverse_desc or "administrative" in reverse_desc), True)
        check("weather._REVERSE_GEOCODE_SCHEMA mentions avoiding water body jumps to neighboring areas like Toyosu",
              "toyosu" in reverse_desc and "water" in reverse_desc, True)

        # The ordinary typo, and the only way it shows: a pair the right way
        # round is 35,139 and the wrong way round is still two numbers.
        for wrong in ("139.107,35.232", "Hakone", "35.232", "", "35.2,"):
            with env(HOME_COORDS=wrong, HOME_LOCATION="Hakone"):
                screensaver._geocoded = None
                check(f"HOME_COORDS={wrong!r} is not a position",
                      screensaver.where(), HAKONE,
                      "falls through to the address")

        asked.clear()
        screensaver._geocoded = None
        with env(HOME_COORDS=None, HOME_LOCATION="Hakone"):
            check("unset, the address already in .env is geocoded",
                  screensaver.where(), HAKONE)
            screensaver.where()
            check("once per process, because it is a bill", asked, ["Hakone"])

        # Neither. The state of a terminal nobody has configured, and it is
        # not an error: the clock still tells the time.
        screensaver._geocoded = None
        screensaver._shown = None
        screensaver._weather_attempted_at = None
        with env(HOME_COORDS=None, HOME_LOCATION=None):
            check("with neither set there is nowhere to ask about",
                  screensaver.where(), None)
            check("and the refresh says so rather than raising",
                  await screensaver.refresh_once(), False)
        check("leaving the face with nothing on it",
              screensaver._shown, None)
    finally:
        screensaver._geocoded = None
        weather._geocode = was
        if was_reverse is not None:
            weather._reverse_geocode = was_reverse
        elif hasattr(weather, "_reverse_geocode"):
            delattr(weather, "_reverse_geocode")

    await pin_cases(screensaver, main)


# ------------------------------------------- and the pin somebody dropped

async def pin_cases(screensaver, main) -> None:
    """That the map pin in the settings sheet moves the clock too (#131).

    It moved the agent from the day #96 landed — the handshake writes
    `home_coords` into the session state and every weather question reads it
    — and left the clock on `HOME_LOCATION`, so the line under it reported a
    different town from the one the person had just confirmed on a map.
    """
    print("\n== the pin the settings sheet dropped ==")

    osaka = {"lat": 34.693, "lng": 135.502}
    screensaver._told = None
    screensaver._shown = None
    screensaver._seen = None
    screensaver._refreshed_at = None
    try:
        with env(HOME_COORDS="35.232,139.107", HOME_LOCATION="Hakone, Japan"):
            check("with no page having said anything, .env is still the answer",
                  screensaver.where(), (35.232, 139.107, "Hakone, Japan"))

            check("a handshake carrying a pin moves it",
                  screensaver.set_home(osaka, "Osaka, Japan"), True)
            check("and that is where the clock asks about now",
                  screensaver.where(), (34.693, 135.502, "Osaka, Japan"),
                  "HOME_COORDS is still set and no longer wins")
            check("the same pin again is not a move",
                  screensaver.set_home(osaka, "Osaka, Japan"), False,
                  "every reconnect carries it; only a change is one")

            # The second page. A laptop open on the same server has no
            # location set and sends none, and that means "use what you are
            # configured with" rather than "forget where you are" — the sheet
            # says as much in `ui_where_unset`.
            check("a page with no pin does not clear the one there is",
                  screensaver.set_home(None, ""), False)
            check("and the clock has not moved", screensaver.where(),
                  (34.693, 135.502, "Osaka, Japan"))

            # What the move costs before the next tick, which is nothing.
            # #86 spends a 2K generation per slot and a pin is not a slot, and
            # a page that reconnected in a loop must not be able to turn that
            # into Open-Meteo requests either.
            counted = []

            def once(lat, lng, past_days=0):
                counted.append((lat, lng))
                return forecast(past_days)

            was = weather._fetch
            weather._fetch = once
            try:
                check("moving the pin fetches nothing on the spot", counted,
                      [], "the loop picks it up on its own tick")
                check("and the tick that follows asks about the new place",
                      await screensaver.refresh_once(), True)
                check("of the pin's own coordinates",
                      counted, [(34.693, 135.502)])
            finally:
                weather._fetch = was

            check("so the face names the place the person confirmed on a map",
                  screensaver._shown["place"], "Osaka, Japan")
            check("and the picture is drawn from that place's own week",
                  screensaver._seen["recent"], RECENT,
                  "no hemisphere to get right: the numbers say which it is")

            # A pin with nothing readable under it. `reverseGeocode` answers
            # "" when Maps will not say, and the page sends the coordinates
            # anyway — a line with a temperature and no place on it is one
            # `paintScreensaver` already filters down to two fields.
            check("a pin with no address on it is still a position",
                  screensaver.set_home(osaka | {"lat": 34.7}, ""), True)
            check("and carries no place", screensaver.where(),
                  (34.7, 135.502, ""))
    finally:
        screensaver._told = None
        screensaver._shown = None
        screensaver._seen = None
        screensaver._refreshed_at = None
        screensaver.home_path().unlink(missing_ok=True)

    await panel_cases(screensaver)
    handshake_cases(main)


# ------------------------------------ and which page the pin belonged to

async def panel_cases(screensaver) -> None:
    """Which page owns the clock's position when two of them disagree.

    #131 let any page's pin move the clock, which is right on a laptop and
    wrong in a kitchen. One process serves the panel on the wall and the
    phone that opened the same URL from a train, there is one clock between
    them, and the phone's pin is a position the household is not standing in.

    The rule is that a *connected panel* owns the position — and the two
    halves of that sentence are the two things worth a case, because both
    were written the other way first and both wrong versions passed
    everything above:

    - **Connected**, not *has pinned*. A panel that comes up with nothing
      configured sends no pin, and if ownership waited for a pin it would
      never arrive: that is exactly #154's terminal, and a tab would still
      have taken the clock off it.
    - **While** connected. Asked of the registry on every write rather than
      remembered, so a panel that is unplugged for the winter stops owning a
      clock nobody can ask it about. The cost is a flap — a tab that pins
      inside a wifi blip gets the clock, and only a panel with a pin of its
      own takes it back on reconnect; a panel with none leaves the tab's
      town up until a restart, which is still less than a frozen clock.
    """
    print("\n== which page the clock's position belongs to ==")

    screen = screensaver.screen
    # Invented, because the two towns only have to be far enough apart that
    # the wrong one is obvious in a diff of the readings.
    panel_pin = {"lat": 41.111, "lng": 12.222}
    phone_pin = {"lat": -8.333, "lng": 101.444}
    NORTHPORT = (41.111, 12.222, "Northport")
    SOUTHMERE = (-8.333, 101.444, "Southmere")
    HAKONE_ENV = (35.232, 139.107, "Hakone, Japan")

    plugged: list[tuple[str, object]] = []

    def plug(session="kitchen"):
        """A panel's socket, as far as anything asking the registry can tell."""
        def send(frame):
            return None
        plugged.append((session, send))
        screen.register(session, send, kiosk=True)

    def unplug():
        session, send = plugged.pop()
        screen.unregister(session, send)

    params = inspect.signature(screensaver.set_home).parameters
    check("the handshake can say which kind of page it is",
          list(params), ["coords", "place", "kiosk"])
    check("and a caller with no opinion is not a panel",
          params["kiosk"].default, False,
          "so every existing caller keeps meaning what it meant")

    screensaver._told = None
    screensaver._shown = None
    screensaver._seen = None
    try:
        with env(HOME_COORDS="35.232,139.107", HOME_LOCATION="Hakone, Japan"):
            # The registry's own answer, which is the whole of the rule. It
            # sweeps every session because the caller has none: the position
            # is per process and the refresh loop that reads it back is not
            # on a socket at all.
            check("with nothing connected, no panel is", screen.any_kiosk(),
                  False)
            screen.register("laptop", lambda frame: None)
            check("and a browser tab is not one either", screen.any_kiosk(),
                  False, "`kiosk` is the `?kiosk=1` the launcher put there")
            plug("kitchen")
            check("a panel on any session is", screen.any_kiosk(), True)

            # #131 on the machine it was reported from: a Mac, no panel.
            unplug()
            screen.unregister("laptop")
            check("with no panel up a page's pin still moves the clock",
                  screensaver.set_home(phone_pin, "Southmere"), True)
            check("which is #131, and still the answer", screensaver.where(),
                  SOUTHMERE)

            # The panel arrives and pins. Its own write is authorised by the
            # flag and not by the registry, because a handshake reaches
            # `set_home` before it reaches `screen.register` — the panel is
            # not yet in the registry it would be looking itself up in.
            plug()
            check("a panel's pin takes the clock", screensaver.set_home(
                panel_pin, "Northport", True), True)
            check("and a phone's does not take it back",
                  screensaver.set_home(phone_pin, "Southmere"), False,
                  "the same False as a pin that did not move, so the "
                  "handshake logs nothing and the page is told nothing")
            check("so the clock stays on the panel's town",
                  screensaver.where(), NORTHPORT)
            check("the panel can still move its own",
                  screensaver.set_home(phone_pin, "Southmere", True), True)

            # #154's terminal exactly: a panel that has never pinned, because
            # nothing was configured for it to pin. Ownership that waited for
            # a pin would leave this open, and this is the reported bug.
            screensaver._told = None
            check("a panel with nothing to send sends nothing to keep",
                  screensaver.set_home(None, "", True), False)
            check("but it is still the page the clock belongs to",
                  screensaver.set_home(phone_pin, "Southmere"), False)
            check("so an unpinned panel falls through to .env, not to a phone",
                  screensaver.where(), HAKONE_ENV, "#154's configuration")

            # And the same terminal before #154, with no HOME_COORDS either.
            with env(HOME_COORDS=None, HOME_LOCATION=None):
                check("and with nothing configured either, to nothing",
                      screensaver.where(), None,
                      "a face with no place on it, which is what it had")

            # The socket goes for good. The pin outlives it — the loop has to
            # have something to ask about — but the ownership does not.
            screensaver.set_home(panel_pin, "Northport", True)
            unplug()
            check("a pin outlives the socket that carried it",
                  screensaver.where(), NORTHPORT)
            check("but owning the clock does not",
                  screensaver.set_home(phone_pin, "Southmere"), True)
            check("so a household whose panel is unplugged can move it again",
                  screensaver.where(), SOUTHMERE,
                  "a remembered flag would have frozen this until a restart")

            # The flap that costs, named so that a future reader knows it was
            # priced rather than missed.
            plug()
            screensaver.set_home(panel_pin, "Northport", True)
            unplug()                                     # a wifi blip
            check("a phone that pins inside a blip does get the clock",
                  screensaver.set_home(phone_pin, "Southmere"), True)
            plug()
            check("and the panel's reconnect takes it straight back",
                  screensaver.set_home(panel_pin, "Northport", True), True)
            check("so the clock is back on the panel's town",
                  screensaver.where(), NORTHPORT)
            unplug()

            # "I set the kitchen panel's location from my laptop." Still
            # works, and it is worth a case because it is the workflow the
            # rule looks like it breaks: the panel comes up carrying no pin,
            # and no pin has never replaced a position.
            screensaver._told = None
            check("a laptop can pin while the panel is off",
                  screensaver.set_home(phone_pin, "Southmere"), True)
            plug()
            check("and the panel coming up with none does not clear it",
                  screensaver.set_home(None, "", True), False)
            check("so the clock is where the laptop put it",
                  screensaver.where(), SOUTHMERE)

            # Through the loop, because a refusal that the reading did not
            # follow would be a green case and a wrong panel.
            counted = []

            def once(lat, lng, past_days=0):
                counted.append((lat, lng))
                return forecast(past_days)

            was = weather._fetch
            weather._fetch = once
            try:
                screensaver.set_home(panel_pin, "Northport", True)
                screensaver.set_home(phone_pin, "Southmere")
                check("the tick after a refused pin asks about the panel's",
                      await screensaver.refresh_once(), True)
                check("of the panel's own coordinates", counted,
                      [(41.111, 12.222)])
            finally:
                weather._fetch = was
            check("and the face names the panel's town",
                  screensaver._shown["place"], "Northport")
            check("and the picture is drawn from that place's own week",
                  screensaver._seen["recent"], RECENT,
                  "no hemisphere to get right: the numbers say which it is")
    finally:
        while plugged:
            unplug()
        screen._SINKS.clear()
        screensaver._told = None
        screensaver._shown = None
        screensaver._seen = None
        screensaver.home_path().unlink(missing_ok=True)


def handshake_cases(main) -> None:
    """The one line that carries the pin from the socket to the clock.

    Read out of the source rather than driven, because driving it means a
    real handshake and that is `wiring_check`'s half of the server. Read as
    a tree rather than as a substring: the argument that was added here is
    the whole of the fix, and a substring case is green against a call that
    passes it positionally in the wrong order or not at all.
    """
    print("\n== the line the handshake meets the clock on ==")

    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "set_home"]
    check("the handshake is the only place the two meet", len(calls), 1)

    call = calls[0]
    check("and it carries the position off the same query string the "
          "agent's own comes off",
          [getattr(arg, "id", None) for arg in call.args],
          ["home_coords", "home_location", "kiosk"])
    check("with nothing hidden in a keyword", call.keywords, [])

    owners = [fn.name for fn in ast.walk(tree)
              if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
              and any(node is call for node in ast.walk(fn))]
    check("on the socket, where there is a page to be right about",
          "websocket_endpoint" in owners, True)

    # And that the flag is the handshake's own reading rather than a second
    # one taken here, which could drift from the one `screen.register` gets.
    assigned = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "kiosk"
                        for target in node.targets)]
    check("`kiosk` is read once for the connection", len(assigned), 1)
    check("by the same function the screen registry is told with",
          getattr(getattr(assigned[0].value, "func", None), "id", None),
          "_is_kiosk")


# ------------------------------------------------------ what to draw, and of

SEASONS = ("winter", "spring", "summer", "autumn")


def season_cases(screensaver) -> None:
    """That the picture is drawn from observations rather than from a word.

    #185: the prompt used to derive `autumn` from the month and then forbid
    the frame to contradict it, so early September in Tokyo — 30 °C and full
    green — was drawn as fall foliage and the code was doing what it said.
    What replaces it is the date, the week behind it and the hour, and no
    conclusion at all.
    """
    print("\n== the picture is drawn from the weather, not from the month ==")

    # The whole year, at the temperature the bug was reported at. Every month
    # rather than September, because the defect was a table and a table is
    # only ever proved gone by reading all of it.
    hot = dict(SEEN, temperature=30.2,
               recent={"days": 7, "low": 24.1, "high": 33.4, "mean": 27.9})
    said = {word
            for month in range(1, 13)
            for word in SEASONS
            if word in screensaver._prompt(BLED, hot, date(2026, month, 10))}
    check("no month puts a season word in the prompt", sorted(said), [],
          "the model is handed observations and draws its own conclusion")

    # Deliberately no case asserting `_SEASONS`, `_OPPOSITE` and `_season` are
    # gone. It would be a structural claim with no observable consequence —
    # the case above already fails if any of them comes back into the prompt,
    # and one that names them only ratchets against a refactor that puts a
    # month lookup somewhere legitimate.

    # The 残暑 case itself, in the words the panel was reported on. What it
    # adds to the whole-year case above is the temperature: the two numbers
    # that used to contradict each other are both in the sentence now, and
    # neither is asserted over the other.
    september = screensaver._prompt(BLED, hot, date(2026, 9, 10))
    check("the September the panel got wrong now carries both its numbers",
          ("30 degrees" in september and "24.1 to 33.4" in september), True,
          "which reads as 残暑 rather than as autumn")

    # The amendment, and the case that fails if someone later simplifies the
    # date back out. Seven days of numbers cannot tell 新緑 from 紅葉: Tokyo
    # runs about 15 °C in April and 18 °C in October and the windows overlap.
    mild = {"days": 7, "low": 11.0, "high": 20.0, "mean": 15.5}
    april = screensaver._prompt(BLED, dict(SEEN, temperature=15.0,
                                           recent=mild), date(2026, 4, 12))
    october = screensaver._prompt(BLED, dict(SEEN, temperature=15.0,
                                             recent=mild), date(2026, 10, 12))
    check("April and October at the same temperature are different prompts",
          april != october, True,
          "the date is the only thing that can separate 新緑 from 紅葉")
    check("and the difference is the date and nothing else",
          (april.replace("12 April", "X") == october.replace("12 October", "X")),
          True)

    # The hemisphere flip is not merely absent, it is not needed: a Sydney
    # January arrives as a week of numbers that says summer by themselves.
    sydney = screensaver._prompt(BLED, dict(SEEN, temperature=29.0,
                                            recent={"days": 7, "low": 20.1,
                                                    "high": 33.0,
                                                    "mean": 26.4}),
                                 date(2026, 1, 20))
    check("a Sydney January carries its own summer with it",
          "20 January" in sydney and "20.1 to 33.0" in sydney, True,
          "no latitude, no flip, and nothing to get the hemisphere wrong")
    check("and no latitude is kept for one",
          "latitude" in SEEN, False)


def window_cases(screensaver) -> None:
    """How far back the picture looks, and every way `.env` gets it wrong."""
    print("\n== how many days of weather the picture is drawn from ==")

    with env(SCREENSAVER_SEASON_DAYS=None):
        check("unset is seven days", screensaver.season_days(), 7,
              "a Pi whose .env predates #185 keeps working")
    with env(SCREENSAVER_SEASON_DAYS="10"):
        check("and a number is that number", screensaver.season_days(), 10,
              "so tuning it is one line in .env and a restart")

    # The empty value is the one that actually arrives, out of a line copied
    # from `.env.example` with the value cut off. It cannot be left to
    # `pi_deploy.py check` — that reports it, since `21277b2`, but only when
    # somebody runs it, and `int("")` raises inside a background task.
    for bad, why in (("", "a line copied out of .env.example, value and all"),
                     ("   ", "and whitespace is the same nothing"),
                     ("seven", "a typo is not worth a failed boot"),
                     ("0", "zero days of history is no history"),
                     ("-3", "nor is a negative window"),
                     ("400", "Open-Meteo caps `past_days` at 92")):
        with env(SCREENSAVER_SEASON_DAYS=bad):
            check(f"{bad!r} falls back to seven",
                  screensaver.season_days(), 7, why)

    with env(SCREENSAVER_SEASON_DAYS=""):
        check("and the prompt still renders over the fallback",
              "24.1 to 33.4" in screensaver._prompt(BLED, SEEN,
                                                    date(2026, 9, 10)),
              True, "a screensaver is not worth a failed boot over a typo")


class Answered:
    """The two methods `_fetch` calls on whatever `requests.get` hands back."""

    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise weather.requests.HTTPError(str(self.status_code))

    def json(self) -> dict:
        return self._body


async def fetch_cases(screensaver) -> None:
    """What the week costs on the wire, and what happens when it is refused.

    Everything above is about the string the model is handed. This is about
    the request behind it, which is where #185's two remaining risks live:
    `past_days` more than doubles the day-units a screensaver spends, and it
    is carried by the same request as the temperature under the clock, so a
    provider that refuses it takes the reading down with it.
    """
    print("\n== how often the week is asked for ==")

    asked: list[int] = []
    was_fetch = weather._fetch

    def record(lat, lng, past_days=0):
        asked.append(past_days)
        return forecast(past_days)

    try:
        weather._fetch = record
        screensaver._recent_on = None
        for _ in range(3):
            screensaver._weather_attempted_at = None
            await screensaver.refresh_once()
        # 48 refreshes a day against a five-day forecast is 240 day-units;
        # asking for a week on every one of them is 576, and
        # `screensaver.py:71-73` promises the tick stays "well inside what a
        # keyless caller is asked to use". Yesterday's high does not move, so
        # one tick a day pays for it and the other 47 ask for the card.
        check("the week is asked for on the first tick of the day only",
              asked, [7, 0, 0],
              "and the other 47 ticks cost exactly what the card costs")
        check("and the picture is still drawn from it on the ticks that "
              "did not ask", screensaver._seen["recent"], RECENT)

        asked.clear()
        screensaver._recent_on = ("1999-12-31", None)
        screensaver._weather_attempted_at = None
        await screensaver.refresh_once()
        check("a day that rolled over asks again", asked, [7],
              "the date is the ceiling, which is why there is not a second")

        asked.clear()
        screensaver._recent_on = None

        def blank(lat, lng, past_days=0):
            asked.append(past_days)
            return {}

        weather._fetch = blank
        screensaver._weather_attempted_at = None
        await screensaver.refresh_once()
        weather._fetch = record
        screensaver._weather_attempted_at = None
        await screensaver.refresh_once()
        check("a reading that was thrown out does not spend the day's week",
              asked, [7, 7],
              "or one blank 200 at 00:03 costs the week until midnight")
        check("and the week is back in the picture on the next good tick",
              screensaver._seen["recent"], RECENT)

        # The knob has to reach the wire, not merely parse. Nothing else here
        # would notice `season_days()` being computed and then dropped.
        asked.clear()
        screensaver._recent_on = None
        screensaver._weather_attempted_at = None
        with env(SCREENSAVER_SEASON_DAYS="10"):
            await screensaver.refresh_once()
        check("and .env decides how far back that one request looks",
              asked, [10])

        # The card's own path, which is every weather question anyone asks.
        # It must never pay for history: `weather()` does not pass
        # `past_days` and there is nothing on the card that would read it.
        asked.clear()
        await weather.weather(location="Hakone")
        check("the card's path never asks for any of it", asked, [0])
    finally:
        weather._fetch = was_fetch
        screensaver._recent_on = None

    print("\n== a request the provider refuses, and one that answers blind ==")

    sent: list[dict] = []
    was_get = weather.requests.get

    def refusing(url, params=None, timeout=None):
        sent.append(dict(params or {}))
        if "past_days" in (params or {}):
            return Answered(400, {})
        return Answered(200, forecast(0))

    try:
        weather.requests.get = refusing
        weather._fetch = _FETCH
        raw = weather._weather_sync(HAKONE[0], HAKONE[1], "Hakone", "en", 7)
    finally:
        weather.requests.get = was_get
        weather._fetch = was_fetch

    # `PAST_DAYS_MAX` is a remembered number, not a measured one — nothing in
    # this repo records Open-Meteo's limits. So the clamp is not what keeps a
    # bad `SCREENSAVER_SEASON_DAYS` from freezing the panel; this is. Without
    # the retry the 400 takes down the request that also carries the
    # temperature on the clock, `refresh_once` returns False, and the face
    # holds its last reading with nothing on screen to say why.
    check("a refused past_days is dropped and the reading asked for again",
          ["past_days" in params for params in sent], [True, False])
    check("the request asks for the jma_seamless model",
          [params.get("models") for params in sent],
          ["jma_seamless", "jma_seamless"])
    check("so the temperature the clock needs still arrives",
          raw["current"]["temperature"], 24.1)
    check("and no week is claimed on the strength of a request that failed",
          "recent" in raw, False)

    # Coordinates outside Japan or a model Open-Meteo refuses must not take
    # down the reading: if `models` is rejected with 400, drop it and ask
    # once more without it (#274).
    sent_models: list[dict] = []

    def refusing_models(url, params=None, timeout=None):
        sent_models.append(dict(params or {}))
        if "models" in (params or {}):
            return Answered(400, {})
        return Answered(200, forecast(0))

    try:
        weather.requests.get = refusing_models
        weather._fetch = _FETCH
        raw_models = weather._weather_sync(HAKONE[0], HAKONE[1], "Hakone", "en")
    finally:
        weather.requests.get = was_get
        weather._fetch = was_fetch

    check("a refused models is dropped and the reading asked for again",
          ["models" in params for params in sent_models], [True, False])
    check("so the reading still arrives when models is refused",
          raw_models["current"]["temperature"], 24.1)

    # The other end of the same coupling. `_weather_sync` falls back to *this*
    # machine's clock when the provider sends no `current.time`, and a Pi in
    # JST asking about London at 08:00 is a day ahead of it. Cutting the daily
    # arrays against that date takes a real forecast row off the card and
    # files it as history — measured in #185's review as rows=4 with a
    # `recent` of one day. Nothing before today can arrive unasked, so the
    # card path does not cut at all.
    blind = {**forecast(0),
             "current": {key: value
                         for key, value in FORECAST["current"].items()
                         if key != "time"}}
    try:
        weather.requests.get = lambda url, params=None, timeout=None: \
            Answered(200, blind)
        weather._fetch = _FETCH
        card = weather._weather_sync(HAKONE[0], HAKONE[1], "Hakone", "en")
    finally:
        weather.requests.get = was_get
        weather._fetch = was_fetch

    check("a reading with no clock in it keeps all five forecast days",
          len(card["daily"]), 5)
    check("and claims no history it was never sent", "recent" in card, False)
    check("with the case above measuring something: the fixture's first row "
          "is behind the clock",
          forecast(0)["daily"]["time"][0] < date.today().isoformat(), True,
          "or the cut it is about would land at zero and take nothing")

    # A language added to `_WEEKDAYS` but missing from `_NAMED` must not
    # KeyError on the first row: `_base` gates both tables, not just weekdays.
    was_weekdays = weather._WEEKDAYS
    try:
        weather._WEEKDAYS = {**was_weekdays,
                             "de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]}
        check("a language missing from _NAMED falls back to English",
              weather._base("de"), "en")
        check("and the label builder builds rows without KeyError",
              [r["label"] for r in weather._daily(forecast(0), "de")[:2]],
              ["Today", "Tomorrow"])
    finally:
        weather._WEEKDAYS = was_weekdays


def prompt_cases(screensaver) -> None:
    print("\n== the prompt, from a weather payload ==")

    # Asserted whole rather than by containment. This string is the entire
    # product: everything else in the feature is about when to send it.
    check("a September overcast afternoon in Hakone asks for exactly this",
          screensaver._prompt(BLED, SEEN, date(2026, 9, 9)),
          "A photorealistic photograph of the shore of Lake Bled, "
          "Slovenia, outdoors, taken by day on 9 September. Over the 7 "
          "days before today the temperature here has run 24.1 to 33.4 "
          "degrees Celsius, averaging 27.9. Right now it is overcast, at "
          "about 24 degrees Celsius, and the light matches that weather "
          "and that hour. Draw the vegetation, the ground and what anyone "
          "in the distance is wearing as they would actually be in this "
          "place under those conditions.\n"
          "Wide landscape composition, horizon in the middle third, open "
          "sky at the top of the frame and open ground or water at the "
          "bottom.\n"
          "No people in the foreground, no text, no captions, no "
          "watermarks, no logos, no borders.",
          "the whole of it, because the whole of it is what is paid for")

    # The composition sentence is not decoration: #84's scrim is darkest
    # across the top and bottom bands, so a subject that lives there is a
    # subject nobody ever sees.
    body = screensaver._prompt(BLED, SEEN, date(2026, 9, 9))
    check("the frame is described as well as the subject",
          "horizon in the middle third" in body, True,
          "or the picture is drawn where the clock's scrim is darkest")

    check("at night it says so", "taken after dark on"
          in screensaver._prompt(BLED, dict(SEEN, is_day=False),
                                 date(2026, 9, 9)), True)
    check("the temperature is rounded, because nobody photographs a decimal",
          "about 24 degrees" in body, True)
    check("and it rounds the cold way too",
          "about -4 degrees" in screensaver._prompt(
              BLED, dict(SEEN, temperature=-3.6), date(2026, 9, 9)), True)
    check("the condition word is lowercased into the sentence",
          "is overcast, at about" in body, True,
          "`weather._CONDITIONS` stores it sentence-cased for a card heading")

    # A reading that came back with no week in it: a provider that ignored
    # `past_days`, or a first refresh whose daily arrays were empty. The
    # clause has to be absent rather than formatted with Nones in it.
    thin = screensaver._prompt(BLED, dict(SEEN, recent=None),
                               date(2026, 9, 9))
    check("a reading with no week behind it leaves the clause out",
          ("None" not in thin and "Over the" not in thin
           and "on 9 September. Right now it is overcast" in thin), True,
          "the model is told what is known and nothing that is not")
    check("and the same for a reading that predates the key altogether",
          screensaver._prompt(BLED, {k: v for k, v in SEEN.items()
                                     if k != "recent"}, date(2026, 9, 9)),
          thin, "an older `_seen` still renders")


def place_cases(screensaver) -> None:
    print("\n== which place, and never the same one twice running ==")

    check("no place is written down twice",
          len(set(screensaver.PLACES)), len(screensaver.PLACES))
    check("and there are more of them than the window holds",
          len(screensaver.PLACES) > screensaver.PLACE_WINDOW, True,
          f"{len(screensaver.PLACES)} places, "
          f"window of {screensaver.PLACE_WINDOW}")

    # A year of generations at four a day, carrying the window forward the
    # way `generate_once` carries it. Seeded, so a run that goes wrong goes
    # wrong the same way twice.
    rng = random.Random(86)
    window, repeats, drawn = [], [], set()
    for _ in range(4 * 365):
        place = screensaver._pick_place(window, rng)
        if place in window:
            repeats.append(place)
        drawn.add(place)
        window = (window + [place])[-screensaver.PLACE_WINDOW:]
    check("a year of picks repeats nothing inside the window", repeats, [])
    check("and it is not repeating nothing by using one place",
          len(drawn), len(screensaver.PLACES))

    # The list shortened below the window is somebody else's future edit, and
    # the failure mode is `random.choice` on an empty sequence — an
    # `IndexError` inside a background task, which is a panel that silently
    # stops changing.
    check("a window that has eaten the whole list still answers",
          screensaver._pick_place(list(screensaver.PLACES), rng)
          in screensaver.PLACES, True)


# --------------------------------------------- reading the model's answer

def blob_cases(screensaver) -> None:
    print("\n== getting the bytes back out of the answer ==")

    photo = types.Part(inline_data=types.Blob(data=NEW, mime_type="image/png"))
    check("the picture comes out of the inline data",
          screensaver._bytes_of(answered(photo)), NEW)
    check("past whatever the model wrote beside it",
          screensaver._bytes_of(answered(types.Part(text="Here you go."),
                                         photo)), NEW)

    # The one that shows on the panel and nowhere else. An empty file passes
    # the route's `is_file()`, so it is served as zero bytes under a 200 and
    # #84's `<img>` fails to load with nothing saying why.
    check("a Blob that came back with no data is not a picture",
          screensaver._bytes_of(answered(types.Part(
              inline_data=types.Blob(mime_type="image/png")))), None,
          "b'' here is a 0-byte file the clock then tries to draw")
    check("nor is one with nothing saying it is an image",
          screensaver._bytes_of(answered(types.Part(
              inline_data=types.Blob(data=NEW)))), None,
          "the rule `Blob.as_image` applies to the same object")
    check("nor is a refusal, which arrives as text",
          screensaver._bytes_of(answered(types.Part(text="I can't."))), None)
    check("and an answer with no candidates in it is not one either",
          screensaver._bytes_of(types.GenerateContentResponse()), None,
          "`response.parts` is None there, not []")


def request_cases(screensaver, generate_sync) -> None:
    """What would go on the wire, with nothing on the other end of it.

    The real `_generate_sync`, handed in because the driver has already put a
    stub over the module's copy of it — this is the only case that runs the
    function itself, and it runs it against a client that is two classes.
    """
    print("\n== what is asked for, with no client behind it ==")

    sent = {}

    class Models:
        def generate_content(self, **kwargs):
            sent.update(kwargs)
            return answered(types.Part(inline_data=types.Blob(
                data=NEW, mime_type="image/png")))

    class Client:
        models = Models()

    was = screensaver.global_client
    screensaver.global_client = Client
    try:
        with env(SCREENSAVER_MODEL=None):
            got = generate_sync("draw me a lake")
    finally:
        screensaver.global_client = was

    check("the prompt is the whole of the contents",
          sent.get("contents"), "draw me a lake")
    check("on the model .env names, defaulting to nano banana pro",
          sent.get("model"), "gemini-3-pro-image")
    check("asked for as content and not through the Imagen surface",
          sent["config"].response_modalities, ["TEXT", "IMAGE"],
          "`generate_images` raises off Vertex and builds a :predict path")
    check("in the shape the panel crops from",
          sent["config"].image_config.aspect_ratio, "16:9",
          "1280x800 under `object-fit: cover`")
    check("at a size worth cropping",
          sent["config"].image_config.image_size, "2K")
    check("with nothing said about people, because the prompt says it",
          sent["config"].image_config.person_generation, None,
          "ALLOW_NONE would contradict `anyone in the distance`")
    check("and the bytes come back out of the answer", got, NEW)

    with env(SCREENSAVER_MODEL="nano-banana-pro-preview"):
        check(".env moves the id, because it took a live call to settle",
              screensaver.image_model(), "nano-banana-pro-preview")


# ------------------------------------------- when a picture is worth buying

def named(slots) -> list:
    """Just the names out of a `_slots` answer, in the order it gives them."""
    return [name for _, _, name in slots]


def slot_cases(screensaver) -> None:
    """The seven times of day, and which of them is the current one.

    The owner asked for sunrise minus, on and plus an hour, the same three
    around sunset, and 21:00. Six of the seven therefore move with the date
    and with the latitude, which is the whole difference between this and an
    interval — and it is why every case here hands the sun times in rather
    than reading a clock.
    """
    print("\n== seven slots a local day, six of them the sun's ==")

    check("the day has the seven the owner asked for",
          named(screensaver._slots(SEEN)),
          ["dawn-1", "dawn", "dawn+1", "dusk-1", "dusk", "dusk+1", "night"])
    check("an hour either side of a 05:23 sunrise and a 17:52 sunset",
          [minute for minute, _, _ in screensaver._slots(SEEN)],
          [263, 323, 383, 1012, 1072, 1132, 1260],
          "minutes past local midnight, straight off the two local stamps")

    # The most recent slot at or before now, at each of the seven and in the
    # long gaps between them. The identity carries the local date, which is
    # what makes a slot fire once on the day it belongs to.
    for label, hour, want in (
            ("04:30, an hour before the sun", 4.5, "dawn-1"),
            ("05:30, just after it is up", 5.5, "dawn"),
            ("06:30, an hour on", 6.5, "dawn+1"),
            ("12:00, with nothing due since", 12.0, "dawn+1"),
            ("17:00, an hour before sunset", 17.0, "dusk-1"),
            ("18:00, just after it", 18.0, "dusk"),
            ("19:00, an hour on", 19.0, "dusk+1"),
            ("21:30, on the wall clock", 21.5, "night")):
        check(f"at {label} the slot is `{want}`",
              screensaver._slot_at(at(hour), SEEN), f"2026-01-14/{want}")

    check("at 00:30 nothing is due yet today, so it is yesterday's last",
          screensaver._slot_at(at(0.5), SEEN), "2026-01-13/night",
          "yesterday's identity, so today's `night` is still owed a picture")
    check("and at 03:00 it is still that one",
          screensaver._slot_at(at(3), SEEN), "2026-01-13/night")

    # A terminal that has not reached Open-Meteo yet. `night` is a wall clock
    # and survives; the six sun slots are skipped rather than guessed at,
    # because a stand-in sunrise of 06:00 is the fixed photograph this module
    # exists to remove, wearing a schedule.
    check("a terminal with no weather yet has only the wall-clock slot",
          named(screensaver._slots(None)), ["night"])
    check("and an empty reading says the same",
          named(screensaver._slots({})), ["night"])

    # #113: Open-Meteo answers a polar day and a polar night with a midnight
    # stamp at both ends, not a null. Read one at a time those are two times
    # and the six sun slots bunch onto 23:00, 00:00 and 01:00 twice over.
    # Read as a pair they are the fact that the sun did not cross the horizon.
    polar = dict(SEEN, sunrise="00:00", sunset="00:00")
    check("a polar day has no sunrise for six of them to hang off",
          named(screensaver._slots(polar)), ["night"],
          "two equal stamps, which is what the API actually sends")
    check("so 09:00 inside one resolves to the night before",
          screensaver._slot_at(at(9), polar), "2026-01-13/night")
    check("and 21:30 to the one that is starting",
          screensaver._slot_at(at(21.5), polar), "2026-01-14/night")

    # The pair above is equal because `_clock` has already been over it, and
    # the two are not equal before that. Asked over the network on
    # 2026-09-09, a polar day and a polar night are two different shapes:
    # 89.9°N, where the sun does not set, answers `2026-09-09T00:00` for the
    # sunrise and `2026-09-10T00:00` for the sunset — *tomorrow's* midnight
    # — while 89.9°S the same day, where it does not rise, sends
    # `2026-09-10T00:00` at both ends, genuinely doubled. `_clock` slices
    # `[11:16]` off both, so the day arrives here as one fact rather than as
    # a sunrise and a sunset twenty-four hours apart.
    #
    # `_hhmm` would refuse a whole stamp anyway and the answer would be the
    # same `night`, so this is the outer of two guards rather than the only
    # one. It is pinned because the shape it records is not the shape the
    # comment it came from records, and because the equality arm firing at
    # all on a polar day is a fact about `_clock`'s slicing rather than
    # about the API.
    day = {"sunrise": weather._clock("2026-09-09T00:00"),
           "sunset": weather._clock("2026-09-10T00:00")}
    check("a real polar day sends tomorrow's midnight for the sunset",
          [day["sunrise"], day["sunset"]], ["00:00", "00:00"],
          "two different stamps rendering to one time")
    check("so the rendered pair is what says the sun did not rise",
          named(screensaver._slots(day)), ["night"])
    night = weather._clock("2026-09-10T00:00")
    check("and a polar night doubles the stamp itself, date and all",
          named(screensaver._slots({"sunrise": night, "sunset": night})),
          ["night"], "89.9°S on the same day, both ends 2026-09-10T00:00")

    # What the card is told on that day is #113's to decide and is not
    # settled yet. Every answer it could give has to arrive here as the same
    # fact, so that landing it moves the screen and not the schedule.
    for rendered in ("", "Midnight sun", "白夜", None):
        check(f"and {rendered!r} at both ends is the same answer",
              named(screensaver._slots(
                  dict(SEEN, sunrise=rendered, sunset=rendered))), ["night"])
    check("one end a word and the other a time is no schedule either",
          named(screensaver._slots(dict(SEEN, sunrise="Midnight sun"))),
          ["night"], "a pair or nothing; half of one is not a sunrise")

    # And the shoulder weeks, which the fix for the polar day must not eat.
    # Near the circle the sun really does come up at 00:14 and set at 23:47,
    # and those are two different stamps.
    shoulder = dict(SEEN, sunrise="00:14", sunset="23:47")
    check("a genuine near-midnight sunrise still gets its slots",
          named(screensaver._slots(shoulder)),
          ["dawn", "dawn+1", "night", "dusk-1", "dusk"],
          "`dawn-1` is 23:14 yesterday and `dusk+1` 00:47 tomorrow")
    check("a slot computed past midnight is dropped, not carried",
          named(screensaver._slots(dict(SEEN, sunset="23:40"))),
          ["dawn-1", "dawn", "dawn+1", "night", "dusk-1", "dusk"],
          "resolving it would need yesterday's sun times, which nothing has")

    # Above about 55 degrees in summer `dusk-1` lands on 21:00 itself. The
    # sort has exactly one answer, so the later slot wins and the earlier is
    # skipped for that day: one picture, not two.
    nordic = dict(SEEN, sunrise="04:02", sunset="22:00")
    check("`dusk-1` on the same minute as `night` is still one of the seven",
          named(screensaver._slots(nordic)),
          ["dawn-1", "dawn", "dawn+1", "dusk-1", "night", "dusk", "dusk+1"])
    check("but never the answer, so the tick that covers both draws once",
          screensaver._slot_at(at(21) + 60, nordic), "2026-01-14/night")

    # The minute itself. A ten-minute refresh lands on :00 more often than
    # anywhere else, so `<=` rather than `<` is the difference between a slot
    # starting on time and every slot starting a tick late, every day.
    check("a tick exactly on the slot's own minute is inside it",
          screensaver._slot_at(at(21), SEEN), "2026-01-14/night",
          "21:00:00 is `night` and not the `dusk+1` before it")
    check("and the same on the minute the API named",
          screensaver._slot_at(at(5) + 23 * 60, SEEN), "2026-01-14/dawn",
          "05:23 exactly, the stamp the payload sent")

    # Two numbers with a colon between them is not yet a time. Both of these
    # parse, and both would hang real slots off an hour that does not exist
    # if the pair were only split and not range-checked.
    check("an out-of-range minute is not a sunrise",
          named(screensaver._slots(dict(SEEN, sunrise="12:99"))), ["night"])
    check("nor is a negative hour, which would put `dawn+1` at midnight",
          named(screensaver._slots(dict(SEEN, sunrise="-1:00"))), ["night"])

    # The day the clocks move. Nothing converts anywhere on this path: the
    # request carries `timezone=auto`, so the two stamps are already local
    # wall times, and the slot is compared against a local wall time too.
    with tz("America/New_York"):
        check("the morning the clocks spring forward needs no conversion",
              screensaver._slot_at(
                  datetime(2026, 3, 8, 7, 30).timestamp(),
                  dict(SEEN, sunrise="07:22", sunset="19:02")),
              "2026-03-08/dawn",
              "07:22 EDT is what the API said, 07:30 EDT what the Pi says")
        check("and the hour that vanished takes no slot with it",
              named(screensaver._slots(
                  dict(SEEN, sunrise="07:22", sunset="19:02"))),
              ["dawn-1", "dawn", "dawn+1", "dusk-1", "dusk", "dusk+1",
               "night"])


def citation_cases(screensaver) -> None:
    """The two references in `_sun`'s docstring, followed to what they name.

    #116 was three landed sentences describing mechanisms that had stopped
    happening, and the hardest of them to catch was a citation naming a real
    function for the wrong claim: `sun_cases`, which exists, where the
    behaviour is pinned in `slot_cases`. A dead reference shows up the next
    time someone follows it; a live one pointing somewhere else reads as
    checked. So these follow both references rather than the prose around
    them, which is the half of the sentence that can be gated.
    """
    print("\n== the references in `_sun`'s docstring ==")

    doc = inspect.getdoc(screensaver._sun) or ""
    named = re.search(r"screensaver_check\.(\w+)", doc)
    where = named.group(1) if named else ""
    check("the function `_sun` cites for the pair is one in this file",
          where in globals(), True, where or "it cites no function at all")

    body = inspect.getsource(globals()[where]) if where in globals() else ""
    check("and it is fed the midnight pair Open-Meteo answers with",
          'sunrise="00:00", sunset="00:00"' in body, True)
    check("and the empty pair `weather._sun` sends in its place",
          '("", "Midnight sun"' in body, True,
          "both shapes in one function is what the docstring claims")

    source = (Path(__file__).resolve().parent.parent
              / "src/ai_terminal/live/tools/weather.py")
    lines = source.read_text().splitlines()
    span = re.search(r"weather\.py:(\d+)-(\d+)", doc)
    cited = ("\n".join(lines[int(span.group(1)) - 1:int(span.group(2))])
             if span else "")
    check("and the weather.py lines it cites are the ones that empty it",
          'first = last = ""' in cited, True,
          span.group(0) if span else "no line reference to follow")


def due_cases(screensaver) -> None:
    print("\n== whether this slot has had its picture yet ==")

    check("there is no interval left for .env to move",
          [hasattr(screensaver, name)
           for name in ("interval_s", "BACKGROUND_S")], [False, False],
          "the schedule is the feature, and a knob nobody sets is untested")
    check("the retry after a failure is far shorter than a slot",
          screensaver.RETRY_S < 3600, True, f"{screensaver.RETRY_S}s")

    check("a terminal that has never generated is due at once",
          screensaver._due(at(5.5), "2026-01-14/dawn", None), True)
    check("the slot that has had its picture is not due again",
          screensaver._due(at(6), "2026-01-14/dawn", "2026-01-14/dawn"),
          False)
    check("the next one is",
          screensaver._due(at(6.5), "2026-01-14/dawn+1", "2026-01-14/dawn"),
          True)
    check("and the same name tomorrow is a different slot",
          screensaver._due(at(29.5), "2026-01-15/dawn", "2026-01-14/dawn"),
          True, "seven a day, not seven ever")

    # A Pi has no clock across a power cut: it comes up in the past and NTP
    # steps it forward. A `last_slot` from the far side of that step is not
    # the current identity, so it re-fires once and settles instead of
    # pinning the photograph until the two clocks agree.
    check("a `last_slot` from the future is due rather than pinning",
          screensaver._due(at(5.5), "2026-01-14/dawn", "2026-01-15/night"),
          True)

    # `_due` reads the monotonic clock and nothing else: its first argument
    # and `failed_at` are both readings of `time.monotonic()`, and the slot
    # names beside them are the only wall-clock thing on this call. The
    # numbers stay `at()`-shaped because all the backoff reads is a
    # difference, and a difference does not care where zero was.
    check("a failure two minutes ago is not retried yet",
          screensaver._due(at(6), "2026-01-14/dawn", None, at(6) - 120),
          False)
    check("and is once the backoff is up",
          screensaver._due(at(6), "2026-01-14/dawn", None,
                           at(6) - screensaver.RETRY_S), True)
    check("a failure does not re-open a slot already drawn for",
          screensaver._due(at(6), "2026-01-14/dawn", "2026-01-14/dawn",
                           at(6) - screensaver.RETRY_S), False)

    # Where the window starts, which is the same stamp twice. A failure this
    # instant is a live backoff rather than a stale one, so the tick that
    # stamped it does not also get to spend an attempt on it.
    check("and a failure stamped this instant is the live end of that",
          screensaver._due(at(6), "2026-01-14/dawn", None, at(6)), False)

    # The reboot, end to end and off disk. Six-hourly slots on the wall clock
    # mean a Pi restarted at 05:59 generates twice inside two minutes; what
    # stands between this one and doing the same is that the slot it last
    # drew for came back off disk rather than out of a global the reboot
    # emptied, and that it is a name rather than an age.
    screensaver._failed_at = None
    screensaver._write_state(
        screensaver._slot_at(at(5) + 40 * 60, LATE_SUN), [BLED])
    check("what the last generation left is on disk beside the picture",
          sorted(screensaver._read_state()), ["last_slot", "places"])
    check("a Pi rebooted at 05:59 reads back which slot that was",
          screensaver._read_state()["last_slot"], "2026-01-14/dawn-1")
    check("and does not spend a second picture inside it",
          screensaver._due(at(5) + 59 * 60,
                           screensaver._slot_at(at(5) + 59 * 60, LATE_SUN),
                           screensaver._read_state()["last_slot"]),
          False, "05:59 is still `dawn-1`, whatever the uptime says")
    check("while one that was off all afternoon draws the moment it is up",
          screensaver._due(at(20), screensaver._slot_at(at(20), LATE_SUN),
                           "2026-01-14/dawn"),
          True, "the most recent past slot, not the next one to come")

    screensaver.state_path().write_text("{ this is not json", encoding="utf-8")
    check("a sidecar that will not parse is the same as having none",
          screensaver._read_state(), {},
          "the worst a corrupt one may cost is one extra picture")
    screensaver.state_path().unlink()


async def generation_cases(client, screensaver, scratch: Path) -> None:
    print("\n== spending a picture, and not spending one ==")

    picture = scratch / screensaver.IMAGE_NAME
    picture.write_bytes(OLD)
    screensaver.state_path().unlink(missing_ok=True)
    screensaver._failed_at = None
    screensaver._refreshed_at = None
    screensaver._seen = None

    check("with no weather yet there is nothing to draw",
          await screensaver.generate_if_due(at(5.5)), False,
          "and it is not a failure either: nothing to back off from")
    check("and the picture already there is untouched",
          picture.read_bytes(), OLD)
    check("nor did that count as an attempt", screensaver._failed_at, None)

    screensaver._seen = dict(SEEN)
    screensaver._refreshed_at = time.monotonic()
    asked = []

    def drew(prompt):
        asked.append(prompt)
        return NEW

    def refused(prompt):
        asked.append(prompt)
        raise OSError("no route to host")

    was = screensaver._generate_sync
    screensaver._generate_sync = drew
    try:
        check("a terminal with a temperature and no picture generates one",
              await screensaver.generate_if_due(at(5.5)), True)
        check("the bytes on disk are the bytes the model gave",
              picture.read_bytes(), NEW)
        check("of a place off the list", place_in(screensaver, asked[0]) != "",
              True)
        check("on the local date it was asked on, not on a season word",
              "on 14 January" in asked[0]
              and not any(word in asked[0] for word in SEASONS), True,
              "the month reaches the model as an observation (#185)")
        check("and the slot it was drawn for is what is written down",
              screensaver._read_state()["last_slot"], "2026-01-14/dawn",
              "a name and a local date, not how long ago it happened")

        check("asking again inside the same slot spends nothing",
              await screensaver.generate_if_due(at(6)), False)
        check("and the model was not asked a second time", len(asked), 1)

        check("the next slot is what asks again",
              await screensaver.generate_if_due(at(7)), True,
              "an hour on, where an interval would still be counting")
        check("recorded by name too", screensaver._read_state()["last_slot"],
              "2026-01-14/dawn+1")
        check("and not for the place it has just been given",
              place_in(screensaver, asked[1])
              == place_in(screensaver, asked[0]), False)
        check("the window that says so is on disk, not in memory",
              screensaver._read_state()["places"],
              [place_in(screensaver, prompt) for prompt in asked])

        # `dawn+1` runs until an hour before sunset, which on this January
        # day is ten and a half hours. A slot is one picture however long it
        # is; the six-hourly rule this replaces would have bought a second
        # one in the middle of it, at an hour nobody chose.
        tried = len(asked)
        check("seven hours on, still inside that slot, nothing is spent",
              await screensaver.generate_if_due(at(14)), False)
        check("and the model was not asked then either", len(asked), tried)

        # The step that happens on this hardware, driven through the real
        # caller with the real clock. A Pi 5 has no RTC, so it boots on
        # whatever `fake-hwclock` saved and `systemd-timesyncd` steps the
        # wall clock forward once the WAN is up — which is the same moment a
        # backoff armed by that WAN going down is live. Both calls here are
        # a fraction of a second apart in real time and a quarter of an hour
        # and a second apart on the wall clock they are handed, so a backoff
        # measured on the wall clock is over and one measured on elapsed
        # time has barely started.
        tried = len(asked)
        screensaver._generate_sync = refused
        await screensaver.generate_if_due(at(17.5))
        await screensaver.generate_if_due(at(17.5) + screensaver.RETRY_S + 1)
        check("a wall clock stepped past the retry buys no early attempt",
              len(asked), tried + 1,
              "a quarter of an hour of step against no elapsed time at all")
        check("because what it waits on is a monotonic reading",
              abs(screensaver._failed_at - time.monotonic()) < 5.0, True,
              "a wall-clock stamp would be nine digits from this one")
        screensaver._failed_at = None

        # The failure this whole backoff exists for: the Wi-Fi blinked once.
        # What must not happen is the panel losing its photograph over it.
        # `mono` is handed in from here on, because the alternative to
        # driving that clock is a check script that sleeps for RETRY_S.
        check("a generation that throws says so rather than raising",
              await screensaver.generate_if_due(at(17.5), 0.0), False)
        check("the old picture is still the picture", picture.read_bytes(),
              NEW)
        check("`last_slot` did not move for a failure",
              screensaver._read_state()["last_slot"], "2026-01-14/dawn+1")
        check("so nothing has been bought and nothing has been lost",
              screensaver._read_state()["places"],
              [place_in(screensaver, prompt) for prompt in asked[:2]])

        tried = len(asked)
        check("a minute later it does not throw good money after bad",
              await screensaver.generate_if_due(at(17.5) + 60, 60.0), False)
        check("nothing was asked", len(asked), tried)
        check("a quarter of an hour later it tries again",
              await screensaver.generate_if_due(
                  at(17.5) + screensaver.RETRY_S, screensaver.RETRY_S), False,
              "`dusk-1` is still owed a picture")
        check("and that was a real attempt", len(asked), tried + 1)

        screensaver._generate_sync = drew
        check("the attempt that works clears the backoff",
              await screensaver.generate_if_due(
                  at(17.5) + screensaver.RETRY_S * 2,
                  screensaver.RETRY_S * 2), True)
        check("and clears it in the global and not only in the answer",
              screensaver._failed_at, None,
              "the name above is about `_failed_at`, so read `_failed_at`")
        check("and is what moves `last_slot` at last",
              screensaver._read_state()["last_slot"], "2026-01-14/dusk")
        check("with no half-written file left beside it",
              sorted(path.name for path in scratch.iterdir()),
              [screensaver.IMAGE_NAME, screensaver.STATE_NAME],
              "the bytes are renamed onto the path the route reads")

        # And the answer that is not an error and not a picture either.
        screensaver._failed_at = None
        screensaver._generate_sync = lambda prompt: None
        check("an answer with no picture in it is a failure",
              await screensaver.generate_if_due(at(19)), False)
        check("no empty file is left where the picture goes",
              picture.read_bytes(), NEW,
              "a 0-byte file passes `is_file()` and draws as broken")
        check("`last_slot` did not move for that either",
              screensaver._read_state()["last_slot"], "2026-01-14/dusk")
        check("and the route goes on serving the last real one",
              client.get("/api/screensaver.jpg").content, NEW)
        check("under the type those bytes are, not the one the name is",
              client.get("/api/screensaver.jpg").headers["content-type"],
              "image/png",
              "the whole path: the model drew PNG and the route says so")

        # How the bytes arrive matters as much as which bytes. The route
        # reads this path from the request thread while the generator writes
        # it from a worker, so a plain write is a `FileResponse` that can
        # open the file halfway through and serve half a picture — under a
        # `?v=` the face has already decided is new.
        renamed = []
        replace = screensaver.os.replace

        def spying(src, dst):
            renamed.append((Path(src).name, Path(dst).name,
                            Path(dst).read_bytes()))
            replace(src, dst)

        screensaver._failed_at = None
        screensaver._generate_sync = lambda prompt: OLD
        screensaver.os.replace = spying
        try:
            await screensaver.generate_if_due(at(19))
        finally:
            screensaver.os.replace = replace
        check("the bytes are renamed onto the served path, not written in",
              [(src, dst) for src, dst, _ in renamed],
              [(screensaver.IMAGE_NAME + ".part", screensaver.IMAGE_NAME)])
        check("so it holds the whole of the last picture until that moment",
              renamed[0][2], NEW)
        check("and the whole of this one after it", picture.read_bytes(), OLD)

        screensaver._generate_sync = drew
        screensaver._refreshed_at = 0.0
        screensaver._failed_at = None
        tried = len(asked)
        check("the afternoon reading buys nothing after dark",
              await screensaver.generate_if_due(
                  at(21), screensaver.STALE_S + 1), False)
        check("the model was not asked", len(asked), tried,
              "the 2K generation is the entire cost of this bug")
        check("_failed_at is still None", screensaver._failed_at, None)
        check("last_slot did not move for a picture nobody drew",
              screensaver._read_state()["last_slot"], "2026-01-14/dusk+1")

        check("and the one-interval control still draws",
              await screensaver.generate_if_due(
                  at(21), screensaver.REFRESH_S), True)
        check("and that was a real attempt", len(asked), tried + 1)
        check("with taken by day in the prompt",
              "taken by day" in asked[-1], True,
              "the thing the bound is guarding")
    finally:
        screensaver._generate_sync = was
        screensaver._failed_at = None
        screensaver._refreshed_at = None
        screensaver._seen = None


async def write_thread_cases(screensaver, scratch: Path) -> None:
    """Where the picture is written, and what the loop pays while it is.

    The photograph is around 8 MB and `_write_image` is one `write(2)` of
    all of it. The loop it would run on is the one holding the microphone's
    WebSocket: frames arrive every 8 ms in one direction and every ADK event
    is serialised to JSON, with the model's audio inside it, in the other. So
    anything that occupies the loop delays both. Off the loop that is a
    worker's problem and the frames keep moving.

    The only cases in this file that sleep for real, and they have to: the
    defect is wall clock time on one thread and there is no way to see it
    without spending some. What sleeps is a stub standing in for the write,
    not the write — the number below is `SLOW_WRITE_S` and not the disk's,
    so the case says the same thing on a fast machine and a slow one.
    """
    print("\n== where the 8 MB write runs ==")

    picture = scratch / screensaver.IMAGE_NAME
    picture.write_bytes(OLD)
    screensaver.state_path().unlink(missing_ok=True)
    screensaver._failed_at = None
    screensaver._refreshed_at = time.monotonic()
    screensaver._seen = dict(SEEN)

    on_loop = threading.get_ident()
    ran_on = []
    real = screensaver._write_image

    def slowly(raw: bytes) -> None:
        ran_on.append(threading.get_ident())
        time.sleep(SLOW_WRITE_S)
        real(raw)

    # A tick at the audio frame's period, recording what each one actually
    # cost. A tick that waited out the write is a frame the model heard late.
    waits: list[float] = []

    async def framing(stop: asyncio.Event) -> None:
        while not stop.is_set():
            began = time.perf_counter()
            await asyncio.sleep(FRAME_S)
            waits.append(time.perf_counter() - began)

    was = screensaver._generate_sync
    screensaver._generate_sync = lambda prompt: NEW
    screensaver._write_image = slowly
    stop = asyncio.Event()
    frames = asyncio.create_task(framing(stop))
    try:
        await asyncio.sleep(FRAME_S * 5)      # let the tick find its rhythm
        landed = await screensaver.generate_if_due(at(5.5))
    finally:
        stop.set()
        await frames
        screensaver._write_image = real
        screensaver._generate_sync = was
        screensaver._failed_at = None
        screensaver._refreshed_at = None
        screensaver._seen = None

    worst = max(waits)
    check("the picture still lands", landed, True)
    check("and the bytes on disk are the new ones", picture.read_bytes(), NEW)
    check("the write happened exactly once", len(ran_on), 1)
    check("and not on the thread the event loop is on",
          ran_on[0] != on_loop, True,
          "8 MB of `write(2)`, and that loop is carrying the microphone")
    check("so no audio frame waited out the write",
          worst < SLOW_WRITE_S / 2, True,
          f"worst tick {worst * 1000:.1f} ms against a "
          f"{SLOW_WRITE_S * 1000:.0f} ms write")
    check("and the sidecar still moves only after the bytes are down",
          screensaver._read_state()["last_slot"], "2026-01-14/dawn")

    # The one thing threading a call can quietly change is where its
    # exceptions come out. `asyncio.to_thread` re-raises in the awaiting
    # coroutine, so the `except OSError` around the call still catches and a
    # full disk is still a warning and a `False` — not a traceback out of
    # `refresh_loop`, and not a `last_slot` that moved for a picture that
    # never landed.
    screensaver._failed_at = None
    screensaver._refreshed_at = time.monotonic()
    screensaver._seen = dict(SEEN)
    screensaver._generate_sync = lambda prompt: OLD
    screensaver._write_image = _no_space
    try:
        check("a write that fails is caught rather than raised",
              await screensaver.generate_if_due(at(7)), False)
    finally:
        screensaver._write_image = real
        screensaver._generate_sync = was
        screensaver._failed_at = None
        screensaver._refreshed_at = None
        screensaver._seen = None
    check("the picture the panel is wearing survives it",
          picture.read_bytes(), NEW)
    check("and `last_slot` did not move for it",
          screensaver._read_state()["last_slot"], "2026-01-14/dawn")


def _no_space(raw: bytes) -> None:
    """A disk with nothing left on it, which is `ENOSPC` and an `OSError`."""
    raise OSError(28, "No space left on device")


# ------------------------------------------------------------------ driver

def geocode_log_cases() -> None:
    """What `_geocode` writes when it cannot answer, and what it does not.

    Both lines are error paths, so neither fires on an ordinary day — but the
    place they print is `forecast`'s `where`, which on a panel nobody has
    pinned is `here()` falling back to `HOME_LOCATION`: the household's real
    address out of `.env`, into a journal kept for a month that the *Forget*
    button does not reach. #226.
    """
    print("\n== the address a geocode failure used to print ==")

    class Journaled(logging.Handler):
        """Formatted, not the records: `%s` is what reaches the journal."""

        def __init__(self) -> None:
            super().__init__()
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(record.getMessage())

    def journalled(run) -> str:
        held = Journaled()
        source = logging.getLogger("ai_terminal.live.tools.weather")
        was, quiet = source.level, source.propagate
        source.setLevel(logging.WARNING)
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

    class Answered:
        """The grounding client, answering with `found` and reaching nothing."""

        def __init__(self, found):
            self.models = self
            self._found = found

        def generate_content(self, **kwargs):
            return type("Response", (), {"parsed": self._found})()

    home = "1-2-3 Nowhere Street, Kanazawa"
    was_client, was_model = weather.grounding_client, weather.grounding_model
    weather.grounding_model = lambda: "a model nothing here calls"
    try:
        def raises():
            raise RuntimeError("no credentials on this machine")

        weather.grounding_client = raises
        fell_over = journalled(lambda: _GEOCODE(home, "en"))
        weather.grounding_client = lambda: Answered({"latitude": 999.0,
                                                 "longitude": 0.0})
        off_planet = journalled(lambda: _GEOCODE(home, "en"))
    finally:
        weather.grounding_client, weather.grounding_model = was_client, was_model

    sized = f"<{len(home)} chars>"
    check("the place a geocode could not resolve does not reach the log",
          home in fell_over, False, fell_over)
    check("only how long it was", sized in fell_over, True, fell_over)
    check("nor does the place that resolved to somewhere off the planet",
          home in off_planet, False, off_planet)
    # Green before this and after it, and here because a line printing
    # nothing at all would pass the case above. The coordinates are the
    # diagnosis on this one and are nobody's words.
    check("only its length, beside the coordinates it refused",
          [sized in off_planet, "999.0" in off_planet], [True, True],
          off_planet)


async def main_async() -> None:
    main = existence_cases()
    from ai_terminal.live import screensaver

    # Before anything can go and ask Open-Meteo what the weather is, before
    # anything can ask a model where Hakone is, and before anything can spend
    # a 2K generation. Nothing below reaches a network, and these are the
    # three seams that make that true. The generator is stubbed to answer
    # with no picture, which is the harmless answer: the cases that want one
    # put their own stub over this for as long as they need it.
    weather._fetch = lambda lat, lng, past_days=0: forecast(past_days)
    weather._geocode = lambda place, lang: HAKONE
    weather._reverse_geocode = lambda lat, lng, lang: "Hakone"
    generate_sync = screensaver._generate_sync
    screensaver._generate_sync = lambda prompt: None
    screensaver._geocoded = None

    directory_cases(screensaver)
    deploy_cases(screensaver)
    geocode_log_cases()

    with tempfile.TemporaryDirectory() as tmp:
        # One directory for the rest, replaced rather than pointed at by
        # environment, the way `cache_check` does it: `screensaver_dir` is
        # cached and the cases below are not about how it is resolved.
        scratch = Path(tmp).resolve() / "pictures"
        scratch.mkdir()
        screensaver.screensaver_dir = lambda: scratch

        # `TestClient(app)` without the `with` runs no lifespan, which is
        # what the route cases want: the refresh is driven by hand below so
        # that each case knows what is on the face when it looks.
        client = TestClient(main.app, base_url=OUR_URL)
        with env(HOME_COORDS="35.232,139.107",
                 HOME_LOCATION="Hakone, Japan"):
            empty_cases(client, screensaver, scratch)
            await payload_cases(client, screensaver)
            await language_cases(client, screensaver)
            picture_cases(client, screensaver, scratch)
            path_cases(client, main, scratch)
            await task_cases(screensaver)
            await picture_loop_cases(screensaver)
            await interval_cases(screensaver)
            await lifespan_cases(main, screensaver)
            season_cases(screensaver)
            window_cases(screensaver)
            await fetch_cases(screensaver)
            prompt_cases(screensaver)
            place_cases(screensaver)
            blob_cases(screensaver)
            request_cases(screensaver, generate_sync)
            slot_cases(screensaver)
            citation_cases(screensaver)
            due_cases(screensaver)
            await generation_cases(client, screensaver, scratch)
            await write_thread_cases(screensaver, scratch)
        await coordinate_cases(screensaver, main)
    sun_cases()
    day_cases()
    polar_cases(screensaver)

    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    asyncio.run(main_async())
