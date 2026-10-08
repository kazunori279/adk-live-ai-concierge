"""YouTube video search as an ADK tool.

Runs a video-biased Google Search grounding query and keeps the grounding
chunks that resolve to a YouTube video. ``enhancedContent.imageSearch`` is not
used here because it rarely surfaces YouTube results.

Grounding chunk URIs are Vertex AI redirects, so each candidate is resolved
with a HEAD request to recover the real video ID. The chunk titles are no use
either — they all read "youtube.com" — so the real ones come from oEmbed.

A grounding chunk is a source the model read while writing a paragraph, not a
video somebody chose. A channel page, a "10 things to do in Kyoto" compilation
and a blog with an embed all resolve to a watch URL, so "this is a YouTube
video" is not much of a test to have passed — which is how a place card ended
up showing a video of somewhere else. The title is the only evidence there is
about what a video is, so it is fetched for the whole pool and the choice is
made from it. See :func:`_relevant`.
"""

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor

import requests as http_requests
from google.adk.tools import FunctionTool, ToolContext

from .. import screen
from . import _match
from ._client import authed_session, search_url
from ._lang import normalize, of_turn, search_in

MAX_VIDEOS = 3
MAX_CANDIDATES = 8

YOUTUBE_SEARCH_DIRECTIVE = (
    "All Google Search queries you generate MUST target YouTube video watch URLs "
    "(`youtube.com/watch` or `youtu.be`). Include `YouTube` and `site:youtube.com` "
    "in your web search queries to find videos rather than text articles. "
    "Never drop `YouTube` or video intent from your search queries."
)

_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?.*v=|shorts/|embed/|live/|v/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


def _resolve_video_id(redirect_uri: str) -> str:
    """Follow a redirect URI and extract a YouTube video ID, or return ''."""
    try:
        resp = http_requests.head(redirect_uri, allow_redirects=True, timeout=5)
    except Exception:
        return ""
    match = _VIDEO_ID_RE.search(resp.url)
    return match.group(1) if match else ""


def _video_title(video_id: str) -> str | None:
    """A video's real title via oEmbed. `""` for no such video, None if unsure.

    The two are worth telling apart because :func:`_relevant` throws away a
    video with no title. oEmbed answers for anything embeddable, so a refusal
    means private, deleted or region-locked, and that is a video the card
    should not carry. A timeout or a 429 means nothing about the video —
    eight parallel unauthenticated requests to one endpoint is the shape that
    finds a rate limit — and reading it as a refusal empties the card over a
    bad thirty seconds on the network.

    A 200 whose JSON carries no `title` is the second kind and not the first.
    oEmbed always sends one, so a reply without it is a reply this does not
    understand, and "could not tell" is what that is.
    """
    try:
        resp = http_requests.get(
            "https://www.youtube.com/oembed",
            params={
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "format": "json",
            },
            timeout=5,
        )
    except Exception:
        return None
    if resp.status_code in (401, 403, 404):
        return ""
    if not resp.ok:
        return None
    try:
        return resp.json().get("title")
    except ValueError:
        return None


def _relevant(
    query: str, candidates: list[tuple[str, str | None]],
) -> list[tuple[str, str]]:
    """The videos whose titles say they are about `query`, best first.

    A title sharing terms with the query is the evidence, and more of them is
    better evidence, so the pool is ranked on it rather than merely sieved. One
    thing is thrown away outright: a video oEmbed says has no title, which is
    one that is private, deleted or region-locked.

    Everything else is a question of whether the zero means anything. It does
    not when the title is in a script the query could never have overlapped —
    the grounding call is told to search in the person's language, not the
    query's, so "how to descale a kettle" asked by a Japanese speaker comes
    back as three Japanese videos, every one of them right and not one sharing
    a character with the query. It does not when the query yielded no terms to
    match, which a one-character query does. And it does not when the title
    could not be fetched at all. All three keep their place and sort below
    anything that could be checked; only a title that had its chance and
    missed is dropped.

    "Below anything that could be checked" is two keys and not one. A title
    that was read and scored zero still beats one nobody could read, and the
    sort is stable, so without the second key the order they arrived in decides
    which of them takes the player — and `cards.js` labels a titleless video
    with its URL. Sorting on hits alone put a bare watch link in front of a
    good Japanese video.

    The pool being unreadable in its entirety is the case to be uneasy about:
    oEmbed rate-limits eight parallel unauthenticated requests happily enough,
    and every title coming back None means three videos on the card that
    nothing checked. That is the deliberate choice — an unverified video beats
    an empty card, where a *wrong* one does not — and the only place it shows
    is that the card's videos are suddenly untitled.

    An empty list is a fine answer. A place card with photos, a summary and a
    map but no video is a card; a place card with a video of somewhere else is
    worse than either, because it is the one thing on screen that looks like it
    was checked. It should mean the pool was bad, though, and not that the
    question was unanswerable.
    """
    wanted = _match.terms(query)
    ranked = []
    for video_id, title in candidates:
        if title == "":
            continue
        judged = bool(wanted) and title is not None
        hits = _match.score(title, wanted) if judged else 0
        if judged and not hits and _match.comparable(title, wanted):
            continue
        ranked.append((hits, video_id, title or ""))
    ranked.sort(key=lambda row: (-row[0], row[2] == ""))
    return [(video_id, title) for _, video_id, title in ranked[:MAX_VIDEOS]]


def _why(searched: int, found: int, watchable: int, unavailable: int,
         kept: int) -> str:
    """Which of the five empties this was, in words the model can repeat.

    An empty `videos` meant one thing in the docstring and four things in fact,
    and the docstring picked the one that is a statement about the world:
    *nothing came back that the title says is about the query*. #246 is what
    that costs. Asked for a video of ramen shops an hour before a demo, the
    grounding call came back in 1.5 s — under the 2.354 s floor of every call
    in that session known to have searched — having run no search at all, and
    the household was told there were no videos of a thing there are thousands
    of videos of. The model had nothing else to say, because this is all it is
    given.

    Ordered by how early the pipeline stopped, so the first true one is the
    furthest upstream and therefore the cause. Empty when there are videos:
    the model reads every field it is handed, and a reason beside a full list
    is a reason it may decide to mention.
    """
    if kept:
        return ""
    if not searched:
        return ("the search did not run at all — this is not an answer about "
                "what exists, and fewer words in the query often fixes it")
    if not found:
        return "the search ran and turned up no videos"
    if not watchable:
        return "what the search turned up was pages rather than videos"
    if unavailable == watchable:
        # `_relevant` drops a video oEmbed refused before it looks at any
        # title, so an all-refused pool empties on availability and never
        # reaches the filter. Saying "their titles were about something else"
        # of it is the same false confidence #246 is about, and worse than
        # useless: it invites a rephrase, and rephrasing cannot unprivate a
        # video.
        return ("the videos that came back are private, deleted, or not "
                "viewable from here")
    return "videos came back, and their titles were about something else"


def _format_search_prompt(query: str) -> str:
    """Format query with YouTube keyword and site:youtube.com constraint."""
    q = query.strip()
    q = re.sub(r"(?i)\s+youtube\s+video$", "", q).strip()
    prefix = "Search exclusively on YouTube for videos"
    if q.startswith(prefix):
        q = q[len(prefix):].strip()
        if q.startswith("about:"):
            q = q[len("about:"):].strip()
    q = re.sub(r"(?i)\s*site:youtube\.com\s*", " ", q).strip()
    q = re.sub(r"(?i)\s+youtube$", "", q).strip()
    if q:
        return f"{q} YouTube site:youtube.com"
    return "YouTube site:youtube.com"


def _query_grounding(search_prompt: str, lang: str) -> tuple[list[str], list[str]]:
    """POST to search grounding endpoint and return (search_queries, candidate_uris)."""
    payload: dict = {
        "contents": [
            {"role": "user", "parts": [{"text": search_prompt}]}
        ],
        "tools": [{"googleSearch": {}}],
    }
    directive = " ".join(filter(None, [search_in(lang), YOUTUBE_SEARCH_DIRECTIVE]))
    if directive:
        payload["systemInstruction"] = {"parts": [{"text": directive}]}
    payload_bytes = json.dumps(payload).encode()

    resp = authed_session().post(
        search_url(), data=payload_bytes,
        headers={"Content-Type": "application/json"}, timeout=30,
    )
    resp.raise_for_status()

    candidate = resp.json().get("candidates", [{}])[0]
    gm = candidate.get("groundingMetadata", {})
    queries = gm.get("webSearchQueries") or []
    uris = [
        chunk["web"]["uri"]
        for chunk in gm.get("groundingChunks", [])
        if chunk.get("web", {}).get("uri")
    ][:MAX_CANDIDATES]
    return queries, uris


def _youtube_search_sync(query: str, lang: str) -> dict:
    # A video's title is the only thing the card says about it, and it comes
    # from oEmbed rather than from the model, so the language has to change
    # which videos come back rather than how they are described.
    # Enforce site:youtube.com constraint to target watch URLs and avoid text aggregators.
    search_prompt = _format_search_prompt(query)
    queries, uris = _query_grounding(search_prompt, lang)

    # Two round trips per video, so fan each stage out across the candidates.
    # Both stages run over the whole pool: stopping at the first three that
    # resolve leaves nothing to choose between, and the choosing is the point.
    # It is a wider round rather than an extra one.
    with ThreadPoolExecutor(max_workers=MAX_CANDIDATES) as pool:
        video_ids = list(dict.fromkeys(
            video_id for video_id in pool.map(_resolve_video_id, uris) if video_id
        ))

    # Fallback retry if grounding dropped YouTube / site:youtube.com constraint resulting in 0 video IDs
    if not video_ids and queries:
        video_kw = "YouTube 動画" if normalize(lang) == "ja" else "YouTube video"
        retry_prompt = f"{query.strip()} {video_kw} site:youtube.com"
        retry_queries, retry_uris = _query_grounding(retry_prompt, lang)
        if retry_uris:
            with ThreadPoolExecutor(max_workers=MAX_CANDIDATES) as pool:
                retry_video_ids = list(dict.fromkeys(
                    video_id for video_id in pool.map(_resolve_video_id, retry_uris) if video_id
                ))
            if retry_video_ids:
                video_ids = retry_video_ids
                uris = retry_uris
                queries = retry_queries

    with ThreadPoolExecutor(max_workers=MAX_CANDIDATES) as pool:
        titles = list(pool.map(_video_title, video_ids))

    # oEmbed says "" for a 401/403/404, which is private, deleted or
    # region-locked, and `_relevant` throws those away without reading a
    # title. Counted here so `_why` can tell that empty from the filter's.
    unavailable = sum(1 for title in titles if title == "")

    videos = [
        {
            "video_id": video_id,
            "title": title,
            "uri": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail_uri": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        }
        for video_id, title in _relevant(query, list(zip(video_ids, titles)))
    ]

    # Three counts and a reason, and the counts are what `main.py:504-507`
    # turns into the log line: a list is logged as its length and an int
    # verbatim, so `videos 0, search_queries 0, found 0, watchable 0` reads
    # off the journal as the whole pipeline. Diagnosing #246 without them took
    # stopwatch forensics against unrelated `web_search` calls in the same
    # file. They are counts and not content, so they add nothing to the
    # surface #226, #231, #234 and #248 are about.
    return {
        "query": query,
        "lang": normalize(lang),
        "videos": videos,
        "search_queries": queries,
        "found": len(uris),
        "watchable": len(video_ids),
        "unavailable": unavailable,
        "why": _why(len(queries), len(uris), len(video_ids), unavailable,
                    len(videos)),
    }


async def youtube_search(
    query: str, lang: str = "", tool_context: ToolContext = None,
) -> dict:
    """Find YouTube videos about a topic.

    Use when the user asks to watch something, or when a video answers better
    than text — recipes, repairs, how-tos, music, trailers. The videos are
    embedded on screen and playable, so say what you found instead of reading
    out links.

    Args:
        query: What to find videos about, e.g. "how to descale a kettle".
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. A video they cannot follow is no use, so this picks which
            ones come back.

    Returns:
        A dict mapping 'youtube_search' to the search result dict.
    """
    effective_lang = of_turn(lang, tool_context)
    data = await asyncio.to_thread(_youtube_search_sync, query, effective_lang)
    session_id = screen.session_of(tool_context) if tool_context else ""
    turn_id = getattr(tool_context, "invocation_id", "") if tool_context else ""
    await screen.push(session_id, {
        "type": "card_section",
        "turn_id": turn_id,
        "section": "youtube_search",
        "data": data,
    })
    return {"youtube_search": data}


youtube_search_tool = FunctionTool(youtube_search)
