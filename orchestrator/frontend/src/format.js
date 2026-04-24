/**
 * Small collection of formatting helpers shared across UI components.
 */

/**
 * Format an ISO timestamp (or Date) as a compact relative string:
 *   "just now", "3 min ago", "2 h ago", "yesterday",
 *   "3 days ago", or the absolute date for older entries.
 *
 * The `now` parameter is exposed so callers that re-render on a timer can pass
 * the same Date instance to keep the output stable for a render.
 */
export function formatRelativeTime(iso, now = new Date()) {
  if (!iso) return "";
  const d = iso instanceof Date ? iso : new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);

  const diffMs = now.getTime() - d.getTime();
  const sec = Math.round(diffMs / 1000);
  const absSec = Math.abs(sec);
  const future = sec < 0;

  if (absSec < 45) return future ? "in a moment" : "just now";

  const min = Math.round(absSec / 60);
  if (min < 60) return future ? `in ${min} min` : `${min} min ago`;

  const hr = Math.round(absSec / 3600);
  if (hr < 24) return future ? `in ${hr} h` : `${hr} h ago`;

  // Calendar-day aware for 1–6 days so "yesterday" feels natural around midnight.
  const startOfDay = (x) => {
    const c = new Date(x);
    c.setHours(0, 0, 0, 0);
    return c.getTime();
  };
  const dayDiff = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
  if (dayDiff === 1) return "yesterday";
  if (dayDiff === -1) return "tomorrow";
  if (dayDiff > 0 && dayDiff < 7) return `${dayDiff} days ago`;
  if (dayDiff < 0 && dayDiff > -7) return `in ${-dayDiff} days`;

  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

/** Render only the hostname of a URL, or "" if it is not parseable. */
export function urlHostname(url) {
  if (!url) return "";
  try {
    const u = new URL(url);
    return u.hostname.replace(/^www\./, "");
  } catch (_) {
    return "";
  }
}
