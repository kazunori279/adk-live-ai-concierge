"""Google image search as an ADK tool.

Goes through Google Search grounding's ``enhancedContent.imageSearch``, which
returns thumbnails plus the page each image came from.
"""

import asyncio
import json

from google.adk.tools import FunctionTool, ToolContext

from .. import screen
from ._client import authed_session, search_url
from ._lang import normalize, of_turn, search_in

MAX_IMAGES = 6


def _why(searched: int, attached: int, kept: int) -> str:
    """Which of the three empties this was, in words the model can repeat.

    The same distinction :func:`youtube_search._why` draws and for the same
    reason: an empty list is a statement about what exists only when a search
    happened. #246's `image_search` call took 2.611 s — inside the band of the
    grounded calls in that session rather than under it — so that one had
    searched and come back with no `attachments`, which is a different failure
    from its `youtube_search` neighbours and looked identical on screen.

    Three stages rather than four because there is no resolving round here:
    the thumbnails arrive with the response.
    """
    if kept:
        return ""
    if not searched:
        return ("the search did not run at all — this is not an answer about "
                "what exists, and fewer words in the query often fixes it")
    if not attached:
        return "the search ran and came back with no photos"
    return "photos came back, and none of them carried a thumbnail"


def _image_search_sync(query: str, lang: str) -> dict:
    # Each tile is captioned with the title of the page the photo came from, so
    # the language decides half of what the card says even though nothing here
    # is written by the model.
    payload: dict = {
        "contents": [{"role": "user", "parts": [{"text": f"{query} photos"}]}],
        "tools": [{"googleSearch": {"enhancedContent": {"imageSearch": {}}}}],
    }
    directive = search_in(lang)
    if directive:
        payload["systemInstruction"] = {"parts": [{"text": directive}]}
    payload = json.dumps(payload).encode()

    resp = authed_session().post(
        search_url(), data=payload,
        headers={"Content-Type": "application/json"}, timeout=30,
    )
    resp.raise_for_status()

    candidate = resp.json().get("candidates", [{}])[0]
    gm = candidate.get("groundingMetadata", {})
    # Whether a search happened, as against whether it found anything. See
    # `web_search.py:47`, which has returned this since it was written, and
    # #246, where its absence here cost an hour of stopwatch forensics.
    queries = gm.get("webSearchQueries") or []
    attachments = (gm.get("attachments") or [])[:MAX_IMAGES]

    images = []
    for attachment in attachments:
        img = attachment.get("image")
        if not img:
            continue
        thumbnail = img.get("thumbnail", {}).get("uri", "")
        if not thumbnail:
            continue
        images.append({
            "source_uri": img.get("source", {}).get("uri", ""),
            "source_title": img.get("source", {}).get("title", ""),
            "thumbnail_uri": thumbnail,
        })

    # Counts, not content: `main.py:504-507` logs a list as its length and an
    # int verbatim, so the journal gets the whole pipeline on one line.
    return {
        "query": query,
        "lang": normalize(lang),
        "images": images,
        "search_queries": queries,
        "attached": len(attachments),
        "why": _why(len(queries), len(attachments), len(images)),
    }


async def image_search(
    query: str, lang: str = "", tool_context: ToolContext = None,
):
    """Find photos of a place, object, person, dish, or product.

    Use whenever the user asks to see something — "show me", "what does it
    look like", "any pictures of". The images are shown on screen, so describe
    them briefly rather than listing URLs out loud.

    Args:
        query: What to find pictures of, e.g. "Kenrokuen garden in autumn".
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. Each photo is captioned with the page it came from, so this
            decides what the captions say.

    Returns:
        A dict mapping 'image_search' to the image search result dict.
    """
    effective_lang = of_turn(lang, tool_context)
    data = await asyncio.to_thread(_image_search_sync, query, effective_lang)
    session_id = screen.session_of(tool_context) if tool_context else ""
    turn_id = getattr(tool_context, "invocation_id", "") if tool_context else ""
    await screen.push(session_id, {
        "type": "card_section",
        "turn_id": turn_id,
        "section": "image_search",
        "data": data,
    })
    return {"image_search": data}


image_search_tool = FunctionTool(image_search)
