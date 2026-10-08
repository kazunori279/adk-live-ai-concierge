"""The few things about the household that outlive the conversation.

`recap` carries what was *said* across one rollover. This carries what is
*true* across all of them. The difference is the whole point: a recap is a note
about a conversation and is cleared the moment that conversation ends, and
under #142 a conversation now ends every time the panel goes back to being a
clock. Nobody is surprised that "and tomorrow?" stops working three minutes
after they walked away. Everybody is surprised to be asked their name twice a
day.

**Standing facts, and nothing else.** Who lives here and what they are called,
a preference somebody stated once, a correction somebody made. The test is
whether a person would be surprised to be asked again. Transcripts fail it,
which is why nothing here ingests a session: what gets remembered is written
one sentence at a time by the agent calling :func:`remember`, so the store
stays small enough to read out to the person it is about.

**On the row ADK already keeps per person.** Every key here is
`user:`-prefixed, so the SQLite backend files it in `user_states` rather than
on the session — measured on ADK 2.8.0, it survives `delete_session`, survives
a fresh service object over the same file, and comes back merged into both
`get_session` and `list_sessions`. That is the one storage layer in this repo
that already outlives a reboot, which #10 requires, and it costs no table, no
migration and no dependency. It is also why nothing here has to be carried by
hand the way `_plan_run.PAD_KEY` does: a roll writes a new session row, and the
facts are not on it.

**Why not an ADK `MemoryService`.** Three of them ship and none fits. Explicit
"remember this one fact" is `add_memory`, declared at
`memory/base_memory_service.py:97` and implemented only by
`VertexAiMemoryBankService` — measured, `InMemoryMemoryService` inherits the
`NotImplementedError` at `:117`. Automatic recall is `PreloadMemoryTool`, which
returns at `tools/preload_memory_tool.py:54-60` when `user_content` is unset,
and `Runner._new_invocation_context_for_live` (`runners.py:2543-2570`) never
sets it — so on the Live path it injects nothing and only `load_memory`, a call
the model has to decide to make mid-sentence, is left. And both Vertex services
need the `gcp` extra and a provisioned Agent Engine, which puts household
speech in a cloud store that outlives the device. `InMemoryMemoryService` dies
with the process.

**Replace, never append.** The store is keyed by topic, so stating a fact about
a topic that is already known overwrites it. That is the correction case, and
it is the reason this is a mapping rather than the list `_plan_run` keeps: two
contradictory memories are worse than neither, and a list has nowhere to put
the contradiction but side by side. The topic is matched without regard to
case, because "the car" and "The Car" are one topic to everybody except a
dictionary.

**One row key per topic, not one for the store.** Two pages on one profile are
not always on one thread — `main.py:1538-1542` says so — and a run reads the
session once (`runners.py:1963-1967`), so a write that carries the whole store
carries a snapshot of every topic the other page has touched since. Measured:
it silently erases them. A write that carries one key cannot, because the
SQLite backend merges the delta key by key (`_MERGE_STATE_SQL`,
`sqlite_session_service.py:54-66`). What a topic costs for that is an age of
its own, since key order on the row is the order things were last written and
runs backwards (measured on ADK 2.8.0) — so the value is `{"fact": …, "at": …}`
and "oldest" means the smallest `at` rather than the first key.

**Append-only in storage, capped in the prompt.** The thirty-fact cap is a
render-time cap, not storage eviction. ADK's `SqliteSessionService` merges
`state_delta` append-only into `user_states` and provides no key deletion API
in `State`. Historical keys therefore remain on the SQLite row as new topics
are added. :func:`kept` caps the window that reaches the system instruction to
the newest facts, and what falls off stays on the row unread until re-stated or
cleared by :func:`forgetting`.
"""

import logging
import threading

from google.adk.tools import FunctionTool, ToolContext

logger = logging.getLogger(__name__)

_MEMORY_LOCK = threading.RLock()
_GLOBAL_MAX_AT: int = 0

# One key per topic, shared across every session of one person — the same
# mechanism `recap` uses and for a stronger reason: a fact is about the person,
# not about a thread. Read back by `agent._instruction` through :func:`known`
# and cleared by `main.api_forget` through :func:`forgetting`, which are the
# only two other places that name any of this.
FACT_PREFIX = "user:fact:"

# What the first pass of this module wrote: the whole store as one mapping
# under one key. Read and never written, so a row written before the split
# still reads back. No migration runs and none is needed — re-stating a topic
# files it under its own key, which shadows the old entry, and eviction and
# `forgetting` both reach the old shape too.
FACTS_KEY = "user:household_facts"

# ESTIMATED, and sized against the prompt rather than against the disk. The
# row is bytes; the cost that matters is that every one of these is sent to
# the Live API in the system instruction on every setup. The instruction is
# about 8.7 KB today, so a full store adds roughly a sixth of it.
#
# Capped at render time (`kept()`) rather than evicted from SQLite storage:
# `SqliteSessionService` merges state_delta append-only, so historical keys
# remain on the row and only the prompt render window is capped.
#
# FACTS is the cap a person can hold in their head — the issue asks for a
# memory "small and nameable, so a person can be told what the terminal knows
# about them", and thirty is already more than anybody wants read back.
# FACT_CHARS is the one that usually bites: at the full FACT_LINE only eight
# fit, and a real standing fact is one short sentence.
FACTS = 30
FACT_CHARS = 1600
FACT_LINE = 200

# A handle, not a sentence. Long enough for "the car" or "alex's work address",
# short enough that it cannot smuggle the fact into its own key and defeat the
# replace.
TOPIC_LINE = 60

NOTHING_TO_KEEP = (
    "A fact needs both a topic and the fact itself. The topic is the short "
    "handle you would look it up under — \"the car\", \"alex's diet\" — and the "
    "fact is one sentence."
)

NOT_HERE = (
    "There is nobody to remember this for — no conversation is open. Say it "
    "in your answer instead."
)


def _one(text, limit: int) -> str:
    """One line of at most `limit` characters, or `""`.

    `str()` rather than a type check: this is called on whatever the model put
    in the argument, and a number or a bool is a fact somebody could reasonably
    state. Which is also why the empty test is against `None` rather than
    against truthiness — `0` and `False` are falsy and are both answers a
    person gives to "how many" and "do you".
    """
    return "" if text is None else " ".join(str(text).split())[:limit]


def kept(store) -> dict[str, str]:
    """The facts that fit, oldest dropped, whatever shape came back.

    Tolerant for the reason `_plan_run._kept` is: this comes off a row a
    previous release of this program wrote, and being asked the household's
    name again is a nuisance where failing the hello is a terminal that will
    not talk. A `dict` specifically — a list from an older release, or a bare
    string, reads as nothing rather than as a store of single characters.

    This is the prompt render cap: historical keys remain in SQLite storage
    due to ADK's append-only state merge, and `kept` selects the newest window
    within `FACTS` and `FACT_CHARS`.

    Newest wins on both caps, and insertion order is what "newest" means here:
    the mapping arrives oldest first, because :func:`_scan` sorted it that way
    and :func:`remember` builds on what `_scan` returned. Re-stating a known
    topic assigns in place rather than appending, so a correction does not
    promote itself past facts that were never wrong.
    """
    if not isinstance(store, dict):
        return {}
    out: dict[str, str] = {}
    total = 0
    for topic, fact in reversed(list(store.items())):
        name, line = _one(topic, TOPIC_LINE), _one(fact, FACT_LINE)
        if not name or not line:
            continue
        # Asked before the entry goes on and not after, so `FACT_CHARS` is the
        # budget the README advertises rather than that plus one more entry.
        # An entry cannot exceed the budget on its own — `TOPIC_LINE` plus
        # `FACT_LINE` is 260 against 1600 — so this cannot drop everything.
        if len(out) >= FACTS or total + len(name) + len(line) > FACT_CHARS:
            break
        out[name] = line
        total += len(name) + len(line)
    return dict(reversed(list(out.items())))


def _all(state) -> dict:
    """Every key on the row, whichever kind of state object this is.

    Three reach here and only one of them is a mapping. `State` is not —
    `sessions/state.py` gives it `get` and `__contains__` and no iteration at
    all — so `to_dict` is the only way to ask it what it holds. The
    instruction is built over a `ReadonlyContext`, and the checks pass plain
    dicts; both of those iterate.
    """
    if hasattr(state, "to_dict"):
        return state.to_dict()
    return dict(state or {})


def _age(at) -> int:
    """The `at` off a stored fact, or the oldest there is.

    Anything that is not a plain integer reads as 0, which files it before
    everything this release wrote and behind everything the old shape holds.
    `bool` is excluded on purpose: `True` is an `int` in Python and is not an
    age.
    """
    return at if isinstance(at, int) and not isinstance(at, bool) else 0


def _scan(state) -> dict[str, tuple[int, str]]:
    """Every fact on the row, topic to (age, fact), oldest first.

    Both shapes at once, because a row written before the split holds the old
    one and a row written since holds both. The old mapping is aged below
    zero: all of it was written before anything under a key of its own, and
    its own insertion order is the only thing that separates it internally.

    An empty value under a topic's own key is not a fact. Nothing writes one
    any more — eviction is left to :func:`kept` on the read path — but a row
    written by the release that did still holds them, and they bury the old
    shape's entry for the same topic. Headstones are not tracked as dead topics:
    `owned` already excludes legacy entries for any topic that has a key, and
    an empty key cannot overwrite a live fact on the same or another spelling.

    Topics that differ only in case are one topic. The oldest spelling is the
    one kept, so the render does not change under somebody who typed it
    differently, and the newest fact and the newest age are the ones under it
    — a topic that fell off and was said again is newly written even where the
    spelling it comes back under is the older one.
    """
    row = _all(state)
    seen: list[tuple[int, str, str]] = []
    # Headstones are excluded by value check; no dead set needed.
    # A topic with a key of its own supersedes the old mapping's entry for it,
    # whatever the two ages say. The key can only have been written since the
    # split, while a correction inherits the age of the folded topic — which
    # is the oldest spelling's — so a sibling spelling still sitting in the
    # old mapping is the newer of the two and overwrites every correction
    # written under the key.
    owned = {_one(key[len(FACT_PREFIX):], TOPIC_LINE).casefold()
             for key in row if key.startswith(FACT_PREFIX)
             and _one(key[len(FACT_PREFIX):], TOPIC_LINE)}
    legacy = row.get(FACTS_KEY)
    if isinstance(legacy, dict):
        for n, (topic, fact) in enumerate(legacy.items()):
            if _one(topic, TOPIC_LINE).casefold() in owned:
                continue
            seen.append((n - len(legacy), topic, fact))
    for key, value in row.items():
        if not key.startswith(FACT_PREFIX):
            continue
        topic = key[len(FACT_PREFIX):]
        if isinstance(value, dict):
            seen.append((_age(value.get("at")), topic, value.get("fact")))
        elif _one(value, FACT_LINE):
            seen.append((0, topic, value))
        # Empty values (headstones) are skipped and not added to seen.
        # No dead set is needed because owned handles legacy exclusion.
    out: dict[str, tuple[int, str]] = {}
    spelt: dict[str, str] = {}
    # Sorted on the topic as well, so two facts that were written far enough
    # apart to share an age still come back in one order rather than in
    # whatever order the row happened to hand them over.
    for at, topic, fact in sorted(seen, key=lambda held: (held[0], held[1])):
        name, line = _one(topic, TOPIC_LINE), _one(fact, FACT_LINE)
        if not name or not line:
            continue
        first = spelt.setdefault(name.casefold(), name)
        out[first] = (at, line)
    # Ordered on the age it ended up with, not on where its oldest spelling
    # first turned up. `kept` drops from the front, so a topic the household
    # said again a moment ago and an entry for it left in the old mapping put
    # the newest fact on the row at the front of the queue to be dropped.
    return {name: out[name] for name in sorted(out, key=lambda n: out[n][0])}


def _key(state, name: str) -> str:
    """The row key a write about `name` has to land on.

    Not always `FACT_PREFIX + name`. :func:`_scan` renders a folded topic under
    the oldest spelling it has seen, and since nothing is evicted on the write
    path a topic that fell off the end and was said again under the other
    spelling leaves two live keys for one topic. A write addressed by the
    rendered spelling then lands on the key that is not holding the fact and,
    on the age it inherited from the fold, ties with the one that is; the sort
    breaks that tie on the key string and can hand back either. So the write
    goes on the key the fold last read from, which is the one that decides what
    the household sees. Still exactly one key per call, and still only this
    topic.
    """
    best, ranked = FACT_PREFIX + name, None
    for key, value in _all(state).items():
        if not key.startswith(FACT_PREFIX):
            continue
        raw = key[len(FACT_PREFIX):]
        topic = _one(raw, TOPIC_LINE)
        if not topic or topic.casefold() != name.casefold():
            continue
        if isinstance(value, dict):
            at, live = _age(value.get("at")), bool(_one(value.get("fact"),
                                                        FACT_LINE))
        else:
            at, live = 0, bool(_one(value, FACT_LINE))
        # Ranked on `raw`, the key's own suffix, because that is what the read
        # is ranked on: `_scan` takes the suffix unfolded (`:225`), sorts on it
        # (`:237`) and folds only to choose the spelling it renders under.
        # Ranking here on the folded topic instead picks a different key from
        # the one the household reads out of whenever the two orders disagree,
        # which folding whitespace is enough to do: ` the car ` sorts before
        # `The Car` raw and after it folded. The correction is then lost on
        # exactly the row the line below exists for.
        if live and (ranked is None or (at, raw) > ranked):
            # `key` and not `FACT_PREFIX + topic`: a key an older release or a
            # hand-edited row spells with the whitespace still in it is read
            # under the folded topic but has to be written back where it is.
            best, ranked = key, (at, raw)
    return best


def _next(aged) -> int:
    """The age a fact written now takes: one past the newest on the row.

    Read off the snapshot the run armed with, so two pages writing at the same
    moment can land on the same age. That costs nothing — the tie breaks on
    the topic and is stable — and the alternative is a counter under a key of
    its own, which is the whole store under one key again.
    """
    global _GLOBAL_MAX_AT
    nxt = max((at for at, _ in aged.values()), default=0) + 1
    nxt = max(nxt, _GLOBAL_MAX_AT + 1)
    _GLOBAL_MAX_AT = nxt
    return nxt


def held(state) -> dict[str, str]:
    """What this person's row says the terminal knows about them."""
    try:
        return kept({topic: fact for topic, (_, fact) in _scan(state).items()})
    except Exception:  # pylint: disable=broad-except
        logger.warning("household facts unreadable, starting empty",
                       exc_info=True)
        return {}


def forgetting(state) -> dict:
    """The delta that leaves this person's row knowing nothing about them.

    Here rather than in `main.api_forget` because it is the shape of the store
    that decides it, and the shape is this module's. Both kinds of key, and
    the new ones read off the row rather than rebuilt from the topics: a key
    whose topic does not survive :func:`_one` still has to be cleared.
    """
    return {FACTS_KEY: {},
            **{key: "" for key in _all(state) if key.startswith(FACT_PREFIX)}}


def known(context) -> str:
    """The facts as the instruction renders them, or `""`.

    `context` is anything carrying a `.state`, the same contract as
    :func:`recap.recalled` and :func:`tools.maps.here`, because the instruction
    is built from a `ReadonlyContext`.
    """
    facts = held(getattr(context, "state", None) or {})
    return "\n".join(f"- {topic}: {fact}" for topic, fact in facts.items())


def remember(topic: str, fact: str, tool_context: ToolContext) -> dict:
    """Keep one standing fact about this household past this conversation.

    Args:
      topic: the short handle to file it under, two or three words — "the
        car", "alex's diet", "the wifi". Re-use the exact topic already listed
        under what you know about this household when you are correcting
        something, because that is what replaces the old fact instead of
        leaving both.
      fact: the fact itself, one sentence, in the language it was said in.

    Returns:
      What is now known: the topic filed, how many facts are held, and how
      many fell off the end.
    """
    with _MEMORY_LOCK:
        name, line = _one(topic, TOPIC_LINE), _one(fact, FACT_LINE)
        if not name or not line:
            return {"kept": False, "why": NOTHING_TO_KEEP}
        state = getattr(tool_context, "state", None)
        if state is None:
            return {"kept": False, "why": NOT_HERE}
        aged = _scan(state)
        # Two views, because the row holds more than it shows. Nothing is evicted
        # on the write path any more, so what the caps push off the end stays on
        # the row — and the read path caps the whole of it, from the front, where
        # that overflow is. `every` is the view :func:`held` will take; `facts` is
        # what the household can see now, which is what a correction folds against
        # and what the count in the answer is about.
        every = {known_topic: known_fact
                 for known_topic, (_, known_fact) in aged.items()}
        facts = kept(every)
        # The spelling already on the row wins. Only the case can differ by now —
        # `_one` has folded the whitespace and cut the length — and if it does,
        # this is the line that makes the write a correction rather than a second
        # entry the model has no way to avoid asking for.
        name = next((t for t in facts if t.casefold() == name.casefold()), name)
        # What the store would hold if nothing fell off: the same count when this
        # is a correction, one more when it is news. Taken before the write,
        # because the write is what makes the two indistinguishable.
        want = len(facts) + (0 if name in facts else 1)
        # A correction keeps the age it already had, so a fact nobody had to fix
        # is not aged out by somebody fixing an older one.
        at = aged[name][0] if name in aged else _next(aged)
        # Assignment and not an append, and this line is the correction case. A
        # known topic overwrites in place; an unknown one goes on the end, where
        # `kept` drops from the front once the caps bite.
        every[name] = line
        capped = kept(every)
        if name not in capped:
            # Unless the budget would eat the very fact being corrected, which is
            # both facts lost and a `kept: True` saying otherwise. Re-filed as the
            # newest instead: one entry cannot exceed the budget on its own —
            # `TOPIC_LINE` plus `FACT_LINE` is 260 against 1600 — so this always
            # keeps it, and it only runs on the store that would have lost it.
            # Asked of `every` and not of what is readable now, because a store
            # that shows nine facts and holds twelve caps deeper than it looks.
            at = _next(aged)
            del every[name]
            every[name] = line
            capped = kept(every)
        # Exactly one key, and only ever the topic this call was given, because a
        # delta that carries a topic this call did not touch overwrites whatever
        # the other page on this profile did to it — and the snapshot this run
        # armed with is too old to say which topic that would be. So nothing is
        # evicted on the write path at all: `kept` caps the store on every read,
        # and what falls off stays on the row unread rather than being blanked out
        # from under another page. `State.__setitem__` (`sessions/state.py:91-98`)
        # is what puts a key in `EventActions.state_delta`.
        state[_key(state, name)] = {"fact": line, "at": at}
        logger.info("remembered %r, %d facts held", name, len(capped))
        return {"kept": True, "topic": name, "known": len(capped),
                "dropped": max(0, want - len(capped))}


remember_tool = FunctionTool(remember)
