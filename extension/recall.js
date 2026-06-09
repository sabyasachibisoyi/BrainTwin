/**
 * BrainTwin Remember Tab — Phase 4 M.5
 *
 * Posts vague-recall queries to the backend's POST /recall endpoint
 * and renders the RecallResponse shape locked by
 * docs/phase4-vague-recall-design.md (U.2 result blocks, S.3 wire
 * shape).
 *
 * Conversation state lives in this script as a single module-level
 * variable. It survives between turns while the popup stays open, and
 * is lost on popup close — which matches U.3's "intentionally short-
 * lived" semantic. Reopening the popup means a fresh conversation;
 * the backend's ConversationStore handles its own TTL on top.
 *
 * The Capture tab logic stays in popup.js. This file only owns the
 * Remember tab. Both are loaded by popup.html.
 */

const BACKEND_URL = "http://127.0.0.1:8000";
const RECALL_TIMEOUT_MS = 30000; // Sonnet calls can be slow; be generous.

// ---- Module state ---------------------------------------------------

let conversationId = null;  // null on first turn; uuid after backend responds
let inflight = false;        // simple lock — guards against double submit

// ---- DOM handles (cached on DOMContentLoaded) ----------------------

let form, input, submitBtn;
let statusEl, answerEl, resultsEl;
let conversationIndicator, newSearchBtn;

document.addEventListener("DOMContentLoaded", () => {
  form = document.getElementById("recallForm");
  input = document.getElementById("recallInput");
  submitBtn = document.getElementById("recallSubmit");
  statusEl = document.getElementById("recallStatus");
  answerEl = document.getElementById("recallAnswer");
  resultsEl = document.getElementById("recallResults");
  conversationIndicator = document.getElementById("conversationIndicator");
  newSearchBtn = document.getElementById("newSearchBtn");

  if (!form) return; // popup.html structure changed — bail quietly

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    handleSubmit();
  });

  newSearchBtn.addEventListener("click", () => {
    resetConversation();
  });
});

// ---- Submit / fetch ------------------------------------------------

async function handleSubmit() {
  const query = (input.value || "").trim();
  if (!query) return;
  if (inflight) return;

  inflight = true;
  submitBtn.disabled = true;
  clearResults();
  showStatus("loading", "Searching your corpus…");

  let payload;
  try {
    payload = await postRecall(query, conversationId);
  } catch (err) {
    showStatus("error", friendlyError(err));
    inflight = false;
    submitBtn.disabled = false;
    return;
  } finally {
    inflight = false;
    submitBtn.disabled = false;
  }

  renderResponse(payload, query);
}

async function postRecall(query, convId) {
  const body = { query };
  if (convId) body.conversation_id = convId;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), RECALL_TIMEOUT_MS);

  let response;
  try {
    response = await fetch(`${BACKEND_URL}/recall`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (err) {
    if (err.name === "AbortError") {
      throw new Error("TIMEOUT");
    }
    throw new Error("NETWORK");
  } finally {
    clearTimeout(timeoutId);
  }

  if (response.status === 503) {
    throw new Error("NO_RECALLER");
  }
  if (response.status === 422) {
    throw new Error("BAD_QUERY");
  }
  if (!response.ok) {
    throw new Error(`HTTP_${response.status}`);
  }

  return response.json();
}

function friendlyError(err) {
  const msg = String(err.message || err);
  switch (msg) {
    case "NETWORK":
      return "Couldn't reach the backend. Is uvicorn running at " +
             `${BACKEND_URL}?`;
    case "TIMEOUT":
      return "Search took too long. Try a shorter query, or check " +
             "if Sonnet is reachable.";
    case "NO_RECALLER":
      return "The recall agent isn't running. Set ANTHROPIC_API_KEY in " +
             ".env and restart the backend.";
    case "BAD_QUERY":
      return "Backend rejected the request shape — please report this.";
    default:
      return `Recall failed: ${msg}`;
  }
}

// ---- Rendering ------------------------------------------------------

function renderResponse(payload, query) {
  clearResults();

  // Update / mint conversation id. Backend always returns one.
  if (payload.conversation_id) {
    conversationId = payload.conversation_id;
    showConversationActive();
  }

  // The conversational answer paragraph (brief mode).
  if (payload.answer) {
    answerEl.innerHTML = "";
    const a = document.createElement("div");
    a.className = "answer";
    a.textContent = payload.answer;
    answerEl.appendChild(a);
  }

  // No-match path gets a soft status banner above the (optional)
  // closest-miss card. We still render the card if results[] is
  // non-empty so the user sees the courtesy match.
  if (payload.no_match) {
    showStatus(
      "no-match",
      payload.results && payload.results.length > 0
        ? "Not confident this is it — here's the closest match."
        : "I don't think this is in your corpus."
    );
  } else {
    hideStatus();
  }

  // Render the result cards.
  const results = payload.results || [];
  for (const r of results) {
    resultsEl.appendChild(renderCard(r));
  }
}

function renderCard(r) {
  const card = document.createElement("div");
  card.className = "card";

  // Title
  const title = document.createElement("div");
  title.className = "card-title";
  title.textContent = r.title || "(untitled)";
  card.appendChild(title);

  // Meta line: source · captured_at · client · dwell · confidence
  const meta = document.createElement("div");
  meta.className = "card-meta";
  meta.appendChild(document.createTextNode(metaLine(r)));
  if (typeof r.confidence === "number") {
    const conf = document.createElement("span");
    const pct = Math.round(r.confidence * 100);
    conf.className = "conf" + (pct < 60 ? " low" : "");
    conf.textContent = ` · ${pct}% confident`;
    meta.appendChild(conf);
  }
  card.appendChild(meta);

  // Why this matches
  if (r.why_this_matches) {
    const why = document.createElement("div");
    why.className = "card-why";
    why.textContent = r.why_this_matches;
    card.appendChild(why);
  }

  // Snippet
  if (r.snippet) {
    const snip = document.createElement("div");
    snip.className = "card-snippet";
    snip.textContent = r.snippet;
    card.appendChild(snip);
  }

  // Original-source link — opens in a new tab. The URL came from the
  // user's own capture, so it's safe to surface as a real link.
  if (r.original_url) {
    const link = document.createElement("a");
    link.className = "card-link";
    link.href = r.original_url;
    link.textContent = "Open original →";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    card.appendChild(link);
  }

  return card;
}

function metaLine(r) {
  const bits = [];
  if (r.source_domain) bits.push(r.source_domain);
  if (r.captured_at) {
    bits.push(formatDate(r.captured_at));
  }
  if (r.client) bits.push(r.client);
  if (typeof r.dwell_time_seconds === "number" && r.dwell_time_seconds > 0) {
    bits.push(`${r.dwell_time_seconds}s dwell`);
  }
  return bits.join(" · ");
}

function formatDate(iso) {
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    // Short, locale-friendly date — drop the time of day for UI density.
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch (_) {
    return iso;
  }
}

// ---- Status helpers ------------------------------------------------

function showStatus(kind, msg) {
  statusEl.innerHTML = "";
  const s = document.createElement("div");
  s.className = `status ${kind}`;
  s.textContent = msg;
  statusEl.appendChild(s);
}
function hideStatus() {
  statusEl.innerHTML = "";
}

function clearResults() {
  resultsEl.innerHTML = "";
  answerEl.innerHTML = "";
}

// ---- Conversation lifecycle ----------------------------------------

function showConversationActive() {
  conversationIndicator.textContent = "Refining results (conversation active)";
  newSearchBtn.style.display = "inline";
}

function resetConversation() {
  conversationId = null;
  conversationIndicator.textContent = "";
  newSearchBtn.style.display = "none";
  input.value = "";
  clearResults();
  hideStatus();
  input.focus();
}
