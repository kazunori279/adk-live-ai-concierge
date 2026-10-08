"""Check what `agenda` says about a day it cannot be asked about.

Seven parts, none of them things the real account can demonstrate on demand.

Whose calendars get read is the first, and it is the one thing here that can
only be checked against a fixture. `calendarList.list` answers with whatever
this Google account happens to be subscribed to, so the roles the filter turns
on — `owner` against `reader`, `freeBusyReader`, `writer` — arrive together
only in a list written out by hand. What the fixture is for is the direction of
the mistake: a filter that also dropped a second calendar the person owns loses
half their day and says "nothing on" about it, which is the failure nobody
reports because it looks like an empty Tuesday.

`_overlaps` decides which pairs of times are worth saying out loud, and its
mistakes are all quiet ones: a pair reported twice under two names, a reminder
reported as an appointment butting up against the meeting containing it. The
counts beside the lists make those visible in a way the lists themselves did
not, which is why they are worth pinning.

Two appointments that look alike is the third, and it is the one part here
whose failure is silence rather than a wrong answer. Nothing between `_shape`
and `_overlaps` may drop a row: an hour holding two entries a person cannot
tell apart from the outside — two children, one word on both — is exactly the
hour they most need read out, and a pass that folded the pair into one row took
the clash out of `overlaps` on the way. `_read` is driven end to end here
rather than the pair being handed straight to `_overlaps`, because what has to
hold is that the whole tool still reports it.

`_Budget` bounds a fan-out that pages, and the interesting numbers are the ones
nobody has a calendar big enough to reach. The fake below returns a full page
every time, so seven calendars page until something stops them, and the stated
ceiling — `calendars × PAGE_SIZE` plus a page — is arithmetic this can check
rather than a claim in a docstring.

The budget's walls are the third, and `agenda` meets them from a side no other
tool does: it is charged for and never refused, so it arrives at one holding
the events while the refusal in its hand says nothing was run. Reaching a wall
takes twelve calls or a spent clock, which is not a state a live question can
be put into on purpose.

The fourth is what the card is handed. `showing` is a ContextVar holding a
mutable dict, and both halves of that are load-bearing in a way nothing shows:
`agenda` runs inside the sub-agent's task, `asyncio.create_task` snapshots the
context, and a `set` in there never reaches the frame `gws` pushes on the way
out. The cases below cross that boundary on purpose, because the version that
rebinds passes every test that stays on one task and draws an empty card on the
panel.

The seventh is the day a multi-day event is drawn on, and it is the one part
here whose failure is a screen rather than a value. The tool files an event
under the day it starts, which is right for the sub-agent and wrong for a card
grouped by day: a three-day trip asked about on its second day heads a block
dated before the window that was asked about, and the row that is happening
right now is not on today's block, so nothing lights. The window has to be
handed in for any of it to be checkable, which is why these drive
`_for_screen` directly rather than `_remember`.

    uv run python tools/agenda_check.py

Exits non-zero on the first case that does not hold.
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ai_terminal.live.tools import _agenda, _gws_cli, _gws_run  # noqa: E402

DAY = "2026-09-08"

# Small enough that the fan-out below is eight round trips instead of eight
# real pages. The ceiling being checked is a formula in `PAGE_SIZE` and
# `MAX_EVENTS`, so it holds at any size; 250 and 300 only make it slow.
CALENDARS = 7
PAGE_SIZE = 10
MAX_EVENTS = 12


def ev(start: str, end: str, title: str) -> dict:
    """One shaped event, the way `_shape` would have left it."""
    lo = datetime.fromisoformat(f"{DAY}T{start}:00")
    hi = datetime.fromisoformat(f"{DAY}T{end}:00")
    return {"day": DAY, "from": start, "to": end, "title": title,
            "_span": (lo, hi)}


def allday(title: str) -> dict:
    """A birthday or a public holiday, shaped the way `_when` leaves one.

    Deliberately not built by :func:`ev`, which always produces a `_span`: the
    thing worth pinning about an all-day entry is that it *has no* `_span`, and
    a helper that cannot express that cannot express the case.
    """
    return {"day": DAY, "all_day": True, "title": title}


def overlaps(events: list[dict]) -> _agenda._Pairs:
    """`_overlaps`, with a raised exception reported as a failed case.

    The all-day case's regression is a `KeyError`, not a wrong pair, and a
    traceback names the line it came from rather than the case that provoked
    it. This keeps the failure inside the report. Every case below goes through
    here and not through `_agenda._overlaps`: which of them a future exclusion
    breaks by raising is not something the cases can be sorted by in advance.
    """
    try:
        return _agenda._overlaps(events)
    except Exception as err:
        check(f"_overlaps walks {len(events)} events without raising", False,
              repr(err))


# Counted rather than written down, so the closing line cannot drift from the
# number of cases.
RAN = 0


def check(name: str, ok: bool, detail: str, got=None, want=None) -> None:
    global RAN
    RAN += 1
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        if want is not None:
            print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


def pairs() -> None:
    print("\n== the pairs a list of times hides ==")

    # A long entry covering two shorter ones, which is the ordinary busy
    # morning: three clashes and not one, because the walk is over pairs and
    # nothing about being inside something else takes an event out of it.
    got = overlaps([
        ev("09:00", "18:00", "Offsite"),
        ev("14:00", "15:00", "Standup"),
        ev("14:30", "15:30", "Dentist"),
    ])
    check("a long entry clashes with everything inside it, pair by pair",
          got.clashes_found == 3
          and ["2026-09-08 14:00 Standup", "2026-09-08 14:30 Dentist"]
          in got.clashes,
          f"{got.clashes_found} clash(es): {got.clashes}",
          got.clashes,
          "3 clashes, one of them the stand-up against the dentist")

    got = overlaps([
        ev("16:00", "18:00", "Meeting"),
        ev("16:00", "16:00", "Reminder"),
    ])
    check("a zero-length reminder is neither", not got.clashes_found
          and not got.touching_found, f"{got.touching}{got.clashes}")

    # The other exclusion, and the one whose regression is not a wrong pair but
    # an exception inside a live tool call: `_when` returns an all-day entry
    # with no `_span` at all, so a walk that reached one would read a key that
    # is not there. `_read` sorts by `(day, from)` and an all-day entry has no
    # `from`, so it heads its day and is the first thing the walk would touch.
    got = overlaps([
        allday("Public holiday"),
        ev("09:00", "10:00", "Stand-up"),
        ev("09:30", "10:30", "Review"),
    ])
    check("an all-day entry is dropped, not walked",
          got.clashes_found == 1 and not got.touching_found,
          f"{got.clashes_found} clash(es): {got.clashes}",
          got.clashes, "just the stand-up against the review")

    # Fifteen chained half-hours: fourteen joins, twelve of them listed. This
    # is the number `back_to_back_total` exists to carry.
    chain = [ev(f"{9 + i // 2:02d}:{'30' if i % 2 else '00'}",
                f"{9 + (i + 1) // 2:02d}:{'00' if i % 2 else '30'}",
                f"Meeting {i}") for i in range(15)]
    got = overlaps(chain)
    check("a long chain counts past the cut",
          got.touching_found == 14 and len(got.touching) == _agenda.MAX_OVERLAPS,
          f"{got.touching_found} joins, {len(got.touching)} listed")

    # And the same on the other branch, which is the field this issue was
    # opened about. Both counters are incremented outside the `len(...)` guard
    # and only one of them was pinned there: move `clashed += 1` inside the
    # guard and the chain above still holds, because it is `touched` that
    # counts on it.
    #
    # Fifteen 45-minute calls half an hour apart, so each runs into the next
    # and no further: 30 < 45 puts the next one inside this one, 60 > 45 keeps
    # the one after that out, and out by fifteen minutes rather than exactly,
    # which would make it an adjacency instead.
    start = datetime.fromisoformat(f"{DAY}T09:00:00")
    ladder = []
    for i in range(15):
        lo = start + timedelta(minutes=30 * i)
        ladder.append(ev(lo.strftime("%H:%M"),
                         (lo + timedelta(minutes=45)).strftime("%H:%M"),
                         f"Call {i}"))
    got = overlaps(ladder)
    check("and so does a long run of clashes",
          got.clashes_found == 14 and len(got.clashes) == _agenda.MAX_OVERLAPS
          and not got.touching_found,
          f"{got.clashes_found} clashes, {len(got.clashes)} listed, "
          f"{got.touching_found} joins")


class Feed:
    """One primary calendar, holding exactly the raw events handed over.

    `Roster` below answers with one invented event per calendar, because what
    it is for is *which* calendars were asked. This one is for what comes back
    off the single calendar that is theirs, so the whole of `_read` — shape,
    sort, overlap walk — runs over a day written out here.
    """

    def __init__(self, items: list[dict]):
        self.items = items

    def __call__(self, argv, cwd=None, timeout=_gws_cli.TIMEOUT):
        if argv[1] == "calendarList":
            return {"items": [{"id": "them@example.com", "summary": "Alex",
                               "primary": True}]}
        return {"items": self.items}


def raw(start: str, end: str, day: str = DAY, **fields) -> dict:
    """One `events.list` item, the way the API hands one over.

    Offsets from the host's own zone rather than a written-in `+09:00`, so a
    machine that is not in Tokyo reads the same hours back out of `_when` as
    :func:`ev` puts in.

    `day` is which day it lands on, for the cases in :func:`ordered` that need
    a window wider than one.
    """
    tz = datetime.now().astimezone().tzinfo
    lo = datetime.fromisoformat(f"{day}T{start}:00").replace(tzinfo=tz)
    hi = datetime.fromisoformat(f"{day}T{end}:00").replace(tzinfo=tz)
    return {"start": {"dateTime": lo.isoformat()},
            "end": {"dateTime": hi.isoformat()}, **fields}


def served(items: list[dict], days: int = 1) -> dict:
    """What `agenda` answers for a primary calendar holding `items`.

    `days` is the width of the window, counted from :data:`DAY`.
    """
    real = (_agenda._gws_cli.run_json, _agenda._CALENDARS)
    try:
        _agenda._gws_cli.run_json = Feed(items)
        _agenda._CALENDARS = None
        return _agenda._read(DAY, days)
    finally:
        _agenda._gws_cli.run_json, _agenda._CALENDARS = real


def copies() -> None:
    print("\n== two appointments that look alike ==")

    # Two children, one teacher, the same hour booked twice. Nothing about the
    # pair is a copy of anything — two rooms, two attendee lists, and an
    # invitation on the second that nobody has answered — and there is no
    # second calendar for a copy to have come off, because only `primary` is
    # read. Every field the two share is a field a household shares.
    twice = served([
        raw("16:00", "17:00", summary="Piano lesson",
            location="Aoba Music, Studio A",
            attendees=[{"email": "them@example.com", "self": True,
                        "responseStatus": "accepted"},
                       {"email": "hana@example.com",
                        "responseStatus": "accepted"}]),
        raw("16:00", "17:00", summary="Piano lesson",
            location="Aoba Music, Studio B",
            attendees=[{"email": "them@example.com", "self": True,
                        "responseStatus": "needsAction"},
                       {"email": "sora@example.com",
                        "responseStatus": "accepted"}]),
    ])
    check("both of two same-named appointments in one hour reach the answer",
          twice["count"] == 2 and len(twice["events"]) == 2,
          f"{twice['count']} of 2", twice["events"], "two rows")
    want = [[f"{DAY} 16:00 Piano lesson", f"{DAY} 16:00 Piano lesson"]]
    check("and the clash is on the answer, which is what to lead with",
          twice["overlaps"] == want, f"{twice['overlaps']}",
          twice["overlaps"], want)
    rooms = sorted(e.get("where", "") for e in twice["events"])
    both = ["Aoba Music, Studio A", "Aoba Music, Studio B"]
    check("with each one's own room still on it", rooms == both,
          f"{rooms}", rooms, both)
    # The field with something to do about it, and the one a merge that keeps
    # the first row loses in silence: the answered lesson is first.
    unanswered = [e.get("your_rsvp") for e in twice["events"]]
    check("and the invitation nobody has answered still asking",
          unanswered.count("needsAction") == 1, f"{unanswered}",
          unanswered, "one needsAction")

    # The same hour with no name on either, which is the case a merge keyed on
    # the title has least to go on and folded hardest: both sides read
    # "untitled" and the pair became one row.
    nameless = served([raw("09:00", "09:30"), raw("09:00", "09:30")])
    check("two nameless entries over the same half hour are still two",
          nameless["count"] == 2 and len(nameless["overlaps"]) == 1,
          f"{nameless['count']} events, {nameless['overlaps']}",
          nameless["count"], 2)

    # The control, and the reason the counts above are not just "nothing is
    # ever merged": a pair that already survived because their ends differ has
    # to keep surviving, and keep reading as one clash rather than two rows
    # nobody joins up.
    ragged = served([
        raw("16:00", "17:00", summary="Piano lesson"),
        raw("16:00", "16:30", summary="Piano lesson"),
    ])
    check("and a pair that starts together and ends apart is unchanged",
          ragged["count"] == 2 and len(ragged["overlaps"]) == 1,
          f"{ragged['count']} events, {len(ragged['overlaps'])} clash(es)",
          ragged["count"], 2)


def ordered() -> None:
    print("\n== the order the rows come back in ==")

    # The card is what makes this a contract rather than a preference.
    # `calendarHtml` groups the rows into date headings by run length -- a new
    # heading wherever the day changes from the row before it -- so `_read`
    # owes it two separate things: the rows ascending, and each day in one
    # run. `events.sort` at `_agenda.py:540` is the only thing that supplies
    # either, and before these three cases nothing here would have noticed if
    # it stopped.

    # Ascending, whatever order they arrived in. `events.list` is asked for
    # `orderBy=startTime` (`_agenda.py:262`), so a descending feed is not
    # something the API sends today; the sort is what keeps that from being
    # the thing the card depends on.
    desc = served([raw("18:00", "19:00", summary="Dinner"),
                   raw("09:00", "10:00", summary="Standup")])
    order = [e.get("from") for e in desc["events"]]
    rising = ["09:00", "18:00"]
    check("a descending feed comes back in time order", order == rising,
          f"{order}", order, rising)

    # Two days interleaved, which is the shape a fan-out over more than one
    # calendar produces: `_read` appends each calendar's whole window in turn
    # (`_agenda.py:510-533`), so two calendars holding the same fortnight
    # arrive as one fortnight after another. Only `primary` is read today
    # (`_agenda._theirs`), so this feed comes from the fixture and not from
    # the API -- and it is the shape that draws a single day twice.
    mixed = served([
        raw("09:00", "10:00", summary="Standup"),
        raw("09:00", "10:00", day="2026-09-10", summary="Dentist"),
        raw("18:00", "19:00", summary="Dinner"),
    ], days=3)
    got = [e["day"] for e in mixed["events"]]
    grouped = [DAY, DAY, "2026-09-10"]
    check("and a day that arrived in two pieces comes back in one",
          got == grouped, f"{got}", got, grouped)

    # Two rows the sort key cannot tell apart. `list.sort` is stable, so a tie
    # keeps the order the calendar gave them, and that is the whole of the
    # rule -- there is nothing else on a row this answer is entitled to order
    # by. Without it the same question twice is two different cards:
    # `_for_screen` cuts the card at `SCREEN_EVENTS` in list order
    # (`_agenda.py:678`), so a tie across that boundary shows a different one
    # of the two each time it is asked.
    # Six of them and not two: a tiebreak that shuffles reproduces two rows in
    # their arrival order half the time and six of them once in 720, so the
    # case says what it means rather than saying it on a coin toss.
    fed = ["Dentist", "Bank", "Piano", "Swim", "Recycling", "Haircut"]
    tied = served([raw("10:00", "11:00", summary=what) for what in fed])
    given = [e.get("title") for e in tied["events"]]
    check("rows in the same minute keep the order the calendar gave them",
          given == fed, f"{given}", given, fed)


# The shape of a real `calendarList.list` reply, anonymised. Written out from
# the household account's own seven rows on 2026-09-10 rather than invented,
# because the thing this roster exists to pin is a fact about that reply that
# nobody would guess: **four of the seven come back as `accessRole: owner` and
# only one of the four is theirs.**
#
# `owner` is the role Calendar grants when a calendar is shared with full
# rights. It does not mark the creator, and there is no field on the resource
# that does. `KIN` below is another person's default calendar handed over at
# `owner`, and it is the row that makes `accessRole == "owner"` the wrong
# filter and `primary` the right one — see `_agenda._theirs`.
ROSTER = [
    {"id": "them@example.com", "summary": "Alex", "accessRole": "owner",
     "primary": True},
    {"id": "kin@example.com", "summary": "Shu", "accessRole": "owner"},
    {"id": "family@group.calendar.google.com", "summary": "Family",
     "accessRole": "owner"},
    {"id": "side@group.calendar.google.com", "summary": "Projects",
     "accessRole": "owner"},
    {"id": "work@example.com", "summary": "Work",
     "accessRole": "freeBusyReader"},
    {"id": "team@example.com", "summary": "Team", "accessRole": "writer"},
    {"id": "ja.japanese#holiday@group.v.calendar.google.com",
     "summary": "Holidays in Japan", "accessRole": "reader"},
]

# Somebody else's default calendar, shared at `owner`. Their RSVPs are on it
# and none of them are the person asking.
KIN = ROSTER[1]
MINE = ROSTER[0]
# `ROSTER[3]` is a second calendar of their own, also `owner`, and nothing in
# the reply tells it apart from `KIN`. Dropping it is what the narrow reading
# costs; the owner accepted that cost after being shown their own list.


class Roster:
    """`_gws_cli.run_json` over a fixed calendar list, recording the fan-out.

    What the events look like does not matter here. Which calendars were asked
    for them does, and that is the half a filter applied one function too late
    still gets right in the answer while having paid for all seven round trips
    on the way to it.
    """

    def __init__(self, items: list[dict]):
        self.items = items
        self.asked: list[str] = []
        self.fields = ""

    def __call__(self, argv, cwd=None, timeout=_gws_cli.TIMEOUT):
        if argv[1] == "calendarList":
            got = json.loads(argv[argv.index("--params") + 1])
            self.fields = got["fields"]
            return {"items": self.items}
        params = json.loads(argv[argv.index("--params") + 1])
        self.asked.append(params["calendarId"])
        return {"items": [{
            "summary": f"on {params['calendarId']}",
            "start": {"dateTime": f"{DAY}T10:00:00+09:00"},
            "end": {"dateTime": f"{DAY}T11:00:00+09:00"},
        }]}


def owned() -> None:
    print("== whose calendars get read ==")
    mine = ["them@example.com"]
    real = (_agenda._gws_cli.run_json, _agenda._CALENDARS)
    try:
        fake = Roster(ROSTER)
        _agenda._gws_cli.run_json = fake
        _agenda._CALENDARS = None
        kept = [cal["id"] for cal in _agenda._calendars()]
        check("only their own calendar comes back", kept == mine,
              f"{len(kept)} of {len(ROSTER)}", kept, mine)
        # The case the whole filter turns on, named on its own because the
        # count above holds for `accessRole == "owner"` on a roster where
        # nobody has shared anything. Here three rows are `owner` and none of
        # the three is theirs; a filter reading the role keeps all four.
        role_kept = [cal["id"] for cal in ROSTER
                     if cal.get("accessRole") == "owner"]
        check("and the three others holding owner rights on it do not",
              [c for c in role_kept if c not in kept]
              == ["kin@example.com", "family@group.calendar.google.com",
                  "side@group.calendar.google.com"],
              f"{len(role_kept)} rows read owner, {len(kept)} kept")
        # Named by role rather than by count, because the count holds just as
        # well for a filter that kept the wrong one.
        dropped = sorted(cal["accessRole"] for cal in ROSTER
                         if cal["id"] not in kept)
        want = ["freeBusyReader", "owner", "owner", "owner", "reader",
                "writer"]
        check("and shared-to-them and subscribed are gone with them",
              dropped == want, f"{dropped}", dropped, want)

        # The point of filtering in `_calendars` and not in `_read`. Dropping
        # the events afterwards gives the same answer, having spent six
        # subprocesses and the slowest of their round trips to get it.
        _agenda._CALENDARS = None
        fake.asked.clear()
        out = _agenda._read(DAY, 1)
        check("and no query is issued against the rest",
              sorted(fake.asked) == sorted(mine), f"{sorted(fake.asked)}",
              sorted(fake.asked), sorted(mine))
        check("so the day holds the one event that is theirs",
              out["count"] == 1, f"{out['count']} events", out["count"], 1)

        # `accessRole` is a field in a partial response and is no longer asked
        # for. A reply that carries it anyway must not start meaning something.
        _agenda._CALENDARS = None
        bare = Roster([
            {"id": "them@example.com", "summary": "Alex", "primary": True},
            ROSTER[1],
        ])
        _agenda._gws_cli.run_json = bare
        kept = [cal["id"] for cal in _agenda._calendars()]
        check("a primary calendar with no accessRole on it is still theirs",
              kept == ["them@example.com"], f"{kept}", kept,
              ["them@example.com"])
        check("and the partial response stops asking for the role at all",
              "accessRole" not in bare.fields, bare.fields)

        # Nothing left after the filter is a clear day. `_read` turns a
        # `_calendars` that raised into `error`, and an answer that says the
        # calendar could not be read is a different sentence from "nothing on".
        _agenda._CALENDARS = None
        _agenda._gws_cli.run_json = Roster([ROSTER[1], ROSTER[4]])
        out = _agenda._read(DAY, 1)
        check("an account with nothing of its own is a clear day, not an error",
              out.get("count") == 0 and "error" not in out, f"{sorted(out)}",
              out, "count 0, no error")
    finally:
        _agenda._gws_cli.run_json, _agenda._CALENDARS = real

    print("\n== an RSVP, and whose it is ==")
    tz = datetime.now().astimezone().tzinfo
    me = {"email": "them@example.com", "self": True,
          "responseStatus": "needsAction"}
    raw = {
        "summary": "Design review",
        "start": {"dateTime": f"{DAY}T10:00:00+09:00"},
        "end": {"dateTime": f"{DAY}T11:00:00+09:00"},
        "attendees": [{"email": "ann@example.com",
                       "responseStatus": "accepted"}, me],
    }
    check("their own row in the attendee list is found on their calendar",
          _agenda._mine(raw, MINE) == me, f"{_agenda._mine(raw, MINE)}",
          _agenda._mine(raw, MINE), me)
    # `self` marks whoever owns the calendar being read. On `KIN` that is the
    # other person, and their "not replied yet" reported as the asker's is the
    # failure `_theirs` guards here as well as in `_calendars` — the role says
    # `owner` on both rows and cannot tell them apart.
    check("and not on somebody else's, however the role reads",
          _agenda._mine(raw, KIN) is None, f"{_agenda._mine(raw, KIN)}")
    shaped = _agenda._shape(raw, MINE, tz)
    rsvp = shaped.get("your_rsvp")
    check("so the card is told they have not answered yet",
          rsvp == "needsAction", f"{rsvp}", rsvp, "needsAction")
    # The other half of the same read. An invitation they turned down is not
    # their day, and it stayed on it for as long as nothing looked at the RSVP.
    declined = dict(raw, attendees=[dict(me, responseStatus="declined")])
    check("and one they turned down is off the day altogether",
          _agenda._shape(declined, MINE, tz) is None,
          f"{_agenda._shape(declined, MINE, tz)}")
    check("with nothing on the row naming the calendar it came off",
          "calendar" not in shaped, f"{sorted(shaped)}")
    # The field list the frame is cut down to. A name left in it after the
    # thing that produced it is gone is a key nothing writes and a renderer
    # branch nothing reaches — the card reads as maintained and is not.
    check("and no field the tool no longer produces is on its way to the card",
          not ({"busy_only", "calendar"} & _agenda.SCREEN_FIELDS),
          f"{sorted(_agenda.SCREEN_FIELDS)}")


class Fake:
    """`_gws_cli.run_json`, answering with as many full pages as it is asked.

    Records the timeout it was handed on every call, because the deadline
    reaching the request in flight is the thing item 3 was about and it is
    invisible in the result.
    """

    def __init__(self, empty: bool = False, delay: float = 0.0):
        self.empty = empty
        self.delay = delay
        self.timeouts: list[float] = []
        self.pages = 0

    def __call__(self, argv, cwd=None, timeout=_gws_cli.TIMEOUT):
        if argv[1] == "calendarList":
            return {"items": [
                {"id": f"c{i}", "summary": f"cal {i}", "accessRole": "owner",
                 "primary": i == 0} for i in range(CALENDARS)]}
        self.timeouts.append(timeout)
        self.pages += 1
        if self.delay:
            time.sleep(self.delay)
        params = json.loads(argv[argv.index("--params") + 1])
        cal = params["calendarId"]
        page = self.pages
        items = [] if self.empty else [{
            "summary": f"{cal} p{page} e{i}",
            "start": {"dateTime": f"{DAY}T{9 + i // 4:02d}:00:00+09:00"},
            "end": {"dateTime": f"{DAY}T{9 + i // 4:02d}:15:00+09:00"},
        } for i in range(PAGE_SIZE)]
        # Always another page. A calendar this fake cannot exhaust is the only
        # way to find out what stops one.
        return {"items": items, "nextPageToken": f"t{page}"}


def budget() -> None:
    print("\n== what the fan-out is allowed to collect ==")
    real = (_agenda.PAGE_SIZE, _agenda.MAX_EVENTS, _agenda.MAX_SECONDS,
            _agenda._gws_cli.run_json, _agenda._CALENDARS, _agenda._theirs)
    _agenda.PAGE_SIZE, _agenda.MAX_EVENTS = PAGE_SIZE, MAX_EVENTS
    # The budget is a property of the fan-out, and `_theirs` now hands the
    # fan-out one calendar. Widened here rather than measured at one, because
    # every ceiling below is `calendars x PAGE_SIZE` and at one calendar the
    # arithmetic stops being able to tell a per-calendar limit from a total.
    # Which calendars reach here is `owned()`'s subject, not this one's.
    _agenda._theirs = lambda calendar: True
    try:
        # The ceiling as the docstring now states it. Every calendar takes a
        # first page whatever the budget says, so `MAX_EVENTS` only bounds what
        # comes after that round: one more page for the one spend that still
        # found room. 7 x 250 + 250 = 2,000 at the real sizes.
        fake = Fake()
        _agenda._gws_cli.run_json = fake
        _agenda._CALENDARS = None
        out = _agenda._read(DAY, 1)
        want = (CALENDARS + 1) * PAGE_SIZE
        check("the real ceiling is calendars x PAGE_SIZE, plus a page",
              out["count"] == want,
              f"{out['count']} events from {fake.pages} pages, "
              f"not MAX_EVENTS={MAX_EVENTS}", out["count"], want)
        check("and every calendar says its tail was cut",
              sorted(out["truncated"]) == sorted(f"cal {i}"
                                                 for i in range(CALENDARS)),
              f"{len(out['truncated'])} truncated")

        # Empty pages with a token behind them, which `singleEvents` expansion
        # really does return. Charging a page at least one is what ends this;
        # before, only the clock did, and 30 s of empty requests is bounded by
        # the wrong limit.
        fake = Fake(empty=True)
        _agenda._gws_cli.run_json = fake
        started = time.monotonic()
        out = _agenda._read(DAY, 1)
        check("empty pages terminate on the event budget, not the clock",
              fake.pages <= CALENDARS + MAX_EVENTS
              and time.monotonic() - started < _agenda.MAX_SECONDS,
              f"{fake.pages} pages in {time.monotonic() - started:.2f}s")
        check("and the calendars are reported as cut, not as empty",
              out["count"] == 0 and len(out["truncated"]) == CALENDARS,
              f"count={out['count']} truncated={len(out['truncated'])}")

        # The clock. Every page is issued with what is left of the deadline as
        # its own timeout, so the fan-out cannot outlive it by a page.
        _agenda.MAX_SECONDS = 1.0
        fake = Fake(delay=0.2)
        _agenda._gws_cli.run_json = fake
        started = time.monotonic()
        _agenda._read(DAY, 1)
        took = time.monotonic() - started
        check("no page outlives the deadline",
              max(fake.timeouts) <= _agenda.MAX_SECONDS,
              f"the longest page was given {max(fake.timeouts):.2f}s of "
              f"{_gws_cli.TIMEOUT}s")
        check("so neither does the fan-out", took < _agenda.MAX_SECONDS + 0.5,
              f"{took:.2f}s against MAX_SECONDS={_agenda.MAX_SECONDS}")
    finally:
        (_agenda.PAGE_SIZE, _agenda.MAX_EVENTS, _agenda.MAX_SECONDS,
         _agenda._gws_cli.run_json, _agenda._CALENDARS,
         _agenda._theirs) = real


async def walls() -> None:
    print("\n== the one tool a wall does not stop ==")
    # `agenda` charges the budget and is never refused, so it is the only
    # caller that reaches a wall holding a result. Every refusal is worded for
    # the callers that do not: it says nothing was run. Said over three events
    # it is a lie with an instruction attached, and it is repeated on every
    # remaining `agenda` call in the question.
    real = _agenda._read
    _agenda._read = lambda start, days: {"count": 3, "events": [], "tz": "UTC"}
    try:
        # Out of calls: spend past STOP_AT, then ask.
        with _gws_run.budget():
            for _ in range(_gws_run.STOP_AT):
                _gws_run.spend()
            out = await _agenda.agenda(DAY, 1)
        check("out of calls, the events still come back",
              out["count"] == 3, f"count={out['count']}")
        # `.get` and not `[...]` throughout: a budget that stopped being
        # charged, or a branch that stopped setting the key, should fail the
        # case that names it rather than raise a KeyError over the top of it.
        check("and the answer does not say nothing was run",
              _gws_run.STOPPED not in out.get("budget", ""),
              out.get("budget"))
        check("while still saying which wall it is",
              "Out of gws calls" in out.get("budget", ""), out.get("budget"))

        # Out of clock: a deadline already inside the reserve.
        with _gws_run.budget(seconds=_gws_run.RESERVE / 2):
            out = await _agenda.agenda(DAY, 1)
        check("out of time, the events still come back",
              out["count"] == 3, f"count={out['count']}")
        # The wall this line did not know about. It named `OUT_OF_CALLS`, so
        # the clock fell through to the branch that passes a refusal on whole.
        check("and that answer does not say nothing was run either",
              _gws_run.STOPPED not in out.get("budget", ""),
              out.get("budget"))
        check("while still saying which wall that one is",
              "Out of time" in out.get("budget", ""), out.get("budget"))

        # A warning is not a refusal and is already addressed to a caller that
        # ran, so it goes through untouched.
        with _gws_run.budget():
            for _ in range(_gws_run.WARN_AT - 1):
                _gws_run.spend()
            out = await _agenda.agenda(DAY, 1)
        check("a warning reaches it whole",
              out.get("budget") == _gws_run.LEFT.format(
                  spent=_gws_run.WARN_AT, total=_gws_run.STOP_AT),
              out.get("budget"))
        with _gws_run.budget():
            out = await _agenda.agenda(DAY, 1)
        check("and a call with nothing to say about the budget says nothing",
              "budget" not in out, sorted(out))
    finally:
        _agenda._read = real

    # `stood` is what makes the two cases above one fix rather than two, so
    # what it promises is that it holds for a refusal nobody has written yet.
    print("\n== a refusal, minus the part that is not true ==")
    for name in ("OUT_OF_CALLS", "OUT_OF_TIME"):
        note = getattr(_gws_run, name)
        said = _gws_run.stood(note)
        check(f"{name} keeps its reason and loses the denial",
              _gws_run.STOPPED not in said
              and said.split(".")[0] == note.split(".")[0], said)
        # Cutting the note off at the denial would pass the case above and
        # throw away the half that tells a caller holding a result what to do
        # with it, which is the only reason to forward the wall at all.
        check(f"{name} keeps the instruction after it too",
              said.endswith(note.split(f"{_gws_run.STOPPED} ")[1]), said)
    repeated = _gws_run.REPEATED.format(n=2, target="gmail +read 1a0")
    check("so does the repeat, which never reaches agenda but would fit",
          _gws_run.STOPPED not in _gws_run.stood(repeated)
          and "gmail +read 1a0" in _gws_run.stood(repeated),
          _gws_run.stood(repeated))
    check("and the seam it leaves behind is closed up",
          "  " not in _gws_run.stood(_gws_run.OUT_OF_TIME),
          repr(_gws_run.stood(_gws_run.OUT_OF_TIME)))


def read(count: int, **extra) -> dict:
    """What `_read` returns for `count` events, as `_remember` sees it."""
    events = [
        {"day": DAY, "from": f"{9 + i // 60:02d}:{i % 60:02d}", "to": "23:00",
         "title": f"event {i}", "note": "a description nobody put on a card",
         "_span": None}
        for i in range(count)
    ]
    return {"from": DAY, "to": DAY, "timezone": "JST", "count": count,
            "overlaps": [], "events": events, **extra}


async def screening() -> None:
    print("\n== what the card is handed ==")

    with _agenda.showing() as slot:
        _agenda._remember(read(2))
    titles = [e["title"] for e in slot["agenda"]["events"]]
    check("an agenda read inside a block reaches the caller's dict",
          titles == ["event 0", "event 1"], f"{titles}",
          titles, ["event 0", "event 1"])
    span = (slot["agenda"]["from"], slot["agenda"]["to"],
            slot["agenda"]["count"])
    check("with the range and the count beside it", span == (DAY, DAY, 2),
          f"{span}", span, (DAY, DAY, 2))
    # `note` is up to 200 characters of description prose per event and the
    # card has nowhere to put it; `_span` is a pair of datetimes that is not
    # JSON at all. Left in, a fortnight's answer is tens of kilobytes down a
    # socket that is also carrying audio — and a frame that will not serialise.
    check("and nothing on a row the card cannot draw",
          all("note" not in e and "_span" not in e
              for e in slot["agenda"]["events"]),
          "note and _span are gone")
    kept = set(slot["agenda"]["events"][0])
    check("what survives is the fields a row is built from",
          kept <= _agenda.SCREEN_FIELDS, f"{sorted(kept)}",
          sorted(kept - _agenda.SCREEN_FIELDS), [])

    with _agenda.showing() as capped:
        _agenda._remember(read(_agenda.SCREEN_EVENTS + 3))
    drawn = capped["agenda"]
    check("a fortnight busier than a card can hold is cut",
          len(drawn["events"]) == _agenda.SCREEN_EVENTS,
          f"{len(drawn['events'])} rows", len(drawn["events"]),
          _agenda.SCREEN_EVENTS)
    check("and says how much was cut rather than ending mid-day",
          drawn.get("more") == 3, f"{drawn.get('more')}", drawn.get("more"), 3)
    check("with the true count still on it",
          drawn["count"] == _agenda.SCREEN_EVENTS + 3, f"{drawn['count']}",
          drawn["count"], _agenda.SCREEN_EVENTS + 3)

    with _agenda.showing() as twice:
        _agenda._remember(read(2))
        _agenda._remember(read(1))
    check("two reads in one question leave the second",
          twice["agenda"]["count"] == 1, f"{twice['agenda']['count']}",
          twice["agenda"]["count"], 1)

    # `_read` returns `{"error": …}` and nothing else when the calendar list
    # cannot be fetched. Clearing on that would take an answer already drawn
    # off the screen on the way out of the question that drew it.
    with _agenda.showing() as failed:
        _agenda._remember(read(2))
        _agenda._remember({"error": "Could not list the calendars: nope"})
    check("a read that failed leaves the one that did not",
          failed["agenda"]["count"] == 2, f"{failed['agenda']['count']}",
          failed["agenda"]["count"], 2)

    # The whole reason this is a dict and not a value. `gws` opens the block,
    # `notes.start` creates the task, and everything the sub-agent does happens
    # in that task's copy of the context — so a `_SHOWING.set` down there is
    # invisible to the frame up here, and the card comes out empty.
    async def sub_agent() -> None:
        _agenda._remember(read(3))

    with _agenda.showing() as crossed:
        await asyncio.create_task(sub_agent())
    check("an agenda read on the sub-agent's own task still crosses back",
          crossed.get("agenda", {}).get("count") == 3,
          f"{crossed.get('agenda', {}).get('count')}",
          crossed.get("agenda"), 3)

    # Outside a block, which is `gws_bench.py`, every other case in this file,
    # and any future caller that is not the live tool.
    _agenda._remember(read(2))
    check("and outside a block nothing is collected at all",
          _agenda._SHOWING.get() is None, "still unset")


# Three days of it, all-day, the way `_when` leaves one: filed under the day it
# starts, with `until` naming the last day it covers.
TRIP = {"day": "2026-09-10", "until": "2026-09-12", "all_day": True,
        "title": "Kyoto"}


def screened(events: list[dict], lo: str, hi: str) -> dict:
    """`_for_screen` on a window that was actually asked about.

    `read` above answers a one-day window and always the same day, which is the
    shape every other case here wants. The whole question below is what happens
    when the window and the event's own span are not the same stretch, so the
    two ends are arguments.
    """
    return _agenda._for_screen({"from": lo, "to": hi, "count": len(events),
                                "overlaps": [], "events": events})


def spanning() -> None:
    print("\n== a trip that is already running ==")

    # Half past twelve on the second day of a three-day trip, asked as "what is
    # on today?". The API returns the event, because a window that overlaps it
    # is a window it is in, and `_when` files it under `start["date"]` —
    # `_agenda.py:308`, the day before the one being asked about.
    midspan = screened([dict(TRIP)], "2026-09-11", "2026-09-11")
    days = [e["day"] for e in midspan["events"]]
    check("a trip asked about on its second day is drawn on that day",
          days == ["2026-09-11"], f"{days}", days, ["2026-09-11"])
    # `cards.js:946-950` heads a block with the row's own `day`, and
    # `cards.js:960` passes the clock in only when that heading is today. A row
    # dated outside the window heads a day nobody asked about and takes the lit
    # row with it — `calNow` is false for an empty clock, `cards.js:780-781`.
    check("so the card heads a day inside the window it was asked for",
          all("2026-09-11" <= e["day"] <= "2026-09-11"
              for e in midspan["events"]),
          f"{days}", days, ["2026-09-11"])
    today = [e for e in midspan["events"] if e["day"] == "2026-09-11"]
    check("and the row on it is the all-day one the card lights",
          bool(today) and today[0].get("all_day") is True,
          f"{today}", today, [dict(TRIP, day="2026-09-11")])
    check("still saying how much further it runs",
          bool(today) and today[0].get("until") == "2026-09-12",
          f"{today and today[0].get('until')}",
          today and today[0].get("until"), "2026-09-12")

    # The other half of the same fault, and the one re-heading the single row
    # would not fix: here the start *is* in the window, so nothing looks wrong,
    # and the trip is still drawn on the Thursday alone.
    week = screened([dict(TRIP)], "2026-09-07", "2026-09-13")
    over = [e["day"] for e in week["events"]]
    check("asked as a week, it is drawn on each day it covers",
          over == ["2026-09-10", "2026-09-11", "2026-09-12"], f"{over}",
          over, ["2026-09-10", "2026-09-11", "2026-09-12"])
    last = [e for e in week["events"] if e["day"] == "2026-09-12"]
    check("and the last day does not point at its own heading",
          bool(last) and "until" not in last[0],
          f"{last}", last and last[0].get("until"), None)

    # The far end of the same clip. Spreading the event over its own span
    # rather than over the window's puts a row on a day the answer does not
    # cover, and `cards.js:946-950` heads the card with it — the same fault
    # from the other side.
    clipped = screened([dict(TRIP)], "2026-09-10", "2026-09-11")
    cut = [e["day"] for e in clipped["events"]]
    check("a trip that outruns the window stops at the end of it",
          cut == ["2026-09-10", "2026-09-11"], f"{cut}",
          cut, ["2026-09-10", "2026-09-11"])

    # `cards.js:946-950` compares each row with the one before it and nothing
    # else, so a copy appended after the list was sorted opens a second block
    # under a heading that already exists. Green before this fix, because one
    # row cannot be out of order, and kept anyway: it is what fails against the
    # obvious spread, the one that appends and does not re-sort.
    mixed = screened([
        dict(TRIP),
        {"day": "2026-09-11", "from": "10:00", "to": "11:00",
         "title": "Standup"},
    ], "2026-09-07", "2026-09-13")
    order = [(e["day"], e.get("from", "")) for e in mixed["events"]]
    check("and the spread rows come back in the order the card groups by",
          order == sorted(order), f"{order}", order, sorted(order))

    # Both halves at once, and the reason the spread is in `_for_screen`
    # rather than in `_read`: the card draws three days and the sub-agent is
    # still told about one appointment, so nobody is told there are three
    # things on.
    check("three rows are still one event in the count",
          (len(week["events"]), week["count"]) == (3, 1),
          f"{(len(week['events']), week['count'])}",
          (len(week["events"]), week["count"]), (3, 1))

    # Green before as well, and the boundary of the fix. A one-day all-day
    # entry has no `until`; a timed event that crosses midnight carries `ends`
    # instead, and its `from` and `to` belong to the day it began, so a copy of
    # that row on the morning after would print a time the event is not at.
    # Neither may grow a second row.
    plain = screened([
        {"day": "2026-09-11", "all_day": True, "title": "Sports day"},
        {"day": "2026-09-11", "from": "23:00", "to": "01:30",
         "ends": "2026-09-12", "title": "Launch"},
    ], "2026-09-11", "2026-09-12")
    check("nothing else on the card grows a row it did not have",
          len(plain["events"]) == 2, f"{len(plain['events'])} rows",
          [e["day"] for e in plain["events"]], ["2026-09-11", "2026-09-11"])


def expiration() -> None:
    print("\n== OAuth expiration detection and status reporting (#584) ==")
    import unittest.mock
    from ai_terminal.live.main import _gws_status
    from ai_terminal.live.tools import gws

    # 1. Detection of auth expiration phrases
    val = _gws_cli.is_auth_expired(stderr="Error: invalid_grant: Token has been expired or revoked")
    check("invalid_grant in stderr detected as expired", val, f"{val}")
    val = _gws_cli.is_auth_expired(stdout="Token has been expired or revoked")
    check("Token has been expired or revoked in stdout detected as expired", val, f"{val}")
    val = _gws_cli.is_auth_expired(stdout="HTTP 401 Unauthorized")
    check("HTTP 401 Unauthorized detected as expired", val, f"{val}")
    val = _gws_cli.is_auth_expired(stderr="Please re-authenticate with gws auth login")
    check("re-authenticate prompt detected as expired", val, f"{val}")
    val = _gws_cli.is_auth_expired(stderr="Error: network unreachable")
    check("generic error not detected as expired", not val, f"{val}")

    # 2. GwsAuthExpiredError mapping in gws tool
    err_kind = gws._NOT_AN_ANSWER.get(gws.EXPIRED)
    check("EXPIRED is registered in _NOT_AN_ANSWER",
          err_kind == "expired", f"{err_kind}")

    # 3. _gws_status reports expired: True when auth is revoked/expired
    with unittest.mock.patch("ai_terminal.live.tools._gws_cli.available", return_value=True), \
         unittest.mock.patch("ai_terminal.live.tools._gws_cli.run_json",
                             side_effect=_gws_cli.GwsAuthExpiredError("Token has been expired or revoked")):
        status = _gws_status()
        ok = (status.get("connected") is False and status.get("expired") is True)
        check("gws_status reports connected: False, expired: True on GwsAuthExpiredError",
              ok, f"connected={status.get('connected')}, expired={status.get('expired')}")

    with unittest.mock.patch("ai_terminal.live.tools._gws_cli.available", return_value=True), \
         unittest.mock.patch("ai_terminal.live.tools._gws_cli.run_json",
                             return_value={"email": "user@example.com", "token_valid": False}):
        status = _gws_status()
        ok = (status.get("connected") is False and status.get("expired") is True)
        check("gws_status reports connected: False, expired: True on invalid token with account",
              ok, f"connected={status.get('connected')}, expired={status.get('expired')}")

    with unittest.mock.patch("ai_terminal.live.tools._gws_cli.available", return_value=True), \
         unittest.mock.patch("ai_terminal.live.tools._gws_cli.run_json",
                             return_value={"email": "user@example.com", "token_valid": True}):
        status = _gws_status()
        ok = (status.get("connected") is True and status.get("expired") is False)
        check("gws_status reports connected: True, expired: False on valid token",
              ok, f"connected={status.get('connected')}, expired={status.get('expired')}")


def main() -> None:
    owned()
    pairs()
    copies()
    ordered()
    budget()
    asyncio.run(walls())
    asyncio.run(screening())
    spanning()
    expiration()
    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()
