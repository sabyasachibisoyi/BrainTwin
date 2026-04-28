/**
 * BrainTwin Popup
 *
 * Reads enabled + today's capture count straight from chrome.storage.local
 * and listens for changes so the count updates while the popup is open.
 * Toggle button just writes back to storage — content scripts and the
 * background worker react via storage.onChanged.
 */

function todayKey() {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

document.addEventListener("DOMContentLoaded", async () => {
  const countEl = document.getElementById("count");
  const statusEl = document.getElementById("status");
  const toggleBtn = document.getElementById("toggleBtn");

  // --- Initial render from storage ---
  const stored = await chrome.storage.local.get(["enabled", "captures"]);
  render(stored.enabled !== false, stored.captures);

  // --- Live updates while popup is open ---
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if ("enabled" in changes || "captures" in changes) {
      // Re-read the merged state — change events only carry the diff.
      chrome.storage.local.get(["enabled", "captures"]).then((s) => {
        render(s.enabled !== false, s.captures);
      });
    }
  });

  // --- Toggle button writes new state to storage ---
  toggleBtn.addEventListener("click", async () => {
    const { enabled } = await chrome.storage.local.get("enabled");
    const next = enabled === false; // flip
    await chrome.storage.local.set({ enabled: next });
    // No render needed here — onChanged listener handles it.
  });

  function render(enabled, captures) {
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
