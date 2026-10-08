/**
 * A small Markdown subset — enough for what the grounding tools emit
 * (bold, italic, code, links, bullets, numbers, short headings).
 * Everything is HTML-escaped first, so tool output can never inject markup.
 * Which escape depends on where the value lands: `escapeHtml` for text
 * between tags, `escapeAttr` for anything interpolated inside an attribute.
 */

/** Escape for text position — between tags, not inside an attribute. */
export function escapeHtml(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

/**
 * Escape for an attribute value: `escapeHtml` plus the quotes.
 *
 * The `textContent` → `innerHTML` round trip serialises a text node, and a
 * text node needs no quote escaping, so `"` and `'` come back untouched.
 * Interpolated into `title="…"` that ends the attribute early and the rest of
 * a page title becomes markup — `onmouseover=` on a chip, `onerror=` on an
 * `<img>`. Kept separate from `escapeHtml` rather than folded into it because
 * most of the thirty-odd callers are in text position, where `&quot;` would
 * show up on screen as a visible regression.
 */
export function escapeAttr(str) {
  return escapeQuotes(escapeHtml(str));
}

/**
 * The quotes on their own, for a value that has already been escaped once.
 *
 * `formatMarkdown` is the only caller: it escapes the whole text up front and
 * then reinstates a URL out of a capture, so by then `&` has become `&amp;`
 * and running `escapeAttr` over it would give `&amp;amp;` — a query string
 * with `&d=2` in it would arrive at the server as `&amp;d=2`.
 */
function escapeQuotes(str) {
  return str.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

export function formatMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/&lt;\s*\/?\s*br\s*\/?\s*&gt;/gi, "<br>");

  // Headings collapse to one rank — a card is too small for a hierarchy.
  html = html.replace(/^#{1,6}\s+(.+?)\s*#*\s*$/gm, "<h4>$1</h4>");

  // Bold before italic, so a single asterisk can't eat half a bold pair.
  html = html.replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/__([^_\n]+?)__/g, "<strong>$1</strong>");
  html = html.replace(
    /(^|[\s(>])\*([^*\s][^*\n]*?[^*\s]|[^*\s])\*(?!\*)/g,
    "$1<em>$2</em>",
  );
  html = html.replace(
    /(^|[\s(>])_([^_\s][^_\n]*?[^_\s]|[^_\s])_(?!_)/g,
    "$1<em>$2</em>",
  );

  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");

  // Links: http(s) only, so a `javascript:` URL can't slip through. The URL
  // lands in an attribute and the pass at the top did not escape its quotes,
  // so it gets `escapeQuotes` here — `[^\s)]+` admits a `"`, and the scheme
  // guard says nothing about what follows it. Quotes only, because the `&`
  // and `<` in it are already escaped. The link text is left alone: it went
  // through the same pass and by now may hold a `<strong>` this is meant to
  // keep.
  const kiosk = typeof window !== "undefined" && new URLSearchParams(window?.location?.search || "").get("kiosk") === "1";
  html = html.replace(
    /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, text, uri) => {
      if (kiosk) {
        return `<a href="#" class="kiosk-link kiosk-citation" data-uri="${escapeQuotes(uri)}" data-title="${escapeQuotes(text)}">${text}</a>`;
      }
      return `<a href="${escapeQuotes(uri)}" target="_blank" rel="noopener">${text}</a>`;
    },
  );

  // Grounding citation markers from Gemini/Search (e.g. [1.32], [1.33]).
  html = html.replace(/\s*\[\d+\.\d+\]/g, "");

  // Mark list items with a sentinel class, group them, then drop the class.
  html = html.replace(
    /^(?:[-*•]\s+|(?=\p{Extended_Pictographic}\s*<strong>))(.+)$/gmu,
    '<li class="b">$1</li>',
  );
  html = html.replace(/^\d+\.\s+(.+)$/gm, '<li class="n">$1</li>');
  // The newlines between items are swallowed only when another item of the
  // same kind follows, so a blank line between items keeps them in one list
  // (otherwise every item became its own <ol> and renumbered itself to 1)
  // while the blank line after the last one still ends the block.
  const run = (cls) =>
    new RegExp(`(?:<li class="${cls}">.*?</li>(?:\\n+(?=<li class="${cls}">))?)+`, "g");
  // The newlines have to go too, or the paragraph split below would cut the
  // list in half at the first blank line it kept.
  const join = (m, cls, tag) =>
    `<${tag}>${m.replace(new RegExp(` class="${cls}"`, "g"), "").replace(/\n+/g, "")}</${tag}>`;
  html = html.replace(run("b"), (m) => join(m, "b", "ul"));
  html = html.replace(run("n"), (m) => join(m, "n", "ol"));

  return html
    .split(/\n{2,}/)
    .map((block) => {
      const trimmed = block.trim();
      if (!trimmed) return "";
      if (/^<(ul|ol|h[1-6])/.test(trimmed)) return trimmed;
      return `<p>${trimmed.replace(/\n/g, "<br>")}</p>`;
    })
    .filter(Boolean)
    .join("");
}
