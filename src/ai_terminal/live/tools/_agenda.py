"""The person's whole calendar for a stretch of days, in one call.

`gws calendar +agenda` is the obvious command and it answers a thinner question
than it looks. It returns five fields — calendar id, summary, location, start,
end — so a Tuesday reads back as "7件の予定", four of them titled `(No title)`,
and nothing to say about any of them. No attendees, no meeting link, no all-day
handling, no way to tell a declined invitation from an accepted one, and no
hint that two of the seven are the same half hour.

The Calendar API has all of that on the Event resource. `events.list` is only
per-calendar, which is why a wrapper exists at all — so this does the fan-out
itself, in parallel, and asks for a `fields` partial response. Seven calendars
came back in 1.6 s, about what one `+agenda` costs, carrying the nine fields
worth having rather than the forty the API would otherwise send. It is one
calendar now, for the reason below, and the fan-out is kept because what it
fans out over is a predicate away from being seven again.

What comes back is already worked out: times in the terminal's timezone rather
than in whichever one the event was created in, all-day said as all-day,
declined and cancelled dropped and counted, overlapping pairs computed. The
sub-agent has a dozen calls to answer a question in and something waiting for
it to finish; deriving that from raw JSON is not what they are for.

Their own calendar and no other, which is what "what is on?" asks about. The
household account this was built against sees seven: their default one, a
second person's default one, a shared family one, a work one, a team one and
two subscribed feeds. Six of those are somebody else's day, and an answer that
reads them out is answering a question nobody asked. :func:`_calendars` drops
them, so the fan-out costs what it reads rather than paying for six round trips
and discarding them afterwards.

Which one is theirs is `primary` and is emphatically not `accessRole: owner` —
four of those seven are `owner`. See :func:`_theirs`.

That took four mechanisms with it, all of them about somebody else's calendar:
the `(No title)` free/busy case, which existed because an event on a
`freeBusyReader` calendar has a title the API will not return; the `calendar`
field naming whose day an event was on; the pass that recognised a nameless
busy block as the silhouette of a meeting on their own calendar and kept it out
of the overlap walk; and the pass that merged the copy of one appointment that
two calendars both carried. Git has all four if the household ever wants the
other calendars back.

The last of those outlived the other three, and the fan-out narrowing to one
calendar turned it from a fix into a fault. Reading `primary` alone, there is
no second copy of anything to merge: `events.list` with `singleEvents` returns
each event once and the page token does not repeat one. So every merge it
could still make was two appointments that were never one — and it merged on
day, start, end and title, which is exactly the shape of the household booking
nobody notices, two children in the same hour with the same word on both. It
took the second one out of `events`, took a minute off `count`, took the
invitation on it that nobody had answered, and took the pair out of `overlaps`,
which is the one thing `gws.INSTRUCTION` says to lead with.

If the fan-out ever does widen again, what identifies one appointment across
two calendars is `iCalUID`, which :data:`FIELDS` does not currently ask for.
Not the times and the title: those are what two different appointments in a
household share.
"""

import asyncio
import concurrent.futures
import contextlib
import contextvars
import html
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import NamedTuple

from . import _gws_cli, _gws_run, _progress

logger = logging.getLogger(__name__)

# A partial response, because the Event resource is mostly bookkeeping: etag,
# iCalUID, sequence, created, updated, reminders, two copies of the organizer.
# Asking for nine fields rather than filtering forty keeps the reply a tenth of
# the size on the wire and in the sub-agent's context.
FIELDS = (
    "nextPageToken,"
    "items(summary,location,description,start,end,status,eventType,"
    "attendees(email,responseStatus,self),hangoutLink,"
    "conferenceData(entryPoints(entryPointType,uri)),organizer(displayName))"
)

# Entries the API returns that are not appointments: `workingLocation` is the
# "in the office today" banner, `birthday` a contact's, repeating every year.
# Neither is an answer to "what's on tomorrow?".
NOT_APPOINTMENTS = {"workingLocation", "birthday"}

# A fortnight. Past that the answer is too long to say out loud, and long
# enough that a busy calendar starts needing a second page for it.
MAX_DAYS = 14

# Events per page. The API's own default is 250 and it stops there quietly:
# with `items(...)` as the field mask there is no `nextPageToken` in the reply
# to notice, so a busy work calendar loses its tail and `count` still sounds
# authoritative. Asking explicitly and following the token costs one round trip
# for an ordinary day, which is what it cost before.
PAGE_SIZE = 250

# What the whole fan-out may collect, and how long it may take doing it. See
# :class:`_Budget` for why these are one thing and not three limits.
MAX_EVENTS = 300
MAX_SECONDS = 30.0

# A busy fortnight can collide with itself a hundred times, and a list that
# long is not a warning any more. The events are all there to look at; these
# are the shortlists, and `overlaps_total` and `back_to_back_total` say how
# long the two real ones were.
MAX_OVERLAPS = 12

# The calendar list changes about once a year and costs a round trip every
# time it is asked for, so one session's worth of questions shares an answer.
CALENDAR_TTL = 600.0
_CALENDARS: tuple[float, list[dict]] | None = None

# Descriptions come back as HTML, because that is what the Calendar web UI
# writes into them. The one line anybody wants out of a description is the
# meeting link, and `<a href="…"><u>…</u></a>` spends sixty tokens hiding it.
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def _theirs(calendar: dict) -> bool:
    """Is this the person's own calendar, rather than one they can see?

    `primary`, and nothing else. The tempting predicate is `accessRole ==
    "owner"`, on the reading that `owner` marks a calendar they created and
    everything shared *to* them holds a lesser role. That reading is wrong, and
    the account this runs against is what says so: of its seven calendars,
    four come back as `owner` and only one of the four is theirs. The other
    three are a second person's default calendar, a shared household calendar
    and a work one — all handed over at `owner`, because that is the role
    Calendar grants when somebody shares a calendar with full rights rather
    than a role reserved for the creator.

    So `owner` is a permission and not a claim of ownership, and there is no
    field on the `calendarList` resource that separates the two. `primary` is
    the only one that means what this needs: exactly one calendar, the person's
    own, the one their invitations land on.

    The cost is a second calendar somebody keeps their own things on, which
    this drops with everybody else's. That is the narrow reading of "just show
    events of the owner" and it is the reading the owner picked when shown
    their own list.
    """
    return bool(calendar.get("primary"))


def _calendars() -> list[dict]:
    """Their own calendar, newest answer within the TTL.

    Still a list and still a `calendarList.list` call, though :func:`_theirs`
    now leaves one row in it. The call is what says *which* calendar is
    primary, and the fan-out below is written for however many come back — a
    household that decides it wants the shared ones again changes the
    predicate and nothing else.
    """
    global _CALENDARS
    if _CALENDARS and time.monotonic() - _CALENDARS[0] < CALENDAR_TTL:
        return _CALENDARS[1]
    got = _gws_cli.run_json([
        "calendar", "calendarList", "list", "--format", "json",
        # No `accessRole`: it was read to decide what to keep and to describe
        # a free/busy calendar, and neither of those happens now. `summary` is
        # still asked for because an unreachable calendar is named with it.
        "--params", json.dumps({"fields": "items(id,summary,primary)"}),
    ])
    # Here and not in `_read`, so the fan-out below queries what it keeps. A
    # filter applied to the events has already paid for the round trips.
    items = [cal for cal in got.get("items", []) if _theirs(cal)]
    _CALENDARS = (time.monotonic(), items)
    return items


class _Budget:
    """What the whole fan-out has left: events to collect, and time to do it.

    Following `nextPageToken` took away two ceilings the API used to impose for
    nothing, and everything past them goes wrong in a different way. The reply
    is what the sub-agent reads, and it has a dozen calls and a person waiting.
    :func:`_overlaps` walks every pair. `_read` runs in a thread nobody can
    cancel, so a fan-out that outlives the sub-agent's own 160 s deadline keeps
    paging after the question has been answered without it.

    Three symptoms of one runaway, so one budget rather than three limits.
    Shared across the calendars because the runaway is a total: one busy
    calendar is the usual shape, and a per-calendar cap either lets it through
    or punishes the six quiet ones for it.

    The two halves are not equally strong, and the events half is the weaker of
    them. Every calendar takes a first page whatever the budget says — a
    calendar nobody read is not a calendar whose tail was cut, and of the two
    things `truncated` could be made to mean, that is the wrong one — so the
    first round is already collected before :meth:`spend` has any say in it.
    The ceiling it enforces is `calendars × PAGE_SIZE`, plus one more page for
    each spend that still found room: on this seven-calendar account that is
    2,000 events and about two million pairs for :func:`_overlaps`, not the 300
    and 45,000 the number would suggest. What :data:`MAX_EVENTS` bounds is the
    growth past the first round, which is the part that had no bound at all —
    ten pages each would have reached 17,500 events and 150 million pairs.

    The clock is the half that holds. :meth:`seconds_left` is passed to every
    page as its own timeout, so no request outlives the deadline and the
    fan-out as a whole is bounded by :data:`MAX_SECONDS` rather than by
    :data:`MAX_SECONDS` plus whatever the page in flight had left. That was 30
    plus `_gws_cli.TIMEOUT`'s 45 before, and it is also what reaches a first
    page, which `spend` is never asked about. :func:`_calendars` runs before
    this object exists and keeps its own 45 s, so the whole call is 75 s worst
    case against the sub-agent's 160 s (`gws.TIMEOUT`) — and 1.6 s on an
    ordinary day.

    `tools/agenda_check.py` pins the ceiling and the clock, with a fake that
    answers a full page every time — no account is big enough to reach either.
    """

    def __init__(self):
        self._left = MAX_EVENTS
        self._until = time.monotonic() + MAX_SECONDS
        self._lock = threading.Lock()

    def seconds_left(self) -> float:
        """How long the fan-out may still spend, never negative.

        Every page is issued with this as its timeout, so the deadline binds
        the request in flight and not only the gap between two of them.
        """
        return max(0.0, self._until - time.monotonic())

    def spend(self, count: int) -> bool:
        """Record a page, and say whether there is room for another.

        A page costs at least one even when it is empty, and an empty page with
        a `nextPageToken` behind it is a real thing `singleEvents` expansion
        does. Charging the literal zero left that loop for the clock alone to
        end, so a calendar could spend the whole of :data:`MAX_SECONDS` asking
        the API for nothing — bounded, but by the wrong limit.
        """
        with self._lock:
            self._left -= max(count, 1)
            return self._left > 0 and self.seconds_left() > 0


def _events(
    calendar_id: str, lo: datetime, hi: datetime, fanout: _Budget,
) -> tuple[list[dict], bool]:
    """One calendar's events in the window, and whether the tail was cut off."""
    items: list[dict] = []
    token = ""
    while True:
        params = {
            "calendarId": calendar_id,
            "timeMin": lo.isoformat(),
            "timeMax": hi.isoformat(),
            # Expanded and in order: without this a weekly stand-up comes back
            # once, as the rule that generates it, dated whenever it started.
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": PAGE_SIZE,
            "fields": FIELDS,
        }
        if token:
            params["pageToken"] = token
        try:
            got = _gws_cli.run_json([
                "calendar", "events", "list", "--format", "json",
                "--params", json.dumps(params),
            ], timeout=min(_gws_cli.TIMEOUT, fanout.seconds_left()))
        except _gws_cli.GwsError:
            # Pages already in hand mean this is a tail that was lost, which is
            # what `truncated` says; with none, there is nothing to report but
            # the failure and the caller turns it into `unreachable`. The two
            # arrive by the same route because the clock reaches a page through
            # its timeout — including a first page, which `spend` never sees.
            if not items:
                raise
            return items, True
        page = got.get("items", [])
        items += page
        token = got.get("nextPageToken") or ""
        if not token:
            return items, False
        # A first page for every calendar whatever the fanout says — a calendar
        # nobody read is not the same as one whose tail was cut, and reporting
        # it as the latter is the more useful of the two lies. After that the
        # fanout decides, and what it cuts is a tail.
        if not fanout.spend(len(page)):
            return items, True


def _when(event: dict, tz) -> dict:
    """Start and end as the terminal reads them.

    An all-day event carries `date` where a timed one carries `dateTime`, and
    that is the only thing that tells them apart — an all-day event rendered
    from its timestamp is "00:00〜00:00", which is how a birthday ends up
    looking like a midnight appointment.
    """
    start, end = event.get("start", {}), event.get("end", {})
    if start.get("date"):
        # The API's end date is exclusive: a one-day event ends the next
        # morning. Say the last day it covers instead.
        last = date.fromisoformat(end["date"]) - timedelta(days=1)
        when = {"day": start["date"], "all_day": True}
        if last.isoformat() != start["date"]:
            when["until"] = last.isoformat()
        return when

    lo = datetime.fromisoformat(start["dateTime"]).astimezone(tz)
    hi = datetime.fromisoformat(end["dateTime"]).astimezone(tz)
    when = {"day": f"{lo:%Y-%m-%d}", "from": f"{lo:%H:%M}", "to": f"{hi:%H:%M}"}
    if hi.date() != lo.date():
        when["ends"] = f"{hi:%Y-%m-%d}"
    when["_span"] = (lo, hi)
    return when


def _mine(event: dict, calendar: dict) -> dict | None:
    """The person's own row in the attendee list, if they are on it.

    Only on their own calendar. `self` marks the attendee who owns the calendar
    being read, not the person asking — so on a family member's calendar it
    points at the family member, and reporting their RSVP as "you have not
    replied yet" is worse than saying nothing. A calendar shared at
    `accessRole: owner` is exactly that case and does not look like it, which
    is why the test here is :func:`_theirs` rather than the role.

    Kept even though :func:`_calendars` now hands this nothing else, because
    what reaches here is a fact about one call site and this is the function
    that would name the wrong person if that call site ever loosened.
    """
    if not _theirs(calendar):
        return None
    for who in event.get("attendees", []):
        if who.get("self"):
            return who
    return None


def _note(description: str) -> str:
    """The description as a line of text, or nothing worth carrying."""
    text = _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", description))).strip()
    return text[:200]


def _meet(event: dict) -> str:
    """The video link, whoever is hosting.

    `hangoutLink` is Meet and only Meet. A Zoom or Teams invitation puts its
    URL in `conferenceData` instead, which is also where Meet keeps a second
    copy — so the field is worth reading whenever the first one is empty.
    """
    link = event.get("hangoutLink")
    if link:
        return link
    for entry in event.get("conferenceData", {}).get("entryPoints", []):
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"]
    return ""


def _shape(event: dict, calendar: dict, tz) -> dict | None:
    """One event as the sub-agent should see it, or None if it is noise."""
    if event.get("status") == "cancelled":
        return None
    if event.get("eventType") in NOT_APPOINTMENTS:
        return None
    me = _mine(event, calendar)
    if me and me.get("responseStatus") == "declined":
        return None

    out = _when(event, tz)
    title = event.get("summary")
    if title:
        out["title"] = title
    else:
        # Their own calendar, so the event really has no title rather than one
        # the API is withholding: nobody named it, and there is no second call
        # that would find a name.
        out["untitled"] = True

    # The organiser's own doubt about the event, which is a separate field from
    # the person's RSVP below and reads the same in English.
    if event.get("status") == "tentative":
        out["tentative"] = True

    for key, value in (
        ("where", event.get("location")),
        ("meet", _meet(event)),
        ("note", _note(event.get("description") or "")),
    ):
        if value:
            out[key] = value

    people = event.get("attendees") or []
    if people:
        out["people"] = len(people)
        rsvp = (me or {}).get("responseStatus")
        # "accepted" is the expected case and not worth a line. The other two
        # are: an invitation nobody has answered is a thing to be told about.
        if rsvp in ("needsAction", "tentative"):
            out["your_rsvp"] = rsvp
    if event.get("eventType") == "outOfOffice":
        out["kind"] = "out of office"
    return out


class _Pairs(NamedTuple):
    """Both lists and both totals. The totals are what says a list is short."""

    clashes: list[list[str]]
    clashes_found: int
    touching: list[list[str]]
    touching_found: int


def _overlaps(events: list[dict]) -> _Pairs:
    """The pairs a list of times hides, and how many of them there were.

    Two rows reading 16:00〜16:30 and 16:00〜18:00 are the most useful fact on
    the page and the one a person reading a list out loud never gets to. A pair
    that merely touches — one ending exactly as the next begins — is the weaker
    version of the same fact, worth hearing and not worth calling a collision,
    so it comes back separately.

    Both lists stop at :data:`MAX_OVERLAPS` and both keep counting past it, so
    a day that chains fifteen meetings can say that rather than report twelve
    and stop.

    Two kinds of event are left out, both before the walk rather than inside
    it, because a pair skipped in one branch is a pair still counted in the
    other. All-day entries have no `_span` at all — :func:`_when` gives them a
    day and nothing else — so they are not merely uninteresting here, they are
    a `KeyError` in the walk. A public holiday in the list takes the whole tool
    call down; a birthday does not reach here at all, since
    :data:`NOT_APPOINTMENTS` drops those back in :func:`_shape`. Zero-length
    entries, which is what a reminder is, touch whatever they sit inside:
    16:00〜16:00 inside a 16:00〜18:00 meeting is not a clash, so it falls
    through to the adjacency test and matches it, and containment reported as
    back-to-back is the wrong end of the fact.

    `tools/agenda_check.py` pins both, and the day that would demonstrate them
    is one nobody can arrange on demand.
    """
    timed = [e for e in events
             if "_span" in e and e["_span"][0] < e["_span"][1]]
    clashes: list[list[str]] = []
    touching: list[list[str]] = []
    clashed = touched = 0
    for i, first in enumerate(timed):
        for second in timed[i + 1:]:
            (a, b), (c, d) = first["_span"], second["_span"]
            if a < d and c < b:
                clashed += 1
                if len(clashes) < MAX_OVERLAPS:
                    clashes.append([_label(first), _label(second)])
            # `d == a` never fires on the list `_read` passes: that one is
            # sorted by `(day, from)`, so `a <= c`, and the only way back to
            # `d == a` from there is a zero-length second event, which the
            # filter above has already dropped. Defensive, and kept because
            # `_overlaps` takes a list and does not sort it — the pair is a
            # real adjacency whichever order the two arrive in.
            elif b == c or d == a:
                touched += 1
                if len(touching) < MAX_OVERLAPS:
                    touching.append([_label(first), _label(second)])
    return _Pairs(clashes, clashed, touching, touched)


def _label(event: dict) -> str:
    what = event.get("title") or "untitled"
    return f"{event['day']} {event.get('from', '')} {what}".strip()


def _read(start: str, days: int) -> dict:
    # The machine's offset as of now, not its zone: there is no portable way to
    # ask Python which zone the host is in. Japan has no DST so the two are the
    # same thing there, and on a host that does, a fortnight that crosses the
    # changeover reads an hour out at the far end.
    tz = datetime.now().astimezone().tzinfo
    try:
        first = date.fromisoformat(start)
    except ValueError:
        return {"error": f"`start` must be a date like 2026-09-08, not {start!r}."}
    days = max(1, min(days, MAX_DAYS))
    lo = datetime.combine(first, datetime.min.time(), tz)
    hi = lo + timedelta(days=days)

    try:
        calendars = _calendars()
    except Exception as err:
        return {"error": f"Could not list the calendars: {err}"}

    # One subprocess per calendar at once. Seven of them serially is 8 s and a
    # question nobody waits out; in parallel it is 1.6 s, which is what one
    # `+agenda` costs for a fraction of the answer.
    # `fanout` and not `budget`, which in `agenda` below is the unrelated
    # per-question call budget. Two of them in one module is enough.
    fanout = _Budget()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_events, cal["id"], lo, hi, fanout): cal
            for cal in calendars
        }
        events, unreachable, truncated = [], [], []
        for future, cal in futures.items():
            name = cal.get("summary") or cal["id"]
            try:
                got, cut = future.result()
            except Exception as err:
                logger.warning("agenda: %s failed: %s", cal["id"], err)
                unreachable.append(name)
                continue
            if cut:
                truncated.append(name)
            for raw in got:
                # One odd event is not the day. A calendar whose command fails
                # already degrades to `unreachable` and leaves the rest of the
                # answer standing; an event that arrives without an `end` — or
                # with neither a `date` nor a `dateTime` on it — should cost no
                # more than itself either.
                try:
                    shaped = _shape(raw, cal, tz)
                except Exception as err:
                    logger.warning("agenda: skipped an event on %s: %s",
                                   cal["id"], err)
                    continue
                if shaped is not None:
                    events.append(shaped)

    # Sorted and no more. Nothing here may drop a row: two entries a person
    # cannot tell apart from the outside are two entries they have to be told
    # about, and the pass that used to merge them is the fourth mechanism in
    # the module docstring above. `tools/agenda_check.py` pins the hour that
    # made it a fault.
    events.sort(key=lambda e: (e["day"], e.get("from", "")))
    pairs = _overlaps(events)
    out = {
        "from": first.isoformat(),
        "to": (first + timedelta(days=days - 1)).isoformat(),
        "timezone": str(tz),
        "count": len(events),
        "overlaps": pairs.clashes,
        "events": [{k: v for k, v in e.items() if k != "_span"} for e in events],
    }
    if pairs.clashes_found > len(pairs.clashes):
        out["overlaps_total"] = pairs.clashes_found
    if pairs.touching:
        out["back_to_back"] = pairs.touching
    if pairs.touching_found > len(pairs.touching):
        out["back_to_back_total"] = pairs.touching_found
    if unreachable:
        out["unreachable"] = unreachable
    if truncated:
        out["truncated"] = truncated
    return out


# How many events the card is given, and which of an event's fields travel
# with it. The card is the reason for both numbers. `note` is up to 200
# characters of description prose with nowhere on a row to go, and the whole
# point of `MAX_EVENTS` being 300 is that a fortnight can be that busy — 300
# rows is not a card, it is the same wall of text in a nicer font.
SCREEN_EVENTS = 40
SCREEN_FIELDS = frozenset({
    "day", "from", "to", "until", "ends", "all_day", "title",
    "untitled", "tentative", "where", "meet", "people",
    "your_rsvp", "kind",
})

# The agenda this question read, on its way to the card.
#
# A ContextVar holding a mutable dict, for the reason `gws_cache._META` is one
# and with one more turn of the screw: `agenda` runs inside the sub-agent's
# task, `asyncio.create_task` snapshots the context, and a `set` in there is
# invisible to the `gws` frame back out here. Mutating a dict the caller
# already put in the context crosses that boundary; rebinding does not. The
# caller holds the dict itself — see :func:`showing` — because by the time the
# answer lands its own `with` block has long since reset the variable.
_SHOWING: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ai_terminal_agenda_shown", default=None
)


@contextlib.contextmanager
def showing():
    """Collect the agenda read inside this block, for the card to draw.

    Yields the dict the answer lands in: `{"agenda": …}` once :func:`agenda`
    has run, empty if the question never looked at the calendar. Nothing
    outside a block of these records anything, so the tool stays exactly as it
    was under `gws_bench.py` and under a test.
    """
    slot: dict = {}
    token = _SHOWING.set(slot)
    try:
        yield slot
    finally:
        _SHOWING.reset(token)


def _spread(events: list[dict], lo: str, hi: str) -> list[dict]:
    """A multi-day all-day event on every day of the window it covers.

    The tool files an event under the day it starts, which is the right answer
    for the sub-agent — one appointment is one row, and `until` says how far it
    runs. The card is grouped by day (`cards.js:946-950`) and lights the row
    that is happening now only inside the block whose heading is today
    (`cards.js:960`, `:780-781`). So a three-day trip asked about on its second
    day arrives with `day` on the first, heads a block dated outside the window
    that was asked about, and lights nothing: the one thing on the screen that
    is happening right now reads as not happening.

    Rather than re-heading the single row, which fixes the mid-span query and
    leaves "this week" still showing the trip only on the Thursday it began,
    the card gets one row per day covered. That is what a day view is for, and
    a household glancing at the panel on day two of a trip wants to see the
    trip on day two.

    Clipped to `lo`..`hi`, the window that was asked about, because the days
    outside it are days the answer does not cover — and a copy dated outside
    the window is the bug this exists to fix. `until` is dropped from the last
    copy, where it would point at the row's own heading; on the copies before
    it, it still says how much further the thing runs.

    Only all-day events. A timed event that crosses midnight carries `ends`
    rather than `until` and cannot be copied this way: its `from` and `to`
    belong to the day it began, and a second row repeating them is a wrong
    time rather than a missing one. See :func:`_when`.
    """
    first_day, last_day = date.fromisoformat(lo), date.fromisoformat(hi)
    out: list[dict] = []
    for event in events:
        until = event.get("until")
        if not (event.get("all_day") and until):
            out.append(event)
            continue
        try:
            begins = max(date.fromisoformat(event["day"]), first_day)
            ends = min(date.fromisoformat(until), last_day)
        except (KeyError, TypeError, ValueError):
            out.append(event)
            continue
        if ends < begins:
            out.append(event)
            continue
        day = begins
        while day <= ends:
            copy = dict(event)
            copy["day"] = day.isoformat()
            if until <= copy["day"]:
                copy.pop("until", None)
            out.append(copy)
            day += timedelta(days=1)
    # The card groups by comparing each row with the one before it, so a row
    # out of order starts a second block under the same heading.
    out.sort(key=lambda e: (e["day"], e.get("from", "")))
    return out


def _for_screen(got: dict) -> dict:
    """The part of an agenda a card can draw. See :data:`SCREEN_FIELDS`.

    `count` is the answer's count and not the number of rows: :func:`_spread`
    draws a three-day trip on three days, and the person was not told about
    three things.
    """
    events = _spread(got["events"], got["from"], got["to"])
    out = {
        "from": got["from"],
        "to": got["to"],
        "count": got["count"],
        "events": [{k: v for k, v in e.items() if k in SCREEN_FIELDS}
                   for e in events[:SCREEN_EVENTS]],
    }
    if len(events) > SCREEN_EVENTS:
        out["more"] = len(events) - SCREEN_EVENTS
    return out


def _remember(got: dict) -> None:
    """Keep this agenda for the card, if anything is collecting one.

    Last read wins. A question that covers two stretches — "today, and what
    about the 20th?" — is answered by the second, and a card still showing the
    first contradicts what is being said out loud. An `_read` that failed
    carries `error` and no `events`, and leaves whatever was there: half an
    answer drawn is better than a card that empties itself on the way out.
    """
    slot = _SHOWING.get()
    if slot is None or "events" not in got:
        return
    slot["agenda"] = _for_screen(got)


async def agenda(start: str, days: int = 1) -> dict:
    """Everything on the person's own calendar over a stretch of days.

    Their own calendar and no other, with the events already sorted, already in
    their timezone, and already filtered: nothing cancelled, nothing they have
    declined, no "working from home" banners or contacts' birthdays. The
    family's calendars, the work one and the holiday feeds are not read, so
    nothing that comes back belongs to anybody else.

    Use this for any question about what is on. It is one call and it is the
    whole answer; do not follow it with `gws calendar +agenda` or with
    `events list`, which return less.

    Each event carries what there is to carry: `title`, `day`, `from`/`to` or
    `all_day`, `where`, `meet` for a video link, `people` for how many are
    invited, `your_rsvp` when they have not accepted yet, and `tentative` when
    the organiser has not settled it. `overlaps` lists the pairs that run over
    each other — say those out loud, they are what a list of times hides — and
    `back_to_back` the pairs with no gap between them, which is worth a mention
    and is not a clash.

    `untitled` is an event on their calendar that nobody named. There is no
    title to fetch and no further call that would find one; say the time and
    leave it at that.

    `overlaps_total`, `back_to_back_total`, `truncated` and `unreachable` each
    mean the answer is short of something: more clashes or more back-to-back
    pairs than are listed, a calendar with more events than fitted, a calendar
    that could not be read. Say so.

    Args:
        start: The first day, as `YYYY-MM-DD`. Work it out from `now` in
            your message — "tomorrow" is not a date.
        days: How many days from `start`, up to 14. One day for "tomorrow",
            seven for "this week".

    Returns:
        The events, the overlaps between them, and the timezone the times are
        in.
    """
    _progress.note("calendar", "Reading their calendar.")
    # One call against the question's budget, like any other. It is not refused
    # when the budget runs out: a question that has spent eleven calls getting
    # nowhere is exactly the one where reading the calendar is the answer.
    budget = _gws_run.spend()
    logger.info("agenda %s +%sd", start, days)
    got = await asyncio.to_thread(_read, start, days)
    # Before the budget lines below, which write a sentence aimed at the
    # sub-agent into the same dict. The card wants the events and not the
    # scolding.
    _remember(got)
    if _gws_run.refused(budget):
        # Whichever wall it is, it says "nothing was run" and something was.
        # The wall still stands, so pass on the part of it that is true. Asking
        # `refused` rather than naming the walls: this line named
        # `OUT_OF_CALLS` alone, and the clock became a second one unnoticed.
        got["budget"] = _gws_run.stood(budget)
    elif budget:
        got["budget"] = budget
    return got
