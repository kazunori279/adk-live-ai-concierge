"""Deciding whether a piece of text is about what was asked for.

Two callers, both holding a short query and some text that came from somewhere
else: :func:`gws_cache.search_cache` ranks cached documents against a question,
and :mod:`youtube_search` decides whether a video title is about the place on
the card. Neither wants a search engine — they want to know whether these two
strings are talking about the same thing, cheaply and without a round trip.

`search_cache` weighs a hit in a title differently from one in the body, so it
counts with :func:`terms` and does its own arithmetic; :func:`score` is the
plain count, for the caller that wants one number.

`tools/match_check.py` pins the cases below.
"""

import functools
import re
import unicodedata

# A word, or a run of anything that is not one. `[^\W\dA-Za-z_]` is "a letter
# that is not a Latin one", which is how a Japanese run gets picked out without
# naming a codepoint range.
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]+|[^\W\dA-Za-z_]+")

# Kanji and both kana are one writing system for the purpose below, whatever
# Unicode calls them: 金閣寺 and サンマルクカフェ are the same language and a
# title mixing them is comparable to a query in either. Matched anywhere in the
# character's name rather than at the front of it, because ー is a
# KATAKANA-HIRAGANA PROLONGED SOUND MARK and ｱ is a HALFWIDTH KATAKANA LETTER,
# and both are as Japanese as the kana beside them. `CJK` also covers Chinese,
# so a Chinese title and a Japanese query read as comparable here — wrong, and
# rarer than the case this exists to get right.
_JAPANESE = ("HIRAGANA", "KATAKANA", "CJK")

# What a video title carries that is not its subject, and what makes reading
# its scripts off the raw string wrong: `【4K】六甲山カンツリーハウス紹介` is a
# Japanese title, and the one `K` in it is enough to call it English if all you
# ask is whether a Latin letter is in there. Bracketed tags are the idiom for
# that, and beside them the handful of words that tag a video without brackets.
#
# Only 【】 and [], and only matched: parentheses hold content — `Kinkaku-ji
# (Golden Pavilion)` — and `【4K]` closing with the wrong one is a typo, not a
# licence to eat the rest of the line. Both leans are towards stripping less.
#
# Stripping more is the safe direction anyway, but only because of how
# :func:`written_in` uses the result: it counts in the stripped title and
# divides by the whole one, so taking text away can lower a script's share and
# can never raise it. Read the share off the stripped title alone and the
# direction reverses, because the tags have their own alphabet too — strip
# `【京都観光おすすめスポット完全ガイド決定版】` off `…Osaka 大阪グルメ` and the
# one Latin word left is suddenly a fifth of what remains.
#
# That "can never raise it" is a property of the pattern and not a wish, and it
# is the tag rule that has to be written carefully to keep it. `_units` counts
# a Latin run as one term however long it is, so a strip that lands inside a
# run *splits* it and hands Latin a term it did not have: with `-` and `'`
# treated as ordinary boundaries, `Rokko-4K-Hike` came out as `Rokko- -Hike`,
# one term turned into two, and `六甲山ハイキング Rokko-4K-Hike` went from an
# eighth Latin to a quarter and got judged by an English query.
#
# So the tag has to have a whole `_RUN` to itself — but only where a run is
# what is on the other side of the hyphen. Refusing every `-` outright is too
# blunt: `金閣寺-vlog` and `六甲山-4K` are ordinary Japanese titles, nothing
# there is a Latin run to split, and leaving the tag in put Latin over the bar
# and threw the video away. Hence the pair of lookbehinds. The first rejects a
# Latin character touching the tag; the second rejects a `-` or a `'` only when
# a Latin character is on the far side of it, which is what makes it part of a
# word rather than punctuation. The lookaheads say the same thing forwards.
# Both are fixed width, which Python's `re` requires of a lookbehind.
#
# The bracket rule needs none of this and is safe by construction — no run
# spans a bracket, since neither `【` nor `[` is a character `_RUN` will cross.
#
# The boundary is a character class rather than `\b`, because Python's `\w`
# includes kanji: there is no word boundary in `六甲山vlog`, which is how
# Japanese titles are actually written, and `\b` left the commonest spelling of
# the case this list exists for untouched.
#
# `hd`, `mv` and `shorts` are also real words, so `HD Radio explained` loses its
# `HD`. Harmless: the strip only feeds the share, never the matching, and the
# words it eats are Latin ones in what is then still a Latin title.
_TAGS = r"4k|8k|hd|fhd|uhd|mv|pv|vlog|shorts|part\s*\d+|ep\s*\d+"
_DECORATION = re.compile(
    r"【[^】]*】|\[[^\]]*\]"
    rf"|(?<![A-Za-z0-9])(?<![A-Za-z0-9]['-])(?:{_TAGS})"
    r"(?![A-Za-z0-9])(?!['-][A-Za-z0-9])",
    re.IGNORECASE,
)

# ` / ` and its friends, where a channel writes its own name beside the title.
# Not part of the strip above, because the rule cannot tell a channel from a
# title and `チャンネル名 / タイトル` is as ordinary a shape as the other order:
# cutting at it takes the subject half the time. So neither side is cut. Both
# are read, and :func:`written_in` asks its question of each — see
# :func:`_readings`.
#
# The ASCII forms need a space on both sides, because a bare `/` is ordinary
# inside text — `and/or`, `24/7`, `w/` — and cutting there would invent a
# reading. The full-width forms do not: `｜` is a separator and nothing else,
# and `Japan Travel Guide｜金閣寺` with no spaces round it is the ordinary
# Japanese spelling, not a rare one.
#
# `／` is spelled unspaced just as often and needs the same licence, with one
# exception: it also writes a date. A date on its own is harmless — every side
# of `2024／10／01` comes out with no terms in it and :func:`_readings` drops
# them all — but a date with text on either side is not, because the outer
# components glue onto that text. `東京スカイツリー 2026／01／01 Tokyo Skytree`
# split into `東京スカイツリー 2026` and `01 Tokyo Skytree`, one reading per
# script, so `written_in` intersected to nothing, `comparable` was False, and
# an unrelated video reached the card as a title nobody was entitled to judge.
#
# Hence the digit lookaround. A `／` with a digit on each side is a date or a
# fraction and never a channel name, so it is not a separator; one with a digit
# on only one side still is, which leaves `第1回／京都の紅葉ライブ` splitting the
# way it reads.
#
# Two assertions per side, because a date is written `2024 ／ 10` as well as
# `2024／10` and the `\s*` around the group has eaten the space by the time a
# lookaround could see it. Python takes no variable-width lookbehind, so the
# one-space spelling needs its own fixed-width assertion beside the zero-space
# one. That space is `\s`, not a literal `" "` — one character wide either way,
# so it still fits in a lookbehind, and it has to be `\s` because the outer
# `\s*` already eats U+3000, NBSP and tab. `2026　／　01` with the full-width
# space is an ordinary Japanese title, and a literal `" "` would let exactly
# that one split.
#
# Two spaces and more are left uncovered on purpose: each costs another
# assertion and `2024  ／  10` is not a spelling titles use. That carve-out
# survives `\s` untouched, because two characters behind the slash are then
# both spaces and neither assertion looks at a pair.
#
# One shape in this class is still open. `六甲山ライブ 4K／8K Rokko hi-res` comes
# through :func:`plain` as `六甲山ライブ  ／  Rokko hi-res` — the tag strip runs
# first and takes the digits with it — so there is nothing left here to protect.
# Splitting the raw title and stripping each side afterwards does not fix it
# either: on the raw title that slash has `K` on its left, not a digit. Widening
# the lookaround to the tag letters would be a much broader claim than "digit",
# and the failure direction is a video kept rather than one thrown away.
_SEPARATOR = re.compile(
    r"\s[/|]\s|\s*(?:｜|(?<!\d)(?<!\d\s)／|／(?!\d)(?!\s\d))\s*")

# How much of a title a writing system has to carry before the title counts as
# written in it. Stripping the decoration is not enough on its own, because
# what is left still has proper nouns in it and they keep their own alphabet:
# `沸騰させるだけ！電気ケトル（T-fal）の掃除方法` is a Japanese video about
# descaling a kettle, and `T-fal` is one Latin word among fourteen Japanese
# terms. Ask only whether Latin is present and an English query is entitled to
# judge that title, find none of its words in it, and throw the video away —
# which is the empty-card failure this file exists to avoid, seen in the field
# rather than imagined.
#
# A quarter, and the test is `>=`, so a script carrying a quarter of a title
# owns it and two scripts can both own the same one — `Kobe Rokkosan Pasture
# 神戸` is three Latin terms and one Japanese, exactly on the line, and comes
# out written in both. It is a
# threshold and there is no principled value for it; what makes it defensible is
# what it is applied to, and that is neither the raw string — which is what let
# `【4K】` decide a title's language — nor the query, which counts presence
# instead for the reason :func:`comparable` gives.
MIN_SHARE = 0.25


def terms(query: str) -> list[str]:
    """Search terms out of a question.

    Latin words are split on spaces the usual way. A run of Japanese has no
    spaces to split on, so it contributes its two-character sequences instead —
    crude, and enough to match 旅程 inside 米国旅行の旅程. Crude in the other
    direction too: the sequences cross word boundaries, so 東京駅 contributes
    東京 and a video about 東京グルメ scores a point.

    A word with a hyphen or an apostrophe in it also contributes the bare form,
    because "Kinkaku-ji" and "Kinkakuji" are one word to everybody except a
    substring test, and place names are where that turns up. So does a
    possessive minus its ending: a question about "Kyoto's temples" is a
    question about Kyoto, and nothing titled "Kyoto Temple Tour" contains
    "kyoto's".
    """
    out = []
    for run in _RUN.findall(query.lower()):
        if run.isascii():
            out.append(run)
            for bare in (run.replace("-", "").replace("'", ""),
                         run.removesuffix("'s")):
                if bare != run and len(bare) > 1:
                    out.append(bare)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    # Deduplicated: a term said twice is not twice the evidence, and
    # :func:`score` counts terms rather than occurrences.
    return list(dict.fromkeys(out))


def score(text: str, wanted: list[str]) -> int:
    """How many of `wanted` appear anywhere in `text`.

    Substring rather than whole-word, which is what makes the two-character
    sequences above work and also catches "kyoto" inside "kyoto's".

    The text is tried with its hyphens and apostrophes taken out as well, so
    that the bare forms :func:`terms` adds are matched from both directions —
    "Kinkakuji" asked of a title reading "Kinkaku-ji Temple" is the same
    question as the other way round.
    """
    low = text.lower()
    bare = low.replace("-", "").replace("'", "")
    return sum(term in low or term in bare for term in wanted)


def plain(title: str) -> str:
    """A title with the decoration taken off. See :data:`_DECORATION`.

    For reading the scripts off, not for showing anybody: the card still
    displays whatever oEmbed said.
    """
    return _DECORATION.sub(" ", title)


def _bucket(char: str) -> str | None:
    """Which writing system `char` belongs to, coarsely. See :data:`_JAPANESE`."""
    name = unicodedata.name(char, "")
    if not name:
        return None
    for prefix in ("HALFWIDTH ", "FULLWIDTH "):
        name = name.removeprefix(prefix)
    return ("JAPANESE" if any(word in name for word in _JAPANESE)
            else name.split(" ", 1)[0])


# Cached because `comparable` asks the same question once per candidate, and
# titles repeat across a card's four groundings.
@functools.lru_cache(maxsize=512)
def _units(text: str) -> dict[str, int]:
    """How many search terms each writing system could contribute to `text`.

    Terms and not letters, because a term is what :func:`score` matches with
    and so the only unit in which the two sides are the same size. A Latin word
    is one term however long it is; a run of Japanese is one per pair of
    characters, which is what :func:`terms` cuts it into. Counting letters made
    `Rokko Kobe Japan Travel` — four words, one of them the country — outweigh
    十九字の日本語, and the title went to an English query that could not read
    it.

    The unit costs something in the other direction, and the price is real
    rather than theoretical: N Latin words plus one two-character place name
    keeps its Japanese only while N is 3 or less, and at four the title is
    written in Latin alone and a Japanese query is not allowed to look at it.
    Counting letters keeps those. Neither unit keeps both them and the
    channel-name case above; this is the side the field pool argued for.

    `Kobe Rokkosan Pasture 神戸` sits on the N=3 edge, so it is written in
    both — and `tools/match_check.py` still drops it against 六甲山の牧場,
    for the opposite reason. Japanese being in the set is what makes the query
    entitled to judge it, and the two share no term. That is the filter
    working, not the unit costing.

    A run with no letter in it is a number or a symbol, not a writing system,
    and a lone kanji is worth nothing because a single character yields no pair
    — the same arithmetic :func:`comparable` relies on for the query side. Worth
    nothing means absent, not present with a zero beside it: a bucket left in
    the dict at zero is a script `scripts` reports and `written_in` waves
    through, since every share is at least zero times :data:`MIN_SHARE`.
    """
    out: dict[str, int] = {}
    for run in _RUN.findall(text.lower()):
        letter = next((c for c in run if c.isalpha()), None)
        bucket = _bucket(letter) if letter else None
        if bucket is None:
            continue
        out[bucket] = out.get(bucket, 0) + (1 if run.isascii() else len(run) - 1)
    return {bucket: count for bucket, count in out.items() if count}


def scripts(text: str) -> frozenset[str]:
    """Every writing system that appears in `text` at all.

    One term is a term. For the query side of :func:`comparable`, where a
    single term is a real chance to match and there is no decoration to be
    fooled by.
    """
    return frozenset(_units(text))


def _readings(title: str) -> list[str]:
    """The stripped title, once per side of it that could be the subject.

    One reading for a title with no ` / ` in it, one per side otherwise. See
    :data:`_SEPARATOR`: the rule cannot tell which side is the channel, so
    neither is cut and :func:`written_in` requires a script to own the title
    under every answer.

    Every separator, not the first. `【4K】 / Japan Travel Guide / 金閣寺` is
    two channel decorations and a subject, and splitting once left the second
    separator inside a single reading — one reading spanning both scripts,
    which is the shape the whole intersection exists to avoid, so the title
    read as Latin and an English query threw the right video away.

    A side with no *terms* in it is not a reading, which is a weaker test than
    no letters: the filter is :func:`_units`, and a lone `A` or a lone `塔` is
    a letter that yields no term. So `塔 / Kyoto Travel` is one reading and the
    Japanese half gets no vote — the same arithmetic :func:`comparable` uses on
    the query side, and wrong here in the same direction. A trailing separator,
    or a side that was all decoration, leaves the title read exactly as it
    would have been without it.
    """
    stripped = plain(title)
    sides = [side for side in _SEPARATOR.split(stripped) if _units(side)]
    return sides or [stripped]


def written_in(title: str) -> frozenset[str]:
    """The writing systems `title` is actually written in. See :data:`MIN_SHARE`.

    The same list as :func:`scripts` minus the alphabets that only turn up in a
    brand or a tag. For the title side of :func:`comparable`, where that
    difference is a video kept or thrown away.

    Counted in the stripped title and divided by the whole one, which is what
    makes :func:`plain` safe to be greedy with: the strip can only take whole
    terms out of the numerator, so it can lower a script's share and never
    raise it — see :data:`_DECORATION` for why the tag rule needs writing
    carefully to keep that true.

    Lowering a share is not the same as making a title unjudgeable, and a strip
    that guesses wrong can still leave one script over the bar on its own:
    `【六甲山カンツリーハウス】Rokko Kobe Japan Travel` loses its whole Japanese
    half to the brackets and comes out `{'LATIN'}`, four terms over a raw total
    of thirteen. What the direction guarantees is narrower — no script the raw
    title does not have a quarter of can be given one by the strip.

    The share is asked of every reading and the answers are intersected, so a
    title whose halves are in different scripts is written in neither. That
    is the honest answer to `Japan Travel Guide / 金閣寺`, which is one of the
    two orders and, under a single reading, a Latin title an English query is
    entitled to judge and drop — the right video, gone. When both halves agree
    the intersection changes nothing, so the filter keeps working on
    `京都グルメ / チャンネル名`; it only steps back where the ambiguity is.
    """
    total = sum(_units(title).values())
    counts = [_units(reading) for reading in _readings(title)]
    return frozenset(
        name for name in set().union(*counts)
        if all(seen.get(name, 0) >= total * MIN_SHARE for seen in counts))


def comparable(text: str, wanted: list[str]) -> bool:
    """Could a zero from :func:`score` mean anything for `text`?

    No shared terms is evidence of two different subjects only when the two
    could have shared a term in the first place. An English query against a
    Japanese title has no overlap to find and never will, so reading the zero
    as "unrelated" throws away every result of a search that was deliberately
    run in the person's language rather than in the query's.

    The terms and not the query they came from, because the terms are what did
    the searching. "Blue Bottle Coffee 清澄白河" yields `清澄`, `澄白` and
    `白河` alongside the Latin, so a Japanese title had three terms aimed at it
    and a zero means something. Going the other way, `塔` yields no term at all
    — one character has no pair to give — so a query of "塔 Tokyo" has nothing
    Japanese to judge a Japanese title with, whatever the query string looks
    like.

    The two sides are asked different questions, and the asymmetry is the
    point. Every term is a whole chance to match, so one Japanese term among
    five Latin ones counts; a title is prose, and one Latin word among twenty
    Japanese ones is a brand name rather than a language. Hence :func:`scripts`
    on the terms and :func:`written_in` on the title.

    Both halves of that have a price, and neither is free:

    - A Latin word carrying the *subject* of a Japanese title is read as a brand
      too. `Osaka 大阪グルメ食べ歩き…` is not comparable to `Kyoto autumn
      leaves`, so an Osaka video survives a Kyoto query. Nothing here can tell
      `Osaka` from `T-fal`, and keeping a video that should have gone costs a
      row on a card while dropping one that should have stayed can empty it.
    - A query in two languages is comparable to everything, and `place_info`
      sends those constantly. `Blue Bottle Coffee 清澄白河` has Japanese terms,
      so it judges `深川のブルーボトル` — the right video — and drops it,
      because the terms are bigrams of the place name and the title spells the
      neighbourhood a different way.

    And a third from the unit the shares are counted in rather than from the
    asymmetry: a short Japanese place name loses to three or four Latin words,
    so `紅葉狩り` judges `Autumn Leaves 京都` and drops it. See :func:`_units`.
    """
    return bool(written_in(text) & scripts(" ".join(wanted)))
