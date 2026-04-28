/**
 * BrainTwin Background Service Worker
 *
 * State (single source of truth) lives in chrome.storage.local so it
 * survives the MV3 service worker dying after ~30s of idle:
 *
 *   {
 *     enabled: boolean,                   // default true
 *     captures: { count: number, date: "YYYY-MM-DD" }   // resets on new day
 *   }
 *
 * The popup reads from storage directly. Content scripts read `enabled`
 * directly and watch for changes. This file only handles two things:
 *   (a) initializing defaults on install / startup
 *   (b) atomically incrementing the count when a capture succeeds, then
 *       reflecting the new total on the toolbar badge.
 */

const DEFAULTS = {
  enabled: true,
  captures: { count: 0, date: todayKey() },
};

function todayKey() {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

// --- Lifecycle: ensure defaults exist and badge is correct ---

async function ensureDefaults() {
  const stored = await chrome.storage.local.get(["enabled", "captures"]);
  const patch = {};
  if (typeof stored.enabled === "undefined") patch.enabled = DEFAULTS.enabled;
  if (!stored.captures) patch.captures = DEFAULTS.captures;
  if (Object.keys(patch).length) await chrome.storage.local.set(patch);
  await refreshBadge();
}

chrome.runtime.onInstalled.addListener(ensureDefaults);
chrome.runtime.onStartup.addListener(ensureDefaults);

// --- Badge ---

async function refreshBadge() {
  const { captures, enabled } = await chrome.storage.local.get([
    "captures",
    "enabled",
  ]);
  const today = todayKey();
  const count = captures && captures.date === today ? captures.count : 0;

  chrome.action.setBadgeText({ text: count > 0 ? String(count) : "" });
  chrome.action.setBadgeBackgroundColor({
    color: enabled === false ? "#666" : "#6366f1",
  });
}

// Repaint badge whenever storage changes (so the popup toggle is reflected
// in the badge color immediately, and the count updates live).
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if ("captures" in changes || "enabled" in changes) {
    refreshBadge();
  }
});

// --- Capture success → atomic count bump ---

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "CAPTURE_SUCCESS") return false;

  // Async work — return true to keep the message channel open.
  (async () => {
    const { captures } = await chrome.storage.local.get("captures");
    const today = todayKey();
    const next =
      captures && captures.date === today
        ? { count: captures.count + 1, date: today }
        : { count: 1, date: today };
    await chrome.storage.local.set({ captures: next });
    sendResponse({ ok: true, count: next.count });
  })();

  return true;
});
