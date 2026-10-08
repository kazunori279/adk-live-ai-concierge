"""One place, four groundings, one card.

Asking about somewhere — "what is Kinkaku-ji", "tell me about that ramen place
in Nakameguro" — wants more than any single grounding gives back: a paragraph
of what it is, a few photos, a video to watch, and a pin on a map. The four
tools can each be called on their own, and the agent does that when a question
only needs one of them. For a place it rarely picks more than two, and when it
does pick four the screen gets four cards stacked down an 800 px column.

So this tool fans out to all four at once and merges what comes back. The
frontend draws it as a single card headed by the place name.
"""

import asyncio
import inspect
import logging
import re

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from ._lang import normalize, of_turn
from .image_search import image_search
from .maps import maps_search
from .web_search import web_search
from .youtube_search import youtube_search

logger = logging.getLogger(__name__)

# Trimmed from what each tool returns alone: this is one card on a narrow
# screen, not four. Three photos rather than four because the grid is three
# wide on the panel, so three is one full row and four is a row and a stub.
MAX_IMAGES = 3
MAX_VIDEOS = 2
MAX_PLACES = 3
MAX_SOURCES = 4

# The four run in parallel, so this is close to the whole cost of the tool. A
# branch still quiet by now has already cost the turn its silence; the card is
# worth drawing from whatever did arrive.
BRANCH_TIMEOUT = 15


async def _drain_gen(gen, name: str) -> dict:
    last = {}
    async for item in gen:
        if isinstance(item, dict):
            last = item.get(name, item)
        else:
            last = item
    return last


async def _branch(name: str, coro, fallback: dict) -> dict:
    """Await one grounding call, and shrug off a slow or broken one.

    A place card with three sources beats no card at all, and one branch
    failing says nothing about the other three — image search in particular
    comes back empty for anywhere obscure.
    """
    try:
        if inspect.isasyncgen(coro):
            return await asyncio.wait_for(_drain_gen(coro, name), BRANCH_TIMEOUT)
        res = await asyncio.wait_for(coro, BRANCH_TIMEOUT)
        if isinstance(res, dict):
            return res.get(name, res)
        return res
    except TimeoutError:
        logger.warning("place_info: %s timed out after %ss", name, BRANCH_TIMEOUT)
    except Exception:
        logger.exception("place_info: %s failed", name)
    return fallback


_LEADING_FILLERS_RE = re.compile(
    r"^(?:"
    r"(?:can\s+you\s+|could\s+you\s+|please\s+)?"
    r"(?:tell\s+me\s+about|tell\s+me|what\s+is|what's|what\s+are|where\s+is|where's|where\s+are|show\s+me|about)"
    r"|(?:(?:について)?教えて(?:ください)?|とは|ってどこ|はどこ(?:ですか)?)"
    r")\s*[:：\s]*"
    r"(?:a\s+|an\s+|the\s+)?",
    re.IGNORECASE,
)

_TRAILING_CLAUSE_RE = re.compile(
    r"(?:"
    r"[\s,、]+(?:"
    r"and\s+(?:why|what|how|where)\b.*|"
    r"why\s+(?:is\s+it|it\s+is)\b.*|"
    r"which\s+is\b.*"
    r")|"
    r"[\s,、]*(?:"
    r"(?:について)?教えて(?:ください)?|"
    r"とは|"
    r"って何|"
    r"ってどこ|"
    r"はどこ(?:ですか)?"
    r")"
    r")$",
    re.IGNORECASE,
)

_TRAILING_PUNCT_RE = re.compile(r"[?？!！.。,、\s]+$")
_LEADING_PUNCT_RE = re.compile(r"^[:：,、\s]+")


def _sanitize_place(place: str) -> str:
    """Sanitize place name for Maps search by stripping conversational filler and questions."""
    p = place.strip()
    if not p:
        return ""
    p = _TRAILING_PUNCT_RE.sub("", p)
    p = _LEADING_FILLERS_RE.sub("", p)
    p = _LEADING_PUNCT_RE.sub("", p)
    p = _TRAILING_CLAUSE_RE.sub("", p)
    p = _TRAILING_PUNCT_RE.sub("", p).strip()
    return p if p else place.strip()


async def place_info(
    place: str, question: str = "", near: str = "", lang: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Everything about one place at once: facts, photos, a video and a map.

    Use this whenever the person asks about a particular place — a landmark,
    restaurant, shop, park, museum, station or town — rather than calling
    web_search, image_search, youtube_search and maps_search separately. It
    runs all four in parallel and the screen shows one card.

    Not for a search with several answers ("ramen shops near here") — that is
    maps_search. This is for somewhere already named.

    Args:
        place: The name of the place, as specific as you can make it, e.g.
            "Kinkaku-ji, Kyoto" rather than "the golden temple".
        question: What the person actually wants to know, in their own
            language, e.g. "何時まで開いてる?". Leave empty for an overview.
        near: A city or address to disambiguate a common name, or `here` for
            around the terminal. Leave it empty for anywhere — which is what
            a landmark wants, since confining 富士山 to this neighbourhood
            finds a 富士塚 in a local park.
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. This is the whole card's language — summary, photo captions
            and video titles.

    Returns:
        A dict with a Markdown summary, photos, videos, the matching places
        and the web sources behind the summary.
    """
    # Floored here and passed down, because three of the four branches are
    # called positionally and carry no `tool_context`: the panel's setting is
    # invisible from inside them. `maps_search` is handed the context anyway
    # for `near`'s sake and would floor this itself, but it is given the
    # floored value like the others rather than being the one branch whose
    # language comes from somewhere else.
    lang = of_turn(lang, tool_context)
    # The question on its own is a pronoun away from useless — "when is it
    # open?" searched literally comes back asking what "it" is — so the place
    # name leads and the question qualifies it.
    query = f"{place}: {question}" if question else place
    maps_place = _sanitize_place(place)

    web, images, videos, maps = await asyncio.gather(
        _branch("web_search", web_search(query, lang), {}),
        _branch("image_search", image_search(place, lang), {}),
        _branch("youtube_search", youtube_search(place, lang), {}),
        # The only branch that takes `tool_context`, and it takes it so that
        # `near` can be passed through untouched. Resolving "here" out here
        # instead would put a location filter on every call that omitted one,
        # which is #196: three states in the argument, two in the code, and
        # the two the wrong way round.
        _branch("maps_search", maps_search(maps_place, near, lang, tool_context),
                {}),
    )

    return {
        "place": place,
        "lang": normalize(lang),
        # The web summary answers the question; the Maps one lists opening
        # hours and ratings. Two paragraphs saying overlapping things is one
        # too many to read out, so Maps only speaks when Search did not.
        "summary": web.get("summary") or maps.get("summary", ""),
        "search_queries": web.get("search_queries", []),
        "sources": web.get("sources", [])[:MAX_SOURCES],
        "images": images.get("images", [])[:MAX_IMAGES],
        "videos": videos.get("videos", [])[:MAX_VIDEOS],
        "places": maps.get("places", [])[:MAX_PLACES],
        "near": maps.get("near", ""),
    }


place_info_tool = FunctionTool(place_info)
