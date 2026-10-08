/**
 * Result cards — one renderer per tool. Each returns a detached element, and
 * app.js decides which pane it goes in; a renderer that draws a map takes that
 * pane as `root` so it knows which scroll container to watch.
 *
 * Maps are initialised lazily: a Pi 5 renders two or three Maps JS instances
 * comfortably, but not a screenful, and an off-screen map draws grey tiles.
 */

import { escapeAttr, escapeHtml, formatMarkdown } from "./markdown.js";
import { t } from "./i18n.js";
// The markdown import goes first because `js_check` reads it by line number,
// pinning that the renderers still have the attribute-position escape in
// scope. Alphabetical order would be prettier and would break that case.

const MAP_ZOOM_CAP = 15;

/* ---------- language ---------- */

/*
 * The card labels used to live here, in a `LABELS` table read off
 * `<html lang>` — the tag the model passed its tools, so a card's chrome
 * changed language with the turn. It is one table in `i18n.js` now, shared
 * with the settings sheet and the starter chips, and `<html lang>` carries the
 * language the terminal is *set* to rather than the one it was last spoken to
 * in. `t` is unchanged and still reads the document, so no renderer's
 * signature moved.
 */

function el(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}

function head(icon, title, extra = "") {
  return `
    <div class="card-head">
      <span class="material-symbols-outlined">${icon}</span>
      <span class="title">${escapeHtml(title)}</span>
      ${extra}
    </div>`;
}

/**
 * The area a place search was confined to, when it was confined to one.
 *
 * Empty draws nothing, which is the honest picture of a search of everywhere.
 * A set one is drawn because a filter applied and then hidden is what made
 * #196 take somebody who knows where Fuji is to notice: the answer was about
 * 富士山, the map was of 港区, and no part of the card said 港区. The tool has
 * always returned this field and no renderer has ever read it.
 */
function nearPill(near) {
  if (!near) return "";
  return `<span class="near">${escapeHtml(
    t("near_around").replace("{where}", near))}</span>`;
}

/**
 * The row of chips under a card.
 *
 * A chip for a place carries `data-place="<its index in items>"`, which is how
 * `markPlaces` finds it again once Places answers with the local name — see
 * `setChipLabel`. The index rather than the place id, for two reasons: two
 * chunks for the same shop would fight over one id, and `escapeHtml` does not
 * escape quotes, so nothing opaque belongs inside an attribute. Chips for
 * sources and videos carry no place id and get no attribute, which is what
 * keeps the two chip rows on a place card apart.
 */
function linkChips(items, icon) {
  if (!items.length) return "";
  const chips = items
    .map((it, i) => {
      const label = it.title || it.uri || t("source");
      const key = it.place_id ? ` data-place="${i}"` : "";
      const kiosk = typeof isKiosk === "function" ? isKiosk() : (typeof window !== "undefined" && new URLSearchParams(window?.location?.search || "").get("kiosk") === "1");
      const href = kiosk ? "#" : (it.uri || "#");
      const target = kiosk ? "" : ' target="_blank" rel="noopener"';
      const kioskAttr = kiosk
        ? ` class="link kiosk-link kiosk-citation" data-uri="${escapeAttr(it.uri || "")}" data-title="${escapeAttr(label)}"`
        : ' class="link"';
      return `
        <a${kioskAttr} href="${escapeAttr(href)}"${key}${target} title="${escapeAttr(label)}">
          <span class="material-symbols-outlined">${icon}</span>
          <span class="chip-label">${escapeHtml(label)}</span>
        </a>`;
    })
    .join("");
  return `<div class="links">${chips}</div>`;
}

/**
 * Put the name Places gave us on the chip that stands for the same place.
 *
 * The chips are drawn synchronously from the grounding chunk titles, which are
 * romanised — "Yamada" for ヤマダデンキ — while Maps JS, loaded with the
 * browser's locale (`loadMaps` in app.js), hands back the local name a moment
 * later. The chip is in the DOM by then, so this is a text update rather than a
 * re-render, and the romanised title stays put wherever the lookup does not
 * happen: no Maps key, no places library, a chunk with no place id, a request
 * that came back denied. The worst case is the card exactly as it reads today.
 */
function setChipLabel(card, i, name) {
  const label = card?.querySelector(`a.link[data-place="${i}"] .chip-label`);
  if (!label || !name) return;
  label.textContent = name;
  const chipLink = label.closest("a.link");
  if (chipLink) {
    chipLink.title = name;
    if (chipLink.dataset.title !== undefined) {
      chipLink.dataset.title = name;
    }
  }
}

/**
 * Keep `.paused` on `card` for exactly as long as it is off screen.
 *
 * Unlike `whenVisible` this one stays subscribed, because it is not setting
 * something up — it is stopping the weather card's animations from costing
 * frames while it is scrolled out of the pane. On a Pi 5 those frames come out
 * of the same budget as the audio.
 */
function whileVisible(card, root) {
  card.classList.add("paused");
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        card.classList.toggle("paused", !entry.isIntersecting);
      }
    },
    { root, threshold: 0 },
  );
  observer.observe(card);
}

/** Run `init` the first time `card` scrolls into view, then never again. */
function whenVisible(card, root, init) {
  const observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        observer.disconnect();
        init();
      }
    },
    { root, threshold: 0.1 },
  );
  observer.observe(card);
}

/**
 * Drop markers for every place that has a place_id, then fit the map to them.
 * Resolves to true if at least one marker landed.
 *
 * `seed` is anything the map already shows — a drawn route — so the fit covers
 * both rather than snapping to whichever was pinned last.
 *
 * The same lookup is what localizes the chips: `getDetails` is the only place
 * a name in the terminal's own language exists, so `setChipLabel` rides along
 * with it rather than costing a second round trip.
 */
function markPlaces(map, places, seed) {
  if (!places.length || !google.maps.places) return Promise.resolve(false);
  const bounds = seed ? new google.maps.LatLngBounds(seed.getSouthWest(), seed.getNorthEast())
                      : new google.maps.LatLngBounds();
  const service = new google.maps.places.PlacesService(map);
  // The card this map was drawn into, so a lookup can find its own chip. Taken
  // off the map div rather than passed in: all four callers already build the
  // map inside the card, so none of them has to change and none can be missed.
  const card = map.getDiv().closest(".card");

  const lookups = places.map((p, i) => {
    const pid = (p.place_id || "").replace(/^places\//, "");
    if (!pid) return Promise.resolve(false);
    return new Promise((resolve) => {
      service.getDetails(
        { placeId: pid, fields: ["geometry", "name"] },
        (place, status) => {
          if (status !== "OK" || !place) return resolve(false);
          // Ahead of the geometry guard: the local name is worth having on the
          // chip even for a place that came back with nowhere to pin it.
          setChipLabel(card, i, place.name);
          if (!place.geometry) return resolve(false);
          new google.maps.Marker({
            map,
            position: place.geometry.location,
            title: place.name || p.title,
            label: { text: String(i + 1), color: "#fff", fontSize: "12px" },
            icon: {
              path: google.maps.SymbolPath.CIRCLE,
              scale: 13,
              fillColor: "#1a73e8",
              fillOpacity: 1,
              strokeColor: "#fff",
              strokeWeight: 2,
            },
          });
          bounds.extend(place.geometry.location);
          resolve(true);
        },
      );
    });
  });

  return Promise.all(lookups).then((hits) => {
    if (bounds.isEmpty()) return false;
    map.fitBounds(bounds, 32);
    google.maps.event.addListenerOnce(map, "idle", () => {
      if (map.getZoom() > MAP_ZOOM_CAP) map.setZoom(MAP_ZOOM_CAP);
    });
    return hits.some(Boolean);
  });
}

/**
 * Center on a named landmark when grounding returned text citations without
 * structured place chunks. Resolves to true if geocoding dropped a marker.
 */
export function markNamedPlace(map, place) {
  if (!place || !window.google?.maps) return Promise.resolve(false);

  function apply(geometry, title) {
    if (!geometry?.location) return false;
    new google.maps.Marker({
      map,
      position: geometry.location,
      title: title || place,
      icon: {
        path: google.maps.SymbolPath.CIRCLE,
        scale: 13,
        fillColor: "#1a73e8",
        fillOpacity: 1,
        strokeColor: "#fff",
        strokeWeight: 2,
      },
    });
    if (geometry.viewport) {
      map.fitBounds(geometry.viewport);
      google.maps.event.addListenerOnce(map, "idle", () => {
        if (map.getZoom() > MAP_ZOOM_CAP) map.setZoom(MAP_ZOOM_CAP);
      });
    } else {
      map.setCenter(geometry.location);
      map.setZoom(MAP_ZOOM_CAP);
    }
    return true;
  }

  function tryGeocode() {
    if (!google.maps.Geocoder) return Promise.resolve(false);
    return new Promise((resolve) => {
      try {
        const geocoder = new google.maps.Geocoder();
        const res = geocoder.geocode({ address: place }, (results, status) => {
          const hit = results?.[0];
          if (status !== "OK" || !hit) return resolve(false);
          resolve(apply(hit.geometry, place));
        });
        if (res && typeof res.then === "function") {
          res.then(({ results }) => {
            const hit = results?.[0];
            if (!hit) resolve(false);
            else resolve(apply(hit.geometry, place));
          }).catch(() => resolve(false));
        }
      } catch {
        resolve(false);
      }
    });
  }

  if (google.maps.places?.PlacesService) {
    const service = new google.maps.places.PlacesService(map);
    return new Promise((resolve) => {
      try {
        service.findPlaceFromQuery(
          { query: place, fields: ["geometry", "name"] },
          (results, status) => {
            const hit = results?.[0];
            if (status === "OK" && hit) {
              resolve(apply(hit.geometry, hit.name));
            } else {
              tryGeocode().then(resolve);
            }
          },
        );
      } catch {
        tryGeocode().then(resolve);
      }
    });
  }

  return tryGeocode();
}

function newMap(div) {
  return new google.maps.Map(div, {
    center: { lat: 35.681, lng: 139.767 },
    zoom: 11,
    disableDefaultUI: true,
    zoomControl: true,
    gestureHandling: "greedy",
  });
}

/* ---------- tool call ---------- */

export function toolCallCard(name, args) {
  if (name && typeof name === "object") {
    args = name.args || {};
    name = name.name;
  }
  // `lang` is left out of the detail: it is how the chip knows which language
  // to be in, not something to print a bare "ja" for. `context` is left out
  // because it is a paragraph, and this line is a glance — nobody needs their
  // own situation recited back at them under the question they just asked.
  const skip = new Set(["lang", "context"]);
  const detail = Object.entries(args || {})
    .filter(([key, v]) => !skip.has(key) && typeof v === "string" && v)
    .map(([, v]) => v)
    .join(" · ");
  const label = t(`call_${name}`, name ? name.replace(/_/g, " ") : "");
  return el(`
    <div class="msg system tool-call">
      ${escapeHtml(label)}${detail ? ` — ${escapeHtml(detail)}` : ""}
    </div>`);
}

/* ---------- web_search ---------- */

export function webSearchCard(data) {
  data = data || {};
  const queries = (data.search_queries || [])
    .map((q) => `<span class="query">${escapeHtml(q)}</span>`)
    .join("");
  const summary = data.summary || "";
  const sources = data.sources || [];
  const images = (data.images || (data.entry && data.entry.images) || []).slice(0, 3);
  if (!summary && !sources.length && !queries && !images.length) return null;

  return el(`
    <div class="card">
      ${head("travel_explore", data.query || t("head_web_search"))}
      ${images.length ? imageGrid(images) : ""}
      <div class="card-body">
        ${queries ? `<div class="queries">${queries}</div>` : ""}
        <div class="md">${formatMarkdown(summary)}</div>
      </div>
      ${linkChips(sources, "article")}
    </div>`);
}

/* ---------- image_search ---------- */

function imageGrid(images) {
  if (!images || !images.length) return "";
  // Three columns on the panel, two when the cards go inline. An odd count
  // leaves a gap at the end of the last row, which is what a photo grid looks
  // like. The first tile used to be widened to a 16:9 hero to fill that gap
  // instead; on the panel that one photo came to 467 px of an 800 px screen
  // and pushed the opening hours the question was about off the bottom.
  const kiosk = typeof window !== "undefined" && new URLSearchParams(window?.location?.search || "").get("kiosk") === "1";
  const tiles = images
    .map((img) => {
      const src = img.thumbnail_uri || img.url || "";
      const href = img.source_uri || img.source || "#";
      const title = img.source_title || img.title || "";
      const safeHref = kiosk ? "#" : href;
      const targetAttr = kiosk ? "" : ' target="_blank" rel="noopener"';
      return `
      <a href="${escapeAttr(safeHref)}"${targetAttr} class="image-thumb" data-preview="${escapeAttr(src)}" data-title="${escapeAttr(title)}">
        <img src="${escapeAttr(src)}" alt="" loading="lazy" onerror="this.closest('a')?.remove()" />
        ${title ? `<span class="src">${escapeHtml(title)}</span>` : ""}
      </a>`;
    })
    .join("");
  return `<div class="image-grid">${tiles}</div>`;
}

export function imageCard(data) {
  const images = data.images || [];
  if (!images.length) return null;

  return el(`
    <div class="card">
      ${head("image_search", data.query || t("head_image_search"))}
      ${imageGrid(images)}
    </div>`);
}

/* ---------- composite card ---------- */

const COMPOSITE_SECTION_ORDER = {
  image_search: 10,
  images: 10,
  map: 20,
  maps_search: 20,
  web_search: 30,
  web: 30,
  summary: 30,
  youtube_search: 40,
  videos: 40,
  video: 40,
  places: 50,
  sources: 60,
};

function canonicalSection(section) {
  const s = String(section || "").toLowerCase();
  if (s === "images" || s === "image_search") return "image_search";
  if (s === "web" || s === "summary" || s === "web_search") return "web_search";
  if (s === "video" || s === "videos" || s === "youtube_search") return "youtube_search";
  if (s === "map" || s === "maps_search" || s === "places") return "maps_search";
  return s;
}

export function compositeCard(data) {
  data = data || {};
  const title = data.title || data.query || t("head_info") || "Info";
  const icon = data.icon || "info";
  const card = el(`
    <div class="card composite-card">
      ${head(icon, title)}
    </div>`);
  if (data.turn_id) {
    card.dataset.turnId = data.turn_id;
  }
  return card;
}

export function renderCompositeSection(card, section, data, root) {
  if (!card || !section || !data) return null;

  const key = canonicalSection(section);
  let sectionEl = null;

  if (key === "image_search") {
    const images = data.images || [];
    if (!images.length) return null;
    const gridHtml = imageGrid(images);
    if (!gridHtml) return null;
    sectionEl = el(gridHtml);
    sectionEl.dataset.section = "image_search";
  } else if (key === "web_search") {
    const queries = (data.search_queries || [])
      .map((q) => `<span class="query">${escapeHtml(q)}</span>`)
      .join("");
    const summary = data.summary || "";
    const sources = data.sources || [];
    const images = (data.images || (data.entry && data.entry.images) || []).slice(0, 3);
    if (!summary && !sources.length && !queries && !images.length) return null;
    sectionEl = el(`
      <div class="composite-section" data-section="web_search">
        ${images.length ? imageGrid(images) : ""}
        <div class="card-body">
          ${queries ? `<div class="queries">${queries}</div>` : ""}
          ${summary ? `<div class="md">${formatMarkdown(summary)}</div>` : ""}
        </div>
        ${linkChips(sources, "article")}
      </div>`);
  } else if (key === "youtube_search") {
    const videos = data.videos || [];
    if (!videos.length) return null;
    sectionEl = el(`
      <div class="composite-section" data-section="youtube_search">
        ${videoPlayer(videos)}
      </div>`);
  }

  if (!sectionEl) return null;

  const titleEl = card.querySelector(".card-head .title");
  if (titleEl && data.query) {
    titleEl.textContent = data.query;
  }

  const existing = card.querySelector(`:scope > [data-section="${escapeAttr(key)}"]`);
  if (existing) {
    card.replaceChild(sectionEl, existing);
  } else {
    const targetWeight = COMPOSITE_SECTION_ORDER[key] || 99;
    const siblings = Array.from(card.children).filter((c) => !c.classList.contains("card-head"));
    let inserted = false;
    for (const sibling of siblings) {
      const sibSection = sibling.dataset.section || "";
      const sibWeight = COMPOSITE_SECTION_ORDER[sibSection] || 99;
      if (sibWeight > targetWeight) {
        card.insertBefore(sectionEl, sibling);
        inserted = true;
        break;
      }
    }
    if (!inserted) {
      card.appendChild(sectionEl);
    }
  }

  if (card.isConnected && typeof sectionEl.scrollIntoView === "function") {
    sectionEl.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  return sectionEl;
}

export const compositeSectionUpdate = renderCompositeSection;

/* ---------- youtube_search ---------- */

/**
 * An embedded player for the first video, and links for the rest. Only the
 * first gets an iframe: each embed is a full player on a machine that also has
 * to keep an audio stream running.
 */
function videoPlayer(videos) {
  if (!videos.length) return "";
  const [first, ...rest] = videos;
  return `
    <div class="video">
      <iframe
        src="https://www.youtube.com/embed/${escapeHtml(first.video_id)}"
        allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
        allowfullscreen loading="lazy"></iframe>
    </div>
    ${first.title ? `<div class="video-title">${escapeHtml(first.title)}</div>` : ""}
    ${linkChips(rest.map((v) => ({ title: v.title, uri: v.uri })), "play_circle")}`;
}

export function videoCard(data) {
  const videos = data.videos || [];
  if (!videos.length) return null;

  return el(`
    <div class="card">
      ${head("smart_display", data.query || t("head_youtube_search"))}
      ${videoPlayer(videos)}
    </div>`);
}

/* ---------- camera ---------- */

export function cameraCard(data, root) {
  const title = (data && data.title) || t("head_camera");
  const card = el(`
    <div class="card camera-card">
      ${head("videocam", title)}
      <div class="video camera-container">
        <video autoplay playsinline muted></video>
        <div class="camera-error hidden"></div>
      </div>
    </div>`);

  const video = card.querySelector("video");
  const errEl = card.querySelector(".camera-error");
  let pc = null;
  let mediaSessionId = null;
  let extendTimer = null;
  let stopped = false;

  function showError(msg) {
    if (errEl) {
      errEl.textContent = msg;
      errEl.classList.remove("hidden");
    }
  }

  async function stopStream() {
    if (stopped) return;
    stopped = true;
    if (extendTimer) {
      clearInterval(extendTimer);
      extendTimer = null;
    }
    if (pc) {
      try {
        pc.close();
      } catch (_) {}
      pc = null;
    }
    if (video) {
      video.srcObject = null;
    }
    if (mediaSessionId) {
      const sid = mediaSessionId;
      mediaSessionId = null;
      try {
        fetch("/api/camera/stop", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mediaSessionId: sid }),
          keepalive: true,
        }).catch(() => {});
      } catch (_) {}
    }
  }

  async function startStream() {
    try {
      pc = new RTCPeerConnection();
      pc.addTransceiver("audio", { direction: "recvonly" });
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.createDataChannel("dataSendChannel");

      pc.ontrack = (event) => {
        if (event.streams && event.streams[0] && video) {
          video.srcObject = event.streams[0];
        }
      };

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      const resp = await fetch("/api/camera/offer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sdp: offer.sdp }),
      });

      if (!resp.ok) {
        if (resp.status === 401) {
          showError(t("camera_auth_required"));
        } else {
          showError(`Camera error: ${resp.status}`);
        }
        return;
      }

      const res = await resp.json();
      mediaSessionId = res.mediaSessionId;
      await pc.setRemoteDescription(new RTCSessionDescription({
        type: "answer",
        sdp: res.answerSdp,
      }));

      extendTimer = setInterval(async () => {
        if (!mediaSessionId || stopped) return;
        try {
          const extResp = await fetch("/api/camera/extend", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mediaSessionId }),
          });
          if (!extResp.ok) {
            clearInterval(extendTimer);
            extendTimer = null;
          }
        } catch (_) {}
      }, 210000);
    } catch (err) {
      showError(err.message || String(err));
      stopStream();
    }
  }

  if (typeof window !== "undefined") {
    window.addEventListener("beforeunload", () => {
      stopStream();
    }, { once: true });
  }

  if (typeof IntersectionObserver !== "undefined") {
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting && !stopped) {
          if (typeof document !== "undefined" && !document.body.contains(card)) {
            stopStream();
            observer.disconnect();
          }
        }
      }
    });
    observer.observe(card);
  }

  if (typeof RTCPeerConnection !== "undefined") {
    startStream();
  } else {
    showError("WebRTC not supported in this browser");
  }

  return card;
}

/* ---------- maps_search ---------- */

export function placesCard(data, root) {
  const places = data.places || [];

  const card = el(`
    <div class="card">
      ${head("explore_nearby", data.query || t("head_maps_search"), nearPill(data.near))}
      <div class="map"></div>
      <div class="card-body"><div class="md">${formatMarkdown(data.summary || "")}</div></div>
      ${linkChips(places, "location_on")}
    </div>`);

  if (window.google?.maps && places.length) {
    whenVisible(card, root, async () => {
      const div = card.querySelector(".map");
      if (!(await markPlaces(newMap(div), places))) div.remove();
    });
  } else {
    card.querySelector(".map").remove();
  }

  return card;
}

/* ---------- maps_route / maps_search_along_route ---------- */

const MODE_ICONS = {
  DRIVE: "directions_car",
  WALK: "directions_walk",
  BICYCLE: "directions_bike",
  TRANSIT: "directions_transit",
};

export function routeCard(data, root) {
  const origin = data.origin || "";
  const destination = data.destination || "";
  const mode = (data.travel_mode || "DRIVE").toUpperCase();
  const places = data.places || [];
  const title = data.query ? t("on_the_way").replace("{q}", data.query) : destination;
  const originCoords = data.origin_coords;
  const destCoords = data.destination_coords;

  const card = el(`
    <div class="card">
      <div class="card-head">
        <span class="material-symbols-outlined">${MODE_ICONS[mode] || "directions"}</span>
        <span class="title">${escapeHtml(title)}</span>
        <span class="mode">${escapeHtml(t(`mode_${mode}`, mode))}</span>
      </div>
      <div class="map"></div>
      <div class="route-ends">
        <span class="material-symbols-outlined">trip_origin</span>
        <span class="leg">${escapeHtml(origin)}</span>
        <span class="material-symbols-outlined">arrow_forward</span>
        <span class="leg">${escapeHtml(destination)}</span>
      </div>
      <div class="card-body"><div class="md">${formatMarkdown(data.summary || "")}</div></div>
      ${linkChips(places, "location_on")}
    </div>`);

  if (window.google?.maps) {
    whenVisible(card, root, () => drawRoute(card, origin, destination, mode, places, originCoords, destCoords));
  } else {
    card.querySelector(".map").remove();
  }

  return card;
}

const JS_TRAVEL_MODES = {
  DRIVE: "DRIVING",
  WALK: "WALKING",
  BICYCLE: "BICYCLING",
  TRANSIT: "TRANSIT",
};

async function drawRoute(card, origin, destination, mode, places, originCoords, destCoords) {
  const div = card.querySelector(".map");
  const map = newMap(div);
  const service = new google.maps.DirectionsService();

  const reqOrigin =
    originCoords && Number.isFinite(originCoords.lat) && Number.isFinite(originCoords.lng)
      ? originCoords
      : origin;
  const reqDest =
    destCoords && Number.isFinite(destCoords.lat) && Number.isFinite(destCoords.lng)
      ? destCoords
      : destination;

  const tryMode = (travelMode) =>
    new Promise((resolve) => {
      service.route({ origin: reqOrigin, destination: reqDest, travelMode }, (result, status) =>
        resolve(status === "OK" ? result : null),
      );
    });

  // Transit and cycling have far patchier coverage than driving, and a
  // ZERO_RESULTS on the mode the model picked would otherwise leave a blank
  // map of central Tokyo. A driving line still answers "which way is it".
  const requested = google.maps.TravelMode[JS_TRAVEL_MODES[mode] || "DRIVING"];
  const result =
    (await tryMode(requested)) ||
    (requested !== google.maps.TravelMode.DRIVING
      ? await tryMode(google.maps.TravelMode.DRIVING)
      : null);

  if (result) {
    new google.maps.DirectionsRenderer({ map }).setDirections(result);
    markPlaces(map, places, result.routes[0]?.bounds);
    return;
  }

  // No route at all — pins are the last thing worth showing, and if those come
  // up empty too the map is just a default view of nowhere in particular.
  if (!(await markPlaces(map, places))) div.remove();
}

/* ---------- place_info ---------- */

/**
 * One place, drawn from all four groundings at once: photos, a map, what the
 * web says, and a video.
 *
 * Photos first because a picture is what the screen is for, and because they
 * arrive as thumbnails rather than something to read. Sources last: they are
 * the audit trail, not the answer. Any block whose branch came back empty is
 * left out rather than stubbed, so a card for somewhere obscure quietly
 * shrinks to a paragraph and a pin.
 */
export function placeInfoCard(data, root) {
  const places = data.places || [];
  const summary = data.summary || "";
  const card = el(`
    <div class="card place-card">
      ${head("location_on", data.place || t("head_place_info"), nearPill(data.near))}
      ${imageGrid(data.images || [])}
      <div class="map"></div>
      ${summary ? `<div class="card-body"><div class="md">${formatMarkdown(summary)}</div></div>` : ""}
      ${videoPlayer(data.videos || [])}
      ${linkChips(places, "location_on")}
      ${linkChips(data.sources || [], "article")}
    </div>`);

  if (window.google?.maps && (places.length || data.place)) {
    whenVisible(card, root, async () => {
      const div = card.querySelector(".map");
      if (!div) return;
      const map = newMap(div);
      if (places.length) {
        if (!(await markPlaces(map, places))) div.remove();
      } else if (data.place) {
        if (!(await markNamedPlace(map, data.place))) div.remove();
      } else {
        div.remove();
      }
    });
  } else {
    card.querySelector(".map")?.remove();
  }

  return card;
}

/* ---------- weather ---------- */

/**
 * A Material Symbol per condition, and a different one after dark where that
 * changes the picture. Rain at night is still rain.
 */
const WX_ICONS = {
  clear: ["clear_day", "clear_night"],
  "mostly-clear": ["clear_day", "clear_night"],
  "partly-cloudy": ["partly_cloudy_day", "partly_cloudy_night"],
  cloudy: ["cloud", "cloud"],
  fog: ["foggy", "foggy"],
  drizzle: ["rainy_light", "rainy_light"],
  rain: ["rainy", "rainy"],
  snow: ["weather_snowy", "weather_snowy"],
  thunderstorm: ["thunderstorm", "thunderstorm"],
};

/**
 * Which animated layers a condition gets.
 *
 * One entry per layer and no more than three of them, because each one is a
 * composited element the Pi has to repaint next to a realtime audio pipeline.
 * The layers themselves are single elements with a tiling background and a
 * `transform` keyframe — a hundred raindrops as a hundred divs is what this is
 * deliberately not.
 */
const WX_LAYERS = {
  clear: ["sun"],
  "mostly-clear": ["sun", "clouds-thin"],
  "partly-cloudy": ["sun", "clouds"],
  cloudy: ["clouds"],
  fog: ["clouds-thin", "fog"],
  drizzle: ["clouds", "drizzle"],
  rain: ["clouds", "rain"],
  snow: ["clouds", "snow"],
  thunderstorm: ["clouds", "rain", "lightning"],
};

/**
 * Which Material Symbol stands for a sky, given the slug and the hour.
 *
 * Exported since #88, which put the same sky in the top bar: a glyph is the
 * one way to say "overcast" that reads the same in both locales, and a second
 * table of the same nine slugs somewhere else would be a bar and a card
 * disagreeing about the weather on one screen.
 */
export function wxIcon(condition, isDay) {
  const pair = WX_ICONS[condition] || WX_ICONS.cloudy;
  return pair[isDay ? 0 : 1];
}

/** A number with its unit, or an em dash when the API left it out. */
function wxValue(n, unit = "") {
  return n === null || n === undefined ? "—" : `${Math.round(n)}${unit}`;
}

/** The `sun` states that are a word on the card instead of two times. */
const WX_POLAR = { polar_day: "wx_polar_day", polar_night: "wx_polar_night" };

/**
 * The sunrise and sunset pair, or the one word that stands in for both.
 *
 * Inside a polar circle there are days with no sunrise and no sunset, and the
 * server says which in `sun` rather than in the times — those come back empty,
 * because no stamp is the right answer. Printing them anyway is what put
 * `Sunrise 00:00   Sunset 00:00` on the card: Open-Meteo answers such a day
 * with midnight at both ends, and `"00:00"` is truthy, so the `—` below never
 * fired. Two identical times read as a reading; the word reads as what it is.
 */
function wxSun(now) {
  const word = WX_POLAR[now.sun];
  if (word) return `<span>${t(word)}</span>`;
  return `<span>${t("wx_sunrise")} ${escapeHtml(now.sunrise || "—")}</span>
        <span>${t("wx_sunset")} ${escapeHtml(now.sunset || "—")}</span>`;
}

function wxHours(hours, unit) {
  return hours
    .map((h) => `
      <div class="wx-hour">
        <span class="wx-h-time">${escapeHtml(h.time)}</span>
        <span class="material-symbols-outlined"
              title="${escapeAttr(h.description || "")}"
          >${wxIcon(h.condition, h.is_day)}</span>
        <span class="wx-h-temp">${wxValue(h.temperature, unit)}</span>
        <span class="wx-h-pop${h.precipitation_chance >= 30 ? "" : " faint"}"
          >${wxValue(h.precipitation_chance, "%")}</span>
      </div>`)
    .join("");
}

function wxDays(days, unit, clientNow = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  const localDate =
    clientNow instanceof Date && !isNaN(clientNow)
      ? `${clientNow.getFullYear()}-${pad(clientNow.getMonth() + 1)}-${pad(clientNow.getDate())}`
      : "";
  const diffDay = Boolean(localDate && days[0]?.date && days[0].date !== localDate);

  return days
    .map((d, i) => {
      let label = d.label;
      if (diffDay && i < 2) {
        label = d.weekday || calWeekday(d.date) || d.label;
      }
      return `
      <div class="wx-day">
        <span class="wx-d-label">${escapeHtml(label)}</span>
        <span class="material-symbols-outlined"
              title="${escapeAttr(d.description || "")}"
          >${wxIcon(d.condition, true)}</span>
        <span class="wx-d-pop${d.precipitation_chance >= 30 ? "" : " faint"}"
          >${wxValue(d.precipitation_chance, "%")}</span>
        <span class="wx-d-range">
          <b>${wxValue(d.high, unit)}</b> / ${wxValue(d.low, unit)}
        </span>
      </div>`;
    })
    .join("");
}

/**
 * The weather, big enough to read from across the kitchen.
 *
 * This is the one card that is not a list of results, and the one question the
 * terminal gets asked most, so it gets the whole pane: the temperature at a
 * size you can glance at, an hourly strip, five days, and the small print
 * underneath. The sky behind it animates on `condition` — see WX_LAYERS — and
 * takes its palette from `is_day` rather than from the clock, which is wrong at
 * both ends of a Nordic summer.
 */
export function weatherCard(data, root) {
  if (data.error || !data.current) return null;

  const now = data.current;
  const clientNow = new Date();
  // The scale is spelled out once, on the number nobody can misread, and every
  // other temperature on the card is a bare degree sign.
  const scale = data.units?.temperature || "°C";
  const unit = "°";
  const layers = (WX_LAYERS[now.condition] || WX_LAYERS.cloudy)
    .map((name) => `<div class="wx-l wx-${name}"></div>`)
    .join("");

  const card = el(`
    <div class="card weather-card ${now.is_day ? "day" : "night"}"
         data-condition="${escapeAttr(now.condition)}">
      <div class="wx-sky" aria-hidden="true">${layers}</div>

      <div class="wx-now">
        <div class="wx-head">
          <span class="material-symbols-outlined">location_on</span>
          <span class="wx-place">${escapeHtml(data.location || t("head_weather"))}</span>
          <span class="wx-time" title="${escapeAttr(data.timezone || "")}">${escapeHtml(now.time)}</span>
        </div>
        <div class="wx-temp">${wxValue(now.temperature)}<span class="wx-deg">${escapeHtml(scale)}</span></div>
        <div class="wx-desc">
          <span class="material-symbols-outlined"
            >${wxIcon(now.condition, now.is_day)}</span
          >${escapeHtml(now.description || "")}
        </div>
        <div class="wx-sub">
          ${t("wx_feels")} ${wxValue(now.feels_like, unit)}
          · ${t("wx_rain")} ${wxValue(now.precipitation_chance, "%")}
        </div>
      </div>

      <div class="wx-hours">${wxHours(data.hourly || [], unit)}</div>
      <div class="wx-days">${wxDays(data.daily || [], unit, clientNow)}</div>

      <div class="wx-detail">
        <span>${t("wx_wind")} ${wxValue(now.wind_speed)} ${escapeHtml(data.units?.wind || "")}</span>
        <span>${t("wx_humidity")} ${wxValue(now.humidity, "%")}</span>
        ${wxSun(now)}
      </div>
    </div>`);

  whileVisible(card, root);
  return card;
}

/* ---------- calendar ---------- */

/**
 * What kind of thing an event is, in one glyph.
 *
 * Google Calendar has no category field, so there is nothing to read: the two
 * fields that look like one are both dead ends. `colorId` is whatever the
 * person last clicked in the web UI and is unset on almost everything, and
 * `eventType` has six values of which `birthday` and `workingLocation` are
 * already dropped by `_agenda.NOT_APPOINTMENTS` and `focusTime` and
 * `fromGmail` never appear on a household calendar — leaving `default` for
 * nearly every row and `outOfOffice` for the rest. So the category is guessed
 * from the title, and the structure of the event decides everything the title
 * cannot.
 *
 * Substring matching, with no word boundaries anywhere: this house's calendar
 * is half Japanese and Japanese has no spaces to anchor on, so `\b` would
 * silently match nothing on half the rows. That admits false positives — 祭
 * inside 祭日 — and they cost a wrong picture next to a title that is right
 * there beside it, which is the whole reason the glyph is decoration and the
 * row never depends on it.
 *
 * Order matters: lunch with a Meet link is lunch. Structure is only consulted
 * once the title has had its say, and 📅 is what a titled, solo, in-person
 * event whose wording nobody anticipated comes out as — which on an ordinary
 * personal calendar is a good third of them.
 *
 * Each entry carries a Material Symbols name beside the emoji, for a panel
 * with no colour emoji font — see `emojiRenders`. That is not a second design,
 * it is the same card with a duller glyph; the icon font is already loaded for
 * every other card on the screen.
 */
const CAL_CATS = [
  ["🍽", "restaurant",
   /lunch|dinner|breakfast|brunch|ランチ|ディナー|夕食|朝食|昼食|食事|会食|飲み会|ごはん|寿司|焼肉|ラーメン/],
  ["✈️", "flight",
   /flight|airport|trip|travel|出張|フライト|空港|旅行|帰省|新幹線/],
  ["🏥", "local_hospital",
   /doctor|dentist|clinic|hospital|vaccin|checkup|病院|歯医者|歯科|クリニック|健診|検診|通院|予防接種|人間ドック/],
  ["🎂", "cake", /birthday|anniversary|誕生日|バースデー|記念日/],
  ["🎒", "school",
   /school|class|lesson|pta|授業|学校|保育園|幼稚園|塾|レッスン|習い事|保護者|参観|運動会|入学|卒業|面談/],
  ["🏃", "directions_run",
   /\bgym\b|yoga|swim|running|workout|ジム|ヨガ|プール|ランニング|練習|トレーニング/],
  ["🎫", "local_activity",
   /concert|festival|movie|museum|match|ライブ|コンサート|映画|美術館|博物館|試合|イベント|お祭り/],
  ["🛠", "build",
   /repair|delivery|service|inspection|配達|宅配|修理|点検|工事|回収/],
  ["💼", "work",
   /meeting|standup|stand-up|1:1|review|sync|interview|会議|打ち合わせ|ミーティング|定例|商談|面接/],
];

const CAL_OOO = ["🏝", "beach_access"];
const CAL_CALL = ["📹", "videocam"];
const CAL_PEOPLE = ["👥", "group"];
const CAL_ALLDAY = ["📆", "event"];
const CAL_ANY = ["📅", "calendar_today"];

function calCat(ev) {
  if (ev.kind === "out of office") return CAL_OOO;
  const title = (ev.title || "").toLowerCase();
  for (const [emoji, icon, words] of CAL_CATS) {
    if (words.test(title)) return [emoji, icon];
  }
  if (ev.meet) return CAL_CALL;
  // `people` counts the person themselves, so two is the first interesting
  // number.
  if (ev.people > 1) return CAL_PEOPLE;
  if (ev.all_day) return CAL_ALLDAY;
  return CAL_ANY;
}

/**
 * Whether this machine can draw a colour emoji, or only a tofu box.
 *
 * Raspberry Pi OS is not guaranteed to have `fonts-noto-color-emoji`, and a
 * missing emoji font is not an error anywhere — Chromium draws a hollow
 * rectangle and the page looks like it was written wrong. The measurement is
 * the standard one: U+FFFF is a noncharacter no font has a glyph for, so
 * anything whose advance width matches it is being drawn out of the same
 * `.notdef`. Two emoji rather than one, because a font can carry a black-and
 * -white 📅 and none of the rest.
 *
 * `measure` is handed in rather than taken off a canvas here, so this stays a
 * pure function the checks can drive; `app.js` supplies the canvas.
 */
export function emojiRenders(measure) {
  const tofu = measure("￿");
  return measure("📅") !== tofu || measure("🍽") !== tofu;
}

/** The `Intl` locale for the turn — see `t`. */
function calLang() {
  return document.documentElement.lang || "en";
}

/** `2026-09-09` as the day of the month, unpadded. */
function calDayNum(day) {
  return String(Number(day.slice(8, 10)) || "");
}

/** `2026-09-09` as `9/9` — for the far end of a range or a spill-over. */
function calShort(day) {
  return `${Number(day.slice(5, 7))}/${Number(day.slice(8, 10))}`;
}

/**
 * A date the way it is written in the language of the turn.
 *
 * `Intl` and not a table, because this is the one string on the card that is
 * a date rather than a word, and "Sep 9" against "9月9日" is exactly the sort
 * of thing a two-entry `STRINGS` block gets wrong. Guarded, because a bad
 * `day` would otherwise take the whole card down over its heading.
 */
function calFormat(day, opts) {
  try {
    return new Intl.DateTimeFormat(calLang(), opts)
      .format(new Date(`${day}T00:00:00`));
  } catch {
    return day;
  }
}

function calWeekday(day) {
  return calFormat(day, { weekday: "short" });
}

function calRange(from, to) {
  const head = calFormat(from, { month: "short", day: "numeric" });
  return from === to ? head : `${head} – ${calShort(to)}`;
}

/** One `<span>` of small print under a title. */
function calChip(icon, text) {
  return `
    <span class="cal-chip">
      <span class="material-symbols-outlined">${icon}</span
      >${escapeHtml(text)}
    </span>`;
}

/**
 * Whether this event is the one happening at `clock`.
 *
 * `clock` is empty for any day that is not today, which is what keeps a
 * 14-day answer from lighting up one row per day. An event that runs past
 * midnight has `to` earlier than `from` and `ends` set, and there the end is
 * on a day this row is not drawn on — so the start is the whole test. The
 * other half of that event, the morning it finishes in, is not marked: it is
 * grouped under the day it began and nothing on the card says otherwise.
 */
function calNow(ev, clock) {
  if (!clock) return false;
  if (ev.all_day) return true;
  if (!ev.from) return false;
  return ev.ends ? clock >= ev.from : clock >= ev.from && clock < ev.to;
}

function calRow(ev, clock) {
  const [emoji, icon] = calCat(ev);
  const live = calNow(ev, clock);
  const time = ev.all_day
    ? `<span class="cal-time"><b>${escapeHtml(t("cal_allday"))}</b>${
        ev.until ? `<small>→ ${escapeHtml(calShort(ev.until))}</small>` : ""
      }</span>`
    : `<span class="cal-time"><b>${escapeHtml(ev.from || "")}</b><small>${
        escapeHtml(ev.to || "")
      }${ev.ends ? ` → ${escapeHtml(calShort(ev.ends))}` : ""}</small></span>`;

  const title = ev.title || t("cal_untitled");
  const bits = [];
  if (ev.where) bits.push(calChip("location_on", ev.where));
  if (ev.people > 1) bits.push(calChip("group", String(ev.people)));
  if (ev.tentative || ev.your_rsvp) bits.push(calChip("help", t("cal_maybe")));

  return `
    <div class="cal-ev${live ? " now" : ""}${ev.title ? "" : " faded"}">
      <span class="cal-cat" aria-hidden="true"
        ><span class="cal-emoji">${emoji}</span
        ><span class="material-symbols-outlined">${icon}</span
      ></span>
      ${time}
      <span class="cal-what">
        <span class="cal-title">${escapeHtml(title)}</span>
        ${bits.length ? `<span class="cal-bits">${bits.join("")}</span>` : ""}
      </span>
      ${live ? `<span class="cal-live">${escapeHtml(t("cal_now"))}</span>` : ""}
    </div>`;
}

/**
 * A token that ends in a full stop without ending a sentence.
 *
 * Three kinds and only the last is a list anybody has to keep. A lone letter
 * is an initial — "A. B. Watanabe". Letters with stops between them are an
 * initialism written out — `e.g.`, `i.e.`, `a.m.`, `U.S.` — and that one
 * pattern covers the whole family, which is most of what a calendar answer
 * says. What is left is words, and the words here are the ones that turn up in
 * front of a time or a name.
 *
 * In front of the token, anything that is not a letter or a digit — so a
 * space, but a dash, a slash or a bracket too. A shortening does not stop
 * being one because of what is written against it: `9:00—e.g.` and `Slot-no.`
 * both end in a token this list already has a rule for, and a class that only
 * knows about whitespace refuses to look at either.
 *
 * Case is load-bearing on `no`: lowercase is a room number and `No` is the
 * answer to "am I free at three?". `al` is lowercase-only for the same reason
 * — `Al.` is a name — and the titles are capital-only, which is how a title is
 * written. Everything in the two lines after them takes either case, because
 * the match is literal and `Oak st.` and `Oak St.` are the same abbreviation,
 * and because `Approx.` and `Etc.` can open an answer. The months and weekdays
 * take only the capital on purpose: `sat`, `sun`, `mar` and `wed` are ordinary
 * English words, and "you could eat in the sun." would stop ending a sentence.
 *
 * A wrong entry costs a line that runs into the next sentence and takes the
 * `text-overflow: ellipsis` on `.cal-said`. That is the cheap mistake, and it
 * is why `etc` is here even though it does sometimes end a sentence. A missing
 * entry is the expensive one, and it does not cost "one word": the walk
 * returns at the first stop nothing explains, so before there was a forward
 * test an abbreviation at the head of an answer was the whole of what reached
 * the card — "Sept. 12 is clear, so you are free." arrived as "Sept." (#164).
 * What bounds that now is the forward test in `calSaid`, not this list.
 */
const CAL_ABBREV = new RegExp(
  "(?:^|[^A-Za-z0-9])(?:"
  + "[A-Za-z]|(?:[A-Za-z]\\.)+[A-Za-z]"
  + "|Mr|Mrs|Ms|Dr|Prof|Rev|Jr|Sr"
  + "|[Ss]t|[Mm]t|[Aa]ve|[Rr]d|[Dd]ept|[Ii]nc|[Ll]td|[Cc]o"
  + "|[Vv]s|[Ee]tc|[Aa]pprox|[Aa]ppt|[Mm]in|[Mm]ins|[Hh]r|[Hh]rs"
  + "|no|al"
  + "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
  + "|Mon|Tue|Tues|Wed|Weds|Thu|Thur|Thurs|Fri|Sat|Sun"
  + ")$",
);

/**
 * One sentence of the sub-agent's answer, as plain text.
 *
 * The grid is the day and it cannot say what was actually asked: "you are free
 * after three" is not derivable from a list of times by anybody standing three
 * metres away. So the card keeps one line of the prose and drops the rest,
 * which the spoken reply has already carried — the whole complaint behind #129
 * is that the rest was being printed a second time underneath the grid.
 *
 * One sentence, because the slot is one line. A four-sentence answer clipped
 * with an ellipsis reads as a card that broke; the first sentence of it reads
 * as a card that chose. The terminators are both alphabets' — `。` needs no
 * space after it and `.` needs one, or `19:00.` and `3.8` end the line early.
 *
 * A space after the stop is not enough on its own, though, because an English
 * abbreviation ends in exactly that: "Dr. Tanaka at ten, then you are free"
 * came out of here as "Dr." So every candidate stop is walked rather than the
 * first one taken, and the token in front of it decides — see `CAL_ABBREV`.
 *
 * The token in front cannot decide alone, because that list is finite and a
 * calendar answer is full of shortenings nobody wrote down. So the character
 * after the stop gets a say too: a sentence does not start with a lowercase
 * letter or a digit, and "Sept. 12" and "Fri. is clear" are exactly that. Only
 * a stop that passes both tests ends the line; failing either one walks past
 * it. That is what makes a word missing from `CAL_ABBREV` cost the sentences
 * after the first rather than every word of the answer (#164).
 *
 * Both tests only ever remove a place this can stop, never add one, so the
 * residual runs the other way: a real sentence end followed by a digit — "Two
 * things on. 8am is the first." — keeps going and takes the ellipsis, which is
 * the mistake `CAL_ABBREV` already calls the cheap one.
 *
 * The markdown is stripped rather than rendered: this is one line inside a
 * grid, and a `<strong>` or a bullet in the middle of it is the paragraph
 * formatting coming back in a smaller font. A link keeps its text and loses
 * the URL, which is unreadable and unclickable at the distance this is read
 * from — the same bargain `sourceChips` makes.
 */
export function calSaid(text) {
  const flat = String(text || "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s*(?:[-*+]|\d+\.)\s+/gm, "")
    .replace(/[*_`#>]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  const term = /[。！？]|[.!?](?=\s|$)/g;
  for (let m; (m = term.exec(flat)); ) {
    // Runs of whitespace above are one space each and the ends are trimmed,
    // and the ASCII lookahead only fires on a stop with whitespace or the end
    // of the text after it — so two past an ASCII stop is the next visible
    // character, or `""`. The other three terminators return before this is
    // read, so it does not matter that no lookahead constrains them.
    const next = flat.charAt(m.index + 2);
    // `!` and `?` end no abbreviation, so they are always the end. A stop
    // inside a run of stops is an ellipsis and is not one either.
    if (m[0] !== "."
        || (flat[m.index - 1] !== "."
            && !CAL_ABBREV.test(flat.slice(0, m.index))
            && !/[\p{Ll}\p{Nd}\p{sc=Hiragana}\p{sc=Katakana}\p{sc=Han}]/u.test(next))) {
      return flat.slice(0, m.index + 1);
    }
  }
  return flat;
}

/**
 * The person's calendar, as a card rather than as a paragraph.
 *
 * A string and not an element, so the whole thing is one pure function of the
 * frame: `gwsFill` writes it into the `.md` the answer was already going into,
 * and `tools/js_check.py` drives it directly.
 *
 * Grouped by day, and only over days that have something on them — a fortnight
 * answered as fourteen headings, eleven of them empty, is a scroll bar where a
 * glance should be. The empty *answer* is the opposite case and gets the whole
 * card: "nothing on" is what the person asked and it should not have to be
 * inferred from a card with no rows in it.
 *
 * `said` is that one line, and it is inside the card rather than a paragraph
 * after it — see `calSaid`. Both shapes carry it, because an empty day is
 * still a day something can have gone wrong on: `gwsFill` puts the failure's
 * own sentence through this slot, and a grid with nothing under it saying why
 * it is short is the card that reads as an answer when it is not one. Which of
 * the two it is, the card says in `.gws.failed .cal-said` rather than here.
 *
 * `now` is an argument with a default rather than a `new Date()` inside,
 * because "which of these is happening right now" is the one thing on this
 * card that cannot be checked at all if the clock cannot be set.
 */
export function calendarHtml(agenda, said = "", now = new Date()) {
  if (!agenda || !Array.isArray(agenda.events)) return "";
  const span = calRange(agenda.from, agenda.to);
  const line = said
    ? `<div class="cal-said">${escapeHtml(said)}</div>`
    : "";
  if (!agenda.events.length) {
    return `
      <div class="cal empty">
        <span class="material-symbols-outlined">event_available</span>
        <b>${escapeHtml(t("cal_free"))}</b>
        <span class="cal-span">${escapeHtml(span)}</span>
        ${line}
      </div>`;
  }

  const pad = (n) => String(n).padStart(2, "0");
  // Built by hand rather than through `toISOString`, which is UTC and is a day
  // out for a third of the evening in Tokyo.
  const today =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const clock = `${pad(now.getHours())}:${pad(now.getMinutes())}`;

  // Run length and not grouping: a heading starts wherever the day changes
  // from the row before it. That is right for the rows this card is handed —
  // `_agenda._read` sorts them on `(day, from)` (`_agenda.py:540`) before
  // `_for_screen` cuts the card out of it, and a day-first sort delivers
  // every day in one piece — and it is a precondition this loop cannot check
  // without becoming the order-neutral grouping it is not. Hand it a day
  // split in two and it draws that day twice, with the other day in between,
  // rather than one heading in the wrong place. `tools/agenda_check.py` pins
  // the sort; `tools/js_check.py` pins this side of it.
  const days = [];
  for (const ev of agenda.events) {
    const last = days[days.length - 1];
    if (last && last.day === ev.day) last.events.push(ev);
    else days.push({ day: ev.day, events: [ev] });
  }

  const body = days
    .map((d) => `
      <div class="cal-day${d.day === today ? " today" : ""}">
        <div class="cal-date">
          <b>${escapeHtml(calDayNum(d.day))}</b>
          <span>${escapeHtml(calWeekday(d.day))}</span>
        </div>
        <div class="cal-rows">${
          d.events.map((e) => calRow(e, d.day === today ? clock : "")).join("")
        }</div>
      </div>`)
    .join("");

  const more = agenda.more
    ? `<div class="cal-more">${
        escapeHtml(t("cal_more").replace("{n}", String(agenda.more)))
      }</div>`
    : "";

  return `<div class="cal"><div class="cal-span">${
    escapeHtml(span)}</div>${line}${body}${more}</div>`;
}

/* ---------- gws ---------- */

/**
 * The Workspace sub-agent answers in Markdown, and ADK wraps a plain-string
 * tool result as `{result}`. Worth a card of its own rather than the generic
 * JSON dump: an agenda or an inbox summary is the one tool result here that is
 * meant to be read rather than glanced at, and the spoken reply is a
 * one-line version of it.
 */
export function gwsCard(data) {
  if (!data.result && !data.error) return null;
  const card = gwsPending();
  gwsFill(card, data);
  return card;
}

/**
 * The Workspace card before there is anything in it.
 *
 * gws answers out of band, and half a minute is a long time to leave the screen
 * showing nothing at all — long enough that someone reasonably concludes it did
 * not hear them. So the card goes up when the call starts, spinner and all, and
 * `gwsFill` writes the answer into it when that arrives.
 */
export function gwsPending() {
  return el(`
    <div class="card gws pending">
      ${head("workspaces", t("head_gws"))}
      <div class="card-body">
        <p class="gws-progress"></p>
        <div class="md"></div>
      </div>
      <div class="links gws-sources"></div>
    </div>`);
}

/**
 * A glyph per kind of source. A Doc, a Sheet and a Slides deck are three
 * different objects, and the difference between them is exactly the size of an
 * icon — the same bargain `WX_ICONS` and `MODE_ICONS` already make. So one
 * renderer with a type field rather than four renderers: they share a row, a
 * card and a frame, and a question that reads a spreadsheet and the mail about
 * it has one answer and wants one row under it.
 *
 * `file` is the fallback and covers everything Drive holds that is not one of
 * its own editor formats — a PDF, a .docx, an image.
 */
const SOURCE_ICONS = {
  doc: "description",
  sheet: "table_chart",
  slides: "slideshow",
  file: "draft",
  mail: "mail",
};

/**
 * How much of each half of a source line the panel shows.
 *
 * Two budgets and not one, because the two halves are not equally safe to put
 * on a kitchen wall. The name is the identity — the file, the sender — and it
 * is what a person standing across the room reads to know whether this is the
 * thing they meant; it gets the space it needs to stay recognisable. The
 * detail is the subject line or the date, and a Gmail subject is the single
 * most disclosive string this terminal handles: it is cut shorter, and it is
 * the half that goes first.
 */
const SOURCE_CLIP = { name: 40, detail: 44 };

/** `text`, or as much of it as fits with an ellipsis to say there was more. */
function clip(text, limit) {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/**
 * A sender, without the address after it.
 *
 * Gmail hands back `Alex Example <alex@example.com>`, and the half after the name
 * costs a wall panel twice over: it is the longest and least readable thing on
 * the row at three metres, and an address left on a screen in a shared room is
 * the one string here that is worth something to somebody who walks past. The
 * name is what identifies the sender to the household anyway. An address with
 * no name in front of it is shown as it is — that is all there is, and
 * inventing a name for it would be worse than printing one.
 */
function senderName(who) {
  const named = who.match(/^\s*(.*?)\s*<[^>]*>\s*$/);
  return (named?.[1] || who).replace(/^"(.*)"$/, "$1").trim() || who.trim();
}

/**
 * The row under a Workspace answer saying what it was read out of.
 *
 * Reusing `.links` and `.link` rather than inventing a third convention: that
 * is the pill row every grounding card already puts its sources in, at the
 * bottom of the card, after the body — see `linkChips`. What it is not is a
 * link. There is no pointer on this panel and a Drive URL is unreadable and
 * unclickable at the distance the thing is read from, so these are spans, and
 * nothing carries a `title`: a tooltip nobody can hover is dead weight, and it
 * would put the untruncated subject back in the DOM that the clipping above
 * exists to keep it out of.
 *
 * An empty list is not the same as no list. `sources: []` is the server saying
 * it went looking through Drive or Gmail and could name nothing, and a card
 * that answers confidently over a blank row looks exactly like a card whose
 * source row was never written — so it says so instead. No `sources` key at
 * all is a question that touched neither, and draws nothing.
 */
function sourceChips(sources) {
  if (sources === undefined || sources === null) return "";
  const items = (sources || []).filter((s) => s && s.name);
  if (!items.length) {
    return `<span class="no-source">${escapeHtml(t("gws_unsourced"))}</span>`;
  }
  return items
    .map((s) => {
      const kind = SOURCE_ICONS[s.kind] ? s.kind : "file";
      const name = kind === "mail" ? senderName(s.name) : s.name;
      const detail = clip(String(s.detail || ""), SOURCE_CLIP.detail);
      const label = clip(name, SOURCE_CLIP.name);
      return `
        <span class="link">
          <span class="material-symbols-outlined">${SOURCE_ICONS[kind]}</span>
          <span class="chip-label">${escapeHtml(label)}</span>
          ${detail ? `<span class="chip-detail">${escapeHtml(detail)}</span>` : ""}
        </span>`;
    })
    .join("");
}

/**
 * Where the sub-agent has got to, while it is still getting there.
 *
 * The tool sends a state name rather than a sentence: what it writes for the
 * model is English, because the model is what knows which language to say it
 * in, and the card has to answer that question for itself. An unknown state
 * leaves the line as it was — a new one on the Python side is a missing label
 * here, not a blank card.
 *
 * The line is gone once the card has resolved: `gwsFill` removes it, and the
 * entry stays in `pendingGws` afterwards so a late answer still lands here. So
 * a note can outlive the line it would have gone on, and a card that has
 * already said "no answer came back" is not worth walking back to "searching
 * their mail".
 */
export function gwsProgress(card, state) {
  const text = t(`gws_${state}`);
  const line = card.querySelector(".gws-progress");
  if (text && line) line.textContent = text;
}

/**
 * Put the answer in the card, or say why there is not one.
 *
 * `error` names a state rather than carrying a sentence, for the same reason
 * `gwsProgress` does: the text the tool sends is written for the model, in
 * English, and telling the model to "offer to try again" is not something to
 * print on a screen.
 *
 * `agenda` is the calendar behind a calendar answer, and it goes *instead* of
 * the prose. The grid is the day and the prose is the answer to what was
 * actually asked — "you are free after three" is not derivable from a list of
 * times by anyone standing three metres away — so the two are not the same
 * thing and the card used to carry both. But the spoken reply carries the
 * answer already, out loud, before anybody looks at the card: printing it a
 * second time under the grid is the duplicate the owner is looking at in #129.
 * So the sentence keeps its place and loses its paragraph — one line, inside
 * `.cal`, which is what `calSaid` cuts and `calendarHtml` draws.
 *
 * Only when there is a grid. `cal` is empty for every Workspace answer that is
 * not a calendar one — mail, Drive, Keep — and those are prose, are the whole
 * of the answer, and come out of here exactly as they always did.
 *
 * A failure takes the same slot with a different sentence. The run that read
 * the calendar and then ran out of clock has everything but the answer, and
 * that line is the only thing on the card saying why it is short.
 *
 * The source row is written on every path, including the failures, because a
 * card is filled more than once in life: a deadline writes `gws_lost` into it
 * and the real answer arrives afterwards, and a row left over from the answer
 * under a line saying none came is a card contradicting itself.
 */
export function gwsFill(card, data) {
  card.classList.remove("pending");
  card.querySelector(".gws-progress")?.remove();
  const md = card.querySelector(".md");
  const sources = card.querySelector(".gws-sources");
  const said = data.error ? t(`gws_${data.error}`) : calSaid(data.result);
  const cal = data.agenda ? calendarHtml(data.agenda, said) : "";
  if (cal) card.classList.add("with-cal");
  else card.classList.remove("with-cal");
  if (data.error) {
    card.classList.add("failed");
    // `textContent` where there is nothing else on the card, because the
    // string is a label and not markup; only the branch that already has HTML
    // in hand builds any.
    if (cal) md.innerHTML = cal;
    else if (data.error === "expired") {
      md.innerHTML = `<p>${escapeHtml(said)}</p><button type="button" class="text-btn primary" onclick="document.getElementById('settingsButton')?.click()">${escapeHtml(t("ui_settings"))}</button>`;
    } else md.textContent = said;
    // Nothing was read, so there is nothing to have come from: a source row
    // under "the request failed" reads as a claim that something was.
    if (sources) sources.innerHTML = "";
    return;
  }
  // Cleared, not merely skipped: this card may already be showing `gws_lost`
  // from a deadline that ran out before the answer did.
  card.classList.remove("failed");
  md.innerHTML = cal || formatMarkdown(data.result || "");
  if (sources) sources.innerHTML = sourceChips(data.sources);
}

/* ---------- plan ---------- */

/**
 * The planner's card before there is anything in it.
 *
 * Same bargain as `gwsPending`, over a longer wait: a plan is several tool
 * calls with a model turn in front of each, so the card goes up when the call
 * starts and fills in as the planner reports. `task` is what the person asked
 * for, which is all there is to show until the first `show_plan` lands.
 *
 * The steps are the progress bar. Unlike `gws`, no state names arrive for this
 * card — the planner writes its own steps, in the person's language, and
 * `plan.plan` passes `on_screen=False` so nothing competes with them.
 */
export function planPending(task = "") {
  const card = el(`
    <div class="card plan pending">
      ${head("checklist", t("head_plan"))}
      <div class="card-body">
        <p class="plan-task"></p>
        <ol class="plan-steps"></ol>
        <div class="md"></div>
      </div>
    </div>`);
  card.querySelector(".plan-task").textContent = task || t("plan_thinking");
  return card;
}

/**
 * Redraw the step list.
 *
 * Rebuilt rather than patched, because a revision is the point: the planner
 * changes what is left when a result surprises it, so steps are added, dropped
 * and reworded, and only the indexes in the frame are reliably indexes into the
 * list it just sent. The steps come from the planner already in the person's
 * language, so they are printed as they arrive — only the chrome is in
 * `STRINGS`.
 *
 * Two sets, where this took a cursor. A cursor said "everything before this is
 * done and this one is running", and the planner runs independent steps
 * together now, so they start at once and come back out of order and neither
 * half of that is true. The server sends the two lists and this draws them; a
 * step named in both is already resolved to running there.
 */
export function planUpdate(card, data) {
  if (!card || !Array.isArray(data.steps)) return;
  const running = new Set(data.running || []);
  const done = new Set(data.done || []);
  const list = card.querySelector(".plan-steps");
  list.replaceChildren();
  data.steps.forEach((step, i) => {
    const li = document.createElement("li");
    li.textContent = String(step);
    if (running.has(i)) li.className = "now";
    else if (done.has(i)) li.className = "done";
    list.appendChild(li);
  });
}

/**
 * Put the answer in the card, or say why there is not one.
 *
 * The steps stay on screen under the answer. They are the account of how it
 * was arrived at, which for a tool that ran four searches on its own is worth
 * more after the fact than during — and on a failure they are the only thing
 * saying what it did get through.
 */
export function planFill(card, data) {
  card.classList.remove("pending");
  card.querySelector(".plan-task")?.remove();
  if (data.error) {
    card.classList.add("failed");
    const md = card.querySelector(".md");
    md.textContent = t(`plan_${data.error}`);
    // `slow` covers both TOO_SLOW and TOO_SLOW_PARTIAL, and only the second
    // has anything under it: the part of the answer the planner did settle
    // before it was stopped, which is the whole reason for reporting it. The
    // other three kinds carry a `result` too, but theirs is an instruction
    // aimed at the model and it must not reach the screen — so this reads
    // `partial`, which the server sets on that one case alone, and never
    // `result`.
    if (data.partial) {
      const rest = document.createElement("div");
      rest.innerHTML = formatMarkdown(data.partial);
      md.append(rest);
    }
    return;
  }
  card.classList.remove("failed");
  card.querySelector(".md").innerHTML = formatMarkdown(data.result || "");
}

/** A plan answer that outlived its card — see `gwsCard`. */
export function planCard(data) {
  if (!data.result && !data.error) return null;
  const card = planPending();
  planUpdate(card, data);
  planFill(card, data);
  return card;
}

/**
 * The browser, live. Unlike every other card this one is built once and then
 * updated in place: the browse tool runs for the better part of a minute, and a
 * still frame of whatever page it is on is the only honest progress bar for
 * work nobody can otherwise see. `browseUpdate` is called on every step frame
 * and once more when the answer lands.
 */
export function browseCard(data) {
  const card = el(`
    <div class="card browse">
      ${head("travel_explore", t("head_browse"))}
      <div class="card-body">
        <div class="browse-shot"><img alt="" /></div>
        <p class="browse-status"></p>
        <div class="md"></div>
      </div>
    </div>`);
  card.querySelector(".browse-status").textContent = data.task || "";
  return card;
}

export function browseUpdate(card, data) {
  if (!card) return;
  const img = card.querySelector(".browse-shot img");
  if (data.shot) {
    img.src = data.shot;
    card.querySelector(".browse-shot").classList.add("has-shot");
  }

  const status = card.querySelector(".browse-status");
  if (data.phase === "done" && data.lost) {
    // Not a frame from the server: the front end writes this one itself when
    // the session the job belonged to is gone, so `done` will never arrive.
    // The step line is left where it got to, because the card stays on screen
    // and how far it reached is the only thing on it that still says anything.
    card.classList.add("done");
    card.querySelector(".md").innerHTML = t("browse_lost");
  } else if (data.phase === "done") {
    card.classList.add("done");
    status.textContent = host(data.url);
    // An answer, or the one case where there is none: the job crashed and the
    // server sent a bare `done` to stop the spinner. The label is written here
    // rather than sent, because the card is bilingual and the server has no
    // business putting English on it.
    card.querySelector(".md").innerHTML =
      data.answer ? formatMarkdown(data.answer) : t("browse_failed");
  } else {
    status.textContent = `${t("browse_step").replace("{n}", data.step)} · ${host(data.url)}`;
  }
}

function host(url) {
  try {
    return new URL(url).host;
  } catch {
    return "";
  }
}

/* ---------- remember ---------- */

/**
 * What became of a fact the household asked the terminal to keep.
 *
 * The one card whose whole job is acknowledgement, so it says what happened in
 * words and shows none of the dict it is built from. `why` in particular is
 * never drawn: both decline strings (`memory.py:354`, `:357`) are instructions
 * aimed at the model — "say it in your answer instead" — and this is a kitchen
 * panel, not a console. Same ruling `planFill` makes about `plan`'s four error
 * kinds, for the same reason.
 *
 * `topic` is drawn, because it is the model's own short handle for the thing —
 * "the blue car" — and it is the only part of the answer that says *which*
 * fact landed. `known` and `dropped` are the count under it: `dropped` only
 * when it is not zero, since "0 dropped" is a sentence about nothing, and it is
 * shown at all because a fact quietly pushed off the end is the one thing here
 * that is news.
 */
export function rememberCard(data) {
  if (!data.kept) {
    return el(`
      <div class="card remember failed">
        ${head("bookmark_added", t("head_remember"))}
        <div class="card-body">
          <div class="md">${escapeHtml(t("remember_declined"))}</div>
        </div>
      </div>`);
  }
  const counts = [t("remember_known").replace("{n}", data.known ?? 0)];
  if (data.dropped) {
    counts.push(t("remember_dropped").replace("{n}", data.dropped));
  }
  return el(`
    <div class="card remember">
      ${head("bookmark_added", t("head_remember"))}
      <div class="card-body">
        <div class="md">${escapeHtml(data.topic || "")}</div>
        <div class="remember-count">${escapeHtml(counts.join(" · "))}</div>
      </div>
    </div>`);
}

export function calculateCard(data) {
  if (data.error) {
    return el(`
      <div class="card calculate failed">
        ${head("calculate", t("head_calculate"))}
        <div class="card-body">
          <div class="md">${escapeHtml(data.expression || "")}</div>
          <div class="calculate-error">${escapeHtml(data.error)}</div>
        </div>
      </div>`);
  }
  return el(`
    <div class="card calculate">
      ${head("calculate", t("head_calculate"))}
      <div class="card-body">
        <div class="calculate-expr">${escapeHtml(data.expression || "")}</div>
        <div class="calculate-result">${escapeHtml(String(data.formatted ?? data.result ?? ""))}</div>
      </div>
    </div>`);
}

/* ---------- fallback ---------- */

export function genericCard(name, content) {
  const body = JSON.stringify(content, null, 2).slice(0, 800);
  return el(`
    <div class="card">
      ${head("terminal", name)}
      <div class="card-body"><div class="md"><code>${escapeHtml(body)}</code></div></div>
    </div>`);
}

/* ---------- image preview modal (lightbox) ---------- */

export function openImageModal(src, title = "") {
  let modal = typeof document !== "undefined" ? document.getElementById("imageModal") : null;
  if (!modal && typeof document !== "undefined" && document.createElement) {
    modal = el(`
      <div class="image-modal hidden" id="imageModal" role="dialog" aria-modal="true" aria-label="Image preview">
        <div class="image-modal-backdrop" id="imageModalBackdrop"></div>
        <div class="image-modal-dialog">
          <button type="button" class="image-modal-close" id="imageModalClose" aria-label="Close" title="Close">✕</button>
          <div class="image-modal-frame">
            <img class="image-modal-img" id="imageModalImg" src="" alt="" />
          </div>
          <div class="image-modal-caption" id="imageModalCaption"></div>
        </div>
      </div>`);
    document.body.appendChild(modal);
    modal.querySelector("#imageModalClose")?.addEventListener("click", closeImageModal);
    modal.querySelector("#imageModalBackdrop")?.addEventListener("click", closeImageModal);
  }
  if (!modal) return;
  const img = modal.querySelector("#imageModalImg") || document.getElementById("imageModalImg");
  const cap = modal.querySelector("#imageModalCaption") || document.getElementById("imageModalCaption");
  if (img) {
    img.src = src;
    img.alt = title;
  }
  if (cap) {
    cap.textContent = title;
  }
  modal.classList.remove("hidden");
}

export function closeImageModal() {
  if (typeof document === "undefined") return;
  const modal = document.getElementById("imageModal");
  if (modal) {
    modal.classList.add("hidden");
    const img = document.getElementById("imageModalImg");
    if (img) img.src = "";
  }
}

/**
 * Whether the terminal is running in kiosk mode (?kiosk=1).
 * In kiosk mode, the browser runs without chrome (no back button, no tabs),
 * so external links and uncontained navigations must be inhibited.
 */
export function isKiosk() {
  try {
    return typeof window !== "undefined" && new URLSearchParams(window?.location?.search || "").get("kiosk") === "1";
  } catch {
    return false;
  }
}

/* ---------- home cards ---------- */

export function homeSummaryCard(data) {
  const rooms = data.rooms || [];
  const roomRows = rooms.map(r => {
    const devs = (r.devices || []).map(d => {
      const stateCls = d.state === "on" ? "state-on" : (d.state === "off" ? "state-off" : "state-other");
      const stateLabel = d.detail ? `${escapeHtml(d.state || "")} (${escapeHtml(d.detail)})` : escapeHtml(d.state || "");
      const icon = d.icon || (d.state === "on" ? "lightbulb" : "lightbulb_outline");
      return `
        <div class="home-device-row">
          <span class="material-symbols-outlined home-device-icon">${escapeHtml(icon)}</span>
          <span class="home-device-name">${escapeHtml(d.name || "")}</span>
          <span class="home-device-state ${escapeAttr(stateCls)}">${stateLabel}</span>
        </div>`;
    }).join("");
    return `
      <div class="home-room">
        <div class="home-room-title">${escapeHtml(r.name || "")}</div>
        <div class="home-device-list">${devs}</div>
      </div>`;
  }).join("");

  return el(`
    <div class="card home-card home-summary">
      ${head("home", t("head_home_summary"))}
      <div class="card-body">
        <div class="home-rooms">${roomRows}</div>
      </div>
    </div>`);
}

export function homeTimelineCard(data) {
  const events = data.events || [];
  const eventRows = events.map(ev => `
    <div class="home-timeline-item">
      <span class="home-timeline-time">${escapeHtml(ev.time || "")}</span>
      <span class="home-timeline-dot"></span>
      <div class="home-timeline-desc">
        <span class="home-timeline-dev">${escapeHtml(ev.device || "")}</span>
        <span class="home-timeline-event">${escapeHtml(ev.event || "")}</span>
      </div>
    </div>`).join("");

  return el(`
    <div class="card home-card home-timeline">
      ${head("history", t("head_home_timeline"))}
      <div class="card-body">
        <div class="home-timeline-list">${eventRows}</div>
      </div>
    </div>`);
}

