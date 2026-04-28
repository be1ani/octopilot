// Minimal whitelist-based HTML sanitizer.
// Trade-off: we don't ship DOMPurify, so we restrict output to a small safe
// subset of tags/attributes and rely on the browser's DOMParser to tokenize.

const ALLOWED_TAGS = new Set([
  "a",
  "b",
  "strong",
  "i",
  "em",
  "u",
  "p",
  "br",
  "ul",
  "ol",
  "li",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "span",
  "div",
  "code",
  "pre",
  "blockquote",
  "hr",
]);

const ALLOWED_ATTRS_BY_TAG = {
  a: ["href", "title"],
};

const URL_SAFE_PROTOCOLS = /^(https?:|mailto:|tel:)/i;

function isSafeUrl(value) {
  if (!value) return false;
  const trimmed = String(value).trim();
  if (trimmed.startsWith("/") || trimmed.startsWith("#")) return true;
  return URL_SAFE_PROTOCOLS.test(trimmed);
}

function sanitizeNode(node, outDoc) {
  if (node.nodeType === Node.TEXT_NODE) {
    return outDoc.createTextNode(node.nodeValue || "");
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return null;

  const tag = node.tagName.toLowerCase();
  if (!ALLOWED_TAGS.has(tag)) {
    // Skip the element, but keep its (sanitized) children inline.
    const frag = outDoc.createDocumentFragment();
    for (const child of Array.from(node.childNodes)) {
      const sanitized = sanitizeNode(child, outDoc);
      if (sanitized) frag.appendChild(sanitized);
    }
    return frag;
  }

  const el = outDoc.createElement(tag);
  const allowedAttrs = ALLOWED_ATTRS_BY_TAG[tag] || [];
  for (const attr of Array.from(node.attributes || [])) {
    const name = attr.name.toLowerCase();
    if (!allowedAttrs.includes(name)) continue;
    if (name === "href" && !isSafeUrl(attr.value)) continue;
    el.setAttribute(name, attr.value);
  }
  if (tag === "a") {
    el.setAttribute("target", "_blank");
    el.setAttribute("rel", "noopener noreferrer");
  }
  for (const child of Array.from(node.childNodes)) {
    const sanitized = sanitizeNode(child, outDoc);
    if (sanitized) el.appendChild(sanitized);
  }
  return el;
}

export function sanitizeHtml(input) {
  if (input == null) return "";
  const raw = String(input);
  // Pre-normalize literal escape sequences that sometimes show up in JSON data.
  const normalized = raw.replace(/\\r/g, "").replace(/\\n/g, "\n");

  const parser = new DOMParser();
  const doc = parser.parseFromString(`<div>${normalized}</div>`, "text/html");
  const root = doc.body.firstChild;
  if (!root) return "";

  const outDoc = document.implementation.createHTMLDocument("");
  const container = outDoc.createElement("div");
  for (const child of Array.from(root.childNodes)) {
    const sanitized = sanitizeNode(child, outDoc);
    if (sanitized) container.appendChild(sanitized);
  }
  return container.innerHTML;
}
