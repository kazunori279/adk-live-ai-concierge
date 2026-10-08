"""Google Search grounding as an ADK tool."""

import asyncio
import json
import re

from google.adk.tools import FunctionTool, ToolContext

from .. import screen
from ._client import authed_session, search_url
from ._lang import SCREEN_STYLE, answer_in, normalize, of_turn
from .image_search import image_search

MAX_SOURCES = 6
BRANCH_TIMEOUT = 10

NEWS_RE = re.compile(
    r"(?i)\b(?:news|headlines?|trending|trends?|top\s+stories|breaking(?:\s+news)?|current\s+events)\b|"
    r"(?:ニュース|ヘッドライン|トレンド|出来事|速報|話題)"
)


def _web_search_sync(query: str, lang: str) -> dict:
    instruction = " ".join(filter(None, [SCREEN_STYLE, answer_in(lang)]))
    payload = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": query}]}],
        "systemInstruction": {"parts": [{"text": instruction}]},
        "tools": [{"googleSearch": {}}],
    }).encode()

    resp = authed_session().post(
        search_url(), data=payload,
        headers={"Content-Type": "application/json"}, timeout=30,
    )
    resp.raise_for_status()

    candidate = resp.json().get("candidates", [{}])[0]
    gm = candidate.get("groundingMetadata", {})

    text = "".join(
        part["text"]
        for part in candidate.get("content", {}).get("parts", [])
        if "text" in part
    )

    sources = [
        {"title": chunk["web"].get("title", ""), "uri": chunk["web"].get("uri", "")}
        for chunk in gm.get("groundingChunks", [])[:MAX_SOURCES]
        if chunk.get("web")
    ]

    return {
        "query": query,
        "lang": normalize(lang),
        "summary": text,
        "search_queries": gm.get("webSearchQueries", []),
        "sources": sources,
    }


def is_news_query(query: str, search_queries: list[str] | None = None) -> bool:
    """Whether a query (or its search queries) expresses news/trending intent."""
    if query and NEWS_RE.search(query):
        return True
    if search_queries:
        for sq in search_queries:
            if sq and NEWS_RE.search(sq):
                return True
    return False


def _clean_headline(title: str) -> str:
    if not title:
        return ""
    cleaned = re.sub(r"^[【\[][^】\]]+[】\]]\s*", "", title).strip()
    parts = re.split(r"\s+[-|—–_]\s+", cleaned)
    if parts:
        if len(parts) > 1 and len(parts[0]) < 4 and len(parts[1]) >= 4:
            cleaned = parts[1].strip()
        else:
            cleaned = parts[0].strip()
    cleaned = re.split(r"[-|]\s*[A-Za-z0-9\u3000-\u9fff\s]+$", cleaned)[0].strip() or cleaned
    return cleaned


def _extract_summary_topics(summary: str) -> list[str]:
    topics = []
    for line in (summary or "").splitlines():
        m = re.search(r"^\s*[*•-]\s*(?:[^\w\s]\s*)?\*\*([^*]+)\*\*", line)
        if m:
            topic = m.group(1).strip()
            topic = re.sub(r"[:：].*$", "", topic).strip()
            if topic and len(topic) >= 2:
                topics.append(topic)
    return topics


def extract_topics(data: dict) -> list[str]:
    """Extract up to 3 distinct topic headlines/entities from grounding results."""
    topics: list[str] = []
    seen_lower: set[str] = set()

    def _add_topic(t: str) -> None:
        cand = t.strip()
        if not cand or len(cand) < 2:
            return
        low = cand.lower()
        if any(low in s or s in low for s in seen_lower):
            return
        seen_lower.add(low)
        topics.append(cand)

    for src in data.get("sources", []):
        title = src.get("title", "")
        cleaned = _clean_headline(title)
        if cleaned:
            _add_topic(cleaned)
        if len(topics) >= 3:
            return topics[:3]

    if len(topics) < 3:
        for st in _extract_summary_topics(data.get("summary", "")):
            _add_topic(st)
            if len(topics) >= 3:
                return topics[:3]

    if len(topics) < 3:
        for sq in data.get("search_queries", []):
            if sq and not re.match(r"(?i)^(?:news|today'?s\s+news|ニュース|最新ニュース)$", sq.strip()):
                _add_topic(sq)
                if len(topics) >= 3:
                    return topics[:3]

    return topics[:3]


async def _fetch_one_topic_image(topic: str, lang: str) -> dict | None:
    try:
        res = await asyncio.wait_for(image_search(topic, lang), timeout=BRANCH_TIMEOUT)
        if isinstance(res, dict):
            inner = res.get("image_search", res)
            images = inner.get("images", []) if isinstance(inner, dict) else []
            if images:
                img = images[0]
                thumb = img.get("thumbnail_uri") or img.get("url") or ""
                if thumb:
                    title = img.get("source_title") or img.get("title") or topic
                    source_uri = img.get("source_uri") or img.get("source") or ""
                    return {
                        "url": thumb,
                        "thumbnail_uri": thumb,
                        "title": title,
                        "source_title": title,
                        "source": source_uri,
                        "source_uri": source_uri,
                    }
    except Exception:
        pass
    return None


async def _fetch_topic_images(topics: list[str], lang: str) -> list[dict]:
    tasks = [_fetch_one_topic_image(t, lang) for t in topics[:3]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    images: list[dict] = []
    seen_urls: set[str] = set()
    for r in results:
        if isinstance(r, dict) and r.get("url") and r["url"] not in seen_urls:
            seen_urls.add(r["url"])
            images.append(r)
    return images


async def web_search(
    query: str, lang: str = "", tool_context: ToolContext = None,
):
    """Search the web with Google Search for up-to-date information.

    Use for news, weather, opening hours, prices, schedules, product details,
    or any fact that changes over time or that you are unsure about.

    Args:
        query: A natural-language search query.
        lang: BCP-47 tag of the language the person is speaking, e.g. `ja`,
            `en`. The summary is written in it and shown on screen.

    Returns:
        A dict mapping 'web_search' to the search result dict.
    """
    effective_lang = of_turn(lang, tool_context)
    data = await asyncio.to_thread(_web_search_sync, query, effective_lang)
    try:
        if is_news_query(query, data.get("search_queries")):
            topics = extract_topics(data)
            if topics:
                data["images"] = await _fetch_topic_images(topics[:3], effective_lang)
            else:
                data["images"] = []
        else:
            data["images"] = []
    except Exception:
        data["images"] = []

    session_id = screen.session_of(tool_context) if tool_context else ""
    turn_id = getattr(tool_context, "invocation_id", "") if tool_context else ""
    await screen.push(session_id, {
        "type": "card_section",
        "turn_id": turn_id,
        "section": "web_search",
        "data": data,
    })
    return {"web_search": data}


web_search_tool = FunctionTool(web_search)
