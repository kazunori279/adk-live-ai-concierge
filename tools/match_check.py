"""Pin what `_match` and the video filter call related.

`_match` is pure functions over two strings, no I/O, and it decides whether a
video reaches a place card at all. The interesting cases are all three lines
long and none of them needs the network, so they live here rather than being
re-derived by hand whenever the tokenizer is touched.

The bar is asymmetric. A video about somewhere else on the card is the defect
this filter exists for, but an empty card where a good video was available is
the regression it can cause, and the second is the easier one to ship without
noticing — every path to it is a plausible-looking `continue`. So most of what
is pinned below is a case that must *not* be filtered.

    uv run python tools/match_check.py

Runs every case and exits non-zero if any of them did not hold.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ai_terminal.live.tools import _match, image_search  # noqa: E402
from ai_terminal.live.tools import youtube_search  # noqa: E402
from ai_terminal.live.tools.youtube_search import _relevant  # noqa: E402

failed = False
# Counted rather than written down. The number of cases went into a commit
# message wrong twice, both times by one, because it was a number somebody typed
# after adding cases and before adding one more.
ran = 0


def check(name: str, got, want) -> None:
    global failed, ran
    ran += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        failed = True


def ids(videos) -> list[str]:
    return [video_id for video_id, _ in videos]


print("== terms ==")
check("Latin words", _match.terms("Tokyo Skytree"), ["tokyo", "skytree"])
check("a hyphen also gives the bare form",
      _match.terms("Kinkaku-ji"), ["kinkaku-ji", "kinkakuji"])
check("a possessive also gives the stem",
      _match.terms("Kyoto's temples"),
      ["kyoto's", "kyotos", "kyoto", "temples"])
check("Japanese runs give their pairs",
      _match.terms("金閣寺"), ["金閣", "閣寺"])
check("said twice is not twice the evidence",
      _match.terms("coffee coffee"), ["coffee"])
check("one character yields nothing to match", _match.terms("塔"), [])
check("one letter yields nothing either", _match.terms("a"), [])

print("\n== score ==")
check("substring, so a plural still hits",
      _match.score("Kyoto's temples", ["kyoto"]), 1)
check("the bare term matches a hyphenated title",
      _match.score("Kinkaku-ji Temple", _match.terms("Kinkakuji")), 1)
check("and the hyphenated term matches a bare title",
      _match.score("Kinkakuji Temple", _match.terms("Kinkaku-ji")), 1)
check("the possessive stem reaches an ordinary title",
      _match.score("Kyoto Temple Tour", _match.terms("Kyoto's temples")), 1)

print("\n== scripts ==")
check("plain Latin", _match.scripts("Tokyo Skytree"), frozenset({"LATIN"}))
check("kanji and kana are one system",
      _match.scripts("金閣寺のカメラ"), frozenset({"JAPANESE"}))
check("the long vowel mark is not its own system",
      _match.scripts("コーヒー"), frozenset({"JAPANESE"}))
check("nor is half-width kana",
      _match.scripts("ｺｰﾋｰ"), frozenset({"JAPANESE"}))
check("full-width Latin is Latin",
      _match.scripts("ＴＯＫＹＯ"), frozenset({"LATIN"}))
check("no letters at all is no writing system",
      _match.scripts("【2024】 - #1 (4)"), frozenset())
check("a genuinely bilingual title is both",
      _match.scripts("Kyoto 京都 guide 案内"),
      frozenset({"JAPANESE", "LATIN"}))
check("one kanji is enough to appear in a title",
      _match.scripts("Osaka 大阪グルメ食べ歩き最強ランキング完全版まとめ動画"),
      frozenset({"JAPANESE", "LATIN"}))
check("but one on its own yielded no term, so it is no script either",
      _match.scripts("塔"), frozenset())

# The unit the shares below are counted in. `written_in` and `scripts` both
# read off this, so it is pinned here rather than through one of its
# consequences.
print("\n== _units ==")
# Terms and not letters. `Rokko Kobe Japan Travel` is 20 Latin letters against
# 13 Japanese ones, so by letters the channel name owns this title; by terms it
# is four against twelve and the title is Japanese.
check("a Latin word is one term however long it is",
      _match._units("六甲山カンツリーハウス紹介 Rokko Kobe Japan Travel"),
      {"JAPANESE": 12, "LATIN": 4})
# `out[bucket] = out.get(bucket, 0) + (len(run) - 1)` creates the key before it
# adds nothing to it, and a bucket sitting at zero is a script `scripts`
# reports and `written_in` waves through — every share is at least zero times
# MIN_SHARE. So a one-character title got judged instead of left unjudged.
check("a lone kanji is absent, not present with a zero",
      _match._units("塔"), {})
check("so a one-character title is written in nothing",
      _match.written_in("塔"), frozenset())

# The title side, where appearing in a title and being what it is written in
# are different questions. The first case is a real title from a real pool: it
# was kept before this split existed, dropped by presence alone, and the video
# is right.
print("\n== written_in ==")
check("a brand name is not the language of the title",
      _match.written_in("沸騰させるだけ！電気ケトル（T-fal）の掃除方法"),
      frozenset({"JAPANESE"}))
check("nor is one Latin word in a Japanese one",
      _match.written_in("Osaka 大阪グルメ食べ歩き最強ランキング完全版まとめ動画"),
      frozenset({"JAPANESE"}))
check("a genuinely bilingual title is still both",
      _match.written_in("Kyoto 京都 guide 案内"),
      frozenset({"JAPANESE", "LATIN"}))
check("and no letters is still nothing", _match.written_in("【2024】 #1"),
      frozenset())
# The share is counted in the stripped title and divided by the whole one, so
# taking text away can only lower it. Read both off the stripped title and this
# reverses into `{'LATIN'}`: the tag holds 19 of the 23 Japanese terms, and with
# it gone the one Latin word is a fifth of what is left. Nothing clears the bar
# here — the strip took most of the title's own script too — and that is the
# right answer: a title this thoroughly decorated is one nobody should judge.
check("a long tag in the title's own script does not promote a Latin word",
      _match.written_in("【京都観光おすすめスポット完全ガイド決定版】Osaka 大阪グルメ"),
      frozenset())
# Lowering a share is not the same as leaving a title unjudgeable, and this one
# is the difference. The brackets eat the whole Japanese half, and the four
# Latin terms left clear a quarter of the raw thirteen on their own — so a wrong
# strip does not always end in "nothing is written here, keep it".
check("but a wrong strip can still leave one script standing",
      _match.written_in("【六甲山カンツリーハウス】Rokko Kobe Japan Travel"),
      frozenset({"LATIN"}))
# "Can only lower it" is a property of the pattern, not a wish, and the tag rule
# is where it has to be earned. `-` and `'` sit inside a Latin run, so a tag
# matched against a plain `[A-Za-z0-9]` boundary cuts `Rokko-4K-Hike` into
# `Rokko- -Hike` and hands Latin a second term the raw title never had: an
# eighth of this title becomes a quarter, and an English query is then
# entitled to throw the video away.
check("a tag with a word round it is not a tag",
      _match.written_in("六甲山ハイキング Rokko-4K-Hike"),
      frozenset({"JAPANESE"}))
# And the other half of that rule. Refusing every `-` is too blunt: there is no
# Latin run to split when the far side of the hyphen is kanji, and leaving the
# tag in put the one Latin word over the bar on titles this short. All three of
# these are ordinary Japanese titles that an English query used to be handed.
for short in ("金閣寺-vlog", "六甲山-4K", "京都-4K"):
    check(f"a tag hyphenated onto kanji is still a tag: {short}",
          _match.written_in(short), frozenset({"JAPANESE"}))
# The bar itself, from below. Nothing else pinned here fails at 0.20, so the
# threshold could be lowered by a fifth without a case noticing.
check("one Latin word in four Japanese terms is under the bar",
      _match.written_in("Osaka 大阪グルメ"), frozenset({"JAPANESE"}))
# Neither side of a ` / ` is cut, because nothing here can tell a channel from a
# title, so both are read and a script has to own the title under either
# reading. The first of these was the one length at which cutting worked — the
# Japanese side carried three times the Latin side's terms. Take eight
# characters off it, or use `Japan Travel Guide / 金閣寺`, and the surviving
# channel name cleared the bar on its own and the right video went.
check("a channel name beside a long subject leaves the title unjudged",
      _match.written_in("Rokko Kobe Japan Travel / 六甲山カンツリーハウスに行ってきた記録"),
      frozenset())
check("and so does one beside a short subject",
      _match.written_in("Rokko Kobe Japan Travel / 六甲山カンツリーハウス"),
      frozenset())
check("and an ordinary two-word title in the other script",
      _match.written_in("Japan Travel Guide / 金閣寺"), frozenset())
check("the other order answers the same",
      _match.written_in("六甲山カンツリーハウス紹介 / Rokko Kobe Japan Travel"),
      frozenset())
# The step back is only where the ambiguity is. Two halves in one script agree
# whichever is the channel, so the filter keeps working on them — without this
# the rule would read as "a slash makes a title invincible".
check("but two halves in one script are still judged",
      _match.written_in("京都グルメ / チャンネル名"), frozenset({"JAPANESE"}))
check("in either script",
      _match.written_in("Kinkaku-ji Temple Guide / Kyoto Travel"),
      frozenset({"LATIN"}))
# Splitting at the first separator only is worse than not splitting at all when
# the first side is decoration: the empty side drops out, and the one surviving
# reading still has a separator inside it with a different script on each side —
# exactly the shape the intersection exists to refuse. Both of these read as
# Latin and went to an English query.
check("a decoration-only first side does not merge the other two",
      _match.written_in("【4K】 / Japan Travel Guide / 金閣寺"), frozenset())
check("nor does a numbered one",
      _match.written_in("#1 / Japan Travel Guide / 金閣寺"), frozenset())
# The full-width separators are how the pool actually spells this. `｜` takes no
# spaces — requiring them missed the commonest Japanese spelling of the case —
# and `／` was not in the class at all.
check("an unspaced full-width bar is a separator",
      _match.written_in("Japan Travel Guide｜金閣寺"), frozenset())
check("and so is a full-width slash",
      _match.written_in("Japan Travel Guide ／ 金閣寺"), frozenset())
# `｜` unspaced is not a licence to cut anywhere: the ASCII forms still need a
# space on each side, so a date or an `and/or` is not two readings.
check("but a bare ASCII slash is not",
      _match.written_in("Kyoto and/or Nara Travel Guide"),
      frozenset({"LATIN"}))
# `and/or` is Latin either side of the cut, so it cannot tell whether the space
# requirement is doing anything. This one can: split at the `/` and the title
# reads as two, one per script, and `written_in` intersects to nothing.
check("and a bare slash between two scripts is not either",
      _match.written_in("AC/DC 東京ライブ映像"),
      frozenset({"JAPANESE", "LATIN"}))
# The unspaced `／` is also how a date is written, and a date with text on both
# sides is the shape that breaks: the outer components glue onto that text, so
# `2026／01／01` split this title into `東京スカイツリー 2026` and
# `01 Tokyo Skytree New Year` — one reading per script, `written_in` empty, and
# an unrelated video kept because nothing was entitled to judge it.
check("a date between two scripts is not two readings",
      _match.written_in("【LIVE】東京スカイツリー 2026／01／01 Tokyo Skytree New Year"),
      frozenset({"JAPANESE", "LATIN"}))
check("and neither is one with no spaces round it",
      _match.written_in("【LIVE】東京スカイツリー2026／01／01Tokyo Skytree New Year"),
      frozenset({"JAPANESE", "LATIN"}))
# The same date, spaced. `\s*` eats the space before a lookbehind could see the
# digit through it, so the zero-space assertion alone let this one split — which
# is the shape above, back again, in the spelling nobody pinned.
check("nor one with a space on each side of the slash",
      _match.written_in("【LIVE】東京スカイツリー 2026 ／ 01 ／ 01 Tokyo Skytree New Year"),
      frozenset({"JAPANESE", "LATIN"}))
# The same date again, spaced with U+3000 — which in a Japanese title is not an
# exotic spelling but the ordinary one, since it is the space the language's own
# input methods produce. The outer `\s*` eats it like any other space, so the
# assertions have to see it too or this is the one-space bug in a new costume.
# NBSP and tab come along for free with `\s` and are here for the same reason
# the two-space case is: to make narrowing it back a deliberate deletion.
check("a full-width space round the slash is a date too",
      _match.written_in("【LIVE】東京スカイツリー 2026　／　01　／　01 Tokyo Skytree New Year"),
      frozenset({"JAPANESE", "LATIN"}))
check("and so are the other spaces the outer \\s* already eats",
      [sorted(_match.written_in(f"東京スカイツリー 2026{sp}／{sp}01 Tokyo Skytree"))
       for sp in ("\u00a0", "\t")],
      [["JAPANESE"], ["JAPANESE"]])
# Two spaces and more are uncovered, deliberately: each one costs another
# fixed-width assertion and `2026  ／  01` is not a spelling titles use. Pinned
# so that the next reader knows it was decided rather than missed, and so that
# widening the rule to swallow it is a change with a case attached.
check("two spaces is where this stops, and it is a split",
      _match.written_in("東京スカイツリー 2026  ／  01 Tokyo Skytree"), frozenset())
check("nor a two-part date",
      _match.written_in("東京スカイツリー 10／1 Tokyo Skytree"),
      frozenset({"JAPANESE"}))
# A date at either end was always harmless — the side it makes has no terms in
# it — and it stays that way, so the rule above is not paid for by the leading
# case going wrong instead.
check("a date at the front still leaves one title",
      _match.written_in("2024／10／01 京都の紅葉ライブ Kyoto Autumn"),
      frozenset({"JAPANESE"}))
check("and a bare date is still no writing system",
      _match.written_in("2024／10／01"), frozenset())
# Only *both* sides being digits disqualifies the slash. A number against a
# word is an ordinary title, and it still splits — without this the rule above
# could be widened to "any slash touching a digit" and nothing would notice.
check("a digit on one side only is still a separator",
      _match.written_in("六甲山2024／Japan Travel Guide Kobe"), frozenset())
# And spaced, which is the control on the two new assertions: they are one-sided
# the same way the originals are, so a digit-space on the left does not stop a
# slash whose right side is a channel name.
check("spaced, with the digit on one side only, it still is",
      _match.written_in("六甲山2024 ／ Japan Travel Guide Kobe"), frozenset())
# And the control that stops the date case being bought by giving up on `／`
# altogether: requiring a space on each side of it, the way the ASCII forms
# need one, fixes every case above and passes every other case in this file.
check("an unspaced full-width slash is still a separator",
      _match.written_in("Japan Travel Guide／金閣寺"), frozenset())
# `|` is the third spelling and the one nothing watched. Same shape as the two
# above so that dropping any one character from the class costs a case.
check("and so is a spaced ASCII bar",
      _match.written_in("Japan Travel Guide | 金閣寺"), frozenset())

print("\n== _readings ==")
check("no separator is one reading",
      _match._readings("六甲山カンツリーハウス紹介"), ["六甲山カンツリーハウス紹介"])
check("a slash is two, and neither of them is cut",
      _match._readings("六甲山カンツリーハウス紹介 / Rokko Kobe Japan Travel"),
      ["六甲山カンツリーハウス紹介", "Rokko Kobe Japan Travel"])
check("two slashes are three",
      _match._readings("【4K】 / Japan Travel Guide / 金閣寺"),
      ["Japan Travel Guide", "金閣寺"])
check("a side with no terms left in it is not a reading",
      _match._readings("六甲山 / 【4K】"), ["六甲山"])
# Terms, not letters. A lone kanji has no pair to give and a one-letter word is
# not a `_RUN`, so both of these sides are dropped despite having a letter in
# them — the same arithmetic that makes `塔 Tokyo` unable to judge Japanese.
check("a lone kanji is a letter but not a term",
      _match._readings("塔 / Kyoto Travel"), ["Kyoto Travel"])
check("and neither is a single Latin letter",
      _match._readings("A / 金閣寺"), ["金閣寺"])
# A date is not a separator however many parts it has, spaced or not. Split
# there and the year joins the text in front of it and the day joins the text
# behind, which is two readings out of one title.
check("a date is one reading, not three",
      _match._readings("東京スカイツリー 2026／01／01 Tokyo Skytree"),
      ["東京スカイツリー 2026／01／01 Tokyo Skytree"])
check("and unspaced is still one",
      _match._readings("六甲山カンツリーハウス2024／10／01Rokko Kobe Travel"),
      ["六甲山カンツリーハウス2024／10／01Rokko Kobe Travel"])
check("and so is a date with one space round each slash",
      _match._readings("東京スカイツリー 2026 ／ 01 ／ 01 Tokyo Skytree"),
      ["東京スカイツリー 2026 ／ 01 ／ 01 Tokyo Skytree"])
# There is no ceiling on the split either. `maxsplit=1` is pinned three times
# over by the two-separator cases above; a `maxsplit=2` would pass all of them
# and still weld the last two sides of a four-part title into one reading.
check("and a fourth side is its own reading too",
      _match._readings("京都の紅葉 / Kyoto Autumn / チャンネル名 / Travel Japan"),
      ["京都の紅葉", "Kyoto Autumn", "チャンネル名", "Travel Japan"])

# What the strip does to the string, on its own. The script questions are a
# group up, on `written_in`, because that is where they are actually asked —
# it takes the numerator from the stripped title and the denominator from the
# raw one, so `scripts(plain(t))` is not a question anything runs.
print("\n== plain ==")
check("a bracketed tag", _match.plain("【4K】六甲山"), " 六甲山")
check("a square-bracketed one", _match.plain("[MV] 六甲山"), "  六甲山")
check("a bare tag word with no brackets round it",
      _match.plain("六甲山 vlog"), "六甲山  ")
# `\b` does not fire between a kanji and a Latin letter, because Python's `\w`
# includes kanji. This is how Japanese titles are actually written — the spaced
# spelling above is the rarer one — so the tag list missed its commonest case.
check("and one with no space in front of it, which is the usual spelling",
      _match.plain("六甲山vlog"), "六甲山 ")
check("but a tag inside a word is part of the word",
      _match.plain("六甲山ハイキング Rokko-4K-Hike"), "六甲山ハイキング Rokko-4K-Hike")
check("apostrophes count as inside it too",
      _match.plain("it's-hd-fine"), "it's-hd-fine")
# With an apostrophe on *both* sides and no hyphen anywhere, so this is the
# only case where the apostrophe half of that rule is what decides. Cutting
# here would turn one Latin run into two, which is the invariant this file
# sweeps for: the strip may never hand a script more of the title than the raw
# string gave it.
check("on their own, with no hyphen to lean on",
      _match.plain("rokko'4k'hike"), "rokko'4k'hike")
# A hyphen is only inside a word when a Latin character is on the far side of
# it. Kanji there means the tag has a run to itself after all.
check("a hyphen with kanji on the other side is punctuation",
      _match.plain("金閣寺-vlog"), "金閣寺- ")
check("in either order", _match.plain("4K-金閣寺"), " -金閣寺")
check("parentheses are content, not decoration",
      _match.plain("Kinkaku-ji (Golden Pavilion)"),
      "Kinkaku-ji (Golden Pavilion)")
check("the channel name after a slash is left where it is",
      _match.plain("六甲山カンツリーハウス紹介 / Rokko Kobe Japan Travel"),
      "六甲山カンツリーハウス紹介 / Rokko Kobe Japan Travel")
# A bracket that closes with the other pair's character is a typo, not a
# licence to eat everything up to the next one.
check("brackets have to match", _match.plain("【4K]六甲山"), "【 ]六甲山")

print("\n== comparable ==")
check("an English query cannot judge a Japanese title",
      _match.comparable("六甲山カンツリーハウスに行ってきた",
                        _match.terms("Rokkosan Country House")), False)
check("and a 4K tag does not change that",
      _match.comparable("【4K】六甲山カンツリーハウス紹介",
                        _match.terms("Rokkosan Country House")), False)
check("nor does a vlog tag",
      _match.comparable("六甲山 vlog", _match.terms("Rokkosan")), False)
check("nor does the channel name after the title",
      _match.comparable("六甲山カンツリーハウス紹介 / Rokko Kobe Japan Travel Vlog",
                        _match.terms("Rokkosan Country House")), False)
check("two Japanese strings can be judged",
      _match.comparable("金閣寺の紅葉", _match.terms("六甲山")), True)
# The terms are what did the searching, so they are what gets asked. A
# romanised shop name and a Japanese place name is the commonest thing
# `place_info` sends here, and the Japanese half of it has to count.
check("a Japanese term in a mostly-Latin query can judge a Japanese title",
      _match.comparable("大阪グルメ食べ歩き",
                        _match.terms("Blue Bottle Coffee 清澄白河")), True)
check("but a mostly-Japanese one is not, whatever Latin is in it",
      _match.comparable("Osaka 大阪グルメ食べ歩き最強ランキング完全版まとめ動画",
                        _match.terms("Kyoto autumn leaves")), False)
check("nor is a Japanese title with a Latin brand name in it",
      _match.comparable("沸騰させるだけ！電気ケトル（T-fal）の掃除方法",
                        _match.terms("how to descale a kettle")), False)
# And the other way: a character too short to become a term never had a
# chance, so its script is not the query's for this purpose.
check("a character that yielded no term does not count as the query's script",
      _match.comparable("東京タワーの夜景", _match.terms("塔 Tokyo")), False)

print("\n== _relevant: what must be dropped ==")
check("junk in the query's own script",
      ids(_relevant("六甲山カンツリーハウス", [
          ("keep", "六甲山カンツリーハウスに行ってきた"),
          ("junk", "【京都観光】金閣寺を歩く"),
      ])), ["keep"])
check("everything junk leaves an empty card",
      _relevant("六甲山カンツリーハウス", [("junk", "【京都観光】金閣寺を歩く")]), [])
check("a video oEmbed says does not exist",
      _relevant("Tokyo Skytree", [("gone", "")]), [])
# A query with a Japanese term in it can throw away a Japanese video, which is
# the direction reading the scripts off the whole query string used to miss.
check("a Japanese title against a half-Japanese query",
      _relevant("Blue Bottle Coffee 清澄白河", [("junk", "大阪グルメ食べ歩き")]), [])
# Reading both sides of a ` / ` instead of cutting at one steps back only where
# the two disagree. Junk that carries a channel name in the title's own script
# still goes, which is most of the junk that carries one at all.
check("junk with a channel name beside it, both in one script",
      _relevant("六甲山カンツリーハウス", [("junk", "京都グルメ / チャンネル名")]), [])
# The decision the date rule exists for. Reading `2024／10／01` as two
# separators left this title written in nothing, so `comparable` was False, the
# title was never judged, and a Kyoto video reached a Rokkosan card.
check("junk with a date in the middle of it",
      _relevant("Rokkosan Country House",
                [("dated", "京都の紅葉ライブ 2024／10／01 Kyoto Autumn Leaves")]), [])
check("in the other direction too",
      _relevant("六甲山の牧場",
                [("dated", "Kyoto Autumn 2024／11／23 京都紅葉の名所めぐり")]), [])
# The same two decisions in the spaced spelling. `written_in` above says the
# reading is right; these say the video is actually thrown away, through the
# `terms`/`score`/`comparable` arithmetic `youtube_search.py` performs.
check("and the same date, spaced, is still junk",
      _relevant("Rokkosan Country House",
                [("dated", "京都の紅葉ライブ 2024 ／ 10 ／ 01 Kyoto Autumn Leaves")]),
      [])
check("in the other direction too, spaced",
      _relevant("六甲山の牧場",
                [("dated", "Kyoto Autumn 2024 ／ 11 ／ 23 京都紅葉の名所めぐり")]), [])
# And in the spelling a Japanese input method actually produces. The ASCII pair
# above passed while this one kept the video, which is the whole of round 10.
check("and with a full-width space, still junk",
      _relevant("Rokkosan Country House",
                [("dated", "京都の紅葉ライブ 2024　／　10　／　01 Kyoto Autumn Leaves")]),
      [])
# The positive control beside it, because a rule that drops everything passes
# every case above. This title is about the place the query names and survives
# the same date in the same spelling.
check("but a matching title with the same date survives it",
      ids(_relevant("Rokkosan Country House",
                    [("good", "六甲山カンツリーハウス 2024　／　10　／　01 Rokko Kobe")])),
      ["good"])

print("\n== _relevant: what must not be dropped ==")
check("a whole pool the query cannot judge",
      ids(_relevant("how to descale a kettle", [
          ("a", "おそうじ革命｜【実録】クエン酸を使った電気ケトルのお手入れ方法"),
          ("b", "【沸騰させるだけ！】電気ケトル・ポットの水垢をクエン酸で洗浄する方法"),
          ("c", "電気ポットの簡単掃除法"),
      ])), ["a", "b", "c"])
check("the 4K cut of the right video",
      ids(_relevant("Rokkosan Country House", [
          ("hd", "【4K】六甲山カンツリーハウス紹介"),
      ])), ["hd"])
check("the vlog cut of it",
      ids(_relevant("Rokkosan", [("vlog", "六甲山 vlog")])), ["vlog"])
check("and the cut with the channel name after it",
      ids(_relevant("Rokkosan Country House", [
          ("channel", "六甲山カンツリーハウス紹介 / Rokko Kobe Japan Travel Vlog"),
      ])), ["channel"])
# The pool that found this one had eight Japanese kettle videos in it and the
# only Latin anywhere was a manufacturer's name.
check("a Japanese video whose subject happens to own a Latin trademark",
      ids(_relevant("how to descale a kettle", [
          ("tfal", "沸騰させるだけ！電気ケトル（T-fal）の掃除方法 - くらしのマーケット"),
      ])), ["tfal"])
check("a query with no terms in it has no opinion",
      ids(_relevant("塔", [("a", "東京タワーの夜景"), ("b", "沖縄の海")])),
      ["a", "b"])
# The three shapes a strip can get wrong, one per mechanism. Each of these was
# dropped at c8ff59a: the first because stripping Japanese decoration raised
# Latin's share, the second because the separator rule took the title and left
# the channel, the third because `\b` will not fire against a kanji.
check("a Japanese title carrying a long tag in its own script",
      ids(_relevant("Kyoto autumn leaves", [
          ("tagged", "【京都観光おすすめスポット完全ガイド決定版】Osaka 大阪グルメ"),
      ])), ["tagged"])
check("and one where the channel name comes first",
      ids(_relevant("Rokkosan Country House", [
          ("channel-first",
           "Rokko Kobe Japan Travel / 六甲山カンツリーハウスに行ってきた記録"),
      ])), ["channel-first"])
check("and one with the tag word run onto the kanji",
      ids(_relevant("Rokkosan", [("run-on", "六甲山vlog"), ("4k", "六甲山4K")])),
      ["run-on", "4k"])
check("a title oEmbed could not be reached for",
      ids(_relevant("Tokyo Skytree", [("unknown", None)])), ["unknown"])

# What the asymmetry costs, on both sides. Neither of these is the answer
# anybody wants; they are the price of the two rules above it, and they are
# pinned so that a change that fixes one has to say it changed this.
print("\n== _relevant: the price of the two rules ==")
# A Latin word carrying the subject reads as a brand, so an Osaka video survives
# a Kyoto query. Nothing here can tell `Osaka` from `T-fal`.
check("a Latin subject word in a Japanese title is read as a brand",
      ids(_relevant("Kyoto autumn leaves", [
          ("osaka", "Osaka 大阪グルメ食べ歩き最強ランキング完全版まとめ動画"),
      ])), ["osaka"])
# And the other side: a two-language query is comparable to everything, which
# is what `place_info` sends most. Both of these are the right video and both
# go, because the Japanese terms are bigrams of a place name neither title
# spells that way.
check("a two-language query judges — and drops — the right Japanese video",
      ids(_relevant("Blue Bottle Coffee 清澄白河", [
          ("fukagawa", "深川のブルーボトル"),
          ("kana", "きよすみしらかわ カフェ巡り"),
      ])), [])
# And a third price, from the unit the shares are counted in rather than from
# the asymmetry: a Latin word is one term at any length and a Japanese run is
# one per pair, so three or four Latin words outweigh a two-character place
# name and the title reads as Latin. By letters these three survive.
check("a short Japanese place name loses to three Latin words",
      ids(_relevant("紅葉狩り", [("kyoto", "Autumn Leaves 京都")])), [])
check("the same shape as the field title, in the other direction",
      ids(_relevant("六甲山の牧場", [("pasture", "Kobe Rokkosan Pasture 神戸")])),
      [])
# Nothing is written in a title that is all decoration, so nothing can judge it
# and it survives every query. There is no fix in this file — the title says
# nothing about the video — and it is pinned so the next change to the strip
# has to say it changed this.
check("a title made only of decoration is invincible",
      ids(_relevant("Rokkosan", [("junk", "【2024】 #1")])), ["junk"])

print("\n== _relevant: order ==")
check("more terms first, unjudgeable last",
      ids(_relevant("Kinkaku-ji Kyoto temple", [
          ("one", "A temple in Japan"),
          ("unknown", None),
          ("three", "Kinkakuji, the golden temple of Kyoto"),
      ])), ["three", "one", "unknown"])
check("a video with no title still carries an empty one, not None",
      _relevant("Tokyo Skytree", [("unknown", None)]), [("unknown", "")])
# Both of these score zero, so only the second key separates them — and the
# sort is stable, so without it the input order decides which one takes the
# player. `cards.js` labels a titleless video with its URL.
check("a title that was read beats one that could not be",
      ids(_relevant("Rokkosan Country House", [
          ("unknown", None),
          ("jp", "六甲山カンツリーハウスに行ってきた"),
      ])), ["jp", "unknown"])


class FakeResponse:
    """The three things `_video_title` reads off a `requests` response."""

    def __init__(self, status_code, payload=None, bad_json=False):
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not JSON")
        return self._payload


def title_for(response) -> str | None:
    """`_video_title` against a fixed response, or an exception if given one."""
    real = youtube_search.http_requests.get

    def fake(*args, **kwargs):
        if isinstance(response, Exception):
            raise response
        return response

    youtube_search.http_requests.get = fake
    try:
        return youtube_search._video_title("x" * 11)
    finally:
        youtube_search.http_requests.get = real


# The branch that decides whether a bad thirty seconds on the network empties
# the card. Reverting it to one catch-all returning "" leaves everything above
# green, because those cases pin what `_relevant` does with a None rather than
# the decision that produces one.
print("\n== _video_title: refused, unsure, or a title ==")
check("a real title comes back",
      title_for(FakeResponse(200, {"title": "六甲山"})), "六甲山")
check("private, deleted or region-locked is a refusal",
      [title_for(FakeResponse(code)) for code in (401, 403, 404)], ["", "", ""])
check("a rate limit says nothing about the video",
      title_for(FakeResponse(429)), None)
check("nor does a 500", title_for(FakeResponse(500)), None)
check("nor does a timeout", title_for(TimeoutError("timed out")), None)
check("nor does a 200 that is not JSON",
      title_for(FakeResponse(200, bad_json=True)), None)
check("nor does a 200 with no title in it",
      title_for(FakeResponse(200, {"author_name": "somebody"})), None)


# --------------------------------------------------- which empty this was
#
# `_relevant` above decides what to throw away, and it is the natural suspect
# whenever a card comes up with no video. #246 is the case where it was
# innocent and looked guilty: the grounding call returned in 1.5 s having run
# no search, the thread pool mapped over an empty list, and `videos: []` came
# out the far end reading exactly like a filter that had rejected everything.
# The tool then told the model, in its own docstring, that the empty meant
# "nothing came back that the title says is about the query" — so the
# household was told there are no videos of ramen shops.
#
# These cases hold each stage of the pipeline responsible for its own empty.
# They are here rather than in `wiring_check.py` because the filter is here
# and the whole point is telling the filter's empty from the other three.


class FakeGrounding:
    """A grounding POST's reply. Separate from `FakeResponse`, which is oEmbed's
    and reads a different two attributes off it."""

    def __init__(self, metadata: dict):
        self.metadata = metadata

    def raise_for_status(self):
        pass

    def json(self):
        return {"candidates": [{"groundingMetadata": self.metadata}]}


class FakeSearch:
    """The grounding endpoint, answering with a fixed `groundingMetadata`."""

    def __init__(self, metadata: dict):
        self.metadata = metadata
        self.posted = 0

    def post(self, url, data=None, headers=None, timeout=None):
        self.posted += 1
        return FakeGrounding(self.metadata)


def video_result(metadata: dict, resolves=None, titles=None) -> dict:
    """`_youtube_search_sync` against a fixed response and no network.

    Four things are replaced, and the count on the session is asserted rather
    than assumed: a case here that silently reached the real endpoint would be
    a case that passes on a laptop with credentials and fails in the loop.
    """
    session = FakeSearch(metadata)
    real = (youtube_search.authed_session, youtube_search.search_url,
            youtube_search._resolve_video_id, youtube_search._video_title)
    youtube_search.authed_session = lambda: session
    youtube_search.search_url = lambda: "https://example.invalid/"
    youtube_search._resolve_video_id = lambda uri: (resolves or {}).get(uri, "")
    youtube_search._video_title = lambda vid: (titles or {}).get(vid, "a title")
    try:
        result = youtube_search._youtube_search_sync("kettle descaling", "en")
    finally:
        (youtube_search.authed_session, youtube_search.search_url,
         youtube_search._resolve_video_id, youtube_search._video_title) = real
    assert session.posted in (1, 2), "the fixture did not reach the stub"
    return result


def image_result(metadata: dict) -> dict:
    """`_image_search_sync` against a fixed response and no network."""
    session = FakeSearch(metadata)
    real = (image_search.authed_session, image_search.search_url)
    image_search.authed_session = lambda: session
    image_search.search_url = lambda: "https://example.invalid/"
    try:
        result = image_search._image_search_sync("kettle descaling", "en")
    finally:
        (image_search.authed_session, image_search.search_url) = real
    assert session.posted == 1, "the fixture did not reach the stub"
    return result


def chunks(*uris) -> list[dict]:
    return [{"web": {"uri": uri}} for uri in uris]


print("\n== youtube_search: four ways to come back with nothing ==")
none_at_all = video_result({})
check("a call that never searched says so, and does not say there are none",
      none_at_all["why"].startswith("the search did not run at all"), True)
check("and the counts behind it are on the result, so the log carries them",
      [none_at_all["found"], none_at_all["watchable"],
       len(none_at_all["search_queries"]), len(none_at_all["videos"])],
      [0, 0, 0, 0])
# The distinction the whole issue turns on: the same empty list, and one of
# them is an answer about the world while the other is a failure to look.
searched_nothing = video_result({"webSearchQueries": ["ramen shops youtube"]})
check("a search that ran and found nothing is a different sentence",
      searched_nothing["why"], "the search ran and turned up no videos")
check("with the query it ran carried back",
      len(searched_nothing["search_queries"]), 1)

# A null field from the endpoint is empty rather than raising TypeError (#272).
null_queries_video = video_result({"webSearchQueries": None})
check("a null webSearchQueries in youtube_search is treated as empty",
      null_queries_video["why"].startswith("the search did not run at all"), True)
check("and queries is reported as an empty list, not None",
      null_queries_video["search_queries"], [])

pages = video_result(
    {"webSearchQueries": ["q"], "groundingChunks": chunks("u1", "u2")})
check("sources that resolve to no video are named as that",
      pages["why"], "what the search turned up was pages rather than videos")
check("and the count says how many there were to reject",
      [pages["found"], pages["watchable"]], [2, 0])

# An English title against an English query, because a Japanese one would be
# *kept* — `comparable` is False across scripts and the sections above are
# most of what pins that. This is the pool `_relevant` really does empty.
rejected = video_result(
    {"webSearchQueries": ["q"], "groundingChunks": chunks("u1")},
    resolves={"u1": "aaaaaaaaaaa"},
    titles={"aaaaaaaaaaa": "Slurping through Japan: a noodle documentary"})
check("and only the last of the four is the filter's doing",
      rejected["why"],
      "videos came back, and their titles were about something else")
check("which is the one the old docstring claimed for all of them",
      [rejected["found"], rejected["watchable"], len(rejected["videos"])],
      [1, 1, 0])

# oEmbed answers 401/403/404 for a private, deleted or region-locked video,
# `_video_title` turns that into "", and `_relevant` drops it at the top of
# the loop without reading a title at all. A pool of nothing else empties on
# availability, not on relevance, and telling the household their titles were
# about something else invites a rephrase that cannot possibly help.
gone = video_result(
    {"webSearchQueries": ["q"], "groundingChunks": chunks("u1", "u2")},
    resolves={"u1": "ccccccccccc", "u2": "ddddddddddd"},
    titles={"ccccccccccc": "", "ddddddddddd": ""})
check("a pool oEmbed refused outright is not the filter's doing",
      gone["why"],
      "the videos that came back are private, deleted, or not viewable "
      "from here")
check("and the count of them rides along",
      [gone["watchable"], gone["unavailable"], len(gone["videos"])],
      [2, 2, 0])
# One gone and one judged is still the filter's empty: something did have its
# title read and missed, so a shorter query is worth trying.
mixed = video_result(
    {"webSearchQueries": ["q"], "groundingChunks": chunks("u1", "u2")},
    resolves={"u1": "ccccccccccc", "u2": "eeeeeeeeeee"},
    titles={"ccccccccccc": "", "eeeeeeeeeee": "Knitting a scarf"})
check("but one readable title among them puts it back on the filter",
      mixed["why"],
      "videos came back, and their titles were about something else")

found = video_result(
    {"webSearchQueries": ["q"], "groundingChunks": chunks("u1")},
    resolves={"u1": "bbbbbbbbbbb"},
    titles={"bbbbbbbbbbb": "how to descale a kettle"})
check("a video that came back leaves no reason to give",
      [found["why"], len(found["videos"])], ["", 1])

print("\n== image_search: the same distinction, three stages ==")
no_search = image_result({})
check("a call that never searched says so here too",
      no_search["why"].startswith("the search did not run at all"), True)
check("a search with no photos on it is the second thing",
      image_result({"webSearchQueries": ["q"]})["why"],
      "the search ran and came back with no photos")
# A null field from the endpoint is empty here too (#272).
null_queries_image = image_result({"webSearchQueries": None})
check("a null webSearchQueries in image_search is treated as empty",
      null_queries_image["why"].startswith("the search did not run at all"), True)
check("and image queries is reported as an empty list, not None",
      null_queries_image["search_queries"], [])
# 2.611 s in the #246 log, inside the grounded band rather than under it —
# so this one had searched, and it is the state this case pins.
thumbless = image_result({"webSearchQueries": ["q"],
                          "attachments": [{"image": {}}]})
check("and a photo with no thumbnail is the third, not the second",
      [thumbless["why"], thumbless["attached"]],
      ["photos came back, and none of them carried a thumbnail", 1])
kept = image_result({"webSearchQueries": ["q"], "attachments": [
    {"image": {"thumbnail": {"uri": "https://example.invalid/t.jpg"}}}]})
check("a photo that came back leaves no reason to give",
      [kept["why"], len(kept["images"])], ["", 1])

# attachments beyond MAX_IMAGES are truncated before counting (#271).
many_attachments = image_result({
    "webSearchQueries": ["q"],
    "attachments": [{"image": {}}] * 8,
})
check("image_search truncates attachments to MAX_IMAGES before counting",
      many_attachments["attached"], 6)

# And attached reflects only the examined attachments (#271).
over_claimed = image_result({
    "webSearchQueries": ["q"],
    "attachments": [{"image": {}}] * 6 + [
        {"image": {"thumbnail": {"uri": "https://example.invalid/t.jpg"}}}
    ] * 2,
})
check("and attached reflects only the attachments examined",
      [over_claimed["attached"], len(over_claimed["images"])], [6, 0])

print(f"\nall {ran} cases hold" if not failed else f"\nsome of {ran} cases failed")
sys.exit(1 if failed else 0)
