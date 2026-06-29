# Local-first BrainTwin — design notes

> **Status:** ideation, not committed. Created 2026-06-23 from a side
> conversation while shipping Phase 4.0.6.1. This document captures the
> thinking so the idea doesn't evaporate; it does NOT commit to a phase,
> a timeline, or a re-architecture. The cloud product shipped in
> Phase 4.0.6 is the one we're running and growing today. This is a
> parallel direction to evaluate when the cloud story is mature
> enough to share its road with a second product line.

---

## 1. Why we'd even consider this

Three motivations, ordered roughly by how strongly we feel them.

**Privacy by construction.** Today every capture flows through
`api.braintwin.net`, gets enriched against the Anthropic API
(transferring the text), and is stored on an EC2 we control. None of
this is *insecure* — bearer auth, TLS, AOP, etc. — but it relies on
trust that we'll do the right thing. A local-first variant gives the
user a knowledge twin whose corpus literally never leaves their
personal devices. That's a different product story, and it's the
right story for the kind of content most people would actually
capture: half-formed thoughts, draft writing, sensitive notes.

**Offline-capable.** The current product needs a working network for
recall. A local-first variant can recall while the user is on a
plane, on a subway, or in a coffee shop with broken Wi-Fi. For a
tool whose value proposition is "always find what you saw," offline
isn't a nice-to-have.

**Cost.** Each Sonnet recall costs ~$0.005-0.02 depending on the
candidate pool. At low usage that's nothing; at "I use this 100x
a day" it's $20-60/month. Local inference has zero marginal cost
after the model is downloaded.

A weaker fourth motivation is **latency** — local inference is often
faster than a Sonnet round trip — but the current latency isn't bad
enough to be a primary driver.

---

## 2. What's already local

The most important thing to notice: BrainTwin is **almost** local
already. The only non-local calls are the two Anthropic API hits.
Everything else runs in-process.

| Layer | Today | Local-friendly? |
|-------|-------|-----------------|
| Capture (Chrome ext + Telegram bot) | Sends to FastAPI | Already runs on `localhost:8000` in dev |
| FastAPI app | Python process | Runs anywhere Python runs |
| SQLite (captures, enrichments, chunks) | Local file | Already local |
| Chroma vector store | Local file | Already local |
| Embeddings (sentence-transformers) | In-process Python | Already local — no API call |
| **Anthropic enrichment** (Haiku, at capture time) | **Network call** | **Bottleneck #1** |
| **Anthropic ranking** (Sonnet, at recall time) | **Network call** | **Bottleneck #2** |

This is the key framing. We're not "rebuilding for local-first." We're
asking "how do we close the last two network calls."

That makes the problem much smaller than it sounds. The `LLMClient`
class already abstracts both calls behind a stable interface
(`enrich`, `complete_json`) — the docstring in
`backend/knowledge/llm_client.py` literally says "swappable to a local
model later (Phase 5+ Llama A/B test)." We wrote that abstraction
with this exact pivot in mind.

---

## 3. Per-device-class feasibility

Personal devices vary enormously. A high-end desktop with a discrete
GPU and 64GB of RAM is a different machine than a 5-year-old laptop
on integrated graphics, which is a different machine again from a
mid-range phone from three years ago. We can't design for one and
hope. We have to define **what we target**, **how it degrades**, and
**where we draw the line** below which local-first stops being a
realistic story.

The strategy this doc advocates: **pick a "starting config" that we
know works today, ship for that, then work backwards through
weaker configurations and decide explicitly where the floor is.**
That order — strongest target first, then degrade — is the opposite
of how most cross-platform projects go, but it's the right one for a
quality-sensitive product. Targeting the floor first tempts us into
designing for the weakest device and capping quality everywhere.
Targeting the strongest device first lets us deliver value where
we can, then make explicit choices about how much we're willing to
give up to extend coverage downward.

### 3.1 Desktops and laptops with modern compute (starting target)

Examples: Apple Silicon Mac (M2 / M3 / M4), modern x86 laptops with
8-core+ CPU and 16GB+ RAM, anything with a recent discrete GPU.

The entire current stack already runs locally as a dev path on this
class. Going from "dev mode" to "production local-only" is a matter
of:

- Swapping LLMClient's two Anthropic calls for a local model
  (Ollama, MLX, llama.cpp, ROCm, or platform-native inference behind
  the same interface)
- Replacing the Chrome extension's cloud endpoint with `localhost`
  (or keeping the env var as-is)
- Optionally: a native UI shell so the user doesn't see "FastAPI on
  port 8000" as the surface

**Model quality on this class:**
- 7-8B models (Llama 3.1 8B Instruct, Qwen 2.5 7B, Mistral 7B) run
  at ~20-50 tokens/sec depending on the specific machine and runtime.
  Fast enough for enrichment (background, latency invisible) and
  recall (foreground, ~2-4 sec total).
- 13-14B models are doable on 32GB+ machines, tight on 16GB.
- A 1.5-3B model (Phi-3 mini, Gemma 2B) gives interactive latency
  even on the lowest end of this class — useful for enrichment,
  marginal for ranking.

**Estimated effort:** ~1 week to make LLMClient pluggable, ~1 more
week for a native UI shell on the dominant platform we target first.
After that we have a fully-local-on-personal-computer product on
that platform.

### 3.2 Mobile devices (constrained target)

Examples: recent iPhones (15 Pro / 16 Pro), high-end Android phones
(Pixel 9 Pro, Samsung S24+), tablets in the same generation.

The interesting class. Several things have shifted in mobile's
favor in the past 2-3 years:

- **System-provided foundation models.** iOS 18.1+ ships Apple
  Intelligence (3B foundation model) callable via Foundation Models
  framework — no model to download, system handles updates. Android
  is following with Gemini Nano on Pixel devices via the AI Core
  framework.
- **Cross-platform inference runtimes.** llama.cpp, MLC-LLM, and
  MLX-Swift all have mobile builds; you can bundle a quantized 3B
  model and run it via the device's GPU.
- **Hardware.** Top-tier 2024 phones run 3B quantized models at
  ~10-20 tokens/sec. That's slow for ranking but fast enough that
  the user doesn't notice on enrichment.
- **Vector store.** `sqlite-vec` / `sqlite-vss` give vector search
  inside SQLite on mobile — simpler than maintaining a separate
  Chroma-style store, and SQLite already runs everywhere.

The blocker isn't the LLM, it's the **capture surface**. Mobile
OSes intentionally don't let an app passively observe what the user
reads. The accessible capture paths are:

- **Share / Share Sheet** — single-tap from any app ("I want to
  remember this"). Works for URLs, text selections, images.
- **Browser extension** (where the OS allows it — e.g. iOS Safari
  extensions) — can scrape page content with explicit user consent.
- **Telegram channel forwarding** — same as today; ingest URLs the
  user forwards to a private bot.

This is actually fine — the "I deliberately want to remember this"
gesture is probably the right interaction model on a phone anyway.
The dwell-time-implicit-capture model is great on desktop but
ill-fitting on a phone.

**Memory is the binding constraint.** Mid-range mobile has 6-8GB of
unified RAM total, OS + app overhead leaves you maybe 2-4GB for ML.
A 3B quantized model fits; 7B is tight; 13B is out. This caps
recall quality vs the desktop class.

**Estimated effort:** 2-3 months for a first usable version per
mobile OS, more if we want feature parity across iOS and Android.
The capture extension is a few weeks; the storage architecture, the
sync story, and the on-device ML pipeline are real engineering.

### 3.3 Working backwards — defining the device floor

Once the starting target works, the question becomes: how far below
that can we still ship something honest?

**5-year-old laptops, integrated GPU, 8GB RAM** — runs a 1-3B model
slowly (~5-10 tokens/sec). Enrichment is fine in the background;
ranking is uncomfortable at 5-15 seconds. Probably the floor where
local-only is still a real product.

**Older or budget mobile devices (>3 years old, <6GB RAM)** —
realistically cannot run a 3B model with usable latency. We have
three honest choices for this tier:

1. **No local LLM. Vector + BM25 search only**, no Sonnet-style
   ranking. Falls back to "show me the top 10 candidates by score."
   Useful for power users who don't need conversational recall.
2. **Companion mode.** Mobile is purely a capture remote; recall
   answers come from a paired desktop/laptop on the same network or
   tied via a cross-device sync layer. The mobile device never runs
   the LLM itself.
3. **Cloud fallback for this tier only.** Defeats the privacy story
   for the tier that can't run local, but extends coverage downward.
   Honest if presented as opt-in.

**Below that floor — 5+ year old budget devices, basic Chromebooks,
low-spec tablets** — we just don't support local-first. The cloud
product is the answer for these users.

**Why this matters.** Defining the floor explicitly forces us to
draw the line between "ship local-first" and "use the cloud
product." Without that line we'll either water down the experience
trying to support everyone, or quietly ship a product that
disappoints lower-end users without being honest about it.

### 3.4 The configuration matrix at a glance

| Device tier | Example | LLM capability | Recommended config |
|------------|---------|----------------|--------------------|
| **High-end desktop / workstation** | Mac Studio, gaming desktop with dGPU, 32GB+ | 13-30B local | Full local, both calls local, fast |
| **Modern laptop (starting target)** | Apple Silicon Mac, modern x86 with 16GB+ | 7-8B local | Full local, both calls local |
| **Lean laptop (target floor)** | 5-year-old laptop, 8GB RAM, no dGPU | 1-3B local | Local enrichment, local ranking (slower) |
| **High-end mobile** | iPhone 15+ Pro, Pixel 9 Pro | 3B local | Local enrichment, local ranking |
| **Mid-range mobile (constrained)** | Older / lower-tier modern phones | 1B-or-smaller, or none | Companion mode OR cloud fallback opt-in |
| **Below floor** | Budget devices, basic Chromebooks | none | Not supported — use cloud product |

The rows are not just a feasibility table — they're the **shipping
matrix**. Each tier needs an explicit answer for: what runs locally,
what falls back, and how we communicate the differences to the user.

---

## 4. The hard part — multi-device sync

This is where "fully local" gets architecturally interesting. If the
user captures on one device and wants to recall on another, the
corpus has to sync somehow.

Three honest options:

### 4.1 Home-device / satellite-device (simplest)

- One device runs the full local stack — LLM, SQLite, vector store,
  recall agent. Call it the **home device** (typically a desktop or
  laptop in the starting-target tier).
- Other devices are thin capture-and-recall **satellites**.
- Capture: satellite's share-extension forwards to the home device
  via a cross-platform sync mechanism — system cloud-drive drop-
  folder, Bonjour over local network, Tailscale, or similar.
- Recall: satellite makes a recall request to the home device; home
  device responds with the answer.
- Satellites need no LLM at all — just network.

**Pros:** Simple. Reuses the entire current FastAPI codebase. Heavy
ML work happens where the hardware is.

**Cons:** Satellites are non-functional if the home device is off or
unreachable. For most users this is acceptable — they recall while
at their desk anyway — but it's a real constraint.

**Sync semantics:** None needed at the corpus level. The home
device is the single source of truth. Satellites are stateless.

### 4.2 Each device fully local + CRDT sync (cleanest, hardest)

- Every device runs the full stack independently.
- Corpus syncs via a CRDT (Y.js, Automerge, or a hand-rolled
  protocol on top of SQLite).
- Each device has its own embeddings index — re-embedded on
  receipt.
- Each device can do recall standalone.

**Pros:** Every device fully offline-capable. No "home machine"
required. Architecturally clean — every device is symmetric.

**Cons:** ~3 months of real engineering. Replication semantics,
conflict resolution on edits, embedding-index reconciliation are
all non-trivial. And the embeddings index is the hard part: if a
desktop and mobile use different embedding models (the desktop can
run a larger one), the vectors aren't directly comparable.

### 4.3 Local capture, cloud index (compromise)

- Captures and content stay on-device.
- A tiny relay service syncs only the chunk text + embeddings
  between devices.
- LLM calls stay local.
- Privacy story is weaker — the relay sees content — but better
  than today.

**Pros:** Quick to ship. Solves cross-device recall.

**Cons:** Defeats much of the privacy story. The relay becomes a
target for compromise and a regulatory question.

### Which to pick

Almost certainly **4.1 (home + satellites)** for v1. Ships in weeks.
Gives the privacy story most users would actually want ("my content
never leaves my devices"). The "home device" doing the heavy lifting
is fine because most users do their knowledge work on one primary
device anyway.

CRDT sync (4.2) is the long-term answer but it's a real product on
its own. Not the place to start.

---

## 5. The ranking quality question

This is the honest trade-off. Sonnet 4 ranks well because it has
deep world knowledge + careful instruction-following + multi-turn
reasoning over the candidate pool. A local 7-8B model is noticeably
worse at the same task today. Rough sense from comparable A/B work:

| Query type | Sonnet 4 (cloud) | Starting-target local (7-8B) | Mobile-class local (3B) |
|-----------|----------|---------------|---------------|
| Keyword-clear ("that article about Rust GCs") | ~95% match | ~90% | ~80% |
| Conceptually adjacent ("climate adaptation in farming") | ~88% | ~70-78% | ~55-65% |
| Multi-turn refinement ("no, the OTHER one") | ~85% | ~60% | ~40-50% |

The numbers above are estimates from comparable benchmarks on
similar tasks, not measured on our own data. Phase 4.0.5 (eval)
gives us the harness to actually measure — once that's in place,
the table becomes empirical.

Three honest paths once we go local:

1. **Accept the drop.** Most queries are keyword-clear-ish. Some
   won't work as well. Ship the privacy story; own the trade-off.

2. **Hybrid: local by default, cloud opt-in.** Run local always.
   If the local answer is unsatisfying, let the user "boost" to
   the cloud LLM — explicit consent for that one query. Privacy
   holds for 99% of queries; the 1% the user explicitly trades it.

3. **Wait.** On-device model quality is improving faster than most
   timelines expected. Apple Intelligence, Llama 3.3, Phi-4, Qwen 3,
   Gemini Nano — the gap narrows quarterly. If we ship cloud now and
   the local variant in 6-12 months, the local quality story is much
   better than evaluating today.

We don't have to pick yet. (2) is the most defensible if we ship
this soon — gives the privacy story without burning the user on
hard recalls.

---

## 6. Order of operations (if we ever pursue this)

This is the staged path. Each step ships standalone and teaches us
whether the next step is worth it. Note the order — **highest-leverage
target first, then expand to weaker configurations**.

### Step 1 — Pluggable LLMClient (~1 week)

Add an `LLM_BACKEND` env var: `anthropic | ollama | local-runtime`.
Implement at least one local backend behind the existing `LLMClient`
interface. Now we can A/B local vs cloud against the same corpus,
same recall agent, same prompts. Quality comparison becomes a single
config flip.

**Decision after Step 1:** is the local quality acceptable for our
own use on the starting target? If yes → continue. If not → stop
here, revisit when local models catch up.

### Step 2 — Native UI shell on the starting-target platform (~1 week)

The Chrome extension already works against a local backend. Add a
native UI shell so the user doesn't see "FastAPI on port 8000" as
the surface. Capture via global shortcut, from clipboard, from
browser-share. Pick the dominant platform in the starting-target
tier for the first build; others follow.

**Decision after Step 2:** is this a different product or a feature
of BrainTwin? If different → fork. If same product → branding +
docs unify.

### Step 3 — Mobile satellite to a starting-target home device (~2-3 weeks per OS)

Mobile is a capture remote. Share-Sheet forwards to the home device
via cross-platform sync or local-network discovery. Home device does
the heavy ML, responds with the recall answer if asked. Multi-device
works, but there's still a single source of truth.

**Decision after Step 3:** is this enough, or do users actually
want fully-local on mobile? If enough → call it done. If not → Step 4.

### Step 4 — Fully local on mobile (~2-3 months per OS)

The big one. Port the backend to a native mobile app or maintain it
as llama.cpp + sqlite-vec + system-provided foundation model. CRDT
sync between home device and mobile. Each device standalone, all
devices in sync.

This is real product engineering, not "just port the code." It's
also where the local-first story is most defensible — at this point,
**the user truly never sends their content anywhere they don't
control**.

### Step 5 — Walk the device floor (~1 month per tier)

Now we work backwards through weaker configurations. For each tier
below the starting target, make an explicit choice from §3.3:
local-with-degraded-quality, companion mode, opt-in cloud fallback,
or "not supported." Document the decision so users in that tier
know what they're getting.

**Decision after Step 5:** does the floor we drew match where real
users actually are? If most users are below the floor → either
lower the floor (more engineering) or accept that local-first is a
high-end product and the cloud version stays the default.

---

## 7. What this means for the cloud product

**Nothing.** The cloud version stays the deployed, supported,
canonical BrainTwin until and unless a local-first variant proves
itself with real usage.

Specifically:

- Phase 4.0.6 / 4.0.6.1 polish work stays on the cloud track.
- Phase 4.0.5 (eval) measures quality of the cloud agent — that
  measurement applies to the local agent too once it exists.
- Phase 4.0.7 (Postgres) is still the right answer for the cloud
  product. It's irrelevant for local-first (SQLite is the right
  answer at small scale).
- Phase 4.1 (multi-user) is **fundamentally incompatible** with
  local-first. They're different products. Picking one path doesn't
  preclude the other but we shouldn't pretend they're the same
  roadmap.

If we ever do pursue local-first seriously, it gets its own phase
number that doesn't try to slot into the existing cloud milestones.
Something like Phase 5L (the L for Local) running in parallel with
Phase 5 (cloud horizontal scaling).

---

## 8. Open questions to revisit

Things we'd want to nail down before committing to a local-first
phase:

- **Which starting-target platform first?** Apple Silicon, x86
  Windows, or x86 Linux? Probably the one we use most, but the
  ecosystem maturity differs (MLX is Apple-only, Ollama is
  cross-platform, llama.cpp runs everywhere). The choice affects
  ~1-2 weeks of engineering.
- **Which model family?** Llama 3 vs Qwen 2.5 vs Mistral vs
  Phi vs platform-native foundation models. The right answer
  depends on quality A/B against our recall task, not abstract
  model benchmarks.
- **Embeddings: stay with `sentence-transformers/all-MiniLM-L6-v2`,
  or upgrade to a larger model now that local compute is the norm?**
  Affects cross-device sync (§4.2) if devices use different
  embedders.
- **Where is the device floor actually?** Real numbers, not
  estimates. Need to test on at least one device per tier before
  drawing the line.
- **Capture deduplication across devices.** If user captures the
  same URL on mobile and desktop, what wins? Current dedupe is
  capture_id keyed, which would need rethinking.
- **Recall conversation state across devices.** Today the
  ConversationStore is in-memory per process. Local-first would
  need it to survive a restart AND a device switch — small but
  real refactor.
- **Image vision.** Desktop-class can run Moondream (1.6B vision-
  language) for OCR + description; mobile has on-device OCR built
  into the OS but image description models are still rough. Parity
  on day one is unlikely.
- **Telegram bot in a local-first world.** Telegram's API is
  inherently cloud (their server). A "local" Telegram bot still
  needs a public endpoint or some polling mechanism. Either the
  bot keeps running on a small VM (degraded privacy story) or the
  Telegram capture path doesn't exist in local-first variant.
- **How do we communicate device-tier differences to users?**
  Without honest UI cues, lower-tier users will think the product
  is broken rather than know they're on a degraded tier.

These are not blockers, just the decisions we'd want made
deliberately rather than discovered.

---

## 9. References

- Main cloud deployment design: `phase4.0.6-deployment-design.md`
- §13 horizontal scaling path (cloud direction): same doc
- `backend/knowledge/llm_client.py` — the abstraction boundary that
  makes the local pivot mechanical
- `backend/agent/recaller.py` — the recall reasoning that
  benchmarks cloud vs local-LLM quality differences
- `backend/storage/embedder.py` — the already-local embedder
  whose model choice influences the cross-device sync story (§4.2)

---

*Author: Sabya (with Claude as design partner). Created 2026-06-23
from a side question while Phase 4.0.6.1 polish work was in flight.
This document is captured thinking, not a commitment — the cloud
product remains the active road. If this idea graduates from
ideation to a real phase, it gets its own phase number (Phase 5L
or similar) and its own scoped design doc; this file is just where
the seed thinking lives so it doesn't evaporate.*
