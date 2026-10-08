"""The language of the current turn, threaded through the grounding calls.

A tool has no view of the conversation. Each one is a fresh sidecar
``generateContent`` that sees only its own arguments, so a question asked in
Japanese reaches the tool as an English-shaped query and comes back as an
English card next to a Japanese spoken answer. Every tool therefore takes a
BCP-47 ``lang``, and this module turns it into the sentence that makes a
sidecar model answer in it. ``of_turn`` decides which tag that is — the
model's when it passed one, the panel's setting when it did not — and every
tool entry point calls it before anything else reads the argument.

The tag has to travel in the prompt because none of these APIs take a language
of their own: the ``googleSearch`` tool has no ``hl``/``lr``, Maps grounding has
no ``languageCode``, and the videos come out of search grounding rather than the
YouTube Data API, so there is no ``relevanceLanguage`` either. What the model
writes follows the tag; what it quotes does not always — a Maps grounding chunk
carries whatever display name Places returned.
"""

import re

# "zh-Hant-HK" with room to spare. A tag is a tag, not a sentence.
MAX_TAG = 20

_TAG = re.compile(r"[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*")


def normalize(lang: str) -> str:
    """A BCP-47 tag, or "" if it is not one.

    The tag is pasted into a system instruction, and the model chooses it, so
    it is checked rather than trusted: "ja" is a language, and a paragraph of
    instructions dressed up as one is not.
    """
    tag = (lang or "").strip()
    return tag if len(tag) <= MAX_TAG and _TAG.fullmatch(tag) else ""


def panel_lang(context) -> str:
    """The language the panel is set to, or "" if nothing said.

    It reaches the server on the socket handshake and lives in the ADK session
    state, which is how the terminal's own location travels too — the read is
    deliberately the same shape as ``maps.here``. A tool called from a check
    script has no context and no panel, and this is where that comes out as ""
    rather than as an attribute error.

    Public because ``plan`` seeds it into the session it mints for the planner:
    the planner shares the live agent's tool objects, so the state those tools
    read down there is not the socket's.
    """
    state = getattr(context, "state", None) or {}
    return normalize(state.get("panel_lang", ""))


def of_turn(lang: str, context=None) -> str:
    """The tag a tool answers in: the one the model passed, floored at the
    panel's setting.

    Every tool takes a ``lang`` and ``agent.py`` says "Always pass it", which
    was the whole of the enforcement — ``maps_search(query, lang="")`` is a
    well-formed call, and the fallback it fell through to asked the sidecar to
    match the language of a query the model had just written in English. So a
    Japanese terminal got English cards. The setting was sitting on
    ``<html lang>`` the whole time and reached exactly one route (#187).

    The precedence is this one expression, and it is a decision rather than an
    accident: the person speaking beats the setting, so someone who asks in
    English on a Japanese panel gets an English card. The setting is the floor
    under a model that said nothing, not an override of one that did.
    """
    return normalize(lang) or panel_lang(context)


# What to say when neither the agent nor the panel named a language. It used to say nothing at all,
# on the reasoning that a model handed a Japanese query would answer in
# Japanese. It does not reliably: the query arrives wrapped in an English
# sentence the tool built, and the sidecar follows the wrapper.
_MATCH_QUERY = (
    "Answer in the language the query itself is written in — the query, not "
    "the English sentence around it. Do not translate it into English first."
)


def answer_in(lang: str) -> str:
    """A directive for a sidecar model.

    English is not a safe default for a terminal that sits in a Japanese
    kitchen, so an unnamed language means "match the query" rather than
    "whatever you like".
    """
    tag = normalize(lang)
    if not tag:
        return _MATCH_QUERY
    return (
        f"Write your answer in the language with BCP-47 tag `{tag}`, whatever "
        "language the query itself is in. Translate place names, titles and "
        "quoted snippets into it where a natural translation exists."
    )


def search_in(lang: str) -> str:
    """The same, for a tool whose card is other people's text rather than the
    model's own.

    An image tile is captioned with the title of the page it came from and a
    video with its own title, so what makes those cards Japanese is not how the
    model writes but which results it goes looking for.
    """
    tag = normalize(lang)
    if not tag:
        return (
            "Run your searches in the language the query itself is written "
            "in, and prefer results published in it."
        )
    return (
        f"The person asking speaks the language with BCP-47 tag `{tag}`. "
        "Run your searches in that language and prefer results published in "
        "it, unless doing so would find nothing."
    )


# How a sidecar model should shape prose that lands in a card. It lives beside
# the language directives because it travels the same way: five prompts write
# into the same ``.md`` block and the person three metres from the panel cannot
# tell which of them answered, so the shape has to be one string rather than
# five paragraphs that drifted.
#
# The Markdown half is only a promise the page already keeps —
# ``static/js/markdown.js`` renders bold, italic, code, links, bulleted and
# numbered lists and a single heading rank, and nothing else. The clauses below
# name that subset rather than Markdown in general, because a table or a
# blockquote reaches the card as literal pipes and angle brackets.
#
# The emoji half is written as a test — "the line reads the same with every
# emoji deleted" — rather than as taste, because the glyph is thrown away twice
# for real. Once by the panel: nothing in this repo installs a colour emoji
# font, ``markEmoji`` (``static/js/app.js``) detects that and sets ``no-emoji``,
# and the only fallback behind it is the calendar's Material Symbol
# (``style.css``), which no prose emoji can reach. Once by the voice: the Live
# model paraphrases a tool's answer out loud, so anything load-bearing that is
# spelled with a picture is simply not said.
SCREEN_STYLE = (
    "Your answer is shown on a small screen and rendered as Markdown. Lead "
    "with the answer in one plain sentence, then the detail as a bulleted "
    "list (`* item`) of at most six items, each opening with its subject in "
    "bold (`**name**`). Only bold, italic, `code`, links, bullets and "
    "numbered lists are rendered: no tables, no blockquotes, no raw HTML. And "
    "no headings — one would draw, but as a second title over the card's own. "
    "Begin each bullet with one emoji that fits what the bullet is about, as the "
    "very first character of the line after the bullet marker (`* 🍜 **name**`). "
    "At most one emoji per line, never "
    "inside a sentence and never standing in for a word — the line must read "
    "exactly the same when every emoji is deleted from it. No emoji in the "
    "opening sentence, which is the one that gets read aloud."
)
