"""Google Maps grounding as three ADK tools: place search, route, and
search-along-route.

All three go through a sidecar ``generateContent`` call with Maps grounding
enabled. The model reads the intent out of the prompt; what matters back here
is the grounding metadata — the place chunks the frontend pins on a map.

Maps grounding can also hand back a widget context token for the
``gmp-place-contextual`` element. It comes back null on this project with
``enable_widget`` either way, so the widget is left out and the frontend pins
markers from the place IDs instead.
"""

import asyncio
import logging
import os
from typing import Any
import unicodedata

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext
from google.genai.types import GenerateContentConfig, GoogleMaps, Tool

from ._client import evict_if_closed, grounding_client, grounding_model
from ._lang import SCREEN_STYLE, answer_in, normalize, of_turn

logger = logging.getLogger(__name__)

MAX_PLACES = 5


def home_location() -> str:
    """The fallback for where "here" is, out of .env.

    Set ``HOME_LOCATION`` to a street address. The tools work without it, but
    the model then has to be told a location every time. Read on each call
    rather than at import so .env load order does not matter.

    This is the fallback and not the answer — see :func:`here`.
    """
    return os.getenv("HOME_LOCATION", "")


def here(context) -> str:
    """Where "here", "nearby" and "home" resolve to for this session.

    The client reports the terminal's own location when it connects and it
    lands in session state (``main.py``); ``HOME_LOCATION`` covers a client
    that sent none. State first, because a browser that knows where it is
    knows better than a file written once — the env var pins every session to
    the same address, which is right for the Pi on the kitchen wall and wrong
    for a laptop somewhere else asking what is nearby.

    `context` is anything carrying a `.state`, or None: a `ToolContext` from a
    tool, the `ReadonlyContext` the instruction is built from. The rule has to
    be the same in both places — the prompt tells the model which address to
    pass, and a prompt naming one address while the tools default to another
    is worse than either being wrong by itself.
    """
    state = getattr(context, "state", None) or {}
    loc = state.get("home_location") or home_location()
    if not loc and state.get("home_coords"):
        coords = state["home_coords"]
        if isinstance(coords, dict) and "lat" in coords and "lng" in coords:
            return f"{coords['lat']:.5f}, {coords['lng']:.5f}"
    return loc


# Everything the summary should not do, which is most of what it did. Kept out
# of the language directive because none of it is about language: it applies
# to the English answer as much as the Japanese one.
_HOUSE_STYLE = (
    "You are writing about places found on a map. Name the "
    "neighbourhood, station or district you searched around. Never write "
    "\"the specified location\" or \"the given area\" — if you cannot name "
    "where you looked, leave it out of the sentence. Write each place's own "
    "name as a person would say it, not the title of the citation it came "
    "from, and never write \" - Google Maps\"."
)


def _ground_on_maps(prompt: str, limit: int, lang: str) -> dict:
    """Run one Maps-grounded prompt and pull the places out of the metadata.

    The summary follows ``lang``. The chip titles do not come from here at
    all: they are grounding chunks copied out of the metadata, and Maps
    grounding hands them back romanised — "Yamada" for a shop the summary
    calls ヤマダデンキ. The browser is where that gets fixed, not this
    module. ``cards.js`` re-resolves each ``place_id`` through Maps JS, which
    is loaded with the terminal's own locale, and writes the local name onto
    the chip and the marker when it arrives; the key is referrer-restricted
    and browser-only, so the browser is the only side that could. The locale
    is the page's rather than the turn's, so an English turn on a Japanese
    terminal gets English prose over Japanese chips — the rarer half of the
    mismatch, and the one that needs a per-lookup language to close.

    Everything about how to write goes in the system instruction and nothing
    goes in `contents`. Maps grounding searches from `contents`, so a house
    style appended there is read as part of the query: with the same
    家電量販店 and the same Tokyo address, moving these three sentences into
    the prompt returned shops in Yokohama and Mitaka and no place chunks at
    all. It is a prompt, not a config field, and it behaves like one.
    """
    directive = answer_in(lang)
    client = grounding_client()
    try:
        response = grounding_client().models.generate_content(
            model=grounding_model(),
            contents=prompt,
            config=GenerateContentConfig(
                tools=[Tool(google_maps=GoogleMaps())],
                system_instruction=f"{SCREEN_STYLE}\n\n{_HOUSE_STYLE}\n\n{directive}",
            ),
        )

        candidate = response.candidates[0] if response.candidates else None
        parts = candidate.content.parts if candidate and candidate.content else None
        summary = parts[0].text if parts and parts[0].text else ""
        # Belt and braces: the instruction above asks the model not to quote the
        # citation title, and the suffix is cheap enough to remove when it does.
        summary = summary.replace(" - Google Maps", "")

        result: dict = {"summary": summary, "places": []}

        gm = candidate.grounding_metadata if candidate else None
        for chunk in (gm.grounding_chunks or [])[:limit] if gm else []:
            if not chunk.maps:
                continue
            result["places"].append({
                # Every chunk title ends in " - Google Maps"; it reads as noise
                # repeated down a column of chips.
                "title": (chunk.maps.title or "").removesuffix(" - Google Maps"),
                "uri": chunk.maps.uri,
                "place_id": chunk.maps.place_id,
            })

        return result
    except Exception as err:
        logger.warning("maps: could not ground on maps: %s", err)
        evict_if_closed(client)
        fallback = "Could not retrieve route information." if "directions" in prompt else ""
        return {"summary": fallback, "places": []}


# Every spelling of "where the terminal is standing" that a turn in either
# language might land in `near` or `origin`. It was the single English word
# `here` until #223, on a panel that is spoken to in Japanese: `現在地` went to
# Maps grounding as the name of a place, the model found something plausible
# somewhere else, and `_HOUSE_STYLE` then asked it to name the district it had
# searched — a wrong place stated back with confidence rather than an error.
#
# Membership is tested against the whole argument, never as a substring, so a
# place whose name merely contains one of these — "Here Comes Cafe",
# 周辺機器ショップ — is still searched for.
HERE = frozenset({
    "here", "nearby", "near me", "around here", "around me", "close by",
    "this area", "my location", "my current location", "current location",
    "current position", "home",
    "現在地", "現在位置", "現在の場所", "自宅",
    "ここ", "ここら", "ここらへん", "この辺", "この辺り", "このへん",
    "この近く", "この付近", "付近", "周辺", "近辺", "近く", "近所", "最寄り",
})

# What a model leaves on the end when it copies the phrasing out of the turn
# rather than writing the slot fresh: "ここから横浜駅まで" arrives as
# `origin="ここから"`. Trimmed for the comparison only — an argument that
# misses is still passed on exactly as it came, so nothing here can shorten a
# real place name.
_PARTICLES = ("から", "より", "では", "には", "で", "に", "の")


def _is_here(text: str) -> bool:
    """Whether this argument names the terminal's own pin rather than a place.

    NFKC before the comparison, because the fold is free and it catches the
    full-width Latin a Japanese IME produces (`ｎｅａｒｂｙ`) without touching
    the kanji beside it. Normalised for the test only; :func:`_where` returns
    the caller's own string when this says no.
    """
    text = unicodedata.normalize("NFKC", text).strip().lower()
    if text in HERE:
        return True
    return any(text.endswith(p) and text[:-len(p)] in HERE
               for p in _PARTICLES)


def _where(near: str, tool_context) -> str:
    """The one place the three states of a location filter are decided.

    Empty means anywhere, any spelling of "here" means the terminal's pin, and
    anything else is taken literally. Before this existed there were two
    states where the domain has three, and the two were the wrong way round:
    an omitted `near` silently became the pin, so "日本で一番高い山は？" drew a
    map of 港区 — Tokyo has enough 富士塚 for Places to find one — and the word
    `here` itself went to Maps as part of the search string (#196).

    `.strip()` and `.lower()`, because the model is not under oath about
    capitalisation or padding and `"Here"` reaching the prompt as a search
    term is the same failure in a different costume. Which spellings count is
    :data:`HERE` (#223).
    """
    near = near.strip()
    return here(tool_context) if _is_here(near) else near


def _from(origin: str, tool_context) -> str:
    """The same three states for a route, except that empty is still here.

    A search with no area is a search of everywhere, which is a sensible thing
    to want. A route with no origin is not a route, so the only two candidates
    for an empty one are the pin and an error, and the pin is what a person
    standing in front of the terminal means. That asymmetry is deliberate and
    is why this is not :func:`_where` (#196).
    """
    return _where(origin, tool_context) or here(tool_context)


def _to(destination: str, tool_context: Any = None) -> str:
    """Resolve 'here' or home terms in destination to coordinates, leaving empty strings and specific places untouched."""
    if destination and _is_here(destination):
        return here(tool_context)
    return destination


def _coords_for(place: str, tool_context) -> dict | None:
    """The terminal's coordinates if place refers to here/home, else None."""
    state = getattr(tool_context, "state", None) or {}
    coords = state.get("home_coords")
    if not coords or not isinstance(coords, dict):
        return None
    lat = coords.get("lat")
    lng = coords.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    target = (place or "").strip()
    if not target or _is_here(target) or target.casefold() == here(tool_context).strip().casefold():
        return {"lat": float(lat), "lng": float(lng)}
    return None


def _maps_search_sync(query: str, where: str, lang: str) -> dict:
    prompt = f"Return only the top {MAX_PLACES} results for: {query}"
    if where:
        prompt += f" near {where}"
    return {
        "query": query,
        "near": where,
        "lang": normalize(lang),
        **_ground_on_maps(prompt, MAX_PLACES, lang),
    }


def _maps_route_sync(
    origin: str, destination: str, travel_mode: str, lang: str,
    origin_coords: dict | None = None, destination_coords: dict | None = None,
) -> dict:
    prompt = (
        f"Give me {travel_mode.lower()} directions from {origin} to "
        f"{destination}. Include the total travel time and distance."
    )
    result = {
        "origin": origin,
        "destination": destination,
        "travel_mode": travel_mode.upper(),
        "lang": normalize(lang),
        **_ground_on_maps(prompt, MAX_PLACES, lang),
    }
    if origin_coords:
        result["origin_coords"] = origin_coords
    if destination_coords:
        result["destination_coords"] = destination_coords
    return result


def _maps_search_along_route_sync(
    origin: str, destination: str, query: str, lang: str,
    origin_coords: dict | None = None, destination_coords: dict | None = None,
) -> dict:
    prompt = (
        f"Find the top {MAX_PLACES} {query} along the driving route "
        f"from {origin} to {destination}."
    )
    result = {
        "origin": origin,
        "destination": destination,
        "travel_mode": "DRIVE",
        "query": query,
        "lang": normalize(lang),
        **_ground_on_maps(prompt, MAX_PLACES, lang),
    }
    if origin_coords:
        result["origin_coords"] = origin_coords
    if destination_coords:
        result["destination_coords"] = destination_coords
    return result


async def maps_search(
    query: str, near: str = "", lang: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Find real places on Google Maps — shops, restaurants, stations, parks.

    Use for "is there a X nearby", "find a good Y", opening hours, or ratings
    of a specific place. When someone explicitly asks to search for places or
    find shops/spots on a map (e.g. "〜を地図で探して", "地図で検索して", "マップで探して"),
    call this tool directly rather than answering without tools, even after a plan
    or schedule card was produced in an earlier turn.
    The results are drawn on a map on screen.

    Args:
        query: What to look for, e.g. "ramen shops open late".
        near: Where to search. Pass `here` for around the terminal, a street
            address or place name to search around that, or leave it empty to
            search anywhere. Leave it empty for anything famous — a mountain,
            a museum, a city — or the search is confined to this
            neighbourhood and finds the wrong one.
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. The summary shown on screen is written in it.

    Returns:
        A dict with a summary and the matching places.
    """
    return await asyncio.to_thread(
        _maps_search_sync, query, _where(near, tool_context),
        of_turn(lang, tool_context),
    )


async def maps_route(
    origin: str,
    destination: str,
    travel_mode: str = "DRIVE",
    lang: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Get directions, distance, and travel time between two places.

    Use for "how do I get to X", "how far is Y", "how long does it take",
    or when a destination is given after asking for directions. When only
    a single place is named, treat the terminal's location (here) as the origin
    and the named place as the destination, calling this tool directly rather
    than asking whether it is origin or destination.
    The route is drawn on a map on screen.

    Args:
        origin: Where the trip starts, as a street address or place name.
            Pass `here`, or leave it empty, for the terminal's own location.
        destination: Where the trip ends. Pass `here` or `home` for the
            terminal's own location.
        travel_mode: One of DRIVE, WALK, BICYCLE, TRANSIT. Defaults to DRIVE.
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. The directions shown on screen are written in it.

    Returns:
        A dict with a route summary and the places on it.
    """
    origin_coords = _coords_for(origin, tool_context)
    destination_coords = _coords_for(destination, tool_context)
    return await asyncio.to_thread(
        _maps_route_sync,
        _from(origin, tool_context), _to(destination, tool_context),
        travel_mode, of_turn(lang, tool_context),
        origin_coords=origin_coords,
        destination_coords=destination_coords,
    )


async def maps_search_along_route(
    origin: str,
    destination: str,
    query: str,
    lang: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Find places to stop at along a driving route.

    Use for "somewhere to eat on the way to X", "a petrol station between here
    and Y". Driving only — this is a preview limitation of Maps grounding.

    Args:
        origin: Where the trip starts, as a street address or place name.
            Pass `here`, or leave it empty, for the terminal's own location.
        destination: Where the trip ends. Pass `here` or `home` for the
            terminal's own location.
        query: What to look for along the way, e.g. "coffee shops".
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. The summary shown on screen is written in it.

    Returns:
        A dict with a route summary and the matching stops.
    """
    origin_coords = _coords_for(origin, tool_context)
    destination_coords = _coords_for(destination, tool_context)
    return await asyncio.to_thread(
        _maps_search_along_route_sync,
        _from(origin, tool_context), _to(destination, tool_context),
        query, of_turn(lang, tool_context),
        origin_coords=origin_coords,
        destination_coords=destination_coords,
    )


maps_search_tool = FunctionTool(maps_search)
maps_route_tool = FunctionTool(maps_route)
maps_search_along_route_tool = FunctionTool(maps_search_along_route)
