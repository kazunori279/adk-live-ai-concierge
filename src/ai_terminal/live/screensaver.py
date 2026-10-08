"""What the panel wears when nobody is talking to it.

`static/js/app.js` paints a clock the moment the document loads and asks the
server what to put on it: a line of weather, and a photograph behind it. This
is the server's half — the weather, kept current by a task in the background,
and the directory the photograph is read out of.

Kept in the background rather than fetched per request, so the endpoint answers
instantly with the freshest thing anybody has: the client asks every ten
minutes (from `tickClock`, `sleep` and `wake`), so the cached weather and
photograph are served over loopback while Open-Meteo and the image model are
polled on their own background schedules. A kitchen nobody walks into between
eleven and seven stays current, and the endpoint never blocks a caller to go
and fetch.

And the photograph is written here too, seven times a local day — around
sunrise, around sunset and once at 21:00 — by asking an image
model for a real place under the weather the line above is reporting. An empty
directory stays a designed state rather than a failure — the payload comes
back with no `image` in it, which is the case `js_check` records as "an empty
one leaves the line empty" — because that is what a terminal looks like for
the first ten minutes after boot and for as long as the WAN is down.
"""

import asyncio
import functools
import json
import logging
import os
import random
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from google.genai import types

from . import screen
from .tools import weather
from .tools._client import global_client

logger = logging.getLogger(__name__)

# The photograph's only name. A constant rather than anything off the request:
# the route that serves it takes no arguments at all, so there is no component
# of the path a caller can steer and nothing for a `..` to travel down.
IMAGE_NAME = "background.jpg"

# What the bytes under that name begin with, and what to call them. The name
# is a name: nothing asks the model for a format and it draws PNG, so `.jpg`
# is a label the route has to stop believing. Each entry says where the magic
# sits as well as what it is, because WEBP's is eight bytes in behind a RIFF
# header and a `startswith` table gets that one wrong.
IMAGE_MAGIC = (
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (8, b"WEBP", "image/webp"),
)

# What a file none of those describes is served as. Not `image/jpeg`, which
# is the bug this replaces: a name no browser will draw is a better answer
# than a name that is wrong, and every one of those files is a failure
# somewhere upstream rather than a format worth guessing at.
UNKNOWN_MEDIA_TYPE = "application/octet-stream"

# Which slot the picture beside it was drawn for, and what the last few were
# of. A sidecar rather than the picture's own mtime: the Pi deploy is a file
# copy, `touch` is one of the things a person debugging a blank panel will
# try, and neither should buy a generation. Two values, both a cache — a file
# that will not parse costs one extra picture and nothing else.
STATE_NAME = "background.json"
HOME_NAME = "home.json"

# How often the weather line is refetched. Thirty minutes cuts the daily quota
# from 144 requests to 48 (a third of the calls ahead of the paid weather API)
# while keeping the displayed forecast fresh for glanceability.
# The background photograph loop ticks every REFRESH_S (ten minutes).
REFRESH_S = 600.0
WEATHER_S = 1800.0
STALE_S = 2 * REFRESH_S

# When a new picture is asked for. Six of the seven hang off the sun, because
# the whole point of drawing the weather is that the light in the picture is
# the light outside the window — and the hour that light changes moves by two
# hours across a Tokyo year and by rather more further north. An interval
# cannot follow that: four pictures every six hours are four pictures at
# whatever times the last success happened to land on, re-phased by every
# reboot, and they miss both ends of the day the request is mostly about.
#
# `(name, anchor, minutes)`. The order is the tie-break and nothing else, so
# the later a slot is written down here the more it wins.
SUN_SLOTS = (
    ("dawn-1", "sunrise", -60),
    ("dawn", "sunrise", 0),
    ("dawn+1", "sunrise", 60),
    ("dusk-1", "sunset", -60),
    ("dusk", "sunset", 0),
    ("dusk+1", "sunset", 60),
)

# And the seventh, which is a wall clock. Nothing in the sky marks the middle
# of an evening, and it is the one slot that still has a time on a day the sun
# never comes up.
NIGHT_SLOT = ("night", 21 * 60)

MINUTES_A_DAY = 24 * 60

# How long after a generation that failed. Short, because the failure this
# exists for is a Wi-Fi link that blinked once at 03:00: waiting out the rest
# of the slot for that costs the household a stale photograph all morning,
# where a quarter of an hour costs a second attempt nobody sees.
RETRY_S = 15 * 60.0

# How many of the last places are off the table. Eight is a bit over a day at
# seven slots, which is the span over which somebody walking past the same
# counter twice would notice a repeat.
PLACE_WINDOW = 8

# Where the photograph is of. Written down here rather than left to the model,
# for three reasons: a list in the repo makes the run reproducible, so a check
# script can exist at all; the model is not free to answer Kyoto eleven times
# running; and the string goes into the prompt as text we already have instead
# of something parsed back out of an answer.
#
# All outdoors, all somewhere a wide landscape frame makes sense, and spread
# across both hemispheres — the weather in the prompt is the household's, not
# the place's, so a list of only northern places would draw a Japanese January
# as a northern winter every time and never as one seen from Patagonia.
PLACES = (
    "the shore of Lake Bled, Slovenia",
    "the coast road above Positano, Italy",
    "the Cliffs of Moher, Ireland",
    "a lane between the drystone walls of Bibury, England",
    "the fishing village of Reine in the Lofoten Islands, Norway",
    "the lakes and boardwalks of Plitvice, Croatia",
    "the wetlands of the Camargue, France",
    "the wooded hills above Sintra, Portugal",
    "the valley floor at Lauterbrunnen, Switzerland",
    "the cliffs above Gasadalur, the Faroe Islands",
    "the rim of the Ngorongoro Crater, Tanzania",
    "the dunes at Sossusvlei, Namibia",
    "Chapman's Peak on the Cape Peninsula, South Africa",
    "the Atlas foothills above Imlil, Morocco",
    "the granite towers of Torres del Paine, Chile",
    "the salt flats at Uyuni, Bolivia",
    "the terraces above Pisac in the Sacred Valley, Peru",
    "the shoreline at Moraine Lake in Banff, Canada",
    "the beach at Cannon Beach on the Oregon coast",
    "the saguaro desert outside Tucson, Arizona",
    "the road into Milford Sound, New Zealand",
    "the Great Ocean Road near Apollo Bay, Australia",
    "the limestone islands of Ha Long Bay, Vietnam",
    "the thatched farmhouses of Shirakawa-go, Japan",
    "the tea hills above Nuwara Eliya, Sri Lanka",
    "the sandstone of Wadi Rum, Jordan",
)

# What is asked for, once the blanks are filled. Two of the three paragraphs
# are about the frame rather than the subject, which is not decoration: #84's
# clock sits over a scrim that is darkest across the top and the bottom bands,
# so a photograph whose subject lives there is a photograph nobody sees.
#
# It hands over observations and asks for a conclusion, and it used to do the
# opposite. Until #185 it computed one word from the calendar — September is
# `autumn`, whatever the thermometer says — and then told the model that
# nothing in the frame might contradict that word. Early September in Tokyo is
# 30 °C and full green, so the panel drew fall foliage over a summer day and
# was doing exactly what it had been asked. There is nothing left here to
# contradict: the date, the week behind it and the hour are all in front of
# the model, and which season that adds up to is the model's to work out.
#
# The date stays because temperature alone cannot tell spring from autumn, and
# in Japan that is the most visually distinct pair in the year — Tokyo runs
# about 15 °C in April and 18 °C in October, indistinguishable in a week of
# numbers, and one is 新緑 and the other 紅葉. The date is one more
# observation now rather than a key into a four-word table, which is strictly
# more than the word `autumn` ever carried.
#
# "anyone in the distance" is also why `person_generation` is left unset in
# `_generate_sync`: the last sentence asks for what people are wearing, and
# asking the API for no people at all would contradict it. That reason
# survived the season going away — it was never the season word that needed
# the people, it was the clothing.
PROMPT = (
    "A photorealistic photograph of {place}, outdoors, taken {when} on "
    "{date}. {recent}Right now it is {description}, at about "
    "{temperature} degrees Celsius, and the light matches that weather and "
    "that hour. Draw the vegetation, the ground and what anyone in the "
    "distance is wearing as they would actually be in this place under "
    "those conditions.\n"
    "Wide landscape composition, horizon in the middle third, open sky at "
    "the top of the frame and open ground or water at the bottom.\n"
    "No people in the foreground, no text, no captions, no watermarks, no "
    "logos, no borders."
)

# The week behind today, dropped into the blank above. A separate string
# because a reading can arrive without one — a provider that ignored
# `past_days`, a first refresh that came back with no daily arrays — and the
# failure mode of formatting it anyway is a sentence reading "run None to
# None degrees", which is worse than no sentence at all.
RECENT = (
    "Over the {days} days before today the temperature here has run {low} "
    "to {high} degrees Celsius, averaging {mean}. "
)

# Month names written down rather than taken from `strftime("%B")`, which
# answers in whatever locale the process came up in — the Pi's is not this
# machine's, and a prompt that changes with `LC_TIME` is a prompt no check
# script can pin.
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# How many days of weather the picture is drawn from, when nothing says
# otherwise. Seven damps a cold snap without lagging the actual turn of a
# season the way a month would.
SEASON_DAYS = 7

# The language the *fetch* is made in, which since #131 is no longer the
# language anything is read in. One process serves whatever pages ask and the
# panel's language lives in that page's localStorage (#96), so the word for
# the sky cannot be settled here — but it does not have to be settled at fetch
# time at all. What Open-Meteo sends is a WMO code, `_code` below keeps it,
# and `payload` turns it into a word per request. This tag is what is left:
# the hourly and daily strips `_weather_sync` builds on the way past, which
# this module throws away, and the geocoder's name for `HOME_LOCATION` when
# that is the rung `where` lands on.
FACE_LANG = "en"

# The last answer Open-Meteo gave, or None until it has given one. Kept so the
# route never waits on the network and so one failed refresh at 4 a.m. shows
# the previous temperature rather than a blank line.
#
# Everything in it is the same in every language. The one field that is not —
# `description` — is not in here at all; it is rendered by `payload` from
# `_code` at request time.
_shown: dict | None = None
_shown_at: float | None = None

# The sky that reading found, as the code Open-Meteo sent rather than as a
# word for it. Kept beside `_shown` and not in it, because it is the one thing
# the face is served that has to be built per request rather than per fetch.
#
# The code and not the `condition` slug already in `_shown`: nine slugs cover
# twenty-eight codes, so `rain` cannot be turned back into `Heavy rain`, and a
# translation table keyed on the slug would lose that distinction in both
# languages at once rather than only in the one it was written for.
_code: int | None = None

# Where the panel in front of this server says it is: `(lat, lng, label)` off
# the last handshake that carried a position, or None until one has. See
# `where`, which prefers it over the two environment variables.
#
# One variable for the whole process, because the clock behind it is one clock
# with one refresh loop and no socket — and so, unlike the session state every
# other reader on this path uses, it cannot belong to a connection. Which page
# is allowed to write it is `set_home`'s rule and is not obvious; the argument
# is there.
_told: tuple[float, float, str] | None = None

# `HOME_LOCATION` geocoded, kept because it is a model call about an address
# that does not move. Only a success is stored, so a Pi that came up before
# its credentials did tries again on the next refresh.
_geocoded: tuple[float, float, str] | None = None

# The rest of what the last refresh found, which is what the picture is drawn
# from and when. Kept apart from `_shown` rather than added to it, because
# `payload()` serves that dict as it stands and a key the face never reads is
# a promise made to nobody. `recent` is the week behind today, which is what
# the picture is drawn from since #185; the two sun times are what the slots
# hang off. No latitude: it was here for a hemisphere flip on a season table
# that no longer exists, and a Sydney January now arrives as "it has run
# 26-33 °C all week" and needs nobody to say which hemisphere that is.
_seen: dict | None = None

# The last week-of-weather asked for, as `(the local date it was asked on,
# what came back)`, and it is a quota decision rather than a cache for
# speed. `past_days` is free in round trips — same request, same response —
# but it is not free in *day-units*, which is what a weather API counts: five
# forecast days become twelve, and at 48 refreshes a day that is a factor of
# 2.4 on the whole screensaver. `screensaver.py:71-73` already promises the
# thirty-minute tick stays "well inside what a keyless caller is asked to use",
# so multiplying the cost by 2.4 in silence is not available.
#
# What makes it cheap is that the answer only changes once a day: yesterday's
# high is yesterday's high all afternoon. Asking on the tick where the date
# rolls and reusing it until the next one takes the extra window from 48
# requests a day to 1, and every other tick asks for exactly what the card
# asks for.
#
# The date *is* the freshness ceiling, which is why there is not a second
# one. #177 is right that a stale week hides better than a stale afternoon —
# but an entry keyed on today's date cannot be older than today, and it is
# replaced on the first refresh after midnight. The reading the picture is
# actually drawn from, `_seen`, is refetched every tick either way; this
# holds only the part that is about last week by definition.
_recent_on: tuple[str, dict | None] | None = None

# When the last attempt failed, or None if the last one did not. A
# `time.monotonic()` reading and not a wall time, because it is only ever
# subtracted from another reading of the same clock. In memory and not in the
# sidecar on purpose: a reboot is the one event that plausibly fixed whatever
# was broken, so a terminal that comes back up should try straight away rather
# than serve out a backoff it can no longer explain — and the clock now says
# the same thing, since monotonic readings share an epoch only with the
# process that took them. Nothing that crosses a restart may be one of these;
# what the sidecar holds is a slot name, which means the same after a reboot.
_failed_at: float | None = None
_refreshed_at: float | None = None

# When the last weather fetch attempt was made, whether it succeeded or failed.
# A `time.monotonic()` reading used to gate weather polling to WEATHER_S (30 min)
# while the background loop ticks at REFRESH_S (10 min).
_weather_attempted_at: float | None = None

# The generator's own source of randomness, so that a check script can hand in
# a seeded one and `random.seed()` somewhere else in the process cannot move
# which place comes next.
_rng = random.Random()


@functools.cache
def screensaver_dir() -> Path:
    """Where the clock's background lives, created if it is not there yet.

    Relative to the process's working directory, which is the repo root under
    `uv run live-server` and the systemd unit's ``WorkingDirectory`` on the Pi
    — the same rule as the Workspace cache (`gws_cache.cache_dir`) and the
    session store, and deliberately not the package's own `static/`. The Pi
    deploy is a file copy over the package, so a photograph written into
    `static/` is clobbered by the next deploy and committed by accident before
    that.

    Which does not by itself put it out of the deploy's way, and that is the
    half worth writing down. That copy's destination is this same
    `WorkingDirectory`, so the name has to appear as one of the command's
    `--exclude`s too — README.md, *Running it on the Pi*, pinned by
    `screensaver_check.deploy_cases`. Without it the picture and the sidecar
    are overwritten by whatever the deploying machine has, or removed outright
    by `--delete` if it has none, and either way the next tick buys a 2K
    generation to replace one that was already right.

    Cached because .env is loaded before the first call and the path cannot
    change after that.
    """
    path = Path(os.getenv("SCREENSAVER_DIR", "screensaver")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def season_days() -> int:
    """How many days of weather the picture is drawn from.

    An environment variable rather than a module constant, and the split in
    this module is meaningful: constants for what is the same everywhere
    (`REFRESH_S`, `RETRY_S`, `PLACE_WINDOW`), `os.getenv` for what differs per
    machine (`SCREENSAVER_DIR`, `SCREENSAVER_MODEL`, `HOME_COORDS`). This one
    looks like a constant and behaves like the second kind, because it is a
    knob on how the picture *looks*: tuning it means trying seven, then ten,
    then five, and standing in front of the panel to see which is preferred.
    On the constant path each of those is an edit, a commit and a deploy.

    Never raises and never returns a number the provider will refuse. An
    unparseable, empty, zero, negative or oversized value takes the default
    and logs it — a screensaver is not worth a failed boot over a typo, and
    #10's unattended-boot promise depends on nothing new being mandatory.

    The **empty** case is the one that actually arrives — a line copied out of
    `.env.example` with the value cut off — and it has to be guarded here
    rather than upstream, because `int("")` raises and this runs in a
    background task. `pi_deploy.py check` no longer covers for it either way:
    its probe requires a value that is still there once quotes and spaces come
    off (`tools/pi_deploy.py:655-672`, since `21277b2`), so a bare
    `SCREENSAVER_SEASON_DAYS=` reports as *missing* rather than as set. That
    is the right answer and it is a `check` run away; this is what the process
    does in the meantime.
    """
    raw = (os.getenv("SCREENSAVER_SEASON_DAYS") or "").strip()
    if not raw:
        return SEASON_DAYS
    try:
        days = int(raw)
    except ValueError:
        logger.warning("screensaver: SCREENSAVER_SEASON_DAYS=%r is not a "
                       "number; using %d", raw, SEASON_DAYS)
        return SEASON_DAYS
    if not 1 <= days <= weather.PAST_DAYS_MAX:
        logger.warning("screensaver: SCREENSAVER_SEASON_DAYS=%d is outside "
                       "1..%d; using %d", days, weather.PAST_DAYS_MAX,
                       SEASON_DAYS)
        return SEASON_DAYS
    return days


def image_path() -> Path:
    """The photograph, whether or not anything has written it yet."""
    return screensaver_dir() / IMAGE_NAME


def image_media_type(picture: Path) -> str:
    """What to call the photograph, read off the photograph.

    Off the bytes rather than off `IMAGE_NAME`, because the name says JPEG
    and the model draws PNG — the route answered `image/jpeg` over PNG magic
    for as long as #86 has been writing files, and the only thing hiding it
    was Chromium sniffing an `<img>` and rendering the picture anyway.

    Off the bytes rather than off the `Blob.mime_type` the generation saw,
    too, and that is the less obvious half. The picture outlives every
    process that could have remembered what it was: the server restarts, the
    Pi deploy copies the tree over the top, the sidecar beside it is a cache
    anything may delete, and the file on this machine right now is a PNG
    under the old name written by code that recorded nothing. Remembered
    metadata is right about the last generation; the bytes are right about
    the file, which is the thing being served.

    An unreadable or absent file answers rather than raising, because the
    caller is a route that has already decided to serve it and the honest
    404 for a file that is not there is `is_file()`'s, one line up.
    """
    try:
        with picture.open("rb") as handle:
            head = handle.read(12)
    except OSError:
        return UNKNOWN_MEDIA_TYPE
    for start, magic, media_type in IMAGE_MAGIC:
        if head[start:start + len(magic)] == magic:
            return media_type
    return UNKNOWN_MEDIA_TYPE


def state_path() -> Path:
    """The sidecar the photograph's slot is kept in."""
    return screensaver_dir() / STATE_NAME


def home_path() -> Path:
    """The sidecar the settings panel's verified position is kept in."""
    return screensaver_dir() / HOME_NAME


def _read_home() -> tuple[float, float, str] | None:
    """Read the saved verified position off disk, or None if absent/unreadable."""
    try:
        data = json.loads(home_path().read_text(encoding="utf-8"))
        lat = float(data["lat"])
        lng = float(data["lng"])
        place = str(data.get("place", "")).strip()
        if place and -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
            return lat, lng, place
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def _write_home(lat: float, lng: float, place: str) -> None:
    """Persist the settings panel's verified position to disk."""
    if not place:
        return
    try:
        home_path().write_text(
            json.dumps({"lat": lat, "lng": lng, "place": place}),
            encoding="utf-8",
        )
    except OSError:
        pass


def restore_home() -> tuple[float, float, str] | None:
    """Restore the persisted home position into memory if not already set."""
    global _told
    if _told is None:
        _told = _read_home()
    return _told


def image_model() -> str:
    """Which model draws the background.

    An environment variable rather than a literal, and this is the one value
    in the module that could not be settled without spending a call. google-
    genai 2.22.0 never validates a model name — `generate_content` passes the
    string through to the endpoint — and the only place the SDK writes an
    image model id down at all is `_gaos/types/interactions/model.py:53,55`,
    a different surface, where the annotation is
    `Union[Literal[...], UnrecognizedStr]` and therefore accepts anything.
    The two ids there are `gemini-3-pro-image` and `nano-banana-pro-preview`,
    the SDK's own comment on the second one reading "Gemini 3 Pro Image
    Preview": the same model, one of them an alias. So the wrong guess is a
    404 at 06:00 on a Pi, and the fix for it should be a line in .env rather
    than a deploy.
    """
    return os.getenv("SCREENSAVER_MODEL", "gemini-3-pro-image")


def _parse_coords(raw: str) -> tuple[float, float] | None:
    """``"35.68,139.76"`` as a position, or None for anything that is not one.

    Same bounds as `_coords` in `main.py`, and for the same reason: a
    transposed pair is the ordinary way to mistype this, and 139 degrees of
    latitude is the only place it shows.
    """
    try:
        lat, lng = (float(part) for part in raw.split(","))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def set_home(coords: dict | None, place: str, kiosk: bool = False) -> bool:
    """Take the position the panel is set to. True if that moved it.

    Called from the handshake (`main.py`), which is where the settings sheet's
    map pin already travels: `{lat, lng}` and the label the pin was
    reverse-geocoded into. It is kept because the refresh loop runs on a timer
    with no page attached, so a position that lived only as long as the socket
    would be gone by the time the next tick asked for it.

    Only a position replaces a position — the same rule `refresh_once` applies
    to a reading. A page that has no location set sends none, and that means
    "use whatever the server is configured with" rather than "forget where you
    are": the settings sheet says so in as many words (`ui_where_unset`), and
    a second browser open on a laptop must not be able to move the panel's
    clock by having no opinion.

    `kiosk` is that same laptop's other way of moving it, which is by having
    one. `_told` is per process and this handler is per socket, so without a
    rule the pin is whichever page connected last: pin the panel to one town,
    open a tab somewhere else pinned to another, and the panel's next tick
    fetches the tab's coordinates and prints the tab's town under the clock.
    So while a panel is connected the panel owns the position, and a page that
    is not it is refused.

    Asked of `screen.any_kiosk()` and not remembered in a flag of our own,
    which is the same shape :func:`screen.wake_sink` uses and for the same
    reason: ownership is a fact about who is here, so it has to lapse when the
    panel goes. A flag would have to be told, and the thing that would have to
    tell it is the socket that has just gone — a panel unplugged for good
    would freeze the pin for the life of the process, with no page able to
    move it.

    Two consequences worth knowing before changing this. A tab can still set
    the position whenever no panel is connected, which is what keeps #131
    working on a development Mac and keeps "set the kitchen's location from my
    laptop" working with the panel off; the pin it leaves survives the panel
    coming up, because a panel with no pin of its own sends none and none does
    not replace a position. And a refusal is silent — the settings sheet on
    that tab still says the location is set, because it is, for that page.
    """
    global _told, _weather_attempted_at, _geocoded, _shown

    if not coords:
        return False
    # Live, so a tab that pins inside a blip owns the clock until a panel
    # with a pin of its own reconnects and re-pins. A panel that has none
    # sends none, so for that one the tab's town stays until a restart —
    # the price of never freezing a pin nobody can be asked about.
    if not kiosk and screen.any_kiosk():
        return False
    clean_place = place.strip()
    told = (float(coords["lat"]), float(coords["lng"]), clean_place)
    if clean_place:
        _write_home(told[0], told[1], clean_place)
    if told == _told:
        return False
    _told = told
    _weather_attempted_at = None
    _geocoded = None
    if _shown is not None:
        _shown["place"] = clean_place
    return True


_LOCAL_TYPES = (
    "sublocality_level_1",
    "sublocality_level_2",
    "neighborhood",
    "locality",
    "postal_town",
    "administrative_area_level_2",
    "sublocality",
)
_REGION_TYPES = ("administrative_area_level_1", "country")
_IS_BLOCK = re.compile(
    r"^(?:[0-9０-９]+(?:[-−ー―][0-9０-９]+)*|[0-9０-９]+(?:\s*丁目|\s*番地?|\s*号)?|[0-9０-９]+[-\s]?[Cc]h[oō]me)$",
    re.IGNORECASE,
)


def _component(components: list[dict], types: tuple[str, ...]) -> str:
    for t in types:
        for part in components:
            part_types = part.get("types", [])
            if t not in part_types:
                continue
            if t == "sublocality" and any(
                re.match(r"^sublocality_level_[3-9]", pt) for pt in part_types
            ):
                continue
            long_name = str(part.get("long_name", "")).strip()
            if long_name and not _IS_BLOCK.match(long_name) and not long_name.isdigit():
                return long_name
    return ""


def _format_maps_place(components: list[dict]) -> str:
    parts = []
    for type_group in (_LOCAL_TYPES, _REGION_TYPES):
        name = _component(components, type_group)
        if type_group is _LOCAL_TYPES and (_IS_BLOCK.match(name) or name.isdigit()):
            continue
        if name and name not in parts:
            parts.append(name)
    if parts and parts[0].isdigit():
        parts.pop(0)
    return ", ".join(parts)


def _reverse_geocode_maps(lat: float, lng: float, lang: str = FACE_LANG) -> str | None:
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={
                "latlng": f"{lat},{lng}",
                "key": api_key,
                "language": lang,
            },
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "OK" and (results := data.get("results")):
                components = results[0].get("address_components") or []
                place = _format_maps_place(components)
                if place:
                    return place
                formatted = str(results[0].get("formatted_address", "")).strip()
                if formatted:
                    return formatted
    except Exception:
        logger.warning(
            "screensaver: Google Maps reverse geocode failed for %s,%s",
            lat, lng, exc_info=True,
        )
    return None


_KNOWN_DISTRICTS: tuple[tuple[float, float, float, float, str], ...] = (
    # Ueno (Taito-ku, Tokyo)
    (35.710, 35.720, 139.770, 139.785, "Ueno, Tokyo"),
    # Hakone
    (35.200, 35.260, 139.050, 139.150, "Hakone"),
    # Greater Tokyo
    (35.500, 35.800, 139.500, 140.000, "Tokyo"),
    # Osaka
    (34.600, 34.800, 135.400, 135.600, "Osaka"),
    # Kyoto
    (35.000, 35.100, 135.700, 135.800, "Kyoto"),
)


def _reverse_geocode(lat: float, lng: float, lang: str = FACE_LANG) -> str:
    """Resolve coordinates to a human-readable place name deterministically.

    Never invokes an LLM. Tries Google Maps Geocoding API if GOOGLE_MAPS_API_KEY
    is set, falling back to known districts or a human label.
    Never returns raw coordinates.
    """
    if place := _reverse_geocode_maps(lat, lng, lang):
        return place
    for min_lat, max_lat, min_lng, max_lng, name in _KNOWN_DISTRICTS:
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            return name
    return "Local Area"


def where() -> tuple[float, float, str] | None:
    """Where this terminal is, asked by something with no browser behind it.

    Three rungs. The pin somebody dropped in the settings sheet, if a page has
    told this process about one (`set_home`) — persisted to `home.json` so a
    restart recovers it before any browser connects — and while a panel is
    connected only that panel can move it.
    Failing that,
    ``HOME_COORDS="35.68,139.76"`` with ``HOME_LOCATION``; if
    ``HOME_LOCATION`` is unset, the coordinates resolve to a human-readable
    place name deterministically (via Google Maps Geocoding API if configured,
    or district fallback) without invoking an LLM.
    Failing that, ``HOME_LOCATION`` — the address `maps.here` already falls
    back to — put through the same grounding geocoder `weather` uses for a
    named place, because a terminal that has been told where it is in one
    variable should not have to be told again in a second one. That rung is a
    model call, so its answer is kept for the life of the process.

    The pin used to be refused, and the objection is worth answering rather
    than deleting. It was that the handshake arrives on an unauthenticated
    query string, at a server that asks nobody for a credential, so trusting
    it would let any page that reaches this move where the terminal thinks it
    is — permanently, with nothing on the screen saying so, and with #86 about
    to spend money per picture of the place it names. Three claims, and none
    of them survives:

    - It buys nobody anything. That same handshake already writes
      `home_coords` and `home_location` into the session state
      (`main.py:1427-1430`), where `weather` reads the first for every "what's
      the weather" anybody asks and the agent's system instruction reads the
      second. A page that can reach this server already decides where the
      terminal thinks it is for everything a person can ask it out loud. The
      clock holding out did not protect the household; it only made the clock
      disagree with the answer beside it.
    - Not permanently. `_told` is in memory and backed by `home.json` on disk,
      so the next handshake or settings sheet update replaces it.
    - Nothing on the screen saying so is no longer true either. The label this
      keeps is the string the settings sheet prints under *Location*, and
      since #131 it is also the place at the end of the line under the clock.
      The position in use is on the screen in two places.

    And the money: a move buys no picture. `_slot_at` names a slot by a local
    date and one of seven names, `_due` compares that name, and neither reads
    a position — so the next photograph is at the next slot, of the new
    weather, and there is no extra one in between. The reading behind it is
    not refetched on the spot either: the loop picks the new position up on
    its own tick, within `REFRESH_S`, which is also what stops a page that
    reconnects in a loop from turning handshakes into Open-Meteo requests.
    Until then the face shows the old place with the old temperature — stale
    as a pair, which is the honest way to be wrong, rather than the new label
    over a reading taken somewhere else.

    Blocking, because the third rung is: call it off the event loop.
    """
    global _geocoded

    if _told is not None:
        return _told

    if _geocoded is not None:
        return _geocoded

    if pinned := _parse_coords(os.getenv("HOME_COORDS", "")):
        lat, lng = pinned
        place = os.getenv("HOME_LOCATION", "").strip()
        if not place:
            place = _reverse_geocode(lat, lng, FACE_LANG)
        return lat, lng, place

    place = os.getenv("HOME_LOCATION", "").strip()
    if not place:
        return None
    found = weather._geocode(place, FACE_LANG)
    if found is not None:
        _geocoded = found
    return found


def payload(lang: str = "") -> dict:
    """What `GET /api/screensaver` answers with, in the language it asked in.

    Exactly the fields `paintScreensaver` reads, and no others: a field
    nothing on the page looks at is a field #86 would have to keep true for
    nobody. Two readers since #88, the clock face and the top bar, and
    between them they read every key here.
    Every one of them is optional and the page draws whatever arrives, so `{}`
    — no WAN since boot, no picture generated yet — is a complete answer.

    `description` is the one field built here rather than at the fetch, and
    `lang` is the whole reason this takes an argument. One reading serves
    every language, because `_code` is a number: the refresh loop goes on
    running on its own timer, no page costs a second request, and there is no
    second copy of `weather._CONDITIONS` anywhere. An unrecognised tag, or
    none at all, is English — the rule `weather._base` already applies, so an
    older page that asks for nothing reads exactly what it read before.

    The `image` URL carries the file's mtime. It has to change when the
    picture changes, or the browser serves the old one out of cache; and it
    has to *not* change when the picture has not, because the face only
    assigns `src` when the string differs and re-assigning it blinks the
    gradient on the panel every time the clock comes up. A timestamp off the
    file is the one value with both properties.
    """
    body: dict = dict(_shown or {})
    if "weather" in body:
        # A new dict rather than a write into `_shown["weather"]`: the copy
        # above is shallow, so assigning through it would leave the last
        # caller's language on the stored reading for the next one.
        body["weather"] = {**body["weather"],
                           "description": weather._describe(_code, lang)[1],
                           "fetched": _shown_at}
    try:
        stamp = int(image_path().stat().st_mtime)
    except OSError:
        return body
    body["image"] = f"/api/screensaver.jpg?v={stamp}"
    return body


def _weather_due(mono: float, attempted_at: float | None,
                 interval: float = WEATHER_S) -> bool:
    """Whether enough time has elapsed to make another weather fetch attempt."""
    if attempted_at is None:
        return True
    return (mono - attempted_at) >= interval


async def refresh_once(mono: float | None = None, *, force: bool = False) -> bool:
    """Fetch the weather once and keep it. True if there is now something.

    Everything is caught. This runs in a background task inside the process
    that is streaming somebody's audio, and the worst thing Open-Meteo
    returning a 502 at 4 a.m. is allowed to cost is a stale temperature on a
    clock. What failed goes to the log once per attempt and the previous
    answer stays on the face until a later attempt replaces it.

    An answer that arrives with no temperature in it counts as a failure for
    the same reason, even though nothing raised. The face has no way to say
    which of the two it is looking at, so the rule has to be the same one:
    something is only allowed to replace a good reading if it is itself a
    reading.
    """
    global _shown, _shown_at, _seen, _code, _recent_on, _weather_attempted_at, _refreshed_at

    if mono is None:
        mono = time.monotonic()

    if not force and _shown is not None and not _weather_due(
            mono, _weather_attempted_at, WEATHER_S):
        return True

    _weather_attempted_at = mono

    found = await asyncio.to_thread(where)
    if found is None:
        logger.warning(
            "screensaver: nowhere to ask about; set HOME_COORDS "
            '("lat,lng") or HOME_LOCATION in .env')
        return False

    lat, lng, place = found
    # The week is asked for once a day and the other 47 ticks ask for what
    # the card asks for — see `_recent_on`. A tick that asked for it and got
    # nothing back still counts as having asked: the entry is written either
    # way, so a provider that ignores `past_days` is not re-asked 47 more
    # times before midnight.
    today = date.today().isoformat()
    held = _recent_on if _recent_on and _recent_on[0] == today else None
    try:
        raw = await asyncio.to_thread(
            weather._weather_sync, lat, lng, place, FACE_LANG,
            0 if held else season_days())
    except Exception:  # pylint: disable=broad-except
        logger.warning("screensaver: weather refresh failed", exc_info=True)
        return False

    current = raw.get("current") or {}
    # A 200 with nothing in it is a failed refresh wearing a success's
    # clothes, and the `except` above cannot see it. Without this the
    # docstring's promise holds for the provider that times out and breaks for
    # the provider that answers — `24°C · Overcast` degrades to
    # `Unknown · Hakone, Japan`, which is worse than the hour-old temperature
    # in exactly the way the paragraph above says it is. Only when there is
    # already something good to keep: on a cold start an empty reading is
    # still the most this knows, and a place with no temperature beside it is
    # a line the face renders correctly.
    if current.get("temperature") is None and _shown is not None:
        logger.warning(
            "screensaver: an answer with no temperature in it; keeping the "
            "last one")
        return False

    # Recorded here rather than beside `_seen` below, once the reading has
    # passed the temperature guard: a thrown-out reading must not spend the
    # day's week. A `recent` of None is a real answer to "what came back", and
    # storing it is what keeps the count at one.
    if held is None:
        _recent_on = (today, raw.get("recent"))
    recent = (held or _recent_on)[1]

    _shown = {
        "place": raw.get("location") or place,
        "weather": {
            "temperature": current.get("temperature"),
            "unit": (raw.get("units") or {}).get("temperature", "°C"),
            # The same sky as the word `payload` renders, as a slug rather
            # than a word. Added for #88's toolbar and it is still how that
            # widget answers the locale question: the bar has no room for a
            # word, so it draws the glyph `cards.js` already keys on these
            # nine slugs, and a glyph reads the same in both languages.
            # `is_day` beside it for the same reason the card takes one — a
            # clear night drawn with a sun in it is a bug somebody
            # photographs.
            "condition": current.get("condition", ""),
            "is_day": bool(current.get("is_day", True)),
        },
    }
    _shown_at = time.time()
    # The word for that sky is not stored, because there is no one word: see
    # `payload`. Set with `_shown` and never apart from it, so the face can
    # never be served a temperature from one reading with a sky from another.
    _code = current.get("weather_code")
    _seen = {
        "is_day": bool(current.get("is_day", True)),
        "temperature": current.get("temperature"),
        "description": current.get("description", ""),
        # What it has been like here lately, or None on a day whose reading
        # came back with no history in it. The picture is drawn from this
        # rather than from the name of the month (#185). It comes off
        # `_recent_on` and not off `raw`, because all but one tick a day
        # deliberately did not ask for it.
        "recent": recent,
        # Today's pair, carried through exactly as `_weather_sync` gave them
        # and read as a pair rather than as two times — see `_sun`. No second
        # request and no second source of truth for where the sun is: these
        # are the same two values the card under the clock is showing.
        "sunrise": current.get("sunrise", ""),
        "sunset": current.get("sunset", ""),
    }
    _refreshed_at = mono
    return True


def _pick_place(recent, rng: random.Random | None = None) -> str:
    """A place that is not one of the last few.

    Falls back to the whole list when the window has eaten it, which cannot
    happen at `PLACE_WINDOW` places out of `PLACES` but would the moment
    somebody shortens the list and not the window — and the failure mode of
    picking from an empty sequence is an `IndexError` inside a background
    task, which is a panel that quietly stops changing.
    """
    pool = [place for place in PLACES if place not in recent] or list(PLACES)
    return (rng or _rng).choice(pool)


def _prompt(place: str, seen: dict, day: date) -> str:
    """The whole of what the model is asked for.

    The condition word is lowercased on the way in. `weather._CONDITIONS`
    stores it sentence-cased for the card heading — "Overcast", "Heavy
    freezing rain" — and it lands mid-sentence here.

    `day` is the terminal's local date and goes in as a date rather than as a
    season: see `PROMPT`. A reading with no `recent` in it renders the same
    sentence with the week's clause simply absent, which is the honest shape —
    the model is told what is known and nothing that is not.
    """
    recent = seen.get("recent") or {}
    return PROMPT.format(
        place=place,
        when="by day" if seen.get("is_day", True) else "after dark",
        date=f"{day.day} {MONTHS[day.month - 1]}",
        recent=RECENT.format(**recent) if recent else "",
        description=str(seen.get("description", "")).lower(),
        temperature=round(seen.get("temperature") or 0),
    )


def _hhmm(value) -> int | None:
    """``"05:23"`` as minutes past local midnight, or None if it is not one.

    Anything that is not an hour and a minute with a colon between them is
    None rather than an exception, and that tolerance is the point rather
    than defensiveness. What the card is told on a day the sun does not rise
    is #113's to decide — an empty string, or a translated "Midnight sun" —
    and every one of those answers has to arrive here as *there is no
    sunrise today* rather than as a time or as a traceback in a background
    task.
    """
    parts = str(value or "").split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


def _sun(seen: dict | None) -> dict:
    """Today's sunrise and sunset in minutes past midnight, or `{}`.

    Empty on a day the sun does not cross the horizon, and the signal for
    that is **the two stamps being equal**, read as a pair rather than one at
    a time. Open-Meteo does not answer such a day with a null — asked on
    2026-09-09 it sends a midnight stamp at both ends — but that is no
    longer the shape that reaches here. `weather._sun` resolves the day
    first and sends `""` for both times (`weather.py:211-214`), so what
    arrives is two equal empty strings. The pair is equal either way, which
    is why this test survived that change unaltered; both shapes are pinned
    against `_slots` in `screensaver_check.slot_cases`. Taken at face value
    a midnight pair would hang six of the seven slots around midnight —
    23:00, 00:00 and 01:00, twice over — and buy four pictures inside two
    hours with three of them duplicates.

    A day with no sunrise gets `night` and nothing else. Not stand-in times
    of 06:00 and 18:00: the reason for anchoring to the sun at all is that
    the light in the picture is the light outside the window, and a "sunrise"
    picture at an hour the sun does not rise is the fixed photograph this
    module exists to remove, wearing a schedule.
    """
    seen = seen or {}
    sunrise, sunset = seen.get("sunrise"), seen.get("sunset")
    if sunrise == sunset:
        return {}
    first, last = _hhmm(sunrise), _hhmm(sunset)
    if first is None or last is None:
        return {}
    return {"sunrise": first, "sunset": last}


def _slots(seen: dict | None) -> list[tuple[int, int, str]]:
    """The times a picture is due on one local day, earliest first.

    `(minute, rank, name)`, where the rank is the order `SUN_SLOTS` writes
    them down in and only ever settles a tie. Two slots land on the same
    minute above about 55 degrees in summer, where sunset is close enough to
    21:00 that `dusk-1` and `night` are the same time: sorting makes the
    later one the answer and the earlier one is skipped for that day, which
    is one picture rather than two and needs no special case.

    A slot whose time falls outside the day is dropped rather than carried
    into the next one. Where sunset is 23:40, `dusk+1` is 00:40 tomorrow
    morning, and resolving it there would need *yesterday's* sun times where
    the payload carries only today's. One fewer picture on the two or three
    days a year that happens, at a latitude the household does not live at,
    is cheaper than a second day's forecast in the state file.

    Never empty: `night` has no anchor to lose, which is also what a terminal
    that has not reached Open-Meteo yet is left with.
    """
    sun = _sun(seen)
    found = [(NIGHT_SLOT[1], len(SUN_SLOTS), NIGHT_SLOT[0])]
    for rank, (name, anchor, offset) in enumerate(SUN_SLOTS):
        if anchor not in sun:
            continue
        minute = sun[anchor] + offset
        if 0 <= minute < MINUTES_A_DAY:
            found.append((minute, rank, name))
    return sorted(found)


def _slot_at(now: float, seen: dict | None) -> str:
    """Which slot the picture on the panel should be for: `"2026-09-09/dusk"`.

    The most recent slot at or before `now`, looking back over the day
    boundary — at 00:30 the answer is yesterday's last slot, because nothing
    has come due yet today. Yesterday's are read off today's sun times, which
    move by a couple of minutes a day and are hours away from the question
    being asked at that hour.

    A local date and a name rather than an elapsed timestamp, and that is
    what makes the record survive the two events a household appliance
    actually has. A Pi rebooted at 05:59 between `dawn-1` and `dawn` reads
    `dawn-1` back off disk and does not buy a second picture. A Pi that was
    off all afternoon comes up on the most recent past slot straight away
    instead of waiting for the next one. And a clock that stepped backwards —
    no RTC across a power cut, then NTP — leaves a `last_slot` that is simply
    not the current identity, so it re-fires once and settles instead of
    pinning the photograph until the two clocks agree.

    Missed slots are not queued: only the most recent one has an identity, so
    a terminal off from dawn to dusk comes back to one picture and not three.

    The times are local wall times on both sides and nothing converts. The
    request carries `timezone=auto`, so Open-Meteo has already answered in
    the coordinates' own zone, and `datetime.fromtimestamp` reads the
    terminal's — which is the same zone for a panel standing where
    `HOME_COORDS` points, and the day the clocks move needs no special case
    because both sides move with them.
    """
    local = datetime.fromtimestamp(now)
    minute = local.hour * 60 + local.minute
    slots = _slots(seen)
    past = [slot for slot in slots if slot[0] <= minute]
    if past:
        return f"{local.date().isoformat()}/{past[-1][2]}"
    return f"{(local.date() - timedelta(days=1)).isoformat()}/{slots[-1][2]}"


def _read_state() -> dict:
    """Which slot the last picture was for and what it was of, `{}` if never.

    Read off disk every time rather than kept in a global, and that is what
    makes a reboot cheap: a Pi restarted two minutes after a generation must
    not read an empty global as "there has never been one" and spend another
    picture proving it. Anything unreadable is the same as nothing.
    """
    try:
        found = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _write_state(last_slot: str, places: list[str]) -> None:
    """Record a success. Only ever called after the bytes are on disk."""
    state_path().write_text(
        json.dumps({
            "last_slot": last_slot,
            "places": places[-PLACE_WINDOW:],
        }),
        encoding="utf-8",
    )


def _write_image(raw: bytes) -> None:
    """Put the bytes where the route reads them, in one step.

    Written beside the photograph and renamed onto it. `os.replace` is atomic
    on POSIX, and the alternative is a `FileResponse` opening the file from
    the request thread halfway through the write — half a picture, served
    under a `?v=` the face has already decided is new.

    Blocking, and called through `asyncio.to_thread` for that reason. The
    picture is around 8 MB and the loop that would otherwise carry the
    `write(2)` is the one holding the microphone's WebSocket, at 125 frames a
    second. On this machine's APFS the write is a millisecond or two and
    costs nobody anything; the reason it is threaded anyway is that neither
    the size nor the disk is fixed here — `SCREENSAVER_DIR` is an
    environment variable, and #10 moves the server onto whatever the Pi
    boots from.
    """
    picture = image_path()
    part = picture.with_name(picture.name + ".part")
    part.write_bytes(raw)
    os.replace(part, picture)


def _bytes_of(response) -> bytes | None:
    """The first image in a `generate_content` response, or None.

    None and never `b""`, and the difference is the whole reason this is a
    function. An empty file passes the route's `is_file()`, so a `Blob` that
    came back with no `data` in it — a safety block, a truncated candidate —
    would be served as zero bytes with a 200 on them, and #84's `<img>`
    would sit there having failed to load with nothing saying why. The old
    photograph is a better answer than a new nothing.

    The mime type is checked as well as the data, which is the rule the SDK
    applies to the same object in `Blob.as_image` (`types.py:2098`): the
    response is asked for as TEXT and IMAGE, so the parts are a mixture and
    the text ones must not be mistaken for a photograph.
    """
    for part in response.parts or ():
        blob = getattr(part, "inline_data", None)
        if blob is None or not blob.data:
            continue
        if (blob.mime_type or "").startswith("image/"):
            return blob.data
    return None


def _generate_sync(prompt: str) -> bytes | None:
    """One picture, blocking. The only model-shaped code in this module.

    `generate_content` with `response_modalities`, not `generate_images`.
    `models.generate_images` (`google/genai/models.py:6504`) delegates to
    `_generate_images` (`:4976`), which raises `ValueError` unless the client
    is in Vertex mode and then builds a `:predict` path — the Imagen surface,
    which is neither this model nor available on the Developer API path
    `use_vertex()` lets this repo run on.

    `global_client()` rather than `genai_client()`, on the precedent of every
    other 3.x model here: `gws`, `browse` and `plan` are all pinned to the
    global endpoint because the regional ones answer a `generate_content` for
    them with a 404. Unmeasured for this model, and the fallback if it turns
    out to be regional is the other client.

    16:9 at 2K. The panel is 1280x800 (`README.md:1820`) and #84's CSS is
    `object-fit: cover`, so a 16:9 frame is cropped a little top and bottom
    rather than leaving the gradient showing down the sides.
    """
    response = global_client().models.generate_content(
        model=image_model(),
        contents=prompt,
        config=types.GenerateContentConfig(
            # Both, not IMAGE alone: the image models answer with a text part
            # beside the picture, and `_bytes_of` skips whatever is not one.
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio="16:9",
                image_size="2K",
            ),
        ),
    )
    return _bytes_of(response)


def _due(mono: float, slot: str, last_slot,
         failed_at: float | None = None, retry: float = RETRY_S) -> bool:
    """Whether this slot is one nothing has been drawn for yet.

    Identity, not elapsed time, and everything the identity buys is argued
    for in `_slot_at`. The backoff is the one piece of clock arithmetic left:
    a generation that failed a minute ago is not retried until `RETRY_S` has
    gone by, however due the slot is, so a WAN that is refusing everything
    costs one attempt a quarter of an hour rather than one every tick.

    That arithmetic is on `time.monotonic()` and the slot identity above it
    is on the wall clock, and the two never meet: `mono` and `failed_at` are
    both readings of the same monotonic clock, `slot` and `last_slot` are
    both names built from wall times, and no comparison here crosses. It has
    to be that way round because a wall clock steps and this hardware steps
    it forward on every cold start — a Pi 5 has no RTC, so it comes up on
    whatever `fake-hwclock` saved and `systemd-timesyncd` corrects it once
    the WAN is there, which is exactly the moment a backoff armed by that
    WAN is live. A step is not elapsed time; on the wall clock the
    subtraction would cross `retry` early by the size of it and retry
    sooner than the quarter of an hour that was promised.
    """
    if failed_at is not None and mono - failed_at < retry:
        return False
    return slot != last_slot


async def generate_once(now: float, slot: str, seen: dict,
                        state: dict) -> bool:
    """Ask for one picture and put it on disk. True if one landed.

    Everything is caught, for the reason `refresh_once` catches everything:
    the worst an image endpoint returning a 503 at 03:00 is allowed to cost
    is the photograph the panel is already wearing. A failure writes no
    sidecar, so `last_slot` does not move and the old bytes stay exactly
    where they are — the caller turns that into a short retry rather than a
    whole slot of nothing.
    """
    places = list(state.get("places") or [])
    place = _pick_place(places)
    prompt = _prompt(place, seen, datetime.fromtimestamp(now).date())

    try:
        raw = await asyncio.to_thread(_generate_sync, prompt)
    except Exception:  # pylint: disable=broad-except
        logger.warning("screensaver: image generation failed", exc_info=True)
        return False
    if not raw:
        logger.warning("screensaver: the model answered with no image")
        return False

    try:
        await asyncio.to_thread(_write_image, raw)
    except OSError:
        logger.warning("screensaver: could not write the picture",
                       exc_info=True)
        return False
    _write_state(slot, places + [place])
    logger.info("screensaver: new background for %s, of %s, %d bytes",
                slot, place, len(raw))
    return True


async def generate_if_due(now: float, mono: float | None = None) -> bool:
    """Generate the background if it is time to. True if one landed.

    Driven off the weather refresh rather than off a timer of its own, so
    the picture is checked once every `REFRESH_S` and lands within ten
    minutes of falling due. A second task would buy those ten minutes back
    and cost a second thing for the lifespan to remember to cancel, on a
    photograph nobody is waiting for.

    Nothing is generated before there is weather to draw. A terminal with no
    `HOME_COORDS` and no WAN would otherwise spend a picture on whatever the
    prompt reads like with the blanks empty, and that costs the same as a
    real one. It is not counted as a failure either — there is nothing to
    back off from. That is also why no slot is guessed at on a cold boot:
    with no refresh behind it there are no sun times, and `night` is the
    only identity `_slot_at` can honestly return.

    Two clocks, and which one answers which question is the whole of it.
    `now` is the wall clock and buys the two things that are wall-clock
    questions: which slot this is, and which date to draw. `mono` is
    `time.monotonic()` and buys the one thing that is a duration — how long
    ago the last attempt failed. Nothing is ever compared across the two.
    `mono` is a parameter only so that a check script can drive the backoff
    without sleeping for a quarter of an hour; the default is the reading,
    so a caller that does not care cannot pick the wrong clock.
    """
    global _failed_at

    if mono is None:
        mono = time.monotonic()
    seen = _seen
    if not seen or seen.get("temperature") is None:
        return False
    if _refreshed_at is None or mono - _refreshed_at > STALE_S:
        logger.info("screensaver: the last reading is too old to draw from; "
                    "leaving the picture where it is")
        return False
    slot = _slot_at(now, seen)
    state = _read_state()
    if not _due(mono, slot, state.get("last_slot"), _failed_at):
        return False

    if await generate_once(now, slot, seen, state):
        _failed_at = None
        return True
    _failed_at = mono
    return False


async def refresh_loop(interval: float = REFRESH_S) -> None:
    """Keep the weather line current for as long as the server is up.

    What has to survive is the loop, not the attempt: a refresh that fails
    must not be the last one made. `refresh_once` already catches the
    network, so this `except` is for the failures nobody thought of — and the
    alternative is worse than it looks. A task that dies takes every later
    attempt with it and announces nothing: the server goes on answering
    audio, and the clock goes on showing this morning's temperature until
    somebody notices and reboots the Pi.

    The picture is checked on the same tick and inside a `try` of its own,
    because the two halves fail for unrelated reasons and neither is allowed
    to take the other down: an image endpoint that is refusing everything
    must not also stop the temperature updating.

    `CancelledError` is a `BaseException`, so it goes straight through this
    and shutdown can still stop the loop.
    """
    restore_home()
    while True:
        try:
            await refresh_once()
        except Exception:  # pylint: disable=broad-except
            logger.exception("screensaver: refresh raised, carrying on")
        try:
            await generate_if_due(time.time())
        except Exception:  # pylint: disable=broad-except
            logger.exception("screensaver: generate raised, carrying on")
        await asyncio.sleep(interval)
