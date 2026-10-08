"""Check what a Workspace card is allowed to say it read.

The source row under a `gws` answer is the one part of that card the owner is
meant to be able to trust without checking, which makes every way of getting it
wrong worse than not having it. A card that names the wrong file is a card that
has told the household something false about their own documents; a card that
names six of the forty a search matched has told them the answer weighs up six
files when it weighed up none of them. The prose above the row is the model's
and is read as the model's. The row is the program's.

So the rulings, each of which is a way this has already been got wrong on
paper:

- **A listing is not provenance.** `drive files list` matching forty files
  means a question that has not read anything yet. Only a search short enough
  to be its own answer stands in for one, and only while nothing was fetched:
  once the question went back for a file by name, the listing is the lookup
  table that holds its title and nothing more. That has to survive both ways
  round — the long search must contribute nothing on its own, and the name it
  carries must still reach the card after the fetch, because `files export`
  prints the document and never its title.
- **A sender is required and a subject is not.** `users messages list` returns
  ids, which is a command that found messages without reading one, and a
  message with a subject and no sender is that command's output dressed up.
  The subject is also the most disclosive string this system handles and the
  sender is the half that is readable across a room, so the required half is
  the safe half — see `cards.js` for what the panel does with each.
- **Silence and absence are different answers.** `[]` is "looked, could name
  nothing" and gets said out loud; `None` is "nothing of the kind was touched"
  and draws no row. Collapse them and either every calendar answer carries a
  "no source" line until the household stops reading the line, or a Drive
  answer that rests on nothing looks exactly like one that rests on a file.
- **Bookkeeping must not be able to take the answer down.** `record` runs on a
  command that has already succeeded, and a response shaped in a way nobody
  anticipated is the ordinary case. Every one of these is fed something
  malformed and asked to stay quiet about it.

Nothing here starts a process, opens a socket or needs an account: a `gws`
command is a list of strings and its output is a string, and both are typed
out below. `_sources.record` is the only thing under test that a real run
reaches through the CLI, and `wiring_check` is what checks that it does.

    uv run python tools/sources_check.py

Exits non-zero on the first case that does not hold.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ai_terminal.live.tools import _sources  # noqa: E402

# Counted rather than written down, so the closing line cannot drift from the
# number of cases the way a figure typed into a commit message does.
RAN = 0


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got!r}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


def a_doc(key: str, name: str, when: str = "2026-09-04T11:02:00.000Z") -> dict:
    return {"id": key, "name": name, "modifiedTime": when,
            "mimeType": "application/vnd.google-apps.document"}


def a_mail(key: str, who: str, subject: str) -> dict:
    """One message the way `gws gmail messages get` prints it.

    Through `payload.headers` rather than as flat keys, because that is the
    shape the API returns and the flat one is the convenience some subcommands
    add on top. Both are read; this is the one that has to work.
    """
    return {"id": key, "threadId": f"t-{key}", "payload": {"headers": [
        {"name": "From", "value": who},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Fri, 4 Sep 2026 11:02:00 +0900"},
    ]}}


def asked(*commands) -> list | None:
    """One question's worth of CLI traffic, and what the card may name.

    Each argument is `(argv, stdout)` in the order the sub-agent ran them,
    which is the only thing the ledger sees: it is handed no question, no
    answer and no model output, on purpose. A third element says the
    command failed, which is what `_gws_run` passes as `ok`.
    """
    with _sources.collecting() as found:
        for argv, stdout, *failed in commands:
            _sources.record(argv, "" if failed else stdout,
                            ok=not failed)
    return _sources.collected(found)


def names(sources) -> list:
    return [s["name"] for s in sources or []]


# ------------------------------------------------------- one file, and which

def file_cases() -> None:
    print("== the file an answer came out of ==")

    got = asked((["drive", "files", "get", "--id", "1abc"],
                 json.dumps(a_doc("1abc", "Portland trip itinerary"))))
    check("a file fetched by name is what the card names", got,
          [{"kind": "doc", "name": "Portland trip itinerary",
            "detail": "2026-09-04"}])

    # The CLI writes a line about its keyring backend ahead of the payload —
    # `_gws_cli.run_json` starts its parse at the first brace for exactly that
    # reason, and `_run_sync` hands `record` the same stdout. Parsed from the
    # first byte it raises, which reads here as an answer that came from
    # nowhere: the pill disappears and the card looks the same as an honest
    # `[]`, on whichever machines print the line and on no others.
    noisy = asked((["drive", "files", "get", "--id", "1abc"],
                   "Using keyring backend: SecretService\n"
                   + json.dumps(a_doc("1abc", "Portland trip itinerary"))))
    check("a line of the CLI's own ahead of the payload still names the file",
          noisy, [{"kind": "doc", "name": "Portland trip itinerary",
                   "detail": "2026-09-04"}])

    # A top-level list is the other shape `record` takes, and the bracket can
    # come before any brace, so the skip is to whichever opens first rather
    # than to the brace `run_json` looks for.
    listed = asked((["drive", "files", "list"],
                    "Using keyring backend: SecretService\n"
                    + json.dumps([a_doc("1abc", "Portland trip itinerary")])))
    check("and a listing behind the same line is read as a listing",
          names(listed), ["Portland trip itinerary"])
    # The date and not the timestamp, and not "5 days ago" either: a relative
    # phrase needs a clock and a language, and every other card here draws the
    # server's own string and translates only its own chrome.
    check("dated to the day, as the API spells it",
          got[0]["detail"], "2026-09-04")

    kinds = asked((["drive", "files", "list"], json.dumps({"files": [
        {"id": "s1", "name": "Budget",
         "mimeType": "application/vnd.google-apps.spreadsheet"},
        {"id": "p1", "name": "Offsite",
         "mimeType": "application/vnd.google-apps.presentation"},
        {"id": "z1", "name": "scan.pdf", "mimeType": "application/pdf"},
    ]})))
    check("a sheet, a deck and a PDF are three different things to a reader",
          [s["kind"] for s in kinds], ["sheet", "slides", "file"])

    skipped = asked((["drive", "files", "list"], json.dumps({"files": [
        {"id": "f1", "name": "Trips",
         "mimeType": "application/vnd.google-apps.folder"},
        {"id": "n1", "mimeType": "application/vnd.google-apps.document"},
        {"id": "d1", "name": "Flights",
         "mimeType": "application/vnd.google-apps.document"},
    ]})))
    # A folder is a place to look and not a thing an answer comes out of, and
    # an object with no name is one the row could only render as a blank pill.
    check("a folder and a nameless file are not sources", names(skipped),
          ["Flights"])

    long_name = "Portland " * 40
    capped = asked((["drive", "files", "get", "--id", "1abc"],
                    json.dumps(a_doc("1abc", long_name))))
    check("and a pathological name is bounded before it reaches the socket",
          len(capped[0]["name"]), _sources.MAX_TEXT,
          f"{_sources.MAX_TEXT} characters, clipped again by the card")


# --------------------------------------------------- a search is not a source

def search_cases() -> None:
    print("\n== what a search may and may not claim ==")

    short = json.dumps({"files": [a_doc("a", "Flights"),
                                  a_doc("b", "Hotels"),
                                  a_doc("c", "Packing")]})
    check("a search short enough to be its own answer stands in for one",
          names(asked((["drive", "files", "list", "--query", "trip"], short))),
          ["Flights", "Hotels", "Packing"])

    many = json.dumps({"files": [a_doc(f"k{i}", f"Note {i}")
                                 for i in range(40)]})
    check("a search that matched forty names none of them",
          asked((["drive", "files", "list", "--query", "note"], many)), [],
          "and says so, rather than picking six and looking certain")

    # The two halves of the same ruling. The listing is still remembered, and
    # the fetch is what promotes exactly one line of it — which is the only way
    # the card can have a title at all, since the export prints the document.
    after = asked(
        (["drive", "files", "list", "--query", "note"], many),
        (["drive", "files", "export", "--id", "k7", "-o", "note.md"],
         "# Note 7\n\nthe body of the document, not its title"),
    )
    check("but the one it went back for is named, out of the listing",
          names(after), ["Note 7"],
          "`files export` prints the document; the title is only upstream")

    both = asked(
        (["drive", "files", "list", "--query", "trip"], short),
        (["drive", "files", "get", "--id", "b"], json.dumps(
            a_doc("b", "Hotels"))),
    )
    check("and once something was fetched the rest of the search drops away",
          names(both), ["Hotels"],
          "three files went past; the answer came out of one")

    # `--id` on the command line and the same id inside a `--params` blob are
    # the same request written two ways, and the sub-agent writes both.
    blob = asked(
        (["drive", "files", "list", "--query", "note"], many),
        (["drive", "files", "get", "--params", '{"fileId": "k3"}'],
         json.dumps({"id": "k3", "name": "Note 3"})),
    )
    check("however the sub-agent spelled the id", names(blob), ["Note 3"])

    # A refusal is an ordinary answer here, not an exotic one: `files
    # export` turns down anything that is not a Google format, which is
    # every PDF and every `.docx` a search can match. The id is on the
    # command line either way, and the listing in front of it holds a
    # title for it — so without the `ok` guard the card names the one
    # file the answer certainly did not come out of, and names it alone,
    # because a fetch outranks the search it came from.
    refused = asked(
        (["drive", "files", "list", "--query", "note"], many),
        (["drive", "files", "export", "--id", "k7", "-o", "note.md"],
         "Error: this file cannot be exported", "failed"),
    )
    check("a file the export refused is not what the answer came from",
          refused, [],
          "it looked and read nothing, which is not the same as read this")

    survived = asked(
        (["drive", "files", "list", "--query", "trip"], short),
        (["drive", "files", "export", "--id", "a", "-o", "f.md"],
         "Error: cannot export", "failed"),
        (["drive", "files", "export", "--id", "b", "-o", "h.md"],
         "# Hotels\n\nthe body"),
    )
    check("and the one that did come back is named on its own",
          names(survived), ["Hotels"],
          "two exports, one refusal; the card names what it read")


# ------------------------------------------------------ a thread, on a wall

def mail_cases() -> None:
    print("\n== the message an answer came out of ==")

    got = asked((["gmail", "messages", "get", "--message-id", "m1"],
                 json.dumps(a_mail("m1", "ANA <no-reply@ana.co.jp>",
                                   "Your booking is confirmed"))))
    check("a thread is named by who sent it", got,
          [{"kind": "mail", "name": "ANA <no-reply@ana.co.jp>",
            "detail": "Your booking is confirmed"}])

    ids_only = asked((["gmail", "messages", "list", "--query", "ana"],
                      json.dumps({"messages": [{"id": "m1", "threadId": "t1"},
                                               {"id": "m2",
                                                "threadId": "t2"}]})))
    # The command that found messages without reading one. It leaves `[]` and
    # not `None`: Gmail was searched, so the card owes the household a line.
    check("a search that returned ids and nothing else names nobody",
          ids_only, [], "looked, and can name no one — which the card says")

    headless = asked((["gmail", "messages", "get", "--message-id", "m9"],
                      json.dumps({"id": "m9", "subject": "Re: the invoice"})))
    check("nor does a subject with no sender behind it", headless, [],
          "the safe half is the required half")

    flat = asked((["gmail", "threads", "get", "--id", "t1"], json.dumps({
        "threads": [{"id": "t1", "from": "Sam <sam@example.com>",
                     "subject": "school run"}]})))
    check("and the flattened shape some subcommands print is read too",
          flat, [{"kind": "mail", "name": "Sam <sam@example.com>",
                  "detail": "school run"}])

    # `+triage` describes a message in full and a later `get` with a fields
    # mask returns a thinner copy of it. Last-writer-wins traded a subject for
    # nothing; the first sighting is the rich one.
    thin = asked(
        (["gmail", "+triage"], json.dumps({"messages": [
            a_mail("m1", "ANA <no-reply@ana.co.jp>", "Your booking")]})),
        (["gmail", "messages", "get", "--message-id", "m1",
          "--fields", "id,payload/headers"],
         json.dumps({"id": "m1", "from": "ANA <no-reply@ana.co.jp>"})),
    )
    check("a thinner second look does not overwrite the first",
          thin, [{"kind": "mail", "name": "ANA <no-reply@ana.co.jp>",
                  "detail": "Your booking"}])


# ------------------------------------------ the copy that ran no command

def a_header(**fields) -> dict:
    """A cached file's header, the way `_meta_fields` writes one.

    Four fields out of Drive's own answer, plus the command that fetched it.
    Written as a dict here because that is what `_read_header` hands back;
    `cache_check` is what pins that it parses out of the file that way.
    """
    return {"source": "gws drive files get --id 1abc -o trip.md",
            "fetched": "2026-09-04T12:00:00+09:00",
            "file_id": "1abc", "title": "Portland trip itinerary",
            "mime_type": "application/vnd.google-apps.document",
            "modified": "2026-09-04T11:02:00.000Z"} | fields


def read(*headers) -> list | None:
    """One question that answered out of the cache, and what it may name."""
    with _sources.collecting() as found:
        for header in headers:
            _sources.cached(header)
    return _sources.collected(found)


def warm_cases() -> None:
    print("\n== and the copy that was already on the disk ==")

    # The case the whole function exists for. `_prefetch` splices a cached
    # document into the first turn and runs no `gws` command, so `record` never
    # sees it — and the card the owner is looking at is the one answered out of
    # a named file.
    check("a document read out of the cache still names itself",
          read(a_header()),
          [{"kind": "doc", "name": "Portland trip itinerary",
            "detail": "2026-09-04"}],
          "the header, not the CLI: the warm path has no argv to read")
    check("and it counts as fetched, not as something that went past",
          read(a_header(),
               a_header(file_id="2xyz", title="Hotel confirmations")),
          [{"kind": "doc", "name": "Portland trip itinerary",
            "detail": "2026-09-04"},
           {"kind": "doc", "name": "Hotel confirmations",
            "detail": "2026-09-04"}],
          "two cached reads are two sources, and neither is a listing")

    for kind, mime in (("sheet", "application/vnd.google-apps.spreadsheet"),
                       ("slides", "application/vnd.google-apps.presentation"),
                       ("file", "application/pdf")):
        check(f"a cached {kind} gets the {kind} glyph",
              read(a_header(mime_type=mime))[0]["kind"], kind)

    # A Gmail attachment names no document id, so `_meta_fields` writes it the
    # first two fields and nothing else. There is nothing to put on a pill.
    check("a copy with no title in its header names nothing",
          read({"source": "gws gmail attachments get", "fetched": "x"}), [],
          "[], not None — a document was read, it just cannot be named")
    check("and the same header twice is one pill",
          len(read(a_header(), a_header())), 1,
          "the PDF and the text copy beside it carry the same file_id")

    check("a header that is not a dict of strings raises nothing",
          read({"title": None, "mime_type": 7, "file_id": []}), [])
    check("and reading with no question open does nothing at all",
          _sources.cached(a_header()), None,
          "`gws_bench` and the tests reach `read_cache` with no card to draw")


# ------------------------------------------- silence, absence and bad input

def quiet_cases() -> None:
    print("\n== the three answers, and staying out of the way ==")

    check("a question that touched no file and no thread answers None",
          asked((["calendar", "events", "list", "--days", "1"],
                 json.dumps([{"summary": "dentist"}]))), None,
          "an event is not a source; it is the answer")
    check("mixing one in does not change that",
          names(asked(
              (["calendar", "events", "list"], "[]"),
              (["drive", "files", "get", "--id", "1abc"],
               json.dumps(a_doc("1abc", "Flights"))))),
          ["Flights"])

    check("a Drive command that printed nothing readable answers []",
          asked((["drive", "files", "get", "--id", "1abc"], "")), [],
          "which is not the same as having read nothing")

    for label, stdout in (("a table", "NAME       MODIFIED\nnotes.md   Sep 4"),
                          ("a document", "# Notes\n\nbody"),
                          ("a bare number", "7"),
                          ("a JSON string", '"done"'),
                          ("a list of strings", '["a", "b"]'),
                          ("half a response", '{"files": [{"id": "a"')):
        check(f"{label} on stdout names nothing, and raises nothing",
              asked((["drive", "files", "list"], stdout)), [], "[]")

    # Every tool here is reachable from `tools/gws_bench.py` and from a test,
    # and neither has a card to draw on. Recording outside a question is not an
    # error, it is the ordinary case for those two.
    _sources.record(["drive", "files", "list"], json.dumps({"files": []}))
    check("and recording with no question open does nothing at all",
          _sources.collected(None), None)

    over = json.dumps({"files": [a_doc(f"k{i}", f"Note {i}")
                                 for i in range(3)]})
    fetched = [(["drive", "files", "get", "--id", f"k{i}"],
                json.dumps(a_doc(f"k{i}", f"Note {i}")))
               for i in range(10)]
    capped = asked((["drive", "files", "list"], over), *fetched)
    check("ten files read is still one card's worth of row",
          len(capped), _sources.MAX_SOURCES,
          f"{_sources.MAX_SOURCES} pills, which is already two rows")

    on_wire = asked((["drive", "files", "get", "--id", "1abc"],
                     json.dumps(a_doc("1abc", "Flights"))))
    check("and what goes on the wire is three keys, not the ledger's row",
          sorted(on_wire[0]), ["detail", "kind", "name"],
          "no id, no `broad` — the card has no use for either")


def main() -> None:
    file_cases()
    search_cases()
    mail_cases()
    warm_cases()
    quiet_cases()
    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()
