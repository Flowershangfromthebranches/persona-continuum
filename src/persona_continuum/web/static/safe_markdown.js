// Safe markdown renderer for protocol chat message bodies.
// UMD pure module shared by the browser controller and Node tests: the input
// is ALWAYS HTML-escaped first, then a strict whitelist subset is applied to
// the escaped text. Raw HTML never passes through, so no <script> (or any
// unescaped "<") can reach the DOM from message content.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) {
    root.PersonaSafeMarkdown = api;
    root.renderSafeMessageContent = api.renderSafeMessageContent;
  }
})(typeof window !== "undefined" ? window : null, function () {
  // Headings map to compact chat sizes: #/## → h3, ### → h4.
  const HEADING_TAGS = { 1: "h3", 2: "h3", 3: "h4" };
  const SENTINEL = "\u0000";

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Pathological one-liners (a huge single line, or one lone unmatched
  // opener) can make the backtick/asterisk scans quadratic.  Overlong lines
  // skip those rules entirely; normal lines use a cheap count pre-check so
  // rules that cannot possibly match never scan.  Everyday inputs render
  // exactly as before.
  const INLINE_RULE_MAX_LENGTH = 20000;

  function countChar(value, ch) {
    let count = 0;
    for (let i = 0; i < value.length; i += 1) {
      if (value[i] === ch) count += 1;
    }
    return count;
  }

  // Captured link groups come from already-escaped text, so entities may
  // appear (e.g. "&amp;").  Decode the whitelist entities back to raw
  // characters, then escape exactly once: label and href are guaranteed
  // single-escaped and can never break out of the attribute.
  function unescapeHtml(value) {
    return String(value)
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, "\"")
      .replace(/&#39;/g, "'")
      .replace(/&amp;/g, "&");
  }

  function renderSafeLink(label, url) {
    const safeLabel = escapeHtml(unescapeHtml(label));
    const safeUrl = escapeHtml(unescapeHtml(url));
    return `<a href="${safeUrl}" rel="noopener noreferrer" target="_blank">${safeLabel}</a>`;
  }

  // Inline whitelist applied to ALREADY-ESCAPED text. Inline code spans are
  // lifted out first so emphasis markers inside them stay literal. Every
  // replacement uses a function callback so "$" sequences in user content
  // can never be re-interpreted as replacement patterns.
  function renderInline(escaped) {
    const codeSpans = [];
    let out = String(escaped).replace(/\u0000/g, "");
    const overlong = out.length > INLINE_RULE_MAX_LENGTH;
    const backticks = overlong ? 0 : countChar(out, "`");
    if (backticks > 1) {
      out = out.replace(/`([^`]+)`/g, (match, code) => {
        codeSpans.push(code);
        return `${SENTINEL}${codeSpans.length - 1}${SENTINEL}`;
      });
    }
    // Safe links: only http/https URLs become anchors; any other scheme
    // keeps rendering as plain escaped text.  Runs after code-span hoisting
    // so link syntax inside inline code stays literal.
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (match, label, url) =>
      renderSafeLink(label, url)
    );
    const asterisks = overlong ? 0 : countChar(out, "*");
    if (asterisks > 1) {
      out = out.replace(/\*\*([^*]+)\*\*/g, (match, body) => `<strong>${body}</strong>`);
      out = out.replace(/(^|[^*])\*([^\s*][^*]*?)\*(?!\*)/g, (match, lead, body) => `${lead}<em>${body}</em>`);
    }
    out = out.replace(
      new RegExp(`${SENTINEL}(\\d+)${SENTINEL}`, "g"),
      (match, index) => `<code>${codeSpans[Number(index)] || ""}</code>`
    );
    return out;
  }

  function renderSafeMessageContent(content) {
    if (content === null || content === undefined) return "";
    let text = content;
    if (typeof text !== "string") {
      if (typeof text === "number" || typeof text === "boolean") text = String(text);
      else return "";
    }
    if (!text.trim()) return "";

    const blocks = [];
    let listType = null;
    let listItems = [];
    let paraLines = [];

    const flushList = () => {
      if (!listType) return;
      blocks.push(`<${listType}>${listItems.map((item) => `<li>${renderInline(item)}</li>`).join("")}</${listType}>`);
      listType = null;
      listItems = [];
    };
    const flushPara = () => {
      if (!paraLines.length) return;
      blocks.push(`<p>${paraLines.map(renderInline).join("\n")}</p>`);
      paraLines = [];
    };

    for (const rawLine of text.split(/\r\n|\n|\r/)) {
      const line = rawLine.trim();
      if (!line) {
        flushList();
        flushPara();
        continue;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        flushList();
        flushPara();
        const tag = HEADING_TAGS[heading[1].length] || "h3";
        blocks.push(`<${tag}>${renderInline(escapeHtml(heading[2]))}</${tag}>`);
        continue;
      }
      const unordered = line.match(/^-\s+(.+)$/);
      if (unordered) {
        flushPara();
        if (listType && listType !== "ul") flushList();
        listType = "ul";
        listItems.push(escapeHtml(unordered[1]));
        continue;
      }
      const ordered = line.match(/^\d{1,9}[.)]\s+(.+)$/);
      if (ordered) {
        flushPara();
        if (listType && listType !== "ol") flushList();
        listType = "ol";
        listItems.push(escapeHtml(ordered[1]));
        continue;
      }
      flushList();
      paraLines.push(escapeHtml(line));
    }
    flushList();
    flushPara();
    return blocks.join("");
  }

  return { renderSafeMessageContent, escapeHtml };
});
