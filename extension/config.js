/**
 * BrainTwin extension config — single source of truth.
 *
 * Phase 4.0.6 M.7.a (Client cutover): the BACKEND_URL used to be
 * duplicated in content.js and recall.js. Centralising here means the
 * next environment switch is one file. Load order is enforced by
 * manifest.json (content_scripts: ["config.js", "content.js"]) and
 * popup.html (<script src="config.js"> before recall.js / popup.js),
 * so by the time either consumer runs, `globalThis.BrainTwinConfig`
 * exists.
 *
 * No imports / exports — Chrome MV3 content scripts and popup pages
 * don't share a module graph. A plain global keeps the load model
 * uniform across both contexts.
 *
 * Local development:
 *   Flip BACKEND_URL to "http://127.0.0.1:8000" and reload the
 *   extension from chrome://extensions. The bearer token in
 *   chrome.storage.local can stay the same one — local docker-compose
 *   reads it from .env and the cloud reads it from SSM, so both
 *   accept the same value as long as you put the same string in
 *   both places.
 */
globalThis.BrainTwinConfig = {
  // Production cloud endpoint. Caddy in front, TLS, AOP, the works.
  // M.7.d smoke test confirms /capture and /recall end-to-end from
  // the extension to this URL.
  BACKEND_URL: "https://api.braintwin.net",
};
