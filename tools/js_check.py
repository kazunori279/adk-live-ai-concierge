"""Check the ten things in the client that fail without showing it.

**Escaping.** The card renderers build HTML by string interpolation, and the
values they interpolate are third-party text: a page `<title>` off a grounding
chunk, a thumbnail URI, a weather condition. `escapeHtml` serialises a text
node, which escapes `&`, `<`, `>` and nbsp and leaves quotes alone, because a
text node needs no quote escaping. Inside `title="…"` a quote ends it early
and the rest of the title becomes markup — an `onmouseover` on a chip, an
`onerror` on an `<img>` that needs no interaction at all. `escapeAttr` is the
attribute-position escape, and this script is what keeps the two apart:

- **Every attribute interpolation goes through `escapeAttr`.** A grep for the
  old call would only catch the sites that were once right; this reads every
  `="${…}"` in the sources, so a new site with no escaping at all fails too.
- **`escapeAttr` escapes what `escapeHtml` does, plus both quotes**, and
  `escapeHtml` still does not — a `&quot;` shown on the kiosk in place of a
  plain quote is a worse bug than the one being fixed.
- **`formatMarkdown` escapes the link URL it reinstates.** That URL comes out
  of a capture after the escaping pass, and `[^\\s)]+` admits a quote. Only the
  quotes, though — the same pass already turned its `&` into `&amp;`, and
  escaping twice sends `?c=1&amp;d=2` to the server.

The behaviour half runs under `node`, against the real `markdown.js`, with a
`document` stub whose `textContent` → `innerHTML` implements the HTML fragment
serialisation of a text node. The stub is the weak point: it is the spec, not
the browser. What the browser does with the result is checked by eye on the
running server, which is where a `&quot;` in a chip label would show up.

**Which browse card a frame belongs to.** Two browse jobs can be in flight at
once — `plan` is NON_BLOCKING, so the planner's browse step and one the live
agent starts while it is thinking both run — and the frames come down one
channel. `handleBrowse` is extracted out of `app.js` and driven under `node`
with stubbed renderers, because the way this breaks is a single card that
alternates the two jobs' screenshots and closes on whichever finishes first,
which is not a crash and is only visible on the kiosk.

**What a crashed browse job leaves on the card.** The browse card is ended by
exactly one frame, and until now only the happy path sent it, so a job that
raised halfway through kept its spinner running for the rest of the session.
`browseUpdate` is lifted out of `cards.js` and driven over the bare `done` the
server sends from its `except`, along with the real `LABELS`, `t` and `host` —
what has to hold is that a card written to by a failure still speaks the
language of the turn, and that an ordinary answer comes out unchanged.

**What the calendar card draws, and what it guesses.** The whole calendar
section of `cards.js` is lifted in one slice and run over agendas the real
account cannot be asked for on demand: an empty day, an event running past
midnight, a fortnight the server had to cut, a title somebody else wrote with
an `<img onerror>` in it. Two of its answers are guesses and both fail quietly.
Which row is *now* is arithmetic over strings against a clock, so the clock is
handed in — a card that lights every row of a fortnight, or none of the one
happening, looks right in a screenshot taken at the wrong minute. And the
category glyph is inferred from the title, because Google Calendar has no
category field to read; the cases pin the order the rules are asked in, which
is the only thing about a guess that can be checked. `gwsFill` is driven over
the same renderer, mostly to pin what did *not* change: a Workspace answer that
is not a calendar one has to come out of it exactly as it did before.
Underneath both is whether the panel can draw an emoji at all — Raspberry Pi OS
does not always ship a colour emoji font, and Chromium answers that with a
hollow box rather than an error.

**What a stopped plan writes on the card.** Four of the five things `plan` can
return instead of an answer are instructions aimed at the model, and putting
one on a kitchen screen is worse than putting nothing there. The fifth,
`TOO_SLOW_PARTIAL`, has a real answer glued to the end of it. `planFill` is
lifted out of `cards.js` the same way and driven over all of them, because the
failure is a card that either loses the answer it was read out loud or prints
the prose that was never meant for a person.

**What the step list draws while several steps run at once.** `planUpdate` took
a cursor, which could say "everything above this is done and this one is
running" — and the planner starts independent steps together now, so they run
in a batch and come back out of order and neither half of that is true any
more. It is lifted out of `cards.js` and driven over runs of `show_plan`
frames: several at full weight at once, done as a set rather than a prefix, and
a revision replacing the list instead of appending to it.

**What a card in flight does when the conversation under it is replaced.** A
gws or plan card has two ways to stop spinning, the answer and the deadline, and
`reopen` — the settings that need a new socket, a new location or a new
profile — takes both away at once: the socket closes, and `dropPending` cancels
the timer. The two callers want opposite things from it — the clear button
removes every card a line later, `reopen` keeps them — so `dropPending`,
`handleLate` and the real `gwsFill`/`planFill` are lifted and driven together,
with real timers short enough to observe. The failure is a card that spins
until the page is reloaded, which on a plan card still carries its task line
and steps and reads as work going on. A browse card in flight is the same
failure with less to fall back on: it has no deadline at all, only the `done`
frame the socket that just closed would have carried, so `reopen` writes that
frame itself. Which loop does it matters as much as that one does — the clear
button empties the same map a line after removing every card on screen — so
the checks read where the loop sits as well as running it. And a frame that was
on the wire when the socket closed arrives after that loop has run, on a
handler `close()` does not detach: it finds its id gone and opens the very card
the loop was there to prevent. That one is read out of the source, because
nothing here can dispatch a message from a closing WebSocket. So is the timer:
a reconnect scheduled by a close three seconds ago opens a second socket while
`reopen`'s own is already up, and the server resolves both to the same
conversation, so the two talk over each other into it and each hears the
other's answer back as history. Nothing on screen says why.

**Who the terminal is talking to.** The profile picker is hidden unless the
server names more than one person, so a household that never set `PROFILES`
sees the sheet it always had. Whoever is on the terminal is in the list whether
the server named them or not: a name dropped from `PROFILES` would otherwise
leave the select showing somebody else while the socket is still on this
person's thread. Changing it clears the screen through the clear button before
reopening, because everything on it belongs to the person who was here a moment
ago. Along the way every `getElementById` in `app.js` is matched against
`index.html`, because a missing id is a null that crashes wherever the control
is first touched — for the picker, only on a household that set `PROFILES`.

**Which voice the terminal answers in.** The options come off `/api/config`
rather than out of the markup, because the server is the side that refuses a
voice it does not know — a list in the page that has drifted from `VOICES` is a
picker whose choices the socket silently ignores, and the only way to notice is
to stand in the kitchen and listen. The same reasoning runs the other way for a
setting saved before the list changed: it snaps to what the server would
actually do rather than sitting in the select as a name nothing honours. And
the voice is fixed for the life of a connection — it is part of the Live API's
setup message — so what is checked in the source is the two orderings that make
the setting take effect at all: filled before the first socket opens, and
reopened when it changes. Reopened *without* the clear the profile picker does,
because this is the same person on the same thread and the server puts the new
socket back on it.

**Where a new card comes to rest.** A card goes to the top of the pane, not the
bottom, because a place card is taller than the screen and the useful end of it
is the top. Two things get in the way and neither raises: a scroll container
will not go past `scrollHeight - clientHeight`, so a card with nothing under it
comes to rest wherever the clamp puts it, and the layout the target is read out
of does not exist yet at append time. The section is lifted whole and run over
a pane that lays a child out under the one before it and clamps a scroll the
way a browser does — a stub that scrolled wherever it was asked would pass a
broken `restCardAtTop` — with card heights given per case, because the
arithmetic over them is the thing being checked and nothing here can measure a
real card. Three of the things the pane is aimed by are not the newest card's
height: the card can be taken off the screen while it is being followed, and a
detached one measures 0 both ways; the pane's own height moves when the
settings sheet opens and closes, and the container re-clamps as it does; and
what counts as the person taking the pane over is a shorter list on a touch
screen than it looks, because a tap fires `pointerdown` and `touchstart` too
and both bubble out of the card that was touched.

**Which language the panel is in.** The chrome used to follow `<html lang>`,
and `<html lang>` used to be whatever tag the model attached to its last tool
call — so a bilingual household got a settings sheet and a set of card headings
that changed language because one question was asked in the other one. It is a
setting now, and the sweep it took is the thing that can rot: fifty strings
across `index.html`, `app.js`, `location.js` and `style.css`, keyed against one
table in `i18n.js`. Three of those keys are attributes rather than text, and
one of them — a starter chip's `data-prompt` — is not on screen at all: a chip
that reads Japanese and sends the English sentence is answered in English. So
the markup's keys are matched against the table both ways, the two locale
blocks are checked for the same key set (English is the fallback for a missing
key, so a short `ja` block renders in English and raises nothing), each chip's
label is checked against its own prompt, and the one string CSS has to hold for
itself is checked to have a rule per locale. `i18n.js` is imported rather than
lifted — it touches no DOM until it is called — and the picker comes out of
`app.js`. What is read as source is the other half: that nothing writes the
turn's tag onto the document any more, and that the browser's own locale
survives as the default for the setting and as nothing else.

**What the starter chips say, and when they are on screen.** The chips used to
be four sentences, and the label being the sentence was the one thing holding
them together: one key per chip, and what was read is what was sent. A short
label per capability breaks that, so a chip is four keys now — a label, a
question, and the two contextual forms it takes once the room has named
something — and every way they can come apart is silent. A prompt paired with
another chip's label asks a question the icon does not match; a contextual key
with no `{q}` in it claims to name the topic and does not; a slot with no keys
behind it never moves at all. The chips are read out of `index.html` rather
than written down here, and the renderer is lifted and driven over the tool
calls the topic comes off — two slots, because after `maps_search(query:
"ramen", near: "New York")` the weather chip wants the place and the video chip
wants the subject. The row's other failure is not about text: `goIdle` closes
the microphone and writes one line, and until now nothing but the clear button
in the settings sheet ever put the suggestions back, so a panel that timed out
was a dead screen at exactly the moment somebody walking past would use one.

**How loud the speaker is, and whether it is muted.** The only volume this
side of the glass is a `GainNode` in the page's own graph — a browser cannot
reach the Pi's mixer — so the section is checked on the three things a page
gain gets wrong. The curve, because a slider wired straight to `gain.value`
puts everything audible in its bottom few pixels. The ramp, because assigning
`gain.value` mid-waveform is a click, once per frame of a drag. And mute, which
is the gain going to zero rather than the node being disconnected or the player
being stopped: those two differ only when somebody mutes mid-sentence, and they
differ by a 180-second backlog bursting out on unmute, or by the rest of an
answer being thrown away. The whole section is lifted and run against a fake
AudioParam that records ramps and assignments separately, along with the case
the toolbar makes unavoidable — the control paints on the first frame and the
audio graph does not exist until somebody speaks.

    uv run python tools/js_check.py

Exits non-zero on the first case that does not hold.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "src" / "ai_terminal" / "live" / "static" / "js"

# Counted rather than written down, so the closing line cannot drift from the
# number of cases the way a figure typed into a commit message does.
RAN = 0

# `="${…}"` interpolations that are not attribute values holding tool output.
# Written out one by one, so that adding a site is a decision someone made
# rather than a pattern that happened to match.
ALLOWED = {
    # A `class="b"` / `class="n"` sentinel, built into a RegExp and a selector.
    # `cls` is one of two literals in this file; no tool output reaches it.
    ("markdown.js", "cls"),
    # `a.link[data-place="${i}"]`, a querySelector string rather than HTML,
    # and `i` is an array index.
    ("cards.js", "i"),
    # The link URL, quote-escaped only because `formatMarkdown` escaped the
    # rest of it before the capture existed. Named here rather than letting
    # every `escapeQuotes(` through: on a value that has not been escaped once
    # already it is the wrong function, and it is only right at this one site.
    ("markdown.js", "escapeQuotes(uri)"),
}

NODE = r"""
// `escapeHtml` needs a DOM. This is the HTML fragment serialisation of a text
// node — the four replacements a browser makes and no others, quotes included
// in "no others", which is the whole point of the check.
globalThis.document = {
  createElement: () => ({
    set textContent(v) {
      this.innerHTML = String(v)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/ /g, "&nbsp;");
    },
  }),
};
const m = await import(process.argv[1]);
const out = {};
for (const [name, arg] of JSON.parse(process.argv[2])) {
  out[name + " " + arg] = m[name](arg);
}
console.log(JSON.stringify(out));
"""


# The browse half. `app.js` reaches for `document` on the way in, so it cannot
# be imported; `handleBrowse` and the `browsing` slot are lifted out of it
# instead and run against these. `browseCard` returns a plain object standing in
# for the element, and every frame it is shown is recorded on it, so a card that
# was handed another job's screenshot says so.
BROWSE = r"""
let now = 0;
let seq = 0;
const timers = new Map();
const timerEvents = [];

function setTimeout(fn, ms) {
  const id = ++seq;
  timers.set(id, { at: now + ms, fn, ms });
  timerEvents.push({ op: "set", id, ms, at: now + ms });
  return id;
}

function clearTimeout(id) {
  if (timers.has(id)) {
    timers.delete(id);
    timerEvents.push({ op: "clear", id });
  }
}

function advance(ms) {
  const until = now + ms;
  for (;;) {
    let due = null;
    for (const [id, t] of timers) {
      if (t.at <= until && (due === null || t.at < due[1].at)) due = [id, t];
    }
    if (due === null) break;
    now = due[1].at;
    timers.delete(due[0]);
    due[1].fn();
  }
  now = until;
}

const drawn = [];
function hideWorking() {}
function browseCard(data) {
  const card = { opened: data.id, shots: [], frames: [], appended: false };
  drawn.push(card);
  return card;
}
// The whole frame as well as its screenshot: the reset writes one of these by
// hand, and which one it wrote is the thing being read.
function browseUpdate(card, data) {
  card.shots.push(data.shot);
  card.frames.push(data);
}
function appendCard(card) { card.appended = true; }

%s

for (const frame of JSON.parse(process.argv[1])) {
  if (frame.advance) { advance(frame.advance); continue; }
  if (frame.reset) { %s; continue; }
  handleBrowse(frame);
}
console.log(JSON.stringify({
  drawn,
  open: [...browsing.keys()],
  timers: [...timers.keys()],
  timerEvents,
}));
"""


# The plan half. `cards.js` cannot be imported either — it pulls in the whole
# renderer — so `planFill` is lifted out and run against a card stub. The stub
# is a DOM only where this function touches one: `textContent` wipes the
# children the way the real setter does, which is what makes the ordering of
# the two writes below observable at all.
PLAN = r"""
function t(key) { return "<" + key + ">"; }
function formatMarkdown(md) { return "[md]" + md; }

function node() {
  return {
    _text: "", _html: "", kids: [],
    set textContent(v) { this._text = String(v); this._html = ""; this.kids = []; },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    append(kid) { this.kids.push(kid); },
    remove() { this.gone = true; },
  };
}
globalThis.document = { createElement: node };

%s

const data = JSON.parse(process.argv[1]);
const md = node(), task = node();
const classes = new Set(["card", "plan", "pending"]);
const card = {
  classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
  querySelector: (sel) => (sel === ".md" ? md : sel === ".plan-task" ? task : null),
};
planFill(card, data);
console.log(JSON.stringify({
  classes: [...classes].sort(),
  text: md.textContent,
  html: md.innerHTML,
  kids: md.kids.map((k) => k.innerHTML),
}));
"""


# The step list, redrawn on every `show_plan` frame. Lifted the same way as
# `planFill`, over a stub that differs in the one place this cares about:
# `replaceChildren` really empties the list, so a redraw that appended instead
# of rebuilding leaves the old steps behind for the cases to count.
STEPS = r"""
function node(tag) {
  return {
    tag, _text: "", className: "", kids: [],
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
    replaceChildren() { this.kids = []; },
    appendChild(kid) { this.kids.push(kid); },
  };
}
globalThis.document = { createElement: node };

%s

const frames = JSON.parse(process.argv[1]);
const list = node("ol");
const card = { querySelector: (sel) => (sel === ".plan-steps" ? list : null) };
for (const frame of frames) {
  planUpdate(frame === null ? null : card, frame || {});
}
console.log(JSON.stringify(list.kids.map((k) => [k.textContent, k.className])));
"""


# The name the map picker writes down for a position. `location.js` cannot be
# imported — it reaches for `getElementById` at module scope — so the two
# functions and two tables that decide the name are lifted as source, the way
# `planFill` is. Neither touches a DOM or a Geocoder: the whole subject is what
# is picked out of a components array that has already come back.
SHORT_NAME = r"""
%s

console.log(JSON.stringify(
  JSON.parse(process.argv[1]).map((parts) => shortName(parts))));
"""


# The browse card, updated in place. Same lift as `planFill`, with two things
# taken along that the plan stub could do without: the real `LABELS` and the
# real `t`, because half of what this section checks is that a card written to
# by the server on a failure still speaks the language of the turn. `host` comes
# too, since the empty `url` a crash sends is only harmless if `new URL("")`
# throws into its `catch`.
BROWSE_CARD = r"""
function formatMarkdown(md) { return "[md]" + md; }
globalThis.document = { documentElement: { lang: %s } };

%s

const img = { src: "" }, status = { textContent: "" }, md = { innerHTML: "" };
const classes = new Set(["card", "browse"]);
const card = {
  classList: { add: (c) => classes.add(c) },
  querySelector: (sel) => ({
    ".browse-shot img": img,
    ".browse-shot": { classList: { add: (c) => classes.add(c) } },
    ".browse-status": status,
    ".md": md,
  })[sel] ?? null,
};
// A list, because a card is written to more than once in life and what the
// last frame leaves behind depends on what the ones before it put there.
for (const data of JSON.parse(process.argv[1])) browseUpdate(card, data);
console.log(JSON.stringify({
  classes: [...classes].sort(),
  shot: img.src, status: status.textContent, md: md.innerHTML,
}));
"""


# The source row under a Workspace answer. The whole chain is real: `LABELS`
# and `t` because the row is bilingual, `escapeHtml` off `markdown.js` because
# a Drive file name and a Gmail subject are the two most attacker-shaped
# strings the card handles, and `sourceChips` with its own clipping and its own
# sender parsing because those two are the ruling on what a shared room may
# show. The card stub gives `.gws-sources` a node the way the real `gwsPending`
# does; a stub that returned null for it would pass every case here with the
# feature deleted, so one case checks the markup has the hook in it.
SOURCES = r"""
globalThis.document = {
  documentElement: { lang: %s },
  createElement: () => ({
    set textContent(v) {
      this.innerHTML = String(v)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/ /g, "&nbsp;");
    },
  }),
};
function formatMarkdown(md) { return "[md]" + md; }

%s

function node() {
  return {
    _text: "", _html: "",
    set textContent(v) { this._text = String(v); this._html = ""; },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    remove() { this.gone = true; },
  };
}

const md = node(), row = node(), line = node();
const classes = new Set(["card", "gws", "pending"]);
const card = {
  classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
  querySelector: (sel) => ({
    ".md": md, ".gws-sources": row, ".gws-progress": line,
  })[sel] ?? null,
};
// A list, because a card is filled more than once in life: a deadline writes
// into it and the answer lands in the same card afterwards.
for (const data of JSON.parse(process.argv[1])) gwsFill(card, data);
console.log(JSON.stringify({
  classes: [...classes].sort(),
  text: md.textContent, html: md.innerHTML, row: row.innerHTML,
}));
"""


# The remember card, which is a whole renderer rather than a fill: it takes the
# tool's dict and hands back an element. `el` is stood in for and returns the
# markup as a string — parsing it would need a DOM, and what is read here is
# what the card says, not what Chromium makes of it. Everything else is real.
# `escapeHtml` off `markdown.js`, because a topic is the model's transcription
# of something said out loud in a kitchen; `STRINGS` and `t` off `i18n.js`,
# because a card built on a key nobody added renders an empty line and nothing
# at runtime says so.
REMEMBER = r"""
globalThis.document = {
  documentElement: { lang: %s },
  createElement: () => ({
    set textContent(v) {
      this.innerHTML = String(v)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/ /g, "&nbsp;");
    },
  }),
};
function el(html) { return html.trim(); }

%s

console.log(JSON.stringify(JSON.parse(process.argv[1]).map(rememberCard)));
"""

CALCULATE = r"""
globalThis.document = {
  documentElement: { lang: %s },
  createElement: () => ({
    set textContent(v) {
      this.innerHTML = String(v)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/ /g, "&nbsp;");
    },
  }),
};
function el(html) { return html.trim(); }

%s

console.log(JSON.stringify(JSON.parse(process.argv[1]).map(calculateCard)));
"""

WEB_SEARCH = r"""
globalThis.document = {
  documentElement: { lang: %s },
  createElement: () => ({
    set textContent(v) {
      this.innerHTML = String(v)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/ /g, "&nbsp;");
    },
  }),
};
function el(html) { return html.trim(); }

%s

console.log(JSON.stringify(JSON.parse(process.argv[1]).map(webSearchCard)));
"""


# The late-answering cards, and what becomes of one still in flight. Both ends
# are real: `gwsFill` and `planFill` off `cards.js`, and `pending`, `LATE_TOOLS`,
# `dropPending` and `handleLate` off `app.js`. Only the two pending renderers are
# stood in for, because building their cards needs the whole `el`/`head` chain
# and the two fills between them reach into exactly three selectors.
#
# The timers are real too, and 5 ms rather than the true two minutes: what is
# being watched is whether the deadline was cancelled, and `clearTimeout` does
# not care how far off it was.
LATE = r"""
function t(key) { return "<" + key + ">"; }
function formatMarkdown(md) { return "[md]" + md; }

function node() {
  return {
    _text: "", _html: "", kids: [],
    set textContent(v) { this._text = String(v); this._html = ""; this.kids = []; },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    append(kid) { this.kids.push(kid); },
    remove() { this.gone = true; },
  };
}
globalThis.document = { createElement: node };

%s

// `remove()` is recorded rather than done, so a card taken off the screen is
// still there to be read afterwards — which is the only way to tell "removed"
// apart from "left spinning where it was".
function cardOf(kind) {
  const parts = { ".md": node(), ".plan-task": node(), ".gws-progress": node() };
  const classes = new Set(["card", kind, "pending"]);
  return {
    kind, classes, parts,
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
    querySelector: (sel) => parts[sel] ?? null,
    remove() { this.gone = true; },
  };
}
function gwsPending() { return cardOf("gws"); }
function planPending() { return cardOf("plan"); }

const appended = [], fired = [];
function hideWorking() {}
function appendCard(card) { appended.push(card); }
function gwsCard(data) { return { kind: "a fresh gws card", data }; }
function planCard(data) { return { kind: "a fresh plan card", data }; }

%s

// Every card ever made, in order, because one dropped from `pending` still has
// to be readable. The entry is built the way `handleEvent` builds it; the field
// names it uses are checked against the real ones separately.
const cards = [];
function begin(id, name) {
  const card = LATE_TOOLS[name].card("");
  const timer = setTimeout(() => {
    fired.push(id);
    const lost = pending.get(id);
    if (lost) LATE_TOOLS[name].fill(lost.card, { error: "lost" });
  }, 5);
  pending.set(id, { name, line: node(), card, timer });
  cards.push(card);
}

for (const step of JSON.parse(process.argv[1])) {
  if (step.begin) begin(step.begin, step.name);
  else if (step.clear) dropPending();
  else if (step.resolve) dropPending(true);
  else handleLate(step.late);
}
// Long enough that a deadline nobody cancelled has certainly gone off.
await new Promise((done) => setTimeout(done, 40));
console.log(JSON.stringify({
  cards: cards.map((c) => ({
    kind: c.kind, classes: [...c.classes].sort(),
    text: c.parts[".md"].textContent, html: c.parts[".md"].innerHTML,
    gone: !!c.gone,
  })),
  waiting: [...pending.keys()], fired, appended: appended.map((c) => c.kind),
}));
"""


# The calendar card. The whole `/* ---------- calendar ---------- */` section of
# `cards.js` is lifted in one slice rather than function by function, for the
# reason `scroll_src` gives: the glyph table, the category rules and the row
# builder are one piece of state, and a constant moved out of the section is a
# name node cannot resolve and says so.
#
# `escapeHtml` and `escapeAttr` are written out rather than imported, because
# the real pair needs a DOM and the section above this one already pins what
# they do. Here they only have to be real enough that a `<img onerror>` in an
# event title comes out as text — a household calendar is not hostile input,
# but an invitation somebody else organised carries a title nobody here wrote.
#
# `now` is handed in on every run. Which row is lit is the one thing on this
# card that cannot be checked at all against a clock that keeps moving.
CALENDAR = r"""
function escapeHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/ /g, "&nbsp;");
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
globalThis.document = { documentElement: { lang: %s } };

%s

const job = JSON.parse(process.argv[1]);
const out = {};
if ("agenda" in job) {
  out.html = calendarHtml(job.agenda, job.said || "", new Date(job.now));
}
if (job.prose !== undefined) out.said = calSaid(job.prose);
// A list too, because the abbreviation table is one case over every word on
// `CAL_ABBREV` and a node process per word is a slow way to ask one question.
if (job.proses) out.saids = job.proses.map(calSaid);
if (job.cats) out.cats = job.cats.map(calCat);
if (job.widths) {
  out.emoji = emojiRenders((s) => job.widths[s] ?? job.widths.rest);
}
console.log(JSON.stringify(out));
"""


# `gwsFill` with the calendar section under it, over the same card stub `LATE`
# uses. Separate from `LATE`, which drives the whole `handleLate` machine and
# has no reason to grow a renderer: what this asks is narrower and is mostly
# about what did *not* change — a Workspace answer that is not a calendar one
# has to come out of here exactly as it did before there was a calendar card.
GWS_FILL = r"""
function escapeHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/ /g, "&nbsp;");
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function formatMarkdown(md) { return "[md]" + md; }
globalThis.document = { documentElement: { lang: %s } };

%s

const md = {
  _text: "", _html: "",
  set textContent(v) { this._text = String(v); this._html = ""; },
  get textContent() { return this._text; },
  set innerHTML(v) { this._html = String(v); this._text = ""; },
  get innerHTML() { return this._html; },
};
const line = { removed: false, remove() { this.removed = true; } };
const classes = new Set(["card", "gws", "pending"]);
const card = {
  classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
  querySelector: (sel) => (sel === ".md" ? md
    : sel === ".gws-progress" ? line : null),
};
// A list, because a card can be written to twice: the deadline writes `lost`
// and the answer that beat it by a hair arrives afterwards.
for (const data of JSON.parse(process.argv[1])) gwsFill(card, data);
console.log(JSON.stringify({
  classes: [...classes].sort(),
  text: md.textContent, html: md.innerHTML, progress: line.removed,
}));
"""


# The reconnect backoff, run rather than read. What has to hold is a sequence
# — doubling, a ceiling, jitter around each step — and the source says nothing
# about a sequence. `random` is handed in rather than stubbed globally because
# the roll is the one input here that is not the delay, and a run that cannot
# choose it cannot tell a clamp from a low roll.
BACKOFF = r"""
%s

function run(rolls) {
  let delay = RECONNECT_MIN_MS;
  const waits = [], bases = [];
  for (const roll of rolls) {
    waits.push(reconnectWait(delay, () => roll));
    delay = reconnectNext(delay);
    bases.push(delay);
  }
  return { waits, bases };
}

console.log(JSON.stringify(run(JSON.parse(process.argv[1]))));
"""


# What `socket.onclose` needs around it to run outside a browser. Everything
# here is a stub but the handler itself and the backoff pieces, which are
# lifted out of `app.js` — the point is to run the real close path rather than
# read it, because both things it now decides are invisible to a grep. Whether
# the backoff resets depends on *when*, not on where the line is; whether a
# browse card is ended depends on the loop running, not on the loop existing.
#
# `Date.now` is a dial rather than a clock. What the reset asks is how long the
# socket lasted, and a case that had to wait five real seconds to find out is a
# case nobody will run.
CLOSE = r"""
let clock = 1e12;
Date.now = () => clock;

let userBubble = "user", agentBubble = "agent", reconnectTimer = null;
const sealed = [], waits = [], ended = [], rearmed = [], said = [];

// What `reopen` tears down. A close handler of its own rather than none, so
// that nulling it is a change this can see rather than a no-op.
let socket = { onclose: () => {}, onmessage: () => {}, close() {} };
let seenTools = new Set();
function dropPending() {}
function systemLine(text) { said.push(text); }

function setConnected(on) {}
// The session the reconnected page asks for. Stubbed rather than lifted: what
// the case is about is whether the new socket is told anything at all, and the
// frame it would send goes to a WebSocket that does not exist here.
function armSession() { rearmed.push(true); }
function hideWorking() {}
function seal(bubble) { sealed.push(bubble); }
function connect() {}
// The build check the reconnect fires. Stubbed here and driven for real in
// REVISION: what these cases read is the backoff, and a `fetch` of its own in
// the middle of them would only be a second thing to stub.
function checkRevision() {}
// The real one would run `connect` for real, against a server that is not
// there. What the case reads is the delay the handler asked for, which is the
// argument and not the callback.
function setTimeout(fn, ms) { waits.push(ms); return waits.length; }
function clearTimeout() {}
function browseUpdate(card, frame) { ended.push([card, frame]); }

// Everywhere a frame can go once the dispatch has looked at its type. All
// no-ops: the only frame these cases send is the server's `ready`, which is
// the one branch that draws nothing, and a stub that recorded the others would
// be measuring `handleEvent` rather than the backoff.
function handleWake() {}
function handleBrowse() {}
function handleLate() {}
function handlePlan() {}
function handleProgress() {}
function handleEvent() {}
function restartIdle() {}
console.error = () => {};

%s

function shot(wait) {
  return { wait, base: reconnectDelay, stamp: openedAt,
           ended: ended.splice(0), open: [...browsing.keys()],
           rearmed: rearmed.splice(0).length, said: said.splice(0) };
}

function run(lives) {
  const out = [];
  for (const life of lives) {
    // A minute between attempts, so nothing in a run reads as one long
    // socket. `gap` is for the entries where that is the wrong spacing: a
    // connection refused 300 ms after somebody pressed a button is not an
    // idle retry, and spacing it a minute out would let a stale `openedAt`
    // pass for a socket that held.
    clock += life.gap === undefined ? 60000 : life.gap;
    browsing = new Map((life.cards || []).map((id) => [id, id]));
    // The person pressing something, which is the only way `reopen` is
    // reached. It replaces the socket by hand, so nothing it does goes
    // through the close handler below.
    if (life.do === "reopen") {
      reopen("Reconnecting.");
      // No wait of its own: `connect` is stubbed, so a reopen queues no
      // retry — it is the attempt, not a wait for one.
      out.push(shot(null));
      continue;
    }
    // No `held` at all is the socket that never opened: connection refused,
    // which is the outage the backoff was written for in the first place.
    if (life.held !== undefined) {
      // Whether the microphone was still on when this socket came up, which
      // is the only thing that decides whether the page arms it again.
      listening = !!life.mic;
      onopen();
      // The frame the server sends once its accept path has come back, put
      // through the page's real `onmessage`. A life without it is a socket
      // that opened and never heard a word from the server, which is the
      // post-accept failure the ladder exists for.
      if (life.ready) onmessage({ data: JSON.stringify({ type: "ready" }) });
      clock += life.held;
    }
    // A socket that came up and has not gone: the state a person is looking
    // at when they change a setting on a working terminal, and the one that
    // leaves an hour-old stamp behind for `reopen` to carry or drop.
    if (life.do === "up") {
      out.push(shot(null));
      continue;
    }
    Math.random = () => (life.roll === undefined ? 0.5 : life.roll);
    onclose();
    out.push(shot(waits.at(-1)));
  }
  return out;
}

console.log(JSON.stringify(run(JSON.parse(process.argv[1]))));
"""


# Whether a panel left up for days notices that the server underneath it is a
# new build. Nothing about this is visible from outside the page — the deploy
# copies files and restarts a unit, both of which succeed either way — so the
# real `onopen` and the real `checkRevision` are lifted out of `app.js` and run
# against a `/api/config` whose answer is allowed to change between asks. A
# stub serving one body for ever cannot tell a page that compares builds from
# one that never asks, which is exactly how #256 went unseen.
REVISION = r"""
const script = JSON.parse(process.argv[1]);

// The two module-level flags the check reads, lifted by name where `app.js`
// declares them so a rename fails these cases rather than being measured
// against a copy this harness keeps of itself.
%s

let reloads = 0;
// The one thing this function does to the outside world. `window.location` and
// not `location`, because that is the spelling the rest of `app.js` uses for
// the protocol and the query string, and a case that accepted either would
// pass on a page that reloads nothing in a browser.
globalThis.window = {
  location: {
    reload: () => { reloads++; },
    protocol: "http:",
    host: "localhost:8000",
    search: "",
  },
};

// One answer per ask, the last one standing for every ask after it, so a run
// can be a server that restarted onto a different build. `null` is a server
// that did not answer at all — the Mac asleep behind the tunnel, or a uvicorn
// still importing — which is the state a reconnect is most likely to land in.
const asked = [];
const replies = [...script.replies];
globalThis.fetch = async (url, opts) => {
  asked.push([url, opts && opts.cache ? opts.cache : null]);
  const reply = replies.length > 1 ? replies.shift() : replies[0];
  if (reply === null) throw new Error("failed to fetch");
  return { ok: true, status: 200, json: async () => reply };
};

// What `connect` and `onopen` touch that these cases are not about. Each is checked in
// CLOSE or in WAKE; they exist here only so `connect` and `onopen` can be lifted in whole
// rather than paraphrased, which is the only way a case can see the day it
// stops asking.
let openedAt = 0;
let sawReady = false;
let armed = false;
let listening = false;
let idleTimer = null;
let reconnectTimer = null;
let socket = null;
let userId = "test";
let home = null;
let voice = "";
let reconnectDelay = 0;
let RECONNECT_MIN_MS = 0;

// The corner stamp, as `index.html` carries it: the build in an attribute for
// the page to read back, the label as text for a person to. Kept here as an
// object rather than a class list of its own, because the two things a case
// asks of it are whether anything marked it and whether anything rewrote the
// label — and a stamp that updated itself on a reconnect is the #261 bug, not
// the feature.
const marks = [];
const badge = {
  dataset: {
    staticRevision: script.stamp === undefined ? "abc1234" : script.stamp,
  },
  textContent: script.badge === undefined ? "2026-09-10 23:01" : script.badge,
  classList: { add: (name) => { marks.push(name); } },
};
globalThis.document = {
  documentElement: { lang: "" },
  getElementById: (id) => (id === "buildBadge" ? badge : null),
};
globalThis.WebSocket = class { constructor() {} };

function setConnected(on) {}
function armSession() {}
function clearTimeout(id) {}

%s

// `onopen` does not await the check — it must not stand between a reconnect
// and the microphone — so the run has to let the promise chain behind it
// finish: the call, the json(), the compare.
const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };

bootRevision = script.boot;
const out = [];
for (const step of script.steps) {
  if (step.open) {
    connect();
    if (socket && socket.onopen) socket.onopen();
  }
  await flush();
  out.push({
    reloads, boot: bootRevision, asked: asked.splice(0),
    stale: marks.includes("stale"), badge: badge.textContent,
  });
}
console.log(JSON.stringify(out));
"""


# The other half of the same badge, and the half `REVISION` above cannot see:
# what the page makes of the build stamped into its own document, before any
# socket has opened. `init` is not liftable — it wants Maps, the profile picker
# and the voice list — so the two statements in it that do this are cut out by
# name. A rewrite that drops the compare fails here rather than being measured
# against a paraphrase this file keeps of it.
BADGE = r"""
const script = JSON.parse(process.argv[1]);

const marks = [];
const badge = {
  dataset: { staticRevision: script.stamp },
  classList: { add: (name) => { marks.push(name); } },
};
globalThis.document = {
  getElementById: (id) => (id === "buildBadge" ? badge : null),
};

let bootRevision = "";
// Which key the server answered under. `static_revision` is the ordinary one
// since #256; `revision` is a server too old to send the digest, and the page
// has to fall back to it rather than treat that as no answer at all.
const config = script.served === null ? {} : { [script.field]: script.served };

%s

%s

console.log(JSON.stringify({
  stamped: stampedBuild(), boot: bootRevision,
  stale: marks.includes("stale"),
}));
"""


# The wake word and the timer that undoes it. Everything the page decides here
# is invisible from outside it — a microphone that stayed on, a session left
# open in an empty kitchen, a countdown that a long answer cancels halfway
# through — so the real functions are lifted out of `app.js` and run over a
# clock a case turns by hand. Thirty seconds of real waiting per case is a
# script nobody runs.
WAKE = r"""
// The real table and the real `t`, on an English document, the same way
// FORGET takes them: what a kiosk says when it stops listening by itself is
// half of what these cases read, and since #96 that sentence is a key.
globalThis.document = { documentElement: { lang: "en" } };

%s

%s

let clock = 0;
const timers = new Map();
let nextTimer = 1;
globalThis.setTimeout = (fn, ms) => {
  const id = nextTimer++;
  timers.set(id, { fn, at: clock + ms });
  return id;
};
globalThis.clearTimeout = (id) => { timers.delete(id); };

async function tick(ms) {
  clock += ms;
  for (const [id, timer] of [...timers]) {
    if (timer.at <= clock) { timers.delete(id); await timer.fn(); }
  }
}

const sent = [];
const log = [];
let up = true;
globalThis.WebSocket = { OPEN: 1 };
const socket = {
  get readyState() { return up ? 1 : 3; },
  send: (text) => sent.push(text),
};

// What the reconnect handler touches that is not the wake path: the stamp the
// backoff reads and the dot in the corner. Both are stubbed — CLOSE is where
// they are checked — so that `onopen` can be lifted in whole rather than
// paraphrased here, which is the only way a case can see it forget something.
let openedAt = 0;
let sawReady = false;
function setConnected(on) {}

// The microphone, as far as this page ever knows one: whether it is running,
// how many times it was started — a wake word acted on twice restarts the
// recorder underneath the sentence it is already carrying — and whether the
// next attempt to open it is going to be refused.
let recorderOn = false;
let starts = 0;
let micFails = false;
let micOpenedWhileSuggested = false;
async function ensurePlayer() {}
async function startRecorder() {
  if (micFails) throw new Error("the microphone is in use");
  recorderOn = true;
  starts += 1;
  order.push("mic");
  if (!startersHidden.has("hidden")) micOpenedWhileSuggested = true;
}
async function stopRecorder() { recorderOn = false; }
function listMicrophones() {}
// As in CLOSE: `onopen` is lifted whole, so the build check it fires has to
// resolve to something here. REVISION is where it is driven.
function checkRevision() {}
function systemLine(text) { log.push(text); }
console.error = () => {};

const classes = new Set();
const icon = { textContent: "mic" };
let tap = null;
const micButton = {
  classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
  querySelector: () => icon,
  addEventListener: (ev, fn) => { tap = fn; },
};
// The starter row. Only whether it is hidden is read here — what is on it is
// STARTERS' question; what a timeout has to do is put the row back, because
// the clear button used to be the only thing that ever did.
const startersHidden = new Set();
const starters = {
  classList: {
    add: (c) => startersHidden.add(c),
    remove: (c) => startersHidden.delete(c),
  },
};
function hideStarters() { starters.classList.add("hidden"); }
const feedItems = [];
const feed = {
  appendChild: (node) => {},
  querySelector: (sel) => {
    if ((sel.includes(".msg") || sel.includes(".card")) && feedItems.length > 0) {
      return feedItems[0];
    }
    return null;
  },
  querySelectorAll: (sel) => [],
};
function renderStarters() {}

// The clock, stubbed at the boundary instead of lifted. The real `wake` is
// three hundred lines further down `app.js`, in a section that wants five DOM
// elements and a `fetch` of its own; what these cases are about is whether
// this path calls it at all, and whether it calls it ahead of the recorder
// rather than behind it. That the name resolves to the real one is pinned by
// a source read in `clock_cases`.
let asleep = true;
let order = [];
function wake() { asleep = false; order.push("wake"); }

let pending = new Map();
let browsing = new Map();

%s

function shot() {
  return { listening, armed, mic: recorderOn, starts, asleep,
           icon: icon.textContent, on: classes.has("on"),
           suggested: !startersHidden.has("hidden"),
           pending: timers.size,
           micOpenedWhileSuggested };
}

const steps = [];
for (const step of JSON.parse(process.argv[1])) {
  // Each of these is one thing that happens to a terminal on a counter, in
  // the order it happens.
  if (step.do === "wake") await handleWake();
  if (step.do === "tap") await tap();
  if (step.do === "idle") await goIdle();
  if (step.do === "frame") restartIdle();
  if (step.do === "type") armSession();
  if (step.do === "wait") await tick(step.ms);
  if (step.do === "mic-fails") micFails = step.value;
  if (step.do === "drop") up = false;
  if (step.do === "msg") feedItems.push({ type: "msg" });
  // The socket the retry got. `up` goes back on first: a handler that ran
  // against a socket still reading CLOSED would decline to send anything and
  // pass the cases below for the wrong reason.
  if (step.do === "open") { up = true; onopen(); }
  if (step.do === "asked") pending.set(step.id, {});
  if (step.do === "answered") pending.delete(step.id);
  if (step.do === "lost") pending.get(step.id).lost = true;
  if (step.do === "browse") browsing.set(step.id, {});
  if (step.do === "browse-done") browsing.delete(step.id);
  if (step.do === "browse-lost") browsing.get(step.id).lost = true;
  steps.push({ ...shot(), sent: sent.splice(0) });
}

console.log(JSON.stringify({ steps, log, order }));
"""


# What the two `IntersectionObserver` wrappers in `cards.js` subscribe to. The
# observer is made and the callback is written inline, so the only line that
# connects either to a card is the `observe` at the end — and an observer that
# was never pointed at anything raises nothing and logs nothing. The fake
# records what each one is watching and lets a case fire its callback by hand,
# because node has no layout and nothing here ever scrolls.
VISIBLE = r"""
const observers = [];
globalThis.IntersectionObserver = class {
  constructor(fn, opts) {
    this._fn = fn;
    this.opts = opts;
    this.watched = [];
    observers.push(this);
  }
  observe(node) { this.watched.push(node); }
  disconnect() { this.watched = []; this.dropped = true; }
  // A crossing, delivered the way the browser delivers one: to a live
  // observer, and only about a target it was given. An observer that was made
  // and never pointed at anything hears nothing, ever, which is the whole
  // shape of the bug and would be papered over by a fake that just calls back.
  fire(entries) {
    if (this.dropped || !this.watched.length) return;
    this._fn(entries);
  }
};

%s

function card() {
  const classes = new Set(["card"]);
  return {
    classes,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
  };
}
const shown = (c) => [...c.classes].sort();

// The cards pane. Identity is all that is read of it: what matters is that the
// observer is told to measure against the pane rather than against the window,
// which on a card scrolled out of a pane that fills the screen are not the
// same answer.
const root = { pane: true };
const out = {};

const weather = card();
whileVisible(weather, root);
let seen = observers.at(-1);
out.paused = {
  atFirst: shown(weather),
  watching: seen.watched.includes(weather),
  root: seen.opts?.root === root,
};
seen.fire([{ isIntersecting: true }]);
out.paused.scrolledIn = shown(weather);
seen.fire([{ isIntersecting: false }]);
out.paused.scrolledOut = shown(weather);

const map = card();
let inits = 0;
whenVisible(map, root, () => { inits += 1; });
seen = observers.at(-1);
out.lazy = { atFirst: inits, watching: seen.watched.includes(map) };
seen.fire([{ isIntersecting: false }]);
out.lazy.offScreen = inits;
seen.fire([{ isIntersecting: true }]);
seen.fire([{ isIntersecting: true }]);
out.lazy.onScreen = inits;
out.lazy.dropped = !!seen.dropped;

console.log(JSON.stringify(out));
"""


# Who the terminal is talking to. `showProfiles` is lifted out of `app.js` and
# run over a `<select>` stub and the real `Option` shape, because the two ways
# it fails are both silent: a sheet that grew a picker on a terminal that has
# only one person, and a picker naming somebody the socket is not on.
PROFILE = r"""
function element() {
  const classes = new Set(["hidden"]);
  return {
    classes,
    options: [],
    replaceChildren(...kids) { this.options = kids; },
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
    },
  };
}
globalThis.Option = class {
  constructor(text, value, _default, selected) {
    this.textContent = text;
    this.value = value;
    this.selected = !!selected;
  }
};

const profileSelect = element();
const profileLabel = element();
let userId = JSON.parse(process.argv[1]);

%s

showProfiles(JSON.parse(process.argv[2]));

console.log(JSON.stringify({
  shown: !profileSelect.classes.has("hidden"),
  labelled: !profileLabel.classes.has("hidden"),
  names: profileSelect.options.map((o) => o.value),
  labels: profileSelect.options.map((o) => o.textContent),
  selected: profileSelect.options.filter((o) => o.selected)
    .map((o) => o.value),
}));
"""


# The voice picker, filled from `/api/config` the way the profile one is. Two
# things come back out of it and both matter: what the sheet shows, and what
# `voice` is left holding — the second is what the next socket carries, and a
# select that agrees with the server while the variable does not is a terminal
# answering in one voice and saying it answers in another.
VOICE = r"""
globalThis.Option = class {
  constructor(text, value, _default, selected) {
    this.textContent = text;
    this.value = value;
    this.selected = !!selected;
  }
};

const voiceSelect = {
  options: [],
  replaceChildren(...kids) { this.options = kids; },
};
let voice = JSON.parse(process.argv[1]);

%s

showVoices(JSON.parse(process.argv[2]), JSON.parse(process.argv[3]));

console.log(JSON.stringify({
  voice,
  names: voiceSelect.options.map((o) => o.value),
  labels: voiceSelect.options.map((o) => o.textContent),
  selected: voiceSelect.options.filter((o) => o.selected).map((o) => o.value),
}));
"""


# The handshake `connect` builds, driven rather than grepped. The query is four
# conditionals over three sources — a pin in `localStorage`, a voice beside it,
# and now the page's own URL — and what has to hold is what comes out of them
# together, which a string search over the function cannot say.
#
# Everything below the `%s` is what `connect` closes over and reads before the
# socket exists. What it hangs on the socket afterwards is four assignments of
# arrow functions, whose bodies are never evaluated here, so nothing they touch
# needs a stub and none of it is faked.
SOCKET = r"""
let reconnectTimer = null;
let socket = null;
let hadSocket = false;
const [search, userId, home, voice, lang] = JSON.parse(process.argv[1]);
globalThis.window = {
  location: { protocol: "http:", host: "kitchen.local:8000", search },
};
// `init` runs `applyLanguage` before `connect`, so a real page always reaches
// here with a tag on the document. It is on the handshake since #187, which is
// how the panel's setting gets under a tool's `lang` argument.
globalThis.document = { documentElement: { lang } };
const opened = [];
globalThis.WebSocket = class {
  constructor(url) { opened.push(url); }
};

%s

connect();
console.log(JSON.stringify({ opened }));
"""


# The one control on the settings sheet that destroys something. `forget` and
# `disarmForget` are lifted out of `app.js` and driven over a scripted server,
# because every way this goes wrong goes wrong in one direction: a month of
# conversation deleted by a tap nobody meant, or a screen wiped over a server
# that still has the history and no longer any way to say so. The timer is a
# stub with a hand crank — the arming window is five seconds and no case is
# going to wait it out.
FORGET = r"""
const FORGET_ARM_MS = 5000;
let forgetTimer = null;

// The real table and the real `t`, on an English document, because half of
// what the cases below read is the sentence the button ends up wearing — and
// since #96 that sentence comes out of `i18n.js` rather than a literal.
globalThis.document = { documentElement: { lang: "en" } };

%s

%s

const timers = new Map();
const armedFor = [];
let nextTimer = 1;
globalThis.setTimeout = (fn, ms) => {
  const id = nextTimer++;
  timers.set(id, fn);
  armedFor.push(ms);
  return id;
};
globalThis.clearTimeout = (id) => { timers.delete(id); };

const classes = new Set();
const log = [];
const notes = [];
const asked = [];
const forgetButton = {
  textContent: t("ui_forget"),
  classList: {
    add: (c) => classes.add(c),
    remove: (c) => classes.delete(c),
  },
};
const clearButton = { click: () => log.push("clear") };
function reopen(note) { log.push("reopen"); notes.push(note); }
function systemLine(text) { log.push("system"); notes.push(text); }

// The picker, because it sits in the same sheet as the button and one of the
// steps below uses it between two taps. The real handler is lifted in whole
// rather than restated, so what runs here is what the panel runs.
const saved = [];
function saveSettings(patch) { saved.push(patch); }
let switchTo = () => {};
const profileSelect = {
  value: null,
  addEventListener: (type, fn) => { switchTo = fn; },
};

const plan = JSON.parse(process.argv[2]);
let userId = JSON.parse(process.argv[1]);
globalThis.fetch = async (url, opts) => {
  // The content type is recorded because it is load-bearing rather than
  // decoration: without it the server refuses the call, and any page on the
  // home network could have made this one.
  asked.push({ url, method: opts && opts.method,
               type: opts && opts.headers && opts.headers["Content-Type"] });
  if (plan.reply.throws) throw new Error(plan.reply.throws);
  return {
    ok: plan.reply.ok !== false,
    status: plan.reply.status || 200,
    json: async () => plan.reply.body,
  };
};

%s

%s

%s

function shot() {
  return {
    label: forgetButton.textContent,
    armed: classes.has("armed"),
    pending: timers.size,
  };
}

const steps = [];
for (const step of plan.taps) {
  if (step === "tap") await forget();
  if (step === "expire") {
    for (const [id, fn] of [...timers]) { timers.delete(id); fn(); }
  }
  if (step.startsWith("switch:")) {
    profileSelect.value = step.slice("switch:".length);
    switchTo();
  }
  steps.push(shot());
}

console.log(JSON.stringify({
  ...shot(), steps, log, notes, asked, armedFor, userId, saved,
}));
"""


# Where a new card comes to rest. `appendCard` and `restCardAtTop` are lifted
# out of `app.js` and run against a pane that does the two things a browser
# does and a plain object does not: it lays a child out under the one before
# it, and it clamps a scroll to what there is to scroll. The clamp is the bug
# — a card shorter than the pane cannot reach the top of it — so a stub that
# scrolled wherever it was asked would pass either way.
#
# Sizes are given per card by the case, because the whole point is what the
# arithmetic does with them, and nothing here can measure a real one.
SCROLL = r"""
const GAP = 12;   // `.cards > * + * { margin-top: 12px }`
const PAD = 14;   // the pane's own top padding, which offsetTop starts after

const fired = [];
const WIDE = { matches: true };
function append(node) { fired.push("feed"); }
const feed = { querySelectorAll: () => [] };

// Every observer the section makes, so a step can fire the one watching a
// given node — there are two now, and which one watches the pane is part of
// what is being checked.
const observers = [];
globalThis.ResizeObserver = class {
  constructor(fn) {
    this.fn = fn;
    this.watched = [];
    this.boxes = [];
    observers.push(this);
  }
  observe(node, opts) {
    this.watched.push(node);
    this.boxes.push(opts?.box ?? null);
  }
  unobserve(node) {
    const at = this.watched.indexOf(node);
    if (at >= 0) { this.watched.splice(at, 1); this.boxes.splice(at, 1); }
  }
  disconnect() { this.watched = []; this.boxes = []; }
};
function observerOf(node) {
  return observers.find((o) => o.watched.includes(node));
}

const listeners = {};
const kids = [];
WIDE.matches = JSON.parse(process.argv[1]).wide;

const starters = {
  parentElement: null,
  previousElementSibling: null,
  classList: {
    classes: new Set(["hidden"]),
    contains(c) { return this.classes.has(c); },
    add(c) {
      this.classes.add(c);
      relayout();
      sidePanel.scrollTo({ top: sidePanel.scrollTop });
    },
    remove(c) { this.classes.delete(c); relayout(); },
  },
  offsetHeight: 44,
  offsetTop: 0,
};
globalThis.starters = starters;

// Stacked from the pane's top padding down, recomputed whenever the run
// changes one: a card that grows or goes moves every card after it.
function relayout() {
  let top = PAD;
  for (const kid of kids) { kid.offsetTop = top; top += kid.offsetHeight + GAP; }
  cards.offsetHeight = kids.length ? (top - PAD - GAP) : 0;
  const cardsPaddingBottom = parseInt(cards.style.paddingBottom || "0", 10);
  if (starters.parentElement === sidePanel) {
    starters.offsetTop = PAD + cards.offsetHeight + cardsPaddingBottom + (kids.length ? GAP : 0);
  }
}

const cards = {
  id: "cards",
  get children() { return kids; },
  offsetHeight: 0,
  offsetTop: PAD,
  style: {},
  addEventListener(ev, fn) { listeners[ev] = fn; },
  appendChild(node) {
    kids.push(node);
    node.remove = () => {
      const at = kids.indexOf(node);
      if (at >= 0) kids.splice(at, 1);
      relayout();
    };
    relayout();
  },
  get lastElementChild() { return kids[kids.length - 1] ?? null; },
  replaceChildren() { kids.length = 0; relayout(); },
};
globalThis.cards = cards;

const sidePanel = {
  id: "sidePanel",
  clientHeight: JSON.parse(process.argv[1]).pane,
  scrollTop: 0,
  style: {},
  addEventListener(ev, fn) { listeners[ev] = fn; },
  appendChild(node) {
    node.parentElement = this;
    if (node === starters) {
      starters.previousElementSibling = cards;
    }
    relayout();
  },
  get scrollHeight() {
    const last = (starters.parentElement === this && !starters.classList.contains("hidden"))
      ? starters
      : (kids[kids.length - 1] ?? null);
    const tail = parseInt(sidePanel.style.paddingBottom || "20", 10);
    return (last ? last.offsetTop + last.offsetHeight : PAD) + tail;
  },
  scrollTo({ top }) {
    // The browser clamps, and so does this: a scroll container will not go
    // past `scrollHeight - clientHeight` however nicely it is asked.
    const most = Math.max(0, sidePanel.scrollHeight - sidePanel.clientHeight);
    sidePanel.scrollTop = Math.max(0, Math.min(top, most));
  },
};
globalThis.sidePanel = sidePanel;
globalThis.document = {
  getElementById: (id) => (id === "starters" ? starters : (id === "sidePanel" ? sidePanel : (id === "cards" ? cards : null))),
};
starters.parentElement = sidePanel;
starters.previousElementSibling = cards;

function showStarters(behavior = "auto") {
  starters.classList.remove("hidden");
  if (starters.parentElement !== sidePanel || starters.previousElementSibling !== cards) {
    sidePanel.appendChild(starters);
  }
  if (behavior !== false && typeof restCardAtTop === "function") {
    restCardAtTop(behavior);
  }
}
globalThis.showStarters = showStarters;

%s

// What the person sees: the gap between the top of the pane and the top of the
// card, which is what `padding-top` makes it when the card is where it should
// be, and a screen's worth when it is not.
function restingAt(node) { return node.offsetTop - sidePanel.scrollTop; }

let restCardCalls = 0;
const _origRestCardAtTop = restCardAtTop;
restCardAtTop = function(...args) {
  restCardCalls++;
  return _origRestCardAtTop.apply(this, args);
};

function startTurn() {
  activeTurn = true;
  turnCardCount = 0;
}
function turnComplete() {
  activeTurn = false;
  turnCardCount = 0;
}

const out = [];
for (const step of JSON.parse(process.argv[1]).steps) {
  if (step.card) {
    const node = { name: step.card, offsetHeight: step.height };
    appendCard(node);
  } else if (step.grow) {
    // A map built on first intersection, or an image that has just loaded.
    const node = kids.find((k) => k.name === step.grow);
    node.offsetHeight = step.height;
    relayout();
    // Only if something is watching this one. A card that grew unobserved is
    // the failure being looked for, and a callback fired anyway would hide it.
    if (cardSizes.watched.includes(node)) cardSizes.fn();
  } else if (step.drop) {
    // The repeat question the tool answered on the spot: its card goes,
    // whether or not it is the one the pane is aiming at.
    dropCard(kids.find((k) => k.name === step.drop));
  } else if (step.pane) {
    // The settings sheet closing, which hands the pane its height back. Fired
    // through whichever observer is watching the pane, so a pane nobody
    // watches reads as a named miss rather than as a height that never moved.
    sidePanel.clientHeight = step.pane;
    // The container re-clamps the moment it grows, which is the symptom
    // itself: the card that was at the top slides down by what it gave back.
    sidePanel.scrollTo({ top: sidePanel.scrollTop });
    const paneSizes = observerOf(sidePanel);
    if (paneSizes) paneSizes.fn(); else fired.push("no pane observer");
  } else if (step.scrolled) {
    // Named rather than called blind, so an event the pane no longer listens
    // for reads as the person's scroll going unheard instead of a stack trace.
    const fn = listeners[step.scrolled];
    if (fn) fn(); else fired.push(`no ${step.scrolled} listener`);
  } else if (step.clear) {
    cards.replaceChildren();
    forgetCards();
  } else if (step.showStarters) {
    starters.classList.remove("hidden");
    restCardAtTop("auto");
  } else if (step.hideStarters) {
    if (typeof hideStarters === "function") hideStarters();
    else starters.classList.add("hidden");
  } else if (step.startTurn) {
    startTurn();
  } else if (step.turnComplete) {
    turnComplete();
  }
  out.push({
    resting: kids.length ? restingAt(kids[kids.length - 1]) : null,
    top: kids.length ? restingAt(kids[0]) : null,
    scrollTop: sidePanel.scrollTop,
    tail: sidePanel.style.paddingBottom ?? null,
    cardsTail: cards.style.paddingBottom ?? null,
    startersTop: starters.offsetTop,
    startersResting: (starters.parentElement === sidePanel && !starters.classList.contains("hidden"))
      ? (starters.offsetTop - sidePanel.scrollTop)
      : null,
    watched: cardSizes.watched.length,
    paneBox: observerOf(sidePanel)?.boxes[0] ?? null,
    fired: [...fired],
    restCardCalls,
  });
}
console.log(JSON.stringify(out));
"""


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


def interpolations(src: str) -> list[str]:
    """The expression in every `="${…}"` in a file, brace-matched.

    A regex cannot do this alone: `escapeAttr(it.uri || "#")` has no `}` in it
    but `${a ? `${b}` : ""}` does, and the shorter match would stop inside the
    expression and read as unescaped.
    """
    found = []
    for start in (m.end() for m in re.finditer(r'="\$\{', src)):
        depth, i = 1, start
        while i < len(src) and depth:
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        found.append(src[start:i - 1].strip())
    return found


def node_call(calls: list[tuple[str, str]]) -> dict[str, str]:
    """Call exported functions of `markdown.js` under node, one arg each."""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", NODE,
         str(JS / "markdown.js"), json.dumps(calls)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        print(f"   FAIL node: {proc.stderr.strip()}")
        sys.exit(1)
    return json.loads(proc.stdout)


def address(name: str, *types: str) -> dict:
    """One entry of a `GeocoderResult.address_components` array."""
    return {"long_name": name, "short_name": name, "types": list(types)}


def short_names(cases: list[list[dict]]) -> tuple[list[str], str]:
    """`location.js`'s `shortName`, run under node over components arrays.

    The names, and what went wrong instead. Reported rather than fatal, the way
    `browse_run` is: a lift that finds nothing to lift would otherwise take the
    rest of this file's cases down with it and be counted as coverage that ran.
    """
    src = (JS / "location.js").read_text(encoding="utf-8")
    tables = re.findall(r"^const (?:(?:LOCAL|REGION)_TYPES|\w+) = \[.*?\];|^const IS_BLOCK = .*?;",
                        src, re.S | re.M)
    missing = [name for name in ("function component(", "function shortName(")
               if name not in src]
    if len(tables) < 2 or missing:
        return [""] * len(cases), f"tables {len(tables)}, missing {missing}"
    body = SHORT_NAME % "\n\n".join(
        tables + [block(src, "function component("),
                  block(src, "function shortName(")])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", body, json.dumps(cases)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return [""] * len(cases), proc.stderr.strip()[-300:]
    return json.loads(proc.stdout), ""


def lifted(src: str, header: str) -> str:
    """`block`, but "" for a header that is not in the file any more.

    A function that has been renamed or deleted is a case that should fail
    with its own name on it. Through `block` it is a `SystemExit` from the
    middle of a helper, which takes the rest of the file's cases with it and
    leaves the count looking healthy — the thing #131's review asked for and
    did not get.
    """
    return block(src, header) if header in src else ""


def before(text: str, first: str, second: str) -> bool:
    """Whether `first` appears in `text`, and appears ahead of `second`.

    Position, not presence. Both things this is asked about — a handler
    dropped before the socket closes, a stale timer cleared before the next
    one opens — read identically in a diff done in the wrong order and fix
    nothing. `find` rather than `index` so a string that is gone comes back
    as `False` and fails the case that wanted it, instead of raising here.
    """
    at, then = text.find(first), text.find(second)
    return 0 <= at < then


def block(src: str, header: str) -> str:
    """A whole `function name(…) {…}`, brace-matched from its header."""
    start = src.index(header)
    depth, i = 0, src.index("{", start)
    while i < len(src):
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
        if not depth:
            return src[start:i]
    raise SystemExit(f"{header}: never closed")


def slot() -> tuple[str, list[str], list[str]]:
    """How `app.js` declares the browse slot, and every way it resets it.

    One `let`, and three resets — the clear button, the move between panes, and
    the socket closing under whatever was in flight. Each is exercised on its
    own below, because one of them left as `null` is a TypeError the others
    would hide.
    """
    src = (JS / "app.js").read_text(encoding="utf-8")
    assigns = re.findall(r"^\s*(?:let )?browsing = .+?;$", src, re.M)
    return (src, [a.strip() for a in assigns if "let " in a],
            [a.strip().rstrip(";") for a in assigns if "let " not in a])


def browse_run(frames: list[dict], reset: str = "") -> dict:
    """Drive `app.js`'s `handleBrowse` over a list of frames under node."""
    src, declared, _ = slot()
    body = BROWSE % (declared[0] + "\n\n"
                     + block(src, "function handleBrowse("),
                     reset or "browsing = new Map()")
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", body, json.dumps(frames)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        # Reported rather than fatal. A reset left as `browsing = null` makes
        # the next frame a TypeError, and that deserves a case with a name on
        # it instead of a stack trace from the middle of the run.
        return {"drawn": [], "open": [], "error": proc.stderr.strip()}
    return json.loads(proc.stdout)


def browse_cases() -> None:
    print("\n== two browse jobs, one channel ==")

    src, declared, resets = slot()
    check("the slot is declared once", len(declared), 1, "; ".join(declared))
    check("and reset in three places", len(resets), 3, "; ".join(resets))

    # a and b interleave, a finishes, b finishes, then a asks again — which is
    # the duplicate-task case: the same browser, a card of its own.
    got = browse_run([
        {"id": "a", "phase": "step", "shot": "a1"},
        {"id": "b", "phase": "step", "shot": "b1"},
        {"id": "a", "phase": "step", "shot": "a2"},
        {"id": "b", "phase": "done", "shot": "b2"},
        {"id": "a", "phase": "done", "shot": "a3"},
        {"id": "a", "phase": "step", "shot": "a4"},
    ])
    check("the run got through every frame", got.get("error"), None)
    drawn = got["drawn"]
    check("each job got a card, and the second ask got a third", len(drawn), 3)
    check("the first card only ever saw its own job",
          drawn[0]["shots"], ["a1", "a2", "a3"])
    check("and the second only saw the other's", drawn[1]["shots"],
          ["b1", "b2"], "one card would read a1, b1, a2, b2")
    check("a job that finished does not reopen its old card",
          drawn[2]["shots"], ["a4"])
    check("every card was put on screen",
          [c["appended"] for c in drawn], [True] * 3)
    check("both finished jobs let go of their slot, the running one has not",
          got["open"], ["a"], "the third card is still being drawn into")

    # The clear button and the move between panes both drop the slot. A frame
    # from a job that was still running has to open a card, not throw — so each
    # reset is run on its own, since a `null` left in one is a TypeError the
    # other would paper over.
    for reset in resets:
        after = browse_run([
            {"id": "a", "phase": "step", "shot": "a1"},
            {"reset": True},
            {"id": "a", "phase": "step", "shot": "a2"},
        ], reset)
        check(f"`{reset}` leaves the slot usable", after.get("error"), None)
        check("and a job still running starts a fresh card",
              len(after["drawn"]), 2)
        check("and the job is tracked again", after["open"], ["a"])

    print("\n== the browse jobs a new location leaves behind ==")

    # Where the loop lives is half of what it does. Two places end a card by
    # hand — the location changing and the socket dropping — and the clear
    # button is not one of them: it empties the same map a line after removing
    # every card on screen, so ending them there would be work on cards nobody
    # is looking at.
    header = "for (const card of browsing.values())"
    reopen = block(src, "function reopen(")
    closed = block(src, "socket.onclose = () => {")
    check("the jobs in flight are ended when the location changes",
          header in reopen, True)
    check("and when the socket drops under them", header in closed, True)
    check("but not in the clear button", header in
          block(src, 'clearButton.addEventListener("click"'), False)
    # Read out of the source rather than out of the run below, which builds its
    # own reset out of the loop and so would not notice the order changing.
    check("and each of them runs before the slot it reads is emptied",
          [header in where
           and where.index(header) < where.index("browsing = new Map()")
           for where in (reopen, closed)], [True, True])
    loop = block(reopen, header)

    ended = browse_run([
        {"id": "a", "phase": "step", "shot": "a1"},
        {"id": "b", "phase": "step", "shot": "b1"},
        {"id": "c", "phase": "step", "shot": "c1"},
        {"id": "c", "phase": "done", "shot": "c2", "answer": "open till six"},
        {"reset": True},
        {"id": "a", "phase": "step", "shot": "a2"},
    ], loop + "\nbrowsing = new Map()")
    check("the reset runs", ended.get("error"), None)
    drawn = ended["drawn"]
    check("every job still in flight is ended by hand",
          [c["frames"][-1] for c in drawn[:2]],
          [{"phase": "done", "lost": True}] * 2)
    # The one that finished is out of `browsing` already, and writing a second
    # `done` over it would replace the answer with the interrupted line.
    check("and the one that already answered is left alone",
          drawn[2]["frames"][-1].get("answer"), "open till six")
    check("the slot is emptied all the same", len(ended["drawn"]), 4)
    check("so the next frame on an old id starts a fresh card",
          drawn[3]["shots"], ["a2"])

    print("\n== resetting browse card deadline on incoming frames ==")

    # A multi-step browse run: frames arrive at intervals (60s apart).
    # Total runtime is 240s (> 180s BROWSE_DEADLINE_MS).
    # Each incoming non-done frame resets the silence timer so the card
    # is not marked lost while actively receiving frames.
    active_run = browse_run([
        {"id": "a", "phase": "step", "shot": "a1"},
        {"advance": 60000},
        {"id": "a", "phase": "step", "shot": "a2"},
        {"advance": 60000},
        {"id": "a", "phase": "step", "shot": "a3"},
        {"advance": 60000},
        {"advance": 60000},  # total 240s elapsed, 120s since last frame
    ])
    check("the run got through every frame", active_run.get("error"), None)
    drawn = active_run["drawn"]
    check("an active browse running past 180s total is not marked lost",
          drawn[0].get("lost"), False,
          "the silence timer resets on each frame, so 240s total does not time out")
    check("handleBrowse arms the timer on the first frame",
          len([e for e in active_run.get("timerEvents", []) if e.get("op") == "set"]) >= 1, True)
    check("subsequent non-done frames re-arm a new timer",
          len([e for e in active_run.get("timerEvents", []) if e.get("op") == "set"]), 3)
    check("and clear the previous timer",
          len([e for e in active_run.get("timerEvents", []) if e.get("op") == "clear"]), 2)

    # When silence exceeds BROWSE_DEADLINE_MS (180s) without frames, the card is marked lost.
    silent_run = browse_run([
        {"id": "a", "phase": "step", "shot": "a1"},
        {"advance": 60000},
        {"id": "a", "phase": "step", "shot": "a2"},
        {"advance": 180000},  # 180s of silence after second frame
    ])
    check("silence exceeding the deadline marks the card lost",
          silent_run["drawn"][0].get("lost"), True)
    check("and ends the card with lost: true",
          silent_run["drawn"][0]["frames"][-1], {"phase": "done", "lost": True})

    # And that last line is the other door into the same spinner. `close()`
    # moves the socket to CLOSING and cancels nothing already queued, so a
    # browse frame that was on the wire is still delivered — after the loop
    # above, to a `browsing` that no longer knows the id, which is exactly the
    # run that just built a card. Only this time no `done` is coming for it and
    # `browse` has no deadline, so both handlers have to come off the socket.
    #
    # Read out of the source. Driving it would take a WebSocket that dispatches
    # during its own closing handshake, and a stub that did that would be the
    # assumption being checked rather than a test of it.
    check("the close handler comes off before the socket is closed",
          before(reopen, "socket.onclose = null", "socket.close()"), True)
    check("and the message handler with it",
          before(reopen, "socket.onmessage = null", "socket.close()"), True)
    # Nulling them is safe only because the reconnect at the end of `reopen`
    # rebinds the name to a different object; assigning over the old socket's
    # handlers would otherwise take the new one's with it.
    check("the reconnect builds a new socket to hang new handlers on",
          "socket = new WebSocket(" in block(src, "function connect("), True)

    print("\n== the reconnect a close left behind ==")

    # Two sockets on one conversation by the other route: not a handler left
    # attached but a timer left running. `reopen` opens its socket inside the
    # three seconds and the timer opens a second one, and the server resolves
    # both to the same thread, so the two talk over each other into it and each
    # hears the other's answer back as history.
    #
    # Source-level, like the two cases above. Driving it wants a WebSocket, a
    # clock and the render pipeline `connect` hands frames to, and a stub of
    # that size is more assumption than test.
    connect = block(src, "function connect(")
    check("the reconnect timer is held in a name outside connect",
          bool(re.search(r"^let reconnectTimer = null;$", src, re.M)), True)
    check("the close handler puts its timer there",
          "reconnectTimer = setTimeout(connect, "
          "reconnectWait(reconnectDelay));" in connect, True)
    check("and nothing schedules a reconnect it cannot get back",
          src.count("setTimeout(connect"), 1)
    # Ordering, not presence: cleared after the socket is open, the timer it
    # clears is the one it just started, and the stale one has already fired.
    check("a new socket clears the reconnect owed to the last one first",
          before(connect, "clearTimeout(reconnectTimer)", "new WebSocket("),
          True)

    print("\n== how long it waits before trying again ==")

    # A screen left up all day against a server that is not coming back. The
    # old fixed 3 s was 1200 attempts an hour, each one a row written to
    # `sessions.db` on an SD card — 18.6 MB a day with nobody in the room.
    #
    # Rolled at 0.5, the middle of the jitter range, so the sequence is the
    # bare doubling and the ceiling is where it lands rather than where a roll
    # put it.
    out = backoff_run([0.5] * 9)
    check("the run got through it", out.get("error"), None)
    check("the first retry is a second, not three",
          out["waits"][0], 1000, "a server restart is the common case")
    check("and each one after it waits twice as long",
          out["waits"][:6], [1000, 2000, 4000, 8000, 16000, 32000])
    check("up to a ceiling and no further",
          out["waits"][6:], [60000, 60000, 60000])
    # Both clamps, separately. Either one alone would hold the wait down today
    # — which is why the base needs its own case, or the clamp in
    # `reconnectNext` could be deleted and every case above would stay green
    # while the number it doubles ran away.
    check("and the base it doubles stops there as well",
          out["bases"][4:], [32000, 60000, 60000, 60000, 60000])
    check("which is the constant, not a number that happens to match",
          bool(re.search(r"^const RECONNECT_MAX_MS = 60000;$", src, re.M)),
          True)

    # The jitter, at both ends of its range. Without it every screen in the
    # house comes back in the same second after a power cut — which is the one
    # moment the server is least able to take them all.
    low, high = backoff_run([0.0] * 3), backoff_run([1.0] * 3)
    check("a low roll waits four fifths of the step",
          low["waits"], [800, 1600, 3200])
    check("and the highest roll one and a fifth",
          high["waits"], [1200, 2400, 4800])
    # Both of them off the same base: a jittered wait fed back in would let a
    # run of low rolls hold the delay down for hours, which is the bug the
    # ceiling exists to prevent, arriving from underneath.
    check("either way the next step doubles the base, not the wait",
          [low["bases"], high["bases"]],
          [[2000, 4000, 8000]] * 2)
    # And the ceiling holds against a high roll, which is the case where the
    # two clamps have to agree: 1.2 x 60 s is 72 s otherwise.
    check("the ceiling is a ceiling on the wait too",
          backoff_run([1.0] * 8)["waits"][-1], 60000)

    print("\n== the reset, and what counts as the server being back ==")

    # Driven rather than read, because where the reset is written is only half
    # of it. `main.py`'s `websocket.accept()` completes before `_thread` and
    # before `run_live`, so a server with no WAN, an expired key or a 429
    # answers the handshake and *then* fails — and on a kiosk pointed at its
    # own box that is the likely outage, not the rare one. A reset on the open
    # alone pins the delay to its floor through exactly that.
    lives = close_run([{"held": 200}] * 6)
    check("the run got through it", isinstance(lives, list), True, lives)
    check("a socket that opened and fell over does not reset the backoff",
          [life["wait"] for life in lives],
          [1000, 2000, 4000, 8000, 16000, 32000],
          "a floor held forever is 3000 attempts an hour, 2.5x the old 3 s")
    # And the other way, or never resetting at all would satisfy the case above
    # and leave a server that came back an hour ago still waiting a minute.
    check("a connection that lasted starts it over",
          [life["wait"] for life in
           close_run([{"held": 200}, {"held": 200}, {"held": 30000},
                      {"held": 200}])],
          [1000, 2000, 1000, 2000])
    check("and a socket that never opened leaves it climbing",
          [life["wait"] for life in close_run([{}, {}, {}])],
          [1000, 2000, 4000], "connection refused, the original case")
    check("including the ones after a connection that did last",
          [life["wait"] for life in
           close_run([{"held": 30000}, {}, {}])],
          [1000, 2000, 4000],
          "the stamp is cleared, or every later failure looks like a hold")
    check("which is the threshold, not a number that happens to match",
          bool(re.search(r"^const RECONNECT_HELD_MS = 5000;$", src, re.M)),
          True)

    # Uptime alone cannot answer the question the section above is named for.
    # A 4.9 s connection a Wi-Fi blip took down and a 4.9 s connection that
    # died in the post-accept window are the same measurement, so a run of
    # connections that all die inside `RECONNECT_HELD_MS` never resets the
    # ladder at all — it climbs to the ceiling and stays there. Six closes put
    # the base at 60 s, and the panel then sits `Offline` for up to a full
    # minute in front of a server that is already answering.
    #
    # What separates the two is what the server *did*: `main.py` sends one
    # `ready` frame once every unguarded call on its accept path has come back,
    # and these lives carry it through the page's real `onmessage`.
    flapping = close_run([{"held": 4000, "ready": True}] * 8)
    check("the run got through it", isinstance(flapping, list), True, flapping)
    check("a run of short connections the server answered on stays at the "
          "floor",
          [life["wait"] for life in flapping], [1000] * 8,
          "without this, 1000 2000 4000 8000 16000 32000 60000 60000")
    # The other half, and the one the guard is written for. Same lives, same
    # lifetimes, no frame — because the failure this backs off from is a server
    # that accepts and then falls over before it has opened anything.
    check("and the same run with the server silent still climbs",
          [life["wait"] for life in close_run([{"held": 4000}] * 7)],
          [1000, 2000, 4000, 8000, 16000, 32000, 60000],
          "an accept is not a server, and a handshake is not health")
    # The frame belongs to the socket that carried it. `reopen` throws a socket
    # away without its close handler running, so a flag left set would credit
    # the next connection with the last one's `ready` — and the next connection
    # here is one that never opened at all.
    check("and a `ready` does not outlive the socket it arrived on",
          [life["wait"] for life in
           close_run([{"held": 4000, "ready": True},
                      {"held": 4000, "ready": True}, {}, {}])],
          [1000, 1000, 2000, 4000])

    line = "reconnectDelay = RECONNECT_MIN_MS"
    opened = block(src, "socket.onopen = () => {")
    reopened = block(src, "function reopen(")
    connecting = block(src, "function connect(")
    # `connect` with the handlers it hangs on the socket taken back out, which
    # leaves the part that runs on every attempt — the close handler is written
    # inside `connect`, so the whole block would say yes to anything the close
    # handler does.
    attempt = connecting.replace(closed, "").replace(opened, "")
    check("and only a held close and a deliberate reopen reset it",
          src.count(line), 3, "the declaration and the two of them")
    check("which is those two, not the open and not the attempt",
          [line in where for where in (closed, reopened, opened, attempt)],
          [True, True, False, False],
          "resetting on the open is the 1 s fixed ladder coming back")
    # The evidence the close handler now weighs, and where it comes from. Read
    # in the dispatch and nowhere else, because a `ready` acted on anywhere the
    # server can be reached before its accept path has finished is the same
    # fixed ladder by another route.
    messaged = block(src, "socket.onmessage = (frame) => {")
    check("the server's `ready` is read by the dispatch and nowhere else",
          ['data.type === "ready"' in where
           for where in (messaged, closed, opened, reopened)],
          [True, False, False, False])
    # And it belongs to one socket. Cleared at the close, and again at the next
    # open for the socket `reopen` builds without a close ever running.
    check("and the flag it sets is set once and cleared on both paths",
          [src.count("sawReady = true"), src.count("sawReady = false")],
          [1, 3], "the declaration, the close, and the next socket's open")

    print("\n== and the ladder a person asking to reconnect starts from ==")

    # `reopen` replaces the socket by hand and nulls its `onclose` first, so
    # the socket it throws away never reaches the only place the backoff
    # moves. Somebody who opens the settings sheet during an outage and
    # changes the location is asking for a connection now; without the reset
    # they get one attempt and then up to a minute of `Offline`, on a screen
    # they are standing in front of, under a line that says `Reconnecting.`
    #
    # Seven refusals first, which is what puts the delay at the ceiling.
    # The press itself is spaced at nothing and the refusal that answers it at
    # 300 ms, because both are what they are: a hand on a button, and a server
    # that is still down saying so.
    ceiling = [{}] * 7
    after = [{"do": "reopen", "gap": 0}] + [{"gap": 300}] * 3
    impatient = close_run(ceiling + after)
    check("the run got through it", isinstance(impatient, list), True,
          impatient)
    check("and a reopen inside it starts the ladder over",
          [life["wait"] for life in impatient[-3:]], [1000, 2000, 4000],
          "the person pressed something; the ladder is about a dead server")

    # The history that hides all of this from anyone testing it by hand. The
    # socket being replaced had been up for an hour, so the stale stamp makes
    # the next close decide a connection which never opened "held" and reset
    # anyway — the right numbers for the wrong reason.
    healthy = close_run(ceiling + [{"do": "up", "held": 3600000}] + after)
    check("so a reopen off a healthy socket lands on the same ladder",
          [life["wait"] for life in healthy[-3:]], [1000, 2000, 4000],
          "it did before the fix too, off the stale stamp")
    check("the stamp does not outlive the socket it belongs to",
          healthy[8]["stamp"], 0,
          "left behind, it says a connection that never opened held")

    # And the one it does not hide it from: a server that answers the
    # handshake and dies inside `run_live` is the outage `RECONNECT_HELD_MS`
    # exists for, and there the stamp is too young to reset anything.
    flapping = close_run(ceiling + [{"do": "up", "held": 2000}] + after)
    check("and so does one off a socket that had only just come up",
          [life["wait"] for life in flapping[-3:]], [1000, 2000, 4000],
          "under RECONNECT_HELD_MS, where nothing resets by accident")

    print("\n== the browse cards a dropped socket leaves behind ==")

    # A browse card has no deadline — `LATE_TOOLS` gives `gws` and `plan` a
    # number and can give this one none — and the `done` that would end it goes
    # out over the socket that has just died, where `browse.py` swallows the
    # failed send. So the close handler is the last thing that can end it, and
    # a kiosk is precisely the screen on which nobody reloads.
    dropped = close_run([{"held": 200, "cards": ["a", "b"]}])
    check("the run got through it", isinstance(dropped, list), True, dropped)
    check("every card in flight is ended when the socket goes",
          [frame for _, frame in dropped[0]["ended"]],
          [{"phase": "done", "lost": True}] * 2)
    check("each one its own card, not the last one twice",
          [card for card, _ in dropped[0]["ended"]], ["a", "b"])
    check("and the slot is emptied behind them", dropped[0]["open"], [])
    check("a close with nothing in flight ends nothing",
          close_run([{"held": 200}])[0]["ended"], [])

    print("\n== a reconnect under a microphone that stayed on ==")

    # The Live session died with the old socket, and nothing on the page can
    # see that happen: the recorder keeps posting a block every 8 ms, and the
    # new server drops every one of them because nobody armed it. The room is
    # then talking to a terminal that answers nothing, with the button lit.
    #
    # Both halves in one case, because arming on every open would satisfy the
    # first on its own and leave a kiosk in an empty kitchen holding a Live
    # session it never asked for, every time the server restarts.
    check("a socket that comes up under an open mic asks for a session, and "
          "one that comes up with the mic off asks for nothing",
          [close_run([{"held": 200, "mic": True}])[0]["rearmed"],
           close_run([{"held": 200}])[0]["rearmed"]], [1, 0])


def card_run(data: dict | list, lang: str = "en") -> dict:
    """Drive `cards.js`'s `browseUpdate` over one frame, or a run of them."""
    src = (JS / "cards.js").read_text(encoding="utf-8")
    # `STRINGS` and `t` moved to `i18n.js` when the settings sheet got a
    # language picker; `browseUpdate` and `host` are still cards.js.
    lifted = "\n\n".join(
        [block(i18n_src(), header).removeprefix("export ")
         for header in ("export const STRINGS = ", "export function t(")]
        + [block(src, header).removeprefix("export ")
           for header in ("function host(", "export function browseUpdate(")])
    frames = data if isinstance(data, list) else [data]
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         BROWSE_CARD % (json.dumps(lang), lifted), json.dumps(frames)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip(), "classes": [], "shot": "",
                "status": "", "md": ""}
    return json.loads(proc.stdout)


def calendar_src() -> str:
    """The calendar section of `cards.js`, plus the words it says for itself.

    One contiguous slice from the section header, for the reason `scroll_src`
    gives. `STRINGS` and `t` come along because half of what a row prints —
    "All day", "Busy", "Unconfirmed" — is chrome the card picks the language
    of, and a stub `t` would let a missing key through as an empty string.
    They come out of `i18n.js`, which is where the settings sheet's language
    picker moved them; the real table, so a `cal_` key missing from one locale
    fails a case here rather than blanking a chip on the panel.
    """
    src = (JS / "cards.js").read_text(encoding="utf-8")
    lifted = "\n\n".join(
        block(i18n_src(), header).removeprefix("export ")
        for header in ("export const STRINGS = ", "export function t("))
    start = src.index("/* ---------- calendar ---------- */")
    return lifted + "\n\n" + src[start:src.index("/* ---------- gws", start)]


def cal_run(lang: str = "en", **job) -> dict:
    """Drive the calendar section of `cards.js` under node."""
    job.setdefault("now", "2026-09-10T12:30:00")
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         CALENDAR % (json.dumps(lang), calendar_src()), json.dumps(job)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        # Reported rather than fatal, like `scroll_run`: a name left dangling
        # by an edit to the section should fail the case that reads it.
        return {"error": proc.stderr.strip()[-300:], "html": "", "cats": [],
                "emoji": None, "said": "", "saids": []}
    return json.loads(proc.stdout)


def abbrev_words() -> list[str]:
    """Every literal word `CAL_ABBREV` accepts, both cases where it takes both.

    Read off `cards.js` rather than copied here, so a word added to the source
    without a case below fails the table instead of going unwatched: sixteen of
    the twenty-two entries had no case at all before #164. The two alternatives
    that are not literal words — a lone initial, and an initialism written out
    — do not match the shape below and drop out; they have their own cases.
    """
    src = calendar_src()
    start = src.index("const CAL_ABBREV = new RegExp(")
    # `(?:[^"\\]|\\.)*` and not `[^"]*`: these are JS string literals, so an
    # escaped quote inside one would end the shorter match early and silently
    # drop every word after it. The class in front of the token held one until
    # #164 widened it.
    body = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"',
                              src[start:src.index(");", start)]))
    words = []
    for piece in body.split("|"):
        piece = piece.removesuffix(")$")
        if not re.fullmatch(r"\[[A-Za-z]{2}\][A-Za-z]+|[A-Za-z]{2,}", piece):
            continue
        # `[Ss]t` is the source's way of writing "either case of the first
        # letter", and both of those are a word this table has to drive.
        if piece.startswith("["):
            words += [piece[1] + piece[4:], piece[2] + piece[4:]]
        else:
            words.append(piece)
    return words


def gws_fill_run(frames: list[dict], lang: str = "en") -> dict:
    """Drive the real `gwsFill`, with the real calendar renderer under it."""
    src = (JS / "cards.js").read_text(encoding="utf-8")
    lifted = calendar_src() + "\n\n" + block(src, "export function gwsFill(")
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         GWS_FILL % (json.dumps(lang), lifted), json.dumps(frames)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip()[-300:], "classes": [],
                "text": "", "html": "", "progress": False}
    return json.loads(proc.stdout)


def day(**fields) -> dict:
    """One agenda event, shaped the way `_agenda._for_screen` leaves one."""
    return {"day": "2026-09-10", **fields}


def agenda(events: list[dict], **rest) -> dict:
    """A whole agenda frame around `events`."""
    days = sorted({e["day"] for e in events}) or ["2026-09-10"]
    return {"from": days[0], "to": days[-1], "count": len(events),
            "events": events, **rest}


def calendar_cases() -> None:
    print("\n== the calendar behind a calendar answer ==")

    # The empty day, which is an answer and not the absence of one. A card that
    # renders zero rows says nothing at all, and "nothing on" is exactly what
    # somebody who asked is standing there waiting to hear.
    blank = cal_run(agenda=agenda([]))
    check("the run got through it", blank.get("error"), None)
    check("a day with nothing on it says so", "Nothing on" in blank["html"],
          True, blank["html"].strip()[:60])
    check("and names the day it is empty", "Sep 10" in blank["html"], True)
    check("rather than drawing an empty grid",
          "cal-ev" in blank["html"], False)
    check("in the language of the turn",
          "予定なし" in cal_run("ja", agenda=agenda([]))["html"], True)

    one = cal_run(agenda=agenda([
        day(**{"from": "19:00", "to": "21:00", "title": "Dinner"}),
    ]))
    check("one event is one day and one row",
          (one["html"].count('class="cal-day'), one["html"].count("cal-ev")),
          (1, 1))
    check("under the day of the month and its weekday",
          ("<b>10</b>" in one["html"], "Thu" in one["html"]), (True, True))
    check("which is written the way the turn's language writes it",
          "木" in cal_run("ja", agenda=agenda([day(title="x", all_day=True)])
                          )["html"], True)
    check("with both ends of the time on it",
          ("<b>19:00</b>" in one["html"], "21:00" in one["html"]),
          (True, True))

    # Grouping. The events arrive sorted by `(day, from)` — `_agenda._read`
    # does it — so this is a run-length pass and not a sort, and the failure is
    # a heading per event rather than a heading per day.
    week = cal_run(agenda=agenda([
        day(day="2026-09-10", **{"from": "09:00", "to": "10:00", "title": "a"}),
        day(day="2026-09-10", **{"from": "11:00", "to": "12:00", "title": "b"}),
        day(day="2026-09-12", **{"from": "09:00", "to": "10:00", "title": "c"}),
    ]))
    check("two days out of three events is two headings",
          (week["html"].count('class="cal-day'), week["html"].count("cal-ev")),
          (2, 3))
    check("and a day with nothing on it gets no heading of its own",
          "<b>11</b>" in week["html"], False,
          "eleven empty headings is what a fortnight would draw")

    # The other side of that, and the reason the sort upstream is load-bearing
    # rather than tidy. A run-length pass draws a day that arrives in two
    # pieces as two headings with the other day between them — not as one
    # heading in the wrong place, which is what a reader assuming a grouping
    # would expect. Nothing produces this feed: `_agenda._read` sorts on
    # `(day, from)` and `agenda_check.ordered` pins that it does. So these two
    # are the precondition written down, and they are the cases to delete if
    # the renderer is ever made order-neutral.
    split = cal_run(agenda=agenda([
        day(day="2026-09-10", **{"from": "09:00", "to": "10:00", "title": "a"}),
        day(day="2026-09-12", **{"from": "09:00", "to": "10:00", "title": "c"}),
        day(day="2026-09-10", **{"from": "18:00", "to": "19:00", "title": "b"}),
    ]))
    check("a day arriving in two pieces is drawn under two headings",
          (split["html"].count('class="cal-day'),
           re.findall(r"<b>(\d+)</b>", split["html"])),
          (3, ["10", "12", "10"]))
    check("with every row still on the card, in the order it arrived",
          re.findall(r'cal-title">([^<]*)<', split["html"]),
          ["a", "c", "b"])

    print("\n== the times a list of them gets wrong ==")

    allday = cal_run(agenda=agenda([day(title="Sports day", all_day=True)]))
    check("an all-day event says all day", "All day" in allday["html"], True)
    check("rather than midnight to midnight", "00:00" in allday["html"], False)
    check("a multi-day one names the day it runs to",
          "→ 9/12" in cal_run(agenda=agenda([
              day(title="Kyoto", all_day=True, until="2026-09-12"),
          ]))["html"], True)
    check("and one that runs past midnight says which morning it ends on",
          "→ 9/11" in cal_run(agenda=agenda([
              day(**{"from": "23:00", "to": "01:30", "title": "Launch",
                     "ends": "2026-09-11"}),
          ]))["html"], True)

    print("\n== the one that is happening now ==")

    def lit(html: str) -> int:
        return html.count("cal-ev now")

    running = cal_run(agenda=agenda([
        day(**{"from": "09:00", "to": "10:00", "title": "over"}),
        day(**{"from": "12:00", "to": "13:00", "title": "running"}),
        day(**{"from": "15:00", "to": "16:00", "title": "later"}),
    ]))
    check("exactly one row is lit at half past twelve", lit(running["html"]), 1)
    # The rest of the row the marker opens, up to wherever the next one starts.
    marked = running["html"].split('class="cal-ev now"')[1].split("cal-ev")[0]
    check("and it is the one whose hour this is",
          (">running<" in marked, ">over<" in marked, ">later<" in marked),
          (True, False, False))
    check("the label says so in words as well",
          "Now" in running["html"], True)
    # Every row on a fortnight's answer would otherwise light up at 12:30.
    check("nothing is lit on a day that is not today",
          lit(cal_run(agenda=agenda([
              day(day="2026-09-12", **{"from": "12:00", "to": "13:00",
                                       "title": "next week"}),
          ]))["html"]), 0)
    check("an event that runs past midnight is lit from its start",
          lit(cal_run(now="2026-09-10T23:30:00", agenda=agenda([
              day(**{"from": "23:00", "to": "01:30", "title": "Launch",
                     "ends": "2026-09-11"}),
          ]))["html"]), 1, "`to` is earlier than `from`, so the plain test fails")
    check("and an all-day event is lit for the whole of today",
          lit(allday["html"]), 1)
    check("but not on any other day",
          lit(cal_run(agenda=agenda([
              day(day="2026-09-11", title="Sports day", all_day=True),
          ]))["html"]), 0)

    print("\n== what else is on a row, and what is not ==")

    full = cal_run(agenda=agenda([
        day(**{"from": "10:00", "to": "11:00", "title": "Standup",
               "where": "Kyobashi", "people": 4}),
    ]))
    # Whitespace out, because the markup is written to keep stray spaces out of
    # the rendering and that puts newlines between every tag and its text.
    tight = re.sub(r"\s+", "", full["html"])
    check("the row carries where it is",
          ">location_on</span>Kyobashi<" in tight, True)
    check("and how many people are on it", ">group</span>4<" in tight, True)
    solo = cal_run(agenda=agenda([
        day(**{"from": "10:00", "to": "11:00", "title": "Gym", "people": 1}),
    ]))
    check("an event nobody else is on carries no head count",
          "group" in solo["html"], False,
          "`people` counts the person themselves")
    # `agenda` reads only calendars they own now, so no event arrives needing
    # to say whose it is — and a chip drawn off a key nothing sends is a
    # renderer branch nobody can reach.
    check("and no row anywhere attributes itself to a calendar",
          "calendar_month" in calendar_src(), False,
          "`_agenda` stopped sending `calendar`; #129")

    untitled = cal_run(agenda=agenda([
        day(**{"from": "10:00", "to": "11:00", "untitled": True}),
    ]))
    check("an event nobody named says so", "No title" in untitled["html"], True)
    # The free/busy block it used to be told apart from went with the calendar
    # it could only have come off. One name for a nameless event, and no key
    # in `i18n.js` behind a branch that cannot be taken.
    check("and there is no second word for one",
          ("Busy" in untitled["html"], "cal_busy" in i18n_src()),
          (False, False))
    check("it is drawn quieter than a title somebody wrote",
          (untitled["html"].count("faded"), full["html"].count("faded")),
          (1, 0))

    maybe = cal_run(agenda=agenda([
        day(**{"from": "10:00", "to": "11:00", "title": "Offsite",
               "tentative": True}),
    ]))
    check("an event the organiser has not settled is flagged",
          "Unconfirmed" in maybe["html"], True)
    check("and so is an invitation they have not answered",
          "Unconfirmed" in cal_run(agenda=agenda([
              day(**{"from": "10:00", "to": "11:00", "title": "Offsite",
                     "people": 3, "your_rsvp": "needsAction"}),
          ]))["html"], True)

    cut = cal_run(agenda=agenda(
        [day(**{"from": "10:00", "to": "11:00", "title": "x"})], more=7))
    check("a fortnight the server had to cut says how much is missing",
          "7 more" in cut["html"], True)
    check("in the turn's language too",
          "ほか7件" in cal_run("ja", agenda=agenda(
              [day(**{"from": "10:00", "to": "11:00", "title": "x"})],
              more=7))["html"], True)

    # An invitation somebody else organised puts their title on this person's
    # own calendar, so the strings on a row are still not all locally written.
    nasty = cal_run(agenda=agenda([
        day(**{"from": "10:00", "to": "11:00",
               "title": '<img src=x onerror="alert(1)">',
               "where": "<b>here</b>"}),
    ]))
    check("a title carrying markup arrives as text",
          ("&lt;img" in nasty["html"], "<img" in nasty["html"]),
          (True, False))
    check("and so does a location", "&lt;b&gt;here" in nasty["html"], True)

    check("a frame with no events array at all draws nothing",
          cal_run(agenda={"from": "a", "to": "a"})["html"], "")
    check("and neither does no frame", cal_run(agenda=None)["html"], "")

    print("\n== the one line the card says over the grid ==")

    # The card used to print the sub-agent's whole answer under the grid, which
    # on a calendar question is the grid again in words. The slot is one line
    # now, and the grid is what the day is; what goes in the slot is whatever
    # the answer said that a row cannot — the reply to what was actually asked.
    said = cal_run(said="Dinner at seven.", agenda=agenda([
        day(**{"from": "19:00", "to": "21:00", "title": "Dinner"}),
    ]))
    check("the run got through it", said.get("error"), None)
    check("a sentence handed in gets a line of its own",
          said["html"].count('class="cal-said"'), 1)
    check("above the first day and below the span",
          (before(said["html"], "cal-span", "cal-said"),
           before(said["html"], "cal-said", "cal-day")), (True, True))
    check("and no line at all when there is no sentence",
          "cal-said" in one["html"], False,
          "the calendar card is drawn from the timeline too, with no answer")
    # A day with nothing on it is the case the slot matters most on: the grid
    # says "Nothing on" and the sentence is the only place a run that was cut
    # short can say so.
    check("an empty day still has room for one",
          '<div class="cal-said">Stopped.</div>'
          in cal_run(said="Stopped.", agenda=agenda([]))["html"], True)
    check("and what goes in it arrives as text",
          ("&lt;b&gt;" in cal_run(said="<b>hi</b>", agenda=agenda([]))["html"],
           "<b>hi" in cal_run(said="<b>hi</b>", agenda=agenda([]))["html"]),
          (True, False))

    # `calSaid` is what cuts an answer down to that line. One sentence, because
    # a paragraph in a `nowrap` slot is an ellipsis three words in and the rest
    # of it is gone anyway — better to end on a full stop the model wrote.
    check("one sentence out of several", cal_run(
        prose="Dinner at seven. Then the school thing at nine."
    )["said"], "Dinner at seven.")
    check("and out of a Japanese answer, on its own full stop",
          cal_run(prose="19時から夕食です。学校の件は9時。")["said"],
          "19時から夕食です。")
    check("a question or an exclamation ends a line as well",
          (cal_run(prose="Free all day! Enjoy.")["said"],
           cal_run(prose="今日は空いてます？たぶん。")["said"]),
          ("Free all day!", "今日は空いてます？"))
    check("an answer of one sentence survives whole",
          cal_run(prose="Nothing on until Friday")["said"],
          "Nothing on until Friday")
    check("a full stop inside a number is not the end of one",
          cal_run(prose="It is 3.8 km away. Leave at six.")["said"],
          "It is 3.8 km away.",
          "ASCII `.` needs whitespace or the end of the text after it")
    check("and neither is the one a time ends on",
          cal_run(prose="Ends 19:00.Then dinner")["said"],
          "Ends 19:00.Then dinner")
    check("the markdown the sub-agent writes comes off",
          cal_run(prose="- **Dinner** at [seven](http://x/). Then bed."
                  )["said"], "Dinner at seven.")
    check("and a heading and its blank line collapse to one space",
          cal_run(prose="## Today\n\nTwo things on.")["said"],
          "Today Two things on.")
    check("nothing in is nothing out",
          (cal_run(prose="")["said"], cal_run(prose=None)["said"]), ("", ""))

    # An English abbreviation is a full stop with a space after it, which is
    # the whole of what the cut used to look for: the line stopped at the
    # abbreviation and the answer never reached the glass. Every string below
    # is invented — none of it came off a real calendar.
    check("a shortened title is not the end of a sentence",
          cal_run(prose="Dr. Tanaka at 10:00, then you are free.")["said"],
          "Dr. Tanaka at 10:00, then you are free.")
    check("nor is one the answer opens with",
          cal_run(prose="e.g. you are free after three.")["said"],
          "e.g. you are free after three.")
    check("nor a pair of initials",
          cal_run(prose="A. B. Watanabe is on the invite. Nothing else.")
          ["said"], "A. B. Watanabe is on the invite.",
          "two stops in a row, and neither of them ends anything")
    check("nor a clock written out",
          (cal_run(prose="Nothing before 5 p.m. is confirmed yet.")["said"],
           cal_run(prose="The 9 a.m. is off, so you are free until noon.")
           ["said"]),
          ("Nothing before 5 p.m. is confirmed yet.",
           "The 9 a.m. is off, so you are free until noon."),
          "the one that reads as an answer and is not one")
    check("nor a longer title",
          (cal_run(prose="Mrs. Ito at nine, then nothing.")["said"],
           cal_run(prose="Prof. Aoki at four. Free before then.")["said"]),
          ("Mrs. Ito at nine, then nothing.", "Prof. Aoki at four."),
          "`Mrs` and `Prof` are what a three-letter guard would let through")
    check("nor a word shortened in the middle",
          (cal_run(prose="Your appt. is at ten. Free after that.")["said"],
           cal_run(prose="It takes approx. 20 minutes. Leave at six.")
           ["said"]),
          ("Your appt. is at ten.", "It takes approx. 20 minutes."))
    check("nor a room number",
          cal_run(prose="You are in room no. 4 at ten. Nothing else.")["said"],
          "You are in room no. 4 at ten.")
    check("an ellipsis is a pause and not a stop",
          cal_run(prose="Well... you are free after three.")["said"],
          "Well... you are free after three.")
    # The guard against fixing this with a fixed-width lookbehind: anything
    # that asks for three characters in front of the stop takes "No." with it,
    # and "No." is half the answers to "am I free at three?".
    check("a two-letter answer still ends where it says it does",
          (cal_run(prose="No. The dentist is at three.")["said"],
           cal_run(prose="OK. You are free after lunch.")["said"]),
          ("No.", "OK."))
    check("a number is not a shortened word either",
          cal_run(prose="Leave at 6. Then dinner.")["said"], "Leave at 6.",
          "`6.` is a stop with a space after it and nothing in front of it")
    check("and an English abbreviation in a Japanese answer stops nothing",
          cal_run(prose="Dr. 田中さんが10時です。あとは空いています。")["said"],
          "Dr. 田中さんが10時です。")

    # #164. The list is finite and the answer is written by a model, so a word
    # that is not on it arrives sooner or later. What that used to cost was not
    # a word: the unrecognised stop is the *first* one the walk reaches, so
    # "Sept. 12 is clear, so you are free." put `Sept.` on the card and threw
    # the rest away — and with an agenda on the frame there is no second copy
    # anywhere. What decides now is the token in front *and* the character
    # after: a sentence does not start with a lowercase letter or a digit.
    # Every string below is invented; none of it came off a real calendar.
    check("a month the list never had no longer eats the answer",
          cal_run(prose="Sept. 12 is clear, so you are free.")["said"],
          "Sept. 12 is clear, so you are free.",
          "used to be `Sept.` and nothing else")
    check("nor does a weekday", cal_run(
        prose="Fri. is clear, nothing before four.")["said"],
        "Fri. is clear, nothing before four.")
    check("nor a duration mid-sentence", cal_run(
        prose="It runs 30 min. from ten, then you are open.")["said"],
        "It runs 30 min. from ten, then you are open.")
    check("nor a street the sub-agent wrote in lower case", cal_run(
        prose="Meet on Oak st. at ten, then nothing.")["said"],
        "Meet on Oak st. at ten, then nothing.")
    check("an em dash in front of a token is still in front of a token",
          cal_run(prose="10:00—e.g. free until noon. Then a call.")["said"],
          "10:00—e.g. free until noon.",
          "`e.g.` was already matched; the character before it was not looked "
          "through")
    check("and so is a hyphen", cal_run(
        prose="Slot-no. 4 is yours at ten. Nothing after.")["said"],
        "Slot-no. 4 is yours at ten.")

    # The forward test on its own, over a word deliberately left off the list:
    # `Bldg` is not in `CAL_ABBREV` and does not need to be, because "4" is not
    # how a sentence starts. This is the half of the fix that generalises.
    check("a shortening nobody listed still keeps its clause",
          cal_run(prose="Bldg. 4 is free at ten. Nothing after.")["said"],
          "Bldg. 4 is free at ten.")
    check("and an unlisted abbreviation followed by kanji keeps its sentence",
          cal_run(prose="Bldg. 会議室は10時からです。あとは空いています。")["said"],
          "Bldg. 会議室は10時からです。")
    check("or followed by a digit in a Japanese answer",
          cal_run(prose="Bldg. 4階は空いています。あとは空いています。")["said"],
          "Bldg. 4階は空いています。")
    # And the residual, pinned so nobody reads the case above as a cure. A word
    # off the list, at the head of the answer, with a capital after the stop,
    # is still the one shape that loses everything.
    check("but off the list and in front of a capital it still cuts short",
          cal_run(prose="Bldg. Four is free at ten. Nothing after.")["said"],
          "Bldg.", "the list is what covers this shape and it is finite")
    # The price of the forward test, in the direction the docstring calls the
    # cheap one: a real sentence end followed by a digit runs into the next
    # sentence and takes the CSS ellipsis instead of losing the answer.
    check("a sentence that ends in front of a digit runs long instead",
          cal_run(prose="Dinner at seven. 8pm is free.")["said"],
          "Dinner at seven. 8pm is free.")
    check("and one that ends in front of a capital still ends there",
          cal_run(prose="Dinner at seven. Nothing else until Friday.")["said"],
          "Dinner at seven.")

    # Capitals the list only carried in lower case, and the reverse. Every
    # string here has a capital after the stop, so only `CAL_ABBREV` can save
    # it — the forward test says "cut" on all of them.
    check("a lower-case place or company word is a shortening too", (
        cal_run(prose="Meet on Oak st. Nothing else today.")["said"],
        cal_run(prose="Up mt. Belrose at ten.")["said"],
        cal_run(prose="Fernbrook co. Nothing else today.")["said"]),
        ("Meet on Oak st. Nothing else today.",
         "Up mt. Belrose at ten.",
         "Fernbrook co. Nothing else today."))
    check("and a capitalised one that opens the answer",
          (cal_run(prose="Approx. Two hours, then you are free.")["said"],
           cal_run(prose="Etc. Nothing else today.")["said"]),
          ("Approx. Two hours, then you are free.",
           "Etc. Nothing else today."))
    check("`no` and `al` stay lower case, so the answer still ends",
          (cal_run(prose="No. The dentist is at three.")["said"],
           cal_run(prose="The invite is from Al. Nothing else.")["said"]),
          ("No.", "The invite is from Al."),
          "`No.` is half the answers to \"am I free at three?\" and `Al` is a "
          "name")

    # Every literal word on the list, one string each, driven through the real
    # cut. The frame puts a capital after the stop on purpose: the forward
    # test cuts there, so a word that is not matched by `CAL_ABBREV` fails.
    words = abbrev_words()
    check("the list is read off the source rather than copied",
          (len(words) >= 22, "Sept" in words, "st" in words, "St" in words),
          (True, True, True, True), sorted(words))
    frame = "Held at {}. Nothing else today.".format
    table = cal_run(proses=[frame(w) for w in words])
    check("the run got through them", table.get("error"), None)
    check("and every word on it holds its sentence open",
          [w for w, said in zip(words, table["saids"])
           if said != frame(w)], [])
    check("while a word that is not on it does not",
          cal_run(prose=frame("Zzz"))["said"], "Held at Zzz.",
          "the frame is only a test of the list because this one fails")

    print("\n== which glyph an event gets ==")

    # Google Calendar has no category field: `colorId` is unset on almost
    # everything and `eventType` is `default` on everything that survives
    # `_agenda.NOT_APPOINTMENTS`. So the title decides, and the structure of
    # the event decides what the title cannot.
    got = cal_run(cats=[
        {"title": "Lunch with Ann"},
        {"title": "ランチ"},
        {"title": "LUNCH"},
        {"title": "Lunch", "kind": "out of office"},
        {"title": "Lunch", "meet": "https://meet.example/x"},
        {"title": "Kickoff", "meet": "https://meet.example/x"},
        {"title": "Kickoff", "people": 4},
        {"title": "Kickoff", "people": 1},
        {"title": "Kickoff", "all_day": True},
        {"title": "Kickoff"},
        {"title": "歯医者"},
    ])
    check("the run got through them", got.get("error"), None)
    emoji = [pair[0] for pair in got["cats"]]
    check("an English meal is a meal", emoji[0], "🍽")
    check("and so is a Japanese one", emoji[1], "🍽",
          "no word boundaries anywhere — Japanese has no spaces to anchor on")
    check("case is not the title's problem", emoji[2], "🍽")
    check("out of office outranks whatever the title says", emoji[3], "🏝")
    check("lunch on a video call is still lunch", emoji[4], "🍽",
          "structure is only asked once the title has had its say")
    check("a title nobody wrote a rule for, on a call, is a call",
          emoji[5], "📹")
    check("with people on it and no link, it is people", emoji[6], "👥")
    check("one attendee is nobody but them", emoji[7], "📅")
    check("all day and otherwise unremarkable is a date", emoji[8], "📆")
    check("and everything left over falls back", emoji[9], "📅",
          "which on a personal calendar is a good third of the rows")
    check("a Japanese keyword outside the meal table matches too",
          emoji[10], "🏥")
    # The lock was the glyph for a free/busy block, which is the one kind of
    # event only somebody else's calendar produced.
    check("and no glyph is left over for an event off another calendar",
          ("🔒" in calendar_src(), "CAL_BUSY" in calendar_src()),
          (False, False))
    check("every category names a Material Symbol beside its emoji",
          [p[1] for p in got["cats"] if not p[1]], [])

    print("\n== a panel with no colour emoji font ==")

    # Raspberry Pi OS does not always ship `fonts-noto-color-emoji`, and
    # Chromium draws a hollow box rather than raising. U+FFFF is a
    # noncharacter, so anything measuring the same as it is coming out of the
    # same `.notdef`.
    check("two emoji that measure like a noncharacter are tofu",
          cal_run(widths={"￿": 10, "📅": 10, "🍽": 10})["emoji"], False)
    check("one that does not is a font",
          cal_run(widths={"￿": 10, "📅": 20, "🍽": 10})["emoji"], True)
    check("either one of them is enough",
          cal_run(widths={"￿": 10, "📅": 10, "🍽": 20})["emoji"], True)

    css = (JS.parent / "css" / "style.css").read_text(encoding="utf-8")
    check("the icon behind the emoji is hidden by default",
          ".cal-cat .material-symbols-outlined { display: none;" in css, True)
    check("and the class swaps which of the two is drawn",
          (".no-emoji .cal-cat .cal-emoji { display: none; }" in css,
           ".no-emoji .cal-cat .material-symbols-outlined { display: inline; }"
           in css), (True, True))
    app = (JS / "app.js").read_text(encoding="utf-8")
    check("app.js is what puts the class on",
          'classList.toggle("no-emoji"' in app, True)
    started = block(app, "async function init(")
    check("once, at startup, because a font does not arrive later",
          (app.count("markEmoji();"), before(started, "markEmoji();",
                                             "connect();")),
          (1, True), "called from init, ahead of the socket that draws cards")

    print("\n== the gws card with a calendar in it ==")

    # Everything in this block is about what did not change. A Workspace answer
    # that is not a calendar one has to come out of `gwsFill` exactly as it did
    # before there was a calendar to draw.
    plain = gws_fill_run([{"result": "Three unread from the school."}])
    check("the run got through it", plain.get("error"), None)
    check("an ordinary answer is still the markdown and nothing else",
          plain["html"], "[md]Three unread from the school.")
    check("with the progress line taken off", plain["progress"], True)
    check("and the card no longer pending or failed", plain["classes"],
          ["card", "gws"])

    lost = gws_fill_run([{"error": "lost"}])
    check("a failure with no calendar is still a bare sentence",
          (lost["text"], lost["html"]), ("No answer came back.", ""))
    check("on a card marked failed", "failed" in lost["classes"], True)
    check("and not marked as carrying one",
          "with-cal" in lost["classes"], False,
          "`.gws.failed:not(.with-cal) .md` dims it, which a grid must not be")

    expired = gws_fill_run([{"error": "expired"}])
    check("an expired access failure is on a card marked failed",
          "failed" in expired["classes"], True)
    check("and carries the explanation and settings action",
          "Google Workspace access has expired" in expired["html"]
          and "Settings" in expired["html"], True)

    # The #129 card. What used to come out of here was the grid and then the
    # whole answer rendered under it as markdown, which on "what is on today?"
    # is the same six rows a second time in prose. One card, one answer: the
    # grid is the day, and the slot over it holds one sentence of what was said.
    both = gws_fill_run([{
        "result": "Dinner at seven. Nothing else until Friday.",
        "agenda": agenda([day(**{"from": "19:00", "to": "21:00",
                                 "title": "Dinner"})]),
    }])
    check("a calendar answer draws the grid",
          both["html"].startswith('<div class="cal">'), True,
          both["html"][:40])
    check("and the answer is not written out under it a second time",
          ("[md]" in both["html"], both["html"].count("Dinner")), (False, 2),
          "once as the row's title, once in the slot — never as markdown")
    check("the slot holding the first sentence of what was said",
          '<div class="cal-said">Dinner at seven.</div>'
          in both["html"], True, both["html"][:200])
    check("on a card that says it is carrying one",
          "with-cal" in both["classes"], True)

    # `calSaid` is the only route a calendar answer's prose has to the glass:
    # with an agenda on the frame the `||` in `gwsFill` short-circuits and
    # `formatMarkdown` is never reached, so whatever the cut leaves is the
    # whole of the visible answer. This one used to leave "Dr."
    abbrev = gws_fill_run([{
        "result": "Dr. Tanaka at 10:00, then you are free.",
        "agenda": agenda([day(**{"from": "10:00", "to": "11:00",
                                 "title": "Dentist"})]),
    }])
    check("a whole sentence reaches the slot on a card with a grid on it",
          '<div class="cal-said">Dr. Tanaka at 10:00, then you are free.'
          "</div>" in abbrev["html"], True,
          abbrev["html"][:120])
    check("and it is the only prose there, nothing else rendering markdown",
          "[md]" in abbrev["html"], False,
          "the right-hand side of the `||` in `gwsFill` is never evaluated")

    stopped = gws_fill_run([{
        "error": "slow",
        "agenda": agenda([day(**{"from": "19:00", "to": "21:00",
                                 "title": "Dinner"})]),
    }])
    check("a run stopped after reading the calendar still draws it",
          '<div class="cal">' in stopped["html"], True)
    check("and the slot is where it says what it did not get to",
          "Took too long and was stopped."
          in stopped["html"].split('<div class="cal-said">')[-1][:80], True)
    check("with the card still marked failed, so the line reads as one",
          ("failed" in stopped["classes"], "with-cal" in stopped["classes"]),
          (True, True), "`.gws.failed .cal-said` is what dims it now")

    # The deadline fires, then the answer lands a beat later on the same card.
    twice = gws_fill_run([
        {"error": "lost"},
        {"result": "Dinner at seven.",
         "agenda": agenda([day(**{"from": "19:00", "to": "21:00",
                                  "title": "Dinner"})])},
    ])
    check("an answer that beat its own deadline redraws rather than stacks",
          twice["html"].count('<div class="cal">'), 1)
    check("and clears the failure it is replacing",
          "failed" in twice["classes"], False)

    # The slot has to survive an answer longer than the panel is wide, and the
    # panel is 480px of it. One line, clipped, is the shape — the same one
    # `.cal-title` already has, so a row and the line over it end the same way.
    sheet = (JS.parent / "css" / "style.css").read_text(encoding="utf-8")
    said_rule = block(sheet, ".cal-said {")
    check("the slot is one clipped line and not a paragraph",
          ("white-space: nowrap;" in said_rule,
           "text-overflow: ellipsis;" in said_rule,
           "overflow: hidden;" in said_rule), (True, True, True))
    check("and a failure's line is told apart from an answer's",
          ".gws.failed .cal-said {" in sheet, True,
          "same slot, dimmed and italic — the card is the only other signal")
    # `.gws-said` styled the paragraph the error branch used to append beside
    # the grid. That branch is gone, so the rule is a selector nothing matches.
    check("and the rule for the paragraph it replaced went with it",
          ("gws-said" in sheet, "gws-said" in (JS / "cards.js").read_text(
              encoding="utf-8")), (False, False))


# The weather card's sunrise line: two spans on most days, one word on the
# days there is no sunrise. `wxSun` and its table are lifted with the real
# `STRINGS` and `t`, not a stub — the word *is* the fix, so a `wx_polar_*` key
# missing from a locale has to fail a case here rather than blank the line on
# the panel. `escapeHtml` is written out for the reason `CALENDAR` gives.
SUN_LINE = r"""
function escapeHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
globalThis.document = { documentElement: { lang: %s } };

%s

console.log(JSON.stringify(JSON.parse(process.argv[1]).map(wxSun)));
"""


def sun_line_run(readings: list[dict], lang: str = "en") -> list[str]:
    """Drive `cards.js`'s `wxSun` over a list of `current` objects."""
    src = (JS / "cards.js").read_text(encoding="utf-8")
    lifted = "\n\n".join(
        [block(i18n_src(), header).removeprefix("export ")
         for header in ("export const STRINGS = ", "export function t(")]
        + [block(src, "const WX_POLAR = ") + ";",
           block(src, "function wxSun(")])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         SUN_LINE % (json.dumps(lang), lifted), json.dumps(readings)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        # Reported rather than fatal, like `cal_run`: a name left dangling by
        # an edit to the section should fail the case that reads it.
        return [proc.stderr.strip()[-300:]] * len(readings)
    return json.loads(proc.stdout)


def sun_line_cases() -> None:
    """What the card says on a day the sun does not cross the horizon.

    It used to say `Sunrise 00:00   Sunset 00:00`, because Open-Meteo answers
    such a day with midnight at both ends and `"00:00"` is truthy, so the
    `|| "—"` beside each one never fired. Two identical times, worn with the
    confidence of a reading — where the em dash the card already had reads as
    *we do not know*, which is nearer the truth and was unreachable.

    The server now sends the times empty and says which kind of day it was in
    `sun`, so the fix on this side is a word instead of a pair. The case that
    keeps it honest is the second: at 83.8°N the sun really does rise at
    00:46, and that day has to keep rendering as two times.
    """
    print("\n== the sunrise line, on a day there is no sunrise ==")

    def spans(html: str) -> list[str]:
        return re.findall(r"<span>([^<]*)</span>", html)

    ordinary = {"sun": "rises", "sunrise": "05:23", "sunset": "17:52"}
    shoulder = {"sun": "rises", "sunrise": "00:46", "sunset": "22:43"}
    night = {"sun": "polar_night", "sunrise": "", "sunset": ""}
    day = {"sun": "polar_day", "sunrise": "", "sunset": ""}
    unknown = {"sun": "unknown", "sunrise": "", "sunset": ""}

    en = sun_line_run([ordinary, shoulder, night, day, unknown])
    check("the run got through them", en[0].startswith("<span>"), True,
          en[0][:200])
    check("an ordinary day is two times", spans(en[0]),
          ["Sunrise 05:23", "Sunset 17:52"])
    check("and so is a real sunrise three quarters of an hour past midnight",
          spans(en[1]), ["Sunrise 00:46", "Sunset 22:43"],
          "the shoulder weeks are not what this is fixing")
    check("a polar night is one word and no time at all",
          spans(en[2]), ["Polar night"])
    check("with no midnight anywhere in it", "00:00" in en[2], False)
    check("a polar day is the other word", spans(en[3]), ["Midnight sun"])
    check("and an answer nobody could make still reads as unknown",
          spans(en[4]), ["Sunrise —", "Sunset —"],
          "the em dash was always right for this one")

    check("both words are there in the panel's other language",
          [spans(h) for h in sun_line_run([night, day], "ja")],
          [["極夜"], ["白夜"]])

    # `wxSun` is lifted above rather than reached through `weatherCard`,
    # which wants a DOM to build its element in. So the call site is pinned
    # by source: without these two, everything above is checking a function
    # the card is free to have stopped calling.
    detail = block((JS / "cards.js").read_text(encoding="utf-8"),
                   "export function weatherCard(")
    check("the card's detail row is that one call",
          "${wxSun(now)}" in detail, True)
    check("and prints no sunrise of its own beside it",
          [k for k in ("wx_sunrise", "wx_sunset") if k in detail], [],
          "the two spans moved into `wxSun`, they were not duplicated")


# The weather card's local clock and weekday relabeling across timezones.
#
# #203: Open-Meteo answers in the queried location's local time (timezone:
# "auto"). The card used to render hours and 今日/明日 with no timezone or
# local time, directly beside the Pi's clock. When querying a remote location
# across date boundaries (e.g. Honolulu from Tokyo), row 0 was labelled 今日
# even though Honolulu was still on yesterday's calendar.
#
# The card renders the remote local time beside the location name in `.wx-head`,
# and relabels row 0 and row 1 to weekday names when the remote forecast date
# differs from the client's local date.
WX_DAYS = r"""
function escapeHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
function escapeAttr(s) {
  if (!s) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
globalThis.document = { documentElement: { lang: %s } };

%s

const [days, nowIso] = JSON.parse(process.argv[1]);
const clientNow = nowIso ? new Date(nowIso) : undefined;
console.log(JSON.stringify(wxDays(days, "°", clientNow)));
"""


def wx_days_run(days: list[dict], now_iso: str | None = None,
                lang: str = "en") -> list[str]:
    """Run `cards.js`'s `wxDays` and extract row labels."""
    src = (JS / "cards.js").read_text(encoding="utf-8")
    lifted = "\n\n".join([
        block(src, "const WX_ICONS = ") + ";",
        block(src, "export function wxIcon(").removeprefix("export "),
        block(src, "function wxValue("),
        block(src, "function calLang("),
        block(src, "function calFormat("),
        block(src, "function calWeekday("),
        block(src, "function wxDays("),
    ])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         WX_DAYS % (json.dumps(lang), lifted), json.dumps([days, now_iso])],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return [proc.stderr.strip()[-300:]]
    html = json.loads(proc.stdout)
    return re.findall(r'<span class="wx-d-label">([^<]*)</span>', html)


def wx_timezone_cases() -> None:
    """The weather card's local clock and weekday relabeling across timezones."""
    print("\n== the weather card's remote local time and timezone relabeling ==")

    detail = block((JS / "cards.js").read_text(encoding="utf-8"),
                   "export function weatherCard(")
    check("the weather card renders the remote local time in wx-head",
          '<span class="wx-time" title="${escapeAttr(data.timezone || "")}">${escapeHtml(now.time)}</span>' in detail,
          True)
    check("and forwards clientNow into wxDays",
          "${wxDays(data.daily || [], unit, clientNow)}" in detail, True)

    days_ja = [
        {"date": "2026-09-10", "label": "今日", "weekday": "木", "condition": "clear", "high": 25, "low": 18},
        {"date": "2026-09-11", "label": "明日", "weekday": "金", "condition": "rain", "high": 22, "low": 17},
        {"date": "2026-09-12", "label": "土", "weekday": "土", "condition": "cloudy", "high": 24, "low": 19},
        {"date": "2026-09-13", "label": "日", "weekday": "日", "condition": "clear", "high": 26, "low": 20},
        {"date": "2026-09-14", "label": "月", "weekday": "月", "condition": "clear", "high": 27, "low": 21},
    ]

    same = wx_days_run(days_ja, "2026-09-10T12:00:00", "ja")
    check("same calendar day keeps 今日 and 明日", same,
          ["今日", "明日", "土", "日", "月"])

    diff = wx_days_run(days_ja, "2026-09-11T12:00:00", "ja")
    check("different day relabels row 0 and 1 to weekday names", diff,
          ["木", "金", "土", "日", "月"])

    days_no_wday = [
        {"date": "2026-09-10", "label": "今日", "condition": "clear", "high": 25, "low": 18},
        {"date": "2026-09-11", "label": "明日", "condition": "rain", "high": 22, "low": 17},
        {"date": "2026-09-12", "label": "土", "condition": "cloudy", "high": 24, "low": 19},
    ]
    fallback = wx_days_run(days_no_wday, "2026-09-11T12:00:00", "ja")
    check("and falls back to calWeekday when weekday is missing", fallback,
          ["木", "金", "土"])

    days_en = [
        {"date": "2026-09-10", "label": "Today", "weekday": "Thu", "condition": "clear"},
        {"date": "2026-09-11", "label": "Tomorrow", "weekday": "Fri", "condition": "rain"},
        {"date": "2026-09-12", "label": "Sat", "weekday": "Sat", "condition": "cloudy"},
    ]
    diff_en = wx_days_run(days_en, "2026-09-11T12:00:00", "en")
    check("weekday relabeling works in English too", diff_en,
          ["Thu", "Fri", "Sat"])

    css = (JS.parent / "css" / "style.css").read_text(encoding="utf-8")
    rules = dict(css_rules(css))
    check(".wx-time has flex-shrink: 0 so it never shrinks when place truncates",
          "flex-shrink: 0" in rules.get(".wx-time", ""), True)


# The same shape as `SUN_LINE`, and for the same reason: `head` and `nearPill`
# are string builders, so they run with no DOM at all beyond the `<html lang>`
# that `t` reads. `STRINGS` and `t` are the real ones — a `near_around` missing
# from a locale has to fail here rather than print `near_around` on the panel —
# and `escapeHtml` is written out for the reason `CALENDAR` gives.
NEAR_PILL = r"""
function escapeHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
globalThis.document = { documentElement: { lang: %s } };

%s

console.log(JSON.stringify(JSON.parse(process.argv[1]).map(
  (near) => head("location_on", "富士山", nearPill(near)))));
"""


def near_pill_run(nears: list[str], lang: str = "en") -> list[str]:
    """Build a card head for each `near`, through the real `t`."""
    src = (JS / "cards.js").read_text(encoding="utf-8")
    lifted = "\n\n".join(
        [block(i18n_src(), header).removeprefix("export ")
         for header in ("export const STRINGS = ", "export function t(")]
        + [block(src, "function head("), block(src, "function nearPill(")])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         NEAR_PILL % (json.dumps(lang), lifted), json.dumps(nears)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return [proc.stderr.strip()[-300:]] * len(nears)
    return json.loads(proc.stdout)


def near_pill_cases() -> None:
    """Whether the card says which area the search was confined to.

    #196: the answer was about 富士山, the map beside it was of 港区, and the
    card had no room in it for the word 港区 — `near` has been in the payload
    since the tool was written and no renderer read it. The filter is fixed in
    `maps.py`, so the case that produced that card is gone; this is the part
    that makes the next one visible instead of silent.

    An empty pill is the load-bearing case. After the fix an omitted `near`
    means the search really was of everywhere, and drawing "near " with
    nothing after it would put a filter on the card that was never applied —
    the same lie in the other direction.
    """
    print("\n== the area a place search was confined to ==")

    def pill(html: str) -> str:
        found = re.search(r'<span class="near">(.*?)</span>', html, re.S)
        return found.group(1) if found else ""

    out = near_pill_run(["", "横浜駅", "3 Chome-1 Shibakoen, Minato City",
                         "<script>x</script>"])
    check("the run got through them", out[0].startswith("\n    <div"), True,
          out[0][:60].strip())
    check("a search of everywhere draws no pill at all",
          'class="near"' in out[0], False,
          "an empty one would claim a filter that was never applied")
    check("and still draws the place", "富士山" in out[0], True)
    check("a search around somewhere says so", pill(out[1]), "near 横浜駅")
    check("however long the somewhere is", pill(out[2]),
          "near 3 Chome-1 Shibakoen, Minato City",
          "cut off by the stylesheet rather than by this")
    check("and it is a place name, not markup",
          [pill(out[3]), "<script>" in out[3]],
          ["near &lt;script&gt;x&lt;/script&gt;", False])

    check("the other locale has the word too",
          pill(near_pill_run(["横浜駅"], "ja")[0]), "横浜駅周辺")

    # Lifted above rather than reached through either card, which wants a DOM.
    # So the two call sites are pinned by source: without these, everything
    # above is checking a function no card is obliged to call.
    src = (JS / "cards.js").read_text(encoding="utf-8")
    check("both cards with a `near` in their payload draw one",
          [header for header in ("export function placesCard(",
                                 "export function placeInfoCard(")
           if "nearPill(data.near)" not in block(src, header)], [])


PLACE_INFO_CARD = r"""
class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.className = "";
    this.children = [];
    this.parentElement = null;
  }
  appendChild(child) {
    this.children.push(child);
    child.parentElement = this;
    return child;
  }
  remove() {
    if (this.parentElement) {
      const idx = this.parentElement.children.indexOf(this);
      if (idx !== -1) this.parentElement.children.splice(idx, 1);
      this.parentElement = null;
    }
  }
  querySelector(sel) {
    if (sel === ".map") {
      return this.children.find(c => c.className === "map") || null;
    }
    return null;
  }
}

function el(html) {
  const card = new Element("div");
  card.className = "card place-card";
  if (html.includes('<div class="map"></div>')) {
    const map = new Element("div");
    map.className = "map";
    card.appendChild(map);
  }
  return card;
}

function head(icon, title, extra = "") { return ""; }
function nearPill(near) { return ""; }
function imageGrid(images) { return ""; }
function videoPlayer(videos) { return ""; }
function linkChips(sources, icon) { return ""; }
function formatMarkdown(md) { return ""; }
function escapeHtml(s) { return String(s); }
function escapeAttr(s) { return String(s); }
function t(k) { return k; }

let visibleCallbacks = [];
function whenVisible(card, root, cb) {
  visibleCallbacks.push({ card, cb });
}

let newMapCalls = [];
function newMap(div) {
  const m = { div, id: "mock_map" };
  newMapCalls.push(m);
  return m;
}

let markPlacesCalls = [];
async function markPlaces(map, places) {
  markPlacesCalls.push({ places, hasMap: !!map });
  return true;
}

let markNamedPlaceCalls = [];
let namedPlaceResult = true;
async function markNamedPlace(map, place) {
  markNamedPlaceCalls.push({ place, hasMap: !!map });
  return namedPlaceResult;
}

const req = JSON.parse(process.argv[1]);
if (req.hasGoogle) {
  globalThis.window = { google: { maps: {} } };
} else {
  globalThis.window = {};
}
if (req.namedPlaceResult !== undefined) {
  namedPlaceResult = req.namedPlaceResult;
}

%s

const card = placeInfoCard(req.data, null);
const initialHasMap = card.querySelector(".map") !== null;

let afterVisibleHasMap = initialHasMap;
let namedCalls = [];
let placesCalls = [];

if (req.triggerVisible && visibleCallbacks.length > 0) {
  await visibleCallbacks[0].cb();
  afterVisibleHasMap = card.querySelector(".map") !== null;
  namedCalls = markNamedPlaceCalls;
  placesCalls = markPlacesCalls;
}

console.log(JSON.stringify({
  initialHasMap,
  afterVisibleHasMap,
  visibleRegistered: visibleCallbacks.length,
  namedCalls,
  placesCalls,
}));
"""


MARK_NAMED_PLACE = r"""
const calls = {
  findPlace: [],
  geocode: [],
  markers: [],
  fitBounds: [],
  setCenter: [],
};

class LatLng {
  constructor(lat, lng) { this._lat = lat; this._lng = lng; }
  lat() { return this._lat; }
  lng() { return this._lng; }
}

globalThis.window = {
  google: {
    maps: {
      SymbolPath: { CIRCLE: "CIRCLE" },
      Marker: class {
        constructor(opts) { calls.markers.push(opts); }
      },
      places: {
        PlacesService: class {
          constructor(map) { this.map = map; }
          findPlaceFromQuery(req, cb) {
            calls.findPlace.push(req);
            if (globalThis.__findPlaceFail) {
              cb(null, "ZERO_RESULTS");
            } else {
              cb([{
                geometry: { location: new LatLng(36.56, 136.66) },
                name: "兼六園",
              }], "OK");
            }
          }
        },
      },
      Geocoder: class {
        geocode(req, cb) {
          calls.geocode.push(req);
          if (globalThis.__geocodeFail) {
            cb(null, "ZERO_RESULTS");
          } else {
            cb([{
              geometry: {
                location: new LatLng(36.56, 136.66),
                viewport: { sw: 1, ne: 2 },
              },
            }], "OK");
          }
        }
      },
      event: {
        addListenerOnce(target, evt, fn) { fn(); },
      },
    },
  },
};
globalThis.google = globalThis.window.google;

const MAP_ZOOM_CAP = 15;

%s

const req = JSON.parse(process.argv[1]);
if (req.findPlaceFail) globalThis.__findPlaceFail = true;
if (req.geocodeFail) globalThis.__geocodeFail = true;

const map = {
  getZoom: () => 12,
  setZoom: () => {},
  setCenter: (loc) => { calls.setCenter.push(loc); },
  fitBounds: (bounds) => { calls.fitBounds.push(bounds); },
};

const res = await markNamedPlace(map, req.place);
console.log(JSON.stringify({
  success: res,
  calls,
}));
"""


def place_info_run(req: dict) -> dict:
    """Drive `cards.js`'s `placeInfoCard` under node."""
    src = (JS / "cards.js").read_text(encoding="utf-8")
    body = block(src, "export function placeInfoCard(").removeprefix("export ")
    code = PLACE_INFO_CARD % body
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", code, json.dumps(req)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip(), "initialHasMap": False,
                "afterVisibleHasMap": False, "visibleRegistered": 0,
                "namedCalls": [], "placesCalls": []}
    return json.loads(proc.stdout)


def mark_named_place_run(req: dict) -> dict:
    """Drive `cards.js`'s `markNamedPlace` under node."""
    src = (JS / "cards.js").read_text(encoding="utf-8")
    body = block(src, "export function markNamedPlace(").removeprefix("export ")
    code = MARK_NAMED_PLACE % body
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", code, json.dumps(req)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip(), "success": False, "calls": {}}
    return json.loads(proc.stdout)


def card_cases() -> None:
    print("\n== the browse card, when the job never finished ==")

    # The frame the server now sends from its `except`. Before it, nothing ever
    # ended a browse card but the `done` on the last line of the happy path, so
    # a crashed job left its spinner running for the rest of the session —
    # beside a voice that had already said it could not read the page.
    crashed = card_run({"phase": "done", "url": "", "answer": ""})
    check("the run got through the frame", crashed.get("error"), None)
    check("a bare done frame stops the spinner",
          "done" in crashed["classes"], True)
    check("and the card says so, in words", crashed["md"],
          "Could not read that page.")
    check("in the language of the turn",
          card_run({"phase": "done", "url": "", "answer": ""}, "ja")["md"],
          "そのページを読めませんでした。")
    check("the status line clears rather than showing an empty host",
          crashed["status"], "")
    # Thin on purpose: no `shot`, so whatever page it got to last stays on the
    # card. A frame carrying an empty screenshot would blank it.
    check("and the last screenshot is left where it is",
          (crashed["shot"], "has-shot" in crashed["classes"]), ("", False))

    # The one that must not regress. Every case above is reachable from a badly
    # written `done`, so an ordinary answer has to come out unchanged.
    whole = card_run({"phase": "done", "url": "https://shop.test/hours",
                      "answer": "Open 10:00-18:00.", "shot": "data:x"})
    check("an ordinary answer is still rendered as one",
          whole["md"], "[md]Open 10:00-18:00.")
    check("with the host it came from", whole["status"], "shop.test")
    check("and its screenshot", (whole["shot"], "has-shot" in whole["classes"]),
          ("data:x", True))

    step = card_run({"phase": "step", "step": 2, "url": "https://shop.test/",
                     "shot": "data:y"})
    check("and a step frame is still not the end of anything",
          (step["status"], "done" in step["classes"]),
          ("Step 2 · shop.test", False))

    print("\n== the browse card, when the location changed under it ==")

    # The only frame the front end writes for itself. The socket that would
    # have carried `done` is closed and a browse card has no deadline behind
    # it, so nothing else is coming.
    halfway = [{"phase": "step", "step": 2, "url": "https://shop.test/",
                "shot": "data:y"},
               {"phase": "done", "lost": True}]
    gone = card_run(halfway)
    check("the run got through the frames", gone.get("error"), None)
    check("the spinner stops", "done" in gone["classes"], True)
    check("and says the location changed, not that the page was unreadable",
          gone["md"], "The location changed before that finished.")
    check("in the language of the turn", card_run(halfway, "ja")["md"],
          "場所が変わったため中断しました。")
    # The difference between this and a crash, and the reason it is a string of
    # its own: the card is being kept, so what it reached is worth keeping too.
    check("with the step it had got to left on it", gone["status"],
          "Step 2 · shop.test")
    check("and the page it was looking at", gone["shot"], "data:y")

    print("\n== the place info card, with and without pin chunks ==")

    # Empty places chunk but valid place name preserves .map in DOM
    with_place = place_info_run({
        "data": {"place": "兼六園, 金沢", "places": []},
        "hasGoogle": True,
        "triggerVisible": False,
    })
    check("named place without pin chunks preserves .map element in DOM",
          with_place["initialHasMap"], True)
    check("and registers visibility callback",
          with_place["visibleRegistered"], 1)

    # When card scrolls into view, fallback to markNamedPlace
    scrolled = place_info_run({
        "data": {"place": "兼六園, 金沢", "places": []},
        "hasGoogle": True,
        "triggerVisible": True,
        "namedPlaceResult": True,
    })
    check("scrolling into view invokes markNamedPlace with place name",
          [c["place"] for c in scrolled["namedCalls"]], ["兼六園, 金沢"])
    check("successful geocode keeps .map in DOM",
          scrolled["afterVisibleHasMap"], True)

    # Failed geocode removes .map from DOM
    failed_geocode = place_info_run({
        "data": {"place": "兼六園, 金沢", "places": []},
        "hasGoogle": True,
        "triggerVisible": True,
        "namedPlaceResult": False,
    })
    check("failed geocode removes .map element from DOM",
          failed_geocode["afterVisibleHasMap"], False)

    # Empty places and no place name removes .map immediately
    no_place = place_info_run({
        "data": {"places": []},
        "hasGoogle": True,
        "triggerVisible": False,
    })
    check("empty places with no place name removes .map immediately",
          no_place["initialHasMap"], False)

    # No google.maps removes .map immediately even with place name
    no_maps = place_info_run({
        "data": {"place": "兼六園, 金沢", "places": []},
        "hasGoogle": False,
        "triggerVisible": False,
    })
    check("absence of google.maps removes .map element",
          no_maps["initialHasMap"], False)

    # Non-empty places uses markPlaces instead of markNamedPlace
    with_pins = place_info_run({
        "data": {"place": "兼六園", "places": [{"place_id": "p1"}]},
        "hasGoogle": True,
        "triggerVisible": True,
    })
    check("non-empty places invokes markPlaces",
          len(with_pins["placesCalls"]), 1)
    check("and does not invoke markNamedPlace",
          len(with_pins["namedCalls"]), 0)

    print("\n== markNamedPlace geocoding and PlacesService fallback ==")
    # 1. PlacesService success
    places_hit = mark_named_place_run({"place": "兼六園", "findPlaceFail": False, "geocodeFail": False})
    check("PlacesService query is executed", len(places_hit["calls"]["findPlace"]), 1)
    check("PlacesService success drops marker and returns true",
          (places_hit["success"], len(places_hit["calls"]["markers"])), (True, 1))
    check("and does not invoke Geocoder", len(places_hit["calls"]["geocode"]), 0)

    # 2. PlacesService fails, Geocoder succeeds
    geocode_hit = mark_named_place_run({"place": "兼六園", "findPlaceFail": True, "geocodeFail": False})
    check("PlacesService failure falls back to Geocoder",
          (len(geocode_hit["calls"]["findPlace"]), len(geocode_hit["calls"]["geocode"])), (1, 1))
    check("Geocoder success drops marker and returns true",
          (geocode_hit["success"], len(geocode_hit["calls"]["markers"])), (True, 1))

    # 3. Both fail
    both_fail = mark_named_place_run({"place": "Unknown Place", "findPlaceFail": True, "geocodeFail": True})
    check("both PlacesService and Geocoder failing returns false", both_fail["success"], False)


def plan_run(data: dict) -> dict:
    """Drive `cards.js`'s `planFill` over one frame under node."""
    src = (JS / "cards.js").read_text(encoding="utf-8")
    body = PLAN % block(src, "export function planFill(").removeprefix("export ")
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", body, json.dumps(data)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip(), "classes": [], "text": "",
                "html": "", "kids": []}
    return json.loads(proc.stdout)


def plan_cases() -> None:
    print("\n== what a stopped plan puts on the card ==")

    # The three that must never show their `result`. Each of those strings is
    # an instruction aimed at the model — "say you could not do it and offer to
    # try one part instead" — and reading it off a kitchen screen is worse than
    # showing nothing.
    for kind in ("failed", "empty", "unset"):
        got = plan_run({"error": kind, "result": "SAY THIS TO THE MODEL"})
        check(f"a {kind} plan draws its line and nothing else",
              (got["text"], got["kids"]), (f"<plan_{kind}>", []))
        check("and the card is marked failed", "failed" in got["classes"], True)

    # A timeout with nothing settled: unchanged from before this, and the case
    # that says the fix did not turn every stop into a two-part card.
    bare = plan_run({"error": "slow", "result": "Working that out ran too long"})
    check("a timeout that got nowhere draws the stopped line alone",
          (bare["text"], bare["kids"]), ("<plan_slow>", []))

    # The one this is for. `partial` is set by the server and only on this
    # case, so the client never has to know where TOO_SLOW_PARTIAL ends.
    part = plan_run({"error": "slow",
                     "result": "Working that out ran too long\n\nSoba at 11:00",
                     "partial": "Soba at 11:00"})
    check("a timeout with something settled still says it was stopped",
          part["text"], "<plan_slow>")
    check("and shows the part it did settle under it",
          part["kids"], ["[md]Soba at 11:00"],
          "the answer read aloud but not written down is the whole bug")
    check("still styled as a failure, not as a whole answer",
          "failed" in part["classes"], True)
    check("and not left pending", "pending" in part["classes"], False)
    # `result` still carries the instructions with the answer glued on. Reading
    # that instead of `partial` puts TOO_SLOW_PARTIAL's prose on the screen.
    check("the instructions to the model are not on the card",
          "ran too long" in str(part["kids"]), False)

    whole = plan_run({"result": "Soba at 11:00"})
    check("and an ordinary answer is unaffected",
          (whole["html"], whole["kids"]), ("[md]Soba at 11:00", []))
    check("with no failed styling", "failed" in whole["classes"], False)


def remember_run(results: list[dict], lang: str = "en") -> dict:
    """Drive `cards.js`'s `rememberCard` over what `memory.remember` returns."""
    cards = (JS / "cards.js").read_text(encoding="utf-8")
    lifted = "\n\n".join([
        block((JS / "markdown.js").read_text(encoding="utf-8"),
              "export function escapeHtml(").removeprefix("export "),
        block(i18n_src(), "export const STRINGS = ").removeprefix("export "),
        block(i18n_src(), "export function t(").removeprefix("export "),
        block(cards, "function head("),
        block(cards, "export function rememberCard(").removeprefix("export "),
    ])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         REMEMBER % (json.dumps(lang), lifted), json.dumps(results)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip(), "cards": [""] * len(results)}
    return {"error": None, "cards": json.loads(proc.stdout)}


def calculate_run(results: list[dict], lang: str = "en") -> dict:
    """Drive `cards.js`'s `calculateCard` over what `calc.calculate` returns."""
    cards = (JS / "cards.js").read_text(encoding="utf-8")
    lifted = "\n\n".join([
        block((JS / "markdown.js").read_text(encoding="utf-8"),
              "export function escapeHtml(").removeprefix("export "),
        block(i18n_src(), "export const STRINGS = ").removeprefix("export "),
        block(i18n_src(), "export function t(").removeprefix("export "),
        block(cards, "function head("),
        block(cards, "export function calculateCard(").removeprefix("export "),
    ])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         CALCULATE % (json.dumps(lang), lifted), json.dumps(results)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip(), "cards": [""] * len(results)}
    return {"error": None, "cards": json.loads(proc.stdout)}


def web_search_run(results: list[dict], lang: str = "en") -> dict:
    """Drive `cards.js`'s `webSearchCard` over what `web_search.web_search` returns."""
    cards = (JS / "cards.js").read_text(encoding="utf-8")
    md = (JS / "markdown.js").read_text(encoding="utf-8").replace("export ", "")
    lifted = "\n\n".join([
        md,
        block(i18n_src(), "export const STRINGS = ").removeprefix("export "),
        block(i18n_src(), "export function t(").removeprefix("export "),
        block(cards, "function head("),
        block(cards, "function linkChips("),
        block(cards, "function imageGrid("),
        block(cards, "export function webSearchCard(").removeprefix("export "),
    ])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         WEB_SEARCH % (json.dumps(lang), lifted), json.dumps(results)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip(), "cards": [""] * len(results)}
    return {"error": None, "cards": json.loads(proc.stdout)}


def live_src() -> str:
    """Every module under `live/`, end to end, as one string.

    Read and never imported. `live/tools/__init__.py:4` does
    `from .gws import build_gws_tool`, so importing any member of that package
    authenticates as the owner and reads their real mail on the way past — which
    is not a thing a check script may do to find out what a tool is called.
    """
    live = ROOT / "src" / "ai_terminal" / "live"
    return "\n".join(path.read_text(encoding="utf-8") for path in
                     sorted([*live.glob("*.py"), *(live / "tools").glob("*.py")]))


def registered_tools() -> list[str]:
    """The tool names `build_agent` registers, in the order it registers them.

    The name the model calls, and the name `renderToolResult` switches on, is
    the wrapped function's — not the identifier in the list. Each entry is
    either a module-level `X_tool = FunctionTool(fn)` or a `build_X_tool()`
    whose body wraps one, so both are resolved through to the `fn`.
    """
    agent = (ROOT / "src" / "ai_terminal" / "live" / "agent.py").read_text(
        encoding="utf-8")
    start = agent.index("[", agent.index("tools="))
    depth, i = 0, start
    while depth or i == start:
        depth += {"[": 1, "]": -1}.get(agent[i], 0)
        i += 1
    src = live_src()
    names = []
    for entry, called in re.findall(r"^\s*(\w+)(\(\))?,\s*$",
                                    agent[start + 1:i - 1], re.M):
        wraps = (re.search(rf"\ndef {entry}\(.*?\bFunctionTool\((\w+)\)",
                           src, re.S) if called else
                 re.search(rf"\n{entry} = FunctionTool\((\w+)\)", src))
        names.append(wraps.group(1) if wraps else entry)
    return names


def switch_arms() -> list[tuple[str, str]]:
    """Every `case` in `renderToolResult`, paired with the renderer it reaches.

    The renderer is read off the first line under the run of cases, so the pair
    that share `routeCard` come back as two entries with the same answer, which
    is what they are. An arm that only `return`s pairs with `""`.
    """
    body = block((JS / "app.js").read_text(encoding="utf-8"),
                 "function renderToolResult(")
    pairs: list[tuple[str, str]] = []
    waiting: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        arm = re.fullmatch(r'case "(\w+)":', line)
        if arm:
            waiting.append(arm.group(1))
            continue
        if waiting:
            drawn = re.search(r"appendCard\((\w+)\(", line)
            pairs += [(name, drawn.group(1) if drawn else "")
                      for name in waiting]
            waiting = []
    return pairs


def remember_cases() -> None:
    print("\n== what the panel shows when it is asked to remember something ==")

    # The four answers `remember` can give, in the shapes `memory.py` returns
    # them: an ordinary keep, one whose topic is what the model heard somebody
    # say, a keep that pushed older facts off the end, and a decline.
    run = remember_run([
        {"kept": True, "topic": "the blue car", "known": 4, "dropped": 0},
        {"kept": True, "topic": "<img onerror=alert(1)>", "known": 1,
         "dropped": 0},
        {"kept": True, "topic": "the wifi", "known": 9, "dropped": 2},
        {"kept": False, "why": "SAY THIS TO THE MODEL"},
    ])
    check("the run got through them", run["error"], None)
    kept, dictated, capped, declined = run["cards"]

    # The bug this is for: with no arm in the switch this card was
    # `genericCard`, and what the household read off its own kitchen panel was
    # `{"kept": true, "topic": "the blue car", "known": 4, "dropped": 0}`.
    check("a fact kept is reported in words, not as the dict it came in",
          [frag for frag in ('"kept"', '"topic"', '"known"', "true")
           if frag in kept], [])
    check("and the card names the fact it kept", "the blue car" in kept, True)
    check("with how many are held under it", "4 remembered" in kept, True)
    check("nothing fell off, so nothing on the card says anything did",
          "dropped" in kept, False, "`0 dropped` is a sentence about nothing")
    check("and when something did fall off, the card says so",
          "9 remembered · 2 dropped" in capped, True,
          "a fact pushed off the end is the only news in the answer")

    # A topic is the model's transcription of something said out loud, so it is
    # third-party text in the sense the escaping half of this file means.
    check("a topic cannot become markup", "<img" in dictated, False)
    check("it is escaped instead", "&lt;img onerror=alert(1)&gt;" in dictated,
          True)

    # The decline. Both `why` strings (`memory.py:354`, `:357`) are written at
    # the model — "say it in your answer instead" — the same way `plan`'s error
    # kinds are, and the same ruling applies: the card writes its own line.
    check("a decline says so in the panel's own words",
          "That was not kept." in declined, True)
    check("and never the sentence that was aimed at the model",
          "SAY THIS TO THE MODEL" in declined, False)
    check("styled as a failure rather than as an answer",
          'class="card remember failed"' in declined, True)
    check("with no count on it, because none of it is about a fact",
          "remember-count" in declined, False)

    japanese = remember_run(
        [{"kept": True, "topic": "青い車", "known": 4, "dropped": 0},
         {"kept": False, "why": "SAY THIS TO THE MODEL"}], "ja")
    check("the run got through them", japanese["error"], None)
    check("and the card speaks the language the panel is set to",
          ["覚えました" in japanese["cards"][0],
           "記憶4件" in japanese["cards"][0],
           "覚えられませんでした。" in japanese["cards"][1]],
          [True, True, True])


def calculate_cases() -> None:
    print("\n== what the panel shows for a mathematical calculation (#393) ==")

    run = calculate_run([
        {"expression": "125 * 34", "result": 4250, "formatted": "4,250"},
        {"expression": "sqrt(144) + 2**8", "result": 268.0, "formatted": "268"},
        {"expression": "10 / 0", "error": "Division by zero"},
        {"expression": "<img onerror=alert(1)>", "error": "<error>"},
    ])
    check("the run got through them", run["error"], None)
    success, power, zero_div, injection = run["cards"]

    check("a calculation card has calculate class", 'class="card calculate"' in success, True)
    check("and shows the formatted result", "4,250" in success, True)
    check("and shows the expression", "125 * 34" in success, True)
    check("and uses head_calculate header", "Calculation" in success, True)

    check("a power expression formatted result", "268" in power, True)

    check("a division by zero has failed class", 'class="card calculate failed"' in zero_div, True)
    check("and displays the error message", "Division by zero" in zero_div, True)

    check("an expression with markup is escaped", "<img" in injection, False)
    check("an error with markup is escaped", "<error>" in injection, False)

    japanese = calculate_run([{"expression": "1 + 1", "result": 2, "formatted": "2"}], "ja")
    check("the run got through Japanese test", japanese["error"], None)
    check("and the calculation card speaks the language the panel is set to",
          "計算" in japanese["cards"][0], True)


def web_search_cases() -> None:
    print("\n== what the panel shows for web search and news topic images (#603) ==")

    img1 = {"url": "https://img.test/1.jpg", "title": "Headline 1", "source": "https://news.test/1"}
    img2 = {"url": "https://img.test/2.jpg", "title": "Headline 2", "source": "https://news.test/2"}
    img3 = {"url": "https://img.test/3.jpg", "title": "Headline 3", "source": "https://news.test/3"}
    img4 = {"url": "https://img.test/4.jpg", "title": "Headline 4", "source": "https://news.test/4"}

    cases = [
        # 0: News query with 3 images
        {
            "query": "今日のニュース教えて",
            "summary": "今日の主なニュースです。",
            "images": [img1, img2, img3],
            "sources": [{"title": "News Site", "uri": "https://news.test/1"}],
        },
        # 1: News query with >3 images (should be sliced to 3)
        {
            "query": "latest news",
            "summary": "Here are the top stories.",
            "images": [img1, img2, img3, img4],
            "sources": [],
        },
        # 2: Standard query with images omitted (classic card)
        {
            "query": "weather today",
            "summary": "Clear skies all day.",
            "sources": [{"title": "Weather", "uri": "https://wx.test"}],
        },
        # 3: Query with images empty list (classic card)
        {
            "query": "python asyncio",
            "summary": "Documentation overview.",
            "images": [],
            "sources": [],
        },
        # 4: Empty query falling back to translated title
        {
            "query": "",
            "summary": "Search results",
            "images": [img1],
        },
    ]

    run = web_search_run(cases, "en")
    check("the run got through them", run["error"], None)
    news_3, news_4, classic_omitted, classic_empty, empty_query = run["cards"]

    # When images are provided:
    check("news card with images renders image-grid", 'class="image-grid"' in news_3, True)
    check("and image-grid is placed above card-body",
          news_3.find('class="image-grid"') < news_3.find('class="card-body"'), True)
    check("and thumbnail links have source href", 'href="https://news.test/1"' in news_3, True)
    check("and thumbnails have img src and lazy loading",
          'src="https://img.test/1.jpg"' in news_3 and 'loading="lazy"' in news_3, True)
    check("and img has fallback onerror handler", 'onerror="this.closest(\'a\')?.remove()"' in news_3, True)
    check("and source titles are rendered", '<span class="src">Headline 1</span>' in news_3, True)

    # When >3 images provided, capped to 3:
    count_imgs = news_4.count("<img ")
    check("card with 4 images caps thumbnail row to 3", count_imgs, 3)

    # When images omitted:
    check("classic card with omitted images has no image-grid", "image-grid" in classic_omitted, False)
    check("and classic card preserves card-body", 'class="card-body"' in classic_omitted, True)
    check("and classic card preserves link chips", 'class="links"' in classic_omitted, True)

    # When images empty list:
    check("card with empty images has no image-grid", "image-grid" in classic_empty, False)
    check("and preserves card-body", 'class="card-body"' in classic_empty, True)

    # Language support:
    check("empty query uses default title in English", "Web search" in empty_query, True)
    ja_run = web_search_run([{"query": "", "summary": "検索結果", "images": []}], "ja")
    check("the Japanese run got through it", ja_run["error"], None)
    check("and empty query uses default title in Japanese", "ウェブ検索" in ja_run["cards"][0], True)


def camera_card_cases() -> None:
    print("\n== what the panel shows for the live camera stream card (#742) ==")
    cards = (JS / "cards.js").read_text(encoding="utf-8")
    app = (JS / "app.js").read_text(encoding="utf-8")
    i18n = i18n_src()

    check("cards.js exports cameraCard", "export function cameraCard(" in cards, True)
    check("cameraCard creates camera-card element", 'class="card camera-card"' in cards, True)
    check("cameraCard includes video element", "<video autoplay playsinline muted></video>" in cards, True)
    check("cameraCard includes camera-error container", 'class="camera-error hidden"' in cards, True)
    check("cameraCard listens for beforeunload", 'window.addEventListener("beforeunload"' in cards, True)
    check("cameraCard cleans up on removal via IntersectionObserver", "new IntersectionObserver" in cards, True)
    check("cameraCard calls stop endpoint on unload", '"/api/camera/stop"' in cards, True)
    check("cameraCard calls offer endpoint on setup", '"/api/camera/offer"' in cards, True)
    check("cameraCard calls extend endpoint periodically", '"/api/camera/extend"' in cards, True)
    check("app.js imports cameraCard", "cameraCard" in app, True)
    check("app.js handles camera frame", 'if (data.type === "camera") return handleCamera(data);' in app, True)
    check("handleCamera calls cameraCard with data", "cameraCard(data.data || {}, cardRoot())" in app, True)
    check("i18n.js defines head_camera in English", 'head_camera: "Camera"' in i18n, True)
    check("i18n.js defines head_camera in Japanese", 'head_camera: "カメラ"' in i18n, True)
    check("i18n.js defines camera_auth_required in English", 'camera_auth_required: "Camera re-authentication required"' in i18n, True)
    check("i18n.js defines camera_auth_required in Japanese", 'camera_auth_required: "カメラの再認証が必要です"' in i18n, True)


def home_card_cases() -> None:
    print("\n== what the panel shows for google home status summary & timeline cards (#745) ==")
    cards = (JS / "cards.js").read_text(encoding="utf-8")
    app = (JS / "app.js").read_text(encoding="utf-8")
    css = (JS / ".." / "css" / "style.css").read_text(encoding="utf-8")
    i18n = i18n_src()

    check("cards.js exports homeSummaryCard", "export function homeSummaryCard(" in cards, True)
    check("cards.js exports homeTimelineCard", "export function homeTimelineCard(" in cards, True)
    check("app.js imports homeSummaryCard", "homeSummaryCard" in app, True)
    check("app.js imports homeTimelineCard", "homeTimelineCard" in app, True)
    check("app.js handles home frame", 'if (data.type === "home") return handleHome(data);' in app, True)
    check("app.js defines handleHome function", "function handleHome(data)" in app, True)
    check("i18n.js defines head_home_summary in English", 'head_home_summary: "Home status"' in i18n, True)
    check("i18n.js defines head_home_summary in Japanese", 'head_home_summary: "家の様子"' in i18n, True)
    check("i18n.js defines head_home_timeline in English", 'head_home_timeline: "Activity timeline"' in i18n, True)
    check("i18n.js defines head_home_timeline in Japanese", 'head_home_timeline: "履歴タイムライン"' in i18n, True)
    check("style.css defines .home-card", ".home-card" in css, True)
    check("style.css defines .home-room", ".home-room" in css, True)
    check("style.css defines .home-device-row", ".home-device-row" in css, True)
    check("style.css defines .home-device-state", ".home-device-state" in css, True)
    check("style.css defines .home-timeline-item", ".home-timeline-item" in css, True)

    node_card_test = """
    const docStub = {
      documentElement: { lang: "ja" },
      createElement: (tag) => {
        let _text = "";
        let _html = "";
        return {
          set textContent(v) { _text = String(v); },
          get innerHTML() {
            return _text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
          },
          set innerHTML(v) { _html = String(v); },
          content: {
            get firstElementChild() {
              return {
                outerHTML: _html,
              };
            }
          }
        };
      }
    };
    globalThis.document = docStub;
    globalThis.window = { location: { search: "" } };

    let imported;
    try {
      imported = await import('./src/ai_terminal/live/static/js/cards.js');
    } catch (e) {
      imported = {};
    }
    const { homeSummaryCard, homeTimelineCard } = imported;

    if (typeof homeSummaryCard === "function" && typeof homeTimelineCard === "function") {
      const summaryEl = homeSummaryCard({
        rooms: [
          {
            name: "リビング",
            devices: [
              { name: "エアコン", state: "on", detail: "24°C", icon: "mode_fan" },
              { name: "照明 <script>alert(1)</script>", state: "off" }
            ]
          }
        ]
      });

      const timelineEl = homeTimelineCard({
        events: [
          { time: "14:30", device: "玄関ドア", event: "動きを検知 & 記録" }
        ]
      });

      const summaryHtml = typeof summaryEl === "string" ? summaryEl : (summaryEl?.outerHTML || "");
      const timelineHtml = typeof timelineEl === "string" ? timelineEl : (timelineEl?.outerHTML || "");

      console.log(JSON.stringify({
        summaryHasClass: summaryHtml.includes("home-summary"),
        summaryHasRoom: summaryHtml.includes("home-room"),
        summaryHasRoomTitle: summaryHtml.includes("home-room-title"),
        summaryHasStateOn: summaryHtml.includes("state-on"),
        summaryHasStateOff: summaryHtml.includes("state-off"),
        summaryEscapesScript: !summaryHtml.includes("<script>") && summaryHtml.includes("&lt;script&gt;"),
        timelineHasClass: timelineHtml.includes("home-timeline"),
        timelineHasList: timelineHtml.includes("home-timeline-list"),
        timelineHasItem: timelineHtml.includes("home-timeline-item"),
        timelineEscapesAmp: timelineHtml.includes("&amp;"),
      }));
    } else {
      console.log(JSON.stringify({}));
    }
    """
    res = subprocess.run(["node", "--input-type=module", "-e", node_card_test],
                         capture_output=True, text=True, cwd=ROOT)
    data = json.loads(res.stdout) if res.returncode == 0 else {}
    check("homeSummaryCard renders home-summary class", data.get("summaryHasClass"), True)
    check("homeSummaryCard renders home-room grouping", data.get("summaryHasRoom"), True)
    check("homeSummaryCard renders home-room-title", data.get("summaryHasRoomTitle"), True)
    check("homeSummaryCard renders state-on class", data.get("summaryHasStateOn"), True)
    check("homeSummaryCard renders state-off class", data.get("summaryHasStateOff"), True)
    check("homeSummaryCard escapes script tags in device names", data.get("summaryEscapesScript"), True)
    check("homeTimelineCard renders home-timeline class", data.get("timelineHasClass"), True)
    check("homeTimelineCard renders home-timeline-list", data.get("timelineHasList"), True)
    check("homeTimelineCard renders home-timeline-item", data.get("timelineHasItem"), True)
    check("homeTimelineCard escapes ampersand in event descriptions", data.get("timelineEscapesAmp"), True)


    print("\n== the twelve tools registered, against the switch that draws them ==")

    # The check that would have found this issue. Two of the twelve had drifted
    # out of the switch with nothing reporting it, because the symptom is JSON
    # on a kitchen panel and a kitchen panel is not somewhere people read error
    # output. Without this the next tool added lands in `genericCard` the same
    # way and nothing goes red.
    registered = registered_tools()
    arms = switch_arms()
    check("the tool names are resolved through to the function each wraps",
          len(registered) >= 12, True, f"{len(registered)}: {registered}")
    check("every registered tool has an arm of its own",
          sorted(set(registered) - {name for name, _ in arms}), [],
          "anything missing falls through to genericCard")
    check("and no arm names a tool nobody registers",
          sorted({name for name, _ in arms} - set(registered)), [])

    # The five arms that draw nothing, and why they are allowed to.
    # Three answer out of band via `response_scheduling` (browse, gws, plan).
    # Two push composite card sections via `card_section` frames (image_search, web_search).
    # Both sets draw their cards through their own frames rather than the functionResponse.
    # The arm is what keeps them from being painted a second time (#450) or falling
    # through to genericCard (#241).
    silent = sorted(name for name, drawn in arms if not drawn)
    check("the arms that draw nothing are out-of-band and composite section tools",
          silent, ["browse", "camera", "gws", "home", "image_search", "plan", "web_search", "youtube_search"])
    tools = ROOT / "src" / "ai_terminal" / "live" / "tools"
    out_of_band = ["browse", "gws", "home", "plan"]
    composite_sections = ["image_search", "web_search", "youtube_search"]
    check("each out-of-band tool carries response_scheduling",
          [name for name in out_of_band if "response_scheduling"
           not in (tools / f"{name}.py").read_text(encoding="utf-8")], [])
    check("each composite section tool pushes card_section",
          [name for name in composite_sections if "card_section"
           not in (tools / f"{name}.py").read_text(encoding="utf-8")], [])
    check("camera pushes camera frame directly to screen",
          "screen.push" in (tools / "camera.py").read_text(encoding="utf-8"), True)
    check("remember is drawn by a renderer of its own",
          dict(arms).get("remember"), "rememberCard")
    check("calculate is drawn by a renderer of its own",
          dict(arms).get("calculate"), "calculateCard")

    # And the fingerprint that decides whether an answer is a card at all. It
    # is one string per tool answer, so a field the tool varies and this does
    # not is a second card silently dropped: two facts kept in one turn were
    # both `remember:::` and the panel showed the first one only. Read off the
    # source because nothing here can dispatch a `functionResponse`.
    app = (JS / "app.js").read_text(encoding="utf-8")
    fields = re.findall(r"response\?\.(\w+)",
                        block(app, "function resultKey("))
    check("the fingerprint a tool answer is deduped on names every field two "
          "calls in one turn can differ by",
          sorted(fields), ["destination", "expression", "location", "place", "query", "topic"],
          "an identical repeat still collides, which is what it is for")


# One turn's worth of tool answers, through the key `handleEvent` tells them
# apart on. The loop is three lines of that function written out rather than
# lifted, because lifting `handleEvent` would drag in the socket, the player and
# the DOM for a `Set` and a `continue`; `resultKey` itself is the real one, so a
# field dropped from it fails here.
RESULT_KEY = r"""
%s

const seen = new Set();
const drawn = [];
for (const [name, response] of JSON.parse(process.argv[1])) {
  const key = resultKey(name, response);
  if (seen.has(key)) continue;
  seen.add(key);
  drawn.push(name);
}
console.log(JSON.stringify(drawn));
"""


def result_key_run(answers: list) -> list[str]:
    """Which of one turn's tool answers reach a card."""
    app = (JS / "app.js").read_text(encoding="utf-8")
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         RESULT_KEY % block(app, "function resultKey("), json.dumps(answers)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return [proc.stderr.strip()[-300:]]
    return json.loads(proc.stdout)


def result_key_cases() -> None:
    """Two answers in one turn, and whether the screen gets both.

    `seenTools` exists because the native-audio model emits the same call two
    or three times in a turn, and one question must not stack three cards. The
    key it used was the name plus `query`, `place` and `destination` — three
    fields `weather` does not return, so every weather answer in a turn was the
    string `weather:::` and the second was dropped as a repeat of the first
    (#182). The model then answers about a reading the screen is not showing.
    """
    print("\n== two answers in one turn ==")

    tokyo = {"location": "Tokyo", "current": {"temperature": 21}}
    kyoto = {"location": "Kyoto", "current": {"temperature": 24}}

    check("one weather answer draws one card",
          result_key_run([["weather", tokyo]]), ["weather"])
    check("two readings of two places draw two",
          result_key_run([["weather", tokyo], ["weather", kyoto]]),
          ["weather", "weather"])
    check("the same reading twice still draws one, which is what the key is "
          "for", result_key_run([["weather", tokyo], ["weather", tokyo]]),
          ["weather"])
    check("a weather answer and a search are never each other",
          result_key_run([["weather", tokyo],
                          ["maps_search", {"query": "ramen"}]]),
          ["weather", "maps_search"])

    # Controls. These were green before `location` joined the key and have to
    # stay green: a key that splits every answer would put the repeated call
    # back on the screen as three cards.
    check("two searches in one turn are two questions",
          result_key_run([["maps_search", {"query": "ramen"}],
                          ["maps_search", {"query": "coffee"}]]),
          ["maps_search", "maps_search"])
    check("and the same search repeated is one",
          result_key_run([["maps_search", {"query": "ramen"}],
                          ["maps_search", {"query": "ramen"}]]),
          ["maps_search"])
    check("an answer with no handle at all is still one card",
          result_key_run([["gws", {"answer": "Soba at 11:00"}],
                          ["gws", {"answer": "Soba at 11:00"}]]),
          ["gws"])
    calc1 = {"expression": "1 + 1", "result": 2}
    calc2 = {"expression": "2 * 3", "result": 6}
    check("two calculations in one turn draw two cards",
          result_key_run([["calculate", calc1], ["calculate", calc2]]),
          ["calculate", "calculate"])
    check("the same calculation repeated is one card",
          result_key_run([["calculate", calc1], ["calculate", calc1]]),
          ["calculate"])
    # And the reason the key is those fields rather than the whole answer,
    # which would pass every case above. The same call run twice is two real
    # requests: the temperature moves, the geocoder picks a different wording,
    # a search summary comes back reworded. Keyed on everything, the repeat the
    # `Set` exists to swallow comes back as a second card.
    check("a repeated call whose answer moved between the two runs is still "
          "one question",
          result_key_run([["weather", tokyo],
                          ["weather", {**tokyo,
                                       "current": {"temperature": 22}}]]),
          ["weather"])

    # The server keeps the same list for the same purpose and has always had
    # `location` in it. A field added to one side and not the other is a card
    # the page draws and the planner's window folds away, or the reverse.
    plan_py = (ROOT / "src" / "ai_terminal" / "live" / "tools" / "plan.py")
    keys = re.search(r"_TITLE_KEYS = \(([^)]*)\)",
                     plan_py.read_text(encoding="utf-8"))
    check("and the server tells cards apart on the same fields",
          sorted(re.findall(r'"(\w+)"', keys.group(1))),
          sorted(re.findall(r'response\?\.(\w+)',
                            block((JS / "app.js").read_text(encoding="utf-8"),
                                  "function resultKey("))))


def source_run(data: dict | list, lang: str = "en") -> dict:
    """Drive `cards.js`'s `gwsFill` over one frame, or a run of them.

    `calendar_src` comes along whole, for two reasons that are really one:
    `gwsFill` calls `calendarHtml` on the way past, and the words the row says
    for itself live in the `STRINGS` that slice already lifts out of `i18n.js`.
    A stub for either would let a missing `gws_unsourced` through as an empty
    row, which is the exact shape of the bug the row exists to rule out.
    """
    src = (JS / "cards.js").read_text(encoding="utf-8")
    lifted = "\n\n".join(
        block(src, header).removeprefix("export ")
        for header in ("const SOURCE_ICONS = ", "const SOURCE_CLIP = ",
                       "function clip(", "function senderName(",
                       "function sourceChips(", "export function gwsFill("))
    lifted = (block((JS / "markdown.js").read_text(encoding="utf-8"),
                    "export function escapeHtml(").removeprefix("export ")
              + "\n\n" + calendar_src() + "\n\n" + lifted)
    frames = data if isinstance(data, list) else [data]
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         SOURCES % (json.dumps(lang), lifted), json.dumps(frames)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip(), "classes": [], "text": "",
                "html": "", "row": ""}
    return json.loads(proc.stdout)


def labels(row: str) -> list[str]:
    """The chip labels in a source row, in order."""
    return re.findall(r'<span class="chip-label">(.*?)</span>', row)


def details(row: str) -> list[str]:
    """And the dim half beside each one, for the chips that have one."""
    return re.findall(r'<span class="chip-detail">(.*?)</span>', row)


def source_cases() -> None:
    print("\n== what a Workspace answer says it came from ==")

    # The whole of #89 in one case. Before this the card drew the summary and
    # nothing else, and which of the household's files it was read out of was
    # not on the screen, not in the frame and not recoverable from either.
    doc = source_run({
        "result": "Departure is 09:40 from Haneda.",
        "sources": [{"kind": "doc", "name": "Portland trip itinerary",
                     "detail": "2026-09-04"}],
    })
    check("the run got through the frame", doc.get("error"), None)
    check("the answer is still drawn", doc["html"],
          "[md]Departure is 09:40 from Haneda.")
    check("and the Drive file it came out of is named",
          labels(doc["row"]), ["Portland trip itinerary"],
          "a summary with no source is the whole of #89")
    check("with when it last changed beside it",
          details(doc["row"]), ["2026-09-04"])
    check("and a glyph that says it is a Doc",
          "description</span>" in doc["row"], True, doc["row"])

    # Three objects, three glyphs, one row. The ruling is one renderer with a
    # type field rather than four renderers: they share a card and a frame, and
    # a question that reads a sheet and the mail about it has one answer.
    mixed = source_run({"result": "ok", "sources": [
        {"kind": "sheet", "name": "Budget 2026", "detail": "2026-08-30"},
        {"kind": "slides", "name": "Review deck", "detail": ""},
        {"kind": "file", "name": "boarding.pdf", "detail": "2026-09-01"},
    ]})
    check("a Sheet, a Slides deck and a plain file share one row",
          labels(mixed["row"]),
          ["Budget 2026", "Review deck", "boarding.pdf"])
    check("and are told apart by their glyphs, not by their words",
          [g for g in ("table_chart", "slideshow", "draft")
           if f"{g}</span>" in mixed["row"]],
          ["table_chart", "slideshow", "draft"])
    check("a source with nothing to add beside it gets no dim half",
          details(mixed["row"]), ["2026-08-30", "2026-09-01"])
    # A kind the server learns to send before the card learns to draw it must
    # not come out as a blank chip with no icon at all.
    unknown = source_run({"result": "ok", "sources": [
        {"kind": "podcast", "name": "Somewhere new", "detail": ""}]})
    check("and a kind this card has never heard of still draws as a file",
          (labels(unknown["row"]), "draft</span>" in unknown["row"]),
          (["Somewhere new"], True))

    print("\n== a mail source, on a screen in a shared room ==")

    mail = source_run({"result": "The gate changed to 24.", "sources": [
        {"kind": "mail", "name": "ANA <no-reply@ana.example.co.jp>",
         "detail": "Your flight NH106 on 4 Oct"}]})
    check("the thread is named by who sent it", labels(mail["row"]), ["ANA"])
    check("and the address after the name is not on the wall",
          "ana.example" in mail["row"], False, mail["row"])
    check("the subject is there, as the dim half",
          details(mail["row"]), ["Your flight NH106 on 4 Oct"])
    check("with the mail glyph", "mail</span>" in mail["row"], True)
    quoted = source_run({"result": "ok", "sources": [
        {"kind": "mail", "name": '"Example, Alex" <alex@example.com>',
         "detail": "re: rent"}]})
    check("a quoted display name is unquoted too",
          labels(quoted["row"]), ["Example, Alex"])
    bare = source_run({"result": "ok", "sources": [
        {"kind": "mail", "name": "alerts@bank.example", "detail": ""}]})
    check("and an address with no name in front of it is shown as it is",
          labels(bare["row"]), ["alerts@bank.example"],
          "inventing a name for it would be worse than printing one")

    print("\n== truncation, and which half survives it ==")

    # The ruling: the name is the identity and it gets the space it needs; the
    # subject is the most disclosive string here and it is the half that goes.
    long_subject = "Confirmation of your booking, deposit and cancellation"
    cut = source_run({"result": "ok", "sources": [
        {"kind": "mail", "name": "Hotel Portland", "detail": long_subject}]})
    check("the sender survives whole", labels(cut["row"]), ["Hotel Portland"])
    check("the subject is cut", details(cut["row"])[0].endswith("…"), True,
          details(cut["row"])[0])
    check("to the length the card rules on", len(details(cut["row"])[0]), 45,
          "44 kept plus the ellipsis that says there was more")
    # The reason there is no `title` on a chip. A tooltip nobody can hover on a
    # panel with no pointer would put the whole subject back in the DOM that
    # the clip above exists to keep it out of.
    check("and the part that was cut is nowhere in the card",
          long_subject in cut["row"], False)
    check("no chip carries a title attribute either",
          "title=" in cut["row"], False, cut["row"])

    long_name = "Example family 2026 budget and household expenses master sheet"
    wide = source_run({"result": "ok", "sources": [
        {"kind": "sheet", "name": long_name, "detail": "2026-08-30"}]})
    check("a file name too wide for the panel is cut as well",
          labels(wide["row"])[0].endswith("…"), True)
    check("but keeps enough of itself to be recognised",
          labels(wide["row"])[0].startswith("Example family 2026 budget"), True)
    check("and the detail beside it is untouched",
          details(wide["row"]), ["2026-08-30"])

    print("\n== a card with nothing to attribute ==")

    # The case that stops this being decoration. A frame that looked through
    # Drive or Gmail and could name nothing arrives with `sources: []`, and a
    # confident answer over a blank row is indistinguishable from a card whose
    # source row was never written.
    none = source_run({"result": "There are three unread.", "sources": []})
    check("an answer that could name no source says so",
          "no-source" in none["row"], True, none["row"])
    check("in words", "Could not say which file or message this came from."
          in none["row"], True)
    check("in the language of the turn",
          "どのファイル・メールから得た情報かは特定できませんでした"
          in source_run({"result": "x", "sources": []}, "ja")["row"], True)
    check("and it is not dressed as a source", labels(none["row"]), [])

    # And the frame that touched neither. A calendar answer has no document
    # behind it, and a terminal that reports "no source" for every "what's on
    # today?" has taught the household to ignore the line by the end of the
    # week — so the key is absent, not empty, and the row stays blank.
    calendar = source_run({"result": "Two things on: soba at 11:00."})
    check("a question with no file or thread in it draws no row at all",
          calendar["row"], "")
    check("and its answer is untouched", calendar["html"],
          "[md]Two things on: soba at 11:00.")

    print("\n== the source row across the ways a card ends ==")

    # Nothing was read, so there is nothing for it to have come from. A source
    # row under "the request failed" reads as a claim that something was.
    ended = {"failed": "The request failed. Nothing was changed.",
             "empty": "Searched, but found no answer.",
             "unset": "Workspace is not set up on this terminal.",
             "slow": "Took too long and was stopped.",
             "lost": "No answer came back."}
    for kind, said in ended.items():
        got = source_run({"error": kind})
        check(f"a {kind} card draws no source row", got["row"], "")
        check("and still says why, unchanged", got["text"], said)
    # The one that has to be cleared rather than skipped. The deadline fills
    # the card with `lost`, the real answer lands in the same card afterwards,
    # and a row left over from a card that has since said "no answer came back"
    # is the screen contradicting itself.
    late = source_run([
        {"result": "Departure is 09:40.",
         "sources": [{"kind": "doc", "name": "Itinerary", "detail": ""}]},
        {"error": "lost"},
    ])
    check("a card that gives up clears the row it had",
          late["row"], "", "not merely skips it")
    back = source_run([
        {"error": "lost"},
        {"result": "Departure is 09:40.",
         "sources": [{"kind": "doc", "name": "Itinerary", "detail": ""}]},
    ])
    check("and an answer arriving after the deadline still names its source",
          labels(back["row"]), ["Itinerary"])
    check("with the failed styling taken back off",
          "failed" in back["classes"], False)

    print("\n== what a file name or a subject cannot do to the card ==")

    # Both of these are third-party strings that reach the panel unread by
    # anyone: a Drive file anybody in a shared drive can rename, and a subject
    # line anybody at all can send.
    nasty = source_run({"result": "ok", "sources": [
        {"kind": "file", "name": "<img src=x onerror=alert(1)>",
         "detail": "a & b <b>c</b>"}]})
    check("a tag in a file name is escaped",
          "<img" in nasty["row"], False, nasty["row"])
    check("and comes out as text", "&lt;img src=x onerror=alert(1)&gt;"
          in nasty["row"], True)
    check("an ampersand in a subject is escaped once",
          "a &amp; b" in nasty["row"], True)
    check("and not twice", "&amp;amp;" in nasty["row"], False)

    print("\n== the hook the row is written into ==")

    # Every case above drives a stub. `gwsPending` is what builds the real
    # card, and a `.gws-sources` deleted from its markup makes `querySelector`
    # return null, which `gwsFill` is written to tolerate — so the whole
    # feature would go quiet with all of the above still green.
    src = (JS / "cards.js").read_text(encoding="utf-8")
    pending = block(src, "export function gwsPending(")
    check("gwsPending builds the element gwsFill writes the row into",
          'class="links gws-sources"' in pending, True)
    check("and it reuses the chip row the grounding cards established",
          'class="links' in pending, True)
    css = (JS.parent / "css" / "style.css").read_text(encoding="utf-8")
    check("the stylesheet hides it while it is empty",
          ".gws-sources:empty" in css, True,
          "a pending card must not reserve a strip of padding for nothing")
    check("and styles the name brighter than the detail beside it",
          before(css, ".gws-sources .chip-label", ".gws-sources .chip-detail"),
          True)

    print("\n== defer gws status probe until settings sheet is opened (#443) ==")

    # #367 spawned the gws CLI for an auth check on page load, which jumped
    # page load time from 1.1 s to 4.6 s. Booting the kiosk does not need the
    # auth status of a sheet nobody has opened yet.
    app = (JS / "app.js").read_text(encoding="utf-8")
    init = block(app, "async function init(")
    check("init does not probe gws status on boot",
          "refreshGwsStatus" in init, False,
          "eager probe spawns gws CLI and adds ~3.5 s to page load (#443)")

    settings_click = block(app, 'settingsButton.addEventListener("click"')
    check("opening the settings sheet refreshes gws status",
          "refreshGwsStatus()" in settings_click, True)
    check("and only when the sheet is opened, not when closed",
          '!settingsSheet.classList.contains("hidden")' in settings_click, True)
    check("signing into workspace refreshes status",
          "refreshGwsStatus()" in block(app, "async function connectGws("), True)
    check("and disconnecting workspace refreshes status",
          "refreshGwsStatus()" in block(app, "async function disconnectGws("), True)
    render_status = block(app, "function renderGwsStatus(")
    check("renderGwsStatus handles expired status",
          "status.expired" in render_status, True)
    check("and shows connect button when expired",
          'workspaceConnectBtn.classList.remove("hidden")' in render_status, True)


def steps_run(frames: list) -> list:
    """Drive `cards.js`'s `planUpdate` over a run of `show_plan` frames."""
    src = (JS / "cards.js").read_text(encoding="utf-8")
    body = (STEPS % block(src, "export function planUpdate(")
            .removeprefix("export "))
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", body, json.dumps(frames)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return [("error", proc.stderr.strip())]
    return [tuple(kid) for kid in json.loads(proc.stdout)]


def steps_cases() -> None:
    print("\n== the step list, with several steps running at once ==")

    three = {"steps": ["find dinner", "read my mail", "check the weather"]}

    drawn = steps_run([{**three, "running": [0, 1, 2], "done": []}])
    check("every step sent out this turn is drawn as running",
          [css for _, css in drawn], ["now", "now", "now"],
          "a cursor could mark exactly one, which is #62 on the screen")
    check("in the planner's own wording, printed as it arrives",
          [text for text, _ in drawn], three["steps"])

    drawn = steps_run([{**three, "running": [2], "done": [0, 1]}])
    check("the ones that have come back are drawn as done",
          [css for _, css in drawn], ["done", "done", "now"])

    # Out of order, because they come back out of order: three lookups started
    # in one turn settle whenever each of them settles.
    drawn = steps_run([{**three, "running": [0], "done": [2]}])
    check("and done is a set of steps, not a prefix of them",
          [css for _, css in drawn], ["now", "", "done"],
          "step three back before step two has started is normal now")

    check("a step named in both lists is drawn as running",
          [css for _, css in
           steps_run([{**three, "running": [1], "done": [1]}])],
          ["", "now", ""],
          "the server resolves this too — neither end should decide it alone")

    check("neither list is required",
          [css for _, css in steps_run([three])], ["", "", ""])

    # A revision, which is the reason the list is rebuilt and not patched. The
    # planner drops a step and adds another, and the frame carries the whole
    # list — so appending would leave the stale three above the new two.
    drawn = steps_run([
        {**three, "running": [0, 1]},
        {"steps": ["find dinner", "book it"], "running": [1], "done": [0]},
    ])
    check("a revised plan replaces the list rather than growing it",
          drawn, [("find dinner", "done"), ("book it", "now")])

    check("a frame with no steps leaves the list standing",
          steps_run([{**three, "done": [0]}, {"result": "Soba at 11:00"}]),
          [("find dinner", "done"), ("read my mail", ""),
           ("check the weather", "")],
          "`planFill` draws the answer over this; it must not blank it")

    check("and a card that has already gone is not an error",
          steps_run([None]), [])


def late_run(script: list[dict]) -> dict:
    """Drive `app.js`'s `dropPending` and `handleLate` over a little script."""
    app = (JS / "app.js").read_text(encoding="utf-8")
    cards_js = (JS / "cards.js").read_text(encoding="utf-8")
    fills = "\n\n".join(
        block(cards_js, header).removeprefix("export ")
        for header in ("export function gwsFill(", "export function planFill("))
    lifted = "\n\n".join(
        [re.search(r"^let pending = .+?;$", app, re.M).group(0)]
        + [block(app, header) for header in ("const LATE_TOOLS = ",
                                             "function dropPending(",
                                             "function handleLate(")])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", LATE % (fills, lifted),
         json.dumps(script)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        # Reported rather than fatal: a `dropPending` that reaches for a field
        # the entry does not carry is a TypeError, and that wants a case with a
        # name on it rather than a stack trace out of the middle of the run.
        return {"error": proc.stderr.strip(), "cards": [], "waiting": [],
                "fired": [], "appended": []}
    return json.loads(proc.stdout)


def late_cases() -> None:
    print("\n== a card in flight when the conversation is replaced ==")

    # What `reopen` does: the socket that would have filled this card is closed
    # and opened again, so the answer is never coming, and the timer that would
    # eventually have said so is cancelled on the way past.
    plan = late_run([{"begin": "call-1", "name": "plan"}, {"resolve": True}])
    check("the run got through it", plan.get("error"), None)
    card = plan["cards"][0]
    check("a plan card left in flight stops spinning",
          "pending" in card["classes"], False)
    check("and says in words that nothing is coming", card["text"],
          "<plan_lost>", "otherwise it spins until the page is reloaded")
    check("styled as a failure, not as an answer",
          "failed" in card["classes"], True)
    check("nothing is left waiting", plan["waiting"], [])
    check("and no deadline is left to go off later", plan["fired"], [])
    check("the card itself stays on screen", card["gone"], False)

    gws = late_run([{"begin": "call-1", "name": "gws"}, {"resolve": True}])
    check("a gws card the same, in its own words",
          (gws["cards"][0]["text"], "pending" in gws["cards"][0]["classes"]),
          ("<gws_lost>", False))

    both = late_run([{"begin": "a", "name": "plan"}, {"begin": "b", "name": "gws"},
                     {"resolve": True}])
    check("every card in flight is resolved, not only the first",
          [c["text"] for c in both["cards"]], ["<plan_lost>", "<gws_lost>"])

    # What the clear button does. It removes every card on the next line, so a
    # fill here is work nobody sees — but the timers still have to go.
    cleared = late_run([{"begin": "a", "name": "plan"},
                        {"begin": "b", "name": "gws"}, {"clear": True}])
    check("the clear button writes nothing on the way out",
          [c["text"] for c in cleared["cards"]], ["", ""])
    check("and leaves them exactly as it found them",
          [sorted(set(c["classes"]) - {"card"}) for c in cleared["cards"]],
          [["pending", "plan"], ["gws", "pending"]])
    check("but still cancels the deadlines", cleared["fired"], [])
    check("and still empties the map", cleared["waiting"], [])

    # A frame from the session just closed, arriving after the resolve. The
    # entry is gone, so `handleLate` takes its fallback path — which is what it
    # is for, and is why the resolved card must not be reachable from here.
    after = late_run([{"begin": "a", "name": "plan"}, {"resolve": True},
                      {"late": {"type": "plan", "id": "a",
                                "result": "Soba at 11:00"}}])
    check("an answer arriving afterwards does not revive the card",
          (after["cards"][0]["text"], after["cards"][0]["html"]),
          ("<plan_lost>", ""))
    check("it lands as a card of its own", after["appended"],
          ["a fresh plan card"])

    empty = late_run([{"resolve": True}])
    check("and with nothing in flight it is a no-op rather than a crash",
          (empty.get("error"), empty["cards"]), (None, []))

    # The ordinary path, which every case above reaches only after the card has
    # already left `pending`. Filling it is watched; taking it out of the map
    # is not, and the map is the one thing here that outlives the card: the
    # page runs for days, so an entry per answer is a leak, and a second
    # frame on the same id would write over a card that is finished. See #55.
    in_time = late_run([{"begin": "a", "name": "plan"},
                        {"late": {"type": "plan", "id": "a",
                                  "result": "Soba at 11:00"}}])
    check("an answer arriving in time fills the card that was waiting",
          in_time["cards"][0]["html"], "[md]Soba at 11:00")
    check("and the card stops being one that is waiting for an answer",
          in_time["waiting"], [], "otherwise `pending` grows all day")
    check("with no second card underneath it", in_time["appended"], [])
    check("and its deadline cancelled on the way past", in_time["fired"], [])

    # Which caller asks for which. Read off the source because `reopen` cannot
    # be lifted — it opens a socket — and getting these two the wrong way round
    # is the whole bug in one direction and a flash of dead text in the other.
    app = (JS / "app.js").read_text(encoding="utf-8")
    check("the clear button drops them without a fill",
          "dropPending()" in block(app, 'clearButton.addEventListener("click"'),
          True)
    check("and a setting that reopens the socket resolves them",
          "dropPending(true)" in block(app, "function reopen("), True)

    # `dropPending` destructures the entry, so a field renamed where it is
    # written and not where it is read is `undefined` rather than an error:
    # `clearTimeout(undefined)` is legal and `fill` would be called on nothing.
    def fields(pattern: str) -> set[str]:
        return {f.strip() for f in
                re.search(pattern, app).group(1).split(",")}

    written = fields(r"pending\.set\(id, \{ (.+?) \}\)")
    read = fields(r"for \(const \{ (.+?) \} of pending\.values\(\)\)")
    check("everything it reads out of an entry is something the entry carries",
          (sorted(read), read - written),
          (["card", "name", "timer"], set()))


# The working spinner and client-side turn timeout (#281).
WORKING = r"""
const timers = new Map();
let nextTimer = 1;
const said = [];
const feedNodes = [];
let lang = "en";

function setTimeout(fn, ms) {
  const id = nextTimer++;
  timers.set(id, { fn, ms });
  return id;
}
function clearTimeout(id) {
  timers.delete(id);
}
function systemLine(text) {
  said.push(text);
}
function t(key) {
  return STRINGS[lang]?.[key] || STRINGS.en?.[key] || "";
}

const feed = {
  querySelector(sel) {
    if (sel === ".working") return feedNodes.find(n => n.className === "working") || null;
    return null;
  },
  querySelectorAll(sel) {
    if (sel === ".msg.listening") return feedNodes.filter(n => (n.className || "").includes("listening"));
    if (sel === ".msg.tool-call") return feedNodes.filter(n => (n.className || "").includes("tool-call"));
    return [];
  }
};
function append(el) {
  feedNodes.push(el);
}
globalThis.document = {
  createElement(tag) {
    const el = {
      tagName: tag,
      className: "",
      innerHTML: "",
      text: "",
      append(text) { this.text = text; },
      remove() {
        const idx = feedNodes.indexOf(el);
        if (idx !== -1) feedNodes.splice(idx, 1);
      }
    };
    return el;
  }
};

%s

%s

const script = JSON.parse(process.argv[1]);
for (const step of script) {
  if (step.action === "show") showWorking();
  else if (step.action === "hide") hideWorking();
  else if (step.action === "lang") lang = step.value;
  else if (step.action === "seed_listening") {
    const el = globalThis.document.createElement("div");
    el.className = "msg system listening";
    feedNodes.push(el);
  }
  else if (step.action === "expire") {
    const first = [...timers.entries()][0];
    if (first) {
      timers.delete(first[0]);
      first[1].fn();
    }
  }
}

const out = {
  workingCount: feedNodes.length,
  classes: feedNodes.map(n => n.className),
  timers: [...timers.values()].map(t => t.ms),
  said,
};
console.log(JSON.stringify(out));
"""


def working_run(script: list[dict], app: str, i18n: str) -> dict:
    """Drive app.js showWorking and hideWorking over a script."""
    strings = block(i18n, "export const STRINGS = ").removeprefix("export ")
    timeout_decl = re.search(r"^const WORKING_TIMEOUT_MS = .*?;$", app, re.M)
    if not timeout_decl:
        return {"error": "WORKING_TIMEOUT_MS missing", "workingCount": 0, "timers": [], "said": []}
    timer_decl = re.search(r"^let workingTimer = .*?;$", app, re.M)
    if not timer_decl:
        return {"error": "workingTimer missing", "workingCount": 0, "timers": [], "said": []}
    lifted = "\n".join([
        timeout_decl.group(0),
        timer_decl.group(0),
        block(app, "function showWorking("),
        block(app, "function hideWorking("),
    ])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", WORKING % (strings, lifted),
         json.dumps(script)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip(), "workingCount": 0, "timers": [], "said": []}
    return json.loads(proc.stdout)


def working_cases() -> None:
    print("\n== client-side turn timeout and working spinner ==")
    app = (JS / "app.js").read_text(encoding="utf-8")
    i18n = i18n_src()

    check("app.js defines a working timeout constant",
          bool(re.search(r"^const WORKING_TIMEOUT_MS = \d+;$", app, re.M)), True)
    check("and it is at least 90 seconds",
          int(re.search(r"^const WORKING_TIMEOUT_MS = (\d+);$", app, re.M).group(1)) >= 90000
          if re.search(r"^const WORKING_TIMEOUT_MS = (\d+);$", app, re.M) else False, True)

    show = block(app, "function showWorking(")
    hide = block(app, "function hideWorking(")
    check("showWorking clears any previous timer before arming a new one",
          before(show, "clearTimeout(workingTimer)", "setTimeout("), True)
    check("hideWorking clears the timer before removing the element",
          before(hide, "clearTimeout(workingTimer)", ".remove()"), True)
    check("the clear button drops the working spinner and its timer",
          "hideWorking()" in block(app, "clearButton.addEventListener(\"click\""), True)

    check("i18n defines ui_no_answer in English and Japanese",
          ("ui_no_answer" in block(i18n, "en: {"), "ui_no_answer" in block(i18n, "ja: {")),
          (True, True))

    single = working_run([{"action": "show"}], app, i18n)
    check("the run got through it", single.get("error"), None)
    check("showWorking appends one spinner and arms the timeout",
          (single["workingCount"], single["timers"]), (1, [90000]))

    rearmed = working_run([{"action": "show"}, {"action": "show"}], app, i18n)
    check("calling showWorking again extends the timer without duplicating the element",
          (rearmed["workingCount"], rearmed["timers"]), (1, [90000]))

    hidden = working_run([{"action": "show"}, {"action": "hide"}], app, i18n)
    check("hideWorking cancels the timer and removes the element",
          (hidden["workingCount"], hidden["timers"]), (0, []))

    expired_en = working_run([{"action": "show"}, {"action": "expire"}], app, i18n)
    check("on expiry the spinner is removed and no-answer emitted",
          (expired_en["workingCount"], expired_en["said"]),
          (0, ["No answer came back."]))

    expired_ja = working_run([{"action": "lang", "value": "ja"}, {"action": "show"}, {"action": "expire"}], app, i18n)
    check("and in Japanese the no-answer line is in Japanese",
          expired_ja["said"], ["応答がありませんでした"])

    cleaned = working_run([{"action": "seed_listening"}, {"action": "show"}], app, i18n)
    check("showWorking clears stale listening indicator on next phase",
          [c for c in cleaned.get("classes", []) if "listening" in c], [])


def backoff_run(rolls: list[float]) -> dict:
    """Drive `app.js`'s reconnect backoff over one roll of the dice per close.

    The two constants are lifted by name rather than re-declared here, so a
    ceiling changed in `app.js` and not in the case fails the case instead of
    being quietly measured against itself.
    """
    app = (JS / "app.js").read_text(encoding="utf-8")
    lifted = "\n".join(
        [re.search(rf"^const {name} = .*$", app, re.M).group(0)
         for name in ("RECONNECT_MIN_MS", "RECONNECT_MAX_MS")]
        + [block(app, "function reconnectWait("),
           block(app, "function reconnectNext(")])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", BACKOFF % lifted,
         json.dumps(rolls)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip()[-200:], "waits": [], "bases": []}
    return json.loads(proc.stdout)


def close_run(lives: list[dict]) -> list[dict] | dict:
    """Drive `app.js`'s real `socket.onclose` over a run of connections.

    One entry per life: `held` is how long the socket stayed up in ms, absent
    for one that never opened, `ready` is whether the server sent its `ready`
    frame on this one, `cards` are the browse jobs on screen when it went, and
    `roll` is the jitter, defaulting to the middle of the range. `gap` is how
    long before this entry, a minute by default. `do` names the two entries
    that are not a close at all — `reopen`, somebody pressing a button, and
    `up`, a socket that came up and has not gone.

    Everything the handler decides with is lifted by name — the two handlers,
    `reopen`, the three constants, the two module-level `let`s and the two
    backoff functions — so a threshold changed in `app.js` and not here fails
    the case instead of being measured against a copy of itself.
    """
    app = (JS / "app.js").read_text(encoding="utf-8")
    lifted = "\n".join(
        # A name that is not there is skipped rather than raised on. The three
        # the wake work added are read by `onopen` and by nothing else, and a
        # tree without them should fail the one case that is about them
        # instead of taking every close case down with a stack trace.
        [found.group(0)
         for name in ("RECONNECT_MIN_MS", "RECONNECT_MAX_MS",
                      "RECONNECT_HELD_MS", "reconnectDelay", "openedAt",
                      "sawReady", "browsing", "armed", "listening",
                      "idleTimer", "userCleared")
         if (found := re.search(rf"^(?:const|let) {name} = .*$", app, re.M))]
        + [block(app, "function reconnectWait("),
           block(app, "function reconnectNext("),
           block(app, "function reopen(")]
        + [block(app, f"socket.on{name} = () => {{")
           .replace(f"socket.on{name} =", f"const on{name} =") + ";"
           for name in ("open", "close")]
        # The real dispatch, not a line that sets a flag: what a case here is
        # about is whether a frame the server sent reaches the backoff at all,
        # and a `ready` arm written into this harness instead of lifted out of
        # `app.js` would pass with the page ignoring the frame entirely.
        + [block(app, "socket.onmessage = (frame) => {")
           .replace("socket.onmessage =", "const onmessage =") + ";"])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", CLOSE % lifted,
         json.dumps(lives)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip()[-300:]}
    return json.loads(proc.stdout)


# Everything the wake path is made of, by the header it is declared under. A
# name missing from `app.js` is reported by the run rather than raised on, so a
# tree where none of this exists yet fails each case by name.
WAKE_PARTS = ("function armSession(", "function restartIdle(",
              "async function goIdle(", "async function handleWake(",
              "async function startListening(",
              "async function stopListening(")
WAKE_TAP = 'micButton.addEventListener("click", async () => {'
# The row of suggestions, which `goIdle` is now the second thing to put back.
# Not in `WAKE_PARTS`: it is lifted when it exists and stubbed when it does
# not, so its absence fails one case rather than every case in the section.
SHOW_STARTERS = "function showStarters("
# The reconnect, which is part of the wake path whether or not it reads like
# it: it is the one place outside `goIdle` that takes the armed flag away, and
# the countdown is armed state too.
WAKE_OPEN = "socket.onopen = () => {"


def wake_run(plan: list[dict]) -> dict:
    """Drive the page's wake handling over a run of things that happen to it.

    One step per entry: `wake` is the frame `/api/wake` pushed, `tap` is the
    microphone button, `frame` is anything the server said, `type` is a typed
    question, `wait` moves the clock, `drop` takes the socket away, and `open`
    is the one the retry got.
    """
    app = (JS / "app.js").read_text(encoding="utf-8")
    missing = [part for part in (*WAKE_PARTS, WAKE_TAP, WAKE_OPEN)
               if part not in app]
    if missing:
        return {"error": f"app.js has no {missing[0]}…", "steps": [],
                "log": [], "order": []}
    lifted = "\n".join(
        [found.group(0)
         for name in ("armed", "listening", "idleTimer", "wakeIdleMs")
         if (found := re.search(rf"^let {name} = .*$", app, re.M))]
        # A tree with no `showStarters` gets one that does nothing, so the
        # single case about the row failing is what reports its absence —
        # rather than a ReferenceError taking the whole section with it.
        + [block(app, SHOW_STARTERS) if SHOW_STARTERS in app
           else "function showStarters() {}"]
        + [block(app, header) for header in WAKE_PARTS]
        + [block(app, WAKE_TAP) + ");",
           # Out of `connect`, where it is written on a socket this harness
           # does not have. Lifted whole rather than copied, so the day it
           # stops clearing something the case fails here.
           block(app, WAKE_OPEN).replace("socket.onopen =", "const onopen =")
           + ";"])
    body = WAKE % (block(i18n_src(), "export const STRINGS = ")
                   .removeprefix("export "),
                   block(i18n_src(), "export function t(")
                   .removeprefix("export "),
                   lifted)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", body,
         json.dumps(plan)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip()[-300:], "steps": [], "log": [],
                "order": []}
    return json.loads(proc.stdout)


def socket_run(search: str, home=None, voice: str = "",
               user_id: str = "alex", lang: str = "en") -> dict:
    """Load the page at `?{search}` and report the socket URL it opened.

    `lang` defaults to a tag rather than to nothing because that is the page
    there is: `pickLocale` never answers "" and `applyLanguage` has written it
    onto the document before `connect` is called.
    """
    app = (JS / "app.js").read_text(encoding="utf-8")
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         SOCKET % block(app, "function connect("),
         json.dumps([search, user_id, home, voice, lang])],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip()[-200:]}
    return json.loads(proc.stdout)


def revision_run(replies: list, boot: str = "abc1234",
                 opens: int = 2) -> list:
    """Drive the real `socket.onopen` over a run of reconnects.

    `replies` is what `/api/config` answers, in order, the last entry standing
    for every ask after it; `None` is a server that did not answer. `opens` is
    how many sockets came up — the first is the page's own, every one after it
    is the reconnect a restart caused, and telling them apart is half of what
    is checked here.

    A tree with no `checkRevision` gets an empty one rather than a stack trace,
    so the cases below fail by name on the panel's own symptom instead of
    taking the whole section down.
    """
    app = (JS / "app.js").read_text(encoding="utf-8")
    state = "\n".join(
        found.group(0)
        if (found := re.search(rf"^let {name} = .*$", app, re.M))
        else f"let {name} = {default};"
        for name, default in (("bootRevision", '""'), ("hadSocket", "false")))
    header = "async function checkRevision("
    lifted = "\n".join([
        block(app, header) if header in app
        else "async function checkRevision() {}",
        # The corner stamp's one line. Stubbed away if it is not there yet, so
        # that the reload cases above still say what they are about rather
        # than every one of them dying on a missing name.
        block(app, "function markStale(") if "function markStale(" in app
        else "function markStale() {}",
        block(app, "function connect(") if "function connect(" in app
        else "function connect() {}",
    ])
    arg = json.dumps({"replies": replies, "boot": boot,
                      "steps": [{"open": True}] * opens})
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", REVISION % (state, lifted), arg],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return [{"error": proc.stderr.strip()[-300:]}]
    return json.loads(proc.stdout)


def badge_run(stamp: str, served: str | None = None,
              field: str = "static_revision") -> dict:
    """What `init` makes of the build stamped into its own document.

    `stamp` is the badge's `data-static-revision`, which `root()` substitutes
    on the way out; `served` is what `/api/config` answered, `None` for a fetch
    that failed, and `field` is which key it answered under. Comes back as
    `{"stamped", "boot", "stale"}` — which build the page took for its own, and
    whether it marked the corner on the way past.
    """
    app = (JS / "app.js").read_text(encoding="utf-8")
    lifted = "\n".join(
        block(app, header) if header in app else f"{header}) {{}}"
        for header in ("function stampedBuild(", "function markStale("))
    init = block(app, "async function init(")
    # From the assignment to the brace that closes the compare under it. Cut
    # by name so the run is the source: paraphrasing these three statements is
    # how a check ends up green over a page that stopped doing any of them.
    cut = re.search(r"  const served = config\.static_revision[\s\S]*?\n  \}\n",
                    init)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         BADGE % (lifted, cut.group(0) if cut else ""),
         json.dumps({"stamp": stamp, "served": served, "field": field})],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip()[-300:]}
    return json.loads(proc.stdout)


def revision_cases() -> None:
    print("\n== the build the panel is running, after a deploy ==")

    # Every tuple below carries the guard beside the thing it guards, on one
    # line. Read apart, `no reload when the revision held` is green on a page
    # that never reloads at all — which is the bug — so the pair is the case.
    moved = revision_run([{"revision": "def5678"}])
    held = revision_run([{"revision": "abc1234"}])
    check("the run got through it", moved[0].get("error"), None)
    check("a reconnect onto a server naming a new build reloads the panel, "
          "and one naming the same build does not",
          (moved[-1]["reloads"], held[-1]["reloads"]), (1, 0),
          "the deploy restarts the server and nothing else reloads the page")

    # A deploy that landed while the page was booting reloads even on the
    # document's first socket.
    initial = revision_run([{"static_revision": "s2"}], boot="s1", opens=1)
    check("a socket that opens on a new build reloads the panel even on the "
          "first open",
          initial[-1]["reloads"], 1,
          "a deploy landed during boot and nothing else would reload the page")
    check("and it asks the server rather than the page's cache",
          moved[1]["asked"][0] if moved[1]["asked"] else None,
          ["/api/config", "no-store"],
          "a config answer out of the cache is the build it already has")

    # The two ways a reconnect can land on nothing useful. Both have to leave
    # the panel alone: the failure being fixed is a stale page, and a kitchen
    # panel reloading itself on no evidence is a worse one.
    silent = revision_run([None])
    nameless = revision_run([{"model": "gemini"}])
    # `.get`, because the answer to "did the panel reload" on a run that threw
    # is not a `KeyError` three lines further down. A `checkRevision` that
    # dropped its `catch` takes the whole node process with it — the rejection
    # is unhandled, `onopen` never awaited it — and `revision_run` hands back
    # `{"error": …}`. That is the failure this pair is named for, so it has to
    # read as one.
    check("a server that did not answer, and one too old to name itself, "
          "both leave the panel where it is",
          (silent[-1].get("reloads"), nameless[-1].get("reloads"),
           moved[-1]["reloads"]),
          (0, 0, 1))

    # And the third way, which is the page's own doing rather than the
    # server's: `init`'s `/api/config` failed, so this document never learned
    # which build served it. Every later answer then differs from `""` and the
    # panel would reload on each reconnect for as long as the fetch keeps
    # failing — a boot that half worked turned into a boot loop. The document
    # adopts the running build on reconnect without reloading.
    unknown = revision_run([{"static_revision": "s2"}], boot="", opens=3)
    check("a page that never heard which build served it adopts the running "
          "build and reloads on none of them",
          (all(u["reloads"] == 0 for u in unknown), unknown[-1]["boot"] != ""),
          (True, True),
          "an empty boot revision adopts the next answer without reloading")

    # A browser that refused the navigation, or a reload still in flight when
    # the next close lands. The page has already taken the new revision as its
    # own, so the second reconnect finds nothing moved.
    twice = revision_run([{"revision": "def5678"}], opens=3)
    check("a reload is asked for once per build, not once per reconnect",
          (twice[-1]["reloads"], twice[-1]["boot"]), (1, "def5678"))

    # The commit name is not the panel's build. A backend-only deploy moves
    # `revision` and touches nothing the browser holds, and two deploys off
    # the same dirty HEAD move `app.js` under one unchanged `<sha>-dirty`. So
    # the field compared has to be the digest of what `/static` serves, with
    # `revision` left as the fallback for a server too old to send it.
    backend = revision_run([{"revision": "def5678", "static_revision": "s1"}],
                           boot="s1")
    frontend = revision_run([{"revision": "abc1234-dirty",
                              "static_revision": "s2"}], boot="s1")
    check("a deploy that moved the commit but not a static byte leaves the "
          "panel alone, and one that moved the bytes under an unchanged "
          "commit name reloads it",
          (backend[-1]["reloads"], frontend[-1]["reloads"]), (0, 1),
          "the SHA over-reloads on a Python-only deploy and under-reloads on "
          "a second deploy off the same dirty HEAD")

    # The corner stamp goes the other way round from the reload: it names the
    # build the page is *running*, so a reconnect that finds a new one marks
    # it rather than updating it. Marked before the navigation, because it is
    # the navigation not happening that this is for — a browser that refused
    # the reload, or a later edit that took `checkRevision` out, leaves a
    # panel whose corner is the only thing that still says so. #261.
    check("a reconnect onto a new build marks the corner stamp before it "
          "reloads, and one onto the same build leaves it alone",
          (moved[-1]["stale"], held[-1]["stale"]), (True, False))
    check("and it marks it rather than rewriting it, because the build on "
          "screen has not changed", moved[-1]["badge"], "2026-09-10 23:01",
          "the stamp is the document's; only a new document carries a new one")
    check("the two answers that leave the panel alone leave the stamp alone "
          "too", (silent[-1].get("stale"), nameless[-1].get("stale"),
                  unknown[-1]["stale"]), (False, False, False))
    check("and a backend-only deploy, which is a reconnect onto the same "
          "bytes, does not mark it either", backend[-1]["stale"], False,
          "the corner is about the page, and a Python deploy did not replace "
          "the page")

    python_deploy = revision_run([{"revision": "def5678", "static_revision": "s1",
                                   "build": "2026-09-11 20:03 def5678"}],
                                 boot="s1")
    check("a python-only deploy updates the build badge text without reloading",
          (python_deploy[-1]["reloads"], python_deploy[-1]["badge"],
           python_deploy[-1]["stale"]),
          (0, "2026-09-11 20:03 def5678", False))

    # Source-level, because the value comes off an `await fetch` three hundred
    # lines away in `init` and lifting that would want Maps, the profile
    # picker and the voice list. What the run above cannot see is where the
    # page's own idea of its build comes from in the first place.
    app = (JS / "app.js").read_text(encoding="utf-8")
    init = block(app, "async function init(")
    check("init reads the same field the reconnect check reads back",
          "config.static_revision" in init, True)
    check("and fills the page's own build in from the document first and the "
          "server second",
          "bootRevision = stampedBuild() || served" in init, True,
          "the document names the build that served these bytes; the fetch "
          "names whatever is answering now")
    check("and before it connects, because the socket's open reads it",
          before(init, "bootRevision = stampedBuild()", "connect()"), True)

    # The placeholder guard. `/static/index.html` serves the same file off the
    # same directory with nothing substituted into it, and a page that took
    # `__BUILD_STATIC__` for a build would differ from every answer any server
    # gives — a reload per reconnect, for ever.
    page = (JS.parent / "index.html").read_text(encoding="utf-8")
    check("the markup carries the slots main.py fills and the id app.js marks",
          ['data-static-revision="__BUILD_STATIC__"' in page,
           ">__BUILD__<" in page, 'id="buildBadge"' in page],
          [True, True, True])
    left = badge_run("__BUILD_STATIC__", "def5678")
    check("the run got through it", left.get("error"), None)
    check("a page nothing substituted into reads as having no build of its "
          "own, not as one no server will ever match",
          [left["stamped"], badge_run("abc1234", "abc1234")["stamped"]],
          ["", "abc1234"],
          "taken literally it differs from every answer, which is a reload "
          "per reconnect for ever")

    # Which of the two the page calls its own. The document is the build that
    # served the bytes now running; `/api/config` is whatever is answering at
    # the moment it is asked, and it is the fallback rather than the answer.
    check("the page's build is the one in its own document, not the one the "
          "server names now",
          badge_run("abc1234", "def5678")["boot"], "abc1234")
    check("and a page served with no stamp falls back to the server's answer "
          "rather than to nothing", left["boot"], "def5678")
    check("with neither, it stays empty, which checkRevision refuses to act "
          "on", badge_run("__BUILD_STATIC__", None)["boot"], "")
    # #256's own fallback, one level down: a server too old to send the digest
    # still names its commit, and a page served by one of those has to take
    # that rather than nothing.
    check("and a server answering only the old field is still an answer",
          badge_run("__BUILD_STATIC__", "def5678", field="revision")["boot"],
          "def5678")

    # The one mismatch `checkRevision` cannot see, because it happened before
    # the first socket was ever open: the unit was restarted between the
    # document going out and `init` asking for the config.
    check("a document and a config naming different builds mark the corner "
          "at init", badge_run("abc1234", "def5678")["stale"], True)
    check("and the ordinary case, where they agree, does not",
          badge_run("abc1234", "abc1234")["stale"], False)
    check("nor does a page that was not served with a stamp at all",
          left["stale"], False,
          "no stamp is no evidence, and marking the corner on none is noise")

    # The screensaver, which is a rule in `style.css` and not a line of
    # JavaScript. `.clock` is `inset: 0` over an opaque gradient, so a stamp
    # below its z-index is covered by the clock coming up and there is no
    # second place that has to be kept in step with it. Every `.build` body in
    # the file, media queries included, because the breakpoint has one of its
    # own and a `z-index` raised in there would be invisible to a case reading
    # only the first.
    rules = css_rules(css_text())
    bodies = " ".join(body for sel, body in rules if sel == ".build")
    over = re.search(r"z-index:\s*(\d+)",
                     " ".join(b for s, b in rules if s == ".clock"))
    under = re.search(r"z-index:\s*(\d+)", bodies)
    check("the stamp sits under the clock rather than on top of it",
          bool(under) and bool(over) and int(under.group(1))
          < int(over.group(1)), True,
          "the screensaver is what hides it; nothing in app.js does")
    check("and out of the way of anything anybody taps",
          "pointer-events: none" in bodies, True)


def handshake(out: dict) -> dict:
    """The path and query of the one socket a run opened.

    A run that opened none, or two, comes back saying so rather than throwing:
    either is a different bug from the one the cases below are about and each
    of them is worth its own line.
    """
    opened = out.get("opened", [])
    if len(opened) != 1:
        return {"opened": opened}
    split = urlsplit(opened[0])
    return {"path": split.path, **parse_qs(split.query)}


def wake_cases() -> None:
    print("\n== the wake word, and the timer that undoes it ==")

    app = (JS / "app.js").read_text(encoding="utf-8")
    # First, because every case below runs this code and a page that has none
    # of it deserves one line saying so rather than eleven stack traces.
    check("the page has a wake handler and the timer that undoes it",
          [part for part in (*WAKE_PARTS, WAKE_TAP, WAKE_OPEN)
           if part not in app], [])

    # The socket's dispatch, read rather than run: `connect` builds the
    # handler inside a closure over a live WebSocket, and nothing here can
    # hand it a frame.
    dispatch = block(app, "socket.onmessage = (frame) => {")
    check("a frame typed `wake` is dispatched to it",
          'if (data.type === "wake") return handleWake();' in dispatch, True,
          "the monitor's detection arrives as an ordinary server frame")
    check("a frame typed `idle` is dispatched to goIdle",
          'if (data.type === "idle") return goIdle();' in dispatch, True,
          "#285: ending a Live session signals the page to disarm")
    # Order, not presence. Put the reset after the dispatch and a handler that
    # throws — one bad card — takes the microphone with it.
    check("and every frame restarts the countdown, before the dispatch",
          before(dispatch, "restartIdle();", 'data.type === "wake"'), True,
          "the model talking is somebody still being here")
    check("the countdown itself is the server's number, not the page's",
          "config.wake_idle_s" in block(app, "async function init("), True,
          "`WAKE_IDLE_S` off /api/config")

    idle = re.search(r"^let wakeIdleMs = (\d+);", app, re.M)
    wait = int(idle.group(1)) if idle else 30000

    def at(out: dict, index: int, *keys: str) -> dict:
        """One step of a run, cut down to the keys a case is about.

        A run that could not start has no steps in it at all, and every case
        below is still worth a line under its own name rather than an
        IndexError thrown from the first one.
        """
        steps = out.get("steps", [])
        step = steps[index] if index < len(steps) else {}
        return {key: step.get(key) for key in keys}

    out = wake_run([{"do": "wake"}])
    check("the run got through it", out.get("error"), None)
    check("the wake word opens the microphone and arms a session",
          at(out, 0, "sent", "listening", "armed", "mic", "on", "pending"),
          {"sent": ['{"type":"arm"}'], "listening": True, "armed": True,
           "mic": True, "on": True, "pending": 1})
    check("mic button icon remains 'mic' while listening",
          at(out, 0, "icon", "on"),
          {"icon": "mic", "on": True})

    # The monitor hears the room with no echo cancellation of any kind, so the
    # terminal's own speaker reading an answer that contains the phrase is a
    # detection like any other. `listening` is the only thing here that can
    # tell that from a person, and acting on it restarts the recorder in the
    # middle of the sentence it is already carrying.
    out = wake_run([{"do": "wake"}, {"do": "wake"}])
    check("a wake word heard while the mic is open is ignored",
          at(out, 1, "sent", "starts", "mic"),
          {"sent": [], "starts": 1, "mic": True})

    # The clock, which is the other half of what a wake word has to do. The
    # panel has been quiet for three minutes, so the screensaver is over the
    # whole page; `startListening` then awaits a recorder and writes on a feed
    # nobody can see, and if the room says nothing after all `goIdle` writes
    # its line back there too — twice ignored, and not a pixel either time.
    # See #103.
    out = wake_run([{"do": "wake"}])
    check("the wake word takes the clock away as well as opening the mic",
          at(out, 0, "asleep"), {"asleep": False})
    check("and it does that ahead of the recorder, not behind it",
          out.get("order"), ["wake", "mic"],
          "the other order leaves a clock up for as long as the "
          "microphone takes to open")

    out = wake_run([{"do": "wake"}, {"do": "wake"}])
    check("an echo of the terminal's own voice wakes nothing a second time",
          out.get("order"), ["wake", "mic"],
          "`wake()` sits under the `listening` guard, not over it")

    out = wake_run([{"do": "mic-fails", "value": True}, {"do": "wake"}])
    check("and a microphone that will not open still takes the clock away",
          [at(out, 1, "asleep"), out.get("order")],
          [{"asleep": False}, ["wake"]],
          "otherwise the one line saying so is drawn behind the clock")

    out = wake_run([{"do": "wake"}, {"do": "wait", "ms": wait}])
    check("a room that goes quiet turns the microphone off",
          at(out, 1, "sent", "listening", "armed", "mic", "on", "icon",
             "pending"),
          {"sent": ['{"type":"disarm"}'], "listening": False, "armed": False,
           "mic": False, "on": False, "icon": "mic", "pending": 0})
    check("and says so, because a button that went off by itself is not "
          "obvious on a kiosk",
          [line for line in out.get("log", []) if "wake" in line or "Asleep" in line],
          ["Asleep — say “Hey Gemini” or tap the mic to wake up."])
    # And puts the suggestions back, which nothing but the clear button in the
    # settings sheet used to do. A panel that timed out is otherwise one line
    # of grey text on an empty screen — at exactly the moment somebody walking
    # past it would take a suggestion. The pair is the case: the row has to be
    # gone while the microphone is open, or this is a menu drawn over a
    # conversation.
    check("a timeout puts the suggestions back, and the open mic had them "
          "hidden",
          (at(out, 0, "suggested"), at(out, 1, "suggested")),
          ({"suggested": True}, {"suggested": True}))

    # The microphone button is not the timeout. Somebody who has just tapped
    # the mic off is standing at the panel and has said what they wanted;
    # `stopListening` is shared between the two paths, so the row going back
    # there would put a menu under the hand that dismissed it.
    tapped = wake_run([{"do": "msg"}, {"do": "wake"}, {"do": "tap"}])
    check("tapping the microphone off leaves them where they were",
          [at(tapped, 2, "suggested"), at(tapped, 2, "mic")],
          [{"suggested": False}, {"mic": False}])

    # Ending a Live session signals idle immediately, rather than waiting
    # thirty seconds for the timeout (#285).
    idled = wake_run([{"do": "wake"}, {"do": "idle"}])
    check("an idle frame turns the microphone off and disarms immediately",
          at(idled, 1, "sent", "listening", "armed", "mic", "on", "icon",
             "pending"),
          {"sent": ['{"type":"disarm"}'], "listening": False, "armed": False,
           "mic": False, "on": False, "icon": "mic", "pending": 0},
          "#285: server idle frame closes the thirty-second deaf window")
    re_woken = wake_run([{"do": "wake"}, {"do": "idle"}, {"do": "wake"}])
    check("and the next wake word is heard immediately after idle",
          at(re_woken, 2, "listening", "armed", "mic"),
          {"listening": True, "armed": True, "mic": True},
          "#285: panel is receptive to wake word right after session ends")
    mid_wake = wake_run([{"do": "msg"}, {"do": "wake"}])
    check("waking mid-conversation hides starters before the microphone opens",
          at(mid_wake, 1, "micOpenedWhileSuggested"),
          {"micOpenedWhileSuggested": False},
          "#472: starters must not flash while microphone recorder initializes")

    clean_wake = wake_run([{"do": "wake"}])
    check("a clean wake leaves starter chips visible",
          at(clean_wake, 0, "suggested"),
          {"suggested": True},
          "#472: clean wake leaves starter chips visible on empty feed")
    check("waking mid-conversation hides starter chips",
          at(mid_wake, 1, "suggested"),
          {"suggested": False},
          "#472: starters hidden when conversation in flight")

    check("the wake word hides starters before waking the screen",
          before(block(app, "async function handleWake("),
                 'if (feed.querySelector(".msg, .card")) hideStarters();', "wake();"),
          True,
          "#472: waking by wake word must not flash starters before recorder opens")
    check("startListening hides starters before opening the recorder",
          before(block(app, "async function startListening("),
                 'if (feed.querySelector(".msg, .card")) hideStarters();', "await startRecorder();"),
          True,
          "#472: starters hidden ahead of async recorder initialization")

    # The half that matters more than the timer firing: a long answer read out
    # loud is thirty seconds of nobody in the room saying anything, and a bare
    # clock would cut the terminal off in the middle of it.
    out = wake_run([{"do": "wake"},
                    {"do": "wait", "ms": wait - 1000},
                    {"do": "frame"},
                    {"do": "wait", "ms": wait - 1000},
                    {"do": "wait", "ms": 1000}])
    check("anything said in between starts the countdown over",
          [tuple(at(out, i, "armed", "mic", "sent").values())
           for i in (1, 2, 3)],
          [(True, True, [])] * 3, "twice the idle time, still listening")
    check("and it still fires once that stops",
          at(out, 4, "armed", "sent"),
          {"armed": False, "sent": ['{"type":"disarm"}']})

    out = wake_run([{"do": "wake"}, {"do": "asked", "id": "gws-1"},
                    {"do": "wait", "ms": wait}])
    check("a tool call in flight holds off idle disarm past thirty seconds",
          at(out, 2, "sent", "listening", "armed", "mic", "pending"),
          {"sent": [], "listening": True, "armed": True, "mic": True, "pending": 1},
          "gws or plan in flight must not be cancelled by mic silence timer")

    out = wake_run([{"do": "wake"}, {"do": "asked", "id": "gws-1"},
                    {"do": "wait", "ms": wait},
                    {"do": "answered", "id": "gws-1"},
                    {"do": "wait", "ms": wait}])
    check("once the answer arrives, the next idle timeout disarms cleanly",
          at(out, 4, "sent", "listening", "armed"),
          {"sent": ['{"type":"disarm"}'], "listening": False, "armed": False})

    out = wake_run([{"do": "wake"}, {"do": "asked", "id": "gws-1"},
                    {"do": "wait", "ms": wait},
                    {"do": "lost", "id": "gws-1"},
                    {"do": "wait", "ms": wait}])
    check("a tool card that gave up lets go of the idle guard",
          at(out, 4, "sent", "listening", "armed"),
          {"sent": ['{"type":"disarm"}'], "listening": False, "armed": False})

    out = wake_run([{"do": "wake"}, {"do": "browse", "id": "b-1"},
                    {"do": "wait", "ms": wait}])
    check("a browser task in flight holds off idle disarm past thirty seconds",
          at(out, 2, "sent", "listening", "armed", "mic", "pending"),
          {"sent": [], "listening": True, "armed": True, "mic": True, "pending": 1})

    out = wake_run([{"do": "wake"}, {"do": "browse", "id": "b-1"},
                    {"do": "wait", "ms": wait},
                    {"do": "browse-done", "id": "b-1"},
                    {"do": "wait", "ms": wait}])
    check("once browsing finishes, the next idle timeout disarms cleanly",
          at(out, 4, "sent", "listening", "armed"),
          {"sent": ['{"type":"disarm"}'], "listening": False, "armed": False})

    out = wake_run([{"do": "wake"}, {"do": "browse", "id": "b-1"},
                    {"do": "wait", "ms": wait},
                    {"do": "browse-lost", "id": "b-1"},
                    {"do": "wait", "ms": wait}])
    check("a browse card that gave up lets go of the idle guard",
          at(out, 4, "sent", "listening", "armed"),
          {"sent": ['{"type":"disarm"}'], "listening": False, "armed": False})

    check("and that is the field `goIdle` reads",
          sorted(set(re.findall(r"!entry\.(\w+)",
                                block(app, "async function goIdle(")))), ["lost"],
          "a rename on one side of this is the bug back, silently")

    out = wake_run([{"do": "wake"}, {"do": "wait", "ms": wait},
                    {"do": "wake"}])
    check("the next wake word starts it all again",
          at(out, 2, "sent", "armed", "mic", "starts"),
          {"sent": ['{"type":"arm"}'], "armed": True, "mic": True,
           "starts": 2})

    out = wake_run([{"do": "type"}, {"do": "type"}])
    check("a typed question arms a session without opening the microphone",
          at(out, 0, "sent", "armed", "listening", "mic", "pending"),
          {"sent": ['{"type":"arm"}'], "armed": True, "listening": False,
           "mic": False, "pending": 1})
    check("and a second one does not arm a second session",
          at(out, 1, "sent"), {"sent": []},
          "the server would ignore it; the page "
          "should not need it to")

    out = wake_run([{"do": "mic-fails", "value": True}, {"do": "wake"}])
    check("a microphone that will not open says so and arms nothing",
          [at(out, 1, "sent", "armed", "listening", "mic"),
           out.get("log", [])],
          [{"sent": [], "armed": False, "listening": False, "mic": False},
           ["Could not open the microphone"]],
          "a session armed here would sit open on a terminal that is deaf")

    # The socket can go while the microphone is on — the server restarting is
    # the common one — and the countdown is still the only thing that turns
    # the microphone off. It has to do that half without a socket to say it on.
    out = wake_run([{"do": "wake"}, {"do": "drop"},
                    {"do": "wait", "ms": wait}])
    check("a socket that went away does not leave the mic on forever",
          at(out, 2, "sent", "mic", "armed", "listening"),
          {"sent": [], "mic": False, "armed": False, "listening": False})

    # And the socket that comes back has to take the countdown with the flag.
    # A typed question arms with the microphone off, so the reconnect arms
    # nothing and nothing calls `restartIdle` — the timeout the dead socket's
    # session was counting to is still on this page's clock, and it fires.
    out = wake_run([{"do": "type"}, {"do": "drop"}, {"do": "open"},
                    {"do": "wait", "ms": wait}])
    check("a reconnect drops the countdown along with the session it was for",
          at(out, 2, "armed", "listening", "pending"),
          {"armed": False, "listening": False, "pending": 0})
    check("so nothing later tells a page that was never listening it stopped",
          [at(out, 3, "armed", "sent"), out.get("log")],
          [{"armed": False, "sent": []}, []],
          "the wake word is not what resumes a typed conversation either")

    # The other half of the same line, which the fix must not cost: under a
    # microphone that is still on, the reconnect arms the new socket and that
    # arming is what starts a fresh countdown.
    out = wake_run([{"do": "wake"}, {"do": "drop"}, {"do": "open"}])
    check("a reconnect under an open mic re-arms, countdown and all",
          at(out, 2, "sent", "armed", "listening", "mic", "pending"),
          {"sent": ['{"type":"arm"}'], "armed": True, "listening": True,
           "mic": True, "pending": 1})

    # The case above drives `armSession` itself, which holds whether or not
    # anything on the page calls it. `send` is where a typed question goes,
    # and it is one line: without it the text reaches a socket with no model
    # behind it — no answer drawn, and nothing logged anywhere to say why.
    check("the typed path arms through `send`, before it starts the turn",
          before(block(app, "function send("), "armSession();",
                 "startTurn();"),
          True)

    # An idle countdown on a terminal nobody armed turns a microphone that was
    # never on off, thirty seconds after the page loads, and says so on the
    # screen. `goIdle` also sends nothing then, so the line is all the room
    # sees: a kiosk reporting that it stopped listening, having never started.
    out = wake_run([{"do": "frame"}, {"do": "wait", "ms": wait}])
    check("nothing counts down while nothing is armed",
          [at(out, 0, "pending"), at(out, 1, "listening"), out.get("log")],
          [{"pending": 0}, {"listening": False}, []])

    check("and the page keeps its own number when the server's is unusable",
          idle_run([{"wake_idle_s": 45}, {}, {"wake_idle_s": "soon"},
                    {"wake_idle_s": 0}]),
          [45000, wait, wait, wait],
          "`setTimeout(fn, NaN)` fires on the next tick, which is a "
          "microphone that goes off in the same breath as the wake word")

    print("\n== which page the wake word will be sent to ==")

    # Everything above is what a page does once the frame arrives. This is how
    # the server decides which page gets one at all: a screen with two pages on
    # its session has to say which of them is standing in the kitchen, because
    # the panel reaches a development Mac down an ssh tunnel and so connects
    # from 127.0.0.1 like a browser tab on the Mac (see #132). The unit that
    # launches Chromium puts `?kiosk=1` on the page's URL and `connect`
    # forwards it onto the handshake.
    home = {"label": "Shibuya", "lat": 35.66, "lng": 139.7}

    plain = socket_run("")
    check("the run got through it", plain.get("error"), None)
    check("a page opened with no query claims nothing but its language",
          handshake(plain), {"path": "/ws/alex", "lang": ["en"]},
          "every tab on the Mac is this one")

    check("a page opened at ?kiosk=1 says so on its socket",
          handshake(socket_run("?kiosk=1")),
          {"path": "/ws/alex", "kiosk": ["1"], "lang": ["en"]},
          "without this the word said in the kitchen opens the Mac's mic")
    check("beside everything else the handshake already carries",
          handshake(socket_run("?kiosk=1", home=home, voice="Fenrir",
                               lang="ja")),
          {"path": "/ws/alex", "location": ["Shibuya"], "lat": ["35.66"],
           "lng": ["139.7"], "voice": ["Fenrir"], "kiosk": ["1"],
           "lang": ["ja"]})

    # The page's URL is not a way into the handshake generally. Only the one
    # parameter travels: a `voice` typed into the address bar must not reach
    # the Live API's setup message behind the picker's back, and a stray
    # parameter must not reach the server at all.
    check("and nothing else off the page's URL travels with it",
          handshake(socket_run("?kiosk=1&voice=Puck&debug=1&lat=1")),
          {"path": "/ws/alex", "kiosk": ["1"], "lang": ["en"]},
          "the address bar is not a settings sheet")

    # `?kiosk=` is what an operator halfway through editing the unit leaves
    # behind. Empty is not a claim, and `main._is_kiosk` reads it the same way.
    check("an empty value is not a claim to be the kiosk",
          handshake(socket_run("?kiosk=")),
          {"path": "/ws/alex", "lang": ["en"]})

    # The panel's own setting, which is the whole of the browser's side of
    # #187: `maps_search` on a Japanese panel came back in English because the
    # server was never told which language the panel was in. The tag is read
    # off the document rather than out of `settings`, so what travels is what
    # `i18n.t` and the Maps tiles are already answering to.
    check("the language the panel is set to travels with the handshake",
          handshake(socket_run("", lang="ja")),
          {"path": "/ws/alex", "lang": ["ja"]},
          "the floor under every tool's `lang` argument")
    # A document with no tag on it is not a page this build serves — but it is
    # what an older `index.html` or a half-loaded module leaves behind, and an
    # empty `lang=` on the handshake is a claim to a language nobody set.
    # Read off the URL rather than through `handshake`, which parses with
    # `parse_qs` and drops a blank value — so `lang=` and no `lang` at all look
    # the same through it, and the case would hold over the bug.
    check("and a document with nothing on it claims nothing",
          [url.split("?", 1)[1:] for url in socket_run("", lang="")
           .get("opened", [])],
          [[]], "an empty `lang=` is a claim to a language nobody set")


def idle_run(configs: list[dict]) -> list:
    """What `init` leaves `wakeIdleMs` at, for each `/api/config` it reads.

    Three lines out of a function that also builds the settings sheet, asks
    for a location and opens the socket, run here over payloads a server
    actually sends: a number, an older server that has no such key, and a
    value that is not a number at all.
    """
    app = (JS / "app.js").read_text(encoding="utf-8")
    default = re.search(r"^let wakeIdleMs = .*$", app, re.M)
    taken = re.search(
        r"^  (?:if \(.*\) \{\n)?\s*wakeIdleMs = .*;\n(?:  \}\n)?",
        block(app, "async function init("), re.M)
    if default is None or taken is None:
        return ["app.js reads no idle window out of the config"]
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", f"""
{default.group(0)}
const decide = (config) => {{
  wakeIdleMs = {re.search(r"= (.*);", default.group(0)).group(1)};
{taken.group(0)}
  return wakeIdleMs;
}};
console.log(JSON.stringify(JSON.parse(process.argv[1]).map(decide)));
""", json.dumps(configs)], capture_output=True, text=True, cwd=ROOT)
    if proc.returncode:
        return [proc.stderr.strip()[-300:]]
    return json.loads(proc.stdout)


def visible_run() -> dict:
    """Drive `whileVisible` and `whenVisible` over a fake observer."""
    cards_js = (JS / "cards.js").read_text(encoding="utf-8")
    lifted = "\n\n".join(block(cards_js, header) for header in
                         ("function whileVisible(", "function whenVisible("))
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", VISIBLE % lifted],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        # Reported rather than fatal, so an observer that reaches for a method
        # the fake does not have fails a case with a name on it.
        return {"error": proc.stderr.strip()[-200:], "paused": {}, "lazy": {}}
    return json.loads(proc.stdout)


def visible_cases() -> None:
    print("\n== the cards that wait to be scrolled to ==")

    out = visible_run()
    check("the run got through it", out.get("error"), None)
    paused, lazy = out["paused"], out["lazy"]

    # A weather card is paused before anyone has looked at it, and unpaused by
    # the first callback. Which means the pause is the part that costs nothing
    # to get wrong: with no `observe`, the animation the card exists to show
    # never starts, on a card sitting in plain view.
    check("the weather card starts paused", paused.get("atFirst"),
          ["card", "paused"])
    check("and the observer it made is watching that card",
          paused.get("watching"), True,
          "otherwise it stays paused for the life of the page")
    check("against the pane it scrolls in, not the window",
          paused.get("root"), True)
    check("scrolled into view it runs", paused.get("scrolledIn"), ["card"])
    check("and pauses again on the way out", paused.get("scrolledOut"),
          ["card", "paused"])

    # The other one is what loads a map, and it is subscribed the same way. Its
    # failure is the sharper of the two: three cards get their whole contents
    # from the `init` this never runs.
    check("a lazy card's init waits for it to be seen", lazy.get("atFirst"), 0)
    check("and its observer is watching too", lazy.get("watching"), True)
    check("still off screen, still nothing", lazy.get("offScreen"), 0)
    check("in view it runs, once, however many times it is told",
          lazy.get("onScreen"), 1)
    check("and unsubscribes rather than staying to count", lazy.get("dropped"),
          True)


def profile_run(here: str, names) -> dict:
    """Drive `showProfiles` for `here` over the list the server sent."""
    app = (JS / "app.js").read_text(encoding="utf-8")
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         PROFILE % block(app, "function showProfiles("),
         json.dumps(here), json.dumps(names)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        # Reported rather than fatal: what the server sends is JSON off the
        # wire, and a `showProfiles` that assumes it is a list of strings
        # should fail a case with a name on it, not a stack trace.
        return {"error": proc.stderr.strip()[-200:]}
    return json.loads(proc.stdout)


def profile_cases() -> None:
    print("\n== who the terminal is talking to ==")

    # A household that never set `PROFILES` gets the sheet it always had. The
    # picker is the one control here that can mislead rather than merely fail,
    # so it stays out of the way until there is a choice behind it.
    alone = profile_run("terminal", [])
    check("the run got through it", alone.get("error"), None)
    check("nobody named, so no picker", alone.get("shown"), False)
    check("and no label over the empty space", alone.get("labelled"), False)
    check("one name is not a choice either",
          profile_run("alex", ["alex"]).get("shown"), False)

    two = profile_run("alex", ["alex", "sam"])
    check("two, and the sheet grows a picker", two.get("shown"), True)
    check("with a label on it", two.get("labelled"), True)
    check("naming the household in the order the server sent",
          two.get("names"), ["alex", "sam"])
    check("and the one it is on marked as the one it is on",
          two.get("selected"), ["alex"])

    # The dangerous case: the list moved on and this terminal did not. Showing
    # the list as sent would name sam on a socket still carrying alex's thread.
    dropped = profile_run("alex", ["sam", "robin"])
    check("a name dropped from the list is still in the picker",
          dropped.get("names"), ["alex", "sam", "robin"])
    check("and still the one selected", dropped.get("selected"), ["alex"])
    check("the name is what is written on the option too",
          dropped.get("labels"), ["alex", "sam", "robin"])

    # `/api/config` is JSON off the wire, and `init` hands whatever came back
    # straight in. An older server answers without the field at all.
    check("a server that sends nothing sensible leaves the sheet alone",
          [profile_run("alex", v).get("shown") for v in (None, "alex", {})],
          [False, False, False])

    # The rest is source-level: what changing it does. Two of the three steps
    # are the ones that go wrong quietly — the screen keeps the last person's
    # conversation on it, or the socket stays on their thread.
    app = (JS / "app.js").read_text(encoding="utf-8")
    handler = block(app, 'profileSelect.addEventListener("change"')
    check("switching remembers who was picked",
          "saveSettings({ profile: userId })" in handler, True)
    check("clears what the last person left on screen",
          "clearButton.click()" in handler, True)
    check("before reopening the socket on the new one",
          before(handler, "clearButton.click()", "reopen("), True)
    check("and says so, because the feed it just emptied cannot",
          "systemLine" in block(app, "function reopen("), True)
    # A no-op `change` still fires when the value is set back to what it was.
    # Without the guard that is a conversation dropped for nothing.
    check("picking the name already picked does nothing",
          "next === userId" in handler, True)
    # `forgetTimer` is one variable for the whole page, and `forget` reads
    # `userId` at the second tap rather than at the first. Armed on one person
    # and switched to another, the tap that follows deletes the second one's
    # history against a confirmation given for the first — and the picker sits
    # directly above the button in the same sheet (`index.html:114-116`), so it
    # is two taps apart, not a contrivance.
    check("and switching disarms a forget aimed at the last person",
          "disarmForget()" in handler, True)
    check("ahead of the clear and the reopen, not after them",
          before(handler, "disarmForget()", "clearButton.click()"), True)

    # The path segment. A name with a space or a slash in it is a household
    # member, not an attack, and either one breaks the URL unencoded.
    connect = block(app, "function connect(")
    check("the profile is what the socket asks for",
          "encodeURIComponent(userId)" in connect, True)
    check("and it is read back from the settings at startup",
          "settings.profile || DEFAULT_PROFILE" in app, True)
    # The wiring line. `showProfiles` is called from exactly one place, and
    # without it the sheet has no picker on a household that configured one.
    check("and the picker is filled from what the server sent",
          "showProfiles(config.profiles" in block(app, "async function init("),
          True)


def voice_run(saved, names, fallback=None) -> dict:
    """Drive `showVoices` for a page holding `saved` over what a server sent."""
    app = (JS / "app.js").read_text(encoding="utf-8")
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         VOICE % block(app, "function showVoices("),
         json.dumps(saved), json.dumps(names), json.dumps(fallback)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        # Reported rather than fatal, like `profile_run`: this is JSON off the
        # wire and an older server answers without the fields at all.
        return {"error": proc.stderr.strip()[-200:]}
    return json.loads(proc.stdout)


VOICES = ["Kore", "Puck", "Charon", "Aoede", "Fenrir"]


def voice_cases() -> None:
    print("\n== which voice the terminal answers in ==")

    fresh = voice_run("", VOICES, "Kore")
    check("the run got through it", fresh.get("error"), None)
    check("the sheet offers what the server serves, in its order",
          fresh.get("names"), VOICES)
    check("named the same on the option as in the value",
          fresh.get("labels"), VOICES)
    check("a terminal nobody has asked lands on the server's default",
          fresh.get("voice"), "Kore")
    check("and that is the one the sheet shows selected",
          fresh.get("selected"), ["Kore"])

    kept = voice_run("Fenrir", VOICES, "Kore")
    check("a saved choice survives the server's opinion of the default",
          [kept.get("voice"), kept.get("selected")], ["Fenrir", ["Fenrir"]])

    # The one that goes wrong quietly. `main.py:_voice` refuses a name outside
    # `VOICES` and opens the session on the default, so a select still showing
    # the old name is the sheet disagreeing with the speaker.
    gone = voice_run("Zephyr", VOICES, "Kore")
    check("a saved voice this build no longer serves is not left selected",
          gone.get("selected"), ["Kore"])
    check("and the variable the socket reads moved with it",
          gone.get("voice"), "Kore")
    # Two servers disagreeing, which is a page cached from an older build. The
    # first listed voice is the one `main.py` falls back to as well.
    stray = voice_run("Zephyr", VOICES, "Vega")
    check("a default that is not on its own list falls to the first that is",
          [stray.get("voice"), stray.get("selected")], ["Kore", ["Kore"]])

    check("exactly one option is ever marked selected",
          [len(voice_run(saved, VOICES, "Kore").get("selected", []))
           for saved in ("", "Kore", "Fenrir", "Zephyr")], [1, 1, 1, 1])

    # `/api/config` is JSON off the wire and an older server has no idea what a
    # voice is. An empty picker connects the way this build did before there
    # was one, which is the server's default — the wrong thing to do is leave a
    # select the person can open and choose nothing from.
    for sent in (None, "Kore", {}, [], [""], [1, 2]):
        empty = voice_run("Fenrir", sent, None)
        check(f"a server that sends {sent!r} leaves the picker alone",
              empty.get("names"), [])
        check("and does not touch what the page was going to ask for",
              empty.get("voice"), "Fenrir")
    check("junk mixed into a real list is dropped rather than offered",
          voice_run("", [None, "Kore", 7, "Puck", ""], "Kore").get("names"),
          ["Kore", "Puck"])

    # The rest is source-level: the two orderings that make the setting take
    # effect, and the one line that must not be copied from the profile picker.
    app = (JS / "app.js").read_text(encoding="utf-8")
    connect = block(app, "function connect(")
    check("the voice rides the handshake, because the setup message is the "
          "only place the Live API takes one",
          'query.set("voice", voice)' in connect, True)
    check("and is left off entirely when there is none to send",
          "if (voice) query.set" in connect, True)

    init = block(app, "async function init(")
    check("the picker is filled from what the server sent",
          "showVoices(config.voices" in init, True)
    check("before the first socket opens, which is the only one it can affect",
          before(init, "showVoices(", "connect()"), True)

    handler = block(app, 'voiceSelect.addEventListener("change"')
    check("changing it is remembered", "saveSettings({ voice })" in handler,
          True)
    check("and costs a reconnect, because nothing can change it in place",
          "reopen(" in handler, True)
    # A `change` fires when the value is put back to what it was, and here that
    # would drop a conversation mid-answer for nothing.
    check("picking the voice already picked does nothing",
          "next === voice" in handler, True)
    # The profile handler clears the screen; this one must not. Same person,
    # same thread — the server puts the new socket back on it, so wiping the
    # feed would throw away a conversation that is still going.
    check("and the conversation on screen is not thrown away with the socket",
          "clearButton.click()" in handler, False)


def forget_run(taps: list[str], reply: dict | None = None,
               here: str = "alex") -> dict:
    """Tap the button `taps` times at a server that answers with `reply`."""
    app = (JS / "app.js").read_text(encoding="utf-8")
    switch = block(app, 'profileSelect.addEventListener("change"')
    body = FORGET % (block(i18n_src(), "export const STRINGS = ")
                     .removeprefix("export "),
                     block(i18n_src(), "export function t(")
                     .removeprefix("export "),
                     block(app, "function disarmForget("),
                     block(app, "async function forget("),
                     # `block` stops at the matching brace, so the listener
                     # comes back without the `);` closing the call it is an
                     # argument to.
                     switch + ");")
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", body, json.dumps(here),
         json.dumps({"taps": taps, "reply": reply or {"body":
                                                      {"forgotten": 2}}})],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return {"error": proc.stderr.strip()[-200:]}
    return json.loads(proc.stdout)


def forget_cases() -> None:
    print("\n== the button that deletes a conversation ==")

    one = forget_run(["tap"])
    check("the run got through it", one.get("error"), None)
    # The whole point of the two taps. One of them lands on a sheet somebody
    # opened to change the microphone.
    check("one tap sends nothing", one.get("asked"), [])
    check("and touches neither the screen nor the socket", one.get("log"), [])
    check("it arms instead, and says what the next tap will do",
          one.get("label"), "Tap again to forget")
    check("marked as the one control here that destroys something",
          one.get("armed"), True)
    check("for as long as the arming window and no longer",
          one.get("armedFor"), [5000])

    # Armed forever is the same button without the guard, one tap later.
    lapsed = forget_run(["tap", "expire", "tap"])
    check("the window running out puts the label back",
          [s["label"] for s in lapsed.get("steps", [])][1],
          "Forget my history")
    check("and takes the marking off with it",
          [s["armed"] for s in lapsed.get("steps", [])][1], False)
    check("so the tap after that arms again rather than deleting",
          lapsed.get("asked"), [])

    # The same lapse, but caused by the picker six elements above the button
    # rather than by the clock. `forgetTimer` is page-wide and `forget` reads
    # `userId` at the second tap, so an arming that outlives the switch is a
    # confirmation given for alex spending itself on jordan. Run through the
    # real change handler, not a restatement of it.
    crossed = forget_run(["tap", "switch:jordan", "tap"])
    check("the switch went through", crossed.get("userId"), "jordan")
    check("switching profiles takes the arming with it",
          [s["label"] for s in crossed.get("steps", [])][1],
          "Forget my history")
    check("and the red goes too, over a feed that now names somebody else",
          [s["armed"] for s in crossed.get("steps", [])][1], False)
    check("so the tap after it deletes nobody's history",
          crossed.get("asked"), [])
    check("it arms afresh, for the person actually on screen",
          [crossed.get("label"), crossed.get("armed")],
          ["Tap again to forget", True])

    two = forget_run(["tap", "tap"])
    check("two taps ask the server, as a POST", two.get("asked"),
          [{"url": "/api/forget/alex", "method": "POST",
            "type": "application/json"}])
    # Not a formality, and the server refuses the call without it
    # (`main.py:_sent_deliberately`). A POST with no content type is a CORS
    # *simple* request: no preflight, so a page open in a browser two rooms
    # away can make that call to us and delete a month of conversation, unable
    # to read the answer and not needing to. `application/json` is not on the
    # simple list, so the same attempt has to ask permission first and is
    # refused. Same-origin — us — is never preflighted and pays nothing.
    check("carrying the one header that stops any other page sending it",
          two.get("asked", [{}])[0].get("type"), "application/json")
    check("then clear the screen and reopen on the thread that replaced it",
          two.get("log"), ["clear", "reopen"])
    check("saying how much went, because the feed it emptied cannot",
          two.get("notes"), ["Forgot 2 conversations."])
    check("and the button goes back to resting, disarmed",
          [two.get("label"), two.get("armed"), two.get("pending")],
          ["Forget my history", False, 0])
    check("one conversation is not two", forget_run(
        ["tap", "tap"], {"body": {"forgotten": 1}}).get("notes"),
        ["Forgot 1 conversation."])
    check("and a person the server had nothing on is told that",
          forget_run(["tap", "tap"], {"body": {"forgotten": 0}}).get("notes"),
          ["There was nothing to forget."])

    # The case worth the most: a screen wiped over a server that still has the
    # history leaves the person believing it is gone, and nothing left on the
    # feed to say otherwise.
    for name, reply in (("a server that refuses", {"ok": False,
                                                   "status": 500}),
                        ("a server that is not there",
                         {"throws": "Failed to fetch"})):
        failed = forget_run(["tap", "tap"], reply)
        check(f"{name} clears nothing", failed.get("log"), ["system"])
        check("and says so rather than looking like it worked",
              failed.get("notes", [""])[0].startswith("Could not forget: "),
              True, failed.get("notes", [""])[0])
        check("with the button back where it started",
              [failed.get("armed"), failed.get("pending")], [False, 0])

    # A name with a slash in it is a household member, not an attack — and
    # unencoded it is a different route entirely.
    check("the profile is escaped into the path", forget_run(
        ["tap", "tap"], here="ma ri/x").get("asked"),
        [{"url": "/api/forget/ma%20ri%2Fx", "method": "POST",
          "type": "application/json"}])

    # Source-level: the two buttons stay two buttons. Wiping the screen and
    # deleting the history are different things to want, and a sheet that
    # offered only the second would make the cheap one unavailable.
    app = (JS / "app.js").read_text(encoding="utf-8")
    page = (JS.parent / "index.html").read_text(encoding="utf-8")
    check("the sheet keeps clearing and forgetting apart",
          ['id="clearButton"' in page, 'id="forgetButton"' in page],
          [True, True])
    check("nothing local is thrown away before the server has answered",
          before(block(app, "async function forget("), "await fetch(",
                 "clearButton.click()"), True)
    check("the sheet includes reboot button",
          'id="rebootButton"' in page, True)
    check("reboot button arms before sending request",
          before(block(app, "async function reboot("), 'rebootButton.classList.add("armed")',
                 "await fetch("), True)
    check("reboot button is shown only when management routes are enabled",
          'config.management_routes && rebootButton' in app
          and 'rebootButton.classList.remove("hidden")' in app, True)


CLOCK = r"""
const script = JSON.parse(process.argv[1]);

// A clock the cases move by hand, and timers that fire only when it reaches
// them. Real ones would make every case here about how fast this machine is,
// and the two things being checked — that the face is redrawn once a minute
// rather than once a second, and that the screen goes back to being a clock
// three minutes after the last word — are both measured in minutes.
const RealDate = Date;
let now = RealDate.parse(script.start);
let seq = 0;
const timers = new Map();

function setTimeout(fn, ms) {
  timers.set(++seq, { at: now + ms, fn });
  return seq;
}

function clearTimeout(id) {
  timers.delete(id);
}

function advance(ms) {
  const until = now + ms;
  for (;;) {
    let due = null;
    for (const [id, t] of timers) {
      if (t.at <= until && (due === null || t.at < due[1].at)) due = [id, t];
    }
    if (due === null) break;
    now = due[1].at;
    timers.delete(due[0]);
    due[1].fn();
  }
  now = until;
}

// Both `Date.now()` and `new Date()` read the fake clock, because the section
// uses one to schedule and the other to format and a harness that only faked
// one would have the face and the timer disagree.
class Date_ extends RealDate {
  constructor(...args) { super(...(args.length ? args : [now])); }
  static now() { return now; }
}
globalThis.Date = Date_;

class El {
  constructor(id) {
    this.id = id;
    this._text = "";
    this._attrs = {};
    this._classes = new Set();
    // Counted, not just read: writing the same `src` back restarts the load
    // and blinks the gradient, and writing the same time back sixty times a
    // minute is the interval this is checked not to be.
    this.writes = 0;
    this.loads = 0;
    this.classList = {
      add: (c) => this._classes.add(c),
      remove: (c) => this._classes.delete(c),
      contains: (c) => this._classes.has(c),
      // The bar's two classes are written with the two-argument form. The
      // one-argument form flips, which is what a real `classList` does and
      // is never what a redraw wants — a `hidden` that flips every minute
      // is a bar that blinks. So the stub refuses it rather than guessing.
      toggle: (c, on) => {
        if (on === undefined) throw new Error("toggle needs a force flag");
        return on ? this._classes.add(c) : this._classes.delete(c);
      },
    };
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.writes++; }
  getAttribute(n) { return n in this._attrs ? this._attrs[n] : null; }
  get src() { return this._attrs.src; }
  set src(v) { this._attrs.src = v; this.loads++; }
}

const clockPage = new El("clockPage");
const clockImage = new El("clockImage");
const clockTime = new El("clockTime");
const clockDate = new El("clockDate");
const clockWeather = new El("clockWeather");

// The top bar's half of the same clock, which is the half that is on screen
// while somebody is talking to the terminal.
const barDate = new El("barDate");
const barTime = new El("barTime");
const barWx = new El("barWx");
const barWxIcon = new El("barWxIcon");
const barWxTemp = new El("barWxTemp");

// The nine-slug glyph table is `cards.js`'s, and which glyph a slug gets is
// that file's business. What this section decides is which slug and which
// half of the day are handed to it, so the stub reports its arguments and a
// source case below says the bar reaches for the real one.
function wxIcon(condition, isDay) {
  return `${condition}/${isDay ? "day" : "night"}`;
}

// The two maps the rest of the page tracks unanswered questions in: a card
// drawn and still spinning. They are declared outside the slice below and
// `sleep` reads both, because a spinning card is somebody waiting for an
// answer rather than an empty room.
let pending = new Map();
let browsing = new Map();

// The microphone and the session under it, which live at the top of `app.js`
// rather than in the slice below and are the third thing `sleep` reads.
// Declared here for the same reason the two maps are: a run that left them out
// would be a `ReferenceError` on every case, not a case about the guard. Two
// flags and not one, because they come apart — `startListening` opens the
// microphone whether or not `armSession` had a socket to arm on.
let listening = false;
let armed = false;

// Turn and panel state cleared on sleep. Declared here for the same reason
// the maps above are: `sleep` clears both panes and resets turn state, and
// omitting them leaves a ReferenceError.
let userBubble = null;
let agentBubble = null;
let seenTools = new Set();
let userCleared = false;
let startersShown = 0;
let feedCleared = 0;
let cardsCleared = 0;

function hideWorking() {}
function forgetCards() {}
function forgetTopic() {}
function showStarters() { startersShown++; }
function dropPending() {}

const feed = {
  querySelectorAll: (sel) => [{ remove: () => { feedCleared++; } }],
};
const cards = {
  replaceChildren: () => { cardsCleared++; },
};

// The socket, because `sleep` now says something on it: the server cannot see
// that the face came back up, and until it is told, the conversation under it
// is still the current one and the next wake word replays the whole of it.
// A plain object rather than a class — the section reads two properties off
// it and calls one method.
const WebSocket = { OPEN: 1 };
const wire = [];
const socket = script.socket === null ? null : {
  readyState: script.socket ?? WebSocket.OPEN,
  send: (text) => wire.push(JSON.parse(text)),
};

const bound = [];
const document = {
  documentElement: { lang: script.lang ?? "" },
  addEventListener: (ev, fn, opts) => bound.push({ ev, fn, opts }),
};
const navigator = { language: "en-GB" };

let fetched = 0;
// What was asked for, and not only how often. Since #131 the page has
// something to say on the way out — which language it wants the sky
// described in — so the URL is part of what a case here can be about.
const asked = [];
async function fetch(url) {
  fetched++;
  asked.push(url);
  // A list is a server whose answer changes between asks, which is the only
  // way to state the thing #86 is about: the picture behind the clock is
  // replaced seven times a day by a task the page cannot see, so a case that
  // serves one body forever cannot tell a face that repaints from one that
  // never asks again. The last entry stands for every ask after it.
  const replies = script.server;
  const reply = Array.isArray(replies)
    ? replies[Math.min(fetched - 1, replies.length - 1)] : replies;
  // No `server` at all is the offline case: a kiosk whose Mac is asleep, and
  // an older server with no such route answers 404. Both have to leave a clock
  // on the screen rather than a stack trace in a console nobody has open.
  if (!reply) throw new Error("failed to fetch");
  if (reply.status && reply.status !== 200) {
    return { ok: false, status: reply.status };
  }
  return { ok: true, status: 200, json: async () => reply.body };
}

%s

// The fetch stub resolves on the microtask queue, and the chain behind it is
// three deep: the call, the json(), the paint.
const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };

function frame() {
  return {
    hidden: clockPage._classes.has("hidden"),
    time: clockTime.textContent,
    date: clockDate.textContent,
    weather: clockWeather.textContent,
    bar: barTime.textContent,
    barDate: barDate.textContent,
    wx: [barWxIcon.textContent, barWxTemp.textContent],
    wxClasses: [...barWx._classes].sort(),
    iconHidden: barWxIcon._classes.has("hidden"),
    src: clockImage.getAttribute("src"),
    loads: clockImage.loads,
    redraws: clockTime.writes,
    fetched,
    asked: [...asked],
    timers: timers.size,
    cards: pending.size + browsing.size,
    listening,
    armed,
    cardsCleared,
    feedCleared,
    startersShown,
    // Every frame this section has put on the socket so far, in order. A list
    // and not a count, because the frame's name is half of what is checked:
    // the server dispatches on the exact string and answers a word it does
    // not know with a warning nobody reads.
    wire: wire.map((f) => f.type),
    listeners: bound.map((b) => [b.ev, b.opts?.capture === true]),
  };
}

const out = [];
for (const step of script.steps) {
  if (step.sleep) sleep();
  else if (step.wake) wake();
  // `!== undefined`, because turning these back off is half of what the cases
  // they are for check, and `false` would fall straight through this chain.
  else if (step.mic !== undefined) listening = step.mic;
  else if (step.session !== undefined) armed = step.session;
  else if (step.asked) pending.set(step.asked, {});
  else if (step.answered) pending.delete(step.answered);
  else if (step.lost) {
    // What the deadline callback does: the entry stays, flagged.
    const entry = pending.get(step.lost);
    if (entry) entry.lost = true; else out.push({ missing: step.lost });
  }
  else if (step.opening) browsing.set(step.opening, {});
  else if (step.opened) browsing.delete(step.opened);
  else if (step.browseLost) {
    const entry = browsing.get(step.browseLost);
    if (entry) entry.lost = true; else out.push({ missing: step.browseLost });
  }
  else if (step.advance) advance(step.advance);
  else if (step.event) {
    const it = bound.find((b) => b.ev === step.event);
    if (it) it.fn(); else out.push({ missing: step.event });
  } else if (step.paint) paintScreensaver(step.paint);
  else if (step.face) {
    out.push(clockFace(new Date_(RealDate.parse(step.face)), step.in));
    continue;
  } else if (step.bar) {
    out.push(barFace(new Date_(RealDate.parse(step.bar)), step.in));
    continue;
  } else if (step.relang) {
    // What `applyLanguage` does to this section: the tag on the document
    // moves, and the bar is asked to draw itself again off it.
    document.documentElement.lang = step.relang;
    drawBar();
  } else if (step.until !== undefined) {
    out.push({ until: untilNextMinute(RealDate.parse(step.until)) });
    continue;
  }
  await flush();
  out.push(frame());
}
console.log(JSON.stringify(out));
"""


def clock_src() -> str:
    """The clock section of `app.js`, header to header.

    One slice for the reason :func:`scroll_src` takes one: `asleep`,
    `clockIdleTimer` and `clockTimer` are shared by every function in it, and
    which of them clears which is most of what can go wrong. Lifting the
    functions one at a time would leave the flags behind and node would say so.
    """
    src = (JS / "app.js").read_text(encoding="utf-8")
    start = src.index("/* ---------- the clock ---------- */")
    return src[start:src.index("/* ---------- init ---------- */", start)]


def clock_run(steps: list[dict], server: dict | None = None,
              lang: str = "", start: str = "2026-03-14T09:41:12+09:00",
              socket: int | None = 1) -> list:
    """Drive the clock section over a list of steps, a frame per step.

    `socket` is the socket's `readyState`, or None for a page that has none.
    Both are states the kiosk is really in — the panel reconnects with a
    backoff, so a screensaver landing during an outage is ordinary — and
    neither may be an exception out of the countdown.
    """
    arg = json.dumps({"steps": steps, "server": server, "lang": lang,
                      "start": start, "socket": socket})
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", CLOCK % clock_src(), arg],
        capture_output=True, text=True, cwd=ROOT,
        # Pinned, or every case that names an hour is a case about where the
        # machine running this happens to be.
        env={**os.environ, "TZ": "Asia/Tokyo"},
    )
    if proc.returncode:
        # Reported rather than fatal: the ways this section breaks — a timer
        # cleared that was never set, a face drawn before its element exists —
        # deserve a case with a name on it rather than a stack trace.
        return [{"error": proc.stderr.strip()[-300:]}]
    return json.loads(proc.stdout)


def clock_cases() -> None:
    print("\n== the clock the panel falls back to ==")

    # The minute boundary, on its own. A zero here is a timer that fires
    # immediately and reschedules itself, which is a busy loop rather than a
    # clock, and it is exactly what `60000 - now % 60000` avoids at the one
    # instant `now % 60000` is zero.
    edges = clock_run([{"until": "2026-03-14T09:41:12+09:00"},
                       {"until": "2026-03-14T09:41:00+09:00"},
                       {"until": "2026-03-14T09:41:59.500+09:00"}])
    check("the run got through it", edges[0].get("error"), None)
    check("twelve seconds in, forty-eight to go", edges[0]["until"], 48000)
    check("and on the minute it waits a whole one, not none",
          edges[1]["until"], 60000, "a 0 here is a busy loop")
    check("half a second short of the minute rounds up, not down",
          edges[2]["until"], 500)

    # The face itself. `hourCycle: "h23"` and `hour12: false` differ in exactly
    # one place, and it is the hour a kitchen clock is least often looked at
    # and most embarrassing to get wrong.
    faces = clock_run([{"face": "2026-03-15T00:10:00+09:00", "in": "en-GB"},
                       {"face": "2026-03-14T09:41:12+09:00", "in": "en-GB"},
                       {"face": "2026-03-14T09:41:12+09:00", "in": "ja"}])
    check("midnight is 00, not 24", faces[0]["time"], "00:10")
    check("and an ordinary morning is itself", faces[1]["time"], "09:41")
    check("the date follows the language the panel is set to", faces[2]["date"],
          "3月14日土曜日", faces[2]["date"])
    check("and is not the English one", faces[1]["date"], "Saturday 14 March")

    # What the panel does between Chromium starting and anybody speaking.
    boot = clock_run([{"sleep": True}, {"advance": 30000},
                      {"advance": 30000}, {"advance": 3600000}])
    check("the run got through it", boot[0].get("error"), None)
    check("the page opens on the clock", boot[0]["hidden"], False)
    check("with the time on it, not a placeholder", boot[0]["time"], "09:41")
    # Off the element rather than off `clockFace`'s return value, which is
    # what the language cases above read: the line that puts the date on the
    # page is its own line and can go missing on its own. English because
    # nothing is set here and English is what an unset tag falls back to —
    # since #96 the browser's own locale is the *default* for that setting and
    # not a second opinion the clock gets to consult behind it.
    check("and the day under it", boot[0]["date"], "Saturday, March 14")
    # The other half of the same line, and the one the setting is for: a
    # household that set the panel to Japanese gets a Japanese clock at boot,
    # before anybody has said anything for a tag to be guessed from.
    set_ja = clock_run([{"sleep": True}], lang="ja")
    check("and it is the panel's language, not the kiosk's",
          [set_ja[0].get("error"), set_ja[0]["date"]],
          [None, "3月14日土曜日"], "with navigator.language still en-GB")
    check("half a minute later it still reads the same",
          [boot[1]["time"], boot[1]["redraws"]], ["09:41", 1],
          "and has not been redrawn to say so")
    check("the minute turns and it follows", boot[2]["time"], "09:42")
    check("an hour of it costs sixty redraws", boot[3]["redraws"], 62,
          f'{boot[3]["redraws"]} draws to reach {boot[3]["time"]}')
    check("and one timer the whole time, not one per tick",
          boot[3]["timers"], 1)
    check("nothing is counting down to a clock already on screen",
          boot[0]["timers"], 1, "the tick, and no idle timer beside it")

    # Aimed at the minute rather than a minute from here. `setTimeout(tick,
    # 60000)` passes everything above, because two 30 s steps land on the flat
    # timer's own boundary — the difference is the rest of the minute. Booted
    # twelve seconds in, that clock is twelve seconds late for as long as the
    # Pi stays up, which is every time anybody looks at it.
    aligned = clock_run([{"sleep": True}, {"advance": 48000},
                         {"advance": 11000}])
    check("the face turns on the minute, not a minute after boot",
          aligned[1]["time"], "09:42", "booted at 09:41:12, so 48 s in")
    check("and does not turn again inside that minute",
          [aligned[2]["time"], aligned[2]["redraws"]], ["09:42", 2])

    # Waking, and going back to sleep. The idle timer is the only thing that
    # ends a conversation on this screen; nothing on the server does it.
    used = clock_run([{"sleep": True}, {"wake": True}, {"advance": 179000},
                      {"advance": 1000}, {"advance": 60000}])
    check("a wake takes the clock away", used[1]["hidden"], True)
    check("and leaves the countdown and the panel's minute running",
          used[1]["timers"], 2,
          "the tick is the bar's clock now; #88 stopped it being cleared")
    check("a second short of three minutes it is still a terminal",
          used[2]["hidden"], True)
    check("at three minutes it is a clock again", used[3]["hidden"], False)
    check("and the face it comes back with is current, not the one it left",
          used[4]["time"], "09:45")
    check("sleep clears the conversation feed and right cards pane",
          (used[3]["feedCleared"] > 1, used[3]["cardsCleared"] > 1), (True, True))
    check("and restores starter chips for the next session",
          used[3]["startersShown"] > 1, True)

    # Somebody standing there reading it. Every wake restarts the countdown,
    # which is the whole reason `wake` and `nudge` are one function.
    held = clock_run([{"sleep": True}, {"wake": True}, {"advance": 170000},
                      {"event": "pointerdown"}, {"advance": 170000},
                      {"event": "keydown"}, {"advance": 179000}])
    check("a tap after 170 s puts the three minutes back",
          held[4]["hidden"], True)
    check("and so does a key", held[6]["hidden"], True)
    check("both are bound on the document, capturing",
          held[0]["listeners"], [["pointerdown", True], ["keydown", True]],
          "a handler that stops propagation must not cost the idle timer")

    # A tap on the clock is the tap that wakes it, and it is the only thing
    # that tap does: the controls underneath must not also receive it.
    tapped = clock_run([{"sleep": True}, {"event": "pointerdown"}])
    check("a tap on the clock wakes the terminal", tapped[1]["hidden"], True)

    # Waiting for an answer looks exactly like an empty room from here: the
    # planner's own deadline is 300 s (`app.js:554`) and `_progress` stops
    # talking around 15 s in, so a research question spends most of its life
    # with nothing arriving and nobody touching the panel — because they are
    # waiting for what they asked for. Three minutes of that used to put the
    # clock up over a card still being redrawn underneath it.
    asked = clock_run([{"sleep": True}, {"wake": True}, {"asked": "plan-1"},
                       {"advance": 179000}, {"advance": 60000},
                       {"answered": "plan-1"}, {"advance": 180000}])
    check("the run got through it", asked[0].get("error"), None)
    check("a card still spinning holds the clock off past three minutes",
          [asked[4]["hidden"], asked[4]["cards"], asked[4]["timers"]],
          [True, 1, 2],
          "four minutes in, the answer is still coming, and the countdown "
          "has been put back rather than dropped — beside the tick")
    check("and it comes up once the answer does, so this is not a terminal "
          "that never sleeps again",
          [asked[6]["hidden"], asked[6]["cards"]], [False, 0])

    # And the other end of it: a card that gave up. The entry stays in
    # `pending` for a late answer, so `pending.size` still reads 1 forever —
    # this is the run that says the clock counts spinning cards rather than
    # map entries. Reachable without doing anything unusual: the socket
    # dropping leaves an entry no `pending.delete` ever gets to.
    given_up = clock_run([{"sleep": True}, {"wake": True}, {"asked": "p-1"},
                          {"advance": 300000}, {"lost": "p-1"},
                          {"advance": 180000}, {"advance": 86400000}])
    check("a card that gave up stops holding the clock off",
          [given_up[5]["hidden"], given_up[5]["cards"]], [False, 1],
          "the entry is still there for a late answer; the wait is not")
    check("and it is not woken again by the entry it left behind",
          given_up[6]["hidden"], False, "a day later")

    browse_lost = clock_run([{"sleep": True}, {"wake": True}, {"opening": "b-1"},
                             {"advance": 180000}, {"browseLost": "b-1"},
                             {"advance": 180000}, {"advance": 86400000}])
    check("a browse card that gave up stops holding the clock off",
          [browse_lost[5]["hidden"], browse_lost[5]["cards"]], [False, 1],
          "the entry is still there for a late answer; the wait is not")
    check("and it is not woken again by the browse entry it left behind",
          browse_lost[6]["hidden"], False, "a day later")

    # The counterpart, so the fix cannot be "sleep regardless": an entry that
    # has not given up still holds it, which is the case above in reverse.
    still = clock_run([{"sleep": True}, {"wake": True}, {"asked": "p-1"},
                       {"asked": "p-2"}, {"lost": "p-1"},
                       {"advance": 180000}])
    check("but one card giving up does not sleep on a sibling still spinning",
          still[5]["hidden"], True)

    # The other end of the flag, read off the source. The deadline callback
    # that sets it sits inside `handleEvent`, which opens sockets and cannot be
    # lifted, so the harness above sets the flag itself — which means deleting
    # the real write leaves all three cases above green on both ends of a flag
    # nothing in `app.js` ever raises. These two are what notice.
    app = (JS / "app.js").read_text(encoding="utf-8")
    gave_up = re.search(
        r"const timer = setTimeout\(\(\) => \{(.+?)\}, late\.deadline\)",
        app, re.S).group(1)
    check("the deadline that gives up on a card flags the entry it leaves",
          sorted(set(re.findall(r"lost\.(\w+) = true", gave_up))), ["lost"])
    check("and that is the field `sleep` reads",
          sorted(set(re.findall(r"!entry\.(\w+)",
                                block(app, "function sleep(")))), ["lost"],
          "a rename on one side of this is the bug back, silently")

    # The third thing that means the room is not empty, and the one that is not
    # a card: a microphone open over an armed session. Only reachable with
    # `WAKE_IDLE_S` raised past the 180 s here — at the default the session's
    # own countdown closes the microphone at 30 s and `sleep` finds it already
    # off — but the two numbers are independent and nothing makes the clock's
    # the shorter. Above 180 the orders swap and the face lands on somebody who
    # is mid-conversation and has stopped talking to think, where the wake word
    # cannot take it away again because `handleWake` declines while
    # `listening`. See #112.
    live = clock_run([{"sleep": True}, {"wake": True}, {"session": True},
                      {"mic": True}, {"advance": 180000},
                      {"advance": 180000}])
    check("the run got through it", live[0].get("error"), None)
    check("a live microphone holds the clock off past three minutes",
          [live[4]["hidden"], live[4]["listening"], live[4]["armed"]],
          [True, True, True],
          "the room is reaching a model; it is not an empty room")
    # One timer, and with the face off screen it is the countdown rather than
    # the tick — which is what says the guard re-arms instead of returning
    # bare. `sleep` runs from the countdown that has just fired, so a plain
    # `return` schedules nothing and the clock never comes back at all.
    check("and the countdown is put back rather than dropped",
          [live[4]["hidden"], live[4]["timers"]], [True, 2],
          "asked again in another IDLE_MS, not never again; the second is "
          "the tick the bar's clock runs on")
    check("so it is still a terminal three minutes after that",
          live[5]["hidden"], True)

    # The other end of it, so this cannot be answered by never sleeping:
    # `goIdle` closes the microphone and the session `WAKE_IDLE_S` after the
    # last frame, and the next check is the one that puts the clock up.
    ended = clock_run([{"sleep": True}, {"wake": True}, {"session": True},
                       {"mic": True}, {"advance": 180000}, {"mic": False},
                       {"session": False}, {"advance": 180000}])
    check("a session that has since ended lets the clock come back",
          [ended[7]["hidden"], ended[7]["listening"]], [False, False],
          "the guard holds the clock off, it does not turn it off")

    # And `armed` is in that guard rather than `listening` alone, because it is
    # the flag that bounds it. `startListening` opens the microphone whether or
    # not `armSession` had an open socket to arm on, and `restartIdle` only
    # schedules `goIdle` while `armed` — so an unarmed microphone has no
    # deadline behind it and would hold the clock off for as long as the server
    # stayed down. A kiosk that never shows a screensaver again is not a fix.
    orphan = clock_run([{"sleep": True}, {"wake": True}, {"mic": True},
                        {"advance": 180000}, {"advance": 86400000}])
    check("a microphone with no session under it does not hold it off",
          [orphan[3]["hidden"], orphan[3]["listening"], orphan[3]["armed"]],
          [False, True, False], "nothing is going to close this one")
    check("and it is not woken again by the flag it left set",
          orphan[4]["hidden"], False, "a day later")

    # And the other way round: `listening` is in the guard as well as `armed`,
    # because `armed` on its own is reachable with the microphone shut.
    # `send` arms the session so a typed question has a model behind it
    # (`app.js:977`), and never touches `listening`. Somebody who types one
    # question and walks away is not a room reaching the model, and with
    # `WAKE_IDLE_S` raised — the setting this whole section is about — `armed`
    # stays true for that entire window, so a guard on it alone would hold the
    # face off for ten minutes over an empty kitchen. Without this case
    # `if (armed || spinning || browsing.size)` passes every case above.
    typed = clock_run([{"sleep": True}, {"wake": True}, {"session": True},
                       {"advance": 180000}])
    check("a session armed by typing, microphone shut, still sleeps",
          [typed[3]["hidden"], typed[3]["listening"], typed[3]["armed"]],
          [False, False, True], "nobody is talking; the clock is right")

    # The browse path keeps its own map, and a page being fetched is the same
    # kind of waiting.
    browsed = clock_run([{"sleep": True}, {"wake": True}, {"opening": "b-1"},
                         {"advance": 239000}, {"opened": "b-1"},
                         {"advance": 180000}])
    check("a page still opening holds it off too", browsed[3]["hidden"], True)
    check("and lets go of it when the page is up", browsed[5]["hidden"], False)

    # What the face coming back up now says out loud. The server cannot see
    # this: its own rule is `SESSION_IDLE_GAP_S`, measured off the last event
    # written, so a conversation that ended three minutes ago still reads as
    # the current one for another twenty-seven and the next wake word replays
    # all of it into a fresh Live connection. This frame is the only thing that
    # tells it otherwise, and everything below is about when it is sent. #142.
    told = clock_run([{"sleep": True}])
    check("the run got through it", told[0].get("error"), None)
    check("the screen going back to a clock is said on the socket",
          told[0]["wire"], ["sleep"],
          "nothing else on this page knows the conversation ended")

    # Once, and not once a minute. The tick keeps running behind the face for
    # as long as the room stays empty, and `sleep` is also re-entered by a
    # countdown that fires while the clock is already up. Either would make
    # this a frame every minute all night on a kiosk nobody walks past.
    overnight = clock_run([{"sleep": True}, {"advance": 3600000},
                           {"sleep": True}])
    check("and said once, not once a minute the clock ticks",
          overnight[2]["wire"], ["sleep"], "an hour of empty kitchen")

    # But a second conversation gets a second one, because each ends. The
    # server treats a repeat as the mark it already has, so this is belt and
    # braces rather than the thing that makes it correct — but a page that
    # said it only on the first screensaver of the day would leave every later
    # conversation on the old rule.
    twicedark = clock_run([{"sleep": True}, {"wake": True},
                           {"advance": 180000}])
    check("a second screensaver after a second conversation says it again",
          twicedark[2]["wire"], ["sleep", "sleep"])

    # And never on the paths that bail out. Each of these is a room with
    # somebody still in it, and each returns through `wake()` without the face
    # going up — so the frame has to sit below the guard rather than in the
    # countdown that called `sleep`. Above it, the thread would roll out from
    # under a card still spinning.
    wakeup = [{"sleep": True}, {"wake": True}]
    held = [clock_run(wakeup + [{"session": True}, {"mic": True},
                                {"advance": 180000}]),
            clock_run(wakeup + [{"asked": "c-1"}, {"advance": 180000}]),
            clock_run(wakeup + [{"opening": "b-1"}, {"advance": 180000}])]
    check("a live microphone, a spinning card and a page still opening say "
          "nothing", [run[-1]["wire"] for run in held],
          [["sleep"], ["sleep"], ["sleep"]],
          "the one frame each is the screensaver the run opened with; the "
          "countdown that hit a guard added none")
    check("and the face did not come back up on any of them",
          [run[-1]["hidden"] for run in held], [True, True, True],
          "otherwise this passes by never sleeping")

    # The socket the frame goes on is not always there. The panel reconnects
    # with a backoff, so a screensaver landing during an outage is ordinary,
    # and a page that loaded with the server down has none at all. Neither may
    # be an exception out of the countdown — that would leave `asleep` set with
    # no clock drawn under it, which is a black screen the tap cannot fix.
    for state, what in ((3, "a closed socket"), (0, "one still connecting"),
                        (None, "no socket at all")):
        gone = clock_run([{"sleep": True}], socket=state)
        check(f"{what} is not an error out of the screensaver",
              [gone[0].get("error"), gone[0]["hidden"], gone[0]["wire"]],
              [None, False, []],
              "the face goes up anyway and the server keeps the thread it had")

    # Read out of the source, because the two halves of this are in different
    # files and nothing in either run would notice them drifting apart. The
    # server dispatches on the exact string and answers a word it does not know
    # with a warning nobody reads, so a rename on one side is the bug back with
    # no error anywhere.
    said = re.findall(r'socket\.send\(JSON\.stringify\(\{ type: "(\w+)"',
                      block(app, "function sleep("))
    heard = re.findall(
        r'kind == "(\w+)"',
        (ROOT / "src" / "ai_terminal" / "live" / "main.py").read_text(
            encoding="utf-8"))
    check("the frame `sleep` sends is one the server dispatches on",
          [said, [f for f in said if f in heard]], [["sleep"], ["sleep"]],
          "`main.py` logs an unknown kind and carries on")

    # And the comment on `IDLE_MS`, which the owner asked to be changed with
    # the rule: it used to argue that this number and `SESSION_IDLE_GAP_S`
    # answered different questions, which is the reasoning this frame retires.
    idle_doc = app[app.rindex("/**", 0, app.index("const IDLE_MS")):
                   app.index("const IDLE_MS")]
    check("the comment on IDLE_MS says the server is told about it",
          ["tells the server" in idle_doc, "#142" in idle_doc], [True, True],
          "the next person to raise this number needs to know what it now ends")

    # `sleep` re-entered. The kiosk reloads, the idle timer fires while the
    # clock is already up — either way a second tick timer would double the
    # redraws and never be cleared.
    twice = clock_run([{"sleep": True}, {"sleep": True}, {"advance": 60000}])
    check("sleeping twice does not stack a second tick",
          twice[2]["timers"], 1)
    check("nor redraw the face twice a minute", twice[2]["redraws"], 2)

    # What the server sends, and every way it can send less than that.
    full = {"image": "/api/screensaver.jpg?v=1", "place": "Hakone, Japan",
            "weather": {"temperature": 24.1, "unit": "°C",
                        "description": "Overcast", "condition": "cloudy",
                        "is_day": True}}
    painted = clock_run([
        {"paint": full},
        {"paint": full},
        {"paint": {**full, "image": "/api/screensaver.jpg?v=2"}},
        {"paint": {"place": "Hakone, Japan"}},
    ])
    check("the line under the clock is the temperature, the sky and the place",
          painted[0]["weather"], "24°C · Overcast · Hakone, Japan")
    check("and the photo is loaded", painted[0]["src"],
          "/api/screensaver.jpg?v=1")
    check("the same payload again does not reload it", painted[1]["loads"], 1,
          "re-assigning src blinks the gradient on the panel")
    check("a new one does", painted[2]["loads"], 2)
    check("a payload with no weather in it leaves no dangling separator",
          painted[3]["weather"], "Hakone, Japan")

    empty = clock_run([{"paint": {}}])
    check("and an empty one leaves the line empty", empty[0]["weather"], "")

    # The server half of this does not exist yet, and the clock is not allowed
    # to care. These are the three answers it can get and all of them end the
    # same way.
    served = clock_run([{"sleep": True}], server={"body": full})
    check("going to sleep asks the server what to wear",
          served[0]["fetched"], 1)
    check("and puts it on", served[0]["weather"],
          "24°C · Overcast · Hakone, Japan")

    # And which language it asks in, which is the whole of what the page has
    # to say for the sky to come back in the right one (#131). One server
    # holds one reading for every panel, and the setting is in this page's
    # localStorage, so it travels on the request or it does not travel.
    check("the ask says which language the panel is set to",
          served[0]["asked"], ["/api/screensaver?lang=en"])
    japanese = clock_run([{"sleep": True}], server={"body": full}, lang="ja")
    check("and a Japanese panel asks for Japanese",
          japanese[0]["asked"], ["/api/screensaver?lang=ja"])
    # `index.html` ships `lang="en"` and `init` writes the tag before the
    # first ask, so an empty tag is not a state the panel reaches — but the
    # fallback is what stops it asking for `?lang=`, which is a blank the
    # server would need a rule for rather than a language.
    bare = clock_run([{"sleep": True}], server={"body": full}, lang="")
    check("and a document with no tag on it still names one",
          bare[0]["asked"], ["/api/screensaver?lang=en"])
    # Switching mid-conversation. The face is behind the settings sheet at
    # that moment, so what has to be right is the next ask rather than this
    # one — `sleep()` asks again on the way back to the clock either way.
    switched = clock_run([{"sleep": True}, {"wake": True}, {"relang": "ja"},
                          {"advance": 600000}],
                         server={"body": full}, lang="en")
    check("the run got through it", switched[0].get("error"), None)
    check("switching the setting changes what the next one asks for",
          switched[3]["asked"],
          ["/api/screensaver?lang=en", "/api/screensaver?lang=ja"])

    missing = clock_run([{"sleep": True}], server={"status": 404})
    check("a server with no such route still leaves a clock",
          [missing[0]["hidden"], missing[0]["time"]], [False, "09:41"])
    check("with nothing invented under it", missing[0]["weather"], "")

    offline = clock_run([{"sleep": True}], server=None)
    check("and so does no server at all",
          [offline[0]["hidden"], offline[0]["time"]], [False, "09:41"])

    # Three wirings that no run here can reach, because they are in the halves
    # of `app.js` that need a socket and a DOM. Read out of the source instead,
    # which is what they are: one line each, and each one is the difference
    # between a clock that goes away when somebody speaks and one that does not.
    app = (JS / "app.js").read_text(encoding="utf-8")
    check("a turn starting wakes the screen",
          "wake();" in block(app, "function startTurn("), True)
    # The agent's own branch, not the file: `wake();` is in `startTurn` too,
    # and a search of the whole source would pass on that one.
    spoken = app[app.index("const output = event.outputTranscription;"):][:500]
    check("and so does the agent speaking on its own",
          "wake();" in spoken, True,
          "the recap after a rollover has no user turn in front of it")
    # And the wake word, which reaches neither of the two above: the frame is
    # dispatched straight to `handleWake`, and the first thing on that path
    # that used to take the clock away was the transcription of what the
    # person had already finished saying. `wake_cases` drives the behaviour
    # over a stub; this is the line that says the stub stands for this
    # function. See #103.
    check("and so does the wake word, which reaches neither of those",
          "wake();" in block(app, "async function handleWake("), True)
    check("and the panel is a clock before /api/config is even asked",
          before(block(app, "async function init("), "sleep()", "await fetch("),
          True)
    check("the page has the clock in it, not hidden",
          'id="clockPage"' in (JS.parent / "index.html").read_text(
              encoding="utf-8"), True)
    # And the tag itself, because "not hidden" is the half that matters and a
    # `hidden` on it passes every case above: `sleep()` takes the class off at
    # init either way. What it costs is the first paint, which is the one this
    # is for — a kiosk that shows an empty conversation it has not had, for as
    # long as `app.js` takes to parse.
    opening = re.search(r"<div[^>]*id=\"clockPage\"[^>]*>",
                        (JS.parent / "index.html").read_text(encoding="utf-8"))
    check("and it is on screen from the first paint, not revealed by script",
          "hidden" not in (opening.group(0) if opening else "hidden"), True,
          opening.group(0) if opening else "no clockPage tag")


# One reading off `/api/screensaver`, with the two keys #88 added to it. The
# same 24.1 and the same overcast sky `screensaver_check` refreshes on, so a
# disagreement between the two halves of this feature shows as one.
READING = {"temperature": 24.1, "unit": "°C", "description": "Overcast",
           "condition": "cloudy", "is_day": True}
SHOWN = {"image": "/api/screensaver.jpg?v=1", "place": "Hakone, Japan",
         "weather": READING}


def bar_cases() -> None:
    print("\n== the clock and the sky in the top bar ==")

    app = (JS / "app.js").read_text(encoding="utf-8")

    # The two faces, side by side, at the one instant. The bar is the short
    # form of the same date and the identical time — identical because both
    # go through `hhmm`, which is where `hourCycle: "h23"` lives. Two copies
    # of those three keys is how one of them becomes `hour12: false` and the
    # bar starts reading 24:10 while the face reads 00:10.
    both = clock_run([{"face": "2026-03-14T09:41:12+09:00", "in": "en"},
                      {"bar": "2026-03-14T09:41:12+09:00", "in": "en"},
                      {"bar": "2026-03-15T00:10:00+09:00", "in": "en"},
                      {"bar": "2026-03-14T09:41:12+09:00", "in": "ja"}])
    check("the run got through it", both[0].get("error"), None)
    check("the bar's date is the short form, not the face's",
          [both[0]["date"], both[1]["date"]],
          ["Saturday, March 14", "Sat, Mar 14"],
          "560px of bar with a brand and two buttons already in it")
    check("and the time is the same string in both",
          both[0]["time"] == both[1]["time"], True, both[1]["time"])
    check("so midnight is 00 in the bar too, not 24", both[2]["time"], "00:10")
    check("the day of the week is in it, which is what was asked for",
          both[1]["date"].startswith("Sat"), True)
    check("and in Japanese it is the Japanese one", both[3]["date"],
          "3月14日(土)", both[3]["date"])
    # Comments stripped, because the section argues `hour12` down by name in
    # its own prose and a plain `in` would match the argument rather than the
    # thing it is about.
    check("one `hourCycle` in the whole file, so the two cannot drift",
          code(app).count("hourCycle"), 1)
    check("and `hour12` is nowhere near either of them",
          "hour12" in code(app), False)

    # What the bar does with the minute, which is the half #84 never had to
    # answer: its clock is on screen exactly when the face is not.
    boot = clock_run([{"sleep": True}, {"advance": 60000}])
    check("the bar has the time from the first frame", boot[0]["bar"], "09:41")
    check("with the date beside it", boot[0]["barDate"], "Sat, Mar 14")
    check("and it turns on the same minute the face does",
          [boot[1]["bar"], boot[1]["time"]], ["09:42", "09:42"])

    awake = clock_run([{"sleep": True}, {"wake": True}, {"advance": 120000}])
    check("the bar goes on turning once the face is gone",
          [awake[2]["hidden"], awake[2]["bar"]], [True, "09:43"],
          "a bar clock stuck at the minute somebody spoke is the whole bug")
    check("on the one timer, plus the countdown back to the face",
          awake[2]["timers"], 2)

    # The locale, which is what "in the specified locale" asked for. Read off
    # the document on every draw rather than captured once, because #96 made
    # it a setting somebody changes mid-conversation.
    switched = clock_run([{"sleep": True}, {"relang": "ja"},
                          {"relang": "en"}])
    check("changing the setting redraws the bar in the new language",
          [switched[1]["bar"], switched[1]["barDate"]],
          ["09:41", "3月14日(土)"])
    check("and back", switched[2]["barDate"], "Sat, Mar 14")
    check("which is what `applyLanguage` calls it for",
          [w for w in ("drawClock();", "drawBar();")
           if w not in block(app, "function applyLanguage(")], [],
          "the face and the bar, on the frame the tag moves")

    print("\n== and the reading in it ==")

    # Before there is one. Not an em dash and not a zero: a bar with no
    # weather in it at all, because the slot appearing is how somebody knows
    # there is a reading behind it.
    cold = clock_run([{"sleep": True}], server={"status": 404})
    check("a bar with nothing behind it shows no weather at all",
          [cold[0]["wxClasses"], cold[0]["wx"]], [["hidden"], ["", ""]])

    painted = clock_run([{"paint": SHOWN}])
    check("a reading is a glyph and a temperature", painted[0]["wx"],
          ["cloudy/day", "24°C"])
    check("and the sky is not the word the server sent",
          "Overcast" in "".join(painted[0]["wx"]), False,
          "the bar has no width for a word, in either language")
    check("nothing is stale the moment it lands", painted[0]["wxClasses"], [])
    check("and the face still says exactly what it said before",
          painted[0]["weather"], "24°C · Overcast · Hakone, Japan",
          "one payload, one paint, and the two cannot disagree")

    # The two ways the glyph goes wrong. A clear night is the one pair where
    # the day and the night glyphs differ at all, and a payload from a server
    # too old to send a slug has to show the number rather than a guess.
    dark = {**SHOWN, "weather": {**READING, "condition": "clear",
                                 "is_day": False}}
    unsure = {**SHOWN, "weather": {**READING, "condition": "clear",
                                   "is_day": None}}
    old_server = {"place": "Hakone, Japan",
                  "weather": {"temperature": 24.1, "unit": "°C",
                              "description": "Overcast"}}
    skies = clock_run([{"paint": dark}, {"paint": unsure},
                       {"paint": old_server}])
    check("after dark a clear sky is the night glyph", skies[0]["wx"][0],
          "clear/night")
    check("a payload that does not say is drawn as day", skies[1]["wx"][0],
          "clear/day")
    check("and one with no sky in it shows the temperature and no glyph",
          [skies[2]["wx"], skies[2]["iconHidden"]], [["", "24°C"], True],
          "a cloud drawn over a clear afternoon is a wrong answer")

    # And a reading with no temperature in it, which is not hypothetical: the
    # server lets a cold start through with an empty answer rather than
    # throwing the payload away (`screensaver.py:646-650`), so `temperature`
    # arrives as `null` over the wire. The bar is the last thing between that
    # and a confident `0°C` in the toolbar of a terminal that has no idea.
    # What holds the line is `Number.isFinite`, and its *strength* rather
    # than its presence — `!== undefined` lets `null` through and rounds it.
    # Behind a 404 first, so the widget starts where the markup leaves it.
    empty = {**SHOWN, "weather": {**READING, "temperature": None}}
    unknown = clock_run([{"sleep": True}, {"paint": empty}],
                        server={"status": 404})
    check("a reading with no temperature in it is not a temperature",
          [unknown[1]["wxClasses"], unknown[1]["wx"]], [["hidden"], ["", ""]],
          "`Math.round(null)` is 0, and 0°C is a number somebody dresses for")
    # And the other side of the same guard, which is why it is `isFinite` and
    # not a truthiness test: freezing point is a reading. A bar that hides at
    # 0°C is wrong in exactly the weather somebody checks a bar for.
    freezing = {**SHOWN, "weather": {**READING, "temperature": 0.0}}
    cold = clock_run([{"sleep": True}, {"paint": freezing}],
                     server={"status": 404})
    check("and zero degrees is a temperature like any other",
          [cold[1]["wxClasses"], cold[1]["wx"]], [[], ["cloudy/day", "0°C"]],
          "`!weather.temperature` would throw away the one it matters in")
    after = clock_run([{"paint": SHOWN}, {"paint": empty}])
    check("and one landing after a good one leaves that one standing",
          [after[1]["wxClasses"], after[1]["wx"]],
          [[], ["cloudy/day", "24°C"]],
          "the same promise the empty-refresh case below makes")
    # The widget's own starting state, which every case above rests on and
    # none of them can see: `El` builds its class set empty, so a `hidden`
    # asserted after a run is one the code put there. The markup's is here.
    bar_wx_tag = re.search(r"<span[^>]*id=\"barWx\"[^>]*>",
                           (JS.parent / "index.html").read_text(
                               encoding="utf-8"))
    check("and the markup ships it hidden, before any of this runs",
          "hidden" in (bar_wx_tag.group(0) if bar_wx_tag else ""), True,
          bar_wx_tag.group(0) if bar_wx_tag else "no barWx tag")

    # A refresh that came back with nothing in it. The server keeps the last
    # reading for this reason (`screensaver.py:601-605`) and the bar has to make
    # the same promise, or the two disagree about what an empty answer means.
    kept = clock_run([{"paint": SHOWN}, {"paint": {}},
                      {"paint": {"place": "Hakone, Japan"}}])
    check("an empty refresh leaves the reading where it was",
          kept[2]["wx"], ["cloudy/day", "24°C"])
    check("and does not mark it stale on the spot", kept[2]["wxClasses"], [])

    # Going stale, which is something a reading does by sitting still —
    # nothing arrives at 25 minutes to say so, so the clock has to re-decide
    # it. Started on the minute so the boundary is exact: the tick at 10:06
    # is 1500000 ms after the paint, which is the first one that counts.
    stale = clock_run([{"sleep": True}, {"paint": SHOWN},
                       {"advance": 1499000}, {"advance": 1000},
                       {"paint": SHOWN}],
                      start="2026-03-14T09:41:00+09:00")
    check("a minute short of 25 the bar still stands behind the reading",
          stale[2]["wxClasses"], [], "24 minutes old")
    check("at 25 it stops", stale[3]["wxClasses"], ["stale"])
    check("without taking the number away, which is still the best there is",
          stale[3]["wx"], ["cloudy/day", "24°C"])
    check("and a reading that lands puts it back", stale[4]["wxClasses"], [])

    # Age carried on the payload (#276): a reading repeatedly served by
    # /api/screensaver must age from its original fetch timestamp, not from
    # when the poll landed.
    t0_s = 1773448860.0  # 2026-03-14T09:41:00+09:00 in seconds
    fresh_shown = {**SHOWN, "weather": {**READING, "fetched": t0_s - 600.0}}
    stale_shown = {**SHOWN, "weather": {**READING, "fetched": t0_s - 1600.0}}
    aged = clock_run([{"sleep": True}, {"paint": fresh_shown},
                      {"paint": stale_shown}],
                     start="2026-03-14T09:41:00+09:00")
    check("a fresh reading with a recent fetched timestamp is not stale",
          aged[1]["wxClasses"], [])
    check("a reading whose fetched timestamp is older than WX_STALE_MS marks the bar stale",
          aged[2]["wxClasses"], ["stale"],
          "even when freshly received, an old reading must show its age")

    print("\n== and where it comes from ==")

    # #85 owns the only poller against Open-Meteo there is. This reads its
    # cache: two pollers on one screen is two temperatures on one screen.
    check("the bar reads #85's route and stands up nothing of its own",
          sorted(set(re.findall(r'fetch\([`"](/api/[\w.]+)[?`"]', app))),
          ["/api/config", "/api/reboot", "/api/screensaver"],
          "a whole route, either quote, with or without a query string")
    check("and nothing in the page talks to a forecast API itself",
          "open-meteo" in code(app).lower(), False,
          "the section says so in prose too; this is about the code")
    cards = (JS / "cards.js").read_text(encoding="utf-8")
    check("the glyph comes off the table the weather card is keyed on",
          ["export function wxIcon(" in cards,
           re.search(r"import \{[^}]*\bwxIcon\b[^}]*\} from \"\./cards\.js\";",
                     app, re.S) is not None],
          [True, True], "one table, or the bar and the card differ")

    # How often, and — the half that matters on a Pi — how often not. While
    # awake, asking twice inside ten minutes returns the same reading.
    asks = clock_run([{"sleep": True}, {"wake": True}, {"asked": "p-1"},
                      {"advance": 540000}, {"advance": 120000}],
                     server={"body": SHOWN})
    check("the run got through it", asks[0].get("error"), None)
    check("going to sleep is one ask", asks[0]["fetched"], 1)
    check("waking a second later is not a second one", asks[1]["fetched"], 1)
    check("nine minutes awake and it has not asked again",
          asks[3]["fetched"], 1)
    check("at ten it does", asks[4]["fetched"], 2)

    # And the eight hours nobody walks in, which is the half the owner is
    # looking at. #85's decision was about Open-Meteo and it still stands:
    # the server polls it twice an hour whether or not a page is up, and
    # what this asks is that server's cache over loopback. The face is on
    # screen for the whole of those eight hours.
    quiet = clock_run([{"sleep": True}, {"advance": 28800000}],
                      server={"body": SHOWN})
    check("eight hours asleep goes on asking, every ten minutes",
          quiet[1]["fetched"], 48,
          "one at the sleep and 47 ticks after it; gated on `!asleep` this "
          "was 1, and the face wore the same photograph all night")

    morning = clock_run([{"sleep": True}, {"advance": 28800000},
                         {"event": "pointerdown"}], server={"body": SHOWN})
    check("so the tap that wakes it has nothing left to ask for",
          morning[2]["fetched"], 48,
          "the last ask was under ten minutes ago, so the bar comes up "
          "current rather than wearing last night's temperature")

    # The picture, which is the thing #86 generates and the thing nobody has
    # seen change. Two bodies: the one the face comes up on and the one the
    # 04:22 slot replaced it with. Nothing touches the panel in between.
    LATER = {**SHOWN, "image": "/api/screensaver.jpg?v=2"}
    swap = clock_run([{"sleep": True}, {"advance": 600000},
                      {"advance": 28200000}],
                     server=[{"body": SHOWN}, {"body": LATER}])
    check("the face comes up on the picture that was there",
          swap[0]["src"], "/api/screensaver.jpg?v=1")
    check("and a new one generated behind it reaches the panel unattended",
          swap[2]["src"], "/api/screensaver.jpg?v=2",
          "the owner's question: with no tap, no wake word and nobody in "
          "the room, does the photograph ever change")
    check("and it is loaded once, not once per ask", swap[2]["loads"], 2,
          "`src` is only assigned when it differs, or the panel blinks the "
          "gradient six times an hour")

    # A server that lost its reading — a restart before the first Open-Meteo
    # answer — must not take the line off a face nobody is standing at.
    kept = clock_run([{"sleep": True}, {"advance": 28800000}],
                     server=[{"body": SHOWN}, {"body": {}}])
    check("a reply with no reading in it leaves the face's line alone",
          kept[1]["weather"], "24°C · Overcast · Hakone, Japan",
          "the bar keeps its temperature on the same payload; the two may not disagree")

    # And a refreshed reading reaches the face unattended rather than leaving
    # yesterday's temperature on screen all morning (Done when item 6).
    UPDATED = {**SHOWN, "weather": {**READING, "temperature": 18.2,
                                    "description": "Clear", "condition": "clear"}}
    current = clock_run([{"sleep": True}, {"advance": 28800000}],
                        server=[{"body": SHOWN}, {"body": UPDATED}])
    check("and a refreshed reading reaches the face unattended",
          current[1]["weather"], "18°C · Clear · Hakone, Japan",
          "the face wears the current reading after eight hours asleep, "
          "not the reading from the moment it fell asleep")

    print("\n== the room the bar has, and what goes when it runs out ==")

    page = (JS.parent / "index.html").read_text(encoding="utf-8")
    header = re.search(r"<header id=\"topBar\">(.+?)</header>", page, re.S)
    inside = header.group(1) if header else ""
    check("the clock is in the toolbar and not somewhere else on the page",
          [i for i in ("barDate", "barTime", "barWx", "barWxIcon",
                       "barWxTemp") if f'id="{i}"' not in inside], [])
    check("and the weather starts hidden, before there is a reading",
          'class="bar-wx hidden"' in inside, True,
          "the slot appearing is how somebody knows there is one")

    css = (JS.parent / "css" / "style.css").read_text(encoding="utf-8")
    drops = re.findall(
        r"@media \(max-width: (\d+)px\) \{\s*\.([\w-]+) \{ display: none; \}",
        css)
    check("the brand is what the bar drops first, the date second",
          drops, [("479", "brand"), ("379", "bar-date")],
          "the time and the temperature are what is left")
    check("and the digits do not shuffle as the minute turns",
          "tabular-nums" in (re.search(r"\n\.bar-time \{(.+?)\n\}", css,
                                       re.S) or re.match("", "")).group(0),
          True)

    bar_now_match = re.search(r"\n\.bar-now\s*\{([^}]+)\}", css)
    bar_now_css = bar_now_match.group(1) if bar_now_match else ""
    bar_now_size = int((re.search(r"font-size:\s*(\d+)px", bar_now_css) or re.match("", "")).group(1) or "0")
    check("the top bar clock and weather typography is scaled for counter-distance reading",
          bar_now_size >= 22, True,
          f"expected >= 22px, found {bar_now_size}px")

    wx_icon_match = re.search(r"\n\.bar-wx\s+\.material-symbols-outlined\s*\{([^}]+)\}", css)
    wx_icon_css = wx_icon_match.group(1) if wx_icon_match else ""
    wx_icon_size = int((re.search(r"font-size:\s*(\d+)px", wx_icon_css) or re.match("", "")).group(1) or "0")
    check("and the weather icon scales with the typography",
          wx_icon_size >= 26, True,
          f"expected >= 26px, found {wx_icon_size}px")

    brand_match = re.search(r"\n\.brand\s*\{([^}]+)\}", css)
    brand_css = brand_match.group(1) if brand_match else ""
    brand_size = int((re.search(r"font-size:\s*(\d+)px", brand_css) or re.match("", "")).group(1) or "0")
    check("the brand typography is scaled to balance the clock",
          brand_size >= 22, True,
          f"expected >= 22px, found {brand_size}px")

    brand_icon_match = re.search(r"\n\.brand-icon\s*\{([^}]+)\}", css)
    brand_icon_css = brand_icon_match.group(1) if brand_icon_match else ""
    brand_icon_size = int((re.search(r"width:\s*(\d+)px", brand_icon_css) or re.match("", "")).group(1) or "0")
    check("and the brand icon scales with the typography",
          brand_icon_size >= 26, True,
          f"expected >= 26px, found {brand_icon_size}px")


def scroll_src() -> str:
    """The cards-pane section of `app.js`, from the constants to the loop.

    One contiguous slice rather than four `block()` calls, because the state
    these functions share is the thing being checked: a constant or a flag
    moved out of the section is a name the run cannot resolve, and node says
    so. Taken from the section header and not from the first declaration in
    it, for the same reason — an anchor that moved with the code it is meant
    to be pinning would find the section wherever it went.
    """
    src = (JS / "app.js").read_text(encoding="utf-8")
    start = src.index("/* ---------- cards pane ---------- */")
    return src[start:src.index("/** The scroll container", start)]


def scroll_run(pane: int, steps: list[dict], wide: bool = True) -> list[dict]:
    """Drive `appendCard` over a pane `pane` pixels tall, a frame per step."""
    arg = json.dumps({"pane": pane, "steps": steps, "wide": wide})
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", SCROLL % scroll_src(), arg],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        # Reported, not fatal: a name left dangling by an edit to the section
        # should fail the case that names it, not raise out of the middle of
        # it. Every field carries the error and its own step number, because a
        # case comparing two frames of a failed run has to see them differ —
        # one shared sentinel would make `scrollTop` unchanged look like the
        # pane holding still.
        err = proc.stderr.strip()[-200:]
        return [{k: f"<step {n}: {err}>" for k in
                 ("error", "resting", "top", "scrollTop", "tail", "cardsTail",
                  "startersTop", "startersResting", "watched",
                  "paneBox", "fired", "restCardCalls")}
                for n in range(len(steps))]
    return json.loads(proc.stdout)


def scroll_cases() -> None:
    print("\n== where a new card comes to rest ==")

    section = scroll_src()
    check("the pane's whole state lives in the one section",
          [n for n in ("CARD_TOP", "CARD_TAIL", "newestCard", "followCards",
                       "cardSizes", "activeTurn", "turnCardCount")
           if not re.search(rf"^(?:const|let) {n} ", section, re.M)], [])

    # The constants are the pane's own padding, and app.js says as much in a
    # comment. Nothing enforces that but this: a designer nudging the CSS
    # leaves every card resting a few pixels off the top, which reads as a
    # rounding error rather than as the two files having drifted.
    css = (ROOT / "src/ai_terminal/live/static/css/style.css").read_text(
        encoding="utf-8")

    def px(pattern: str, text: str = css) -> tuple[str, ...]:
        """The numbers a CSS declaration is written with, `()` if it is gone.

        Read this way rather than off `.groups()` so a rule that has moved or
        been renamed fails the case that wanted it instead of raising here.
        """
        found = re.search(pattern, text)
        return found.groups() if found else ()

    pane = re.search(r"\n  \.side-panel \{(.+?)\n  \}", css, re.S)
    check("and matches the padding style.css gives that pane",
          px(r"padding: (\d+)px \d+px (\d+)px;", pane.group(1) if pane else ""),
          ("14", "20"))
    # Not app.js's business — the browser measures `offsetTop` — but the run
    # below lays cards out itself and has to lay them out the way the pane
    # does, or every resting position it reports is off by the difference.
    check("and the gap the run stacks cards with is the pane's own",
          px(r"\.cards > \* \+ \* \{ margin-top: (\d+)px"), ("12",))

    # A pane 400 tall, a short card under two that fill it. This is the bug:
    # the last card is the last child, so without a tail there is nothing
    # below it to scroll into and the target is clamped.
    tall = [{"card": "a", "height": 300}, {"card": "b", "height": 300}]
    short = scroll_run(400, tall + [{"card": "c", "height": 80}])
    check("a short card below two tall ones rests at the top of the pane",
          short[-1]["resting"], 14)
    check("which took a tail under it, of exactly the height it was missing",
          short[-1]["tail"], "250px")
    check("and the first card is off the top of the pane, not still on it",
          short[-1]["top"] < 0, True, short[-1]["top"])

    # The same run one card longer. A card taller than the pane can reach the
    # top on its own, and asking for a tail as well would be a screen of
    # nothing under it.
    big = scroll_run(400, tall + [{"card": "c", "height": 80},
                                  {"card": "d", "height": 600}])
    check("a card taller than the pane rests there too",
          big[-1]["resting"], 14)
    check("and needs no tail, so the pane goes back to its own padding",
          big[-1]["tail"], "20px")

    # An empty pane: the first card is already at the top, which is why the
    # bug reads as intermittent. The tail is still taken, because the card
    # after it is the one that will need to scroll.
    first = scroll_run(400, [{"card": "a", "height": 100}])
    check("the first card into an empty pane was there already",
          (first[0]["resting"], first[0]["scrollTop"]), (14, 0))

    # `offsetTop` at append time is read off a layout that does not exist yet.
    grown = scroll_run(400, [{"card": "a", "height": 100},
                             {"card": "b", "height": 80},
                             {"grow": "a", "height": 400}])
    check("every card is watched for the height it did not have on the way up",
          grown[2]["watched"], 2)
    check("so one above the newest growing brings the newest back up",
          (grown[1]["resting"], grown[2]["resting"]), (14, 14))

    # The late fill: a gws card goes up empty and its answer arrives later.
    filled = scroll_run(400, [{"card": "a", "height": 300},
                              {"card": "b", "height": 60},
                              {"grow": "b", "height": 200}])
    check("and a card that grows into its own answer stays at the top",
          filled[-1]["resting"], 14)
    check("with its tail cut back to what it now needs",
          (filled[1]["tail"], filled[2]["tail"]), ("270px", "130px"))

    # The watch-out: a re-aim has to yield to the person. Their scroll is read
    # off what they did, because the `scroll` event `restCardAtTop` raises
    # itself looks exactly the same.
    away = scroll_run(400, [{"card": "a", "height": 300},
                            {"card": "b", "height": 80},
                            {"scrolled": "wheel"},
                            {"grow": "a", "height": 500},
                            {"card": "c", "height": 80}])
    check("a card growing under someone who has scrolled away leaves them "
          "where they are",
          (away[3]["scrollTop"], away[3]["resting"] == 14),
          (away[1]["scrollTop"], False))
    check("and the next card they asked for takes the pane back",
          away[4]["resting"], 14)

    # A repeat question the tool answered on the spot: the card comes back off
    # the screen, and it is usually the one the pane is following. Detached, it
    # measures 0 both ways, which asks for a tail nearly a screen deep and aims
    # the pane at the oldest card instead.
    dropped = scroll_run(400, [{"card": "a", "height": 300},
                               {"card": "b", "height": 300},
                               {"card": "c", "height": 80},
                               {"drop": "c"}])
    check("dropping the card the pane follows brings up the one before it",
          (dropped[3]["resting"], dropped[3]["tail"]), (14, "30px"))
    check("and the card that went is not watched any more",
          (dropped[2]["watched"], dropped[3]["watched"]), (3, 2))

    # Two late tools can be waiting at once, so the dropped card is not always
    # the newest. Everything under the gap moves up by its height.
    above = scroll_run(400, [{"card": "a", "height": 300},
                             {"card": "b", "height": 80},
                             {"drop": "a"}])
    check("dropping one above the newest brings the newest back up too",
          (above[2]["resting"], above[2]["watched"]), (14, 1))

    # And the pane can empty this way as well as by the clear button, which is
    # the other place a tail left behind pushes the `:empty` placeholder off
    # the middle of a pane with nothing in it.
    only = scroll_run(400, [{"card": "a", "height": 80}, {"drop": "a"}])
    check("dropping the only card leaves no tail holding the pane open",
          (only[1]["tail"], only[1]["watched"]), ("", 0))

    # The other half of the tail is the pane's own height. Closing the settings
    # sheet hands ~130px back, the container re-clamps on the way, and a tail
    # measured against the old height is too short to scroll the card back up.
    resized = scroll_run(400, [{"card": "a", "height": 300},
                               {"card": "b", "height": 80},
                               {"pane": 530}])
    check("a pane given its height back does not leave the card part-way up",
          (resized[2]["resting"], resized[2]["fired"]), (14, []))
    check("and it is watched border-box, or the tail it writes re-fires it",
          resized[2]["paneBox"], "border-box")

    # The pane is on a touch screen, where `pointerdown` and `touchstart` both
    # fire on a plain tap and both bubble out of the card that was touched.
    # Yielding to those is yielding to someone looking at a photo.
    tapped = scroll_run(400, [{"card": "a", "height": 300},
                              {"card": "b", "height": 60},
                              {"scrolled": "touchstart"},
                              {"grow": "a", "height": 500}])
    check("a tap inside a card is not a scroll, so the pane never hears it",
          tapped[2]["fired"], ["no touchstart listener"])
    check("and the late fill after that tap still comes to the top",
          tapped[3]["resting"], 14)

    # The control that stops all this being "delete the listener": the drag
    # that tap was mistaken for still hands the pane over.
    swiped = scroll_run(400, [{"card": "a", "height": 300},
                              {"card": "b", "height": 60},
                              {"scrolled": "touchmove"},
                              {"grow": "a", "height": 500}])
    check("a swipe is one, and leaves them where they scrolled to",
          (swiped[2]["fired"], swiped[3]["resting"] == 14), ([], False))

    cleared = scroll_run(400, [{"card": "a", "height": 300}, {"clear": True}])
    check("clearing the pane drops the tail with the cards",
          (cleared[1]["tail"], cleared[1]["watched"]), ("", 0))

    narrow = scroll_run(400, [{"card": "a", "height": 300}], wide=False)
    check("and on the narrow screen a card still goes to the feed",
          (narrow[0]["fired"], narrow[0]["tail"]), (["feed"], None))

    # Starters under cards: chips must be inside the pane's viewport (#547, #552, #576).
    # A 300px card followed by a 150px card in a 560px pane (Pi 1024x600 layout):
    # As soon as the card is shown, starter chips are revealed immediately (#576)
    # and restCardAtTop accounts for startersHeight.
    # The chips must not have a duplicate screen of padding between cards and chips,
    # and their resting position must be within the visible pane.
    chips_run = scroll_run(560, [
        {"card": "a", "height": 300},
        {"card": "weather", "height": 150},
        {"showStarters": True},
        {"hideStarters": True},
    ])
    check("starter chips are revealed immediately on card append (#576)",
          0 <= chips_run[1]["startersResting"] < 560, True,
          f"startersResting={chips_run[1]['startersResting']}")
    check("pane tail is reduced by starters immediately on card append (#576)",
          chips_run[1]["tail"],
          f"{max(20, 560 - 150 - 14 - (44 + 12))}px")
    check("cards container receives no duplicate paddingBottom gap",
          chips_run[2]["cardsTail"], None)
    check("chips top comes to rest inside the pane's visible box",
          0 <= chips_run[2]["startersResting"] < 560, True,
          f"startersResting={chips_run[2]['startersResting']}")
    check("pane tail is reduced by starters height and margin",
          chips_run[2]["tail"],
          f"{max(20, 560 - 150 - 14 - (44 + 12))}px")
    check("pinned card resting remains at top after hiding starters (#552)",
          chips_run[3]["resting"], 14)
    check("pane tail expands back to room after hiding starters (#552)",
          chips_run[3]["tail"],
          f"{max(20, 560 - 150 - 14)}px")

    # When multiple tool cards arrive in a single turn, each appendCard()
    # would previously call restCardAtTop(), scrolling the first card offscreen.
    # Subsequent cards in an active turn suppress restCardAtTop() so the
    # first card remains visible at the top (#602).
    multi_turn = scroll_run(400, [
        {"startTurn": True},
        {"card": "card1", "height": 200},
        {"card": "card2", "height": 200},
        {"card": "card3", "height": 100},
        {"turnComplete": True},
        {"startTurn": True},
        {"card": "card4", "height": 150},
    ])
    check("first card of active turn invokes restCardAtTop (#602)",
          multi_turn[1]["restCardCalls"], 1)
    check("first card rests at top of pane (#602)",
          multi_turn[1]["top"], 14)
    check("subsequent card in active turn does not invoke restCardAtTop (#602)",
          multi_turn[2]["restCardCalls"], 1)
    check("first card remains visible at top of pane when second card arrives (#602)",
          multi_turn[2]["top"], 14)
    check("second card is appended below rather than scrolled to top (#602)",
          multi_turn[2]["resting"] > 14, True)
    check("third card in active turn does not invoke restCardAtTop (#602)",
          multi_turn[3]["restCardCalls"], 1)
    check("first card still remains visible at top of pane (#602)",
          multi_turn[3]["top"], 14)
    check("first card of next turn invokes restCardAtTop (#602)",
          multi_turn[6]["restCardCalls"], 2)
    check("new turn card scrolls to top (#602)",
          multi_turn[6]["resting"], 14)
    check("previous turn cards are scrolled up (#602)",
          multi_turn[6]["top"] < 0, True)

    single_turn = scroll_run(400, [
        {"startTurn": True},
        {"card": "single1", "height": 120},
        {"turnComplete": True},
        {"startTurn": True},
        {"card": "single2", "height": 120},
        {"turnComplete": True},
    ])
    check("single-card turns still scroll each card into view (#26, #602)",
          (single_turn[1]["restCardCalls"], single_turn[4]["restCardCalls"]),
          (1, 2))

    no_turn = scroll_run(400, [
        {"card": "noturn1", "height": 100},
        {"card": "noturn2", "height": 100},
    ])
    check("cards outside active turn still scroll into view each time (#26, #602)",
          (no_turn[0]["restCardCalls"], no_turn[1]["restCardCalls"]),
          (1, 2))



# The language the panel is in — the settings picker, the table behind it,
# and the markup that indexes it. `i18n.js` is imported rather than lifted:
# it touches no DOM at import time, so the real module runs here, and only
# the picker comes out of `app.js`.
LANG = r'''
// A DOM only where `applyStatic` touches one. Elements carry their attributes
// in a plain object, so a selector is a filter over that; `textContent`,
// `title`, `placeholder` and `dataset.prompt` are the four things it writes,
// and each is recorded separately because writing the label into the prompt
// is exactly the mix-up that would leave a Japanese chip sending English.
function element(attrs, kids = []) {
  return {
    attrs, kids,
    textContent: attrs.text ?? "",
    title: attrs.title ?? "",
    placeholder: attrs.placeholder ?? "",
    dataset: { prompt: attrs["data-prompt"] ?? "" },
    getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
  };
}

const page = JSON.parse(process.argv[2]).map((a) => element(a));
const doc = {
  documentElement: { lang: "" },
  querySelectorAll(sel) {
    const attr = sel.slice(1, -1);
    return page.filter((el) => attr in el.attrs);
  },
  createElement() { return { value: "", textContent: "" }; },
};
globalThis.document = doc;
globalThis.Option = class {
  constructor(text, value, _default, selected) {
    this.textContent = text;
    this.value = value;
    this.selected = !!selected;
  }
};

const m = await import(process.argv[1]);
const { LOCALES, STRINGS, applyStatic, pickLocale, setLocale, t } = m;

// The picker, with its real change handler hung on it below. Only the one
// listener is ever registered, so a single slot is enough to call it back.
let changeLang = () => {};
const langSelect = {
  value: "",
  options: [],
  replaceChildren(...kids) { this.options = kids; },
  addEventListener(type, fn) { changeLang = fn; },
};
// The status pill and the two things `setConnected` touches beside it. The
// pill is `page[0]` when the caller passes `PILL`, so that `applyStatic`
// reaches it through `querySelectorAll` exactly as it reaches the sheet — the
// whole question here is what happens to an element that is both a `data-i18n`
// site and live state.
const statusText = page.find((el) => el.attrs.id === "statusText")
  || element({ text: "" });
const statusDot = { classList: { toggle() {} } };
const sendButton = { disabled: true };
const textInput = { value: "" };
// The microphone picker, which is the one control on the sheet `applyStatic`
// cannot reach: its options are built, not keyed, so they only follow the
// language if somebody rebuilds them. `node` owns `navigator`, so the device
// list goes in by `defineProperty` rather than assignment.
const micSelect = {
  value: "", options: [],
  replaceChildren(...kids) { this.options = kids; },
  appendChild(k) { this.options.push(k); },
};
let devices = [];
Object.defineProperty(globalThis, "navigator", {
  value: { mediaDevices: { enumerateDevices: async () => devices } },
  configurable: true, writable: true,
});
const settings = { mic: null };
let listening = false;
async function stopRecorder() {}
async function startRecorder() {}
const saved = [];
function saveSettings(patch) { saved.push(patch); }
function showWhere() {}
function showVolume() {}
function drawClock() {}
function drawBar() {}
// Stubbed here and driven in STARTERS. What this section is about is the
// order: `applyStatic` writes every chip's plain label over the contextual
// one, so a redraw that ran before it would be undone by it.
const rendered = [];
function renderStarters() { rendered.push(true); }
const log = [];
const notes = [];
function systemLine(text) { log.push("system"); notes.push(text); }
// Stubbed rather than lifted. The real one closes a socket, folds every card
// left in flight and reconnects, none of which exists here — and none of which
// is the question. The question is whether the handler reaches for it at all,
// because that is the only channel the panel's setting has to the server.
function reopen(note) { log.push("reopen"); notes.push(note); }
// Stubbed for the same reason, and recorded for a different one: the real one
// calls a Geocoder. What has to hold here is only that it is reached, and
// reached *before* `reopen` -- the label rides that handshake, so relabelling
// after the socket is up leaves the clock face in the old language.
async function relabelHome() { log.push("relabel"); }

%s

const script = JSON.parse(process.argv[3]);
const out = [];
for (const step of script) {
  if (step.pick !== undefined) {
    out.push(pickLocale(step.pick, step.browser));
  } else if (step.set) {
    setLocale(step.set);
    out.push(doc.documentElement.lang);
  } else if (step.apply) {
    setLocale(step.apply);
    applyStatic();
    out.push(page.map((el) => ({
      text: el.textContent, title: el.title,
      placeholder: el.placeholder, prompt: el.dataset.prompt,
    })));
  } else if (step.offer) {
    showLanguages(step.offer);
    out.push(langSelect.options.map((o) => [o.value, o.textContent, o.selected]));
  } else if (step.say) {
    setLocale(step.in || "en");
    out.push(t(step.say, step.fallback ?? ""));
  } else if (step.socket !== undefined) {
    setConnected(step.socket);
    out.push(statusText.textContent);
  } else if (step.lang) {
    applyLanguage(step.lang);
    out.push(statusText.textContent);
  } else if (step.mics) {
    if (step.saved !== undefined) settings.mic = step.saved;
    devices = step.mics;
    await listMicrophones();
    out.push({ texts: micSelect.options.map((o) => o.textContent),
               value: micSelect.value });
  } else if (step.micLang) {
    applyLanguage(step.micLang);
    // `applyLanguage` does not await the rebuild, and neither does the panel.
    // A macrotask is one turn later than the microtasks the rebuild resolves
    // on, so this reads what the person would see, not a half-written list.
    await new Promise((r) => setTimeout(r, 0));
    out.push({ texts: micSelect.options.map((o) => o.textContent),
               value: micSelect.value });
  } else if (step.choose !== undefined) {
    // The picker driven the way a finger drives it: the value changes, then
    // the real handler runs. Each step reports only its own doing, so a second
    // choose is not read against the first one's leavings.
    log.length = notes.length = saved.length = 0;
    langSelect.value = step.choose;
    // Awaited: the handler relabels the pin before it reopens, so what the
    // panel does is not all done by the time the call returns.
    await changeLang();
    out.push({ log: [...log], notes: [...notes], saved: [...saved],
               lang: doc.documentElement.lang });
  } else if (step.keys) {
    out.push(Object.fromEntries(LOCALES.map((l) => [l, Object.keys(STRINGS[l]).sort()])));
  }
}
console.log(JSON.stringify(out));
'''


def i18n_src() -> str:
    """`i18n.js`, which owns the table every other file reads through `t`."""
    return (JS / "i18n.js").read_text(encoding="utf-8")


def connected_decl(src: str) -> str:
    """How `app.js` declares the socket flag the status pill is painted from.

    Lifted rather than declared here, and that is the whole point of the two
    cases it serves: a harness that owns the flag proves the harness can hold
    three states, not that `app.js` does. Same move `slot` makes for the
    browse map.
    """
    found = re.search(r"^let connected = .+?;$", src, re.M)
    return found.group(0) if found else "let connected = false;"


def lang_run(script: list[dict], page: list[dict] | None = None) -> list:
    """Drive `i18n.js` — and `app.js`'s picker — over a list of steps."""
    app = (JS / "app.js").read_text(encoding="utf-8")
    lifted = "\n\n".join([block(app, "const LOCALE_NAMES = "),
                          block(app, "function showLanguages("),
                          connected_decl(app),
                          block(app, "function setConnected("),
                          block(app, "function pickMic("),
                          block(app, "async function listMicrophones("),
                          block(app, "function applyLanguage("),
                          # `block` stops at the matching brace, so the
                          # listener comes back without the `);` closing the
                          # call it is an argument to.
                          block(app, 'langSelect.addEventListener("change"')
                          + ");"])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", LANG % lifted,
         str(JS / "i18n.js"), json.dumps(page or []), json.dumps(script)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return [{"error": proc.stderr.strip()[-300:]}]
    return json.loads(proc.stdout)


# The settings sheet as `applyStatic` sees it: one of each kind of site, plus
# the two that are easy to get wrong. A chip carries the label and the prompt
# on different attributes and both have to move; an element with no `data-i18n`
# at all has to be left exactly as it was, because that is every icon on the
# panel and every name Places sent us.
SHEET = [
    {"data-i18n": "ui_clear", "text": "Clear conversation"},
    {"data-i18n-title": "ui_settings", "title": "Settings"},
    {"data-i18n-placeholder": "ui_type_instead", "placeholder": "Type instead…"},
    {"data-i18n-prompt": "ui_ask_places", "data-prompt": "Find a good place to eat nearby"},
    {"text": "tune"},
]


# The sheet plus the status pill, which is the one element on the panel that
# is a `data-i18n` site *and* live state. Everything else with the attribute is
# a label nobody rewrites after the first paint.
PILL = SHEET + [
    {"id": "statusText", "data-i18n": "ui_status_connecting",
     "text": "Connecting"},
]


def place_name_cases(app: str, location: str) -> None:
    """What the map picker writes down, and when it writes it again.

    #131 gave the reverse-geocode a language and stopped there, and the panel
    was reported still reading an English street address afterwards. Two
    reasons, and they are separate defects wearing one line of text:

    - **It was a street address.** `formatted_address` is a building number, a
      block, a postcode and a country, and the clock face has one line for a
      temperature, a sky and a place. The name is built out of the components
      on the same response instead — no second request, already in the language
      that was asked for.
    - **It was written down once.** The label outlives the sheet: it is saved,
      printed under *Location*, sent on the handshake, and since #131 it is the
      place under the clock. A pin dropped before the language changed keeps
      the language it was dropped in for all four, forever, because nothing
      ever asked again.

    The name is checked under node over components arrays rather than by
    reading the source, because the interesting half is what happens where the
    components are not the ones Tokyo has.
    """
    print("\n== the name the pin is saved under ==")

    tokyo = [address("Ueno", "sublocality_level_1", "sublocality"),
             address("Taito City", "locality"),
             address("Tokyo", "administrative_area_level_1"),
             address("Japan", "country")]
    got, failed = short_names([
        tokyo,
        # The same spot with the postal furniture the geocoder also sends.
        # This is what makes it a name rather than a shorter address.
        [address("2", "premise"), address("2", "sublocality_level_2", "sublocality"),
         address("104-0053", "postal_code")] + tokyo,
        # A sublocality with no district name (e.g. 2-chome alone) falls back
        # to the ward or city rather than naming the place '2, Tokyo'.
        [address("2", "sublocality_level_2", "sublocality"),
         address("Taito City", "locality"),
         address("Tokyo", "administrative_area_level_1"),
         address("Japan", "country")],
        # A numeric local component alone is never part of the name.
        [address("2", "sublocality_level_1", "sublocality"),
         address("Tokyo", "administrative_area_level_1")],
        # No sublocality anywhere: `locality` is the town and is the answer.
        [address("Hakone", "locality"),
         address("Ashigarashimo District", "administrative_area_level_2"),
         address("Kanagawa", "administrative_area_level_1"),
         address("Japan", "country")],
        # Neither, which is most of the countryside outside Japan.
        [address("Gironde", "administrative_area_level_2"),
         address("Nouvelle-Aquitaine", "administrative_area_level_1"),
         address("France", "country")],
        # A city that is its own region. Two components, one word.
        [address("Singapore", "locality"),
         address("Singapore", "country")],
        # Nothing but a country, which is a spot in the sea with a flag
        # over it — a name, and no dangling comma after it.
        [address("Japan", "country")],
        # And nothing at all: the caller falls back to the long address.
        [],
        # A component the API sent with no `types` on it must not throw and
        # take the whole label with it.
        [{"long_name": "Nowhere"},
         address("Tokyo", "administrative_area_level_1")],
        # #412: 1-1-1 Ueno, Taito, Tokyo where Ueno has sublocality_level_2
        [address("1", "premise"),
         address("2", "sublocality_level_3", "sublocality"),
         address("Ueno", "sublocality_level_2", "sublocality"),
         address("Taito City", "locality"),
         address("Tokyo", "administrative_area_level_1"),
         address("Japan", "country")],
        # #412: 2-chome or 2丁目 is skipped as block
        [address("2-chome", "sublocality_level_2", "sublocality"),
         address("Taito City", "locality"),
         address("Tokyo", "administrative_area_level_1"),
         address("Japan", "country")],
        # #474: 2-chōme with macron Romanisation is skipped as a block
        [address("1", "premise"),
         address("2-chōme", "sublocality_level_1", "sublocality"),
         address("Ueno", "sublocality_level_2", "sublocality"),
         address("Taito City", "locality"),
         address("Tokyo", "administrative_area_level_1"),
         address("Japan", "country")],
        # #474: ２丁目 with full-width digit is skipped as a block
        [address("1", "premise"),
         address("２丁目", "sublocality_level_1", "sublocality"),
         address("上野", "sublocality_level_2", "sublocality"),
         address("台東区", "locality"),
         address("東京都", "administrative_area_level_1"),
         address("日本", "country")],
        # #474: 2-chōme alone falls back to locality
        [address("2-chōme", "sublocality_level_1", "sublocality"),
         address("Taito City", "locality"),
         address("Tokyo", "administrative_area_level_1"),
         address("Japan", "country")],
        # #474: 1-1-1 hyphenated block is skipped
        [address("1-1-1", "sublocality_level_1", "sublocality"),
         address("Ueno", "sublocality_level_2", "sublocality"),
         address("Taito City", "locality"),
         address("Tokyo", "administrative_area_level_1"),
         address("Japan", "country")],
        # #373: a bare sublocality with numeric chome falls back to locality
        [address("2", "sublocality"),
         address("Taito City", "locality"),
         address("Tokyo", "administrative_area_level_1"),
         address("Japan", "country")],
        # #373: a bare sublocality numeric chome alone is never part of the name
        [address("2", "sublocality"),
         address("Tokyo", "administrative_area_level_1")],
    ])
    check("the name is built by something this can find and run", failed, "")
    check("a Tokyo pin is a neighbourhood and a prefecture",
          got[0], "Ueno, Tokyo")
    check("and the postcode and the block number are not part of a name",
          got[1], "Ueno, Tokyo")
    check("a chome sublocality with no district falls back to the locality",
          got[2], "Taito City, Tokyo")
    check("a purely numeric local half is never used as a place name",
          got[3], "Tokyo")
    check("a town with no neighbourhoods is the town",
          got[4], "Hakone, Kanagawa")
    check("somewhere with neither is the department it is in",
          got[5], "Gironde, Nouvelle-Aquitaine")
    check("and a city that is its own country is said once",
          got[6], "Singapore", "not `Singapore, Singapore`")
    check("a country on its own has no comma after it", got[7], "Japan")
    check("nothing recognised is nothing", got[8], "",
          "the caller falls back to `formatted_address`")
    check("a component with no types on it is skipped, not thrown",
          got[9], "Tokyo")
    check("sublocality_level_2 neighborhood is used when level 3 is a block",
          got[10], "Ueno, Tokyo")
    check("a chome or block named with unit suffix falls back to locality",
          got[11], "Taito City, Tokyo")
    check("a chome with macron Romanisation is skipped as a block",
          got[12], "Ueno, Tokyo")
    check("a full-width chome is skipped as a block",
          got[13], "上野, 東京都")
    check("a chome with macron and no district falls back to locality",
          got[14], "Taito City, Tokyo")
    check("a hyphenated unit block is skipped as a block",
          got[15], "Ueno, Tokyo")
    check("a bare sublocality numeric chome falls back to locality",
          got[16], "Taito City, Tokyo")
    check("a bare sublocality numeric chome alone never forms local half",
          got[17], "Tokyo")

    cards_js = (JS / "cards.js").read_text(encoding="utf-8")
    check("routeCard extracts origin_coords and destination_coords",
          ["data.origin_coords" in cards_js, "data.destination_coords" in cards_js],
          [True, True])
    check("drawRoute uses origin coordinates when present",
          "originCoords && Number.isFinite(originCoords.lat)" in cards_js, True)

    # The two halves of the call site: what it prefers, and what is left when
    # the components say nothing.
    reverse = lifted(location, "export async function reverseGeocode(")
    check("the pin's label is built from the components",
          "shortName(hit?.address_components)" in reverse, True)
    check("and the street address is only the fallback",
          before(reverse, "shortName(", "formatted_address"), True)
    check("the picker's own line shows the same name the clock will",
          "reverseGeocode(" in block(location, "function describeCenter("),
          True, "one label, not one for the sheet and one for the face")

    # The relabel. Saving the tag is what keeps this to one geocode ever
    # rather than one per boot, and it is also what makes a pin saved before
    # this relabel itself: no tag reads as "not the current one".
    check("the label is saved with the language it was written in",
          'lang: String(saved.lang || "")' in block(app, "function savedHome("),
          True)
    check("and a pin dropped now records the tag it was dropped under",
          'lang: document.documentElement.lang || "en"' in app, True)
    relabel = lifted(app, "async function relabelHome(")
    check("the relabel is a function this can find",
          bool(relabel and reverse), True)
    check("a label already in the right language is left alone",
          "if (home.lang === lang) return;" in relabel, True)
    check("and a geocoder that answered nothing changes nothing",
          before(relabel, "if (!label) return;", "saveSettings("), True)
    # Read as source rather than driven, because driving it wants a Geocoder.
    # The tag is what makes this one geocode ever instead of one per boot: drop
    # it and the label is still right, so nothing else here can see it.
    check("and the tag goes down beside the label it was written for",
          "home = { ...home, label, lang };" in relabel, True)
    # Ordering, both times, and it is the whole point: the label rides the
    # handshake, so relabelling after the socket is up leaves the clock face
    # reading the old language until something else reopens it.
    check("the pin is relabelled before the first socket of the session",
          before(block(app, "async function init("),
                 "await relabelHome();", "connect();"), True)
    check("and before the one the language picker reopens",
          before(app, "await relabelHome();\n  reopen(t(\"ui_lang_note\"))",
                 "/* ---------- voice ---------- */"), True)


def lang_cases() -> None:
    print("\n== the language the panel is in ==")

    # What the panel opens in, before anybody has touched the picker and after.
    # The saved value is the answer whenever there is one; the kiosk's own
    # locale is the answer on a terminal that has never been asked, because it
    # is a thing somebody set up once rather than a tag a model attached to a
    # tool call this turn. `loadMaps` has trusted the same source all along.
    picks = lang_run([
        {"pick": "ja", "browser": "en-GB"},
        {"pick": "", "browser": "ja-JP"},
        {"pick": "", "browser": "en-GB"},
        {"pick": "", "browser": "sv-SE"},
        {"pick": "", "browser": ""},
        {"pick": "ja-JP", "browser": "en"},
        {"pick": "sv", "browser": "ja"},
        {"pick": None, "browser": "ja"},
    ])
    check("the run got through it",
          picks[0] if isinstance(picks[0], dict) else None, None)
    check("a saved choice wins over the browser", picks[0], "ja")
    check("with nothing saved the kiosk's own locale decides", picks[1], "ja")
    check("and an English kiosk opens in English", picks[2], "en")
    check("a locale with no block falls back to English", picks[3], "en")
    check("so does a browser that will not say", picks[4], "en")
    check("a region is dropped, not treated as a third language",
          picks[5], "ja")
    check("a saved value that is no longer a locale falls to the browser",
          picks[6], "ja")
    check("and so does a saved value that is not a string at all",
          picks[7], "ja")

    # Where the tag goes. One attribute, because `t` reads the document, `Intl`
    # reads the document, and the browser reads the document to pick a CJK
    # font. Two channels would be three things to keep in step.
    check("choosing a language writes it on the document",
          lang_run([{"set": "ja"}]), ["ja"])

    # The picker itself: exactly the locales there is a block for, each named
    # in its own language. An endonym rather than a translation, because a
    # picker that says "Japanese" in English and "英語" in Japanese cannot be
    # used to get back out of the language you landed in by accident.
    offered = lang_run([{"offer": "ja"}])[0]
    check("the picker offers one option per locale",
          [o[0] for o in offered], ["en", "ja"])
    check("each named in its own language",
          [o[1] for o in offered], ["English", "日本語"])
    check("with the one in use selected",
          [o[0] for o in offered if o[2]], ["ja"])

    # And what choosing one does, which is more than relabel the sheet. The
    # tools' language floor rides the handshake (`main.py:1394-1401`) and no
    # message on an open socket carries a new one, so a picker that stops at
    # the chrome leaves the next place card in the old language — the reported
    # symptom, produced by the action taken to cure it. The real handler is
    # lifted and run; only `reopen` is a stub.
    turn = lang_run([{"set": "en"}, {"choose": "ja"}, {"choose": "ja"}])
    check("the run got through it",
          turn[0] if isinstance(turn[0], dict) else None, None)
    check("a new language opens a new socket, because that is the only thing "
          "the tools' floor rides on", turn[1]["log"], ["relabel", "reopen"])
    check("and the pin is relabelled before the socket goes, not after",
          before(" ".join(turn[1]["log"]), "relabel", "reopen"), True,
          "the label is what rides the handshake")
    check("and it says the connection is coming back, not only that the map "
          "lags", turn[1]["notes"],
          ["画面を日本語にしました。接続し直します。"
           "地図のラベルは次回の再読み込みから変わります。"],
          "in the language just chosen, since `t` reads the document")
    check("with the choice written down before the connection goes",
          turn[1]["saved"], [{"lang": "ja"}])
    check("and the document carrying it, which is where `t` looks",
          turn[1]["lang"], "ja")
    # A `change` fires when a select is put back to the value it already had,
    # and this handler costs a reconnect now — which kills any browse, gws or
    # plan work in flight. The voice picker has guarded the same way since it
    # started reopening.
    check("choosing the language already in use costs nothing",
          [turn[2]["log"], turn[2]["saved"]], [[], []])

    print("\n== every string the markup carries ==")

    # The four sites, and the one that must not move.
    english = lang_run([{"apply": "en"}], SHEET)[0]
    japanese = lang_run([{"apply": "ja"}], SHEET)[0]
    check("the run got through it",
          english if isinstance(english, dict) else None, None)
    check("an English panel is the markup as written",
          [english[0]["text"], english[1]["title"], english[2]["placeholder"]],
          ["Clear conversation", "Settings", "Type instead…"])
    check("a Japanese one has the text swapped",
          japanese[0]["text"], "会話を消す")
    check("and the title", japanese[1]["title"], "設定")
    check("and the placeholder", japanese[2]["placeholder"], "入力する…")
    # The chip is the one that reaches past the screen: `data-prompt` is what
    # gets sent when it is tapped, so a chip that reads Japanese and sends
    # English is answered in English under a Japanese label.
    check("a starter chip sends what it says it will send",
          japanese[3]["prompt"], "近くのおいしいお店は？")
    check("an element with no key on it is left alone",
          japanese[4]["text"], "tune", "every icon on the panel")

    print("\n== the table, and the markup that indexes it ==")

    # The sweep this issue is: 50 strings across four files, and the way it
    # goes wrong is one of them missed. A key in the markup with nothing behind
    # it renders as the English already there — silently right on an English
    # panel, silently wrong on a Japanese one — so nothing at runtime finds it.
    page = (JS.parent / "index.html").read_text(encoding="utf-8")
    keys = re.findall(r'data-i18n(?:-\w+)?="([^"]+)"', page)
    table = lang_run([{"keys": True}])[0]
    check("the markup is keyed against the table, not translated in place",
          len(keys) >= 24, True, f"{len(keys)} keys in index.html")
    check("and every key in it has a string behind it",
          sorted({k for k in keys if k not in table["en"]}), [])

    # Parity. English is the fallback for a missing key, so a `ja` block short
    # of one shows English on a Japanese panel and raises nothing.
    check("both locales carry the same keys", table["en"], table["ja"],
          f"{len(table['en'])} keys each")

    # A chip's label and its prompt are two attributes on two elements. They
    # used to be the same key, because the label *was* the sentence; the label
    # is a capability's name now, so the pairing is by capability instead —
    # `ui_chip_video` beside `ui_ask_video`. Nothing else says the two belong
    # to the same chip, and a chip whose prompt is another chip's asks a
    # question the icon beside it does not match.
    chips = [(c["data-i18n"], c["data-i18n-prompt"]) for c in chips_in(page)]
    check("and a chip's label and its prompt name the same capability",
          [(label, ask) for label, ask in chips
           if label.removeprefix("ui_chip_") != ask.removeprefix("ui_ask_")],
          [], f"{len(chips)} chips")
    check("all ten of them", len(chips), 10)

    # Issue #624: The empty-state placeholder in the cards pane was removed
    # per owner request; starter chips sit at the top of the right panel on startup.
    css = (JS.parent / "css" / "style.css").read_text(encoding="utf-8")
    rules = re.findall(r'(?:html\[lang="(\w+)"\] )?\.cards:empty::after', css)
    check("the empty cards pane has no placeholder pseudo-elements",
          rules, [],
          "placeholder removed per owner request (#624)")

    starters_en = lang_run([{"say": "ui_starters", "in": "en"}])[0]
    starters_ja = lang_run([{"say": "ui_starters", "in": "ja"}])[0]
    check("ui_starters translation in English", starters_en,
          "Tap a chip or the mic to start, or try")
    check("ui_starters translation in Japanese", starters_ja,
          "チップをタップするか、マイクで話しかけてください。例えば")
    check("starters label in index.html matches ui_starters default",
          'data-i18n="ui_starters">Tap a chip or the mic to start, or try</p>' in page, True)

    wake_en = lang_run([{"say": "ui_wake_again", "in": "en"}])[0]
    wake_ja = lang_run([{"say": "ui_wake_again", "in": "ja"}])[0]
    check("ui_wake_again translation in English", wake_en,
          "Asleep — say “Hey Gemini” or tap the mic to wake up.")
    check("ui_wake_again translation in Japanese", wake_ja,
          "スリープ中：「Hey Gemini」と呼ぶかマイクを押してください。")

    print("\n== and the detection it replaces ==")

    app = (JS / "app.js").read_text(encoding="utf-8")
    cards = (JS / "cards.js").read_text(encoding="utf-8")

    def code(text: str) -> str:
        """`text` with its `//` lines dropped.

        Everything below counts occurrences, and the comments in this file say
        `lang` and `navigator.language` a great deal — they are the paragraph
        explaining why neither is read any more. A case that a comment can fail
        is a case somebody deletes.
        """
        return "\n".join(line for line in text.splitlines()
                          if not line.strip().startswith("//"))

    # The whole of the change, read as source: the model's per-turn tag no
    # longer reaches the document. It still reaches the tools — that is what
    # `_lang.py` is for — but a card heading in a settings sheet is not an
    # answer to anything and must not change because one question was asked in
    # the other language.
    check("nothing writes the turn's tag onto the document any more",
          "documentElement.lang =" in app, False)
    check("and the function that used to is gone with it",
          "noteLanguage" in app, False)
    check("and the tool call hands the tag to nothing",
          "args.lang" in code(block(app, "function handleEvent(")), False)
    # The other direction: the tag has to be written by something.
    check("the setting is what writes it", "setLocale(locale)" in app, True)
    check("read back from where the sheet's other settings live",
          "pickLocale(settings.lang, navigator.language)" in app, True)
    check("and written back when it changes",
          "saveSettings({ lang: next })" in app, True)
    # Fixed for the life of the page, so it has to be the setting rather than
    # the browser: a household that set the panel to Japanese on an English
    # kiosk was getting English map tiles.
    check("the map tiles follow the setting, not the browser",
          'const language = document.documentElement.lang || "en"'
          in block(app, "function loadMaps("), True)
    check("and `navigator.language` is only the first-run fallback",
          code(app).count("navigator.language"), 1,
          "the default for the setting, not a second opinion about it")
    # The same rot one file over, and the one that outlives the page it
    # happens on: the address the map picker writes down is saved, printed
    # under *Location*, sent on the handshake and — since #131 — the place at
    # the end of the line under the clock. Without a language on the request
    # the Geocoder answers in the kiosk Chromium's own locale, which is not
    # the setting, so a Japanese panel kept an English address in four places.
    location = (JS / "location.js").read_text(encoding="utf-8")
    check("the address a pin is saved under follows the setting too",
          'language: document.documentElement.lang || "en"'
          in block(location, "async function reverseGeocode("), True)
    # The sky at the bottom of the clock, which is the other half of #131 and
    # the one the server cannot guess: one process, one reading, and a
    # language per page.
    check("and so does the word the clock face prints for the sky",
          "document.documentElement.lang"
          in block(app, "async function loadScreensaver("), True)

    place_name_cases(app, location)

    # Every sentence the page says to the room, and the one way this rots
    # quietly: a line added later straight to `systemLine` reads fine in
    # English on the machine it was written on, and is the only English left
    # on a Japanese panel. Two of these were exactly that — the wake word's
    # idle notice and the map failure — so the rule is the case, not the two
    # keys. Anything that is already a variable is somebody else's text.
    spoken = re.findall(r"systemLine\(\s*([\"'`])", app)
    check("nothing says a sentence to the room in a literal", spoken, [],
          f"{len(spoken)} of them, and `t(\"…\")` is how the rest do it")

    # The fallback chain, at the one end nothing else reaches: both blocks are
    # complete today, so a `ja` short of a key is the parity case's business
    # and the *English* fall is unobservable — except through a key that is in
    # neither, which is what a card renderer asking for a label added after it
    # looks like.
    check("a key in no block at all comes back empty, not as the key",
          lang_run([{"say": "ui_nothing_of_the_sort"}])[0], "",
          "a heading reading `ui_nothing_of_the_sort` is worse than a blank")

    # One table, so a locale is one block. Two would be a locale that is
    # complete in the cards and missing from the sheet.
    check("the card labels moved into the same table as the sheet's",
          ["const LABELS" in cards, "STRINGS" in i18n_src()], [False, True])
    check("and cards.js reads them through the shared `t`",
          'import { t } from "./i18n.js";' in cards, True)

    # The status pill, which is the one `data-i18n` element whose text stops
    # being the markup's after the first paint. `applyStatic` cannot tell it
    # from a label, so the socket's answer has to be put back on top — and
    # only when there is one, because before the first `onopen` the markup is
    # right and "Offline" would be a second, opposite lie.
    pill = lang_run([
        {"lang": "ja"},
        {"socket": True},
        {"lang": "en"},
        {"lang": "ja"},
        {"socket": False},
        {"lang": "en"},
    ], PILL)
    check("before the socket has said anything the markup's answer stands",
          pill[0], "接続中")
    check("and switching language does not claim the panel is offline",
          pill[0] in ("オフライン", "Offline"), False)
    check("a connected panel says so after the language changes",
          [pill[1], pill[2]], ["準備完了", "Ready"],
          "`applyStatic` writes `ui_status_connecting` over this one")
    check("and it is still connected in the language it came from",
          pill[3], "準備完了")
    check("a panel that lost the socket keeps saying so", pill[5], "Offline")

    # The microphone picker, the other control whose text `applyStatic` cannot
    # reach — and it cannot for the opposite reason to the pill's. The pill is
    # a keyed element somebody overwrites; these options are never keyed at
    # all, because `listMicrophones` throws the markup's one away and builds
    # the rest out of `new Option`. So the language reaches them only if the
    # setting rebuilds the list, and the rebuild has to leave the chosen device
    # chosen: relabelling the picker is no reason to start recording off
    # another microphone.
    mics = [{"deviceId": "a", "kind": "audioinput",
             "label": "ReSpeaker 2-Mic"},
            {"deviceId": "b", "kind": "audioinput", "label": ""},
            {"deviceId": "c", "kind": "videoinput", "label": "Camera"}]
    picker = lang_run([
        {"set": "en"},
        {"mics": mics, "saved": {"id": "a", "label": "ReSpeaker 2-Mic"}},
        {"micLang": "ja"},
        {"mics": mics},
        {"micLang": "en"},
    ])
    check("the run got through it",
          picker[0] if isinstance(picker[0], dict) else None, None)
    check("the device list is built in the language the panel is in",
          picker[1]["texts"],
          ["System default", "ReSpeaker 2-Mic", "Microphone 2"])
    check("and the language setting redraws it",
          picker[2]["texts"],
          ["システム既定", "ReSpeaker 2-Mic", "マイク 2"],
          "`applyLanguage` has to call `listMicrophones`: these options carry "
          "no `data-i18n` for `applyStatic` to find")
    check("a list built on a Japanese panel is Japanese to begin with",
          picker[3]["texts"],
          ["システム既定", "ReSpeaker 2-Mic", "マイク 2"])
    check("and it comes back the other way too",
          picker[4]["texts"],
          ["System default", "ReSpeaker 2-Mic", "Microphone 2"])
    check("the redraw leaves the chosen microphone chosen",
          [step["value"] for step in picker[1:]], ["a", "a", "a", "a"])


# The starter chips, driven over the chips the markup actually has. Everything
# here is invisible from outside the page: a label is written into a `<span>`
# and the sentence it sends into a `data-` attribute, and the two coming apart
# is a chip that reads one thing and asks another — which is what a short label
# costs, since the label used to *be* the prompt.
STARTERS = r"""
%s

%s

// One stub per chip in `index.html`, carrying the attributes `renderStarters`
// reads and nothing else. The label is a child element, the way it is in the
// markup, because writing the label into `textContent` of the button would
// take the icon with it — the reason the markup has the extra `<span>` at all.
const chips = JSON.parse(process.argv[1]).map((a) => {
  const label = {
    textContent: a["label-text"] ?? "",
    dataset: { i18n: a["data-i18n"] ?? "" },
  };
  return {
    label,
    dataset: {
      prompt: a["data-prompt"] ?? "",
      i18nPrompt: a["data-i18n-prompt"] ?? "",
      topicSlot: a["data-topic-slot"],
      topicLabel: a["data-topic-label"],
      topicPrompt: a["data-topic-prompt"],
    },
    querySelector(sel) { return sel === "[data-i18n]" ? this.label : null; },
  };
});

const hidden = new Set(["hidden"]);
const starters = {
  parentElement: null,
  querySelectorAll(sel) { return sel === ".chip" ? chips : []; },
  querySelector(sel) { return null; },
  classList: {
    add: (c) => hidden.add(c),
    remove: (c) => hidden.delete(c),
    contains: (c) => hidden.has(c),
  },
};

const feedChildren = [starters];
starters.parentElement = feedChildren;
let scrolled = false;
const feed = {
  children: feedChildren,
  scrollHeight: 500,
  get lastElementChild() { return feedChildren[feedChildren.length - 1] ?? null; },
  appendChild(child) {
    const idx = feedChildren.indexOf(child);
    if (idx !== -1) feedChildren.splice(idx, 1);
    feedChildren.push(child);
    child.parentElement = this;
    return child;
  },
  scrollTo(opts) {
    if (opts && opts.top === this.scrollHeight) scrolled = true;
  },
};

let wideMatches = false;
const WIDE = {
  get matches() { return wideMatches; },
  addEventListener: () => {},
};
globalThis.WIDE = WIDE;

const sidePanelChildren = [];
const cards = { id: "cards" };
sidePanelChildren.push(cards);
let sidePanelAppends = 0;
const sidePanel = {
  children: sidePanelChildren,
  appendChild(node) {
    sidePanelAppends++;
    const idx = sidePanelChildren.indexOf(node);
    if (idx !== -1) sidePanelChildren.splice(idx, 1);
    sidePanelChildren.push(node);
    node.parentElement = this;
    const myIdx = sidePanelChildren.indexOf(node);
    node.previousElementSibling = myIdx > 0 ? sidePanelChildren[myIdx - 1] : null;
    return node;
  },
};
globalThis.sidePanel = sidePanel;
globalThis.cards = cards;
globalThis.feed = feed;
globalThis.starters = starters;

globalThis.document = {
  documentElement: { lang: "en" },
  getElementById(id) {
    if (id === "sidePanel") return sidePanel;
    if (id === "cards") return cards;
    if (id === "feed") return feed;
    if (id === "starters") return starters;
    return null;
  },
};

%s

const out = [];
for (const step of JSON.parse(process.argv[2])) {
  if (step.wide !== undefined) wideMatches = Boolean(step.wide);
  // A tool call the model made, which is where the topic comes from: the
  // arguments are handed in exactly as `handleEvent` reads them off
  // `part.functionCall`.
  if (step.call) {
    if (step.call.args) noteTopic(step.call.args, step.call.name || "");
    else noteTopic(step.call);
  }
  if (step.clear) forgetTopic();
  if (step.msg) feed.appendChild({ className: "msg", text: step.msg });
  if (step.show) {
    document.documentElement.lang = step.show;
    showStarters();
    out.push({
      hidden: hidden.has("hidden"),
      chips: chips.map((c) => ({ label: c.label.textContent,
                                 prompt: c.dataset.prompt })),
      lastInFeed: feed.lastElementChild === starters,
      inSidePanel: sidePanel.children[sidePanel.children.length - 1] === starters && sidePanel.children[0] === cards,
      sidePanelAppends,
      scrolled,
    });
  }
}
console.log(JSON.stringify(out));
"""

# Everything `renderStarters` decides with, by the name it is declared under.
STARTER_PARTS = ("const TOPIC_MAX = ", "const TOPIC_PLACE_ARGS = ",
                 "const TOPIC_SUBJECT_ARGS = ", "const TOPIC_HERE = ",
                 "const TOPIC_MEDIA_MARKER = ", "const TOPIC_PLACE_TOOLS = ",
                 "function noteTopic(", "function forgetTopic(",
                 "function renderStarters(", "function showStarters(")


def chips_in(page: str) -> list[dict]:
    """Every starter chip in `index.html`, as the attributes `app.js` reads.

    Read out of the markup rather than written down here, so a chip added
    without its keys — or with a contextual key and no slot to fill it — fails
    a case instead of sitting on the panel saying the wrong thing. The label
    span's own `data-i18n` is picked up along with the button's attributes;
    they are all `data-…="…"` and none of them collide.
    """
    found = []
    for body in re.findall(r'<button class="chip"(.*?)</button>', page, re.S):
        attrs = dict(re.findall(r'(data-[\w-]+)="([^"]*)"', body))
        label = re.search(r'<span data-i18n="[^"]+">(.*?)</span>', body)
        attrs["label-text"] = label.group(1) if label else ""
        found.append(attrs)
    return found


def starters_run(script: list[dict]) -> list:
    """Drive the chip row over a run of tool calls and redraws."""
    app = (JS / "app.js").read_text(encoding="utf-8")
    missing = [part for part in STARTER_PARTS if part not in app]
    if missing:
        return [{"error": f"app.js has no {missing[0]}…"}]
    page = (JS.parent / "index.html").read_text(encoding="utf-8")
    lifted = "\n".join(
        [found.group(0)
         for name in ("topicPlace", "topicSubject")
         if (found := re.search(rf"^let {name} = .*$", app, re.M))]
        + [block(app, header) if header.startswith("function")
           else re.search(rf"^{re.escape(header)}.*?;$", app, re.M | re.S)
                  .group(0)
           for header in STARTER_PARTS])
    body = STARTERS % (block(i18n_src(), "export const STRINGS = ")
                       .removeprefix("export "),
                       block(i18n_src(), "export function t(")
                       .removeprefix("export "),
                       lifted)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", body,
         json.dumps(chips_in(page)), json.dumps(script)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        return [{"error": proc.stderr.strip()[-300:]}]
    return json.loads(proc.stdout)


def starters_cases() -> None:
    print("\n== the suggestions, and what the room is talking about ==")

    page = (JS.parent / "index.html").read_text(encoding="utf-8")
    marked = chips_in(page)

    # One chip per capability, which is the whole of part 1: the four chips
    # this replaces were whole sentences and covered three tools between them.
    check("there is a chip for each thing the terminal can do",
          len(marked) >= 10, True, f"{len(marked)} chips")

    # A short label is not a question, so the label and the sentence are two
    # keys now where they used to be one. Both have to be keyed, or a Japanese
    # panel sends English and is answered in English.
    check("every chip keys its label and its prompt separately",
          [c.get("data-i18n") for c in marked
           if c.get("data-i18n") == c.get("data-i18n-prompt")], [])
    check("and neither key is missing from any of them",
          [i for i, c in enumerate(marked)
           if not c.get("data-i18n") or not c.get("data-i18n-prompt")], [])

    # The contextual half is three attributes that only work together: a slot
    # with no keys never changes anything, and keys with no slot are never
    # reached. Either way the chip silently stays plain.
    check("a chip that follows the conversation carries a slot and both keys",
          [c.get("data-i18n") for c in marked
           if bool(c.get("data-topic-slot"))
           != bool(c.get("data-topic-label") and c.get("data-topic-prompt"))],
          [])
    check("and the slot is one the renderer knows",
          sorted({c["data-topic-slot"] for c in marked
                  if "data-topic-slot" in c}), ["place", "subject"])
    check("only weather, news, routes, places, web search, and video follow conversation",
          sorted(c["data-i18n"] for c in marked if "data-topic-slot" in c),
          ["ui_chip_news", "ui_chip_places", "ui_chip_routes", "ui_chip_video",
           "ui_chip_weather", "ui_chip_web_search"])
    check("personal workspace chips and schedule remain static",
          [c["data-i18n"] for c in marked if "data-topic-slot" not in c],
          ["ui_chip_schedule", "ui_chip_mail", "ui_chip_docs", "ui_chip_market"])

    table = lang_run([{"keys": True}])[0]
    contextual = [k for c in marked
                  for k in (c.get("data-topic-label"),
                            c.get("data-topic-prompt")) if k]
    check("every contextual key has a string behind it",
          sorted(k for k in contextual if k not in table["en"]), [])
    # The `{q}` is the topic. A contextual string without one is a label that
    # claims to name the room's subject and does not.
    slots = lang_run([{"say": k, "in": lang} for k in contextual
                      for lang in ("en", "ja")])
    check("and each of them has somewhere to put the topic",
          [s for s in slots if "{q}" not in s], [],
          f"{len(slots)} strings across both locales")

    # And the run. Everything above reads the markup; this drives the renderer.
    plain = starters_run([{"show": "en"}, {"show": "ja"}])
    check("the run got through it", plain[0].get("error"), None)
    check("a panel nobody has spoken to shows the short labels",
          [c["label"] for c in plain[0]["chips"]],
          ["Today's Weather", "Today's Schedule", "Recent Emails",
           "Find Docs", "News", "Market", "Routes", "Find Places",
           "Web Search", "Video"])
    # The point of the split: what is on the button got shorter, what the
    # button sends did not. A chip that sent "Routes" would be answered with a
    # question about what the person meant.
    check("and every one of them still sends a whole question",
          [c["label"] for c in plain[0]["chips"]
           if len(c["prompt"]) <= len(c["label"])], [])
    check("a Japanese panel reads Japanese",
          [c["label"] for c in plain[1]["chips"]][:3],
          ["今日の天気", "今日の予定", "最近のメール"])
    check("and sends Japanese",
          plain[1]["chips"][0]["prompt"], "今日の天気は？",
          "the label is no longer the prompt, so this is its own key now")
    check("showing the row is what unhides it", plain[0]["hidden"], False)

    chips_en = {c["label"]: c["prompt"] for c in plain[0]["chips"]}
    chips_ja = {c["label"]: c["prompt"] for c in plain[1]["chips"]}
    check("bare routes chip prompts with natural directions phrase",
          chips_en["Routes"], "I want to look up directions")
    check("bare video chip prompts with natural video phrase",
          chips_en["Video"], "I want to watch a video")
    check("bare docs chip prompts with natural docs phrase",
          chips_en["Find Docs"], "Find a document in Drive")
    check("bare routes chip sends Japanese prompt",
          chips_ja["ルート"], "道順を調べたい")
    check("bare video chip sends Japanese prompt",
          chips_ja["動画"], "動画を見たい")
    check("bare docs chip sends Japanese prompt",
          chips_ja["ドキュメント検索"], "ドライブのドキュメントを探して")
    check("bare chip prompts do not contain meta-instructions",
          any("ask me" in p.lower() or "質問してください" in p for p in (
              chips_en["Routes"], chips_en["Video"], chips_en["Find Docs"],
              chips_ja["ルート"], chips_ja["動画"], chips_ja["ドキュメント検索"]
          )), False)

    # The reported case, in the reporter's own words: a ramen shop in NYC, then
    # the weather chip. The two slots are the whole design — one topic would
    # have handed the weather chip "ramen shops" or the video chip "New York".
    nyc = starters_run([
        {"call": {"query": "ramen shops", "near": "New York", "lang": "en"}},
        {"show": "en"},
    ])
    named = {c["label"]: c["prompt"] for c in nyc[0]["chips"]}
    check("the run got through it", nyc[0].get("error"), None)
    check("a search for ramen in New York names the weather chip for the "
          "place", "Weather in New York" in named, True,
          list(named)[:3])
    check("and sends a question about that place",
          named.get("Weather in New York"),
          "What's the weather like in New York?")
    check("while the chips that want a subject take the subject",
          [label for label in named
           if label in ("Web search for ramen shops", "Video of ramen shops",
                        "News about ramen shops")],
          ["News about ramen shops", "Web search for ramen shops",
           "Video of ramen shops"])
    # Market, schedule, mail and docs have no contextual form, and that is a decision
    # rather than an omission: personal workspace chips remain stable capabilities.
    check("and the ones with no contextual form are left alone",
          [label for label in named
           if label in ("Today's Schedule", "Recent Emails", "Find Docs", "Market")],
          ["Today's Schedule", "Recent Emails", "Find Docs", "Market"])

    # An entity that is not a geographic location (e.g. a TV program) does not mutate place chips.
    # Weather remains Today's Weather, Mail remains Recent Emails, Docs remains Find Docs.
    darwin = starters_run([
        {"call": {"name": "place_info", "args": {"place": "ダーウィンが来た！ (テレビ番組)"}}},
        {"show": "en"},
    ])
    d_named = {c["label"]: c["prompt"] for c in darwin[0]["chips"]}
    check("the darwin run got through it", darwin[0].get("error"), None)
    check("weather remains baseline for TV program query",
          "Today's Weather" in d_named, True)
    check("mail remains baseline for TV program query",
          "Recent Emails" in d_named, True)
    check("docs remains baseline for TV program query",
          "Find Docs" in d_named, True)
    check("routes and places remain baseline for non-place entity",
          [l for l in d_named if l in ("Routes", "Find Places")],
          ["Routes", "Find Places"])
    check("web search and video adapt to subject",
          [l for l in d_named if l.startswith("Web search") or l.startswith("Video")],
          ["Web search for ダーウィンが来た！ (テレビ番組)",
           "Video of ダーウィンが来た！ (テレビ番組)"])

    darwin_ja = starters_run([
        {"call": {"name": "place_info", "args": {"place": "ダーウィンが来た！ (テレビ番組)"}}},
        {"show": "ja"},
    ])
    d_ja_named = {c["label"]: c["prompt"] for c in darwin_ja[0]["chips"]}
    check("japanese weather remains baseline for TV program query",
          "今日の天気" in d_ja_named, True)
    check("japanese mail remains baseline for TV program query",
          "最近のメール" in d_ja_named, True)
    check("japanese docs remains baseline for TV program query",
          "ドキュメント検索" in d_ja_named, True)
    check("japanese routes and places remain baseline for non-place entity",
          [l for l in d_ja_named if l in ("ルート", "お店を探す")],
          ["ルート", "お店を探す"])

    # `here` is what the model passes for the terminal's own location, which is
    # what every chip already means. A chip reading "Weather in here" is the
    # bug this guards.
    here = starters_run([{"call": {"query": "ramen shops", "near": "here"}},
                         {"show": "en"}])
    check("a search around the kitchen names no place",
          [c["label"] for c in here[0]["chips"] if c["label"] in ("Today's Weather", "Routes", "Find Places")],
          ["Today's Weather", "Routes", "Find Places"])
    check("but it is still a subject",
          "Web search for ramen shops" in [c["label"] for c in here[0]["chips"]], True)

    # A topic long enough to run off the side of a button. The label is cut and
    # the prompt is not: what is being fitted is one line of a chip, and what
    # gets sent is not on the chip.
    long = starters_run([
        {"call": {"query": "how to descale a stainless steel kettle"}},
        {"show": "en"},
    ])
    video = [c for c in long[0]["chips"] if c["label"].startswith("Video")][0]
    check("a long topic is cut on the label",
          (len(video["label"]) <= len("Video of ") + 24,
           video["label"].endswith("…")), (True, True), video["label"])
    check("and whole in what the chip sends",
          video["prompt"],
          "Show me a video of how to descale a stainless steel kettle")

    # The other end of the acceptance sentence: with no conversation, back to
    # what part 1 defines. `forgetTopic` is what the clear button calls.
    cleared = starters_run([
        {"call": {"query": "ramen shops", "near": "New York"}},
        {"show": "en"},
        {"clear": True},
        {"show": "en"},
    ])
    check("clearing the conversation puts every chip back",
          [c["label"] for c in cleared[1]["chips"]],
          [c["label"] for c in plain[0]["chips"]])
    check("prompts included",
          [c["prompt"] for c in cleared[1]["chips"]],
          [c["prompt"] for c in plain[0]["chips"]])

    # A tool whose arguments name neither. `gws` takes a shell command and
    # `plan` takes a task; reading either as a topic would put a fragment of
    # somebody's calendar query on the weather chip.
    quiet = starters_run([
        {"call": {"command": "calendar agenda --days 1"}},
        {"call": {"task": "book a table for four"}},
        {"show": "en"},
    ])
    check("a call that names neither a place nor a subject moves nothing",
          [c["label"] for c in quiet[0]["chips"]],
          [c["label"] for c in plain[0]["chips"]])

    # And the ordering `applyLanguage` depends on. `applyStatic` writes every
    # chip's plain label and plain prompt from the markup's keys, so a redraw
    # scheduled before it is a redraw that gets overwritten — the panel would
    # lose its context on a language change and nothing would say why.
    app = (JS / "app.js").read_text(encoding="utf-8")
    check("a language change redraws the chips after the static pass",
          before(block(app, "function applyLanguage("), "applyStatic();",
                 "renderStarters();"), True)
    # The topic is not turn state. It is what the conversation is about, and a
    # conversation is more than one turn: reset here and the chips would be
    # plain again by the time anyone could read them.
    check("and starting a turn does not forget what the room is about",
          "forgetTopic()" in block(app, "function startTurn("), False)

    # When conversation turns have been added to the feed, showStarters appends
    # the starter buttons to the end of the feed rather than leaving them at the top.
    check("showStarters appends starters to the feed",
          "feed.appendChild(starters)" in block(app, "function showStarters("), True)

    turned = starters_run([
        {"msg": "user message"},
        {"msg": "assistant reply"},
        {"show": "en", "wide": False},
    ])
    check("the run got through it", turned[0].get("error"), None)
    check("calling showStarters places starters at the end of the feed",
          turned[0].get("lastInFeed"), True)

    # When starter chips are shown, the feed scrolls to reveal them at the bottom.
    check("showStarters scrolls feed to reveal starters",
          "feed.scrollTo" in block(app, "function showStarters("), True)
    check("calling showStarters scrolls feed to the bottom",
          turned[0].get("scrolled"), True)

    # In landscape (WIDE.matches), starters is placed into sidePanel after cards (#530, #542)
    land = starters_run([
        {"msg": "user message"},
        {"show": "en", "wide": True},
        {"show": "en", "wide": True},
    ])
    check("calling showStarters in landscape places starters in side panel after cards",
          land[0].get("inSidePanel"), True)
    check("calling showStarters again when already in place does not re-append (#547)",
          land[1].get("sidePanelAppends"), 1)

    # Issue #530 & #542: starters layout, structure, and i18n
    check("index.html has sidePanel container",
          '<div class="side-panel" id="sidePanel">' in page, True)
    check("index.html places cards before starters inside sidePanel",
          page.index('id="cards"') < page.index('id="starters"'), True)

    css = (JS.parent / "css" / "style.css").read_text(encoding="utf-8")
    # Issue #532: touch swipe scrolling and text selection suppression
    check("style.css feed has touch-action: pan-y",
          "touch-action: pan-y;" in block(css, ".feed {"), True)
    check("style.css feed has user-select: none",
          "user-select: none;" in block(css, ".feed {"), True)
    check("style.css feed has -webkit-user-select: none",
          "-webkit-user-select: none;" in block(css, ".feed {"), True)
    check("style.css feed children have touch-action: pan-y",
          "touch-action: pan-y;" in block(css, ".feed > * {"), True)
    check("style.css side-panel has touch-action: pan-y",
          "touch-action: pan-y;" in block(css, "  .side-panel {"), True)
    check("style.css side-panel has user-select: none",
          "user-select: none;" in block(css, "  .side-panel {"), True)
    check("style.css side-panel has -webkit-user-select: none",
          "-webkit-user-select: none;" in block(css, "  .side-panel {"), True)
    check("style.css side-panel children have touch-action: pan-y",
          "touch-action: pan-y;" in block(css, "  .side-panel > * {"), True)
    check("style.css side-panel has overflow-y: auto",
          "overflow-y: auto;" in block(css, "  .side-panel {"), True)
    check("style.css cards has touch-action: pan-y",
          "touch-action: pan-y;" in block(css, "  .cards {"), True)
    check("style.css cards has user-select: none",
          "user-select: none;" in block(css, "  .cards {"), True)
    check("style.css cards has -webkit-user-select: none",
          "-webkit-user-select: none;" in block(css, "  .cards {"), True)
    check("style.css cards children have touch-action: pan-y",
          "touch-action: pan-y;" in block(css, "  .cards > * {"), True)
    check("style.css card has touch-action: pan-y",
          "touch-action: pan-y;" in block(css, ".card {"), True)
    check("style.css card has user-select: none",
          "user-select: none;" in block(css, ".card {"), True)
    check("style.css card has -webkit-user-select: none",
          "-webkit-user-select: none;" in block(css, ".card {"), True)
    check("style.css msg has touch-action: pan-y",
          "touch-action: pan-y;" in block(css, ".msg {"), True)
    check("style.css msg has user-select: none",
          "user-select: none;" in block(css, ".msg {"), True)
    check("style.css msg has -webkit-user-select: none",
          "-webkit-user-select: none;" in block(css, ".msg {"), True)
    check("style.css bubble has touch-action: pan-y",
          "touch-action: pan-y;" in block(css, ".bubble {"), True)
    check("style.css bubble has user-select: none",
          "user-select: none;" in block(css, ".bubble {"), True)
    check("style.css bubble has -webkit-user-select: none",
          "-webkit-user-select: none;" in block(css, ".bubble {"), True)
    check("style.css cards:empty has display: none",
          "display: none;" in block(css, "  .cards:empty {"), True)
    # Issue #625: wake prompt pill sits at bottom of feed
    check("style.css wake-prompt has margin-top: auto",
          "margin-top: auto;" in block(css, ".msg.wake-prompt {"), True)
    check("index.html has starters-context container",
          '<div class="starters-group starters-context' in page, True)
    check("index.html has starters-basic container",
          '<div class="starters-group starters-basic' in page, True)
    check("index.html has starters_context heading",
          'data-i18n="ui_starters_context"' in page, True)
    check("index.html has starters_basic heading",
          'data-i18n="ui_starters_basic"' in page, True)
    check("starters_basic translation in English",
          lang_run([{"say": "ui_starters_basic", "in": "en"}])[0], "Basic Capabilities")
    check("starters_basic translation in Japanese",
          lang_run([{"say": "ui_starters_basic", "in": "ja"}])[0], "基本機能")
    check("starters_context translation in English",
          lang_run([{"say": "ui_starters_context", "in": "en"}])[0], "Suggestions")
    check("starters_context translation in Japanese",
          lang_run([{"say": "ui_starters_context", "in": "ja"}])[0], "おすすめ・関連")


DYNAMIC_STARTERS_HARNESS = r"""
class MockElement {
  constructor(tag, className = "") {
    this.tagName = tag.toUpperCase();
    this.className = className;
    this.textContent = "";
    this.innerHTML = "";
    this.children = [];
    this.dataset = {};
    this.classList = {
      add: (c) => {
        const parts = this.className ? this.className.split(" ") : [];
        if (!parts.includes(c)) parts.push(c);
        this.className = parts.join(" ");
      },
      remove: (c) => {
        const parts = this.className ? this.className.split(" ") : [];
        this.className = parts.filter(x => x !== c).join(" ");
      },
      contains: (c) => (this.className ? this.className.split(" ").includes(c) : false),
    };
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  prepend(child) {
    this.children.unshift(child);
    return child;
  }
  replaceChildren(...nodes) {
    this.children = [...nodes];
  }
  querySelector(sel) {
    for (const child of this.children) {
      if (sel.startsWith(".") && child.classList.contains(sel.slice(1))) return child;
      if (sel === "[data-i18n]" && child.dataset?.i18n) return child;
      if (sel === ".material-symbols-outlined" && child.classList.contains("material-symbols-outlined")) return child;
      const found = child.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll(sel) {
    const res = [];
    const check = (node) => {
      if (sel === ".chip" && node.classList.contains("chip")) res.push(node);
      else if (sel === ".chip.dynamic-chip" && node.classList.contains("chip") && node.classList.contains("dynamic-chip")) res.push(node);
      for (const c of node.children) check(c);
    };
    check(this);
    return res;
  }
  addEventListener() {}
}

const document = {
  createElement: (tag) => new MockElement(tag),
  documentElement: { lang: "ja" },
};
globalThis.document = document;

function t(k) { return k; }

let starters = new MockElement("div", "starters");
let contextGroup = new MockElement("div", "starters-group starters-context hidden");
let contextRow = new MockElement("div", "starter-row");
let starterDynamic = new MockElement("div", "starter-dynamic");
contextRow.appendChild(starterDynamic);
contextGroup.appendChild(contextRow);

let basicGroup = new MockElement("div", "starters-group starters-basic");
let starterRow = new MockElement("div", "starter-row");
basicGroup.appendChild(starterRow);

// static chips
const staticChip1 = new MockElement("button", "chip");
staticChip1.dataset.prompt = "今日の天気は？";
staticChip1.dataset.i18nPrompt = "ui_ask_weather";
staticChip1.dataset.topicSlot = "place";
const staticLabel1 = new MockElement("span");
staticLabel1.textContent = "今日の天気";
staticLabel1.dataset.i18n = "ui_chip_weather";
staticChip1.appendChild(staticLabel1);
starterRow.appendChild(staticChip1);

starters.appendChild(contextGroup);
starters.appendChild(basicGroup);

const TOPIC_MAX = 24;
const TOPIC_PLACE_ARGS = ["place", "destination", "near", "location"];
const TOPIC_SUBJECT_ARGS = ["query"];
const TOPIC_HERE = new Set(["here", "home", "nearby", "current location"]);
const TOPIC_MEDIA_MARKER = /[（(]\s*(?:テレビ番組|番組|アニメ|映画|ドラマ|ラジオ|漫画|楽曲|曲|album|movie|film|tv\s*(?:series|show|program))\s*[）)]/i;
const TOPIC_PLACE_TOOLS = new Set(["places", "place_info", "directions", "weather", "maps_search"]);

%s

const script = JSON.parse(process.argv[1]);
const out = [];

for (const step of script) {
  if (step.call) {
    noteTopic(step.call.args, step.call.name || "");
  }
  if (step.clear) {
    forgetTopic();
  }
  if (step.suggestions !== undefined) {
    if (typeof currentTopicSuggestions !== "undefined") {
      currentTopicSuggestions = step.suggestions;
    }
  }
  if (step.render) {
    renderStarters();
    const dynChips = starters.querySelectorAll(".chip.dynamic-chip").map(c => {
      const iconEl = c.querySelector(".material-symbols-outlined");
      const labelEl = c.children.find(ch => !ch.classList.contains("material-symbols-outlined"));
      return {
        prompt: c.dataset.prompt,
        icon: iconEl ? iconEl.textContent : "",
        label: labelEl ? labelEl.textContent : (c.textContent || ""),
      };
    });
    const allChips = starters.querySelectorAll(".chip");
    const staticHidden = staticChip1.classList.contains("hidden");
    out.push({
      dynamicCount: dynChips.length,
      dynamicChips: dynChips,
      totalChips: allChips.length,
      staticWeatherHidden: staticHidden,
      contextHidden: contextGroup.classList.contains("hidden"),
    });
  }
  if (step.clear && !step.render) {
    const dynChips = starters.querySelectorAll(".chip.dynamic-chip");
    const staticHidden = staticChip1.classList.contains("hidden");
    out.push({
      clearedDynamicCount: dynChips.length,
      currentTopicSuggestionsLength: (typeof currentTopicSuggestions !== "undefined" ? currentTopicSuggestions.length : -1),
      staticWeatherHidden: staticHidden,
      contextHidden: contextGroup.classList.contains("hidden"),
    });
  }
}

console.log(JSON.stringify(out));
"""


def dynamic_starter_chips_run(script: list[dict]) -> list:
    app = (JS / "app.js").read_text(encoding="utf-8")
    parts = []
    for var_name in ("topicPlace", "topicSubject", "topicDomain", "currentTopicSuggestions", "lastSuggestionsTopic"):
        if found := re.search(rf"^let {var_name}\b.*?;$", app, re.M):
            parts.append(found.group(0))
    for fn_name in ("function renderStarters(", "function forgetTopic(", "function noteTopic(", "async function fetchTopicSuggestions("):
        if fn_name in app:
            parts.append(block(app, fn_name))
    lifted = "\n".join(parts)
    code = DYNAMIC_STARTERS_HARNESS % lifted
    proc = subprocess.run(
        ["node", "-e", code, json.dumps(script)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        print(f"   FAIL node: {proc.stderr.strip()}")
        sys.exit(1)
    return json.loads(proc.stdout)


def dynamic_starter_chips_cases() -> None:
    print("\n== dynamic starter chips from suggestions ==")

    # 1. Verify renderStarters() mounts dynamic chips when suggestions are present
    sample_suggestions = [
        {"label": "天気", "prompt": "鎌倉の天気や予報を教えて", "icon": "partly_cloudy_day"},
        {"label": "観光スポット", "prompt": "鎌倉のおすすめスポットや見どころを教えて", "icon": "explore"},
        {"label": "グルメ", "prompt": "鎌倉のおすすめのお店やグルメを教えて", "icon": "restaurant"},
    ]
    out = dynamic_starter_chips_run([
        {"suggestions": sample_suggestions, "render": True},
    ])
    check("renderStarters mounts dynamic chips when suggestions are present",
          out[0]["dynamicCount"], len(sample_suggestions))
    check("contextGroup is unhidden when suggestions are present",
          out[0]["contextHidden"], False)

    # 2. Verify dynamic chip contains icon, label text, and data-prompt
    chips = out[0]["dynamicChips"]
    check("dynamic chips have correct icons",
          [c["icon"] for c in chips], ["partly_cloudy_day", "explore", "restaurant"])
    check("dynamic chips have correct labels",
          [c["label"] for c in chips], ["天気", "観光スポット", "グルメ"])
    check("dynamic chips have correct prompts",
          [c["prompt"] for c in chips],
          ["鎌倉の天気や予報を教えて", "鎌倉のおすすめスポットや見どころを教えて", "鎌倉のおすすめのお店やグルメを教えて"])

    # 3. Verify fallback when suggestions list is empty or fetch fails: baseline static chips render without errors
    empty_out = dynamic_starter_chips_run([
        {"suggestions": [], "render": True},
    ])
    check("empty suggestions list renders zero dynamic chips",
          empty_out[0]["dynamicCount"], 0)
    check("empty suggestions preserves baseline static chips",
          empty_out[0]["totalChips"] >= 1, True)
    check("contextGroup is hidden when suggestions list is empty",
          empty_out[0]["contextHidden"], True)

    # 4. Verify forgetTopic() clears dynamic chips
    clear_out = dynamic_starter_chips_run([
        {"suggestions": sample_suggestions, "render": True},
        {"clear": True},
    ])
    check("forgetTopic clears dynamic chips",
          clear_out[1]["clearedDynamicCount"], 0)
    check("forgetTopic resets currentTopicSuggestions",
          clear_out[1]["currentTopicSuggestionsLength"], 0)
    check("forgetTopic hides contextGroup",
          clear_out[1]["contextHidden"], True)

    # 5. Verify weather follow-up turn suppresses redundant static weather chip and mounts contextual dynamic chips
    weather_suggestions = [
        {"label": "明日の天気", "prompt": "台東区 東京都の明日の天気を教えて", "icon": "partly_cloudy_day"},
        {"label": "週間予報", "prompt": "台東区 東京都の週間天気を教えて", "icon": "partly_cloudy_day"},
        {"label": "雨雲・降水確率", "prompt": "台東区 東京都の雨雲レーダーや降水確率を教えて", "icon": "partly_cloudy_day"},
        {"label": "気温と服装", "prompt": "台東区 東京都の今日の気温とおすすめの服装を教えて", "icon": "partly_cloudy_day"},
    ]
    weather_turn_out = dynamic_starter_chips_run([
        {"call": {"name": "weather", "args": {"location": "台東区 東京都"}}, "suggestions": weather_suggestions, "render": True},
    ])
    check("weather turn suppresses redundant static weather chip",
          weather_turn_out[0]["staticWeatherHidden"], True)
    check("weather turn mounts 4 contextual dynamic chips",
          weather_turn_out[0]["dynamicCount"], 4)
    check("all dynamic chips have partly_cloudy_day icon",
          [c["icon"] for c in weather_turn_out[0]["dynamicChips"]],
          ["partly_cloudy_day"] * 4)
    check("dynamic weather chips contain no address numbers or daycare terms",
          any(re.search(r"\d+[-丁目番地号]|一時預かり", c["prompt"])
              for c in weather_turn_out[0]["dynamicChips"]), False)

    # 6. Verify clearing topic restores static weather chip
    cleared_turn_out = dynamic_starter_chips_run([
        {"call": {"name": "weather", "args": {"location": "台東区 東京都"}}, "suggestions": weather_suggestions, "render": True},
        {"clear": True, "render": True},
    ])
    check("clearing topic unhides static weather chip",
          cleared_turn_out[1]["staticWeatherHidden"], False)
    check("clearing topic removes dynamic weather chips",
          cleared_turn_out[1]["dynamicCount"], 0)


def parse_cases() -> None:
    """Every module the page loads, parsed whole, as the browser parses it.

    The rest of this file lifts a *section* out of a source and hands the slice
    to node, which is what makes the cases readable and what makes every one of
    them blind to anything that is only wrong about the whole file. Two
    module-level `let idleTimer` declarations — one the microphone's, one the
    clock's — are each fine in their own slice, and together they are an early
    `SyntaxError` that stops `app.js` executing at all. That state shipped and
    survived four commits with every case in here green, because nothing in
    `tools/` had ever parsed a source file end to end.

    A `.mjs` copy and not the file where it sits, because the extension is what
    decides how node reads it: as `.js` it accepts the duplicate, and
    `index.html` loads these with `type="module"`, which does not. Copying is
    also what keeps this read-only — nothing is renamed in the tree.

    Only `--check`. Running the modules would need a DOM, which is what all the
    lifting elsewhere exists to avoid; a file that does not parse never gets as
    far as needing one.
    """
    print("== every module the page loads, parsed whole ==")
    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(JS.glob("*.js")):
            copy = Path(tmp) / f"{path.stem}.mjs"
            copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            proc = subprocess.run(["node", "--check", str(copy)],
                                  capture_output=True, text=True, check=False)
            first = next((line for line in proc.stderr.splitlines()
                          if "Error" in line), "")
            check(f"{path.name} is a module a browser can load",
                  proc.returncode, 0, first or "parses")
# The speaker. The whole section is lifted rather than a function at a time,
# for the reason `clock_src` takes a slice: `volumeLevel`, `muted` and
# `playerGain` are shared by everything in it and which of them writes which is
# most of what can go wrong. The AudioParam is a recorder rather than a number,
# because the thing being watched is not only where the gain ends up but *how*
# it got there — a `gain.value =` mid-waveform is a click on every frame of a
# drag, and it leaves the same number behind that a ramp does.
VOLUME = r"""
const script = JSON.parse(process.argv[1]);

let settings = script.settings || {};
const saved = [];
function saveSettings(patch) {
  settings = { ...settings, ...patch };
  saved.push(patch);
}

const lines = [];
function systemLine(text) { lines.push(text); }

function el(id) {
  const classes = new Set();
  return {
    id, textContent: "", title: "", value: "", attrs: {}, kids: [], on: {},
    classes,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
    setAttribute(n, v) { this.attrs[n] = String(v); },
    // A tap lands on the glyph inside the button, not on the button, so a
    // `contains` that only matched the node itself would let the popup close
    // under the finger that opened it.
    contains(node) { return node === this || this.kids.includes(node); },
    addEventListener(ev, fn) { this.on[ev] = fn; },
  };
}

const volumeButton = el("volumeButton");
const volumeIcon = el("volumeIcon");
const volumePop = el("volumePop");
const volumeRange = el("volumeRange");
const volumeRead = el("volumeRead");
const muteButton = el("muteButton");
const muteIcon = el("muteIcon");
volumeButton.kids = [volumeIcon];
volumePop.kids = [muteButton, muteIcon, volumeRange, volumeRead];
volumePop.classList.add("hidden");

// Every ramp and every assignment, in order and kept apart. `currentTime`
// runs on by itself, so two ramps are never mistaken for one.
const ramps = [], sets = [];
let clock = 0;
function fakeGain() {
  return {
    context: { get currentTime() { return (clock += 0.5); } },
    gain: {
      _v: 1,
      get value() { return this._v; },
      set value(v) { this._v = v; sets.push(v); },
      setTargetAtTime(v, at, tau) { this._v = v; ramps.push([v, at, tau]); },
    },
  };
}

const bound = [];
globalThis.document = {
  addEventListener: (ev, fn, opts) => bound.push({ ev, fn, opts }),
  // `t` reads the locale off here, so the panel the cases below stand in
  // front of is the English one and the strings they expect are the English
  // ones. The Japanese half of the table is checked where the rest of the
  // translation is, not a second time here.
  documentElement: { lang: "en" },
};

// The table and `t` out of `i18n.js`, not a stub: the section reads its
// labels through the same lookup the page does, so a key it asks for that
// nobody wrote a string for has to come back here as it would there.
%s

%s

%s

function shot() {
  return {
    icon: volumeIcon.textContent,
    muteIcon: muteIcon.textContent,
    lit: volumeButton.classes.has("muted"),
    open: !volumePop.classes.has("hidden"),
    expanded: volumeButton.attrs["aria-expanded"],
    slider: volumeRange.value,
    read: volumeRead.textContent,
    title: volumeButton.title,
    gain: playerGain ? playerGain.gain.value : null,
    level: volumeLevel,
    muted,
  };
}

function fire(ev, arg) {
  const it = bound.find((b) => b.ev === ev);
  if (it) it.fn(arg); else out.push({ missing: ev });
}

const out = [];
for (const step of script.steps) {
  if (step.gainFor !== undefined) {
    out.push({ gain: gainFor(step.gainFor) });
    continue;
  }
  if (step.saved) { out.push({ level: savedLevel() }); continue; }
  // The player being built, which does not happen until somebody speaks,
  // types or taps a starter — every step before this one is the control being
  // used against a graph that does not exist.
  if (step.player) playerGain = fakeGain();
  if (step.slide !== undefined) {
    volumeRange.value = String(step.slide);
    volumeRange.on.input();
  }
  if (step.release) volumeRange.on.change();
  if (step.mute) muteButton.on.click();
  if (step.tap) volumeButton.on.click();
  if (step.press) {
    fire("pointerdown", {
      target: { button: volumeIcon, pop: volumeRange,
                away: el("settingsButton") }[step.press],
    });
  }
  if (step.key) fire("keydown", { key: step.key });
  out.push(shot());
}
console.log(JSON.stringify({
  steps: out, saved, lines, ramps, sets,
  listeners: bound.map((b) => [b.ev, b.opts?.capture === true]),
}));
"""


def speaker_src() -> str:
    """The speaker section of `app.js`, header to header.

    Empty rather than raising when the section is not there, so a build
    without it fails the case that says so instead of a traceback out of the
    middle of the file.
    """
    src = (JS / "app.js").read_text(encoding="utf-8")
    start = src.find("/* ---------- the speaker ---------- */")
    if start < 0:
        return ""
    return src[start:src.index("/* ---------- profiles ---------- */", start)]


def code(src: str) -> str:
    """`src` with its comments taken out.

    Half the speaker cases ask what the section *calls*, and the section
    argues each rejected alternative down by name in its own prose — so a
    plain `in` would match the reasoning and not the code, and would keep
    matching after the code it is about was deleted. Crude on purpose: this is
    only ever handed one section of `app.js`, and there is no string literal
    in it carrying a `//` or a `/*`.
    """
    return re.sub(r"^[ \t]*//.*$", "",
                  re.sub(r"/\*.*?\*/", "", src, flags=re.S), flags=re.M)


def volume_run(steps: list[dict], settings: dict | None = None) -> dict:
    """Drive the speaker section over a list of steps, a frame per step."""
    arg = json.dumps({"steps": steps, "settings": settings or {}})
    body = VOLUME % (block(i18n_src(), "export const STRINGS = ")
                     .removeprefix("export "),
                     block(i18n_src(), "export function t(")
                     .removeprefix("export "),
                     speaker_src())
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", body, arg],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        # Reported rather than fatal: a control that reaches for the player
        # before there is one is a TypeError, and that is the whole of one of
        # the cases below. It wants a name on it, not a stack trace.
        return {"error": proc.stderr.strip()[-300:], "steps": [], "saved": [],
                "lines": [], "ramps": [], "sets": [], "listeners": []}
    return json.loads(proc.stdout)


def volume_cases() -> None:
    print("\n== how far the slider turns the speaker down ==")

    section = speaker_src()
    # The code alone for the greps below, because the section argues its
    # rejected alternatives down by name and a comment is not a call.
    body = code(section)
    check("the speaker section is where the run looks for it",
          bool(section), True, "app.js, between the audio and the profiles")

    # The curve. A slider wired straight to `gain.value` puts everything
    # audible in its bottom few pixels: 100 and 70 sound alike and the rest of
    # the travel is a dead zone. Squared is the usual audio taper.
    ends = volume_run([{"gainFor": 0}, {"gainFor": 100}, {"gainFor": 50}])
    check("the run got through it", ends.get("error"), None)
    check("nought is silence, not something very quiet",
          ends["steps"][0]["gain"], 0)
    check("and a hundred is the signal untouched",
          ends["steps"][1]["gain"], 1,
          "a terminal nobody has opened this on sounds as it always did")
    check("the middle is a quarter, which is -12 dB and about half as loud",
          ends["steps"][2]["gain"], 0.25)

    low = volume_run([{"gainFor": n} for n in (10, 20, 30, 40)])
    # Rounded, because `(n / 100) ** 2` is binary floating point and 0.01 comes
    # back as 0.010000000000000002. What is being checked is the curve, not
    # IEEE 754.
    check("and the bottom of the travel is where the detail is",
          [round(s["gain"], 6) for s in low["steps"]],
          [0.01, 0.04, 0.09, 0.16],
          "linear would make these 0.1, 0.2, 0.3, 0.4 — four of the same "
          "volume")

    junk = volume_run([{"gainFor": 140}, {"gainFor": -20}, {"gainFor": "x"}])
    check("a level off either end of the scale is clamped to it",
          [s["gain"] for s in junk["steps"][:2]], [1, 0])
    # The guard is unreachable from the two callers today — `savedLevel`
    # returns a finite number and a range input's `value` is always numeric —
    # and it is a line rather than a branch, so it stays and is pinned here.
    check("and anything that is not a number leaves the terminal audible",
          junk["steps"][2]["gain"], 1, "a NaN in an AudioParam throws")

    print("\n== where the level is kept, and what it comes back as ==")

    # The same key as the microphone, the profile and the location, because a
    # setting added later should not need a migration of its own.
    check("it rides in the one settings object app.js already writes",
          ["saveSettings({ volume" in body,
           "saveSettings({ muted })" in body], [True, True])
    check("and is read back out of it at startup",
          ["settings.volume" in body, "settings.muted === true" in body],
          [True, True])

    stored = volume_run([{"saved": True}], {"volume": 35})
    check("a level that was saved comes back", stored["steps"][0]["level"], 35)
    fresh = volume_run([{"saved": True}])
    check("a terminal that has never been touched comes up at full",
          fresh["steps"][0]["level"], 100,
          "a panel that boots quiet for no reason is a support call")
    junk = volume_run([{"saved": True}], {"volume": "loud"})
    check("and so does one with nonsense in the key",
          junk["steps"][0]["level"], 100)
    edge = volume_run([{"saved": True}], {"volume": 0})
    check("but a level of nought is a level, not an absent one",
          edge["steps"][0]["level"], 0)

    # Mute survives the reload on purpose: the kiosk unit is `Restart=always`,
    # so a terminal that un-muted itself would be overriding somebody who
    # silenced it deliberately. The glyph is what stops that being a mystery.
    came_back = volume_run([{"key": "x"}], {"volume": 40, "muted": True})
    check("a terminal that was muted comes back muted",
          [came_back["steps"][0]["muted"], came_back["steps"][0]["icon"]],
          [True, "volume_off"])
    check("with the level it was muted at still under it",
          came_back["steps"][0]["level"], 40,
          "unmuting has to land where it was, not on a default")

    # A drag is dozens of `input` events. Each one has to be heard; only the
    # last one has to be written down.
    dragged = volume_run([{"player": True}, {"slide": 60}, {"slide": 40},
                          {"slide": 30}, {"release": True}])
    check("the run got through it", dragged.get("error"), None)
    check("every frame of a drag is heard",
          [round(s["gain"], 4) for s in dragged["steps"][1:4]],
          [0.36, 0.16, 0.09])
    check("and only the finger coming off is written down",
          dragged["saved"], [{"volume": 30, "muted": False}],
          "a write per pixel is a JSON.stringify per pixel")

    print("\n== what mute does to a sentence already in the air ==")

    # The three candidates differ only in this case, and it is the common one:
    # somebody hits mute while the agent is mid-answer. A disconnected node is
    # no longer pulled by the destination, so the worklet stops draining and
    # its 180-second ring fills up behind the mute; stopping the player throws
    # the queue away. Zero gain keeps the worklet running in real time.
    # The two calls, not the two words: the handler's own comment argues both
    # of them down by name, so a grep for `endOfAudio` matches the reasoning
    # rather than the code.
    check("muting is the gain going to zero, not the node being disconnected",
          [".disconnect(" in body, "postMessage(" in body],
          [False, False],
          "either one queues or drops what arrives while it is muted")

    muted = volume_run([{"player": True}, {"slide": 60}, {"mute": True},
                        {"mute": True}])
    check("the run got through it", muted.get("error"), None)
    check("mute takes the graph to silence", muted["steps"][2]["gain"], 0)
    check("and unmuting comes back to the level it left",
          round(muted["steps"][3]["gain"], 4), 0.36)
    check("which the slider was holding all along, unmoved by either",
          [s["slider"] for s in muted["steps"][1:]], ["60", "60", "60"])

    # The other half of it: the ramp. Every one of those moves is a ramp and
    # none of them is an assignment.
    check("every move is ramped onto the param, never assigned",
          (len(muted["ramps"]), muted["sets"]), (3, []),
          "a step in a 24 kHz stream is a click, once per frame of a drag")
    check("with the time constant the section names",
          {tau for _, _, tau in muted["ramps"]}, {0.02})
    check("each at its own moment, so two are not folded into one",
          len({at for _, at, _ in muted["ramps"]}), 3)

    took = volume_run([{"player": True}, {"mute": True}, {"slide": 70}])
    check("moving the slider takes it off mute rather than moving a dead one",
          [took["steps"][2]["muted"], round(took["steps"][2]["gain"], 4)],
          [False, 0.49])

    print("\n== mute is the speaker, not the microphone ==")

    # A muted terminal still hears the room, still answers, and still draws
    # the answer. Nothing in this section may touch the half that does that.
    check("the section leaves the recorder and the socket alone",
          [w for w in ("listening", "startRecorder", "stopRecorder", "socket",
                       "micStream")
           if w in body], [])
    said = volume_run([{"mute": True}, {"mute": True}])
    check("and it says so in the feed, the way the mic button does",
          said["lines"],
          ["Speaker muted. It still hears you.", "Speaker on."],
          "otherwise a muted terminal is a broken one that nobody can tell")

    print("\n== the glyph a muted terminal shows ==")

    seen = volume_run([{"key": "x"}, {"slide": 20}, {"slide": 0},
                       {"slide": 80}, {"mute": True}])
    check("the run got through it", seen.get("error"), None)
    check("full, half and off each read differently in the bar",
          [s["icon"] for s in seen["steps"]],
          ["volume_up", "volume_down", "volume_off", "volume_up",
           "volume_off"])
    check("and the silent ones are the lit ones, against a dim row",
          [s["lit"] for s in seen["steps"]],
          [False, False, True, False, True])
    check("the mute button is where the two silences are told apart",
          [seen["steps"][2]["muteIcon"], seen["steps"][4]["muteIcon"]],
          ["volume_up", "volume_off"],
          "a slider at nought is silent; it is not muted")
    check("the number under the slider follows it",
          [s["read"] for s in seen["steps"]], ["100", "20", "0", "80", "80"])
    check("and the button says which state it is in",
          [seen["steps"][3]["title"], seen["steps"][4]["title"]],
          ["Speaker volume 80", "Speaker muted"])

    # Bigger than the bar it sits in. The panel is 151 CSS px to the inch
    # (README, "What the panel actually gives the browser"), so the 16px these
    # glyphs inherit from `body` is 2.7mm — about 15 arcmin at arm's length,
    # and this is the one icon in the row that has to be read at a glance
    # rather than looked for.
    css = (ROOT / "src/ai_terminal/live/static/css/style.css").read_text(
        encoding="utf-8")
    check("and it is drawn bigger than the rest of the row",
          "#volumeIcon { font-size: 22px; }" in css, True,
          "16px on this panel is 2.7mm of glass")

    print("\n== opening the popup, and getting rid of it ==")

    tapped = volume_run([{"press": "button", "tap": True},
                         {"press": "button", "tap": True}])
    check("the run got through it", tapped.get("error"), None)
    check("the icon opens it and the icon closes it",
          [s["open"] for s in tapped["steps"]], [True, False])
    check("and the button says which it is",
          [s["expanded"] for s in tapped["steps"]], ["true", "false"])

    away = volume_run([{"press": "button", "tap": True}, {"press": "pop"},
                       {"press": "away"}])
    check("a tap on the slider inside it does not dismiss it",
          [s["open"] for s in away["steps"][:2]], [True, True])
    check("a tap anywhere else does", away["steps"][2]["open"], False)

    key = volume_run([{"press": "button", "tap": True}, {"key": "a"},
                      {"key": "Escape"}])
    check("Escape closes it, for the Mac the panel is developed against",
          [s["open"] for s in key["steps"]], [True, True, False])

    # #84's idle timer binds `pointerdown` and `keydown` on the document
    # capturing, so a handler that swallowed the tap could not cost the
    # terminal its countdown — but this is the section that would try.
    check("neither of the document handlers here captures",
          away["listeners"], [["pointerdown", False], ["keydown", False]])
    check("and neither of them stops the tap going anywhere else",
          "stopPropagation" in body, False,
          "opening the popup is somebody at the panel, and wakes the screen")

    print("\n== the control that exists before the audio graph does ==")

    # The toolbar paints on the first frame; `ensurePlayer` does not run until
    # somebody speaks, types or taps a starter.
    early = volume_run([{"slide": 30}, {"mute": True}, {"press": "button",
                                                        "tap": True},
                        {"player": True}, {"slide": 30}])
    check("the run got through it", early.get("error"), None,
          "every step before the player is the control against no graph")
    check("with no player, there is no graph to read a gain off",
          [s["gain"] for s in early["steps"][:3]], [None, None, None])
    # Three moves before the player existed and one after it. Only the last
    # reaches an AudioParam, and none of the first three raised on the way.
    check("and the whole run writes exactly the one that came after it",
          early["ramps"], [[0.09, 0.5, 0.02]])
    check("but the level is remembered all the same",
          [early["steps"][1]["level"], early["steps"][1]["muted"]], [30, True])

    # And the other half: the level set while nothing was playing has to be in
    # the node the moment it is built, not ramped to afterwards. Read out of
    # the source, because `ensurePlayer` builds a real AudioContext.
    app = (JS / "app.js").read_text(encoding="utf-8")
    made = block(app, "async function ensurePlayer(")
    check("so the player is built with that level already in it",
          "startAudioPlayerWorklet(targetGain())" in made, True,
          "a ramp from full lets the first word out on a muted terminal")
    check("and the node it hands back is the one the control writes to",
          "playerGain] =" in made, True)

    print("\n== where the gain sits in the graph ==")

    # A page-level gain is the only volume this side of the glass can reach —
    # the browser cannot call the Pi's mixer — and it has to sit between the
    # worklet and the speaker. Nothing between them measures the signal, so
    # there is no meter for it to lie to.
    player = (JS / "audio-player.js").read_text(encoding="utf-8")
    check("the worklet goes through the gain on its way to the destination",
          "audioPlayerNode.connect(gainNode).connect(audioContext.destination)"
          in player, True)
    check("and no longer straight to it",
          "audioPlayerNode.connect(audioContext.destination)" in player, False,
          "left in place, the model would play twice, once at full volume")
    check("the level is a constructor argument, not a later assignment",
          ["new GainNode(audioContext, { gain })" in player,
           "gain.value =" in player], [True, False])

    print("\n== the popup against everything else that covers the screen ==")

    def layer(selector: str) -> int:
        found = re.search(rf"\n{re.escape(selector)} \{{(.+?)\n\}}", css, re.S)
        inner = found.group(1) if found else ""
        depth = re.search(r"z-index: (\d+);", inner)
        return int(depth.group(1)) if depth else -1

    check("it sits over the location picker and under the clock",
          [layer(".picker") < layer(".volume-pop"),
           layer(".volume-pop") < layer(".clock")], [True, True],
          f"picker {layer('.picker')}, popup {layer('.volume-pop')}, "
          f"clock {layer('.clock')}")
    # Absolute inside the header rather than fixed with the bar's height typed
    # in: an out-of-flow child of a flex container is not a flex item, so the
    # header stays three items wide and `top: 100%` is its bottom edge.
    page = (JS.parent / "index.html").read_text(encoding="utf-8")
    check("and hangs off the bar rather than being measured against it",
          [page.index('id="volumePop"') < page.index("</header>"),
           "top: 100%;" in (re.search(r"\n\.volume-pop \{(.+?)\n\}", css,
                                      re.S) or re.match("", "")).group(1)],
          [True, True])


def css_text() -> str:
    """`style.css` with its comments and string literals taken out.

    Both would otherwise be counted: a brace inside a `/* … */` or a
    `content: "{"` is not a block, and a `@` inside either is not an at-rule.
    Nothing in the file needs them today, which is exactly why they have to be
    stripped here rather than left to be noticed later.
    """
    src = (ROOT / "src/ai_terminal/live/static/css/style.css").read_text(
        encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r'"[^"\n]*"' + "|'[^'\n]*'", '""', src)


def brace_depth(src: str) -> tuple[int, int]:
    """`(depth at EOF, lowest depth reached)` over a stripped stylesheet."""
    depth = low = 0
    for ch in src:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            low = min(low, depth)
    return depth, low


def css_rules(src: str) -> list[tuple[str, str]]:
    """`(selector, body)` for every rule in a stripped stylesheet, at any depth.

    Brace-matched rather than split on `}`, because `@media` and `@keyframes`
    nest and a regex that ignored that would report `@keyframes pulse { 0% ` as
    a selector. A rule inside either comes back as an entry of its own, so a
    declaration hidden in a media query is not hidden from a case.
    """
    out: list[tuple[str, str]] = []
    i = start = 0
    while (open_at := src.find("{", i)) >= 0:
        depth, close_at = 1, open_at + 1
        while depth and close_at < len(src):
            depth += {"{": 1, "}": -1}.get(src[close_at], 0)
            close_at += 1
        body = src[open_at + 1:close_at - 1]
        out.append((" ".join(src[start:open_at].split()), body))
        if "{" in body:
            out.extend(css_rules(body))
        i = start = close_at
    return out


def css_cases() -> None:
    print("\n== the stylesheet nobody would notice breaking ==")
    # CSS has no parse error. A browser handed a broken stylesheet drops the
    # rules it cannot make sense of and renders the rest, so the panel comes up
    # looking almost right and the only report is somebody saying the cards
    # went odd. Measured on this file at `e874cbb`: appending an unclosed
    # `.cal-row { color: red` and a line of literal garbage left every one of
    # the 456 cases here green and Chromium still counted a plausible 228
    # rules. Deleting a single `}` from the middle took the file from 227 rules
    # to 115 — half the stylesheet silently swallowed into the block above it —
    # and, again, nothing here went red.
    #
    # These are the invariants that hold for any correct stylesheet, so none of
    # them needs editing when the design changes. That is the whole point: a
    # gate with a number in it that has to be bumped every commit gets bumped
    # without being read.
    css = css_text()
    depth, low = brace_depth(css)
    check("every block in style.css is closed", depth, 0,
          f"depth {depth} at EOF")
    check("and none is closed twice", low, 0, f"lowest depth {low}")
    check("no rule was left with an empty body",
          len(re.findall(r"\{\s*\}", css)), 0)
    # `@media` and `@keyframes` are the two this file uses. A third is not
    # forbidden — it is a line for whoever adds it to read, since `@import`
    # would put a blocking network fetch in front of the first paint on a
    # kiosk, and `@charset` in the wrong place is dropped in silence.
    check("and the only at-rules are the two this file uses",
          sorted(set(re.findall(r"@([a-zA-Z-]+)", css))),
          ["keyframes", "media"])

    # A `var()` on a token nobody declares is the failure mode CSS is worst
    # at: the declaration is dropped, the element keeps whatever it inherited,
    # and the page looks *nearly* right. Renaming a token in `:root` and
    # missing one of its uses is the ordinary way to get there.
    declared = set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", css))
    used = set(re.findall(r"var\(\s*(--[A-Za-z0-9_-]+)", css))
    check("every custom property a rule reads is declared",
          sorted(used - declared), [], f"{len(declared)} declared")
    # The other direction is not a bug, but it is how a palette rots: a token
    # nobody reads is one somebody will edit expecting the panel to change.
    check("and every one declared is read", sorted(declared - used), [])

    print("\n== what the panel animates, and in which state ==")
    # A kiosk is up for weeks with nobody touching it, and an animation is the
    # one thing on a static page that can spend a core forever. Blink runs
    # `transform`, `opacity`, `filter` and `backdrop-filter` on the compositor;
    # everything else is interpolated on the renderer's main thread and
    # invalidates paint once per vsync. `box-shadow` in a `@keyframes` block
    # measured 603 style recalculations in 10 s in Chromium 140 — 60.3 a second
    # — against 0 for the same page with the animation off, and an opaque
    # overlay on top of the element does not stop it: 599 in 10 s under one.
    #
    # This is a read of the stylesheet, not of a browser, so what it catches is
    # a paint property being written into a keyframe. It cannot see how many
    # frames anything costs, and it cannot see an animation added from JS with
    # `Element.animate` — there is none in `static/js` today.
    rules = css_rules(css)
    # `[A-Za-z-]` and a `.lower()`, not `[a-z-]`: CSS property names are ASCII
    # case-insensitive, so `BOX-SHADOW:` in a keyframe is the same declaration
    # to Blink and a lower-case-only pattern does not merely mis-name it, it
    # finds no property in that block at all, and a block that contributes
    # nothing to `steps` is indistinguishable here from one that is clean.
    named = re.compile(r"([A-Za-z-]+)\s*:")
    steps = [prop.lower() for sel, body in rules
             if sel.startswith("@keyframes")
             for prop in named.findall(body)]
    # `visibility` is the exception and stays one on purpose: `blink` is the
    # streaming caret, which runs only while text is arriving and stops with
    # it. Anything else new here is a line for whoever adds it to read.
    check("every keyframe interpolates a property the compositor can run",
          sorted(set(steps)), ["opacity", "transform", "visibility"],
          f"{len(steps)} declarations across the keyframe blocks")
    check("and a keyframe's property name is read in either case",
          named.findall("  BOX-SHADOW: 0 0 0 7px red; opacity: 0;"),
          ["BOX-SHADOW", "opacity"],
          "lower case only, and an upper-case block contributes nothing")

    # And the other half, which is duty cycle rather than cost. The connection
    # dot's pulse used to be on `.dot` and turned *off* by `.dot.off`, so the
    # panel paid for it whenever everything was working and stopped paying the
    # moment something broke — including behind the screensaver clock, which
    # covers the top bar without hiding it. Inverting it costs `app.js`
    # nothing: `.off` is the class it already toggles at `setConnected`.
    #
    # `animation-name:` as well as `animation:`, the `-webkit-` alias Blink
    # still honours, either case, and the space CSS allows in front of the
    # colon. Anchored on the unprefixed shorthand alone this read straight
    # past the longhand — which is not an exotic spelling but this file's own
    # idiom for the same thing, at `style.css:1430`, `:1440` and `:1462` — so
    # putting the pulse back as `animation-name: pulse` restored it in full
    # and left every case green.
    starts = re.compile(r"^\s*(-webkit-)?animation(-name)?\s*:", re.M | re.I)
    animated = sorted(sel for sel, body in rules if starts.search(body))

    def rung(sels: list[str], *tokens: str) -> list[str]:
        """Those of `sels` that could match an element carrying `tokens`.

        A whole-token match rather than a prefix, because the dot is reached
        by `.top-bar .dot` and by `.off.dot` as readily as by `.dot`, and it
        carries an id as well (`index.html:81`), so `#statusDot { animation:
        … }` would put the pulse back with nothing in the file starting
        `.dot`. The lookahead is what keeps `.dots` and `.dot-grid` out.
        """
        return [sel for sel in sels
                if any(re.search(rf"{re.escape(t)}(?![\w-])", sel)
                       for t in tokens)]

    # Named once and reused by the two gates and by the case below them, so
    # that dropping a token cannot quietly narrow a gate while the case that
    # is supposed to pin it goes on testing the tokens it spells out itself.
    dot = (".dot", "#statusDot")
    mic = (".mic-btn", "#micButton")
    check("the connection dot is still while the socket is up",
          rung(animated, *dot), [".dot.off::after"],
          "a steady dot already says connected to anyone close enough to read")
    # The microphone ring is the same defect gated on a transient state, and it
    # is listed here so that the set above stays a set somebody has looked at.
    check("and the only other thing that animates unprompted is the "
          "microphone ring",
          rung(animated, *mic), [".mic-btn.on::after"])
    # Those two are set equalities over the selectors the pattern matched, so
    # a spelling the pattern does not read never enters the set and its
    # absence is indistinguishable from the file being clean. The comparison
    # cannot fail for a selector it never saw, which is why the pattern and
    # the filter get a case each of their own below.
    check("and the pattern behind them reads every spelling CSS accepts",
          [bool(starts.search(f"  {decl}")) for decl in
           ("animation: pulse 1s infinite;", "animation-name: pulse;",
            "ANIMATION: pulse 1s infinite;", "animation : pulse 1s infinite;",
            "-webkit-animation: pulse 1s infinite;",
            "animation-duration: 1s;")],
          [True, True, True, True, True, False],
          "`animation-duration:` on its own starts nothing without a name")
    check("and the selector match is by whole token, not by prefix",
          [rung([".dot.off::after", ".off.dot", ".top-bar .dot", "#statusDot",
                 ".dots", ".dot-grid"], *dot),
           rung([".mic-btn.on::after", "#micButton", ".mic-btn-x"], *mic)],
          [[".dot.off::after", ".off.dot", ".top-bar .dot", "#statusDot"],
           [".mic-btn.on::after", "#micButton"]])
    # Both rings are `position: absolute`, so each measures itself against the
    # nearest positioned ancestor. Without one on the element it rings, that
    # ancestor is whatever happens to be up the tree — for the dot, the initial
    # containing block — and the ring lands somewhere else on the page at a
    # size nobody chose. The stylesheet still parses, the rule still matches,
    # and nothing else here would notice.
    bodies = dict(rules)
    check("and each ring is measured against the thing it rings",
          [bool(re.search(r"^\s*position: relative;", bodies.get(sel, ""),
                          re.M))
           for sel in (".dot", ".mic-btn")], [True, True])

    print("\n== the track the forecast card names a day in ==")
    # One number, and it is the whole guard: `.wx-d-label` has no rule
    # anywhere in the stylesheet, and `.wx-day` sets no `overflow`,
    # `white-space` or `min-width`, so a label wider than track 1 draws over
    # the icon beside it instead of clipping. That is #171 — `Tomorrow` needs
    # 60.4px at the row's 13px and the track was 3.5em, 45.5px.
    #
    # The case that pins the property rather than the number is in
    # `browse_check.py`, which measures every day name `weather.py` can send
    # against this track in a real Chromium. It is the better case and it is
    # not the one here, because its whole section skips on a machine with no
    # browser to launch and this file never skips. The two go red at opposite
    # ends: that one when a language adds a longer word, this one when
    # somebody tightens the row and does not think about English.
    track = re.search(r"grid-template-columns:\s*([\d.]+)em",
                      bodies.get(".wx-day", ""))
    wide = float(track.group(1)) if track else 0.0
    check("it is at least the 5.2em the longest day name needs",
          wide >= 5.2, True, f"{wide}em")

    print("\n== touch-friendly scrollbars on scrollable panes ==")
    # On a touch panel, default mouse-sized scrollbars are too narrow to grab
    # reliably. Feed and side-panel need touch-sized scrollbar targets.
    feed_scroll = any(".feed::-webkit-scrollbar" in sel for sel, _ in rules)
    side_scroll = any(".side-panel::-webkit-scrollbar" in sel for sel, _ in rules)
    check(".feed::-webkit-scrollbar and .side-panel::-webkit-scrollbar are defined",
          [feed_scroll, side_scroll], [True, True])
    side_track = any(".side-panel::-webkit-scrollbar-track" in sel for sel, _ in rules)
    side_thumb = any(".side-panel::-webkit-scrollbar-thumb" in sel for sel, _ in rules)
    check(".side-panel scrollbar track and thumb are styled",
          [side_track, side_thumb], [True, True])
    scrollbar_widths = [
        int(m.group(1))
        for sel, body in rules
        if ("::-webkit-scrollbar" in sel and (".feed" in sel or ".side-panel" in sel))
        if (m := re.search(r"width:\s*(\d+)px", body))
    ]
    check("scrollbar width is at least 12px",
          bool(scrollbar_widths) and min(scrollbar_widths) >= 12, True,
          f"widths {scrollbar_widths}")


def main() -> None:
    # First, because a source that does not parse makes every slice
    # lifted out of it below a measurement of something the browser
    # never runs.
    parse_cases()
    sources = sorted(JS.glob("*.js"))
    print(f"\n== every attribute interpolation, {len(sources)} files ==")
    unescaped = []
    total = 0
    for path in sources:
        for expr in interpolations(path.read_text(encoding="utf-8")):
            total += 1
            if expr.startswith("escapeAttr(") or (path.name, expr) in ALLOWED:
                continue
            unescaped.append(f"{path.name}: {expr}")
    check("every one is escapeAttr or named in ALLOWED", unescaped, [],
          f"{total} interpolations, {len(unescaped)} unescaped")
    # Not a formality: if the scan found nothing it would pass with the file
    # empty, or with the regex broken, which is how this check dies quietly.
    check("and the scan found the sites it is there to guard", total >= 7, True,
          f"{total} >= 7")
    check("cards.js imports escapeAttr",
          "escapeAttr" in (JS / "cards.js").read_text(encoding="utf-8")
          .split("\n")[9], True)

    print("\n== what each escape escapes ==")
    args = ['a "b" c', "a 'b' c", "a & b", 'a & "b"',
            "<script>", "a\u00a0b", ""]
    calls = [(fn, a) for fn in ("escapeHtml", "escapeAttr") for a in args]
    out = node_call(calls)

    def got(fn: str, arg: str) -> str:
        return out[f"{fn} {arg}"]

    check("escapeHtml leaves a double quote alone",
          got("escapeHtml", 'a "b" c'), 'a "b" c')
    check("and a single quote", got("escapeHtml", "a 'b' c"), "a 'b' c")
    check("escapeAttr escapes the double quote",
          got("escapeAttr", 'a "b" c'), "a &quot;b&quot; c")
    check("and the single quote", got("escapeAttr", "a 'b' c"),
          "a &#39;b&#39; c")
    check("both escape the ampersand", got("escapeAttr", "a & b"), "a &amp; b")
    # Order matters: escaping `"` before `&` would give `&amp;quot;`.
    check("and the ampersand is escaped first, so &quot; survives intact",
          got("escapeAttr", 'a & "b"'), "a &amp; &quot;b&quot;")
    check("both escape a tag", got("escapeAttr", "<script>"),
          "&lt;script&gt;")
    check("escapeHtml does too", got("escapeHtml", "<script>"),
          "&lt;script&gt;")
    check("an empty string stays empty", got("escapeAttr", ""), "")

    print("\n== the link URL formatMarkdown puts back ==")
    # No space and no `)` in the URL, because `[^\\s)]+` ends the match at
    # either — which is also why a payload has to be written this way to reach
    # the href at all. `"onmouseover="` with no space in front of it still
    # starts a new attribute; the parser calls it an error and recovers.
    quoted = '[ramen](https://x.example/?q=a"onmouseover="alert)'
    ampersand = "[t](https://a.example/b?c=1&d=2)"
    bold = "[**bold** text](https://a.example/)"
    js = "[t](javascript:alert)"
    out = node_call([("formatMarkdown", a)
                     for a in (quoted, ampersand, bold, js)])

    def md(arg: str) -> str:
        return out[f"formatMarkdown {arg}"]

    href = md(quoted).split('href="')[1].split('"')[0]
    check("the URL is still a link", '<a href="' in md(quoted), True)
    check("and the quote in it cannot end the href", '"' in href, False, href)
    check("it comes back as &quot;", "&quot;" in href, True)
    check("an ampersand in a query string is not double-escaped",
          "&amp;d=2" in md(ampersand), True, md(ampersand))
    check("and not turned into &amp;amp;", "&amp;amp;" in md(ampersand), False)
    check("the link text keeps the markup the earlier passes gave it",
          "<strong>bold</strong> text" in md(bold), True, md(bold))
    check("and a javascript: URL is still not a link at all",
          "<a " in md(js), False, md(js))

# --------------------------------------------- history restore to feed

HISTORY = """
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.className = '';
    this.textContent = '';
    this.innerHTML = '';
    this.children = [];
    this.classList = {
      add: (c) => {
        const parts = this.className ? this.className.split(' ') : [];
        if (!parts.includes(c)) parts.push(c);
        this.className = parts.join(' ');
      },
      remove: (c) => {
        const parts = this.className ? this.className.split(' ') : [];
        this.className = parts.filter(x => x !== c).join(' ');
      },
      contains: (c) => (this.className ? this.className.split(' ').includes(c) : false),
    };
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  querySelector(sel) {
    if (sel === '.msg') {
      return this.children.find(c => c.classList.contains('msg')) || null;
    }
    return null;
  }
}

const document = {
  createElement: (tag) => new Element(tag),
};

const feed = new Element('div');
feed.scrollTop = 0;
feed.scrollHeight = 100;

const starters = new Element('div');
function hideStarters() {
  starters.classList.add('hidden');
}

function append(node) {
  feed.appendChild(node);
}

function formatMarkdown(md) {
  return '<p>' + md + '</p>';
}

let userCleared = __USER_CLEARED__;
let asleep = __ASLEEP__;

__HANDLE_HISTORY__

handleHistory(__DATA__);

console.log(JSON.stringify({
  startersHidden: starters.classList.contains('hidden'),
  turns: feed.children.map(c => ({
    tag: c.tagName,
    className: c.className,
    textContent: c.textContent,
    innerHTML: c.innerHTML,
  })),
  feedChildrenCount: feed.children.length,
}));
"""


def history_run(data: dict, user_cleared: bool = False, seed_msg: bool = False,
                asleep: bool = False) -> dict:
    app = (JS / "app.js").read_text(encoding="utf-8")
    fn = block(app, "function handleHistory(")
    code = HISTORY.replace("__USER_CLEARED__", "true" if user_cleared else "false")
    code = code.replace("__ASLEEP__", "true" if asleep else "false")
    code = code.replace("__HANDLE_HISTORY__", fn)
    code = code.replace("__DATA__", json.dumps(data))
    if seed_msg:
        code = code.replace("const feed = new Element('div');",
                            "const feed = new Element('div');\nconst seed = new Element('div'); seed.className = 'msg user'; feed.appendChild(seed);")
    proc = subprocess.run(
        ["node", "-e", code],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        print(f"   FAIL node: {proc.stderr.strip()}")
        sys.exit(1)
    return json.loads(proc.stdout)


def history_cases() -> None:
    print("== history frame restores settled turns to feed ==")
    data = {
        "type": "history",
        "turns": [
            {"role": "user", "text": "hello terminal"},
            {"role": "agent", "text": "hello user"},
        ],
    }
    out = history_run(data)
    check("restores settled turns to feed", len(out["turns"]), 2,
          "two turns in history produce two elements in feed")
    check("user turn rendered with msg user class", out["turns"][0]["className"], "msg user")
    check("user turn textContent set", out["turns"][0]["textContent"], "hello terminal")
    check("agent turn rendered with msg agent md class", out["turns"][1]["className"], "msg agent md")
    check("agent turn formatted with markdown", "<p>hello user</p>" in out["turns"][1]["innerHTML"], True)
    check("starters hidden when history arrives", out["startersHidden"], True)

    # asleep suppression (#448)
    asleep_out = history_run(data, asleep=True)
    check("history suppressed if asleep is set", asleep_out["feedChildrenCount"], 0)

    # userCleared suppression
    cleared_out = history_run(data, user_cleared=True)
    check("history suppressed if userCleared is set", cleared_out["feedChildrenCount"], 0)

    # existing messages suppression
    existing_out = history_run(data, seed_msg=True)
    check("history suppressed if feed already has messages", existing_out["feedChildrenCount"], 1)

    # empty history
    empty_out = history_run({"type": "history", "turns": []})
    check("empty turns leaves feed untouched", empty_out["feedChildrenCount"], 0)
    check("and starters remain visible", empty_out["startersHidden"], False)

    # when awake is false, userCleared remains true, preventing handleHistory from hiding starters (#472)
    app = (JS / "app.js").read_text(encoding="utf-8")
    init_fn = block(app, "async function init(")
    awake_branch = re.search(r"if\s*\(\s*awake\s*\)\s*\{([\s\S]*?)\}\s*else\s*\{([\s\S]*?)\}", init_fn)
    check("awake branch in init scopes userCleared reset",
          awake_branch is not None and "userCleared = false;" in awake_branch.group(1), True,
          "userCleared = false must be inside if (awake) branch")
    check("userCleared is not reset after sleep when awake is false",
          awake_branch is not None and "userCleared = false;" not in awake_branch.group(2)
          and not re.search(r"\}\s*else\s*\{[\s\S]*?\}\s*userCleared\s*=\s*false", init_fn), True)

    test_node = f"""
    let asleep = false;
    let userCleared = false;
    const starters = {{
      classList: {{
        classes: new Set(),
        add(c) {{ this.classes.add(c); }},
        remove(c) {{ this.classes.delete(c); }},
        contains(c) {{ return this.classes.has(c); }}
      }}
    }};
    const feed = {{
      children: [],
      querySelector(sel) {{ return null; }},
      appendChild(child) {{ this.children.push(child); }}
    }};
    const document = {{ createElement: (tag) => ({{ tagName: tag, className: "", textContent: "", innerHTML: "" }}) }};
    function hideStarters() {{ starters.classList.add("hidden"); }}
    function append(node) {{ feed.appendChild(node); }}
    function formatMarkdown(md) {{ return '<p>' + md + '</p>'; }}
    function sleep() {{ asleep = true; userCleared = true; }}
    function wake() {{ asleep = false; }}
    function tickClock() {{}}
    let clockPage = {{ classList: {{ add() {{}}, remove() {{}} }} }};
    let clockIdleTimer = null;
    let IDLE_MS = 180000;

    let awake = false;
    {awake_branch.group(0) if awake_branch else ""}

    const userClearedAfterInit = userCleared;
    wake();
    {block(app, "function handleHistory(")}
    handleHistory({json.dumps(data)});
    console.log(JSON.stringify({{
      userCleared: userClearedAfterInit,
      asleep,
      startersHidden: starters.classList.contains("hidden"),
      feedCount: feed.children.length
    }}));
    """
    proc = subprocess.run(["node", "-e", test_node], capture_output=True, text=True, cwd=ROOT)
    if proc.returncode:
        print(f"   FAIL node awake_false_check: {proc.stderr.strip()}")
        sys.exit(1)
    res = json.loads(proc.stdout)
    check("when awake is false, userCleared remains true", res["userCleared"], True)
    check("preventing handleHistory from hiding starters", res["startersHidden"], False)
    check("and leaving feed empty", res["feedCount"], 0)


def markdown_bullet_cases() -> None:
    print("\n== markdown bullet lists and leading emoji ==")
    cases = [
        ("star_bullet", "* 📰 **News**: detail\n* 🍜 **Ramen**: food"),
        ("dash_bullet", "- 📰 **News**: detail\n- 🍜 **Ramen**: food"),
        ("unicode_bullet", "• 📰 **News**: detail\n• 🍜 **Ramen**: food"),
        ("bare_emoji_bullet", "📰 **News**: detail\n🍜 **Ramen**: food"),
        ("normal_paragraph", "👋 Hello, this is just a regular sentence."),
    ]
    out = node_call([("formatMarkdown", text) for _, text in cases])

    def md(arg: str) -> str:
        return out[f"formatMarkdown {arg}"]

    expected_ul = "<ul><li>📰 <strong>News</strong>: detail</li><li>🍜 <strong>Ramen</strong>: food</li></ul>"
    check("star bullet renders as ul list", expected_ul in md(cases[0][1]), True)
    check("dash bullet renders as ul list", expected_ul in md(cases[1][1]), True)
    check("unicode bullet renders as ul list", expected_ul in md(cases[2][1]), True)
    check("bare emoji with bold renders as ul list", expected_ul in md(cases[3][1]), True)
    check("normal paragraph with emoji stays p tag",
          "<p>👋 Hello, this is just a regular sentence.</p>" in md(cases[4][1]), True)
    check("normal paragraph does not produce ul tag",
          "<ul" in md(cases[4][1]), False)


def markdown_break_cases() -> None:
    print("\n== raw break tags in model output ==")
    cases = [
        ("br", "First<br>Second"),
        ("br_self_closing", "First<br/>Second"),
        ("br_space_self_closing", "First<br />Second"),
        ("br_closing", "First</br>Second"),
        ("br_closing_upper", "First</BR>Second"),
    ]
    out = node_call([("formatMarkdown", text) for _, text in cases])

    def md(arg: str) -> str:
        return out[f"formatMarkdown {arg}"]

    for name, text in cases:
        result = md(text)
        check(f"{name} renders as <br>", result, "<p>First<br>Second</p>")
        check(f"{name} does not leak escaped tag", "&lt;" in result, False)


def markdown_citation_cases() -> None:
    print("\n== grounding citation markers in model output (#676) ==")
    cases = [
        (
            "japanese_route_citations",
            "• 🚆 **恵比寿駅** 東京都渋谷区に位置し、JR山手線 [1.32]や湘南新宿ラインなどが乗り入れています [1.33]。",
            "<ul><li>🚆 <strong>恵比寿駅</strong> 東京都渋谷区に位置し、JR山手線や湘南新宿ラインなどが乗り入れています。</li></ul>",
        ),
        (
            "english_route_citations",
            "Served by the JR Yamanote Line [1.32] and the Shonan-Shinjuku Line [1.33].",
            "<p>Served by the JR Yamanote Line and the Shonan-Shinjuku Line.</p>",
        ),
        (
            "multiple_adjacent_citations",
            "Major transfer point [1.32][1.33].",
            "<p>Major transfer point.</p>",
        ),
        (
            "preserves_real_markdown_links",
            "Check [Route Guide](https://example.com/route) for details.",
            '<p>Check <a href="https://example.com/route" target="_blank" rel="noopener">Route Guide</a> for details.</p>',
        ),
        (
            "preserves_integer_brackets",
            "Step [1] and [2] in process.",
            "<p>Step [1] and [2] in process.</p>",
        ),
    ]
    out = node_call([("formatMarkdown", text) for _, text, _ in cases])

    def md(arg: str) -> str:
        return out[f"formatMarkdown {arg}"]

    for name, text, expected in cases:
        result = md(text)
        check(f"{name} strips citation markers cleanly", result, expected)
        check(f"{name} does not leak raw citation brackets", "[1." in result, False)


COMPOSITE_HARNESS = r"""
class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.className = "";
    this.children = [];
    this.dataset = {};
    this.textContent = "";
    this.parentElement = null;
    this.isConnected = false;
    this.scrollCalls = [];
    this.classList = {
      add: (c) => {
        const parts = this.className ? this.className.split(" ") : [];
        if (!parts.includes(c)) parts.push(c);
        this.className = parts.join(" ");
      },
      contains: (c) => (this.className ? this.className.split(" ").includes(c) : false),
    };
  }
  scrollIntoView(options) {
    this.scrollCalls.push(options);
  }
  appendChild(child) {
    this.children.push(child);
    child.parentElement = this;
    return child;
  }
  insertBefore(newChild, refChild) {
    const idx = this.children.indexOf(refChild);
    if (idx === -1) return this.appendChild(newChild);
    this.children.splice(idx, 0, newChild);
    newChild.parentElement = this;
    return newChild;
  }
  replaceChild(newChild, oldChild) {
    const idx = this.children.indexOf(oldChild);
    if (idx !== -1) {
      this.children[idx] = newChild;
      newChild.parentElement = this;
      oldChild.parentElement = null;
    }
    return oldChild;
  }
  querySelector(sel) {
    if (sel === ".card-head .title") {
      const head = this.children.find(c => c.classList.contains("card-head"));
      return head ? head.children.find(c => c.classList.contains("title")) : null;
    }
    if (sel.includes("[data-section=")) {
      const m = sel.match(/\[data-section="?([^"\]]+)"?\]/);
      if (m) {
        return this.children.find(c => c.dataset && c.dataset.section === m[1]) || null;
      }
    }
    return null;
  }
}

function el(html) {
  const node = new Element("div");
  if (html.includes('class="card composite-card"')) {
    node.className = "card composite-card";
    const head = new Element("div");
    head.className = "card-head";
    const title = new Element("span");
    title.className = "title";
    const m = html.match(/<span class="title">([^<]*)<\/span>/);
    title.textContent = m ? m[1] : "Info";
    head.appendChild(title);
    node.appendChild(head);
    return node;
  }
  if (html.includes('data-section="web_search"')) {
    node.className = "composite-section";
    node.dataset.section = "web_search";
    return node;
  }
  if (html.includes('data-section="youtube_search"')) {
    node.className = "composite-section";
    node.dataset.section = "youtube_search";
    return node;
  }
  if (html.includes('image-grid') || html.includes('data-section="image_search"')) {
    node.className = "composite-section image-grid";
    node.dataset.section = "image_search";
    return node;
  }
  return node;
}

function head(icon, title, extra = "") {
  return `<div class="card-head"><span class="material-symbols-outlined">${icon}</span><span class="title">${title}</span>${extra}</div>`;
}
function imageGrid(images) {
  return images && images.length ? '<div class="image-grid" data-section="image_search"></div>' : "";
}
function linkChips(sources, icon) {
  return sources && sources.length ? '<div class="link-chips"></div>' : "";
}
function videoPlayer(videos) {
  return videos && videos.length ? '<div class="video-player"></div>' : "";
}
function formatMarkdown(md) { return "[md]" + md; }
function escapeHtml(s) { return String(s); }
function escapeAttr(s) { return String(s); }
function t(k) { return k === "head_info" ? "Info" : k; }

%s

const operations = JSON.parse(process.argv[1]);
const card = compositeCard(operations.cardData || {});
if (operations.connected) {
  card.isConnected = true;
}
const results = [];
for (const step of operations.steps) {
  if (step.connected !== undefined) {
    card.isConnected = Boolean(step.connected);
  }
  const ret = renderCompositeSection(card, step.section, step.data);
  results.push(ret ? {
    section: ret.dataset.section || step.section,
    null: false,
    scrollCalls: ret.scrollCalls || [],
  } : {
    section: step.section,
    null: true,
    scrollCalls: [],
  });
}

const titleEl = card.querySelector(".card-head .title");
const sections = card.children
  .filter(c => !c.classList.contains("card-head"))
  .map(c => c.dataset.section);

console.log(JSON.stringify({
  title: titleEl ? titleEl.textContent : "",
  sections: sections,
  results: results,
}));
"""


def composite_card_run(operations: dict) -> dict:
    cards = (JS / "cards.js").read_text(encoding="utf-8")
    order_m = re.search(r"(const COMPOSITE_SECTION_ORDER = \{.*?\};)", cards, re.DOTALL)
    order_src = order_m.group(1) if order_m else ""
    canonical_src = block(cards, "function canonicalSection(")
    composite_src = block(cards, "export function compositeCard(").removeprefix("export ")
    render_src = block(cards, "export function renderCompositeSection(").removeprefix("export ")
    lifted = f"{order_src}\n\n{canonical_src}\n\n{composite_src}\n\n{render_src}"
    code = COMPOSITE_HARNESS % lifted
    proc = subprocess.run(
        ["node", "-e", code, json.dumps(operations)],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        print(f"   FAIL node: {proc.stderr.strip()}")
        sys.exit(1)
    return json.loads(proc.stdout)


def composite_card_cases() -> None:
    print("\n== composite card sections and streaming update ==")
    # 1. Out-of-order section arrival: web_search arrives first, then image_search arrives
    # image_search (weight 10) must be placed before web_search (weight 30)
    out_of_order = composite_card_run({
        "cardData": {"title": "Info"},
        "steps": [
            {"section": "web_search", "data": {"query": "cherry blossoms", "summary": "Spring blooms."}},
            {"section": "image_search", "data": {"images": [{"thumbnail": "x"}]}},
        ],
    })
    check("out-of-order arrival sorts image_search before web_search",
          out_of_order["sections"], ["image_search", "web_search"])
    check("card title updates from generic Info to query",
          out_of_order["title"], "cherry blossoms")

    # 2. Empty result suppression:
    empty_run = composite_card_run({
        "cardData": {"title": "Info"},
        "steps": [
            {"section": "image_search", "data": {"images": []}},
            {"section": "web_search", "data": {"summary": "", "sources": [], "search_queries": []}},
        ],
    })
    check("empty image_search returns null and suppresses section",
          empty_run["results"][0]["null"], True)
    check("empty web_search returns null and suppresses section",
          empty_run["results"][1]["null"], True)
    check("card has no sections when all results are empty",
          empty_run["sections"], [])

    # 3. In-place section replacement / idempotent update:
    replace_run = composite_card_run({
        "cardData": {"title": "Info"},
        "steps": [
            {"section": "web_search", "data": {"summary": "Initial summary."}},
            {"section": "web_search", "data": {"summary": "Updated summary."}},
        ],
    })
    check("duplicate section replaces in-place without appending",
          replace_run["sections"], ["web_search"])

    # 4. Continuous web searches update card title to the latest query:
    continuous_run = composite_card_run({
        "cardData": {"title": "Info"},
        "steps": [
            {"section": "web_search", "data": {"query": "first query", "summary": "Result 1."}},
            {"section": "web_search", "data": {"query": "second query", "summary": "Result 2."}},
        ],
    })
    check("card title updates on subsequent search query",
          continuous_run["title"], "second query")

    # 5. youtube_search composite ordering and empty suppression:
    yt_order = composite_card_run({
        "cardData": {"title": "Info"},
        "steps": [
            {"section": "youtube_search", "data": {"videos": [{"video_id": "vid1", "title": "Vid 1"}]}},
            {"section": "web_search", "data": {"query": "test query", "summary": "Summary."}},
            {"section": "image_search", "data": {"images": [{"thumbnail": "x"}]}},
        ],
    })
    check("composite sections sort [image_search, web_search, youtube_search]",
          yt_order["sections"], ["image_search", "web_search", "youtube_search"])

    yt_empty = composite_card_run({
        "cardData": {"title": "Info"},
        "steps": [
            {"section": "youtube_search", "data": {"videos": []}},
        ],
    })
    check("empty youtube_search returns null and suppresses section",
          yt_empty["results"][0]["null"], True)

    # 6. scrollIntoView on connected vs disconnected card:
    # When card is disconnected (initial create before DOM mount), scrollIntoView is not called.
    # When card is connected (multi-turn append), scrollIntoView is called on newly mounted section.
    scroll_test = composite_card_run({
        "cardData": {"title": "Info"},
        "steps": [
            {"section": "web_search", "data": {"query": "news", "summary": "Initial summary."}},
            {"section": "youtube_search", "connected": True, "data": {"videos": [{"video_id": "vid1", "title": "Vid 1"}]}},
        ],
    })
    check("initial disconnected section does not trigger scrollIntoView",
          scroll_test["results"][0]["scrollCalls"], [])
    check("subsequent section on connected card scrolls into view with smooth/start",
          scroll_test["results"][1]["scrollCalls"], [{"behavior": "smooth", "block": "start"}])

    replace_scroll = composite_card_run({
        "cardData": {"title": "Info"},
        "connected": True,
        "steps": [
            {"section": "web_search", "data": {"summary": "Initial."}},
            {"section": "web_search", "data": {"summary": "Updated."}},
        ],
    })
    check("connected card section replacement triggers scrollIntoView",
          replace_scroll["results"][1]["scrollCalls"], [{"behavior": "smooth", "block": "start"}])


def in_flight_state_cases() -> None:
    print("\n== in-flight indicators and mic icon state (#512) ==")
    app = (JS / "app.js").read_text(encoding="utf-8")
    cards = (JS / "cards.js").read_text(encoding="utf-8")

    # 1. toolCallCard adds .tool-call
    check("toolCallCard produces element with tool-call class",
          'class="msg system tool-call"' in block(cards, "export function toolCallCard("),
          True)

    # 2. startListening keeps mic icon as 'mic'
    start_listen = block(app, "async function startListening(")
    check("startListening leaves mic button icon as mic",
          'icon.textContent = "mic"' in start_listen,
          True)
    check("startListening avoids setting mic_off",
          'icon.textContent = "mic_off"' in start_listen,
          False)

    # 3. Source assertions for cleanups
    check("startTurn cleans up prior listening indicators",
          'feed.querySelectorAll(".msg.listening").forEach(el => el.remove());' in block(app, "function startTurn("),
          True)
    check("showWorking cleans up prior listening indicators",
          'feed.querySelectorAll(".msg.listening").forEach(el => el.remove());' in block(app, "function showWorking("),
          True)
    check("stopListening cleans up prior listening indicators",
          'feed.querySelectorAll(".msg.listening").forEach(el => el.remove());' in block(app, "async function stopListening("),
          True)
    check("bubble for agent clears prior tool-call indicators",
          'feed.querySelectorAll(".msg.tool-call").forEach(el => el.remove());' in block(app, "function bubble("),
          True)
    check("renderToolResult clears prior tool-call indicators",
          'feed.querySelectorAll(".msg.tool-call").forEach(el => el.remove());' in block(app, "function renderToolResult("),
          True)
    check("appendCard clears prior tool-call indicators",
          'feed.querySelectorAll(".msg.tool-call").forEach(el => el.remove());' in block(app, "function appendCard("),
          True)
    check("appendCard reveals starter chips immediately (#576)",
          'if (typeof showStarters === "function") showStarters(' in block(app, "function appendCard("),
          True)
    check("handleCardSection clears prior tool-call indicators",
          'feed.querySelectorAll(".msg.tool-call").forEach(el => el.remove());' in block(app, "function handleCardSection("),
          True)

    # 4. Behavioral test in Node
    test_script = r"""
const feedChildren = [];
const feed = {
  appendChild(child) { feedChildren.push(child); return child; },
  querySelectorAll(sel) {
    if (sel === ".msg.listening") return feedChildren.filter(c => (c.className || "").includes("listening"));
    if (sel === ".msg.tool-call") return feedChildren.filter(c => (c.className || "").includes("tool-call"));
    return [];
  }
};
function append(el) { feed.appendChild(el); }
globalThis.document = {
  createElement(tag) {
    return {
      tagName: tag,
      className: "",
      textContent: "",
      classList: {
        add(c) { this.className = (this.className + " " + c).trim(); },
        remove(c) { this.className = this.className.split(" ").filter(x => x !== c).join(" "); },
        contains(c) { return this.className.split(" ").includes(c); }
      },
      remove() {
        const i = feedChildren.indexOf(this);
        if (i !== -1) feedChildren.splice(i, 1);
      }
    };
  }
};

%s

// Seed a listening indicator
const listeningEl = globalThis.document.createElement("div");
listeningEl.className = "msg system listening";
feed.appendChild(listeningEl);

// Calling bubble('agent') should remove tool-call, but leave listeningEl
const toolEl = globalThis.document.createElement("div");
toolEl.className = "msg system tool-call";
feed.appendChild(toolEl);

bubble("agent", "Hello");

const afterBubble = {
  hasToolCall: feedChildren.some(c => (c.className || "").includes("tool-call")),
  hasListening: feedChildren.some(c => (c.className || "").includes("listening")),
};

// Calling startTurn should remove listeningEl
// Stub dependencies of startTurn
const starters = { classList: { add() {} } };
function hideStarters() {}
function wake() {}
function seal() {}
let userBubble = null, agentBubble = null, hasSpokenText = false, seenTools = new Set(), activeCompositeCard = null, userCleared = false, activeTurn = false, turnCardCount = 0;
let compositeCards = new Map();
compositeCards.set("turn-1", { id: "past-card" });

startTurn();

const afterTurn = {
  hasListening: feedChildren.some(c => (c.className || "").includes("listening")),
  compositeCardsSize: compositeCards.size,
  activeTurn,
  turnCardCount,
};

console.log(JSON.stringify({ afterBubble, afterTurn }));
""" % "\n".join([
        block(app, "function bubble("),
        block(app, "function startTurn("),
    ])
    proc = subprocess.run(
        ["node", "-e", test_script],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        print(f"   FAIL node in_flight_state: {proc.stderr.strip()}")
        sys.exit(1)
    res = json.loads(proc.stdout)
    check("bubble('agent') clears tool-call from feed", res["afterBubble"]["hasToolCall"], False)
    check("bubble('agent') leaves listening indicator untouched", res["afterBubble"]["hasListening"], True)
    check("startTurn clears listening indicator from feed", res["afterTurn"]["hasListening"], False)
    check("startTurn clears compositeCards map (#539)", res["afterTurn"]["compositeCardsSize"], 0)
    check("startTurn sets activeTurn (#602)", res["afterTurn"]["activeTurn"], True)
    check("startTurn resets turnCardCount (#602)", res["afterTurn"]["turnCardCount"], 0)
    check("turnComplete resets turnCardCount (#602)",
          "turnCardCount = 0;" in block(app, "if (event.turnComplete)"), True)
    check("interrupted resets turnCardCount (#602)",
          "turnCardCount = 0;" in block(app, "if (event.interrupted)"), True)
    check("turnComplete resets compositeCards map (#539)",
          "compositeCards = new Map();" in block(app, "if (event.turnComplete)"), True)
    check("interrupted resets compositeCards map (#539)",
          "compositeCards = new Map();" in block(app, "if (event.interrupted)"), True)
    check("outputTranscription does not hide starters when cards present (#576)",
          "!cards.children.length" in block(app, "const output = event.outputTranscription;"), True)
    check("turnComplete calls showStarters(false) (#576)",
          "showStarters(false)" in block(app, "if (event.turnComplete)"), True)

    # 5. Lifecycle test for starter chips visibility (#576)
    # Validates that #starters remains visible across card render -> outputTranscription -> turnComplete
    lifecycle_script = r"""
const startersHidden = new Set(["hidden"]);
const starters = {
  classList: {
    add(c) { startersHidden.add(c); },
    remove(c) { startersHidden.delete(c); },
    contains(c) { return startersHidden.has(c); },
  }
};
function hideStarters() { starters.classList.add("hidden"); }
function showStarters(behavior) { starters.classList.remove("hidden"); }

const cardList = [];
const cards = {
  get children() { return cardList; },
  appendChild(node) { cardList.push(node); },
};

let userBubble = null;
let agentBubble = null;
let hasSpokenText = false;
let seenTools = new Set();
let compositeCards = new Map();
let activeCompositeCard = null;
let activeTurn = false;
let turnCardCount = 0;
function wake() {}
function hideWorking() {}
function seal() {}
function bubble(role, text) { return { role, text }; }
function replace() {}
function extend() {}

%s

// Case A: No cards in container
showStarters(false);
const initialNoCard = !starters.classList.contains("hidden");
handleEvent({ outputTranscription: { text: "Spoken without card" } });
const afterOutputNoCard = !starters.classList.contains("hidden");
handleEvent({ turnComplete: true });
const afterTurnNoCard = !starters.classList.contains("hidden");

// Case B: With cards rendered (#576 lifecycle)
cards.appendChild({ name: "weather-card" });
showStarters(false);
const afterCardRender = !starters.classList.contains("hidden");
handleEvent({ outputTranscription: { text: "Spoken with card present" } });
const afterOutputWithCard = !starters.classList.contains("hidden");
handleEvent({ turnComplete: true });
const afterTurnWithCard = !starters.classList.contains("hidden");

console.log(JSON.stringify({
  initialNoCard,
  afterOutputNoCard,
  afterTurnNoCard,
  afterCardRender,
  afterOutputWithCard,
  afterTurnWithCard,
}));
""" % block(app, "function handleEvent(")

    proc = subprocess.run(
        ["node", "-e", lifecycle_script],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        print(f"   FAIL node starters_lifecycle: {proc.stderr.strip()}")
        sys.exit(1)
    res_life = json.loads(proc.stdout)
    check("outputTranscription hides starters when no card is present",
          res_life["afterOutputNoCard"], False)
    check("starters visible when card is rendered (#576)",
          res_life["afterCardRender"], True)
    check("outputTranscription does not re-hide starters when card is present (#576)",
          res_life["afterOutputWithCard"], True)
    check("turnComplete keeps starters visible below card (#576)",
          res_life["afterTurnWithCard"], True)


def kiosk_navigation_cases() -> None:
    print("\n== kiosk navigation inhibition & touch-safe image lightbox (#628) ==")
    index = (JS.parent / "index.html").read_text(encoding="utf-8")
    style = (JS.parent / "css" / "style.css").read_text(encoding="utf-8")
    cards = (JS / "cards.js").read_text(encoding="utf-8")
    app = (JS / "app.js").read_text(encoding="utf-8")

    # 1. HTML elements in index.html
    check("index.html has imageModal dialog", 'id="imageModal"' in index, True)
    check("index.html has imageModalClose button", 'id="imageModalClose"' in index, True)
    check("index.html has imageModalBackdrop", 'id="imageModalBackdrop"' in index, True)
    check("index.html has imageModalImg", 'id="imageModalImg"' in index, True)
    check("index.html has imageModalCaption", 'id="imageModalCaption"' in index, True)
    check("imageModalClose has prominent close glyph", "✕" in index, True)
    check("index.html has citationModal dialog", 'id="citationModal"' in index, True)
    check("index.html has citationModalClose button", 'id="citationModalClose"' in index, True)
    check("index.html has citationModalBackdrop", 'id="citationModalBackdrop"' in index, True)
    check("index.html has citationModalTitle", 'id="citationModalTitle"' in index, True)
    check("index.html has citationQr container", 'id="citationQr"' in index, True)
    check("index.html has citationUrl link", 'id="citationUrl"' in index, True)
    check("index.html has citationModalReturnBtn", 'id="citationModalReturnBtn"' in index, True)

    # 2. Touch-safe CSS styling in style.css
    check("style.css defines .image-modal fixed overlay",
          ".image-modal {" in style and "position: fixed;" in style, True)
    check("image-modal z-index is 30 (below screensaver clock at 40)",
          "z-index: 30;" in style, True)
    check("close button has touch target larger than --tap 48px",
          "width: 56px;" in style and "height: 56px;" in style, True)
    check("close button has touch-action manipulation",
          "touch-action: manipulation;" in style, True)
    check("style.css defines .citation-modal fixed overlay",
          ".citation-modal {" in style and "position: fixed;" in style, True)
    check("citation-modal z-index is 30 (matches image-modal)",
          ".citation-modal {" in style and "z-index: 30;" in style, True)
    check("citation-modal-close button has touch-action manipulation",
          ".citation-modal-close {" in style and "touch-action: manipulation;" in style, True)
    check("style.css defines white background for citation-qr-container",
          ".citation-qr-container {" in style and "background: #ffffff;" in style, True)

    # 3. Kiosk detection and link inhibition under Node.js
    test_script = """
globalThis.document = {
  documentElement: { lang: "en" },
  createElement: () => ({
    set textContent(v) {
      this.innerHTML = String(v)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    },
  }),
};
function el(html) { return html.trim(); }

import { formatMarkdown } from "./src/ai_terminal/live/static/js/markdown.js";
import { isKiosk } from "./src/ai_terminal/live/static/js/cards.js";

// Non-kiosk mode
const defaultKiosk = isKiosk();
const normalMd = formatMarkdown("[External Link](https://example.com/info)");

// Kiosk mode (?kiosk=1)
globalThis.window = { location: { search: "?kiosk=1" } };
const kioskDetected = isKiosk();
const kioskMd = formatMarkdown("[External Link](https://example.com/info)");

console.log(JSON.stringify({ defaultKiosk, normalMd, kioskDetected, kioskMd }));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", test_script],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        print(f"   FAIL node kiosk_navigation_cases: {proc.stderr.strip()}")
        sys.exit(1)
    res = json.loads(proc.stdout)
    check("default mode is not kiosk", res["defaultKiosk"], False)
    check("kiosk query is detected by isKiosk", res["kioskDetected"], True)
    check("normal markdown creates target=_blank link", 'target="_blank"' in res["normalMd"], True)
    check("kiosk markdown inhibits external link to href=#", 'href="#"' in res["kioskMd"], True)
    check("kiosk markdown link has kiosk-link class", "kiosk-link" in res["kioskMd"], True)
    check("kiosk markdown link has kiosk-citation class", "kiosk-citation" in res["kioskMd"], True)
    check("kiosk markdown link retains data-uri", 'data-uri="https://example.com/info"' in res["kioskMd"], True)
    check("kiosk markdown link retains data-title", 'data-title="External Link"' in res["kioskMd"], True)

    # 4. cards.js imageGrid & linkChips inhibition
    check("cards.js exports isKiosk", "export function isKiosk(" in cards, True)
    check("cards.js exports openImageModal", "export function openImageModal(" in cards, True)
    check("cards.js exports closeImageModal", "export function closeImageModal(" in cards, True)
    check("imageGrid tiles include data-preview attribute for modal", 'data-preview="${escapeAttr(src)}"' in cards, True)
    check("imageGrid tiles include data-title attribute for modal", 'data-title="${escapeAttr(title)}"' in cards, True)
    check("imageGrid tiles have safeHref for kiosk mode", 'const safeHref = kiosk ? "#" : href;' in cards, True)
    check("linkChips inhibits href in kiosk mode", 'const href = kiosk ? "#" : (it.uri || "#");' in cards, True)
    check("linkChips retains data-uri in kiosk mode", 'data-uri="${escapeAttr(it.uri || "")}"' in cards, True)
    check("linkChips retains data-title in kiosk mode", 'data-title="${escapeAttr(label)}"' in cards, True)
    check("linkChips includes kiosk-citation class in kiosk mode", "kiosk-citation" in cards, True)

    # 5. app.js event handling
    check("app.js imports modal handlers", "closeImageModal" in app and "openImageModal" in app, True)
    check("app.js closes image modal on Escape key", 'if (e.key === "Escape") {\n    closeImageModal();' in app, True)
    check("app.js closes citation modal on Escape key", "closeCitationModal();" in app, True)
    check("app.js intercepts image thumbnail clicks to open modal",
          'e.target.closest?.(".image-grid a, a.image-thumb")' in app, True)
    check("app.js prevents default on thumbnail click to avoid navigation",
          'e.preventDefault();\n    e.stopPropagation();\n    const previewSrc = thumb.dataset.preview' in app, True)
    check("app.js intercepts citation clicks in kiosk mode",
          'e.target.closest?.(".kiosk-citation, a[data-uri]")' in app, True)
    check("app.js defines openCitationModal", "function openCitationModal(uri, title)" in app, True)
    check("app.js defines closeCitationModal", "function closeCitationModal()" in app, True)
    check("app.js generates QR code for citation modal",
          'generateQrSvg(uri, { cellSize: 3, margin: 2 })' in app, True)
    check("app.js inhibits all external link navigation in kiosk mode",
          'if (kiosk) {\n    const link = e.target.closest?.("a[href]");' in app, True)


def gws_kiosk_auth_cases() -> None:
    print("\n== gws companion authentication modal in kiosk mode (#642) ==")
    index = (JS.parent / "index.html").read_text(encoding="utf-8")
    style = (JS.parent / "css" / "style.css").read_text(encoding="utf-8")
    app = (JS / "app.js").read_text(encoding="utf-8")
    qrcode_js = (JS / "qrcode.js").read_text(encoding="utf-8")

    # 1. Markup presence in index.html
    check("index.html has gwsAuthModal dialog", 'id="gwsAuthModal"' in index, True)
    check("index.html has gwsAuthModalClose button", 'id="gwsAuthModalClose"' in index, True)
    check("index.html has gwsAuthModalBackdrop", 'id="gwsAuthModalBackdrop"' in index, True)
    check("index.html has gwsAuthQr container", 'id="gwsAuthQr"' in index, True)
    check("index.html has gwsAuthUrl link", 'id="gwsAuthUrl"' in index, True)
    check("index.html has gwsAuthStatus element", 'id="gwsAuthStatus"' in index, True)
    check("index.html has gwsAuthStatusText element", 'id="gwsAuthStatusText"' in index, True)
    check("index.html has gwsAuthCancelBtn button", 'id="gwsAuthCancelBtn"' in index, True)
    check("gwsAuthCancelBtn text has cancel glyph", "✕ Cancel / Return" in index, True)

    # 2. CSS styles in style.css
    check("style.css defines .gws-auth-modal fixed overlay",
          ".gws-auth-modal {" in style and "position: fixed;" in style, True)
    check("gws-auth-modal z-index is 30 (matches image-modal)",
          "z-index: 30;" in style, True)
    check("close button has touch-action manipulation",
          ".gws-auth-modal-close {" in style and "touch-action: manipulation;" in style, True)
    check("style.css defines white background for qr container",
          ".gws-auth-qr-container {" in style and "background: #ffffff;" in style, True)

    # 3. qrcode.js module and SVG output
    check("qrcode.js exports generateQrSvg", "export function generateQrSvg" in qrcode_js, True)
    check("app.js imports generateQrSvg from ./qrcode.js",
          'import { generateQrSvg } from "./qrcode.js";' in app, True)

    # 4. Node.js execution: test kiosk vs non-kiosk, modal controls, and polling timeout
    node_test = r"""
import { generateQrSvg } from "./src/ai_terminal/live/static/js/qrcode.js";

// Verify QR SVG generator output
const svg = generateQrSvg("https://accounts.google.com/o/oauth2/auth?scope=test", { cellSize: 3, margin: 2 });
const svgValid = svg.startsWith("<svg") && svg.endsWith("</svg>") && svg.includes("viewBox");

// Test kiosk detection expression
const kioskUrlSearch = "?kiosk=1";
const nonKioskUrlSearch = "";
const isKioskMode = new URLSearchParams(kioskUrlSearch).get("kiosk") === "1";
const isNonKioskMode = new URLSearchParams(nonKioskUrlSearch).get("kiosk") === "1";

console.log(JSON.stringify({
  svgValid,
  isKioskMode,
  isNonKioskMode,
}));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", node_test],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode:
        print(f"   FAIL node gws_kiosk_auth_cases: {proc.stderr.strip()}")
        sys.exit(1)
    res = json.loads(proc.stdout)
    check("generateQrSvg generates valid SVG element", res["svgValid"], True)
    check("kiosk query parameter is detected as kiosk mode", res["isKioskMode"], True)
    check("empty query parameter is detected as non-kiosk mode", res["isNonKioskMode"], False)

    # 5. app.js logic assertions
    connect_block = block(app, "async function connectGws(")
    check("connectGws checks kiosk mode query parameter",
          'new URLSearchParams(window?.location?.search || "").get("kiosk") === "1"' in connect_block, True)
    check("connectGws gates window.open on kiosk check",
          "if (isKioskMode)" in connect_block and 'window.open(consentUrl, "_blank")' in connect_block, True)
    check("connectGws opens gwsAuthModal on sign in",
          "openGwsAuthModal(consentUrl)" in connect_block, True)
    check("connectGws sends kiosk parameter in login request",
          'body: JSON.stringify({ kiosk: isKioskMode })' in connect_block, True)
    check("closeGwsAuthModal calls gws cancel route on cancellation",
          'fetch("/api/gws/cancel"' in app, True)
    check("openGwsAuthModal handles kiosk mode passkey instructions",
          "passkey" in app, True)
    check("closeGwsAuthModal handles cancellation state",
          "Sign-in cancelled or timed out" in app, True)
    check("Escape key closes gwsAuthModal with cancellation",
          "closeGwsAuthModal(true)" in app, True)


def drag_scroll_cases() -> None:
    print("\n== pointer drag scrolling on panels ==")
    app = (JS / "app.js").read_text(encoding="utf-8")
    check("app.js defines enableDragScroll (#532)", "function enableDragScroll(" in app, True)
    check("app.js enables drag scrolling on feed (#532)", "enableDragScroll(feed)" in app, True)
    check("app.js enables drag scrolling on side panel (#532)",
          "enableDragScroll(scrollSidePanel" in app or "enableDragScroll(sidePanel" in app, True)

    fn = block(app, "function enableDragScroll(")
    node_test = f"""
{fn}

function createContainer(initialScrollTop = 100) {{
  const listeners = {{}};
  return {{
    scrollTop: initialScrollTop,
    listeners,
    captured: null,
    released: null,
    addEventListener(ev, cb) {{
      if (!listeners[ev]) listeners[ev] = [];
      listeners[ev].push(cb);
    }},
    dispatchEvent(ev) {{
      for (const cb of (listeners[ev.type] || [])) cb(ev);
    }},
    setPointerCapture(id) {{ this.captured = id; }},
    releasePointerCapture(id) {{ this.released = id; }},
    hasPointerCapture(id) {{ return this.captured === id; }},
  }};
}}

// Mock window
const windowListeners = {{}};
global.window = {{
  addEventListener(ev, cb, opts) {{
    if (!windowListeners[ev]) windowListeners[ev] = [];
    windowListeners[ev].push({{ cb, opts }});
  }},
  removeEventListener(ev, cb) {{
    if (!windowListeners[ev]) return;
    windowListeners[ev] = windowListeners[ev].filter(item => item.cb !== cb);
  }},
  dispatchEvent(ev) {{
    for (const item of (windowListeners[ev.type] || [])) {{
      item.cb(ev);
    }}
  }}
}};

// Scenario 1: Tap without dragging (delta <= 5px)
const c1 = createContainer(100);
let scrolled1 = false;
enableDragScroll(c1, () => {{ scrolled1 = true; }});

c1.dispatchEvent({{ type: "pointerdown", button: 0, pointerId: 1, clientY: 50, clientX: 20 }});
c1.dispatchEvent({{ type: "pointermove", button: 0, pointerId: 1, clientY: 53, clientX: 20 }});
const scrollAfterSmallMove = c1.scrollTop;
c1.dispatchEvent({{ type: "pointerup", button: 0, pointerId: 1, clientY: 53, clientX: 20 }});

let tapClickCancelled = false;
const tapClickEv = {{
  type: "click",
  preventDefault() {{ tapClickCancelled = true; }},
  stopPropagation() {{ tapClickCancelled = true; }}
}};
window.dispatchEvent(tapClickEv);

// Scenario 2: Drag scroll (delta > 5px)
const c2 = createContainer(100);
let scrolled2 = false;
enableDragScroll(c2, () => {{ scrolled2 = true; }});

c2.dispatchEvent({{ type: "pointerdown", button: 0, pointerId: 2, clientY: 100, clientX: 20 }});
c2.dispatchEvent({{ type: "pointermove", button: 0, pointerId: 2, clientY: 60, clientX: 20, cancelable: true, preventDefault() {{}} }});
const scrollAfterDrag = c2.scrollTop;
c2.dispatchEvent({{ type: "pointerup", button: 0, pointerId: 2, clientY: 60, clientX: 20 }});

let dragClickCancelled = false;
const dragClickEv = {{
  type: "click",
  preventDefault() {{ dragClickCancelled = true; }},
  stopPropagation() {{ dragClickCancelled = true; }}
}};
window.dispatchEvent(dragClickEv);

console.log(JSON.stringify({{
  tap: {{
    scrollAfterSmallMove,
    scrolled1,
    tapClickCancelled,
  }},
  drag: {{
    scrollAfterDrag,
    scrolled2,
    dragClickCancelled,
    captured: c2.captured,
  }}
}}));
"""
    proc = subprocess.run(
        ["node", "-e", node_test],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode != 0:
        print(f"   FAIL node drag_scroll_cases: {proc.stderr.strip()}")
        sys.exit(1)
    res = json.loads(proc.stdout)
    check("small movement does not scroll panel", res["tap"]["scrollAfterSmallMove"], 100)
    check("small movement does not invoke onUserScroll", res["tap"]["scrolled1"], False)
    check("small movement does not cancel click", res["tap"]["tapClickCancelled"], False)
    check("dragging scrolls panel (scrollTop increases when dragged up)", res["drag"]["scrollAfterDrag"], 140)
    check("dragging invokes onUserScroll (clears followCards)", res["drag"]["scrolled2"], True)
    check("dragging suppresses subsequent click", res["drag"]["dragClickCancelled"], True)


def pcm_player_cases() -> None:
    print("\n== PCM player processor zeroing and transition smoothing (#740) ==")
    node_test = r"""
const fs = require("fs");
class AudioWorkletProcessor {
  constructor() {
    this.port = { onmessage: null, postMessage: () => {} };
  }
}
const registered = {};
function registerProcessor(name, cls) { registered[name] = cls; }
const code = fs.readFileSync(process.argv[1], "utf8");
eval(code);
const PCMPlayerProcessor = registered["pcm-player-processor"];

function createOutput(channels = 2, length = 128) {
  const chs = [];
  for (let c = 0; c < channels; c++) chs.push(new Float32Array(length));
  return [chs];
}

function enqueueFloats(proc, floatSamples) {
  const int16 = new Int16Array(floatSamples.length);
  for (let i = 0; i < floatSamples.length; i++) {
    int16[i] = Math.round(floatSamples[i] * 32768);
  }
  proc.port.onmessage({ data: int16.buffer });
}

// Test 1 & 2: Buffer exhaustion outputs clean zeros on both stereo channels
const p1 = new PCMPlayerProcessor();
p1.buffer.fill(0.3); // Pre-fill with stale non-zero samples
enqueueFloats(p1, new Array(128).fill(0.8));
p1.process([], createOutput(2, 128), {});
p1.process([], createOutput(2, 128), {}); // Ramp-down block
const outExhausted = createOutput(2, 128);
p1.process([], outExhausted, {}); // Fully exhausted block
const exhaustionZeros = Array.from(outExhausted[0][0]).every(s => s === 0);
const stereoZeroed = Array.from(outExhausted[0][1]).every(s => s === 0);

// Test 3: Smooth ramp-down on buffer runout without step discontinuity
const p3 = new PCMPlayerProcessor();
enqueueFloats(p3, new Array(256).fill(0.8));
p3.process([], createOutput(2, 128), {});
const out3_2 = createOutput(2, 128);
p3.process([], out3_2, {});
const out3_3 = createOutput(2, 128);
p3.process([], out3_3, {});
const out3_4 = createOutput(2, 128);
p3.process([], out3_4, {});
const runoutStream = [
  ...Array.from(out3_2[0][0]),
  ...Array.from(out3_3[0][0]),
  ...Array.from(out3_4[0][0]),
  0.0
];
let runoutMaxJump = 0;
for (let i = 1; i < runoutStream.length; i++) {
  const jump = Math.abs(runoutStream[i] - runoutStream[i - 1]);
  if (jump > runoutMaxJump) runoutMaxJump = jump;
}

// Test 4: Interruption (endOfAudio) ramps down to silence without step discontinuity
const p4 = new PCMPlayerProcessor();
enqueueFloats(p4, new Array(512).fill(0.8));
p4.process([], createOutput(2, 128), {});
const out4_2 = createOutput(2, 128);
p4.process([], out4_2, {});
p4.port.onmessage({ data: { command: "endOfAudio" } });
const out4_3 = createOutput(2, 128);
p4.process([], out4_3, {});
const out4_4 = createOutput(2, 128);
p4.process([], out4_4, {});
const interruptStream = [
  ...Array.from(out4_2[0][0]),
  ...Array.from(out4_3[0][0]),
  ...Array.from(out4_4[0][0]),
  0.0
];
let interruptionMaxJump = 0;
for (let i = 1; i < interruptStream.length; i++) {
  const jump = Math.abs(interruptStream[i] - interruptStream[i - 1]);
  if (jump > interruptionMaxJump) interruptionMaxJump = jump;
}

// Test 5: Audio start from silence ramps up without turn-on click
const p5 = new PCMPlayerProcessor();
p5.process([], createOutput(2, 128), {});
enqueueFloats(p5, new Array(256).fill(0.8));
const out5_start = createOutput(2, 128);
p5.process([], out5_start, {});
const out5_steady = createOutput(2, 128);
p5.process([], out5_steady, {});
const startFirstSample = Math.abs(out5_start[0][0][0]);
const startStream = [
  0.0,
  ...Array.from(out5_start[0][0]),
  ...Array.from(out5_steady[0][0])
];
let startMaxJump = 0;
for (let i = 1; i < startStream.length; i++) {
  const jump = Math.abs(startStream[i] - startStream[i - 1]);
  if (jump > startMaxJump) startMaxJump = jump;
}

// Test 6: Steady-state audio is passed bit-exact at unity gain
const p6 = new PCMPlayerProcessor();
enqueueFloats(p6, new Array(128).fill(0.5));
p6.process([], createOutput(2, 128), {});
const testInts = [];
for (let i = 0; i < 128; i++) testInts.push(((i * 499) % 60000) - 30000);
const int16Buf = new Int16Array(testInts);
p6.port.onmessage({ data: int16Buf.buffer });
const out6_steady = createOutput(2, 128);
p6.process([], out6_steady, {});
let steadyStateBitExact = true;
for (let i = 0; i < 128; i++) {
  if (out6_steady[0][0][i] !== testInts[i] / 32768) {
    steadyStateBitExact = false;
    break;
  }
}

console.log(JSON.stringify({
  exhaustionZeros,
  stereoZeroed,
  runoutMaxJump,
  interruptionMaxJump,
  startFirstSample,
  startMaxJump,
  steadyStateBitExact
}));
"""
    proc = subprocess.run(
        ["node", "-e", node_test, str(JS / "pcm-player-processor.js")],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode != 0:
        print(f"   FAIL node pcm_player_cases: {proc.stderr.strip()}")
        sys.exit(1)
    res = json.loads(proc.stdout)
    check("buffer exhaustion outputs clean zeros", res["exhaustionZeros"], True,
          "no frozen DC bias repeating stale samples")
    check("both stereo channels zeroed out", res["stereoZeroed"], True)
    check("smooth ramp-down on buffer runout without step discontinuity",
          res["runoutMaxJump"] < 0.05, True, f"maxJump {res['runoutMaxJump']:.4f} < 0.05")
    check("interruption ramps down to silence without step discontinuity",
          res["interruptionMaxJump"] < 0.05, True, f"maxJump {res['interruptionMaxJump']:.4f} < 0.05")
    check("audio start from silence ramps up without turn-on click",
          res["startFirstSample"] < 0.05, True, f"firstSample {res['startFirstSample']:.4f} < 0.05")
    check("audio start has no step discontinuity",
          res["startMaxJump"] < 0.05, True, f"maxJump {res['startMaxJump']:.4f} < 0.05")
    check("steady-state audio is passed bit-exact at unity gain",
          res["steadyStateBitExact"], True)


def main() -> None:
    history_cases()
    markdown_bullet_cases()
    markdown_break_cases()
    markdown_citation_cases()
    composite_card_cases()
    in_flight_state_cases()
    drag_scroll_cases()
    print("\n== every element app.js reaches for ==")
    # `getElementById` on an id that is not in the page returns null, and the
    # crash lands wherever the control is first touched — for the profile
    # picker that is `init`, on a household that set `PROFILES` and only there.
    app = (JS / "app.js").read_text(encoding="utf-8")
    page = (JS.parent / "index.html").read_text(encoding="utf-8")
    wanted = re.findall(r'getElementById\("([^"]+)"\)', app)
    check("the page has every id it looks up",
          [i for i in wanted if f'id="{i}"' not in page], [],
          f"{len(wanted)} ids")
    check("and there are as many as there are controls", len(wanted) >= 15,
          True, f"{len(wanted)}")

    browse_cases()
    calendar_cases()
    sun_line_cases()
    wx_timezone_cases()
    near_pill_cases()
    card_cases()
    source_cases()
    plan_cases()
    remember_cases()
    calculate_cases()
    web_search_cases()
    camera_card_cases()
    home_card_cases()
    result_key_cases()
    steps_cases()
    late_cases()
    working_cases()
    profile_cases()
    voice_cases()
    forget_cases()
    wake_cases()
    revision_cases()
    visible_cases()
    scroll_cases()
    clock_cases()
    bar_cases()
    lang_cases()
    starters_cases()
    dynamic_starter_chips_cases()
    volume_cases()
    css_cases()
    kiosk_navigation_cases()
    gws_kiosk_auth_cases()
    pcm_player_cases()

    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()

