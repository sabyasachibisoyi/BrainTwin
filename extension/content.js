/**
 * BrainTwin Content Script
 * Tracks dwell time on pages and captures content when threshold is met.
 *
 * Reads the on/off state from chrome.storage.local (set by the popup).
 * Listens for storage changes so an in-flight dwell timer is cancelled
 * the moment the user hits "Pause Capture".
 */

const BACKEND_URL = "http://127.0.0.1:8000";
const DWELL_THRESHOLD_MS = 30000; // 30 seconds

let startTime = null;
let captured = false;
let dwellTimer = null;
let enabled = true;          // mirrored from chrome.storage.local
let trackingActive = false;  // are we currently counting dwell on this tab?

// --- Dwell Time Tracking ---

function startTracking() {
  if (!enabled) return;
  if (trackingActive) return;
  trackingActive = true;
  startTime = Date.now();
  captured = false;

  dwellTimer = setTimeout(() => {
    if (!captured && enabled) {
      capturePageContent();
    }
  }, DWELL_THRESHOLD_MS);
}

function stopTracking() {
  if (dwellTimer) {
    clearTimeout(dwellTimer);
    dwellTimer = null;
  }
  trackingActive = false;
}

// Track visibility changes (tab switches, minimize)
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopTracking();
  } else {
    startTracking();
  }
});

// React to popup toggling enabled / disabled
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !("enabled" in changes)) return;
  enabled = changes.enabled.newValue !== false;
  if (!enabled) {
    // Pause: kill any in-flight dwell timer on this tab.
    stopTracking();
    console.log("[BrainTwin] capture paused");
  } else {
    // Resume: start tracking this tab if it's visible.
    if (!document.hidden && !shouldSkip(window.location.href)) {
      startTracking();
      console.log("[BrainTwin] capture resumed");
    }
  }
});

// --- Platform Detection ---

function detectPlatform(url) {
  const hostname = new URL(url).hostname;
  if (hostname.includes("youtube.com")) return "youtube";
  if (hostname.includes("twitter.com") || hostname.includes("x.com")) return "twitter";
  if (hostname.includes("instagram.com")) return "instagram";
  if (hostname.includes("reddit.com")) return "reddit";
  if (hostname.includes("whatsapp.com")) return "whatsapp";
  if (hostname.includes("linkedin.com")) return "linkedin";
  if (hostname.includes("facebook.com")) return "facebook";
  return "general";
}

// --- Content Extraction ---

function extractMainText() {
  // Try to get the main article content
  const selectors = [
    "article",
    '[role="main"]',
    ".post-content",
    ".article-body",
    ".entry-content",
    "main",
  ];

  for (const selector of selectors) {
    const el = document.querySelector(selector);
    if (el && el.innerText.length > 200) {
      return el.innerText.trim();
    }
  }

  // Fallback: get body text (cleaned up)
  return document.body.innerText.substring(0, 10000).trim();
}

function extractImages() {
  // Capture significant images on the page (memes, infographics, etc.)
  const images = [];
  const imgElements = document.querySelectorAll("img");

  for (const img of imgElements) {
    // Only capture reasonably sized images (likely content, not icons)
    if (img.naturalWidth >= 200 && img.naturalHeight >= 200) {
      images.push(img.src);
      if (images.length >= 5) break; // Cap at 5 images
    }
  }

  return images;
}

// --- Capture & Send ---

async function capturePageContent() {
  if (captured) return;
  if (!enabled) return; // Re-check in case the user paused during the dwell
  captured = true;

  const url = window.location.href;
  const platform = detectPlatform(url);
  const dwellTime = Math.floor((Date.now() - startTime) / 1000);

  const payload = {
    url: url,
    title: document.title,
    platform: platform,
    content_type: "article",
    text: extractMainText(),
    images: extractImages(),
    timestamp: new Date().toISOString(),
    dwell_time_seconds: dwellTime,
    metadata: {
      description:
        document
          .querySelector('meta[name="description"]')
          ?.getAttribute("content") || "",
      author:
        document
          .querySelector('meta[name="author"]')
          ?.getAttribute("content") || "",
    },
  };

  try {
    const response = await fetch(`${BACKEND_URL}/capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (response.ok) {
      // Notify background script of successful capture
      chrome.runtime.sendMessage({
        type: "CAPTURE_SUCCESS",
        title: payload.title,
        platform: platform,
      });
      console.log("[BrainTwin] Content captured:", payload.title);
    } else {
      console.warn("[BrainTwin] Backend rejected capture:", response.status);
    }
  } catch (error) {
    console.log("[BrainTwin] Backend not running, skipping capture.");
  }
}

// --- Skip List ---

const SKIP_DOMAINS = [
  "mail.google.com",
  "online.citi.com",
  "chase.com",
  "bankofamerica.com",
  "accounts.google.com",
  "login.",
  "signin.",
  "auth.",
];

function shouldSkip(url) {
  return SKIP_DOMAINS.some((domain) => url.includes(domain));
}

// --- Initialize ---

(async () => {
  try {
    const stored = await chrome.storage.local.get("enabled");
    enabled = stored.enabled !== false; // default true if unset
  } catch {
    enabled = true;
  }

  if (enabled && !shouldSkip(window.location.href)) {
    startTracking();
  }
})();
