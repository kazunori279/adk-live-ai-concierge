/**
 * Where the terminal is — the map picker behind the settings sheet.
 *
 * The browser used to work this out for itself at startup. On this device it
 * could not: geolocation needs a secure context, a kiosk cannot answer a
 * permission prompt, and Chromium on Linux resolves position from Wi-Fi
 * through a service that wants its own key. When all three lined up it still
 * only landed in the neighbourhood, which is fine for a forecast and wrong for
 * "a ramen shop nearby". The terminal sits on one counter and does not move, so
 * the position is a setting now, and geolocation is only the starting point the
 * `my_location` button offers.
 *
 * `pickLocation` resolves to `{ lat, lng, label }` or to null if the sheet was
 * dismissed. Nothing here writes to storage; app.js owns that.
 */

import { t } from "./i18n.js";

// Geolocation can sit on a permission prompt nobody answers, so cap the wait.
const GEO_TIMEOUT_MS = 8000;
const GEO_CAP_MS = 10000;

// Close enough to see which side of the street the pin is on. The wider zoom is
// for a first run with nothing saved, where the map opens on the whole city.
const PIN_ZOOM = 17;
const WIDE_ZOOM = 11;

// Tokyo, the same fallback centre the cards use, for a terminal that has never
// been told where it is and cannot be geolocated either.
const DEFAULT_CENTER = { lat: 35.681, lng: 139.767 };

// Panning fires `idle` constantly, and each settled position costs a
// reverse-geocode. Long enough that dragging across a city is one lookup, short
// enough that letting go feels like it answered.
const GEOCODE_DEBOUNCE_MS = 600;

const picker = document.getElementById("picker");
const pickerMap = document.getElementById("pickerMap");
const pickerSearch = document.getElementById("pickerSearch");
const pickerFind = document.getElementById("pickerFind");
const pickerLocate = document.getElementById("pickerLocate");
const pickerLabel = document.getElementById("pickerLabel");
const pickerCancel = document.getElementById("pickerCancel");
const pickerConfirm = document.getElementById("pickerConfirm");

let map = null;
let settle = null; // debounce timer for the reverse-geocode
let chosen = null; // { lat, lng, label } as it stands under the pin
let resolvePick = null; // set while the sheet is open

// What a place is called, in order of how specific it is. The first of these
// the geocoder answered with is the first half of the name.
//
// A list rather than the two components the ask was phrased in ("Ueno,
// Tokyo"): the components a spot has are not the same everywhere. A Tokyo
// address has a `sublocality_level_1` and the ward under `locality`; a hot
// spring two hours away has no sublocality at all and `locality` is the town;
// a house in the French countryside has neither and stops at the department.
// Picking the most specific one that exists gives all three a name of the same
// size, and hardcoding two would give the first a ward it did not need and the
// third nothing at all.
const LOCAL_TYPES = [
  "sublocality_level_1", "sublocality_level_2", "neighborhood",
  "locality", "postal_town", "administrative_area_level_2", "sublocality",
];

const IS_BLOCK = /^(?:[0-9０-９]+(?:[-−ー―][0-9０-９]+)*|[0-9０-９]+(?:\s*丁目|\s*番地?|\s*号)?|[0-9０-９]+[-\s]?[Cc]h[oō]me)$/i;

// And the wider thing it is in, for the half after the comma. `country` only
// when there is no state or prefecture, which is the small-country case.
const REGION_TYPES = ["administrative_area_level_1", "country"];

/* ---------- geocoding ---------- */

/** The first component of any of `types`, most specific first, or "". */
function component(components, types) {
  for (const type of types) {
    const hit = components.find((part) => {
      if (!part.types?.includes(type)) return false;
      if (type === "sublocality" &&
          part.types.some((t) => /^sublocality_level_[3-9]/.test(t))) return false;
      return part.long_name && !IS_BLOCK.test(part.long_name.trim()) && !/^\d+$/.test(part.long_name.trim());
    });
    if (hit?.long_name) return hit.long_name;
  }
  return "";
}

/**
 * A place name out of a geocoder result's components: "Ueno, Tokyo".
 *
 * `formatted_address` is a postal address — a building number, a block, a
 * postcode and a country — and the clock face has one line for a temperature,
 * a sky and this. It is also more than anyone standing in front of the panel
 * needs told about where they are.
 *
 * The components come back on the same response, so this costs no second
 * request, and they are already in the language the request asked for.
 *
 * "" when the geocoder returned nothing this recognises — the middle of the
 * sea, a plus code — and the caller falls back to the formatted address, which
 * is long but is at least an answer.
 */
function shortName(components) {
  const parts = [];
  for (const types of [LOCAL_TYPES, REGION_TYPES]) {
    const name = component(components || [], types);
    if (types === LOCAL_TYPES && (IS_BLOCK.test(name.trim()) || /^\d+$/.test(name.trim()))) continue;
    // Deduped, or a city that is also its own prefecture reads "Tokyo, Tokyo".
    if (name && !parts.includes(name)) parts.push(name);
  }
  if (parts.length > 0 && /^\d+$/.test(parts[0].trim())) {
    parts.shift();
  }
  return parts.join(", ");
}

/** Resolve within `ms`, or give up and resolve null. */
function cap(promise, ms) {
  return Promise.race([
    promise,
    new Promise((resolve) => setTimeout(() => resolve(null), ms)),
  ]);
}

/** The browser's own idea of where it is, or null. Never throws. */
function coords() {
  if (!navigator.geolocation) return Promise.resolve(null);
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (position) => resolve(position.coords),
      () => resolve(null),
      { timeout: GEO_TIMEOUT_MS, maximumAge: 10 * 60 * 1000 },
    );
  });
}

/**
 * A place name for a position, or "" when Maps cannot say.
 *
 * A name and not the street address it used to be: see `shortName`. Exported
 * because `app.js` calls it too, to rewrite a label that was written down in
 * the other language.
 *
 * `language` because this string outlives the sheet it is written in: it is
 * saved, it is what the settings sheet prints under *Location*, it travels on
 * the handshake, and since #131 it is the place at the end of the line under
 * the clock. Without it the Geocoder answers in the kiosk Chromium's own
 * locale, which is a thing somebody set up once on the Pi and is not the
 * language picker (#96) — so a panel switched to Japanese kept an English
 * address in four places at once. Off `<html lang>`, the same tag every other
 * string on the page is chosen by.
 *
 * It used to decide only how a label is written down and not which ones
 * already are, so a spot picked before #131 kept the English address it was
 * picked with until somebody dropped the pin again — which is how the panel
 * came to be reported as still English after #131 shipped. `relabelHome` in
 * `app.js` is the other half: it calls this for the saved pin whenever the
 * language it was written in is not the one the panel is set to.
 */
export async function reverseGeocode(lat, lng) {
  if (!window.google?.maps?.Geocoder) return "";
  try {
    const { results } = await new google.maps.Geocoder().geocode({
      location: { lat, lng },
      language: document.documentElement.lang || "en",
    });
    const hit = results?.[0];
    return shortName(hit?.address_components) || hit?.formatted_address || "";
  } catch {
    return "";
  }
}

/** What to show for a position when the geocoder has nothing. */
function asCoords({ lat, lng }) {
  return `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
}

/* ---------- the sheet ---------- */

/**
 * Reverse-geocode whatever is under the pin and print it.
 *
 * Debounced, and the result is only kept if the map has not moved on since —
 * two pans in quick succession would otherwise race and the slower lookup would
 * win with the older position.
 */
function describeCenter() {
  clearTimeout(settle);
  const center = map.getCenter();
  const here = { lat: center.lat(), lng: center.lng() };
  // Opening the sheet on a saved spot fires `idle` without anything having
  // moved. The label is already right; looking it up again would spend a
  // geocode to replace it with itself.
  if (chosen?.label && asCoords(chosen) === asCoords(here)) return;
  chosen = { ...here, label: "" };
  pickerLabel.textContent = asCoords(chosen);
  const mine = chosen;
  settle = setTimeout(async () => {
    const label = await reverseGeocode(mine.lat, mine.lng);
    if (chosen !== mine || !label) return;
    chosen.label = label;
    pickerLabel.textContent = label;
  }, GEOCODE_DEBOUNCE_MS);
}

function centerOn(lat, lng, zoom) {
  map.setCenter({ lat, lng });
  if (zoom) map.setZoom(zoom);
}

async function search() {
  const address = pickerSearch.value.trim();
  if (!address || !window.google?.maps?.Geocoder) return;
  pickerFind.disabled = true;
  try {
    const { results } = await new google.maps.Geocoder().geocode({ address });
    const hit = results?.[0];
    if (!hit) {
      pickerLabel.textContent = t("ui_no_address");
      return;
    }
    // The viewport is what makes a search for a city show the city and a search
    // for a building show the building; setCenter alone would keep whatever
    // zoom the last pan left behind.
    if (hit.geometry.viewport) map.fitBounds(hit.geometry.viewport);
    else centerOn(hit.geometry.location.lat(), hit.geometry.location.lng(), PIN_ZOOM);
  } catch {
    pickerLabel.textContent = t("ui_geocode_failed");
  } finally {
    pickerFind.disabled = false;
  }
}

async function locate() {
  pickerLocate.disabled = true;
  pickerLabel.textContent = t("ui_finding_you");
  const position = await cap(coords(), GEO_CAP_MS);
  pickerLocate.disabled = false;
  if (!position) {
    pickerLabel.textContent = t("ui_no_position");
    return;
  }
  centerOn(position.latitude, position.longitude, PIN_ZOOM);
}

function close(result) {
  clearTimeout(settle);
  picker.classList.add("hidden");
  const done = resolvePick;
  resolvePick = null;
  done?.(result);
}

/**
 * Open the map, resolve with the spot that was confirmed, or null.
 *
 * The pin does not move: it is painted at the centre of the map and the map
 * pans under it. A pin you drag is a 40 px target on a panel people prod with a
 * thumb, and it hides under the thumb at the moment you need to see it; the
 * whole map is the drag target this way.
 */
export function pickLocation(initial) {
  if (!window.google?.maps?.Map) return Promise.resolve(null);
  if (resolvePick) return Promise.resolve(null);

  picker.classList.remove("hidden");
  pickerSearch.value = "";

  const start = initial?.lat !== undefined ? initial : null;
  const center = start ? { lat: start.lat, lng: start.lng } : DEFAULT_CENTER;
  const zoom = start ? PIN_ZOOM : WIDE_ZOOM;

  if (!map) {
    // Built on first open rather than at startup: a map laid out in a hidden
    // element comes up as grey tiles, and most sessions never open this.
    map = new google.maps.Map(pickerMap, {
      center,
      zoom,
      disableDefaultUI: true,
      zoomControl: true,
      gestureHandling: "greedy",
    });
    map.addListener("idle", describeCenter);
  } else {
    centerOn(center.lat, center.lng, zoom);
  }

  pickerLabel.textContent = start?.label || asCoords(center);
  chosen = { ...center, label: start?.label || "" };

  return new Promise((resolve) => {
    resolvePick = resolve;
  });
}

pickerConfirm.addEventListener("click", () => {
  if (!chosen) return close(null);
  close({ ...chosen, label: chosen.label || asCoords(chosen) });
});

pickerCancel.addEventListener("click", () => close(null));
pickerFind.addEventListener("click", search);
pickerLocate.addEventListener("click", locate);

pickerSearch.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    search();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && resolvePick) close(null);
});
