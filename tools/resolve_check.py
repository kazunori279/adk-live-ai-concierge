"""Check which document a request resolves to before the sub-agent sees it.

`gws_cache.named` decides, off one string, whether a question is about a
document the cache already holds. When it says yes, `gws._prefetch` reads that
document and puts it in front of the sub-agent's first turn, and the sub-agent
is told not to go looking. Getting it wrong is not a slow answer, it is a
confident answer about the wrong file, so the two failures are not symmetric:

- **A false positive is the expensive one.** Nothing downstream re-checks the
  choice. The sub-agent is told the document in its message is the document the
  request is about, and it answers from it.
- **A miss costs nothing** beyond today's behaviour — one turn spent naming the
  file. So the cases below spend most of their time on questions that must
  *not* resolve, and the gate is deliberately narrow enough that plenty of real
  document questions fall through it.

The other half is freshness. `_prefetch` goes through `read_cache` rather than
opening the file, because `_is_stale` lives inside it, and a resolver that
skipped that check would answer fluently out of a copy Drive has moved past.
That is checked here too, against a stubbed Drive.

Runs against a fixture cache in a temporary directory, in a fraction of a
second, and talks to nothing.

    uv run python tools/resolve_check.py

Exits non-zero on the first case that does not hold.
"""

import asyncio
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ai_terminal.live.tools import gws, gws_cache  # noqa: E402

# The six in `tools/latency_report.py`, verbatim, and what each one is about.
# Verbatim because they are the questions every latency number in the README
# was measured on: a resolver that works on a paraphrase of them and not on
# them is a resolver measured against nothing.
READ = {
    "In their US trip itinerary document in Drive, what time does the flight "
    "to San Francisco leave, and where from?": "us-trip-itinerary.md",
    "In their US trip itinerary document in Drive, which hotel are they in in "
    "New York, and for which nights?": "us-trip-itinerary.md",
    "In their LLM notes document in Drive, what does it say about Mixture of "
    "Experts?": "llm-notes.md",
    "In their LLM notes document in Drive, what does it say about KV cache "
    "memory?": "llm-notes.md",
    "In their paper notes document in Drive, what does it say about token "
    "compression?": "paper-notes.md",
    "In their paper notes document in Drive, which robotics papers does it "
    "cover?": "paper-notes.md",
}

# Questions with no cached document behind them, or two. Every one of these has
# to fall through: several outscore a true hit on `_scored`'s arithmetic, which
# is why the gate is phrases and not a number.
DECLINE = [
    "Any mail from the airline about the flight?",
    "What does it say about token compression?",
    "In their Q3 budget document in Drive, what is the headcount line?",
    "What is on their shopping list note?",
    "What is on their calendar tomorrow afternoon?",
    "Next Tokyo slides, what is the session abstract?",
    "In that document, what did they decide?",
    "米国旅行の旅程で、ニューヨークのホテルはどこ？",
    "論文メモにロボティクスの論文はある？",
]

# Counted rather than written down, so the closing line cannot drift from the
# number of cases the way a figure typed into a commit message does.
RAN = 0


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


def cache(name: str, body: str, **fields) -> Path:
    """One cached file with a header, as `_annotate` would have left it."""
    path = gws_cache.cache_dir() / name
    path.write_text(
        gws_cache._header({"source": f"gws drive files export -o {name}",
                           "fetched": "2026-09-08T09:00:00+09:00", **fields})
        + body, encoding="utf-8")
    return path


def drive(**modified: str):
    """Stub `_drive_meta`, keyed by file id, so no `gws` subprocess runs."""
    gws_cache._drive_meta = lambda file_id: (
        {"modifiedTime": modified[file_id]} if file_id in modified else {})


def resolved(request: str) -> str:
    """What `_prefetch` puts in front of the first turn, as text."""
    content = asyncio.run(gws._prefetch(request))
    return "" if content is None else content.parts[0].text


def unconditional(note: str) -> list[str]:
    """Sentences in the note that direct an answer without saying when.

    `named` matches on the phrase and refuses to guess at intent, so this note
    is read by *"any mail from the airline about the US trip itinerary?"* as
    well as by a question about the itinerary. A sentence telling the model to
    answer from the document, with no condition in front of it, is addressed to
    the mail question too, and the answer it gets is the itinerary's flight
    details instead of the mail.

    Looking for a condition rather than for the old wording: the sentence that
    caused this ended "answer from what is here", and a check that greps for
    that phrase passes the moment someone rephrases the same instruction.

    The condition has to open the sentence. "Answer from the text below if the
    request is about the document" means the same thing and is reported here as
    a failure, which is stricter than English needs — kept that way because a
    model skimming an imperative reads the scope before the verb only when the
    scope comes first.
    """
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+", note)
            if re.search(r"\banswer\b", s, re.I)
            and not re.match(r"\s*if\b", s.strip(), re.I)]


def main() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        gws_cache.cache_dir = lambda: Path(scratch)
        # Names and titles copied off the owner's real cache, because the shape
        # is the point: two of the titles are one word, one is 95 characters of
        # slide-deck boilerplate, and three of the ten files have no title at
        # all. A fixture with tidy titles would not exercise the fallback to
        # the filename that carries five of the six questions below.
        cache("paper-notes.md", "Robotics papers and token compression.\n",
              file_id="P1", modified="2026-09-01T00:00:00.000Z", title="Paper",
              summary="Notes on recent papers.")
        cache("llm-notes.md", "KV cache memory and Mixture of Experts.\n",
              file_id="L1", modified="2026-09-01T00:00:00.000Z", title="LLM",
              summary="Notes on language models.")
        cache("us-trip-itinerary.md", "Flight to San Francisco, hotel in NY.\n",
              file_id="T1", modified="2026-09-01T00:00:00.000Z",
              title="2026 US旅行 旅程（9/14〜9/23）", summary="Trip plan.")
        cache("next-tokyo-24-slides.md", "Session abstract.\n", file_id="N1",
              modified="2026-09-01T00:00:00.000Z",
              title="D1-AIML-01 - EN Session - Next '24 Tokyo Session Slides")
        cache("download.csv", "a,b\n1,2\n", file_id="D1",
              modified="2026-09-01T00:00:00.000Z", title="収入・支出詳細")
        # No file_id and no title, the shape three of the ten real records have.
        cache("exported_itinerary.txt", "Another copy of the itinerary.\n")
        drive(P1="2026-09-01T00:00:00.000Z", L1="2026-09-01T00:00:00.000Z",
              T1="2026-09-01T00:00:00.000Z", N1="2026-09-01T00:00:00.000Z",
              D1="2026-09-01T00:00:00.000Z")

        print("== the six questions every latency number was measured on ==")
        for request, want in READ.items():
            check(request[:52] + "…", gws_cache.named(request), want)

        print("\n== and the questions that must not resolve ==")
        for request in DECLINE:
            check(request[:52] + "…", gws_cache.named(request), None,
                  "declined")

        print("\n== what counts as naming a document ==")
        check("the extension is not part of the name",
              gws_cache.named("read paper-notes.md to me"), "paper-notes.md")
        check("nor is the punctuation or the case",
              gws_cache.named("What about PAPER NOTES?"), "paper-notes.md")
        check("the Drive title is a second name, however unwieldy",
              gws_cache.named("what is in D1-AIML-01 - EN Session - Next '24 "
                              "Tokyo Session Slides?"),
              "next-tokyo-24-slides.md")
        # A title is a phrase, not a bag of words. Half of one is somebody
        # talking about the deck, not naming it, and this is the same rule
        # that declines "Next Tokyo slides, what is the session abstract?"
        # above — a question about a deck whose file is right there.
        check("but half of a title is not the title",
              gws_cache.named("what is in the EN Session slides?"), None)
        # `download.csv` is one word once the extension goes, and "download"
        # turns up in questions about something else.
        check("one word is not a name",
              gws_cache.named("download the itinerary for me"), None)
        check("and a phrase has to fall on word boundaries",
              gws_cache.named("what did the bus trip itinerary cost?"), None)
        check("but need not sit at either end of the question",
              gws_cache.named("paper notes"), "paper-notes.md")
        # Japanese is written without spaces, so a name run against the
        # particle after it is one word to `_spoken` and the padding never
        # lands. `download.csv`'s title is 収入・支出詳細 and no Japanese
        # question reaches it. This is the reason both Japanese questions in
        # DECLINE decline, and it is a miss rather than a wrong answer: the
        # sub-agent spends the turn it spends today.
        check("a Japanese name is not a phrase this can hear",
              gws_cache.named("収入・支出詳細を見せて"), None)

        print("\n== two matches is not a match ==")
        cache("trip-itinerary.md", "A third copy.\n")
        check("a longer name contains a shorter one, and neither wins",
              gws_cache.named("In their US trip itinerary document in Drive, "
                              "which hotel?"), None)
        (Path(scratch) / "trip-itinerary.md").unlink()
        check("and with it gone the longer name resolves again",
              gws_cache.named("In their US trip itinerary document in Drive, "
                              "which hotel?"), "us-trip-itinerary.md")

        print("\n== a file with no readable text is never resolved ==")
        (Path(scratch) / "board-deck-slides.pdf").write_bytes(
            b"%PDF-1.7\n\xff\xfe\x00\x01binary")
        check("naming a PDF resolves to nothing, not to an empty document",
              gws_cache.named("what is in the board deck slides?"), None)
        (Path(scratch) / "board-deck-slides.pdf").unlink()

        print("\n== what goes in front of the first turn ==")
        put = resolved("In their paper notes document in Drive, which "
                       "robotics papers does it cover?")
        check("the document's text is in it",
              "Robotics papers and token compression." in put, True)
        check("under its own filename", "`paper-notes.md`" in put, True)
        check("and told not to go and read it again",
              "read_cache" in put and "Do not call" in put, True)
        check("as a user turn, which is what the field takes",
              asyncio.run(gws._prefetch("read paper-notes.md")).role, "user")
        # Nothing, not a note saying the lookup came up empty — a note like
        # that invites the model to trust a matcher that has just said it
        # cannot judge.
        check("a question naming nothing puts nothing there",
              asyncio.run(gws._prefetch("Any mail from the airline?")), None)

        print("\n== and is read by requests that are not about the document ==")
        note = put.split("\n\n")[0]
        check("the note is the first paragraph, ahead of the text",
              note.startswith("The request names"), True)
        check("no sentence in it directs an answer without saying when",
              unconditional(note), [])
        check("a question about the document is one of the two cases it names",
              "If the request is about what the document says" in note, True)
        # The share case is the one this costs nothing and saves the most on:
        # "share the us trip itinerary with Alice by email" is a write, and the
        # note used to tell the sub-agent the whole job was already done.
        check("and a request that needs another service is the other",
              "needs another service" in note, True)
        # Still there. Every turn #31 saved is on the strength of this line.
        check("the sub-agent is still told not to fetch the document again",
              "do not export it again" in note, True)

        print("\n== the freshness check the resolver must not skip ==")
        drive(P1="2026-09-07T00:00:00.000Z", L1="2026-09-01T00:00:00.000Z",
              T1="2026-09-01T00:00:00.000Z", N1="2026-09-01T00:00:00.000Z",
              D1="2026-09-01T00:00:00.000Z")
        stale = resolved("In their paper notes document in Drive, which "
                         "robotics papers does it cover?")
        check("a copy Drive has moved past is not handed over as the answer",
              "Robotics papers and token compression." in stale, False)
        check("the sub-agent is told it went stale",
              "edited since" in stale, True)
        # Without this the sub-agent spends the turn the resolver just saved
        # being told to fetch the document again.
        check("and given the command that refetches it",
              "gws drive files export -o paper-notes.md" in stale, True)
        drive(P1="2026-09-01T00:00:00.000Z", L1="2026-09-01T00:00:00.000Z",
              T1="2026-09-01T00:00:00.000Z", N1="2026-09-01T00:00:00.000Z",
              D1="2026-09-01T00:00:00.000Z")

        print("\n== a document longer than the cache keeps ==")
        cache("board-minutes.md", "Long minutes. " + "filler " * 4000,
              file_id="B1", modified="2026-09-01T00:00:00.000Z")
        drive(B1="2026-09-01T00:00:00.000Z")
        long_one = resolved("what is in the board minutes?")
        check("only the first 20 000 characters are there",
              len(long_one) < 21000, True, f"{len(long_one)} chars")
        # `read_cache` would have handed over the same prefix. The line is
        # here because a document that stops mid-sentence otherwise reads as
        # one that ends there.
        check("and it says so, rather than reading as a document that ends",
              "cut off at the end" in long_one, True)
        # `CUT_OFF` is glued onto the end of the note, so it is the one place
        # the note is built rather than written, and the one place a sentence
        # could arrive after the conditions and undo them.
        check("the cut-off sentence does not put an unconditional one back",
              unconditional(long_one.split("\n\n")[0]), [])
        (Path(scratch) / "board-minutes.md").unlink()

        print("\n== when the read fails after the name resolved ==")
        # The file went between `named` reading the directory and `read_cache`
        # opening it, or it stopped being readable. Neither is something the
        # sub-agent can act on, so the question goes over as it arrived.
        real_read = gws_cache.read_cache

        async def gone(name):
            return {"name": name, "error": "No cached file by that name."}

        gws_cache.read_cache = gone
        check("the question is handed over untouched",
              asyncio.run(gws._prefetch("read paper-notes.md")), None)
        gws_cache.read_cache = real_read

        print("\n== a copy with nothing to check against ==")
        # Five of the ten real records have no `file_id`, so `_is_stale`
        # returns False without a subprocess. That is the pre-existing rule —
        # served as it is — and the resolver inherits it rather than refusing.
        drive()
        check("no file id means no check, and the copy is served",
              "Another copy of the itinerary."
              in resolved("read exported_itinerary.txt to me"), True)

        print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()
