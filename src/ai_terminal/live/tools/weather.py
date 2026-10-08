"""Weather, as structured data rather than prose.

"What's the weather like?" is the first starter chip on the page and the one
question a search grounding answers worst: it comes back as a paragraph with
link chips under it, when what a terminal on a kitchen counter owes you is a
number you can read from the other side of the room. So this is the one tool
that does not go through a model — it returns a temperature, a forecast, and a
condition code the frontend switches an animation on.

Open-Meteo rather than the Google Maps Platform Weather API. That API is not
enabled on this project, and ``GOOGLE_MAPS_API_KEY`` is a browser key with
referrer restrictions, so a server-side call would need both a project change
and a second key. Open-Meteo needs neither, and it hands back the WMO code, the
day/night flag and the local timezone in one request. ``_fetch`` is the only
function that knows any of that; a swap later is one function.

Coordinates come from the browser when the question is about here, which is
nearly always. For a named place, the grounding model is the geocoder: the
Geocoding and Places APIs are behind the same referrer-restricted key, and a
weather grid cell is kilometres across, so a model that puts 京都 within a
couple of them is exact enough. It also takes Japanese and street addresses,
which the keyless geocoders do not.
"""

import asyncio
import logging
from datetime import date, datetime

import requests
from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext
from google.genai.types import GenerateContentConfig

from ._client import grounding_client, grounding_model
from ._lang import normalize, of_turn
from .maps import _is_here, here

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Enough to fill the card and no more. The hourly strip shows half a day, and
# five days is what fits under it on the panel without the rows going
# unreadable. More would fit in the 800 px the panel turned out to have, but
# the sixth day of a forecast is a guess nobody leans in for.
HOURS = 12
DAYS = 5

TIMEOUT = 10

CURRENT = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "weather_code,wind_speed_10m,wind_direction_10m,is_day"
)
HOURLY = "temperature_2m,weather_code,precipitation_probability,is_day"
DAILY = (
    "weather_code,temperature_2m_max,temperature_2m_min,"
    "precipitation_probability_max,sunrise,sunset"
)

# WMO code -> the slug the card animates on, and what to call it.
#
# Nine slugs, not twenty-eight: the difference between drizzle and heavy
# drizzle is worth a word and not a different sky. The words are a table rather
# than a translation call because they are a closed set that never changes, and
# a card should not wait on a model to find out how to say "cloudy".
_CONDITIONS: dict[int, tuple[str, str, str]] = {
    0: ("clear", "Clear", "快晴"),
    1: ("mostly-clear", "Mainly clear", "晴れ"),
    2: ("partly-cloudy", "Partly cloudy", "晴れときどき曇り"),
    3: ("cloudy", "Overcast", "曇り"),
    45: ("fog", "Fog", "霧"),
    48: ("fog", "Freezing fog", "霧氷"),
    51: ("drizzle", "Light drizzle", "弱い霧雨"),
    53: ("drizzle", "Drizzle", "霧雨"),
    55: ("drizzle", "Heavy drizzle", "強い霧雨"),
    56: ("drizzle", "Freezing drizzle", "着氷性の霧雨"),
    57: ("drizzle", "Heavy freezing drizzle", "強い着氷性の霧雨"),
    61: ("rain", "Light rain", "小雨"),
    63: ("rain", "Rain", "雨"),
    65: ("rain", "Heavy rain", "大雨"),
    66: ("rain", "Freezing rain", "着氷性の雨"),
    67: ("rain", "Heavy freezing rain", "強い着氷性の雨"),
    71: ("snow", "Light snow", "小雪"),
    73: ("snow", "Snow", "雪"),
    75: ("snow", "Heavy snow", "大雪"),
    77: ("snow", "Snow grains", "霧雪"),
    80: ("rain", "Light showers", "弱いにわか雨"),
    81: ("rain", "Showers", "にわか雨"),
    82: ("rain", "Violent showers", "激しいにわか雨"),
    85: ("snow", "Snow showers", "にわか雪"),
    86: ("snow", "Heavy snow showers", "激しいにわか雪"),
    95: ("thunderstorm", "Thunderstorm", "雷雨"),
    96: ("thunderstorm", "Thunderstorm with hail", "雷雨・ひょう"),
    99: ("thunderstorm", "Severe thunderstorm with hail", "激しい雷雨・ひょう"),
}

_UNKNOWN = ("cloudy", "Unknown", "不明")

_WEEKDAYS = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "ja": ["月", "火", "水", "木", "金", "土", "日"],
}
_NAMED = {"en": ["Today", "Tomorrow"], "ja": ["今日", "明日"]}

_GEOCODE_SCHEMA = {
    "type": "object",
    "required": ["latitude", "longitude", "name"],
    "properties": {
        "latitude": {"type": "number"}, "longitude": {"type": "number"},
        "name": {"type": "string", "description": (
            "Specific neighborhood, district, locality, or sublocality name (such as Ueno, "
            "Ebisu, Roppongi, Soho, strictly preferring closest neighborhood over broader "
            "administrative ward without jumping across water bodies to neighboring Toyosu)"
        )},
    },
}


def _base(lang: str) -> str:
    """The language subtag the tables are keyed on. English for anything else."""
    tag = normalize(lang).split("-")[0].lower()
    return tag if tag in _WEEKDAYS and tag in _NAMED else "en"


def _describe(code: int | None, lang: str) -> tuple[str, str]:
    """A WMO code as (slug, phrase)."""
    slug, en, ja = _CONDITIONS.get(code, _UNKNOWN) if code is not None else _UNKNOWN
    return slug, ja if _base(lang) == "ja" else en


def _clock(iso: str | None) -> str:
    """The time out of an Open-Meteo local-time stamp. Already local — the
    request asks for `timezone=auto`, so there is nothing to convert.

    `None` reads the same as absent, and a caller that needs to tell those
    two apart should look at the field rather than at this. The annotation is
    the point of the guard as much as the `if` is: it used to say `str`, which
    is what made `(daily.get("sunrise") or [""])[0]` at `:285` look safe —
    that `or` short-circuits on a truthy `[None]`, and `len(None)` raised out
    of the tool with nothing above it to catch the exception.

    Nothing in the API is known to send a null today. Asked on 2026-09-09,
    Open-Meteo answers a day the sun does not cross the horizon with midnight
    stamps rather than with a null, at both ends. The guard is for the shape,
    not for a caller anybody has seen.

    What those midnight stamps *mean* is `_sun`'s business rather than this
    one's. One string on its own cannot tell a polar night from the 00:46
    sunrise the shoulder weeks near the circle really do have.
    """
    if not iso:
        return ""
    return iso[11:16] if len(iso) >= 16 else iso


# What the sun did today. Four states rather than two strings, because the
# days that read wrong are the ones with no sunrise on them and a time cannot
# say that. `_weather_sync` puts these in the reading beside the rendered pair.
SUN_RISES = "rises"
SUN_POLAR_DAY = "polar_day"
SUN_POLAR_NIGHT = "polar_night"
SUN_UNKNOWN = "unknown"


def _sun(daily: dict, is_day: bool) -> dict:
    """Today's sunrise and sunset: rendered, raw, and whether there were any.

    Open-Meteo does not answer a day the sun stays down, or one it stays up,
    with a null. Measured on 2026-09-09 and pinned in-repo at
    `screensaver_check.slot_cases`, the two shapes are not even the same shape
    as each other:

    - 89.9°S, no sunrise: `2026-09-10T00:00` at both ends, doubled outright.
    - 89.9°N, no sunset: `2026-09-09T00:00` for the sunrise and
      `2026-09-10T00:00` for the sunset — *tomorrow's* midnight, so the raw
      strings differ.

    So the test is the pair **after `_clock`**, not before it. Both shapes
    slice to `00:00` and `00:00`; comparing the raw stamps catches the first
    and misses the second, which is how a polar day comes out as an ordinary
    day whose sun rose and set at midnight. Rendering that is the bug — two
    identical times, worn with the confidence of a reading.

    This is the only place both times are visible at once, which is why
    `_clock` cannot be where it is decided: one string cannot tell midnight
    from midnight. Nor does the compare eat a genuine near-midnight sunrise,
    because those two times differ — 83.8°N on the same day answers `00:46`
    and `22:43`, and that day still renders as two times.

    Which way a degenerate day went comes from `current.is_day`: a day the sun
    never sets is day all day, a day it never rises is night all day.
    `daylight_duration` would say the same and would cost a wider request for
    a field this repo has never received, so it is not used.

    Five keys, and the last three are the contract a scheduler builds on:

    - `sunrise` / `sunset`: `HH:MM` local for the card, `""` when there is no
      such moment. The card's `|| "—"` fallback already covers the empty, so
      an unknown reads as unknown rather than as midnight.
    - `sunrise_at` / `sunset_at`: the provider's local stamp as it arrived, or
      `None` when there is no such moment. `datetime.fromisoformat` parses
      one. Anchor on these, never on the rendered pair.
    - `sun`: `rises`, `polar_day`, `polar_night` or `unknown` — why the `_at`
      pair is missing on the days it is.
    """
    rise = (daily.get("sunrise") or [None])[0] or None
    down = (daily.get("sunset") or [None])[0] or None
    first, last = _clock(rise), _clock(down)

    if first and last and first == last:
        state = SUN_POLAR_DAY if is_day else SUN_POLAR_NIGHT
        first = last = ""
        rise = down = None
    elif first and last:
        state = SUN_RISES
    else:
        # One of the two, or neither. Whatever did arrive is still shown and
        # still anchorable; what is not known is `None` and says so.
        state = SUN_UNKNOWN

    return {
        "sunrise": first,
        "sunset": last,
        "sunrise_at": rise,
        "sunset_at": down,
        "sun": state,
    }


def _at_home(location: str, tool_context) -> bool:
    """Whether a `location` argument is asking about the terminal itself.

    Three spellings mean it, and only the first is the one the model is asked
    for: empty, which `agent.py:278-281` tells it to send; the word `here`,
    which is what `maps_search` was taught to take in #196; and the address out
    of that same "Where you are" paragraph, which is what it actually sent on
    both of the calls #181 was filed from.

    The third is the one that costs something, and it is the model following
    the general rule over the exception — the paragraph opens by telling it to
    pass this address to the tools. The address is a reverse-geocoding of the
    pin, so geocoding it again cannot get back to the pin, and this geocoder
    does not return the same answer twice anyway: #181's two calls, twelve
    minutes apart for one address, came back with names of 24 and 15
    characters.

    `.strip().lower()` on the word for the reason `maps._where` gives, and
    `casefold` against the address, which can be non-ASCII. Compared whole
    rather than by prefix — a district whose name is the first line of this
    address is a different question.
    """
    location = location.strip()
    return (not location or _is_here(location)
            or location.casefold() == here(tool_context).strip().casefold())


def _geocode(place: str, lang: str) -> tuple[float, float, str] | None:
    """Turn a place name into coordinates, via the grounding model.

    Returns None rather than raising: a weather question with an unrecognisable
    place in it should fall back to here, not fail.

    And on a machine with no credentials at all, for the same reason. The
    original list of exceptions was the ones a bad *answer* raises, which
    quietly assumed the call had happened — on a Pi missing ADC it does not, and
    `weather` was the tool being recommended as the thing to try when grounding
    looks broken. `Exception` rather than `DefaultCredentialsError` by name:
    every way this call can fail ends in the same fallback, and a narrower list
    is how it came to be wrong the first time.
    """
    try:
        response = grounding_client().models.generate_content(
            model=grounding_model(),
            # The name comes back too, and in the language of the turn: the card
            # heading is the one string here that is not a number, and "Kyoto"
            # over a Japanese forecast is the sort of seam that gets noticed.
            contents=(
                f"Latitude, longitude, and specific neighborhood or sublocality "
                f"name (such as Ueno, Ebisu, Roppongi, Soho, Greenwich Village "
                f"— strictly preferring the closest neighborhood or sublocality "
                f"over a broader ward, city, or administrative area, resolving "
                f"strictly to the immediate neighborhood without jumping across "
                f"water bodies or administrative wards to neighboring areas like "
                f"Toyosu) of: {place}\n"
                f"Write the name in this language: {normalize(lang) or 'en'}"
            ),
            config=GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_GEOCODE_SCHEMA,
                # The coordinates of a city are a fact, not a choice.
                temperature=0,
            ),
        )
        found = response.parsed or {}
        lat, lng = float(found["latitude"]), float(found["longitude"])
    except Exception:
        # Sized, both here and below. `place` is `where` from
        # :func:`forecast`, and on a panel nobody has pinned that is `here()`
        # falling back to `HOME_LOCATION` — the household's real address out
        # of `.env`. Both sites need a geocode failure first, so they are
        # rare; what keeps them rare is the model leaving `location` empty
        # when it means home, which is a prompt and not a mechanism. And the
        # name is not what either line is read for. This one has the
        # traceback (#226).
        logger.warning("weather: could not geocode <%d chars>", len(place),
                       exc_info=True)
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        # And this one has the coordinates it refused.
        logger.warning("weather: <%d chars> geocoded off the planet: %s,%s",
                       len(place), lat, lng)
        return None
    return lat, lng, (found.get("name") or place)


_REVERSE_GEOCODE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Specific neighborhood, district, locality, or sublocality name (such as Ueno, "
                "Ebisu, Roppongi, Soho, Greenwich Village — strictly preferring "
                "the specific neighborhood, district, locality, or sublocality "
                "over a broader ward, city, or administrative area, "
                "resolving strictly to the immediate neighborhood containing the "
                "coordinates without jumping across water bodies or administrative "
                "wards to neighboring areas like Toyosu, excluding street address, "
                "house number, or block/chome number), in the asked-for "
                "language"
            ),
        },
    },
    "required": ["name"],
}


def _reverse_geocode(lat: float, lng: float, lang: str) -> str | None:
    """Turn coordinates into a district or locality name, via the grounding model.

    Returns None rather than raising: a screensaver or weather request with
    unresolvable coordinates falls back gracefully.
    """
    coords = f"{lat},{lng}"
    try:
        response = grounding_client().models.generate_content(
            model=grounding_model(),
            contents=(
                f"Specific neighborhood or sublocality name (such as Ueno, "
                f"Ebisu, Roppongi, Soho, Greenwich Village — strictly preferring "
                f"the closest neighborhood or sublocality over a broader ward, "
                f"city, or administrative area, resolving strictly to the immediate "
                f"neighborhood containing the coordinates without jumping across "
                f"water bodies or administrative wards to neighboring areas like "
                f"Toyosu) at coordinates: {lat}, {lng}\n"
                f"Write the name in this language: {normalize(lang) or 'en'}"
            ),
            config=GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_REVERSE_GEOCODE_SCHEMA,
                temperature=0,
            ),
        )
        found = response.parsed or {}
        name = (found.get("name") or "").strip()
        return name or None
    except Exception:
        logger.warning("weather: could not reverse geocode <%d chars>",
                       len(coords), exc_info=True)
        return None


# What a caller's `past_days` is clamped to. Believed to be Open-Meteo's own
# ceiling and not verified from here: nothing in this repo records the API's
# limits, and the reading is model knowledge rather than a measurement. That
# is why the clamp is not what makes this safe — the retry in `_fetch` is.
# The clamp only keeps an obviously silly number off the wire.
PAST_DAYS_MAX = 92


def _fetch(lat: float, lng: float, past_days: int = 0) -> dict:
    """One Open-Meteo forecast request. The only Open-Meteo-shaped code here.

    `past_days` asks for that many days *before* today, prepended to the same
    `daily` and `hourly` arrays at no second request and no second round trip.
    Zero for the card, which is a forecast; the screensaver asks for a week,
    because what the weather has actually been is what tells a picture apart
    from the name of the month (#185).

    A rejected `past_days` costs the whole reading, not just the week — it is
    the same request that carries the temperature on the clock — so a 400 with
    the parameter on drops it and asks once more without. The alternative is
    to trust `PAST_DAYS_MAX`, and it is a remembered number: get it wrong and
    `SCREENSAVER_SEASON_DAYS=92` freezes the panel's reading at whatever it
    last held, with nothing on screen to say why.
    """
    params = {
        "latitude": lat,
        "longitude": lng,
        "models": "jma_seamless",
        "current": CURRENT,
        "hourly": HOURLY,
        "daily": DAILY,
        # Local time everywhere in the response, so nothing downstream has
        # to know what timezone the place is in to print an hour.
        "timezone": "auto",
        "forecast_days": DAYS,
    }
    if past_days > 0:
        params["past_days"] = min(past_days, PAST_DAYS_MAX)
    response = requests.get(FORECAST_URL, params=params, timeout=TIMEOUT)
    if response.status_code == 400 and "past_days" in params:
        logger.warning("weather: past_days=%s refused; asking again without "
                       "it", params.pop("past_days"))
        response = requests.get(FORECAST_URL, params=params, timeout=TIMEOUT)
    if response.status_code == 400 and "models" in params:
        logger.warning("weather: models=%s refused; asking again without it",
                       params.pop("models"))
        response = requests.get(FORECAST_URL, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def _split_daily(daily: dict, today: str) -> tuple[dict, dict]:
    """The daily arrays cut in two at today: before it, and from it onwards.

    This is the whole cost of `past_days`, and it is not optional. Open-Meteo
    *prepends* the past to every array in `daily`, and everything downstream
    of here indexes from zero and means today by it: `_daily` labels row 0
    "today" (`:333`), and `_sun` reads element 0 as this morning's sunrise
    (`:207-208`), which is what the screensaver hangs six of its seven slots
    off. Ask for a week without cutting here and the card's five-day forecast
    silently becomes last week's, under a row still labelled 今日.

    A `daily` with no `time` in it cuts at zero, so nothing is trimmed and
    nothing is claimed as history. That is also the shape a request with
    `past_days=0` returns, which is why the card path costs nothing.
    """
    times = daily.get("time") or []
    cut = next((i for i, day in enumerate(times) if day >= today), len(times))
    before = {key: value[:cut] for key, value in daily.items()
              if isinstance(value, list)}
    after = {key: value[cut:] if isinstance(value, list) else value
             for key, value in daily.items()}
    return before, after


def _recent(before: dict) -> dict | None:
    """What the temperature actually did over the days before today.

    None rather than a dict of Nones when there is no history — nothing was
    asked for, or the provider sent none — so a caller can tell "no week" from
    "a flat week" and leave the sentence out rather than print `None to None`.

    Three numbers and they are not the same kind of number. `low` and `high`
    are the window's **extremes** — the coldest night and the warmest
    afternoon anywhere in it — while `mean` is over the highs and the lows
    together, the daily midpoint averaged across the window and not a
    temperature anybody measured.

    Extremes are the right answer at a week, which is what this is for: "it
    has run 24 to 33" is how a person describes the week they have just had,
    and the outlying afternoon is part of what the place looked like. They
    get worse the longer the window, because an extreme over a quarter is one
    day out of ninety and says nothing about the other eighty-nine — at
    `SCREENSAVER_SEASON_DAYS=92` the sentence reads "run 8.2 to 36.1 degrees,
    averaging 22.0" and only the last number is still informative. That is an
    argument about how far the knob should turn, and it is the owner's.
    """
    lows = [v for v in (before.get("temperature_2m_min") or []) if v is not None]
    highs = [v for v in (before.get("temperature_2m_max") or []) if v is not None]
    if not lows or not highs:
        return None
    return {
        "days": max(len(lows), len(highs)),
        "low": round(min(lows), 1),
        "high": round(max(highs), 1),
        "mean": round((sum(lows) + sum(highs)) / (len(lows) + len(highs)), 1),
    }


def _hourly(raw: dict, now: str, lang: str) -> tuple[list[dict], int | None]:
    """The next `HOURS` hours, and the chance of rain in the current one.

    Open-Meteo returns the hourly series from midnight local, so most of it is
    already in the past by the afternoon. The strip starts at the first hour
    that has not finished yet.
    """
    hourly = raw.get("hourly") or {}
    times = hourly.get("time") or []
    start = next((i for i, t in enumerate(times) if t >= now[:13]), 0)

    strip = []
    for i in range(start, min(start + HOURS, len(times))):
        slug, phrase = _describe((hourly.get("weather_code") or [None])[i], lang)
        strip.append({
            "time": _clock(times[i]),
            "temperature": (hourly.get("temperature_2m") or [None])[i],
            "precipitation_chance": (hourly.get("precipitation_probability")
                                     or [None])[i],
            "is_day": bool((hourly.get("is_day") or [1])[i]),
            "condition": slug,
            "description": phrase,
        })

    chance = strip[0]["precipitation_chance"] if strip else None
    return strip, chance


def _daily(raw: dict, lang: str) -> list[dict]:
    """One row per day. The first two are named; the rest are weekdays."""
    daily = raw.get("daily") or {}
    times = daily.get("time") or []
    base = _base(lang)

    rows = []
    for i, day in enumerate(times[:DAYS]):
        slug, phrase = _describe((daily.get("weather_code") or [None])[i], lang)
        wday = _WEEKDAYS[base][date.fromisoformat(day).weekday()]
        rows.append({
            "date": day,
            "label": _NAMED[base][i] if i < 2 else wday,
            "weekday": wday,
            "high": (daily.get("temperature_2m_max") or [None])[i],
            "low": (daily.get("temperature_2m_min") or [None])[i],
            "precipitation_chance": (daily.get("precipitation_probability_max")
                                     or [None])[i],
            "condition": slug,
            "description": phrase,
        })
    return rows


def _weather_sync(lat: float, lng: float, place: str, lang: str,
                  past_days: int = 0) -> dict:
    raw = _fetch(lat, lng, past_days)
    current = raw.get("current") or {}
    units = raw.get("current_units") or {}

    now = current.get("time") or datetime.now().isoformat(timespec="minutes")
    # Everything below this line indexes `daily` from today, so the past days
    # come off here and go into `recent` instead. `_hourly` needs no such cut:
    # it already starts at the first hour that has not finished yet.
    #
    # Gated on `past_days`, not run unconditionally, and the difference is a
    # row on the card. `now` falls back to *this* machine's clock when the
    # provider sent no `current.time`, and that fallback exists precisely
    # because the code does not trust the field to be there. A Pi in JST
    # asking about London at 08:00 is then a day ahead of the place it is
    # cutting, and the cut takes a real forecast day off the front and files
    # it as history: five rows become four, with `recent` claiming one day of
    # a week nobody asked for. Nothing before today can have arrived unless
    # something asked for it, so on the card path there is nothing to cut.
    before, daily = (_split_daily(raw.get("daily") or {}, now[:10])
                     if past_days > 0 else ({}, raw.get("daily") or {}))
    raw = {**raw, "daily": daily}
    recent = _recent(before)
    strip, chance = _hourly(raw, now, lang)
    slug, phrase = _describe(current.get("weather_code"), lang)
    is_day = bool(current.get("is_day", 1))

    return {
        "location": place,
        "lang": normalize(lang),
        "timezone": raw.get("timezone", ""),
        **({"timezone_abbreviation": raw["timezone_abbreviation"]}
           if "timezone_abbreviation" in raw else {}),
        "units": {
            "temperature": units.get("temperature_2m", "°C"),
            "wind": units.get("wind_speed_10m", "km/h"),
            "precipitation": units.get("precipitation", "mm"),
        },
        "current": {
            "time": _clock(now),
            "condition": slug,
            "description": phrase,
            # The same sky as those two, in the one form that is not in a
            # language and not rounded to nine slugs. Nothing on the card
            # reads it; `screensaver` does, because it holds one reading and
            # is asked for it in whichever language the page in front of it
            # is set to, and neither the word nor the slug can be turned back
            # into the other twenty-seven.
            "weather_code": current.get("weather_code"),
            # The card picks a day or a night palette off this rather than off
            # the clock, which is wrong at both ends of a Nordic summer.
            "is_day": is_day,
            "temperature": current.get("temperature_2m"),
            "feels_like": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "precipitation": current.get("precipitation"),
            "precipitation_chance": chance,
            "wind_speed": current.get("wind_speed_10m"),
            "wind_direction": current.get("wind_direction_10m"),
            # `sunrise`, `sunset`, `sunrise_at`, `sunset_at` and `sun`.
            **_sun(daily, is_day),
        },
        "hourly": strip,
        "daily": _daily(raw, lang),
        # Only when a caller asked for history. Absent rather than null for
        # the card, because a key nothing on the page reads is a promise made
        # to nobody -- the rule `screensaver_check` states about `payload`.
        #
        # Deliberately not in `weather()`'s `Returns:`, though it is part of
        # what this function returns: `weather()` never passes `past_days`, so
        # the key cannot reach the tool path at all, and that docstring is
        # read by the model. Documenting a key it will never be sent is worse
        # than leaving it out. The screensaver is the only caller that asks.
        **({"recent": recent} if recent else {}),
    }


async def weather(
    location: str = "", lang: str = "", tool_context: ToolContext = None,
) -> dict:
    """Current weather and a five-day forecast, drawn as an animated card.

    Use this for any weather question rather than web_search — it comes back
    as numbers the screen can draw, where a search comes back as a paragraph.
    Covers "is it raining?", "how hot will it get?", "do I need an umbrella
    tomorrow?" and "what's the weather in Kyoto this week?".

    Args:
        location: A city, district or address to look up. Leave empty for
            where the terminal is, which is what "here" and "today" mean;
            the word `here` and the terminal's own address mean it too.
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. Conditions and weekdays on the card are written in it.

    Returns:
        A dict with the current conditions, an hourly strip and a daily
        forecast, each carrying a condition slug the frontend animates on.
    """
    lang = of_turn(lang, tool_context)
    state = getattr(tool_context, "state", None) or {}
    # The terminal is pinned to an exact spot, so for "here" those coordinates
    # beat geocoding the address they were reverse-geocoded into — however the
    # model spelled "here" (#181).
    place = "" if _at_home(location, tool_context) else location.strip()
    coords = None if place else state.get("home_coords")
    where = place or here(tool_context)

    if coords:
        found = (coords["lat"], coords["lng"], where or "Here")
    elif where:
        found = await asyncio.to_thread(_geocode, where, lang)
    else:
        found = None

    if not found:
        return {"error": "I don't know where to look up the weather for."}

    lat, lng, name = found
    return await asyncio.to_thread(_weather_sync, lat, lng, name, lang)


weather_tool = FunctionTool(weather)
