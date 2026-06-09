/**
 * BrainTwin Popup
 *
 * Owns the Capture tab (count + status + toggle) and the tab switcher.
 * The Remember tab is handled in recall.js — both scripts are loaded
 * by popup.html. We coexist by binding only to the elements each
 * script owns; the only shared surface is the tab switcher below.
 *
 * Capture tab state:
 *   - Reads enabled + today's capture count straight from
 *     chrome.storage.local and listens for changes so the count
 *     updates while the popup is open.
 *   - Toggle button just writes back to storage — the content script
 *     and background worker react via storage.onChanged.
 *
 * Tab switcher state:
 *   - The active tab is persisted to chrome.storage.local under
 *     `activeTab` ("capture" | "remember") so reopening the popup
 *     drops you back into the same view you were last using.
 */

function todayKey() {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

document.addEventListener("DOMContentLoaded", async () => {
  // ---- Tab switcher ------------------------------------------------
  // Wire BEFORE we render anything else so the first-paint shows the
  // right view (no flash of the wrong tab).
  const tabButtons = Array.from(document.querySelectorAll(".tab"));
  const views = {
    capture: document.getElementById("viewCapture"),
    remember: document.getElementById("viewRemember"),
  };

  function activateTab(name) {
    if (!views[name]) return;
    for (const btn of tabButtons) {
      btn.classList.toggle("active", btn.dataset.tab === name);
    }
    for (const [viewName, el] of Object.entries(views)) {
      el.classList.toggle("active", viewName === name);
    }
    // Persist for next popup open.
    chrome.storage.local.set({ activeTab: name }).catch(() => {});
    // When switching INTO Remember, hand focus to the search input
    // so the user can just type.
    if (name === "remember") {
      const input = document.getElementById("recallInput");
      if (input) setTimeout(() => input.focus(), 0);
    }
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  }

  // Hydrate the active tab from storage.
  const { activeTab } = await chrome.storage.local.get("activeTab");
  if (activeTab && views[activeTab]) {
    activateTab(activeTab);
  }

  // ---- Capture view: count, status, toggle (unchanged from v0.1) ---
  const countEl = document.getElementById("count");
  const statusEl = document.getElementById("status");
  const toggleBtn = document.getElementById("toggleBtn");

  // Initial render from storage
  const stored = await chrome.storage.local.get(["enabled", "captures"]);
  renderCapture(stored.enabled !== false, stored.captures);

  // Live updates while popup is open
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if ("enabled" in changes || "captures" in changes) {
      chrome.storage.local.get(["enabled", "captures"]).then((s) => {
        renderCapture(s.enabled !== false, s.captures);
      });
    }
  });

  // Toggle button writes new state to storage
  toggleBtn.addEventListener("click", async () => {
    const { enabled } = await chrome.storage.local.get("enabled");
    const next = enabled === false; // flip
    await chrome.storage.local.set({ enabled: next });
  });

  function renderCapture(enabled, captures) {
    const today = todayKey();
    const count =
      captures && captures.date === today ? captures.count : 0;
    countEl.textContent = String(count);

    if (enabled) {
      statusEl.textContent = "Active";
      statusEl.style.color = "#4ade80";
      toggleBtn.textContent = "Pause Capture";
      toggleBtn.className = "toggle on";
    } else {
      statusEl.textContent = "Paused";
      statusEl.style.color = "#f87171";
      toggleBtn.textContent = "Resume Capture";
      toggleBtn.className = "toggle off";
    }
  }
});
