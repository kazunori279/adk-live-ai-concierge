/**
 * AI Terminal — browser side of the ADK Live API bridge.
 *
 * Sends 16 kHz PCM up over a WebSocket, plays 24 kHz PCM back down, and
 * renders each tool result as a card.
 *
 * Where the card goes depends on how much screen there is. On the landscape
 * terminal the cards get their own pane beside the conversation, because a
 * place card runs to about 2100 px and the panel has 800 — sharing one column
 * with the transcript would push the conversation off the top. Narrower than
 * `WIDE`, they go inline in the feed and the whole thing reads top to bottom.
 */

import { startAudioPlayerWorklet } from "./audio-player.js";
import { startAudioRecorderWorklet } from "./audio-recorder.js";
import { formatMarkdown } from "./markdown.js";
import { pickLocation, reverseGeocode } from "./location.js";
import { LOCALES, applyStatic, pickLocale, setLocale, t } from "./i18n.js";
import {
  browseCard,
  browseUpdate,
  calculateCard,
  cameraCard,
  closeImageModal,
  compositeCard,
  emojiRenders,
  genericCard,
  gwsCard,
  homeSummaryCard,
  homeTimelineCard,
  gwsFill,
  gwsPending,
  gwsProgress,
  imageCard, openImageModal,
  placeInfoCard,
  placesCard,
  planCard,
  planFill,
  planPending,
  planUpdate,
  rememberCard,
  renderCompositeSection,
  routeCard,
  toolCallCard,
  videoCard,
  weatherCard, webSearchCard,
  wxIcon,
} from "./cards.js";
import { generateQrSvg } from "./qrcode.js";

// What a terminal nobody has introduced themselves to answers as, and the
// thread every conversation before the profile picker was stored under — so it
// has to keep working as a name, not just as a placeholder.
//
// Not a session id: the browser used to mint one of those per page load, which
// is exactly how the terminal came to forget every conversation. See #19.
const DEFAULT_PROFILE = "terminal";
// The screen is left up unattended, and every reconnect costs a row in
// sessions.db (main.py:256-257) on an SD card. A fixed 3 s is 1200 attempts an
// hour against a server that is not coming back — 18.6 MB of writes a day with
// nobody in the room, and `_thread` slowing from 1.6 ms to 22 ms over two
// hours as `get_session` replays the events it wrote.
//
// The first retry gets faster, not slower: a server restart is the common case
// and it comes back in a second now. What backs off is the case nobody is
// watching.
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 60000;
let reconnectDelay = RECONNECT_MIN_MS;

// A socket that came up and fell over is not a server that is back. The
// handshake at `main.py:1375` completes before `_thread`, whose five
// `session_service` awaits are unguarded — a store another writer is holding,
// a card going bad — and on a kiosk keeping its threads on an SD card that
// is the *likely* outage, not the rare one. Resetting on the open alone turns
// the backoff into a fixed 1 s: 3000 attempts an hour, 2.5x what the fixed 3 s
// cost before any of this.
//
// So the reset asks how long the socket lasted, which is only knowable once it
// has closed. Five seconds is well past those failures and well short of a
// person noticing.
const RECONNECT_HELD_MS = 5000;
let openedAt = 0;

// The other half of the same question, and the one that actually answers it.
// Uptime is a proxy for "the server is healthy" and it cannot separate the two
// cases it has to: a 4.9 s connection a Wi-Fi blip took down and a 4.9 s
// connection that died in the post-accept window are the same measurement, so
// any threshold low enough to credit the first credits the second — and a run
// of connections that all die inside five seconds never resets at all. Six of
// them put the base at the ceiling and the panel then sits `Offline` for up to
// a minute in front of a server that is already answering.
//
// What separates them is what the server *did* while connected. `main.py`
// sends one `ready` frame once every unguarded call on its accept path has
// come back, so a socket that carried one is a socket whose server got all the
// way through. Nothing else on this page can stand in for it: the server sends
// nothing at all to an idle socket, and a kiosk in an empty kitchen can go
// hours without a frame of any other kind.
let sawReady = false;

// The next wait, jittered. A factor rather than an offset so it scales with
// the delay, and clamped again afterwards so the ceiling is a real ceiling — a
// power cut brings every screen in the house back at once otherwise, and at
// the top of the range that is the worst moment to do it.
function reconnectWait(delay, random = Math.random) {
  const jittered = Math.round(delay * (0.8 + random() * 0.4));
  return Math.min(RECONNECT_MAX_MS, jittered);
}

// The base the jitter is applied to, which doubles rather than drifting: the
// jittered wait is never fed back in, or a run of low rolls would hold the
// delay down for hours.
function reconnectNext(delay) {
  return Math.min(RECONNECT_MAX_MS, delay * 2);
}

const MAPS_CALLBACK = "__onMapsReady";

// Wide enough for a cards pane beside the chat. Must match the `@media` in
// style.css, which cannot read this and so repeats the number.
const WIDE = window.matchMedia("(min-width: 720px)");

// One key for the whole settings sheet, so a setting added later comes along
// without a migration.
const SETTINGS_KEY = "ai-terminal.settings";

/* ---------- settings ---------- */

/**
 * The settings sheet, as it was left last time.
 *
 * Anything unreadable is treated as absent rather than thrown: the sheet holds
 * a microphone and a checkbox or two, and a half-written key in localStorage is
 * not worth a terminal that will not boot. Unknown keys are kept, so an older
 * build does not wipe a newer one's settings by writing the object back.
 */
function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
  } catch {
    return {};
  }
}

/** Merge a change in and write it out. Saved per change, not on sheet close. */
function saveSettings(patch) {
  settings = { ...settings, ...patch };
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // Private browsing, or a storage quota. The session still works; it just
    // will not be remembered.
  }
}

let settings = loadSettings();

/* ---------- DOM ---------- */

const feed = document.getElementById("feed");
const cards = document.getElementById("cards");
const sidePanel = document.getElementById("sidePanel");
const starters = document.getElementById("starters");
const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");
const micButton = document.getElementById("micButton");
const micSelect = document.getElementById("micSelect");
const langSelect = document.getElementById("langSelect");
const voiceSelect = document.getElementById("voiceSelect");
const interruptCheck = document.getElementById("interruptCheck");
const profileSelect = document.getElementById("profileSelect");
const profileLabel = document.getElementById("profileLabel");
const textInput = document.getElementById("textInput");
const sendButton = document.getElementById("sendButton");
const settingsButton = document.getElementById("settingsButton");
const settingsSheet = document.getElementById("settingsSheet");
const volumeButton = document.getElementById("volumeButton");
const volumeIcon = document.getElementById("volumeIcon");
const volumePop = document.getElementById("volumePop");
const volumeRange = document.getElementById("volumeRange");
const volumeRead = document.getElementById("volumeRead");
const muteButton = document.getElementById("muteButton");
const muteIcon = document.getElementById("muteIcon");
const clearButton = document.getElementById("clearButton");
const forgetButton = document.getElementById("forgetButton");
const rebootButton = document.getElementById("rebootButton");
const workspaceState = document.getElementById("workspaceState");
const workspaceScopes = document.getElementById("workspaceScopes");
const workspaceConnectBtn = document.getElementById("workspaceConnectBtn");
const workspaceDisconnectBtn = document.getElementById("workspaceDisconnectBtn");
const scrollPill = document.getElementById("scrollPill");
const whereText = document.getElementById("whereText");
const whereButton = document.getElementById("whereButton");
const clockPage = document.getElementById("clockPage");
const clockImage = document.getElementById("clockImage");
const clockTime = document.getElementById("clockTime");
const clockDate = document.getElementById("clockDate");
const clockWeather = document.getElementById("clockWeather");
const barDate = document.getElementById("barDate");
const barTime = document.getElementById("barTime");
const barWx = document.getElementById("barWx");
const barWxIcon = document.getElementById("barWxIcon");
const barWxTemp = document.getElementById("barWxTemp");

/* ---------- state ---------- */

let socket = null;
// The reconnect a close scheduled, held so `connect` can cancel it. Without a
// handle the timer outlives whatever reason it had: `reopen` opens a socket of
// its own inside the three seconds, and the timer then opens a second one on
// the same thread — two Live connections talking over each other into one
// conversation, each one hearing the other's answer back as history.
let reconnectTimer = null;
// Which build served this document, as the server named it in `/api/config`.
// Empty until `init` has asked, and empty is the state `checkRevision` refuses
// to act on: a page that never heard an answer cannot tell a new build from
// the one it is already running.
let bootRevision = "";
// Whether this document has attempted a socket. Set in `connect()` so that
// any socket that comes up after a failed attempt or a restart knows an
// attempt was made.
let hadSocket = false;
let home = savedHome(); // { lat, lng, label } — sent to the agent on connect
// Whose conversation the terminal is carrying on. In the socket path, so
// changing it means reopening; remembered, so a reboot comes back to the same
// person rather than to whoever the household is named after.
let userId = settings.profile || DEFAULT_PROFILE;
// Which voice the terminal answers in. Empty until `/api/config` has said what
// there is to choose from, and empty is a safe thing to connect on: the server
// reads a missing `voice` and an unknown one the same way, as its own default.
// A string, because this comes out of localStorage and everything else there
// is validated before it is used.
let voice = typeof settings.voice === "string" ? settings.voice : "";
let interrupt = Boolean(settings.interrupt);

// Turn state. `userBubble` and `agentBubble` are the elements currently being
// appended to; both are cleared when their transcription reports finished.
let userBubble = null, agentBubble = null;
let hasSpokenText = false; // output transcription arrived, so ignore raw text
let seenTools = new Set(); // dedupe: native audio can repeat a call in a turn
let userCleared = false;

// What the room is talking about, for the starter chips to be named after.
// Two slots rather than one, because the chips want different halves of the
// same question: after `maps_search(query: "ramen", near: "New York")` the
// weather chip wants New York and the video chip wants ramen, and one topic
// would hand each of them the other's.
//
// Read off the arguments the model passed its tools rather than out of the
// transcript. Those arguments are already its own reading of the question, in
// the words a search takes; the alternative is picking a noun out of a spoken
// sentence in two languages. Turn state in name only — it outlives a turn, and
// only clearing the conversation takes it away.
let topicPlace = "";
let topicSubject = "";
let topicDomain = "";
let currentTopicSuggestions = [];
let lastSuggestionsTopic = "";

// Audio worklets.
let listening = false;
// Whether a Live session is open underneath the socket. Not the same question
// as `listening`: the socket is up from page load until the kiosk is rebooted
// and the session is not, and a typed question opens one without ever turning
// the microphone on.
let armed = false;
// The countdown to disarming, and how long it runs for. The number is
// `WAKE_IDLE_S`, read off `/api/config` at startup; this default is what a
// page that could not reach the server falls back on, rather than a terminal
// that stays armed until somebody notices.
let wakeIdleMs = 30000;
let idleTimer = null;
let playerNode = null;
let playerContext = null;
let recorderNode = null;
let recorderContext = null;
let micStream = null;

/* ---------- feed helpers ---------- */

function nearBottom() {
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
}

function append(node) {
  if (!node) return;
  const wasDown = nearBottom();
  feed.appendChild(node);
  if (wasDown) {
    feed.scrollTo({ top: feed.scrollHeight, behavior: "smooth" });
  } else {
    scrollPill.classList.remove("hidden");
  }
}

/* ---------- cards pane ---------- */

// The pane's own padding, read back out of `.cards` in style.css because the
// scroll arithmetic below is written in terms of it: CARD_TOP is where a card
// at rest sits, CARD_TAIL is the padding under the last one when the pane has
// room to spare. Change either there and change it here.
const CARD_TOP = 14;
const CARD_TAIL = 20;

let newestCard = null; // the card `restCardAtTop` is trying to bring up
let followCards = true; // false once the person scrolls the pane themselves
let activeTurn = false; // true while an active turn is in progress
let turnCardCount = 0; // tool cards appended during the active turn (#602)

// Every card in the pane, watched for the size it did not have when it was
// appended. The callback fires once per card as it starts being watched, which
// is the after-layout pass the append itself cannot do.
const cardSizes = new ResizeObserver(() => restCardAtTop("auto"));

/**
 * Bring the newest card's top edge to the top of the cards pane.
 *
 * A place card is taller than the pane and its last few pixels are source
 * chips, so the top of the new card is the useful end of it. Two things stop
 * `scrollTo` from putting it there on its own.
 *
 * A scroll container stops at `scrollHeight - clientHeight`, and the newest
 * card is the last child. A card shorter than the pane has nothing underneath
 * it to scroll into, so the target is clamped and the card comes to rest at
 * the bottom instead — which is every weather card and every one-line answer.
 * The pane is given exactly the tail that card needs and no more, so the empty
 * space below it is never more than one screen and is gone again as soon as
 * something taller arrives.
 *
 * And the layout the target is read out of does not exist yet at append time:
 * an image has no height until it loads and a map is built on first
 * intersection, so a card *above* this one can grow afterwards and push it
 * back down. `cardSizes` watches all of them and re-aims when any one moves,
 * which covers the late fill too — a gws card goes up empty and its answer
 * arrives a turn or two later.
 */
function restCardAtTop(cardOrBehavior = "smooth", maybeBehavior = "smooth") {
  let behavior = "smooth";
  let target = newestCard;
  if (cardOrBehavior && typeof cardOrBehavior === "object") {
    target = cardOrBehavior;
    newestCard = target;
    if (typeof maybeBehavior === "string") behavior = maybeBehavior;
  } else if (typeof cardOrBehavior === "string") {
    behavior = cardOrBehavior;
  }
  if (!target || !followCards || !WIDE.matches) return;
  const pane = (typeof sidePanel !== "undefined" && sidePanel) ? sidePanel : cards;
  const room = pane.clientHeight - target.offsetHeight - CARD_TOP;
  const startersHeight = (typeof starters !== "undefined" && starters?.parentElement === pane && !starters.classList.contains("hidden")) ? ((starters.offsetHeight || 0) + 12) : 0;
  const extraTail = Math.max(0, room - startersHeight);
  pane.style.paddingBottom = `${Math.max(CARD_TAIL, extraTail)}px`;
  pane.scrollTo({
    top: Math.max(0, target.offsetTop - CARD_TOP),
    behavior,
  });
}

/**
 * Put a card wherever there is room for it, and bring it into view.
 *
 * In the feed the usual bottom-anchored append is right, because there the
 * card is the newest thing said. In the pane it goes to the top — see
 * `restCardAtTop`.
 */
function appendCard(node) {
  if (!node) return;
  feed.querySelectorAll(".msg.tool-call").forEach(el => el.remove());
  if (!WIDE.matches) {
    append(node);
    if (typeof showStarters === "function") showStarters();
    return;
  }
  cards.appendChild(node);
  const shouldScroll = !activeTurn || turnCardCount === 0;
  if (shouldScroll) {
    newestCard = node;
    // A card is something they asked for out loud, so it wins back the pane even
    // if they had scrolled away from the last one.
    followCards = true;
  }
  cardSizes.observe(node);
  if (typeof showStarters === "function") showStarters(false);
  if (shouldScroll) {
    restCardAtTop(node);
  }
  if (activeTurn) {
    turnCardCount++;
  }
}

/**
 * Take one card off the pane, and hand the pane whatever is left.
 *
 * The card being taken away can be the one the pane is aiming at — a repeat
 * question is answered on the spot and its card dropped while the first one is
 * still running. A detached element measures 0 for both `offsetTop` and
 * `offsetHeight`, so leaving it as `newestCard` would scroll the pane back to
 * the oldest card and open a tail nearly a screen deep under it.
 *
 * It can also be one of the others: two late tools can be waiting at once, and
 * a repeat of the older one drops a card with a newer one above it. Every card
 * after the gap moves up by its height, so the pane re-aims either way.
 */
function dropCard(node) {
  cardSizes.unobserve(node);
  node.remove();
  if (node === newestCard) newestCard = cards.lastElementChild;
  if (newestCard) restCardAtTop("auto");
  else {
    if (typeof sidePanel !== "undefined" && sidePanel) sidePanel.style.paddingBottom = "";
  }
}

/** Forget the pane: no card to follow, and no tail left holding it open. */
function forgetCards() {
  cardSizes.disconnect();
  newestCard = null;
  if (typeof sidePanel !== "undefined" && sidePanel) sidePanel.style.paddingBottom = "";
  activeTurn = false;
  turnCardCount = 0;
}

/**
 * Hide starter chips and recompute tail padding symmetrically with showStarters (#552).
 */
function hideStarters() {
  const starters = document.getElementById("starters");
  if (!starters) return;
  starters.classList.add("hidden");
  if (WIDE.matches && typeof restCardAtTop === "function") {
    restCardAtTop("auto");
  }
}

// The pane's own height is the other half of the tail: the settings sheet
// takes 156px off it at 1200x800, and giving them back clamps the scroll down
// again, leaving the card that was at the top part-way up it.
// `border-box` is required, not decoration — watching the content box means
// re-firing on the `padding-bottom` the callback itself writes, which Chrome
// reports as `ResizeObserver loop completed with undelivered notifications`.
new ResizeObserver(() => restCardAtTop("auto"))
  .observe((typeof sidePanel !== "undefined" && sidePanel) ? sidePanel : cards, { box: "border-box" });

// Their own scroll wins until the next card arrives. On what they did rather
// than on the `scroll` event, which `restCardAtTop` raises itself and which
// carries nothing to tell the two apart by.
//
// Three events that are only ever a scroll. Not `pointerdown` or `touchstart`:
// on a touch screen both fire on a plain tap and both bubble, so touching a
// photo in a card would hand the pane over for the rest of the turn — and the
// late fill this is all for arrives after that tap.
for (const ev of ["wheel", "touchmove", "keydown"]) {
  if (typeof sidePanel !== "undefined" && sidePanel) {
    sidePanel.addEventListener(ev, () => { followCards = false; }, { passive: true });
  }
  cards.addEventListener(ev, () => { followCards = false; }, { passive: true });
}

/** The scroll container a lazily-initialised map should watch. */
function cardRoot() {
  return WIDE.matches ? ((typeof sidePanel !== "undefined" && sidePanel) || cards) : feed;
}

feed.addEventListener("scroll", () => {
  if (nearBottom()) scrollPill.classList.add("hidden");
});

scrollPill.addEventListener("click", () => {
  feed.scrollTo({ top: feed.scrollHeight, behavior: "smooth" });
  scrollPill.classList.add("hidden");
});

function bubble(role, text) {
  if (role === "agent") {
    feed.querySelectorAll(".msg.tool-call").forEach(el => el.remove());
  }
  const div = document.createElement("div");
  div.className = `msg ${role} partial`;
  div.textContent = text;
  div.classList.add("caret");
  append(div);
  return div;
}

/** Append streamed text and keep the caret at the end. */
function extend(node, text) {
  node.textContent += text;
  if (nearBottom()) feed.scrollTop = feed.scrollHeight;
}

/** Replace a bubble's text, e.g. with a final, whole transcription. */
function replace(node, text) {
  node.textContent = text;
  if (nearBottom()) feed.scrollTop = feed.scrollHeight;
}

/** Freeze a bubble: drop the caret and render its Markdown. */
function seal(node) {
  if (!node) return;
  node.classList.remove("partial", "caret");
  if (node.classList.contains("agent") && node.textContent) {
    node.innerHTML = formatMarkdown(node.textContent);
    node.classList.add("md");
  }
}

function systemLine(text, extraClass = "") {
  const div = document.createElement("div");
  div.className = extraClass ? `msg system ${extraClass}` : "msg system";
  if (extraClass === "wake-prompt") {
    const icon = document.createElement("span");
    icon.className = "material-symbols-outlined";
    icon.textContent = "hearing";
    div.appendChild(icon);
    const span = document.createElement("span");
    span.textContent = text;
    div.appendChild(span);
  } else {
    div.textContent = text;
  }
  append(div);
  return div;
}

const WORKING_TIMEOUT_MS = 90000;
let workingTimer = null;

function showWorking() {
  clearTimeout(workingTimer);
  workingTimer = setTimeout(() => {
    hideWorking();
    systemLine(t("ui_no_answer"));
  }, WORKING_TIMEOUT_MS);
  feed.querySelectorAll(".msg.listening").forEach(el => el.remove());
  if (feed.querySelector(".working")) return;
  const div = document.createElement("div");
  div.className = "working";
  div.innerHTML =
    '<span class="material-symbols-outlined">progress_activity</span>';
  // Appended as a text node rather than interpolated into the line above:
  // the string is ours, but `innerHTML` with a table value in it is how a
  // translation table becomes an injection site the day somebody adds a
  // locale by pasting from a spreadsheet.
  div.append(t("ui_working"));
  append(div);
}

function hideWorking() {
  clearTimeout(workingTimer);
  workingTimer = null;
  feed.querySelector(".working")?.remove();
}

/** A new user turn: close any open bubbles and forget the last turn's tools. */
function startTurn() {
  // Somebody spoke, which on this terminal is the usual way in: the mic is on
  // the whole time and nobody walks up to a kitchen panel and taps it before
  // asking it something. This is the wake that matters.
  hideStarters();
  wake();
  feed.querySelectorAll(".msg.listening").forEach(el => el.remove());
  seal(userBubble);
  seal(agentBubble);
  userBubble = null;
  agentBubble = null;
  hasSpokenText = false; seenTools = new Set();
  compositeCards = new Map();
  activeCompositeCard = null;
  userCleared = false;
  activeTurn = true;
  turnCardCount = 0;
}

/* ---------- tool results ---------- */

// What tells two answers in one turn apart: the tool's name, and whichever
// field of the answer names what the answer is about. `plan.py:460` keeps the
// same list for the same reason on the server and has always had `location` in
// it; this side did not, and `weather` returns that field and none of the other
// three — so every weather answer hashed to `weather:::` and the second card in
// a turn was dropped as a repeat of the first (#182).
//
// The fields rather than the whole answer, because the native-audio model emits
// the same call two or three times in a turn and the two runs of it can differ:
// keying on everything would put the duplicate back as a second card.
function resultKey(name, response) {
  return [
    name,
    response?.query ?? "",
    response?.place ?? "",
    response?.location ?? "",
    response?.destination ?? "",
    response?.topic ?? "",
    response?.expression ?? "",
  ].join(":");
}

function renderToolResult(name, content) {
  feed.querySelectorAll(".msg.tool-call").forEach(el => el.remove());
  if (!content || typeof content !== "object") return;
  if (content.status) return;

  const root = cardRoot();

  switch (name) {
    // The three that answer out of band; their own frames draw their cards.
    //
    // Nothing reaches these arms today and they are still the right thing to
    // write. All three carry `response_scheduling`, and ADK defers such a call:
    // `handle_function_calls_live` returns None for it
    // (`flows/llm_flows/functions.py:1019`), so no function-response event is
    // yielded into the stream `main.py:1585` sends here — the answer goes back
    // to the model through the live queue and arrives on this side as a `plan`,
    // `gws` or `browse` frame instead. The day one of them stops being
    // NON_BLOCKING, the answer starts arriving here as well, and the arm is
    // what keeps it from being painted a second time as raw JSON beside the
    // card that already has it. `plan` was the one missing it (#241).
    case "browse":
    case "gws":
    case "home":
    case "plan":
    // The three whose cards are drawn out of band as composite card sections.
    // Their return value arrives as a functionResponse, but their card is already
    // drawn by handleCardSection; returning prevents drawing a second card (#450, #480)
    // while keeping them from falling through to genericCard (#241).
    case "web_search":
    case "image_search":
    case "youtube_search":
    case "camera":
      return;
    // Its dict is an acknowledgement rather than a result, so the card says so
    // in words. Without an arm here it fell through to `genericCard` and the
    // household was shown `{"kept": true, "topic": …}` (#241).
    case "remember":
      return appendCard(rememberCard(content));
    case "place_info":
      return appendCard(placeInfoCard(content, root));
    case "weather":
      return appendCard(weatherCard(content, root));
    case "maps_search":
      return appendCard(placesCard(content, root));
    case "maps_route":
    case "maps_search_along_route":
      return appendCard(routeCard(content, root));
    case "calculate":
      return appendCard(calculateCard(content));
    default:
      return appendCard(genericCard(name, content));
  }
}

/* ---------- websocket ---------- */

// Whether the socket is up, or `null` until it has said either way. Three
// states rather than two, because the pill has three things to say and the
// third one lives in the markup: `index.html` ships
// `data-i18n="ui_status_connecting"` on it, which is the truth from the first
// paint until `onopen` or `onclose` fires. `applyLanguage` repaints every
// `data-i18n` element, so it needs to know whether the markup's answer is
// still the current one — and `false` is not that answer. Saying "Offline" at
// boot to a panel that is one round trip from Ready is the same class of lie
// as the one this exists to stop.
let connected = null;

function setConnected(ok) {
  connected = ok;
  statusDot.classList.toggle("off", !ok);
  statusText.textContent = ok ? t("ui_ready") : t("ui_offline");
  sendButton.disabled = !ok || !textInput.value.trim();
}

// How long a browse card waits in silence between frames before giving up.
// Reset on each incoming frame so an active multi-step run is not cut short.
// Bounded so a stranded entry does not hold off the microphone or clock forever.
const BROWSE_DEADLINE_MS = 180000;

// The browse jobs in flight, by function-call id. Keyed rather than held
// singly because two can run at once: `plan` is NON_BLOCKING, so the planner's
// browse step and one the live agent starts while it thinks are both live, and
// one shared slot would interleave their screenshots into a single card and
// close it on whichever finished first. An entry is dropped when its job is
// done, so a second lookup gets a card of its own.
let browsing = new Map();

// Late-answering calls waiting on an answer, by function-call id: the tool's
// `name`, the empty `card`, the `line` above it announcing the call, and the
// `timer` that gives up on it. gws and plan both answer in a later turn than
// the one that called them — past the point `seenTools` is reset — so the id is
// the only thread tying the answer back to them.
//
// One map for both tools rather than one each. They differ in three things —
// the card, the deadline, and which renderer fills it — and every one of those
// is a lookup keyed on `name`. Two maps is how one of them gets a fix the other
// does not.
let pending = new Map();

// Composite cards in flight, by turn id (or active card for the current turn).
// Updated in place as streaming sections arrive out of order.
let compositeCards = new Map();
let activeCompositeCard = null;

// Per tool: how to draw the empty card, how to fill it, and how long to wait.
//
// The deadline covers only what the tool never gets the chance to turn into an
// answer of its own: the server gone, the socket dropped, ADK's background task
// cancelled. Both sit 1.2× above the tool's own timeout — `gws.TIMEOUT` 160 s
// and `plan.TIMEOUT` 250 s — so the tool always gives up first and the card
// hears it say so. A card that gives up on a call still running tells a worse
// lie than one that spins a while longer.
//
// Both numbers are pinned in `frame_cases` in tools/plan_check.py, against the
// timeouts rather than as literals: raising a tool's timeout and leaving its
// card here is exactly the inversion the paragraph above says cannot happen,
// and it used to pass the whole suite.
const LATE_TOOLS = {
  gws: { card: gwsPending, fill: gwsFill, deadline: 192000 },
  plan: { card: planPending, fill: planFill, deadline: 300000 },
};

/** Forget every late-answering call in flight, timers and all.
 *
 * `resolve` is for the caller that keeps its cards on screen. A card in this
 * map has exactly two ways to stop spinning — the answer, or the deadline —
 * and this cancels the second while `relocate()` has just destroyed the first
 * by closing the socket on a new session id. Left alone it spins until the page
 * is reloaded, which on a plan card reads as work still going on: it carries the
 * task line and the steps it reached, frozen. Filled with the same `lost` the
 * deadline would have written, it says what happened.
 *
 * The clear button passes nothing, because it removes every card on the next
 * line and a fill on the way out is work nobody sees. */
function dropPending(resolve = false) {
  // Cancelled, not left to fire: the callback looks its id up in whatever map
  // is current by then and would find nothing, but a timer outliving the card
  // it belongs to is one reload away from being a bug rather than a shrug.
  if (typeof browsing !== "undefined") {
    browsing.forEach((card) => {
      if (card.timer) {
        clearTimeout(card.timer);
        card.timer = null;
      }
    });
  }
  for (const { name, card, timer } of pending.values()) {
    clearTimeout(timer);
    if (resolve) LATE_TOOLS[name].fill(card, { error: "lost" });
  }
  pending = new Map();
}

/**
 * Reload the panel when the socket has come back onto a different build.
 *
 * A deploy is an rsync and a `systemctl --user restart ai-terminal`. It
 * replaces the files under `static/` and it restarts the server, and it does
 * neither of those things to the Chromium standing in the kitchen: the kiosk
 * unit is a plain `chromium --kiosk <url>`
 * (`deploy/ai-terminal-kiosk.service:104-111`) and the deploy's kiosk restart
 * is conditional on the managed policy or the unit file having changed
 * (`tools/pi_deploy.py:1013`, `:1349-1350`) — neither of which a new `app.js`
 * is. Nothing else here reloads: `location.reload` appears nowhere else in
 * this file and a reconnect replaces the socket, not the document. So the
 * panel runs whatever it loaded when the browser last started, which on
 * 2026-09-11 was three commits behind its own disk. #256.
 *
 * The restart is the signal, and it is a free one. It drops this page's
 * socket, the backoff in `onclose` brings it back, and the server behind the
 * new one names its build in `/api/config` — `AI_TERMINAL_REVISION`, stamped
 * into the unit by the deploy and read once at import (`live/main.py:156`,
 * `:179`, served at `:545`). That field exists to answer *which build is
 * running* and until now nothing on this page read it.
 *
 * A revision that moved is a deploy. The same revision is a server that
 * merely restarted — a crash loop, a reboot, an `ai-terminal` bounced by
 * hand — and reloading on that would turn a unit flapping every five seconds
 * into a panel reloading every five seconds.
 *
 * `no-store`, because the question is which build is up: an answer out of
 * this page's own cache is by construction the one it already has.
 *
 * What this does not settle is whether the reload arrives on *new bytes*.
 * Nothing sends `Cache-Control` for the `/static` mount (`live/main.py:222`)
 * and `index.html:16` carries no version on the URL, so Chromium decides on
 * heuristic freshness — that is #178, and it is the half that makes the
 * reload deterministic rather than the half that makes it happen at all.
 */
// The build stamped into this document, read back off the corner badge. Empty
// for a document opened directly (tests, file://) and for one served off the
// static mount where placeholders stay literal.
function stampedBuild() {
  const badge = document.getElementById("buildBadge");
  const stamp = badge && badge.dataset ? badge.dataset.staticRevision : "";
  return stamp && !stamp.startsWith("__") ? stamp : "";
}

function markStale() {
  const badge = document.getElementById("buildBadge");
  if (badge) badge.classList.add("stale");
}

async function checkRevision() {
  const first = !hadSocket;
  if (first) return;
  if (!bootRevision) {
    const seen = await fetch("/api/config", { cache: "no-store" })
      .then((r) => r.json()).catch(() => ({}));
    if (seen.build) {
      const badge = document.getElementById("buildBadge");
      if (badge && badge.textContent !== seen.build) badge.textContent = seen.build;
    }
    bootRevision = seen.static_revision || seen.revision || "";
    return;
  }
  const fresh = await fetch("/api/config", { cache: "no-store" })
    .then((r) => r.json())
    .catch(() => ({}));
  if (fresh.build) {
    const badge = document.getElementById("buildBadge");
    if (badge && badge.textContent !== fresh.build) badge.textContent = fresh.build;
  }
  // A server that answered nothing, or one too old to name itself, leaves the
  // panel alone. The failure this is written against is a stale page.
  // `static_revision` and not `revision`: the digest moves when the bytes
  // this document is made of moved; `revision` is the fallback.
  const fresh_id = fresh.static_revision || fresh.revision;
  if (!fresh_id || fresh_id === bootRevision) return;
  // Mark the corner stale before navigating. If the reload replaces the
  // document, this is invisible; if navigation fails, badge shows stale (#261).
  markStale();
  // Written back before navigation rather than after it: keeps the next
  // reconnect from asking for the same reload for ever.
  bootRevision = fresh_id;
  window.location.reload();
}

function connect() {
  // A reconnect scheduled by an earlier close is stale the moment anything
  // else opens a socket. `clearTimeout(null)` is a no-op, so the ordinary
  // reconnect path needs no special case: it clears the timer that just fired.
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  hadSocket = true;
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  // The Live API takes its system instruction once, at connection setup, so
  // the terminal's location has to travel with the handshake.
  const query = new URLSearchParams();
  if (home) {
    // The label is for the prompt, because the agent can drop an address into a
    // web search as readily as into a maps call. The coordinates go along
    // because the label is a rounding of them to the nearest thing with a name,
    // and geocoding it again does not get the exact spot back — `weather` wants
    // the spot the pin was on.
    query.set("location", home.label);
    query.set("lat", home.lat);
    query.set("lng", home.lng);
  }
  // The voice travels here and nowhere else. It is part of the Live API's
  // setup message, so there is no sending it down a socket that is already up
  // — which is why changing it goes through `reopen` like the location does.
  if (voice) query.set("voice", voice);
  if (typeof interrupt !== "undefined" && interrupt) query.set("interrupt", "1");
  // And the language, for the tools rather than for the page. The model fills
  // each tool's `lang` in and is free to leave it out; this is the setting the
  // server floors that argument at, so a Japanese panel stops drawing English
  // cards when it does. Read off the document rather than out of `settings`
  // because `applyLanguage` has already run by here and the document is what
  // `i18n.t` and the Maps tiles answer to — one source, not two that agree
  // until they do not.
  if (document.documentElement.lang) {
    query.set("lang", document.documentElement.lang);
  }
  // The one setting that comes off this page's own URL rather than out of
  // `localStorage`, because it is not a preference somebody set — it is which
  // machine this is, and the unit that launched the browser is the only thing
  // that knows. `deploy/ai-terminal-kiosk.service` opens
  // `http://localhost:8000/?kiosk=1`; nothing else does, so nothing else
  // forwards it. The server needs it because it cannot tell the panel from a
  // tab on the Mac: the panel arrives down an ssh tunnel and so connects from
  // 127.0.0.1 like every one of them. Only the wake word reads it — see
  // `screen.wake_sink` — because a wake frame opens a microphone and belongs
  // in the room the word was said in, where a card belongs on whichever page
  // is in front.
  //
  // Forwarded as written rather than turned into a boolean here, the way the
  // location and the voice are: one side decides what a value means, and it is
  // the side that acts on it.
  const kiosk = new URLSearchParams(window.location.search).get("kiosk");
  if (kiosk) query.set("kiosk", kiosk);
  const tail = query.size ? `?${query}` : "";
  // A person in the path, not a session id. The server picks the conversation
  // — it is the only side that knows how long ago this person last spoke, and a
  // fresh id per page load is what used to throw the conversation away.
  // Encoded because a profile is somebody's name, and a name is not a path
  // segment until it is escaped.
  socket = new WebSocket(
    `${scheme}//${window.location.host}/ws/${encodeURIComponent(userId)}${tail}`,
  );

  socket.onopen = () => {
    // Stamped rather than acted on. What says the server is back is a
    // connection that *lasted*, and how long this one lasts is not known until
    // it closes — see `RECONNECT_HELD_MS`.
    openedAt = Date.now();
    // Cleared beside the stamp and for the same reason. `reopen` replaces the
    // socket by hand with its `onclose` nulled, so the close that would have
    // cleared this never runs, and without this line the socket somebody
    // pressed a button to get would be credited with the last one's `ready`.
    sawReady = false;
    setConnected(true);
    // Whatever the last socket had armed died with it, and nothing on this
    // page can see that happen: a reconnect under a microphone that is still
    // on would stream the room to a server holding no session to put it in.
    // Say it again rather than finding out when nobody answers.
    armed = false;
    // The countdown it was arming for died with it too, and clearing the flag
    // alone does not stop it: `idleTimer` is this page's timeout, not the
    // socket's. With the microphone off the line below arms nothing, so
    // nothing calls `restartIdle` and the old deadline still arrives — at
    // which point `goIdle` tells a kitchen that was never listening that it
    // has stopped. `clearTimeout(null)` is a no-op, so no guard.
    clearTimeout(idleTimer);
    idleTimer = null;
    if (listening) armSession();
    // A reconnect while the screen is dark tells the server so immediately.
    // Server-side dark_at is scoped to one connection, so without this report
    // a reconnect or reload behind the screensaver leaves the server with
    // dark_at = None and replaying stale history on the next wake word. #448.
    if (typeof asleep !== "undefined" && asleep && socket?.send) {
      socket.send(JSON.stringify({ type: "sleep" }));
    }
    // Last, and deliberately not awaited. Everything above is what this
    // socket needs to be usable; this is the different question of whether
    // the page holding it is still the right page, and its answer must not
    // stand between a reconnect and the microphone.
    checkRevision();
  };
  socket.onerror = () => setConnected(false);

  socket.onclose = () => {
    setConnected(false);
    hideWorking();
    seal(userBubble);
    seal(agentBubble);
    userBubble = null;
    agentBubble = null;
    // Ended by hand, for the same reason as `reopen` and with more cause: a
    // browse card has no deadline — `LATE_TOOLS` gives it none — and its only
    // terminal frame is the `done` the tool sends over the socket that has
    // just gone (`browse.py`'s `ended` swallows the send). Left alone it
    // spins until a reload, which on a kiosk nobody performs.
    for (const card of browsing.values()) {
      if (card.timer) {
        clearTimeout(card.timer);
        card.timer = null;
      }
      browseUpdate(card, { phase: "done", lost: true });
    }
    browsing = new Map();
    if (typeof compositeCards !== "undefined") compositeCards = new Map();
    if (typeof activeCompositeCard !== "undefined") activeCompositeCard = null;
    // The backoff starts over for a connection whose server said `ready` or
    // one that held, and never in `connect` — which runs on every failed
    // attempt too. Both are decided here rather than where the evidence
    // arrives, so there is one place that says what counts as the server being
    // back, and so the flag is cleared on the same line as the stamp.
    if (sawReady
        || (openedAt && Date.now() - openedAt > RECONNECT_HELD_MS)) {
      reconnectDelay = RECONNECT_MIN_MS;
    }
    sawReady = false;
    openedAt = 0;
    reconnectTimer = setTimeout(connect, reconnectWait(reconnectDelay));
    reconnectDelay = reconnectNext(reconnectDelay);
  };

  socket.onmessage = (frame) => {
    // One bad frame is not a reason to stop drawing. Without this a throw in a
    // handler is swallowed by the event dispatch: the frame is lost, the next
    // one is fine, and the only trace is an uncaught error in a console nobody
    // has open on a kiosk.
    try {
      const data = JSON.parse(frame.data);
      // Anything at all arriving here is somebody still being here: the model
      // answering, or the transcription of what was just said to it. Reset
      // before the dispatch rather than after, so a handler that throws costs
      // the terminal one frame and not its microphone.
      restartIdle();
      // Nothing to draw. It is the server saying its accept path came back
      // clean — the conversation is open and the four registries are up — and
      // the only thing that reads it is the backoff, at the close after this
      // one. See `sawReady`.
      if (data.type === "ready") {
        sawReady = true; return;
      }
      if (data.type === "history") return handleHistory(data);
      // Not from the model at all: the wake-word monitor on the Pi, reaching
      // this page through `/api/wake` and the screen sink.
      if (data.type === "wake") return handleWake();
      // The server ended the Live session (clean return or failure). Disarm
      // and stop listening immediately so the next wake word is heard without
      // waiting for the 30-second idle timer. #285.
      if (data.type === "idle") return goIdle();
      // browse, gws and plan talk to the screen directly rather than through
      // the model. All three are NON_BLOCKING, and ADK feeds a NON_BLOCKING
      // tool's response back into the live queue instead of yielding it, so
      // nothing about any of them would otherwise reach this socket but the
      // call.
      if (data.type === "browse") return handleBrowse(data);
      if (data.type === "gws") return handleLate(data);
      if (data.type === "plan") return handlePlan(data);
      if (data.type === "progress") return handleProgress(data);
      if (data.type === "card_section") return handleCardSection(data);
      if (data.type === "camera") return handleCamera(data);
      if (data.type === "home") return handleHome(data);
      handleEvent(data);
    } catch (err) {
      console.error("dropped a frame", err);
    }
  };
}

/**
 * A section for a composite card from a streaming tool (web_search, image_search).
 *
 * One composite card per turn, updated in place as sections arrive. The card is
 * created on the first non-empty section frame rather than when the tool is
 * called, so it appears with something in it.
 */
function handleCardSection(data) {
  if (!data || !data.section || !data.data) return;
  hideWorking();
  feed.querySelectorAll(".msg.tool-call").forEach(el => el.remove());

  const turnId = data.turn_id || "";
  let card = (turnId && compositeCards.get(turnId)) || activeCompositeCard;

  if (!card) {
    card = compositeCard({ turn_id: turnId, query: data.data?.query });
    const rendered = renderCompositeSection(card, data.section, data.data, cardRoot());
    if (!rendered) return;
    if (turnId) compositeCards.set(turnId, card);
    activeCompositeCard = card;
    appendCard(card);
    return;
  }

  renderCompositeSection(card, data.section, data.data, cardRoot());
}

function handleCamera(data) {
  hideWorking();
  const card = cameraCard(data.data || {}, cardRoot());
  if (card) appendCard(card);
}

function handleHome(data) {
  hideWorking();
  if (!data || !data.card_type) return;
  let card = null;
  if (data.card_type === "summary") {
    card = homeSummaryCard(data);
  } else if (data.card_type === "timeline") {
    card = homeTimelineCard(data);
  }
  if (card) appendCard(card);
}

/**
 * A frame from a running browse job.
 *
 * One card per job, updated in place. The card is created on the first frame
 * rather than when the tool is called, so it appears with something in it.
 */
function handleBrowse(data) {
  hideWorking();
  let card = browsing.get(data.id);
  if (!card) {
    card = browseCard(data);
    card.lost = false;
    browsing.set(data.id, card);
    appendCard(card);
  }
  browseUpdate(card, data);
  if (data.phase === "done") {
    if (card.timer) {
      clearTimeout(card.timer);
      card.timer = null;
    }
    browsing.delete(data.id);
  } else if (!card.lost) {
    // Reset silence timer on each incoming frame while the job is active.
    if (card.timer) clearTimeout(card.timer);
    const deadline = typeof BROWSE_DEADLINE_MS !== "undefined" ? BROWSE_DEADLINE_MS : 180000;
    const timer = setTimeout(() => {
      card.lost = true;
      card.timer = null;
      browseUpdate(card, { phase: "done", lost: true });
    }, deadline);
    if (timer.unref) timer.unref();
    Object.defineProperty(card, "timer", {
      value: timer,
      writable: true,
      configurable: true,
      enumerable: false,
    });
  }
}

/**
 * Where a slow tool has got to, halfway through.
 *
 * Only Workspace draws these. browse sends none: its card is already showing a
 * screenshot of the page and the step it is on, which says more than a line of
 * text would; and plan sends none for the same reason, its card being the step
 * list. A note from a gws call nested inside a plan carries its own call id,
 * which is in no map, so it lands nowhere — the plan card belongs to the plan.
 */
function handleProgress(data) {
  const waiting = data.id && pending.get(data.id);
  if (waiting?.name === "gws") gwsProgress(waiting.card, data.state);
}

/**
 * The planner, mid-plan or done.
 *
 * A frame with `steps` is `show_plan` reporting; the card takes it and keeps
 * waiting, because a plan sends several of these and only the last frame is the
 * answer. Everything else is the answer and goes through `handleLate`.
 */
function handlePlan(data) {
  if (Array.isArray(data.steps)) {
    const waiting = data.id && pending.get(data.id);
    // No fallback card for a step list that has lost its card. It is progress,
    // and progress on a plan nobody can see any more is not worth a card of its
    // own — the answer still gets one.
    if (waiting) planUpdate(waiting.card, data);
    return;
  }
  handleLate(data);
}

/**
 * An answer from a late tool, arriving a turn or two after the call that asked
 * for it.
 *
 * It fills the empty card that call put up. The fallback matters: an answer can
 * outlive its card — the conversation was cleared while the sub-agent worked —
 * and an answer nobody can see is worse than one in a card of its own.
 *
 * `error` is a call that ended without one — it failed, or it searched and came
 * up empty. Either way the card resolves and says so rather than spinning.
 *
 * `drop` is the third way a card ends: the model asked the same question again
 * while the first one was still running, and the tool answered the repeat on
 * the spot rather than searching twice. Nothing will ever fill that card, and
 * the line announcing it says the same thing as the one above it, so both go.
 *
 * The fourth is the tool's `deadline` running out, which is the only one that
 * does not arrive as a frame — nothing here can be told about a server that is
 * gone. A card that gave up that way is still in `pending`, so an answer
 * arriving after it replaces the text rather than appearing beneath it.
 */
function handleLate(data) {
  hideWorking();
  const waiting = data.id && pending.get(data.id);
  if (waiting) {
    clearTimeout(waiting.timer);
    pending.delete(data.id);
  }
  if (data.drop) {
    if (waiting) {
      dropCard(waiting.card);
      waiting.line.remove();
    }
    return;
  }
  if (waiting) return LATE_TOOLS[waiting.name].fill(waiting.card, data);
  appendCard(data.type === "plan" ? planCard(data) : gwsCard(data));
}

function handleEvent(event) {
  // What the user said. A `finished` transcription carries the whole utterance
  // rather than the last delta, so it replaces the bubble instead of extending
  // it — appending it would print the sentence twice.
  const input = event.inputTranscription;
  if (input?.text) {
    if (!userBubble) {
      startTurn();
      userBubble = bubble("user", input.text);
    } else if (input.finished) {
      replace(userBubble, input.text);
    } else {
      extend(userBubble, input.text);
    }
    if (input.finished) {
      seal(userBubble);
      userBubble = null;
    }
  }

  // What the model said, transcribed from its own audio.
  const output = event.outputTranscription;
  if (output?.text) {
    // The agent can be the one talking without `startTurn` having run — it
    // opens on the recap after a rollover, and a long tool answer lands in a
    // turn of its own. The screen must not still be a clock while it speaks.
    if (!cards.children.length) hideStarters();
    wake();
    hasSpokenText = true;
    hideWorking();
    seal(userBubble);
    userBubble = null;
    if (!agentBubble) agentBubble = bubble("agent", output.text);
    else if (output.finished) replace(agentBubble, output.text);
    else extend(agentBubble, output.text);
    if (output.finished) {
      seal(agentBubble);
      agentBubble = null;
    }
  }

  for (const part of event.content?.parts || []) {
    // Model audio.
    if (part.inlineData?.mimeType?.startsWith("audio/pcm")) {
      playerNode?.port.postMessage(base64ToArrayBuffer(part.inlineData.data));
      continue;
    }

    if (part.functionCall) {
      const { name = "tool", args = {}, id } = part.functionCall;
      // What the room is about, for the chips shown after it. Ahead of the
      // dedupe: a repeated call names the same topic, and a call whose card
      // was suppressed is still the model saying what this is about.
      noteTopic(args, name);
      // `args.lang` is read no further than this. It is the language the model
      // heard, and it still decides what the tool searches in and what the
      // sidecar writes back — but it used to be parked on `<html lang>` too,
      // which made the settings sheet and every card heading change language
      // because somebody asked one question in the other one. The panel's
      // language is a setting now; see `langSelect` below.
      // The native-audio model sometimes emits the same call two or three
      // times in a turn. Identical arguments are the same question; two
      // searches with different arguments still get a line each.
      const key = `call:${name}:${JSON.stringify(args)}`;
      if (!seenTools.has(key)) {
        seenTools.add(key);
        const line = toolCallCard(name, args);
        append(line);
        // gws and plan answer in a later turn, so the card goes up now and is
        // filled in when the answer lands. Every other tool has its result in
        // hand by the time the turn ends and draws its card then. The line
        // above it is kept too, because a repeat of a question already running
        // takes both back off the screen.
        const late = LATE_TOOLS[name];
        if (late && id) {
          const card = late.card(args.task || args.request || "");
          // The entry stays in the map after the deadline fires. Giving up on
          // an answer is not the same as it never coming, and a late frame
          // that found no entry would append a second card with the real
          // answer, stacked under one saying none arrived — the screen
          // contradicting itself and the audio. Left in place, the answer
          // lands in this card and overwrites the line. A missing entry means
          // the conversation was cleared out from under it.
          //
          // The cost of keeping it is that a progress note can arrive after
          // the card has resolved, so `gwsProgress` is written to tolerate a
          // card with no progress line rather than this being cleaned up here.
          const timer = setTimeout(() => {
            const lost = pending.get(id);
            // Marked, not deleted. The entry has to stay for the reason
            // above, but the card has stopped spinning and says no answer
            // came, so from here on it is not somebody waiting — and
            // anything reading `pending` to find out whether it is wants
            // this flag and not the size. See `sleep`.
            if (lost) {
              lost.lost = true;
              late.fill(lost.card, { error: "lost" });
            }
          }, late.deadline);
          pending.set(id, { name, line, card, timer });
          appendCard(card);
        }
      }
      showWorking();
      continue;
    }

    if (part.functionResponse) {
      hideWorking();
      const { name = "tool", response } = part.functionResponse;

      // The query identifies a grounding call, and two searches in one turn
      // are different questions that both deserve a card. `topic` is the same
      // thing for `remember`: "remember the car is blue and that I don't eat
      // gluten" is two calls in one turn, and without it here the second is
      // fingerprinted `remember:::` exactly like the first and dropped, so the
      // panel says one fact was kept when two were. An identical repeat — the
      // native-audio model emitting the same call twice, which is what this
      // guard is for — still carries the same topic and is still dropped.
      const key = resultKey(name, response);
      if (seenTools.has(key)) continue;
      seenTools.add(key);
      renderToolResult(name, response);
      continue;
    }

    // Text from a half-cascade model. Native audio sends transcription
    // instead, and the two would otherwise duplicate each other.
    if (part.text && !part.thought && !hasSpokenText) {
      hideWorking();
      seal(userBubble);
      userBubble = null;
      if (!agentBubble) agentBubble = bubble("agent", part.text);
      else extend(agentBubble, part.text);
    }
  }

  if (event.interrupted) {
    playerNode?.port.postMessage({ command: "endOfAudio" });
    agentBubble?.classList.add("interrupted");
    seal(agentBubble);
    agentBubble = null;
    hideWorking();
    compositeCards = new Map();
    activeCompositeCard = null;
    activeTurn = false;
    turnCardCount = 0;
  }

  if (event.turnComplete) {
    hideWorking();
    seal(userBubble);
    seal(agentBubble);
    userBubble = null;
    agentBubble = null;
    hasSpokenText = false;
    compositeCards = new Map();
    activeCompositeCard = null;
    activeTurn = false;
    turnCardCount = 0;
    if (typeof showStarters === "function") showStarters(false);
  }
}

/* ---------- composer ---------- */

function send(text) {
  if (socket?.readyState !== WebSocket.OPEN) return;
  // The server starts a Live session on an `arm` frame and on nothing else, so
  // a typed question has to open one exactly the way the wake word does. Skip
  // this and the text goes to a socket with no model behind it: no answer, and
  // no error anywhere to say why.
  armSession();
  startTurn();
  const div = document.createElement("div");
  div.className = "msg user";
  div.textContent = text;
  append(div);
  socket.send(JSON.stringify({ type: "text", text }));
  showWorking();
}

async function submit() {
  const text = textInput.value.trim();
  if (!text) return;
  textInput.value = "";
  sendButton.disabled = true;
  // A typed question still gets a spoken answer, so the player has to exist.
  // This click is the user gesture the AudioContext needs.
  await ensurePlayer();
  send(text);
}

textInput.addEventListener("input", () => {
  sendButton.disabled = !textInput.value.trim() || socket?.readyState !== WebSocket.OPEN;
});

textInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    submit();
  }
});

sendButton.addEventListener("click", submit);

starters.addEventListener("click", async (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  await ensurePlayer();
  send(chip.dataset.prompt);
});

/* ---------- the starter chips ---------- */

// Longest topic a chip label carries before it is cut. The prompt keeps the
// whole of it: what is being fitted is one line of a button, and what gets
// sent is not on the button.
const TOPIC_MAX = 24;

// Which arguments name a where and which name a what.
const TOPIC_PLACE_ARGS = ["place", "destination", "near", "location"];
const TOPIC_SUBJECT_ARGS = ["query"];
// The words the tools take for the terminal's own location. Every chip
// already means here, so a search around the kitchen names no new place and
// the chips are left saying what they said.
const TOPIC_HERE = new Set(["here", "home", "nearby", "current location"]);
const TOPIC_MEDIA_MARKER = /[（(]\s*(?:テレビ番組|番組|アニメ|映画|ドラマ|ラジオ|漫画|楽曲|曲|album|movie|film|tv\s*(?:series|show|program))\s*[）)]/i;
const TOPIC_PLACE_TOOLS = new Set(["places", "place_info", "directions", "weather", "maps_search"]);

/** Remember what this tool call was about, for the chips shown after it. */
function noteTopic(args, toolName = "") {
  if (typeof args === "string" && typeof toolName === "object") {
    const tmp = args;
    args = toolName;
    toolName = tmp;
  }
  if (args && typeof args === "object" && args.args && typeof args.name === "string" && !toolName) {
    toolName = args.name;
    args = args.args;
  }
  const value = (key) => {
    const raw = args?.[key];
    return typeof raw === "string" ? raw.trim() : "";
  };
  for (const key of TOPIC_PLACE_ARGS) {
    const where = value(key);
    if (!where) continue;
    if (TOPIC_HERE.has(where.toLowerCase())) continue;
    if (TOPIC_MEDIA_MARKER.test(where)) continue;
    const isExplicitKey = (key === "destination" || key === "near" || key === "location");
    if (!isExplicitKey && !TOPIC_PLACE_TOOLS.has(toolName)) continue;
    topicPlace = where;
    break;
  }
  const subjectKeys = (toolName === "place_info" && !value("query"))
    ? ["query", "place"]
    : TOPIC_SUBJECT_ARGS;
  for (const key of subjectKeys) {
    const what = value(key);
    if (what) {
      topicSubject = what;
      break;
    }
  }
  let dom = typeof topicDomain !== "undefined" ? topicDomain : "";
  if (toolName) {
    if (toolName === "weather") {
      dom = "weather";
    } else if (toolName === "directions") {
      dom = "directions";
    } else if (toolName === "places" || toolName === "place_info") {
      dom = "explore";
    } else if (toolName === "maps_search") {
      dom = "places";
    } else {
      dom = "";
    }
  } else if (!dom) {
    if (args?.location && !args?.query) {
      dom = "weather";
    }
  }
  if (typeof topicDomain !== "undefined") {
    topicDomain = dom;
  }
  const topicToFetch = dom === "weather" ? (topicPlace || topicSubject) : (topicSubject || topicPlace);
  if (topicToFetch && typeof fetchTopicSuggestions === "function") {
    fetchTopicSuggestions(topicToFetch, dom);
  }
}

/** Fetch dynamic starter suggestions based on a topic string. */
async function fetchTopicSuggestions(topic, domain = "") {
  if (!topic || typeof fetch !== "function") return;
  const trimmed = topic.trim();
  const cacheKey = `${trimmed}::${domain}`;
  if (!trimmed || cacheKey === lastSuggestionsTopic) return;
  lastSuggestionsTopic = cacheKey;
  try {
    const lang = (typeof document !== "undefined" && document.documentElement?.lang) || "ja";
    const domainParam = domain ? `&domain=${encodeURIComponent(domain)}` : "";
    const res = await fetch(`/api/starters/suggestions?q=${encodeURIComponent(trimmed)}${domainParam}&lang=${encodeURIComponent(lang)}`);
    if (!res.ok) return;
    const data = await res.json();
    if (lastSuggestionsTopic === cacheKey) {
      currentTopicSuggestions = Array.isArray(data.suggestions) ? data.suggestions : [];
      renderStarters();
      if (typeof WIDE !== "undefined" && WIDE?.matches && typeof restCardAtTop === "function" && starters && !starters.classList.contains("hidden")) {
        restCardAtTop("auto");
      }
    }
  } catch (err) {
    currentTopicSuggestions = [];
  }
}

/** Back to a panel nobody has asked anything. */
function forgetTopic() {
  topicPlace = "";
  topicSubject = "";
  if (typeof topicDomain !== "undefined") {
    topicDomain = "";
  }
  if (typeof currentTopicSuggestions !== "undefined") {
    currentTopicSuggestions = [];
  }
  if (typeof lastSuggestionsTopic !== "undefined") {
    lastSuggestionsTopic = "";
  }
  const dynamicContainer = typeof starters !== "undefined" && starters?.querySelector ? starters.querySelector(".starter-dynamic") : null;
  if (dynamicContainer) {
    if (dynamicContainer.replaceChildren) {
      dynamicContainer.replaceChildren();
    } else {
      dynamicContainer.innerHTML = "";
    }
  }
  const contextGroup = typeof starters !== "undefined" && starters?.querySelector ? starters.querySelector(".starters-context") : null;
  if (contextGroup?.classList?.add) {
    contextGroup.classList.add("hidden");
  }
}

/**
 * Write the chips: in the panel's language, and about whatever the
 * conversation last named.
 *
 * Called when the row is about to be shown rather than whenever the topic
 * moves. The row remains visible unless a conversation is in flight —
 * `startTurn` and mid-conversation wake both hide it — so the moments its
 * text can be read are before speech starts and when a timeout puts it back.
 *
 * A chip with no `data-topic-slot`, and a chip whose slot is still empty, is
 * written from the two keys `applyStatic` would have used. So this is what a
 * panel nobody has spoken to shows, and it is also the undo: the chips go
 * back to their plain labels the moment the topic does.
 */
function renderStarters() {
  let dynamicContainer = typeof starters !== "undefined" && starters.querySelector ? starters.querySelector(".starter-dynamic") : null;
  if (!dynamicContainer && typeof starters !== "undefined" && starters.querySelector) {
    const row = starters.querySelector(".starter-row");
    if (row && typeof document !== "undefined" && document.createElement) {
      dynamicContainer = document.createElement("div");
      dynamicContainer.className = "starter-dynamic";
      if (row.prepend) {
        row.prepend(dynamicContainer);
      } else if (row.children && row.children.unshift) {
        row.children.unshift(dynamicContainer);
      }
    }
  }

  if (dynamicContainer) {
    if (dynamicContainer.replaceChildren) {
      dynamicContainer.replaceChildren();
    } else {
      dynamicContainer.innerHTML = "";
    }
    if (typeof currentTopicSuggestions !== "undefined" && currentTopicSuggestions && currentTopicSuggestions.length > 0) {
      for (const item of currentTopicSuggestions) {
        const btn = document.createElement("button");
        btn.className = "chip dynamic-chip";
        btn.dataset.prompt = item.prompt || "";
        const iconSpan = document.createElement("span");
        iconSpan.className = "material-symbols-outlined";
        iconSpan.textContent = item.icon || "lightbulb";
        const labelSpan = document.createElement("span");
        labelSpan.textContent = item.label || "";
        btn.appendChild(iconSpan);
        btn.appendChild(labelSpan);
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (typeof ensurePlayer === "function") await ensurePlayer();
          if (typeof send === "function") send(btn.dataset.prompt);
        });
        dynamicContainer.appendChild(btn);
      }
    }
  }

  const contextGroup = typeof starters !== "undefined" && starters.querySelector ? starters.querySelector(".starters-context") : null;
  if (contextGroup) {
    const hasDynamic = dynamicContainer && (dynamicContainer.children ? dynamicContainer.children.length > 0 : false);
    if (contextGroup.classList) {
      if (hasDynamic) contextGroup.classList.remove("hidden");
      else contextGroup.classList.add("hidden");
    }
  }

  for (const chip of starters.querySelectorAll(".chip")) {
    if (chip.classList?.contains?.("dynamic-chip")) continue;
    const label = chip.querySelector("[data-i18n]");
    if (!label) continue;
    const slot = chip.dataset.topicSlot;
    const isPlaceValid = Boolean(
      topicPlace &&
      typeof topicPlace === "string" &&
      topicPlace.trim() &&
      !TOPIC_HERE.has(topicPlace.trim().toLowerCase()) &&
      !TOPIC_MEDIA_MARKER.test(topicPlace)
    );
    const isRedundantWeather = (
      slot === "place" &&
      chip.dataset.i18nPrompt === "ui_ask_weather" &&
      typeof topicDomain !== "undefined" &&
      topicDomain === "weather" &&
      isPlaceValid
    );
    if (isRedundantWeather) {
      if (chip.classList?.add) chip.classList.add("hidden");
      if (chip.style) chip.style.display = "none";
      continue;
    } else {
      if (chip.classList?.remove) chip.classList.remove("hidden");
      if (chip.style) chip.style.display = "";
    }
    const topic = (slot === "place" && isPlaceValid) ? topicPlace.trim()
      : slot === "subject" ? topicSubject
      : "";
    if (topic && chip.dataset.topicLabel && chip.dataset.topicPrompt) {
      const short = topic.length > TOPIC_MAX
        ? `${topic.slice(0, TOPIC_MAX - 1).trimEnd()}…`
        : topic;
      label.textContent = t(chip.dataset.topicLabel).replace("{q}", short);
      chip.dataset.prompt = t(chip.dataset.topicPrompt).replace("{q}", topic);
    } else {
      label.textContent = t(label.dataset.i18n); if (chip && chip.dataset.i18nPrompt) { chip.dataset.prompt = t(chip.dataset.i18nPrompt); }
    }
  }
}

/** Put the row back, named for whatever the conversation last named. */
function showStarters(behavior = "auto") {
  renderStarters();
  const isWide = typeof WIDE !== "undefined" && WIDE?.matches;
  if (isWide && typeof sidePanel !== "undefined" && sidePanel) {
    if (starters.parentElement !== sidePanel || starters.previousElementSibling !== cards) {
      sidePanel.appendChild(starters);
    }
  } else if (typeof feed !== "undefined" && feed) {
    feed.appendChild(starters);
    feed.scrollTo?.({ top: feed.scrollHeight, behavior: "smooth" });
  }
  starters.classList.remove("hidden");
  if (behavior !== false && isWide && typeof restCardAtTop === "function") {
    restCardAtTop(behavior);
  }
}

if (typeof WIDE !== "undefined" && WIDE?.addEventListener) {
  WIDE.addEventListener("change", () => {
    if (starters?.classList?.contains?.("hidden")) return;
    if (WIDE.matches && typeof sidePanel !== "undefined" && sidePanel) {
      if (starters.parentElement !== sidePanel || starters.previousElementSibling !== cards) {
        sidePanel.appendChild(starters);
      }
      if (typeof restCardAtTop === "function") restCardAtTop("auto");
    } else if (typeof feed !== "undefined" && feed) {
      feed.appendChild(starters);
      feed.scrollTo?.({ top: feed.scrollHeight, behavior: "smooth" });
    }
  });
}

if (typeof WIDE !== "undefined" && !WIDE.matches && typeof feed !== "undefined" && feed && typeof starters !== "undefined" && starters && starters.parentElement !== feed) {
  feed.appendChild(starters);
}

settingsButton.addEventListener("click", () => {
  settingsSheet.classList.toggle("hidden");
  if (!settingsSheet.classList.contains("hidden")) {
    refreshGwsStatus();
  }
});

clearButton.addEventListener("click", () => {
  hideWorking();
  feed.querySelectorAll(".msg, .card, .working").forEach((n) => n.remove());
  cards.replaceChildren();
  // Before the placeholder goes back: `.cards:empty` centres it, and a tail
  // left behind from the last card would centre it around a screen of nothing.
  forgetCards();
  if (typeof sidePanel !== "undefined" && sidePanel) {
    sidePanel.scrollTo?.({ top: 0, behavior: "auto" });
  }
  // The conversation is gone, so what it was about goes with it: the chips
  // come back reading what they read at boot, which is the fallback the
  // contextual ones are measured against.
  forgetTopic();
  showStarters();
  userBubble = null;
  agentBubble = null;
  browsing.forEach((card) => {
    if (card.timer) {
      clearTimeout(card.timer);
      card.timer = null;
    }
  });
  browsing = new Map();
  if (typeof compositeCards !== "undefined") compositeCards = new Map();
  if (typeof activeCompositeCard !== "undefined") activeCompositeCard = null;
  dropPending();
  seenTools = new Set(); userCleared = true;
});

// How long "Forget my history" stays armed after the first tap. Long enough to
// read the label that changed under your finger and mean it, short enough that
// a settings sheet left open on the counter is not one stray tap away from
// deleting a month of conversation.
const FORGET_ARM_MS = 5000;
let forgetTimer = null;

/** Put the button back to its resting label, whatever armed it. */
function disarmForget() {
  clearTimeout(forgetTimer);
  forgetTimer = null;
  forgetButton.classList.remove("armed");
  forgetButton.textContent = t("ui_forget");
}

/**
 * Delete this profile's stored conversations — on the second tap.
 *
 * Two taps rather than `window.confirm`: a modal on a kiosk with no window
 * manager is a dialog nobody reads, and this is the one control on the sheet
 * that destroys something. Arming relabels the button, so what the second tap
 * is about to do is written on the thing being tapped.
 *
 * It deletes the whole of this profile's history, not the thread on screen,
 * because the server cannot honestly offer the narrower one: `_thread`
 * resolves to the newest session left, so forgetting only this one surfaces
 * the previous conversation at the next hello. The label says so.
 *
 * Nothing local is thrown away until the server says the delete happened. A
 * failed POST leaves the screen, the socket and the thread as they were and
 * says so — a cleared display over a server that still has the history is the
 * one outcome this button must never produce.
 */
async function forget() {
  if (!forgetTimer) {
    forgetButton.classList.add("armed");
    forgetButton.textContent = t("ui_forget_armed");
    forgetTimer = setTimeout(disarmForget, FORGET_ARM_MS);
    return;
  }
  disarmForget();
  let count;
  try {
    // The header is the point, and the server refuses the request without it
    // (`main.py:_sent_deliberately`). Without a content type this is a CORS
    // simple request, which any page on the home network may send to us
    // unpreflighted — it cannot read the answer, but the conversations are
    // already gone. `application/json` is not on the simple list, so a
    // cross-origin attempt has to ask permission first and is refused.
    // Same-origin, which is us, is never preflighted and pays nothing.
    const res = await fetch(`/api/forget/${encodeURIComponent(userId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    ({ forgotten: count } = await res.json());
  } catch (err) {
    systemLine(t("ui_forget_failed").replace("{e}", err.message));
    return;
  }
  // The open socket is still on a thread that no longer exists, so it goes
  // before anything else is said down it. `reopen` lands on a new one, now
  // the only one there is.
  clearButton.click();
  reopen(
    count
      ? t(count === 1 ? "ui_forgot_one" : "ui_forgot_many")
        .replace("{n}", count)
      : t("ui_forgot_none"),
  );
}

forgetButton.addEventListener("click", forget);

const REBOOT_ARM_MS = 5000;
let rebootTimer = null;

/** Put the reboot button back to its resting label, whatever armed it. */
function disarmReboot() {
  clearTimeout(rebootTimer);
  rebootTimer = null;
  if (rebootButton) {
    rebootButton.classList.remove("armed");
    rebootButton.textContent = t("ui_reboot");
  }
}

/**
 * Restart the appliance host — on the second tap.
 *
 * Two taps rather than `window.confirm`: a modal on a kiosk with no window
 * manager is a dialog nobody reads, and restarting takes the terminal down.
 * Arming relabels the button with a 5 s expiry.
 */
async function reboot() {
  if (!rebootButton) return;
  if (!rebootTimer) {
    rebootButton.classList.add("armed");
    rebootButton.textContent = t("ui_reboot_armed");
    rebootTimer = setTimeout(disarmReboot, REBOOT_ARM_MS);
    return;
  }
  disarmReboot();
  try {
    const res = await fetch("/api/reboot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    systemLine(t("ui_rebooting"));
  } catch (err) {
    systemLine(t("ui_reboot_failed").replace("{e}", err.message));
  }
}

if (rebootButton) {
  rebootButton.addEventListener("click", reboot);
}
 
/* ---------- Google Workspace sign-in ---------- */

let gwsPollTimer = null;

function renderGwsStatus(status) {
  if (!workspaceState || !workspaceConnectBtn || !workspaceDisconnectBtn) return;
  if (!status || !status.available) {
    workspaceState.textContent = "Google Workspace — CLI not installed";
    workspaceConnectBtn.disabled = true;
    workspaceConnectBtn.classList.remove("hidden");
    workspaceDisconnectBtn.classList.add("hidden");
    if (workspaceScopes) {
      workspaceScopes.classList.add("hidden");
      workspaceScopes.replaceChildren();
    }
    return;
  }
  workspaceConnectBtn.disabled = false;
  if (status.connected) {
    const acc = status.account || "account";
    workspaceState.textContent = `Connected as ${acc}`;
    workspaceConnectBtn.classList.add("hidden");
    workspaceDisconnectBtn.classList.remove("hidden");
    if (workspaceScopes) {
      workspaceScopes.replaceChildren();
      if (Array.isArray(status.scopes) && status.scopes.length > 0) {
        workspaceScopes.classList.remove("hidden");
        for (const s of status.scopes) {
          const badge = document.createElement("span");
          const name = s.includes("/") ? s.split("/").pop() : s;
          badge.textContent = name;
          badge.title = s;
          workspaceScopes.appendChild(badge);
        }
      } else {
        workspaceScopes.classList.add("hidden");
      }
    }
  } else if (status.expired) {
    workspaceState.textContent = "Google Workspace — Access expired";
    workspaceConnectBtn.classList.remove("hidden");
    workspaceDisconnectBtn.classList.add("hidden");
    if (workspaceScopes) {
      workspaceScopes.classList.add("hidden");
      workspaceScopes.replaceChildren();
    }
  } else {
    workspaceState.textContent = "Google Workspace — Not connected";
    workspaceConnectBtn.classList.remove("hidden");
    workspaceDisconnectBtn.classList.add("hidden");
    if (workspaceScopes) {
      workspaceScopes.classList.add("hidden");
      workspaceScopes.replaceChildren();
    }
  }
}

async function refreshGwsStatus() {
  try {
    const res = await fetch("/api/gws/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const status = await res.json();
    renderGwsStatus(status);
    return status;
  } catch (err) {
    renderGwsStatus({ available: false });
    return null;
  }
}

const gwsAuthModal = document.getElementById("gwsAuthModal");
const gwsAuthModalBackdrop = document.getElementById("gwsAuthModalBackdrop");
const gwsAuthModalClose = document.getElementById("gwsAuthModalClose");
const gwsAuthQr = document.getElementById("gwsAuthQr");
const gwsAuthUrl = document.getElementById("gwsAuthUrl");
const gwsAuthStatus = document.getElementById("gwsAuthStatus");
const gwsAuthSpinner = document.getElementById("gwsAuthSpinner");
const gwsAuthStatusText = document.getElementById("gwsAuthStatusText");
const gwsAuthCancelBtn = document.getElementById("gwsAuthCancelBtn");

function openGwsAuthModal(consentUrl) {
  if (!gwsAuthModal) return;
  const instructions = gwsAuthModal.querySelector(".gws-auth-modal-instructions");
  if (instructions) {
    instructions.textContent = consentUrl
      ? "Scan this QR code with your phone camera or visit the companion link to approve access:"
      : "Sign in with your Google account or scan the passkey QR code on screen:";
  }
  if (gwsAuthQr) {
    if (consentUrl) {
      try {
        gwsAuthQr.innerHTML = generateQrSvg(consentUrl, { cellSize: 3, margin: 2 });
      } catch (err) {
        gwsAuthQr.textContent = "QR code generation failed";
      }
    } else {
      gwsAuthQr.replaceChildren();
    }
  }
  if (gwsAuthUrl) {
    if (consentUrl) {
      gwsAuthUrl.href = consentUrl;
      gwsAuthUrl.textContent = consentUrl.length > 50 ? consentUrl.slice(0, 50) + "…" : consentUrl;
      gwsAuthUrl.title = consentUrl;
    } else {
      gwsAuthUrl.removeAttribute("href");
      gwsAuthUrl.textContent = "";
    }
  }
  if (gwsAuthStatusText) {
    gwsAuthStatusText.textContent = consentUrl
      ? "Waiting for approval on your device..."
      : "Complete sign-in on the terminal screen or using your passkey...";
  }
  gwsAuthModal.classList.remove("hidden");
}

function closeGwsAuthModal(cancelled = false) {
  if (gwsAuthModal) {
    gwsAuthModal.classList.add("hidden");
  }
  if (cancelled) {
    if (gwsPollTimer) {
      clearInterval(gwsPollTimer);
      gwsPollTimer = null;
    }
    if (workspaceConnectBtn) {
      workspaceConnectBtn.disabled = false;
    }
    if (workspaceState) {
      workspaceState.textContent = "Google Workspace — Sign-in cancelled or timed out";
    }
    fetch("/api/gws/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).catch(() => {});
  }
}

async function connectGws() {
  if (!workspaceConnectBtn) return;
  workspaceConnectBtn.disabled = true;
  try {
    const isKioskMode = new URLSearchParams(window?.location?.search || "").get("kiosk") === "1";
    const res = await fetch("/api/gws/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kiosk: isKioskMode }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const consentUrl = data.url || data.consent_url;
    if (isKioskMode) {
      if (consentUrl) {
        openGwsAuthModal(null);
      }
    } else {
      if (consentUrl) {
        window.open(consentUrl, "_blank");
        openGwsAuthModal(consentUrl);
      }
    }
    clearInterval(gwsPollTimer);
    let attempts = 0;
    gwsPollTimer = setInterval(async () => {
      attempts++;
      const status = await refreshGwsStatus();
      if (status && status.connected) {
        clearInterval(gwsPollTimer);
        gwsPollTimer = null;
        closeGwsAuthModal(false);
        if (workspaceConnectBtn) workspaceConnectBtn.disabled = false;
      } else if (attempts >= 60) {
        clearInterval(gwsPollTimer);
        gwsPollTimer = null;
        closeGwsAuthModal(true);
      }
    }, 2000);
  } catch (err) {
    workspaceConnectBtn.disabled = false;
    closeGwsAuthModal(true);
    if (workspaceState) {
      workspaceState.textContent = "Google Workspace — Sign-in failed";
    }
  }
}

async function disconnectGws() {
  if (!workspaceDisconnectBtn) return;
  workspaceDisconnectBtn.disabled = true;
  clearInterval(gwsPollTimer);
  gwsPollTimer = null;
  closeGwsAuthModal(false);
  try {
    const res = await fetch("/api/gws/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    if (workspaceState) {
      workspaceState.textContent = "Google Workspace — Disconnect failed";
    }
  } finally {
    workspaceDisconnectBtn.disabled = false;
    await refreshGwsStatus();
  }
}

workspaceConnectBtn?.addEventListener("click", connectGws);
workspaceDisconnectBtn?.addEventListener("click", disconnectGws);
gwsAuthModalClose?.addEventListener("click", () => closeGwsAuthModal(true));
gwsAuthModalBackdrop?.addEventListener("click", () => closeGwsAuthModal(true));
gwsAuthCancelBtn?.addEventListener("click", () => closeGwsAuthModal(true));

/* ---------- audio ---------- */

function base64ToArrayBuffer(base64) {
  let std = base64.replace(/-/g, "+").replace(/_/g, "/");
  while (std.length % 4) std += "=";
  const bin = atob(std);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

async function ensurePlayer() {
  if (playerNode) {
    if (playerContext.state === "suspended") await playerContext.resume();
    return;
  }
  // The level the person set before there was anything to apply it to. It goes
  // in at construction rather than being ramped on afterwards, so the first
  // packet through the graph is already at the right volume — on a muted
  // terminal a ramp would let the front of the first word out.
  [playerNode, playerContext, playerGain] =
    await startAudioPlayerWorklet(targetGain());
}

async function startRecorder() {
  [recorderNode, recorderContext, micStream] = await startAudioRecorderWorklet(
    (pcm) => {
      if (listening && socket?.readyState === WebSocket.OPEN) socket.send(pcm);
    },
    micSelect.value || undefined,
  );
}

async function stopRecorder() {
  micStream?.getTracks().forEach((t) => t.stop());
  micStream = null;
  await recorderContext?.close();
  recorderNode = null;
  recorderContext = null;
}

/**
 * Open the microphone, arm a session under it, and light the button.
 *
 * Shared by the button and the wake word rather than written twice. The two
 * want exactly the same thing, and the wake path is the one with nobody
 * standing in front of it to notice the day the two drift apart.
 */
async function startListening() {
  if (feed.querySelector(".msg, .card")) hideStarters();
  try {
    await ensurePlayer();
    await startRecorder();
    // After the recorder exists and before a single block leaves: the server
    // drops audio that arrives with nothing armed, so arming later loses the
    // first words of the question. A microphone that refused to open this way
    // arms nothing at all, which is the point of doing it here and not first.
    armSession();
    listening = true;
    const icon = micButton.querySelector(".material-symbols-outlined");
    micButton.classList.add("on");
    if (icon) icon.textContent = "mic";
    const el = systemLine(t("ui_listening"), "listening");
    if (el) el.classList.add("listening");
    // Device labels are blank until permission is granted — refresh now.
    listMicrophones();
  } catch (err) {
    console.error(err);
    systemLine(t("ui_mic_failed"));
    await stopRecorder();
  }
}

/**
 * Close the microphone and put the button back.
 *
 * Says nothing on screen: the button and the idle timer have different things
 * to say about why it went off, and only they know which happened.
 */
async function stopListening() {
  listening = false;
  const icon = micButton.querySelector(".material-symbols-outlined");
  micButton.classList.remove("on");
  if (icon) icon.textContent = "mic";
  feed.querySelectorAll(".msg.listening").forEach(el => el.remove());
  await stopRecorder();
}

micButton.addEventListener("click", async () => {
  if (listening) {
    await stopListening();
    systemLine(t("ui_mic_off"));
    return;
  }
  await startListening();
});

/* ---------- the wake word, and the idle timer that undoes it ---------- */

/**
 * Open a Live session underneath the socket, and push the deadline out.
 *
 * Every path that puts something in front of the model comes through here.
 * The socket no longer implies a session — the server starts `run_live` on
 * this frame and on nothing else — so the socket being open says nothing
 * about whether anyone is listening on the other end of it.
 */
function armSession() {
  if (socket?.readyState !== WebSocket.OPEN) return;
  if (!armed) {
    socket.send(JSON.stringify({ type: "arm" }));
    armed = true;
  }
  restartIdle();
}

/**
 * Start the countdown to disarming over.
 *
 * Called on anything that says a person is still here: every frame the server
 * sends, which covers the model talking and the transcript of what was said
 * to it, and everything this page sends of its own accord.
 *
 * Deliberately not called per block of captured audio. The recorder posts one
 * every 128 frames at 16 kHz whether the room is talking or silent, so a
 * countdown reset by those would never reach zero and the microphone would
 * stay open until the kiosk was rebooted.
 */
function restartIdle() {
  clearTimeout(idleTimer);
  idleTimer = armed ? setTimeout(goIdle, wakeIdleMs) : null;
}

/**
 * Nobody has said anything for `WAKE_IDLE_S`: microphone off, session closed.
 *
 * Both, and never one without the other. A microphone left on over a closed
 * session streams the kitchen into nothing; a session left open under a
 * closed microphone holds a model connection for a room that has gone quiet.
 * The socket itself stays up through both, because it is what the next wake
 * word arrives on.
 */
async function goIdle() {
  clearTimeout(idleTimer);
  idleTimer = null;
  const spinning = [...pending.values()].some((entry) => !entry.lost);
  const activeBrowsing = [...browsing.values()].some((entry) => !entry.lost);
  if (spinning || activeBrowsing) {
    restartIdle();
    return;
  }
  if (listening) await stopListening();
  if (armed && socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "disarm" }));
  }
  armed = false;
  systemLine(t("ui_wake_again"), "wake-prompt");
  // And the suggestions, which nothing but the clear button used to put back.
  // A panel that has timed out is otherwise one line of text on a dead screen,
  // and this is the one moment the page is certain nobody is mid-question.
  // Here rather than in `stopListening`, which the microphone button also
  // goes through: somebody who has just tapped the mic off has not asked for
  // a menu.
  showStarters();
}

/**
 * The wake word, heard by the monitor on the Pi and pushed here by
 * `/api/wake`.
 *
 * Ignored while the microphone is already on, and that is not tidiness. The
 * monitor listens to raw capture with no echo cancellation, so the terminal's
 * own speaker saying the phrase back is a detection like any other, and
 * `listening` is the only thing here that can tell that apart from a person.
 * Acting on it would restart the recorder in the middle of the sentence it is
 * carrying.
 */
async function handleWake() {
  if (listening) return;
  // Starters are hidden before the clock is removed, so suggestion chips do
  // not briefly flash while the recorder initializes (#472).
  if (feed.querySelector(".msg, .card")) hideStarters();
  // The clock first, and only once the detection has survived the guard
  // above. Nothing else on this path takes it away: `startTurn` and the
  // agent's own transcription both do, but neither runs until somebody has
  // finished a sentence, so everything `startListening` says in the meantime
  // — "Listening", or the line about a microphone that would not open — is
  // written under a full-screen clock. Ahead of the recorder rather than
  // behind it, because opening one takes a visible moment.
  wake();
  await startListening();
}

/**
 * Which microphone to select, given the devices that exist right now.
 *
 * Three chances, most trusted first. Whatever is already selected wins if it is
 * still plugged in — the list is rebuilt on every device change and re-picking
 * from storage mid-session would undo a choice made a moment ago. Then the
 * saved `deviceId`, which is the exact answer when it works: Chrome scopes the
 * id to the origin and rotates it whenever the permission is reset, so it
 * survives a reload and not much else. Then the label, which is stable across
 * all of that and only ambiguous between two identical devices.
 */
function pickMic(mics, current) {
  if (mics.some((m) => m.deviceId === current)) return current;
  const saved = settings.mic;
  if (!saved) return "";
  const byId = mics.find((m) => m.deviceId === saved.id);
  if (byId) return byId.deviceId;
  // Labels are empty until the microphone permission is granted, so an empty
  // saved label must not match the blank ones every device has right now.
  const byLabel = saved.label && mics.find((m) => m.label === saved.label);
  return byLabel ? byLabel.deviceId : "";
}

async function listMicrophones() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const previous = micSelect.value;
  const mics = (await navigator.mediaDevices.enumerateDevices()).filter(
    (d) => d.kind === "audioinput",
  );
  micSelect.replaceChildren(new Option(t("ui_mic_default"), ""));
  mics.forEach((mic, i) => {
    const opt = document.createElement("option");
    opt.value = mic.deviceId;
    opt.textContent = mic.label || t("ui_mic_n").replace("{n}", i + 1);
    micSelect.appendChild(opt);
  });

  micSelect.value = pickMic(mics, previous);
  // The first list, drawn before the permission prompt, has no labels and no
  // real ids, so the saved device cannot be found in it. The mic button grants
  // the permission and calls this again; that is the pass that restores the
  // choice, and by then the recorder is already running on the wrong device.
  if (listening && micSelect.value !== previous) {
    await stopRecorder();
    await startRecorder();
  }
}

micSelect.addEventListener("change", async () => {
  const mic = micSelect.selectedOptions[0];
  // The label goes in beside the id because the id will not last: it is
  // per-origin and Chrome rotates it whenever the mic permission is reset.
  saveSettings({
    mic: micSelect.value ? { id: micSelect.value, label: mic.textContent } : null,
  });
  if (!listening) return;
  await stopRecorder();
  await startRecorder();
});

navigator.mediaDevices?.addEventListener?.("devicechange", listMicrophones);

/* ---------- language ---------- */

// What each locale calls itself. Endonyms, not translations: a picker that
// says "Japanese" to an English reader and "英語" to a Japanese one is a
// picker you cannot use to get back out of the language you landed in by
// mistake, which on a kiosk with no keyboard is the whole risk.
const LOCALE_NAMES = { en: "English", ja: "日本語" };

/**
 * Put the terminal in a language, and redraw everything already written in
 * the last one.
 *
 * Not a page reload, which is the obvious way to do this: the panel is a
 * kiosk, and a reload throws away the conversation on screen to relabel a
 * settings sheet. So `<html lang>` goes first, because everything below reads
 * the document; then the markup, the picker's own selected option (which
 * matters on the first pass, where nothing is selected yet), the location line
 * that is rendered rather than keyed, and the two clocks — the face and the
 * top bar — whose weekday and month come out of `Intl` with the same tag.
 *
 * What it cannot redo is the cards already drawn and the Maps JS script tag,
 * which reads its `language` once per page load. Both keep the language they
 * were made in until something replaces them, and the note the change handler
 * prints says so out loud rather than leaving it to be noticed.
 */
function applyLanguage(locale) {
  setLocale(locale);
  applyStatic();
  // After `applyStatic` for the same reason the pill is: it has just written
  // every chip's plain label and plain prompt from the markup's keys, over the
  // contextual ones the conversation earned.
  renderStarters();
  // After `applyStatic`, which has just written the markup's "Connecting"
  // over whatever the socket last said. The pill is live state wearing a
  // static string's clothes: it is the one `data-i18n` element on the panel
  // whose text somebody else owns after the first paint.
  if (connected !== null) setConnected(connected);
  langSelect.value = locale;
  showWhere();
  showVolume();
  // The device list is built rather than keyed: `listMicrophones` replaces
  // the markup's `data-i18n` option with a plain `new Option`, so
  // `applyStatic` cannot reach it and only a redraw relabels it.
  listMicrophones();
  drawClock();
  drawBar();
}

/** Fill the picker with the locales `i18n.js` has a block for. */
function showLanguages(locale) {
  langSelect.replaceChildren(
    ...LOCALES.map((code) => new Option(LOCALE_NAMES[code] || code, code,
                                        false, code === locale)),
  );
}

langSelect.addEventListener("change", async () => {
  const next = pickLocale(langSelect.value);
  // A `change` fires when the value is put back to what it already was, and
  // the reopen below costs a reconnect — the same guard the voice picker has,
  // for the same reason. The document's tag is where the current one lives:
  // `setLocale` writes it and `t` reads it.
  if (next === document.documentElement.lang) return;
  saveSettings({ lang: next });
  applyLanguage(next);
  // A new socket, because the tools' language floor rides the handshake and
  // nothing else on this connection can move it — the same reason the voice
  // reopens. Without it the sheet relabels itself and the next place card
  // still comes back in the old language, which is the whole complaint.
  //
  // The conversation survives: the server resumes the same session and
  // rewrites the handshake state as a delta, exactly as the location picker
  // already relies on. What does not survive is work in flight — a running
  // `browse`, `gws` or `plan` dies with the connection, as it does for the
  // other two pickers.
  //
  // `reopen` prints the note itself, so the feed still says what happened.
  //
  // The pin's label goes first, because it rides that handshake: relabel after
  // the reconnect and the terminal spends the rest of the session — and the
  // clock face until the next tick — with the address it had before the
  // setting moved. `applyLanguage` has already written the new tag on the
  // document, which is what `relabelHome` reads.
  await relabelHome();
  reopen(t("ui_lang_note"));
});

/* ---------- voice ---------- */

/**
 * Fill the voice picker from what the server serves, and settle which one is on.
 *
 * The list is the server's, not the page's: `main.py` refuses a voice outside
 * `VOICES` and falls back to its own, so an option written into the markup
 * that the server has since dropped is a picker offering a setting the socket
 * quietly ignores — the terminal answers in the default and the sheet says it
 * is answering in something else.
 *
 * That is also why a saved voice that is no longer served snaps to the
 * server's default rather than staying selected: the socket is about to be
 * given the same treatment, and the sheet should say what the terminal will
 * actually do. It is not written back to storage, so a name that comes back in
 * a later release is picked up again instead of having been overwritten here.
 *
 * A server that answers without the field leaves the picker empty and `voice`
 * alone, which connects exactly as this build did before there was a picker.
 */
function showVoices(names, fallback) {
  const listed = (Array.isArray(names) ? names : []).filter(
    (name) => typeof name === "string" && name,
  );
  if (!listed.length) return;
  if (!listed.includes(voice)) {
    voice = listed.includes(fallback) ? fallback : listed[0];
  }
  voiceSelect.replaceChildren(
    ...listed.map((name) => new Option(name, name, false, name === voice)),
  );
}

voiceSelect.addEventListener("change", () => {
  const next = voiceSelect.value;
  // A `change` fires when the value is set back to what it was, and this one
  // costs a reconnect.
  if (!next || next === voice) return;
  voice = next;
  saveSettings({ voice });
  // The profile picker clears the screen before it reopens; this must not.
  // That one is somebody else's thread, and this is the same person on the
  // same one — the server puts the new socket straight back on it, so what is
  // on screen is still theirs and still what the next answer follows on from.
  reopen(t("ui_voice_set").replace("{voice}", voice));
});

if (interruptCheck) {
  interruptCheck.checked = Boolean(settings.interrupt);
  interruptCheck.addEventListener("change", () => {
    interrupt = interruptCheck.checked;
    saveSettings({ interrupt });
    reopen(t(interrupt ? "ui_interrupt_note_on" : "ui_interrupt_note_off"));
  });
}

/* ---------- the speaker ---------- */

/**
 * How loud, from a slider that runs 0 to 100.
 *
 * Squared, not straight through. The ear reads loudness as roughly a power of
 * amplitude, so a slider wired to `gain.value` directly spends its top half in
 * a range nobody can pick apart and puts everything audible in the bottom
 * few pixels: 100 and 70 sound nearly alike, 10 and 5 do not. The square is
 * the usual audio taper and it costs a line. The midpoint lands on 0.25, which
 * is -12 dB and reads as somewhat less than half as loud — halving loudness is
 * nearer -10 dB — and the difference between 20 and 30 is a difference you can
 * hear rather than the same volume twice.
 *
 * Only the two ends are exact, and they are the two that have to be: 100 is
 * the signal untouched, so a terminal nobody has ever opened this on sounds
 * exactly as it did before any of it existed, and 0 is silence rather than
 * something very quiet.
 */
function gainFor(level) {
  const clamped = Math.min(100, Math.max(0, Number(level)));
  // A `gain.value` of NaN throws, and this reads off a slider and off
  // localStorage. Anything unreadable leaves the terminal audible, which is
  // the safe direction of the two.
  return Number.isFinite(clamped) ? (clamped / 100) ** 2 : 1;
}

/**
 * The level as it was left, and full volume for anything else.
 *
 * Validated rather than trusted, the same as `savedHome`: it comes out of
 * localStorage, it ends up in an AudioParam, and a string or a NaN there is a
 * terminal that has stopped talking with nothing on screen to say why.
 */
function savedLevel() {
  const level = settings.volume;
  if (!Number.isFinite(level) || level < 0 || level > 100) return 100;
  return Math.round(level);
}

// Both remembered, and remembered separately: coming off mute has to land back
// on the level it was muted at, not on a default.
//
// Mute survives a reload on purpose, and it is the one setting here where that
// is arguable. The case for it: the kiosk unit is `Restart=always`, so
// Chromium comes back on its own, and a terminal that un-muted itself
// overnight would be overriding somebody who deliberately silenced it at
// eleven. The case against is a panel that boots quiet for no visible reason,
// and the answer to that is the toolbar glyph — which is why the glyph is in
// the bar rather than in the settings sheet.
//
// `=== true` rather than truthiness, so a half-written key reads as unmuted.
let volumeLevel = savedLevel();
let muted = settings.muted === true;

// The GainNode `ensurePlayer` builds, or null for as long as nobody has spoken
// and nobody has typed. See `applyVolume`.
let playerGain = null;

// How long the gain takes to reach a level it has just been moved to.
// Assigning `gain.value` mid-waveform is a step discontinuity, and a step in a
// 24 kHz stream is an audible click — on every frame of a slider drag, which
// is dozens of them. `setTargetAtTime` is an exponential approach instead: it
// never formally arrives, but at this time constant it is within 0.7% of the
// target after 100 ms, which against a muted target is -43 dB and is not a
// thing anybody hears in a kitchen. 20 ms is shorter than a syllable and far
// longer than the handful of samples it takes to stop clicking.
const VOLUME_RAMP_S = 0.02;

/** What the graph should be doing right now: the level, or nothing. */
function targetGain() {
  return muted ? 0 : gainFor(volumeLevel);
}

/**
 * Put the level on the graph, if there is a graph to put it on.
 *
 * The toolbar paints on the first frame and the player is not built until
 * somebody speaks, types or taps a starter — so for the first part of the
 * terminal's life there is nothing here to set, and a control that threw then
 * would be a control that breaks the page before the page has done anything.
 * Nothing is lost by the no-op: `volumeLevel` and `muted` are where the truth
 * lives and this is a projection of them, so `ensurePlayer` reads the same two
 * back and builds the node with the answer already in it.
 */
function applyVolume() {
  if (!playerGain) return;
  playerGain.gain.setTargetAtTime(
    targetGain(), playerGain.context.currentTime, VOLUME_RAMP_S);
}

/**
 * Draw the state on the two buttons and the slider.
 *
 * The toolbar glyph is the whole of the answer to a terminal that hears a
 * question, scrolls a transcript, draws a card and says nothing: muted is a
 * `volume_off` in the bar at full brightness against a row of dimmed icons.
 * It answers that question for somebody standing at the panel and not for
 * somebody across the room — 22px of glyph is under 5 arcmin at three metres
 * — which is why the feed gets a line about it as well.
 * A slider dragged to 0 shows the same glyph, because it is the same outcome
 * for anybody looking at the screen — the mute button inside the popup is
 * where the two states are told apart, and that is a thing you have to open
 * the popup to care about.
 */
function showVolume() {
  const silent = muted || volumeLevel === 0;
  volumeIcon.textContent =
    silent ? "volume_off" : volumeLevel < 50 ? "volume_down" : "volume_up";
  muteIcon.textContent = muted ? "volume_off" : "volume_up";
  volumeButton.classList.toggle("muted", silent);
  muteButton.classList.toggle("muted", muted);
  volumeButton.title = muted
    ? t("ui_speaker_muted")
    : t("ui_speaker_volume_at").replace("{level}", volumeLevel);
  muteButton.title = t(muted ? "ui_unmute" : "ui_mute");
  volumeRange.value = String(volumeLevel);
  volumeRead.textContent = String(volumeLevel);
}

function openVolume(open) {
  volumePop.classList.toggle("hidden", !open);
  volumeButton.setAttribute("aria-expanded", String(open));
}

volumeButton.addEventListener("click", () => {
  openVolume(volumePop.classList.contains("hidden"));
});

// Heard on the way past rather than written down on the way out. A drag fires
// one of these a pixel and each one would be a `JSON.stringify` and a
// localStorage write; `change` fires once, when the finger comes off.
volumeRange.addEventListener("input", () => {
  volumeLevel = Number(volumeRange.value);
  // Moving the slider is asking to hear it. Leaving mute on would move a
  // number nothing is listening to and leave the person dragging a dead
  // control, which is worse than the mute they have plainly changed their
  // mind about.
  muted = false;
  applyVolume();
  showVolume();
});

volumeRange.addEventListener("change", () => {
  saveSettings({ volume: volumeLevel, muted });
});

/**
 * Mute, which is the speaker and not the microphone.
 *
 * The recorder, `listening` and the socket are all untouched: a muted terminal
 * still hears the room, still answers, and still draws the answer. What it
 * does not do is say it out loud. That is the behaviour the owner asked for
 * and it is also the one that can be mistaken for a broken box, so it is said
 * twice — once in the bar, where it stays, and once in the feed, where the
 * mic button already says the same kind of thing.
 *
 * Muting is the gain going to zero, not the node being disconnected and not
 * the player being stopped, and the three are different in the case that
 * matters: somebody hits mute in the middle of a sentence. A disconnected node
 * is no longer pulled by the destination, so the worklet stops draining while
 * the socket keeps filling its 180-second ring, and unmuting empties the whole
 * backlog at once. Stopping the player means `endOfAudio`, which throws the
 * queue away and loses the rest of an answer that was still coming. Zero gain
 * keeps the worklet running in real time: the sentence carries on being
 * consumed and thrown away, and unmuting halfway lands you where the terminal
 * actually is rather than where it was when you muted it.
 */
muteButton.addEventListener("click", () => {
  muted = !muted;
  saveSettings({ muted });
  applyVolume();
  showVolume();
  systemLine(t(muted ? "ui_speaker_muted_line" : "ui_speaker_on"));
});

// Four ways out of the popup, because the panel has no window manager and no
// keyboard: the icon again, a tap anywhere else, a tap inside that is not one
// of them, and Escape for the Mac this is developed against.
//
// Neither of these captures and neither stops propagation. The idle timer's
// own `pointerdown` and `keydown` are bound capturing on the document (see the
// clock section) precisely so a handler down here cannot cost the terminal its
// countdown, and these are the handlers that would: opening the volume popup
// is somebody standing at the panel, and it wakes the screen and restarts the
// three minutes exactly as any other tap does.
document.addEventListener("pointerdown", (e) => {
  if (volumePop.classList.contains("hidden")) return;
  // The tap that lands on the button itself gets left alone, or it would close
  // the popup here and the `click` behind it would reopen it — a control that
  // cannot be shut.
  if (volumePop.contains(e.target) || volumeButton.contains(e.target)) return;
  openVolume(false);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") openVolume(false);
});

// At parse rather than in `init`, which is behind a `fetch`. A terminal that
// was muted must not spend a round trip looking like one that is working.
showVolume();

/* ---------- profiles ---------- */

/**
 * Fill the picker with the household, and show it if there is a choice to make.
 *
 * Whoever is on the terminal goes in the list whether the server named them or
 * not. A name dropped from `PROFILES` would otherwise leave the select showing
 * somebody else while the socket is still on this person's thread — the one
 * thing a picker must never do is say the wrong name.
 */
function showProfiles(names) {
  const listed = Array.isArray(names) ? names : [];
  const all = listed.includes(userId) ? listed : [userId, ...listed];
  if (all.length < 2) return;
  profileSelect.replaceChildren(
    ...all.map((name) => new Option(name, name, false, name === userId)),
  );
  profileSelect.classList.remove("hidden");
  profileLabel.classList.remove("hidden");
}

profileSelect.addEventListener("change", () => {
  const next = profileSelect.value;
  if (!next || next === userId) return;
  userId = next;
  saveSettings({ profile: userId });
  // Before anything else, because `forget` reads `userId` at the moment of the
  // second tap and the line above has just changed it. An arming is a
  // confirmation of one specific delete, and the thing it confirmed is gone:
  // left standing, the next tap deletes the history of somebody nobody was
  // asked about, and the button is still red and still says "Tap again to
  // forget" over a feed that now says "Now talking to jordan."
  disarmForget();
  // Everything on screen belongs to the person who was here a moment ago, and
  // the follow-up they were in the middle of must stop making sense. Through
  // the button rather than a function lifted out of it: `js_check` reads the
  // clear handler and `reopen` as literal blocks, and pulling the lines they
  // share into one would move them out of both.
  clearButton.click();
  reopen(t("ui_now_talking").replace("{who}", userId));
});

/* ---------- location ---------- */

/**
 * The saved location, if there is one worth having.
 *
 * Validated rather than trusted: this comes out of localStorage, it is put
 * straight into a WebSocket query string, and a NaN latitude would reach the
 * weather tool as a forecast for nowhere. Anything that fails is treated as
 * unset, which falls back to `HOME_LOCATION` on the server.
 */
function savedHome() {
  const saved = settings.home;
  if (!saved || typeof saved !== "object") return null;
  const { lat, lng } = saved;
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  if (Math.abs(lat) > 90 || Math.abs(lng) > 180) return null;
  // `lang` is which language the label was written down in, not a setting.
  // Absent for a pin saved before `relabelHome` existed, which is the case
  // that has to relabel itself rather than be left alone.
  return {
    lat, lng,
    label: String(saved.label || ""),
    lang: String(saved.lang || ""),
  };
}

/**
 * Write the saved pin's label in the language the panel is set to.
 *
 * The label is not a setting somebody typed; it is the geocoder's answer for a
 * position, and it goes stale the moment the language changes under it. Four
 * things read it — the settings sheet, the agent's system instruction, the
 * handshake, and the place at the end of the line under the clock — so a label
 * left in the old language is wrong in four places at once, which is what #131
 * fixed for new pins and left standing for the one already saved.
 *
 * Awaited in `init` before `connect`, so the first handshake of the session
 * carries the good label rather than moving the clock a second time. That
 * costs a geocode round trip before the socket opens, and only on the boot
 * after the language changed: the tag is saved beside the label, so the
 * ordinary boot compares two strings and returns.
 *
 * Silent about failure, like everything else on this path. No Maps, no
 * network, no answer — the old label stays and no tag is written, so the next
 * boot tries again.
 */
async function relabelHome() {
  if (!home || !window.google?.maps?.Geocoder) return;
  const lang = document.documentElement.lang || "en";
  if (home.lang === lang) return;
  const label = await reverseGeocode(home.lat, home.lng);
  if (!label) return;
  home = { ...home, label, lang };
  saveSettings({ home });
  showWhere();
}

function showWhere() {
  whereText.textContent = home
    ? home.label || `${home.lat.toFixed(5)}, ${home.lng.toFixed(5)}`
    : t("ui_where_unset");
}

/**
 * Open a new socket on whatever the settings sheet has just changed, and say so.
 *
 * Two settings need this and neither can be sent down a socket that is already
 * up: the Live API takes its system instruction once, at setup, and the profile
 * is a path segment. A new location no longer costs the conversation — the
 * server writes the handshake state on every connect, so the address arrives on
 * the thread that was already going, where it used to take a fresh session id
 * to force that write and the conversation went with it. A new profile is meant
 * to cost it: that is somebody else's thread.
 */
function reopen(note) {
  if (socket) {
    // Drop both handlers first. The close handler's reconnect would race this
    // one and leave two sockets on the same session. The message handler is
    // the same story a frame later: `close()` starts the handshake and cancels
    // nothing already queued, so a browse frame that was on the wire arrives
    // after the cleanup below, finds its id gone from `browsing`, and builds a
    // second card for the job — one nothing left is able to finish.
    //
    // `connect()` at the end of this function assigns a new socket to the same
    // variable, so nulling these leaves the next one untouched. The reconnect
    // path in `connect` needs no such teardown: it fires from `onclose`, by
    // which time the socket is CLOSED and delivers nothing more.
    socket.onclose = null;
    socket.onmessage = null;
    socket.close();
  }
  setConnected(false);
  // Ended by hand, for the same reason as the pending cards below and with
  // less to fall back on: a browse card has no deadline, only the `done` frame
  // the socket that just closed would have carried, so left alone it spins
  // until the page is reloaded.
  for (const card of browsing.values()) {
    if (card.timer) {
      clearTimeout(card.timer);
      card.timer = null;
    }
    browseUpdate(card, { phase: "done", lost: true });
  }
  browsing = new Map();
  if (typeof compositeCards !== "undefined") compositeCards = new Map();
  if (typeof activeCompositeCard !== "undefined") activeCompositeCard = null;
  // Resolved, because the cards stay. Whatever was in flight belonged to the
  // connection just closed, and the server cancels a departing connection's own
  // work, so nothing left is able to fill them.
  dropPending(true);
  seenTools = new Set(); userCleared = false;
  // Somebody asked for this connection, so it starts from the floor. The close
  // handler is the only other place the backoff moves, and the socket being
  // replaced never reaches it — its `onclose` was nulled above — so without
  // this line the attempt below inherits whatever the last outage left: press
  // the button after an hour of no server and the one retry that follows is a
  // minute away, on a screen somebody is standing in front of.
  //
  // The stamp goes with it. It belongs to the socket just torn down, and left
  // behind it tells the next close that a connection which never opened held
  // for an hour — the reset `RECONNECT_HELD_MS` was added to refuse.
  reconnectDelay = RECONNECT_MIN_MS;
  openedAt = 0;
  connect();
  systemLine(note);
}

whereButton.addEventListener("click", async () => {
  if (!window.google?.maps?.Map) {
    settingsSheet.classList.add("hidden");
    systemLine(t("ui_no_map"));
    return;
  }
  const picked = await pickLocation(home);
  if (!picked) return;
  // The tag the picker's own reverse-geocode ran under, saved with the label
  // so `relabelHome` knows it does not have to ask again on the next boot.
  home = { ...picked, lang: document.documentElement.lang || "en" };
  saveSettings({ home });
  showWhere();
  reopen(t("ui_where_set").replace("{label}", home.label));
});

/* ---------- the clock ---------- */

/**
 * How long the room can go quiet before the screen goes back to being a clock.
 *
 * Three minutes. Long enough to read a recipe off the panel without it going
 * away under you, short enough that a conversation that ended is not still
 * lighting the room at midnight.
 *
 * It used to answer a different question from `SESSION_IDLE_GAP_S`, and the
 * comment here said so at length. It answers both now: when the face comes
 * back up, the conversation under it is over. `sleep` tells the server, and
 * the next wake word starts a new thread instead of replaying the day into a
 * fresh Live connection. Half an hour was never going to bound that, because
 * a kitchen in use is never quiet for half an hour — a measured day ran
 * 10 h 43 m and 636 events without ever crossing it. #142.
 *
 * One direction only. The half hour is still the ceiling for a page that
 * never slept, and it is still what decides which conversation a page load
 * belongs to; this decides when the one on screen has finished. What it costs
 * was taken deliberately: ask about the weather, walk away for four minutes,
 * come back and say "and tomorrow?" and the terminal will not know what
 * "tomorrow" attaches to. Anything worth remembering across that boundary
 * wants a memory service (#151) rather than a bigger number here.
 */
const IDLE_MS = 180000;

/**
 * How often the bar asks the server what the sky is doing.
 *
 * Ten minutes, which is `screensaver.REFRESH_S` (`screensaver.py:74`) and
 * deliberately not finer: `/api/screensaver` serves whatever the background
 * task last stored, so asking twice as often returns the same dict twice.
 *
 * This is not a second poller against Open-Meteo. #85 owns the only one of
 * those there is, in the server, and everything here reads its cache — which
 * is the whole reason the toolbar is allowed to show a temperature at all.
 */
const WX_REFRESH_MS = 600000;

/**
 * How long a reading may stand before the bar stops vouching for it.
 *
 * Twenty-five minutes: two refreshes missed plus a minute of slack. One
 * missed refresh is a Pi on Wi-Fi and says nothing about the reading; two in
 * a row is a WAN that has gone, and from there the number on the screen is
 * this morning's. It is still shown — an hour-old temperature beats a blank
 * for the same reason the server keeps one (`screensaver.py:601-605`) — but
 * dimmed, so the person deciding whether to take a coat can see the terminal
 * is not standing behind it.
 */
const WX_STALE_MS = 1500000;

// The clock is what the page opens on, so `asleep` starts false and `init`
// calls `sleep()` — one entry point into the state rather than two, and the
// markup already has the face on screen so nothing flashes on the way.
//
// `clockIdleTimer` and not `idleTimer`: the microphone keeps its own countdown
// under that name at the top of this file, and two module-level `let`s of one
// name is a `SyntaxError` that stops the whole file loading. The two are not
// the same clock and must not be merged — this one is 180 s to a screensaver,
// that one is `WAKE_IDLE_S` to a closed microphone — so they need two names.
let asleep = false;
let clockIdleTimer = null;
let clockTimer = null;

// The last payload that had a temperature in it, already formatted, and the
// moment it landed. Kept rather than read back off the element, because going
// stale changes how the same two strings are drawn and not what they say.
let wxSeen = null;
let wxAt = 0;
// When the server was last *asked*, answer or not. A refusal counts: keyed on
// the last good reading instead, a server that is down would be asked once a
// minute for as long as it stayed down.
let wxAsked = null;

/** Milliseconds from `now` to the start of the next minute. */
function untilNextMinute(now) {
  return 60000 - (now % 60000);
}

/**
 * How both clocks on this panel print an hour and a minute.
 *
 * `hourCycle: "h23"` rather than `hour12: false`, which is not the same thing:
 * with `hour12: false` the hour after midnight prints as 24 in several
 * locales, and a kitchen clock reading 24:10 is the sort of thing somebody
 * photographs. One object rather than the same three keys written twice,
 * because the second copy is where the next person puts `hour12` back.
 */
const HHMM = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };

function hhmm(now, lang) {
  return new Intl.DateTimeFormat(lang, HHMM).format(now);
}

/** The two lines of the face, in the language the panel is set to. */
function clockFace(now, lang) {
  return {
    time: hhmm(now, lang),
    date: new Intl.DateTimeFormat(lang, {
      weekday: "long", month: "long", day: "numeric",
    }).format(now),
  };
}

/**
 * The same two things at toolbar size, which is a different two things.
 *
 * `weekday: "short"` and `month: "short"` rather than the face's `long`, and
 * that is the whole difference: the bar is 560 px wide in portrait, because
 * the body is capped there (`style.css:44`), and it already carries a brand,
 * two 36 px buttons and a status pill. "Saturday 14 March" does not fit
 * beside those; "Sat, Mar 14" does, and Japanese comes out `3月14日(土)`,
 * which is how a Japanese calendar writes it anyway.
 *
 * Split from `clockFace` rather than given a width argument, because a
 * boolean parameter at two call sites is a thing to look up and two named
 * functions are not. What they must share is the time, and they do.
 */
function barFace(now, lang) {
  return {
    time: hhmm(now, lang),
    date: new Intl.DateTimeFormat(lang, {
      weekday: "short", month: "short", day: "numeric",
    }).format(now),
  };
}

function drawClock(now = new Date()) {
  // The document tag, and nothing behind it: `init` writes one before the
  // first face is drawn and `index.html` ships with `lang="en"` on it, so
  // there is no state in which this is empty. It used to fall through to
  // `navigator.language`, which was already unreachable and is now the wrong
  // answer as well — the kiosk's locale is the *default* for the setting, not
  // a second opinion about it.
  const { time, date } = clockFace(now, document.documentElement.lang || "en");
  clockTime.textContent = time;
  clockDate.textContent = date;
}

/**
 * The same, in the top bar, which is the one that is up while somebody is
 * talking to the terminal.
 *
 * Reads the document tag for the reason `drawClock` does and off the same
 * line: the language is a setting now (#96), `applyLanguage` writes the tag
 * and then calls both of these, so a household switching to Japanese gets a
 * Japanese weekday in the bar on the same frame as everywhere else rather
 * than at the top of the next minute.
 */
function drawBar(now = new Date()) {
  const { time, date } = barFace(now, document.documentElement.lang || "en");
  barTime.textContent = time;
  barDate.textContent = date;
}

/**
 * Redraw, then sleep until the digits are due to change.
 *
 * Aligned to the minute rather than a one-second interval: the face has no
 * seconds on it, so an interval would wake the Pi sixty times for every time
 * the screen changes, all night, to draw the same two digits. Re-aimed from
 * the clock on every tick rather than adding 60000, so a drifting timer and a
 * suspend both come back on the minute instead of somewhere inside it.
 */
function tickClock() {
  drawClock();
  drawBar();
  // Re-drawn on the minute as well as when a reading lands, because going
  // stale is something a reading does by sitting still: nothing arrives at
  // 25 minutes to mark it, so the mark has to be re-decided by the clock.
  drawBarWeather();
  // And the ask, on the same timer rather than one of its own. A second
  // interval would be a second thing in every `timers` count and a second
  // thing to cancel, for a fetch that happens six times an hour.
  //
  // Asleep as well as awake, and that is the half #86 needs. What #85
  // settled was the poll against Open-Meteo, and this is not one:
  // `/api/screensaver` answers out of the background task's cache
  // (`main.py:560-565`), which goes on refreshing on its own timer
  // whether or not a page is asking. So an ask here is a loopback GET of a
  // dict the server already holds, and gating it cost the one screen that
  // is up for the eight hours nobody walks in — the face wears a
  // temperature the server replaces every ten minutes and a photograph it
  // replaces seven times a local day, and behind `!asleep` it wore neither
  // until somebody touched the panel.
  if (dueForWeather(Date.now())) loadScreensaver();
  clockTimer = setTimeout(tickClock, untilNextMinute(Date.now()));
}

/**
 * Put one screensaver payload on the face.
 *
 * Split out from the fetch because it is the half worth checking: the endpoint
 * is allowed not to exist, and everything about what a missing field does to
 * the face is decided here.
 *
 * The `src` is only assigned when it differs. Writing the same URL back
 * restarts the load, and on the Pi that is a visible blink of the gradient
 * every time the clock comes up.
 */
function paintScreensaver(shown) {
  if (shown?.image && clockImage.getAttribute("src") !== shown.image) {
    clockImage.src = shown.image;
  }
  const weather = shown?.weather;
  // Temperature, condition, and where the photograph is of — which is the
  // question anyone looking at it has. Joined by filtering first, so a payload
  // missing any one of the three does not leave a dangling separator.
  const line = [
    Number.isFinite(weather?.temperature)
      ? `${Math.round(weather.temperature)}${weather.unit || "°"}` : "",
    weather?.description || "",
    shown?.place || "",
  ].filter(Boolean).join(" · ");
  // Only a reading may replace a reading — `paintBarWeather`'s rule, which the
  // face needs too now that the tick asks while asleep. A server restarted at
  // 03:00 serves `{}` until its first Open-Meteo answer, and nobody is standing
  // there to see the line go empty and come back.
  if (line !== "" || clockWeather.textContent === "") {
    clockWeather.textContent = line;
  }
  // One payload, one paint, two places it lands. The bar and the face are on
  // the same screen a second apart — the face is what the bar becomes after
  // three minutes — and a second fetch behind the bar is how they would come
  // to disagree about the temperature.
  paintBarWeather(weather);
}

/**
 * Put the sky in the top bar: a glyph and a number, and nothing else.
 *
 * No condition word, and that is a saving of space rather than the locale
 * answer it used to be. `description` arrives in the panel's own language
 * since #131, so `Overcast` over a Japanese panel is no longer what a word
 * here would cost; what it would cost is the width, in a bar that is 560 px
 * wide and already carries a brand, two buttons and a status pill. The glyph
 * comes off `condition`, the slug the weather card is already keyed on, and
 * a glyph reads the same in both. The temperature is digits and a unit, which
 * are the same in both as well.
 *
 * A payload with no reading in it changes nothing, which is the rule the
 * server keeps on its own side (`screensaver.py:601-605`): only a reading may
 * replace a reading. So a refresh that came back empty leaves the last
 * temperature up and lets it go stale on its own, rather than blanking a bar
 * somebody is reading.
 */
function paintBarWeather(weather) {
  if (!Number.isFinite(weather?.temperature)) return;
  wxSeen = {
    temp: `${Math.round(weather.temperature)}${weather.unit || "°"}`,
    // No glyph at all rather than the `cloudy` the card falls back to: a
    // payload with a temperature and no sky in it is an older server, and a
    // cloud drawn over a clear afternoon is a wrong answer where a missing
    // one is only a missing one.
    icon: weather.condition
      ? wxIcon(weather.condition, weather.is_day !== false) : "",
  };
  const fetched = weather?.fetched ?? weather?.weather?.fetched;
  wxAt = (fetched ? fetched * 1000 : Date.now());
  drawBarWeather();
}

/** Draw whatever reading there is, and say how much it is still worth. */
function drawBarWeather(now = Date.now()) {
  barWx.classList.toggle("hidden", wxSeen === null);
  if (wxSeen === null) return;
  barWxIcon.textContent = wxSeen.icon;
  barWxIcon.classList.toggle("hidden", wxSeen.icon === "");
  barWxTemp.textContent = wxSeen.temp;
  barWx.classList.toggle("stale", now - wxAt >= WX_STALE_MS);
}

/** Whether the server is due to be asked again. */
function dueForWeather(now) {
  return wxAsked === null || now - wxAsked >= WX_REFRESH_MS;
}

/**
 * Ask the server what the clock should be wearing.
 *
 * Every failure is the same failure and none of them is worth saying out loud:
 * no endpoint on an older server, no image generated yet, no WAN at 3 a.m. The
 * face keeps the last thing it had, over the gradient if it never had one, and
 * that is the whole of what a clock owes anybody.
 *
 * `lang` is the one thing the page has to say out loud. The server holds one
 * reading for every panel and cannot guess which language any of them is set
 * to — the setting is in this page's localStorage — so the word for the sky
 * is chosen per request and this is the request (#131). Off `<html lang>`,
 * which is where `applyLanguage` puts it and where the two clock faces and
 * every card heading already read it from; a server that does not know the
 * parameter ignores it and answers in English, as it did before.
 */
async function loadScreensaver() {
  wxAsked = Date.now();
  try {
    const lang = document.documentElement.lang || "en";
    const res = await fetch(`/api/screensaver?lang=${encodeURIComponent(lang)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    paintScreensaver(await res.json());
  } catch {
    // Deliberately silent. See above.
  }
}

/** Show the clock and stop counting down to it, unless somebody is waiting. */
function sleep() {
  if (asleep) return;
  // A card still spinning is somebody's question, not an empty room. Still
  // *spinning*, not still in `pending`: an entry whose deadline has fired
  // stays in the map on purpose, so that a late answer has a card to land in,
  // and a card that has given up and says so is nobody waiting. Counting
  // those held the clock off for the rest of the session — the server going
  // away leaves an entry no `pending.delete` ever reaches, and every 180 s
  // after that `sleep()` saw a size of 1 and re-armed instead of sleeping.
  const spinning = [...pending.values()].some((entry) => !entry.lost);
  const activeBrowsing = [...browsing.values()].some((entry) => !entry.lost);
  // A microphone open over a session that is armed is the third of those, and
  // the least ambiguous of the three: that pair is exactly the state in which
  // the room is reaching the model, so somebody is mid-sentence or thinking
  // between two of them. At the default `WAKE_IDLE_S` it never decides
  // anything — the session's own 30 s countdown closes the microphone long
  // before the 180 s here, and `sleep` finds it already shut — but the two
  // numbers are independent and nothing makes the clock's the shorter. Raised
  // past 180 the orders swap and the face lands on a live conversation, where
  // the wake word cannot take it away again: `handleWake` declines while
  // `listening`, correctly, because the monitor hears the terminal's own
  // speaker with no echo cancellation. That leaves a tap, from the person who
  // has just been talking to a clock.
  //
  // Both flags and not `listening` alone. `armed` is what bounds this: the
  // deadline `goIdle` runs on is only scheduled while it is set, so a
  // microphone opened over a socket that is down — `startListening` sets
  // `listening` whether or not `armSession` could send anything — has no
  // countdown behind it and would hold the clock off until the server came
  // back. Audio captured in that state is sent nowhere anyway; the recorder's
  // own gate is the socket, and the server drops audio with nothing armed.
  //
  // `wake()` rather than a bare `return`, like the branch it joins: `sleep` is
  // reached from the countdown that has just fired, so returning schedules
  // nothing and the clock never comes back. This puts it back, and the page is
  // asked again in another `IDLE_MS`.
  if ((listening && armed) || spinning || activeBrowsing) { wake(); return; }
  asleep = true;
  try {
    sessionStorage.removeItem("panel_awake");
  } catch {}
  // And the server is told, because it cannot see this. Its own rule is
  // `IDLE_GAP_S`, measured off the last event written, so a conversation that
  // finished three minutes ago still reads as the current one for another
  // twenty-seven — and the next wake word replays all of it. This frame is
  // what makes that wake word a new conversation instead. #142.
  //
  // Below the guards and not above them, which is the whole of why it is
  // here rather than in the countdown that called this: every branch above
  // is a room with somebody still in it, and each of those returns through
  // `wake()`. Saying the screen went dark on one of those paths would roll
  // the thread out from under a card still spinning.
  //
  // Guarded on the socket and silent when it is down, like `armSession` and
  // `goIdle` above. A page that could not send it goes to sleep anyway; the
  // server keeps the thread it had, which is the same conversation it would
  // have kept before this frame existed.
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "sleep" }));
  }
  clearTimeout(clockIdleTimer);
  clockIdleTimer = null;
  clockPage.classList.remove("hidden");
  clearTimeout(clockTimer);
  // Ahead of the tick, and the order is load-bearing now that the tick asks
  // while asleep too. `loadScreensaver` sets `wxAsked` on its first line,
  // before its first `await`, so by the time `tickClock` runs `dueForWeather`
  // is already false and the sleep costs one request rather than two.
  loadScreensaver();
  tickClock();
  hideWorking();
  feed.querySelectorAll(".msg, .card, .working").forEach((n) => n.remove());
  cards.replaceChildren();
  forgetCards();
  forgetTopic();
  showStarters();
  userBubble = null;
  agentBubble = null;
  dropPending();
  seenTools = new Set();
  userCleared = true;
}

/**
 * Take the clock away, or — if it is already away — start the countdown over.
 *
 * One function for both because every caller means the same thing by it:
 * somebody is here. Splitting it into `wake` and `nudge` puts the choice at
 * each of the six call sites, and the site that gets it wrong is the one that
 * lets the screen go to sleep with a person standing in front of it.
 */
function wake() {
  if (asleep) {
    asleep = false;
    clockPage.classList.add("hidden");
    // The tick is not stopped with the face any more. It is the panel's
    // minute rather than the screensaver's: the bar's clock is on screen
    // exactly when the face is not, so something has to go on drawing it,
    // and one timer doing both is one thing to keep aimed at the minute
    // instead of two. What it costs while the face is hidden is two
    // `textContent` writes a minute.
    //
    // And an ask, because the bar has just become visible with whatever the
    // last `sleep()` fetched on it — which on a kitchen nobody walked into
    // overnight is eight hours old. Guarded, so the taps that arrive in a
    // burst on a touchscreen do not each become a request.
    if (dueForWeather(Date.now())) loadScreensaver();
  }
  try {
    sessionStorage.setItem("panel_awake", "1");
  } catch {}
  clearTimeout(clockIdleTimer);
  clockIdleTimer = setTimeout(sleep, IDLE_MS);
}

// A tap anywhere is somebody being here. On the document, not on the clock:
// while the clock is up it covers the page and gets the tap itself, and once
// it is away the same listener is what keeps the screen awake through a
// conversation being read rather than spoken. `pointerdown` covers touch,
// mouse and pen in one; `keydown` is for the Mac the panel is developed
// against, which has no touchscreen at all.
//
// Capturing, so a handler that stops propagation — the picker's, the cards
// pane's — cannot quietly cost the terminal its idle timer.
for (const ev of ["pointerdown", "keydown"]) {
  document.addEventListener(ev, wake, { capture: true, passive: true });
}

/* ---------- init ---------- */

function loadMaps(key) {
  // Loaded on demand rather than from the HTML so the key stays server-side
  // until the page is actually served. `callback` rather than the script's own
  // load event: under loading=async the script settles before the API is
  // assembled, and google.maps.importLibrary does not exist yet at that point.
  //
  // `language` is what labels the map tiles and what PlacesService returns as a
  // display name, and it is fixed for the life of the page: Maps JS reads it
  // once, off this URL, and loading a second copy to switch it is not supported.
  // So it follows the panel's own language rather than the turn — the setting
  // is what the terminal is set up in, and it is stable in a way the turn is
  // not. A turn in the other language gets its prose translated and its map
  // labels not, which is the same compromise the Maps grounding chunks make.
  // Changing the setting does not relabel a map already loaded; the note the
  // picker prints says so.
  const language = document.documentElement.lang || "en";
  return new Promise((resolve) => {
    window[MAPS_CALLBACK] = () => resolve(true);
    const script = document.getElementById("maps-api-script");
    script.onerror = () => resolve(false);
    script.src =
      `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}` +
      `&language=${encodeURIComponent(language)}` +
      `&libraries=places&loading=async&callback=${MAPS_CALLBACK}`;
    script.async = true;
  });
}

/**
 * Say on the root element whether this machine has a colour emoji font.
 *
 * Once, at startup: fonts do not arrive later, and the calendar card asks the
 * question on every row. Raspberry Pi OS does not always ship
 * `fonts-noto-color-emoji`, and the failure is silent — Chromium draws tofu
 * and the card looks broken rather than unsupported. `no-emoji` swaps the
 * category glyphs for Material Symbols, which every other card already needs.
 *
 * A canvas that cannot be had leaves the class off, which is the emoji path:
 * the same guess as the browsers this was written on.
 */
function markEmoji() {
  const ctx = document.createElement("canvas").getContext("2d");
  if (!ctx) return;
  ctx.font = "32px sans-serif";
  const has = emojiRenders((s) => ctx.measureText(s).width);
  document.documentElement.classList.toggle("no-emoji", !has);
}

async function init() {
  // Before the clock and before the `await`, in that order and for two
  // reasons. The clock's first face is drawn by `sleep()` and its weekday
  // comes out of `Intl` with whatever tag is on the document by then; and
  // `loadMaps` below reads the same tag to label its tiles, once, for the
  // life of the page. Nothing here is a round trip — the choice is in
  // localStorage — so the panel is never briefly in the wrong language.
  const locale = pickLocale(settings.lang, navigator.language);
  showLanguages(locale);
  applyLanguage(locale);

  // Distinguish awake reload (mid-conversation reload, #356) from fresh boot
  // or screensaver reload (#448). When awake, hide clock, start top bar clock
  // tick, and arm idle timer; otherwise sleep immediately.
  let awake = false;
  try {
    awake = sessionStorage.getItem("panel_awake") === "1";
  } catch {}

  if (awake) {
    clockPage.classList.add("hidden");
    clearTimeout(clockIdleTimer);
    clockIdleTimer = setTimeout(sleep, IDLE_MS);
    tickClock();
    userCleared = false;
  } else {
    sleep();
  }
  markEmoji();

  const config = await fetch("/api/config")
    .then((r) => r.json())
    .catch(() => ({}));
  // Which build this document came off, kept so the next reconnect can tell a
  // deploy from a restart. First from the document itself — the server that
  // wrote these bytes is the only one that knows what they are — and then
  // from the fetch for a page that was not stamped (a static mount, or a
  // file:// opened for a test).
  //
  // A mismatch between what the document was served with and what the server
  // is answering now means the server was restarted under a new build while
  // this document was in flight. Mark the corner, so that if the reconnect
  // loop fails to reload it is visible from across the room. #261.
  const served = config.static_revision || config.revision || "";
  bootRevision = stampedBuild() || served;
  if (bootRevision && served && served !== bootRevision) {
    markStale();
  }
  if (config.maps_api_key) await loadMaps(config.maps_api_key);

  // Milliseconds here, seconds on a server that may also be too old to send
  // this or unreachable and sending nothing. Anything that is not a positive
  // number leaves the default standing, because `setTimeout(fn, NaN)` fires on
  // the next tick — the microphone would go off in the same breath as the wake
  // word that opened it.
  if (Number.isFinite(config.wake_idle_s) && config.wake_idle_s > 0) {
    wakeIdleMs = config.wake_idle_s * 1000;
  }

  if (config.management_routes && rebootButton) {
    rebootButton.classList.remove("hidden");
  }

  showProfiles(config.profiles || []);
  // Before `connect`, and that ordering is the whole of it: the voice is fixed
  // for the life of a connection, so a socket opened ahead of this one speaks
  // in the default until something else reopens it.
  showVoices(config.voices, config.voice);
  // Before `connect`, and awaited: the label rides the handshake, so a pin
  // relabelled after the socket is up leaves the clock face reading the old
  // language until something reopens it. Costs nothing on an ordinary boot —
  // see `relabelHome`, which compares two strings and returns.
  await relabelHome();
  showWhere();
  listMicrophones();
  connect();
}

init();


/**
 * Restore settled turns into the feed on reconnect or page reload.
 */
function handleHistory(data) {
  if (typeof asleep !== "undefined" && asleep) return;
  if (userCleared) return;
  if (feed.querySelector(".msg")) return;
  if (!Array.isArray(data?.turns) || data.turns.length === 0) return;

  hideStarters();
  for (const turn of data.turns) {
    if (!turn || typeof turn.text !== "string" || !turn.text) continue;
    const div = document.createElement("div");
    if (turn.role === "user") {
      div.className = "msg user";
      div.textContent = turn.text;
    } else {
      div.className = "msg agent md";
      div.innerHTML = formatMarkdown(turn.text);
    }
    append(div);
  }
  feed.scrollTop = feed.scrollHeight;
}

/* ---------- image modal & kiosk link inhibition (#628) ---------- */

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeImageModal();
    closeGwsAuthModal(true);
    closeCitationModal();
  }
});

const imageModal = document.getElementById("imageModal");
const imageModalClose = document.getElementById("imageModalClose");
const imageModalBackdrop = document.getElementById("imageModalBackdrop");

imageModalClose?.addEventListener("click", closeImageModal);
imageModalBackdrop?.addEventListener("click", closeImageModal);

const citationModal = document.getElementById("citationModal");
const citationModalClose = document.getElementById("citationModalClose");
const citationModalBackdrop = document.getElementById("citationModalBackdrop");
const citationModalReturnBtn = document.getElementById("citationModalReturnBtn");
const citationModalTitle = document.getElementById("citationModalTitle");
const citationQr = document.getElementById("citationQr");
const citationUrl = document.getElementById("citationUrl");

function openCitationModal(uri, title) {
  if (!citationModal || !uri) return;
  if (citationModalTitle) {
    citationModalTitle.textContent = title || uri;
    citationModalTitle.title = title || uri;
  }
  if (citationQr) {
    try {
      citationQr.innerHTML = generateQrSvg(uri, { cellSize: 3, margin: 2 });
    } catch (err) {
      citationQr.textContent = "QR code generation failed";
    }
  }
  if (citationUrl) {
    citationUrl.href = uri;
    citationUrl.textContent = uri.length > 50 ? uri.slice(0, 50) + "…" : uri;
    citationUrl.title = uri;
  }
  citationModal.classList.remove("hidden");
}

function closeCitationModal() {
  if (citationModal) {
    citationModal.classList.add("hidden");
  }
}

citationModalClose?.addEventListener("click", closeCitationModal);
citationModalBackdrop?.addEventListener("click", closeCitationModal);
citationModalReturnBtn?.addEventListener("click", closeCitationModal);

// In-app image preview modal, citation QR modal, and kiosk link inhibition.
// On kiosk mode (?kiosk=1), Chromium runs without browser chrome (no back button,
// no tabs, no address bar) and has no keyboard, so navigating to external websites
// permanently strands the appliance.
document.addEventListener("click", (e) => {
  const kiosk = typeof window !== "undefined" && new URLSearchParams(window?.location?.search || "").get("kiosk") === "1";

  // Check if click was on an image thumbnail in cards
  const thumb = e.target.closest?.(".image-grid a, a.image-thumb");
  if (thumb) {
    e.preventDefault();
    e.stopPropagation();
    const previewSrc = thumb.dataset.preview || thumb.querySelector("img")?.src || "";
    const previewTitle = thumb.dataset.title || thumb.querySelector(".src")?.textContent || "";
    openImageModal(previewSrc, previewTitle);
    return;
  }

  // Check if click was on a citation link or chip in kiosk mode (#701)
  const citation = e.target.closest?.(".kiosk-citation, a[data-uri]");
  if (citation && (kiosk || citation.classList.contains("kiosk-citation"))) {
    const uri = citation.dataset.uri || (citation.getAttribute("href") !== "#" ? citation.getAttribute("href") : "");
    if (uri) {
      e.preventDefault();
      e.stopPropagation();
      const title = citation.dataset.title || citation.querySelector(".chip-label")?.textContent || citation.textContent || uri;
      openCitationModal(uri, title.trim());
      return;
    }
  }

  // In kiosk mode, inhibit any other external navigation to avoid stranding
  if (kiosk) {
    const link = e.target.closest?.("a[href]");
    if (link) {
      const href = link.getAttribute("href");
      if (href && (href.startsWith("http://") || href.startsWith("https://") || href === "#")) {
        e.preventDefault();
        e.stopPropagation();
      }
    }
  }
});

/**
 * Enable vertical pointer drag scrolling on a scrollable container (#532).
 * Allows swiping / dragging to scroll on touchscreen / pointer displays.
 */
function enableDragScroll(container, onUserScroll) {
  if (!container) return;
  let isDown = false;
  let startY = 0;
  let startScrollTop = 0;
  let isDragging = false;
  let activePointerId = null;

  container.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    isDown = true;
    isDragging = false;
    activePointerId = e.pointerId;
    startY = e.clientY;
    startScrollTop = container.scrollTop;
  });

  const onPointerMove = (e) => {
    if (!isDown || (activePointerId !== null && e.pointerId !== activePointerId)) return;
    const dy = e.clientY - startY;
    if (!isDragging) {
      if (Math.abs(dy) > 5) {
        isDragging = true;
        if (typeof onUserScroll === "function") {
          onUserScroll();
        }
        try {
          container.setPointerCapture(e.pointerId);
        } catch (_) {}
      }
    }
    if (isDragging) {
      container.scrollTop = startScrollTop - dy;
      if (e.cancelable) e.preventDefault();
    }
  };

  const onPointerUp = (e) => {
    if (!isDown || (activePointerId !== null && e.pointerId !== activePointerId)) return;
    if (isDragging) {
      const suppressClick = (ev) => {
        ev.stopPropagation();
        ev.preventDefault();
      };
      if (typeof window !== "undefined") {
        window.addEventListener("click", suppressClick, { capture: true, once: true });
        setTimeout(() => {
          window.removeEventListener("click", suppressClick, { capture: true });
        }, 100);
      }
      try {
        if (container.hasPointerCapture(e.pointerId)) {
          container.releasePointerCapture(e.pointerId);
        }
      } catch (_) {}
    }
    isDown = false;
    isDragging = false;
    activePointerId = null;
  };

  container.addEventListener("pointermove", onPointerMove);
  container.addEventListener("pointerup", onPointerUp);
  container.addEventListener("pointercancel", onPointerUp);
  if (typeof window !== "undefined") {
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
  }
}

if (typeof feed !== "undefined" && feed) {
  enableDragScroll(feed);
}
const scrollSidePanel = (typeof sidePanel !== "undefined" && sidePanel) ? sidePanel : (typeof cards !== "undefined" ? cards : null);
if (scrollSidePanel) {
  enableDragScroll(scrollSidePanel, () => {
    followCards = false;
  });
}

