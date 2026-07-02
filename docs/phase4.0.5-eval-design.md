# Phase 4.0.5 — Eval & quality

> **Status:** design, milestones not yet started. Created 2026-06-29
> after Phase 4.0.6 (cloud deploy) and Phase 4.0.6.1 (operational
> polish) shipped to production. The cloud product works and is
> operationally mature. This phase answers a question we've deferred
> for months: **is the recall any good, and where are the levers?**
> Originally numbered 4.0.5 (predating 4.0.6 by intent) and deferred
> until the cloud was live enough for an eval target to actually
> exist. That target now exists.

---

## 0. Prerequisites

- ✅ Cloud product live at `api.braintwin.net` (Phase 4.0.6)
- ✅ App-level metrics + dashboard live (Phase 4.0.6.1 M.11) — gives
  us per-route latency to compare against eval-time numbers
- ✅ Discovery-pattern refresh (M.10) — lets us add new SSM secrets
  without instance churn (e.g. an eval S3 path; Langfuse keys only if
  the §5.0 graduation is ever taken)
- ✅ s3.Asset refactor (M.12) — gives user-data headroom for any
  small CDK additions needed here (GitHub Actions OIDC role; eval
  results/traces S3 prefix)

Operationally we're in the right state for this phase. The
work below is product engineering, not infra firefighting.

---

## 1. Why eval now

**The honest gap.** Every RAG quality decision in BrainTwin so far
has been intuition + spot-checks. The list of choices waiting for
empirical validation is long:

- The embedder choice (`sentence-transformers/all-MiniLM-L6-v2`)
- BM25 weight in the RRF fusion (currently equal-weight)
- The RRF k=60 constant
- Chunk size + overlap parameters
- The Sonnet re-rank prompt structure
- The confidence threshold V.7 at 0.6 (no_match cutoff)
- The diversification step (one chunk per capture_id)
- Top-k=20 for each ranker before fusion

Some of those are probably fine. Some are probably wrong. Without
measurement we can't tell which is which, so we don't change any of
them. Phase 4.0.5 builds the harness that converts "we should
probably bump the embedder" from a multi-day vibes argument into a
30-minute experiment.

**The portfolio angle.** A RAG system with no eval is hard to
defend — it works on the queries the builder tried, and the
reviewer has to take "it's good" on trust. Once eval exists, the
same demo answers "here's how we know it works, and here's the
specific 23% that doesn't work yet, and here's what's planned to
move it." That's a different kind of story.

**The unblock.** Until now, every recall complaint ("this should
have found X") was a singleton observation. With a golden set, the
same complaint becomes a row in the eval set that either already
exists (and the regression is now visible) or doesn't yet (and
adding it documents the case for future regression).

---

## 2. What we're measuring

Multiple metrics, not one. A single number flattens too many
trade-offs (a system that hits 100% recall@5 but takes 30 seconds
per recall isn't actually good).

**We adopt the RAGAS metric taxonomy** (faithfulness, answer
relevancy, context precision, context recall) as the vocabulary,
because it's the de-facto industry standard and "I rolled my own
ad-hoc metrics" reads worse than "I used the standard names and
implemented them myself." We do NOT take RAGAS-the-library as a
dependency — see §2.5 for the build-vs-buy call.

### 2.1 Quality metrics

> **Two layers, measured separately.** Standard RAG eval distinguishes
> *retrieval* quality (did the candidate generator surface the right
> capture?) from *answer* quality (did the final answer use it and
> answer the question?). We measure both and never collapse them into
> one number — see §2.4 for why this attribution matters.

**Retrieval-layer metrics** (scored on the fused candidate list
*before* the Sonnet re-rank — see §2.4):

| Metric | What it measures | Why it matters |
|--------|------------------|----------------|
| **recall@k** for k ∈ {1, 3, 5, 10, 20} | Did a correct capture appear in the top-k? | The fundamental "did retrieval do its job" signal. |
| **MRR** (Mean Reciprocal Rank) | Average reciprocal position of the first correct result | Penalises systems that get the right answer but rank it 8th. |
| **nDCG@k** | Rank-discounted gain; rewards correct results higher up | The standard IR ranking metric. With one relevant capture per query it reduces to a function of rank (≈ MRR), so it earns its keep only once we add *graded* relevance (§3.3) — included now so the harness doesn't need re-plumbing later. |
| **context precision / recall** (RAGAS) | Of the retrieved chunks, how many are relevant / of the relevant chunks, how many were retrieved | Names the retrieval-quality signal in the standard vocabulary; computed from the same capture_id matches. |

> **Note on redundancy:** with exactly one labelled `expected_capture_id`
> per query, `precision@1 == recall@1` and `MRR == mean(1/rank)`. We
> report precision@1 separately only because it's the number a
> non-IR reader intuitively understands ("how often is the top answer
> right"). Once §3.3 allows multiple acceptable captures, they diverge
> and both are worth keeping.

**Answer-layer metrics** (scored on the final `/recall` response):

| Metric | What it measures | Why it matters |
|--------|------------------|----------------|
| **faithfulness** (RAGAS) | Are the answer's claims *entailed by* the retrieved snippets, or confabulated from training? | The anti-hallucination signal — "model knew the answer and ignored our corpus." |
| **answer relevancy** (RAGAS) | Does the answer actually address the *question* (vs being faithful but off-topic)? | Faithful-but-irrelevant is a distinct failure; collapsing it into "faithfulness" hides it. |
| **no_match rate** + **no_match correctness** | Fraction below the V.7 threshold, AND whether saying no_match was the *right* call (needs negative queries — §3.4) | A high rate alone is ambiguous; without negative-query ground truth a system that answers *everything* looks healthy while being broken. |
| **conversational coherence** (multi-turn) | Does refinement "the other one about X" land on the prior pool? | The ConversationStore feature only matters if measured. |

### 2.2 Operational metrics

| Metric | What it measures | Why it matters |
|--------|------------------|----------------|
| **end-to-end latency** p50/p95/p99 | Wall-clock from query to response | The dashboard from M.11 already measures this in prod; the eval harness measures it on a controlled load. |
| **token spend per query** (Anthropic input + output) | Sonnet cost per recall | Lets us compare prompt-shape changes head-to-head on cost. |
| **embedding model latency** | Time to embed one query | When we A/B embedder models, this is the tax we pay. |

### 2.3 Scoring methodology

> **Three model roles, kept explicitly separate.** A common
> confusion in RAG eval is treating "the LLM" as one thing when
> it plays three distinct roles across the pipeline. Making them
> explicit here so the model choice per role is deliberate rather
> than accidental:
>
> | Role | When it runs | What it does | Which model |
> |------|--------------|--------------|-------------|
> | **Golden set bootstrap** (§3.1) | Once, during M.E.1 | Generates candidate Q&A pairs from real captures for human review | Sonnet (fine — human reviews everything before it hits the golden set) |
> | **System under test** | Every eval run (nightly) | Prod's `/recall` pipeline; the thing being graded | Sonnet 4.6 (whatever's in `settings.agent_model` — this is what's shipped) |
> | **Judge** | Every eval run (nightly) | Scores answers for faithfulness + answer relevancy | **Different model from system under test.** See §2.3.1 below. |
>
> The rule "judge ≠ ratee" applies only to the third row.
> Bootstrap and judge can be the same model if we want; system-
> under-test and judge cannot.

- **recall@k, MRR, nDCG, precision@1** — match on capture_id against
  the labelled set (one or more acceptable captures per query, §3.3).
  Deterministic, no model involved — these are pure functions.
- **faithfulness / answer relevancy** — **LLM-as-judge**, with three
  guardrails so the metric is trustworthy rather than decorative:
  1. **The judge is a different (and ≥ as strong) model than the one
     under test.** Sonnet rating Sonnet is the same model grading its
     own homework; using a separate judge breaks that loop. (§10 keeps
     "which judge" open, but the *principle* — judge ≠ ratee — is
     fixed here.)
  2. **For A/B runs, prefer pairwise comparison over absolute 0-1
     scoring** ("is answer A or B better?"). LLM judges are far more
     reliable relative than absolute. **Swap the A/B presentation
     order and average** to cancel position bias.
  3. **Validate the judge against humans before trusting it.** We
     hand-label ~20-30 answers and compute judge↔human agreement
     (Cohen's κ). If κ is low the judge metric is noise dressed as a
     number, and we say so in the baseline report rather than quoting
     it. This is the step that separates a real eval from a toy one.
- **conversational coherence** — pair refinement queries with their
  prior turn; check whether the refined answer is from the prior
  pool. Boolean per pair.
- **latency, token spend** — measured directly from the harness.

> **Why these changes (judge rigor):** the original draft used
> "Sonnet-as-judge, biased but consistent across runs." Consistent
> bias is fine for *regression detection* but not for *absolute*
> claims ("faithfulness is 0.9") — and a portfolio reviewer reads an
> unvalidated self-judge as a red flag. The judge≠ratee rule, pairwise
> A/B, and human-κ validation are the standard mitigations; they cost
> little and buy a defensible number.

### 2.4 Stage-level attribution — measure the retriever and the pipeline separately

**The problem this solves.** The `/recall` API runs the *whole*
pipeline: embed → BM25 + vector → RRF fusion → diversify → Sonnet
re-rank → confidence gate. If we only score the API output, every
quality number is end-to-end. But §1's tuning levers live at
*different stages* — embedder, BM25 weight, RRF `k`, and chunk size
are **retrieval-stage**; the re-rank prompt and confidence threshold
are **answer-stage**. An end-to-end-only eval can't tell you *which
stage* a regression came from, which is exactly the question you'll
ask after every A/B.

**The fix.** Two measurement entry points over the same golden set:

1. **Retrieval-only harness** — calls `RetrievalService.retrieve()`
   directly (it already returns the fused, diversified candidate list
   with `capture_id`s, *before* the Sonnet re-rank). Scores the
   retrieval-layer metrics (§2.1). Isolates embedder / BM25 / RRF /
   chunking changes. Fast and cheap — no Sonnet call.
2. **End-to-end harness** — calls `/recall` (§4.1). Scores both layers,
   including the re-rank's effect and the answer-layer metrics.

A regression that shows in (2) but not (1) is in the re-rank/answer
stage; one in both is upstream in retrieval. That attribution is the
whole point — it turns "recall dropped" into "the embedder swap cost
us 4pts of retrieval recall, which the re-rank only half-recovered."

> **Why we added this:** it's the single change that makes the eval
> *actionable* for the levers §1 names, and it's standard practice in
> production RAG (retriever eval and generation eval are separate
> harnesses). It's also nearly free here because `RetrievalService` is
> already a clean seam.

### 2.5 Build vs buy — why we implement the scorers, not import RAGAS

We considered the established eval frameworks — **RAGAS, DeepEval,
TruLens, Arize Phoenix, promptfoo, OpenAI Evals** — and use their
*vocabulary and metric definitions* but not the libraries. Reasoning,
same shape as the EMF "raw JSON not the lib" call (polish §4):

- The scorers we need (recall@k, MRR, nDCG, an LLM-judge call) are
  ~150 lines of pure functions we fully control and can unit-test.
- The frameworks pull heavy transitive deps and, more importantly,
  many default to sending prompts/answers to *their* hosted judge or
  telemetry — which breaks the privacy story that justifies the whole
  self-hosted-Langfuse §5.
- We keep the option open: the golden set is plain JSONL, so feeding
  it to RAGAS later for cross-validation is a small script, not a
  rewrite.

Naming the landscape and justifying the build-vs-buy is itself part
of the portfolio signal — it shows the decision was made, not missed.

### 2.6 How to read the numbers — diagnostic tree

The metrics aren't a scoreboard, they're a diagnosis. Each failure
mode points at a specific lever, and the ordering below is roughly
"cheapest to try" → "most expensive to try":

**Read the retrieval-only harness (§2.4) first.** It tells us
whether the problem is upstream or downstream of the re-rank.

| What you observe | Most likely cause | First levers to try (cheap → expensive) |
|------------------|-------------------|------------------------------------------|
| `recall@20` **low** | Right capture isn't even in the candidate pool — retrieval is failing at the recall step | (1) BM25 weight in fusion; (2) RRF `k`; (3) top-k per ranker before fusion; (4) chunk size + overlap (expensive: re-embed corpus); (5) embedder model swap (expensive: re-embed corpus) |
| `recall@20` high but `recall@1` / `precision@1` low | Right capture is in the pool but ranked poorly — problem is fusion or re-rank | (1) RRF `k`; (2) Sonnet re-rank prompt structure; (3) re-rank temperature (default → 0); (4) different re-rank model (Sonnet vs Haiku) |
| Retrieval fine, faithfulness **low** | Answer confabulates instead of grounding in retrieved snippets — problem is answer prompt | (1) Answer prompt structure ("cite the snippets"); (2) answer temperature; (3) constrain to top-N snippets only |
| Retrieval fine, faithfulness high, answer relevancy **low** | Answer is grounded but doesn't address the *question* | (1) Answer prompt structure ("answer the specific question"); (2) prompt example shift |
| `no_match rate` high, `no_match correctness` low | Threshold V.7 is too strict for real queries | (1) Lower threshold (0.6 → 0.5 → 0.4); (2) per-query calibration |
| `no_match rate` high, `no_match correctness` high | Corpus genuinely doesn't answer many queries — not a bug | Capture more content in the missing domains (product signal, not code signal) |
| `conversational coherence` low | Refinement loses prior-turn context | (1) Recheck ConversationStore lookup; (2) prompt template for refinement path |
| **Latency** rising, quality steady | Cost/perf regression, not a quality bug | (1) Reduce top-k per ranker; (2) reduce re-rank input size; (3) prompt-shortening pass; (4) faster/cheaper model for re-rank |

**Lever cost + reversibility (cheap to expensive):**

| Lever | Cost to try | Reversibility if wrong |
|-------|-------------|------------------------|
| Sonnet re-rank prompt tweak | 5-30 min | Trivial — revert commit |
| Confidence threshold V.7 | One constant change | Trivial |
| RRF `k` constant | One constant | Trivial |
| BM25 weight in fusion | One constant | Trivial |
| Re-rank temperature | One arg | Trivial (changes determinism though) |
| Top-k per ranker before fusion | One constant + more Anthropic tokens | Trivial revert but costs money while wrong |
| Chunk size + overlap | Hours — must re-chunk + re-embed entire corpus | Painful — rollback means re-chunking again |
| Embedder model | Hours — same re-embed pain | Same |
| Re-rank model swap (Sonnet ↔ Haiku) | One config; large cost shift | Trivial revert but big cost delta |

**The workflow.** Read the metric that regressed → cross-reference
the "most likely cause" column → try the cheapest lever in that row
first → re-run the eval → if the fix is real (per §4.4's paired
significance test), keep it; if not, revert and try the next lever.
Never touch chunk size or embedder until every cheaper lever has
plateaued — those two reshape the whole corpus and eat days of
compute to reverse.

---

## 3. The golden Q&A set

### 3.1 Construction — Claude-assisted bootstrap

Hand-curating 100 high-quality Q&A pairs from scratch takes 1-2
weeks. A pure auto-generated set from Sonnet biases the eval (the
same model that ranks is also writing the test). The middle path:
**Sonnet generates candidates; you review and keep the good ones**.

Specifically:

1. For each of ~200 captures in the corpus, send the processed
   text to Sonnet with the prompt: *"Generate one question a user
   might ask weeks later that this content would answer. Make the
   question vague enough that the user has to remember the topic,
   not the exact phrasing. Paraphrase — do NOT reuse distinctive or
   rare terms verbatim from the text."* (The paraphrase clause guards
   against **lexical leakage**, see below.)
2. This yields ~200 candidate Q&A pairs (question + the
   capture_id that should answer it).
3. You review the set in a small tool (CLI or Streamlit page —
   bias toward CLI, smaller surface). For each candidate, three
   choices:
   - **Keep** → goes in the golden set
   - **Reject** → bad question (too literal, ambiguous, unfair)
   - **Edit** → keep the capture_id, rewrite the question
4. Target: ~60-80 kept-or-edited pairs. Don't force 100 — quality
   over quantity.

**Why this avoids the worst form of the same-model-bias.** The
question is generated *from* the capture, but the system has to
*find* the capture without seeing it. That's the actual recall
task. Sonnet generating questions about content it knows isn't
the same as Sonnet evaluating its own answers — those are the
two separate roles where bias would compound.

**Guard against lexical leakage (added).** Generating the question
*from* the source text risks the question echoing rare salient terms
verbatim — which then makes BM25 and the embedder look better than
they'd do on a real user's vaguer phrasing. This *inflates retrieval
recall* and is a classic way RAG evals lie to their authors. Two
mitigations, both cheap:

- The "paraphrase, don't reuse rare terms" clause in the generation
  prompt (step 1).
- During review (step 3), compute token-overlap between the question
  and its source capture; **flag high-overlap candidates** so the
  reviewer rewrites or rejects them. A question that shares a rare
  trigram with its source is a leak suspect.

### 3.2 Conversational set (separate)

A second, smaller set (~10-15 pairs) for refinement testing.
Construction:

1. Start with a query that returns multiple candidates from §3.1
   (intentionally pick an ambiguous one).
2. Manually write the refinement turn ("no, the OTHER one — the
   one about X").
3. Manually label which capture_id the refinement should resolve
   to.

This set tests U.3 (the conversation refinement feature) which
the single-turn set doesn't touch.

### 3.3 Storage

The golden set lives in `BrainTwin/eval/golden_set.jsonl`. Each
row:

```json
{
  "id": "q-001",
  "query": "what did I read about embedding model trade-offs",
  "relevant_captures": [
    {"capture_id": "cap-abc-123", "grade": "primary"},
    {"capture_id": "cap-def-456", "grade": "acceptable"}
  ],
  "tags": ["embeddings", "rag"],
  "difficulty": "easy",
  "added_at": "2026-06-30T..."
}
```

> **Why `relevant_captures` (a graded list) instead of one
> `expected_capture_id`:** for *vague* recall, more than one capture
> can legitimately answer a query ("what did I read about embeddings"
> may have three good sources). A single-truth label scores a
> genuinely-relevant top-1 as a miss whenever it isn't *the* one
> capture we happened to write down — making recall pessimistically
> wrong and A/B deltas noisier. A list of acceptable captures fixes
> the correctness of the metric; the `grade` field (`primary` /
> `acceptable`) feeds **nDCG**'s graded relevance (§2.1) and lets
> precision@1 reward the best answer without punishing a good one.
> recall@k counts a hit if *any* relevant capture appears in top-k.

Conversational rows have an additional `prior_turn` field with the
preceding `(query, conversation_id, candidate_pool)` tuple.

The set is version-controlled. Adding a row is a PR. **The set
itself is the eval contract** — when it changes, eval results
across runs are no longer directly comparable, so changes need to
be deliberate.

Each results/summary file also records the **system-under-test
version** — git SHA / `image_tag`, embedder name+version, re-rank
prompt version, and the RRF/threshold params in effect (see §4.1).
Without that, an A/B "before vs after" across time is comparing two
unknowns. (This lives in the *results*, not the golden set — the
golden set is the fixed contract; the SUT version is what changed.)

### 3.4 Negative / no-answer queries (added)

A separate small set (~10-15) of queries that the corpus **should
not** be able to answer — topics the user never captured. Each is
labelled `relevant_captures: []` (intentionally empty).

**Why this exists:** §2.1 measures `no_match rate`, but a rate alone
is ambiguous — you can't tell "correctly declined" from "failed to
find something that was there." With negative queries we get ground
truth for the confidence gate (V.7 @ 0.6) and can measure its
**false-positive rate** (answered when it should have declined) and
**false-negative rate** (declined when it should have answered, from
the positive set). A system that *never* says no_match scores great
on recall while being silently broken — exactly the failure a
portfolio reviewer probes, and the one negative queries catch.

Source these from real "this should've found nothing" moments plus a
few hand-written plausible-but-absent topics. Keep them in
`eval/golden_set_negative.jsonl`.

### 3.5 The eval user — corpus provisioning + freeze semantics

The eval runs against `/recall`, which post-Phase-4.1 (multi-user)
authenticates as a specific user. We need a stable *corpus* under
that user for the golden set to make sense. Two competing needs:

- **Determinism** — the golden set labels reference specific
  `capture_id`s. If those captures change, labels no longer
  correspond to the corpus, and metrics drift for reasons unrelated
  to code changes. The eval becomes noise.
- **Realism** — the eval should reflect the current product; a
  corpus frozen at 200 captures forever won't stress the retrieval
  code paths that appear at 2,000 captures.

**We resolve this by freezing the eval corpus at creation and
refreshing only via planned events.** No continuous sync. The eval
user's captures are a *snapshot* of Sabya's captures at time T,
plus deliberate additions when we want to test new territory.

**Provisioning (part of Phase 4.1 M.M.1):**

1. Provision the eval user with `email = eval@braintwin.net`,
   `is_admin = false`. Same auth path as a real user; the account
   just happens to belong to the eval harness.
2. Copy Sabya's captures at time T:
   `INSERT INTO captures (capture_id, user_id, ...) SELECT gen_random_uuid(), <eval_user_id>, ... FROM captures WHERE user_id = <sabya_user_id>`.
   Each row gets a fresh UUID (since capture_id is unique).
3. Copy the corresponding `chunks` and `enrichments` rows the same
   way — fresh IDs, remapped to the new capture_ids.
4. For Chroma: query every vector under `where: {user_id: <sabya>}`,
   re-add under `user_id: <eval_user>` with new capture_id metadata.
5. Persist an `eval_capture_lineage(eval_capture_id, source_capture_id, copied_at)`
   table so that when a labelled query looks wrong six months later
   we can trace back to the original.
6. **Build the golden set (M.E.1) against the eval user's fresh
   UUIDs**, not the source UUIDs. Otherwise labels reference
   nothing on the eval user's side of the DB.
7. Mint a JWT for the eval user with a long TTL, store in SSM at
   `/braintwin/eval_bearer_token`, wire into the eval harness.
   (The param itself exists from Day 0 holding the shared bearer;
   this step swaps the *value*. ⚠️ Bumping the eval user's
   `token_version` silently kills this token — the nightly run
   starts 401ing. Treat eval-run 401s as an alarm condition, and
   re-mint + re-put the secret after any deliberate bump.)

> **Sequencing unblock (added 2026-07-02): M.E.1 does NOT wait for
> the eval user.** Steps 1-7 depend on Phase 4.1's data model, but
> the golden set doesn't have to: bootstrap and curate it (M.E.1)
> against **Sabya's current capture_ids today**, then when the eval
> user is provisioned, remap every `relevant_captures[].capture_id`
> through `eval_capture_lineage` (source → eval UUID) with a ~20-line
> script. The lineage table was designed for tracing; it doubles as
> the remap key. This is what lets 4.0.5 and 4.1 genuinely run in
> parallel — without it, M.E.1 (the longest-wall-clock milestone,
> because curation is human-paced) chains behind 4.1 M.M.1. The
> retrieval-only harness (§2.4) can likewise run against the current
> single-user corpus before 4.1 lands — same labels, no remap needed
> until the eval user exists.

**What normal usage does *not* do.** Sabya (as user_id = 1) keeps
capturing normally. The eval user's corpus stays exactly as
snapshotted. Real usage and eval never mix:

- Sabya real-user → 250 captures today, 340 in a month, 800 by
  year-end (or whatever)
- Sabya eval-user → 200 captures at freeze time, still 200 in
  December unless a refresh event fires

**Refresh events.** Explicit and ceremonial — not automatic. Fire
one when *at least one* of these becomes true:

- The eval corpus is materially smaller than the real corpus
  (say, 3× the delta) and the eval is no longer stressing the same
  retrieval code paths real users hit
- New topic areas we want to test have accumulated in real captures
  but aren't in the eval user's corpus
- A prior refresh is stale enough (~6-12 months) that it's just
  time to re-baseline

The refresh procedure is deliberately heavy:

1. Provision a *new* eval user (e.g. `eval-2027-q1@braintwin.net`).
   Keep the old one either active for historical comparison or
   archived — don't overwrite.
2. Copy the current-state captures + chunks + enrichments + Chroma
   vectors into the new user (same procedure as steps 2-5 above).
3. Regenerate the golden set (M.E.1) against the new eval user's
   corpus. This IS a new golden set — old queries may not map cleanly.
4. Establish a new baseline. From this point forward, results are
   compared against the new baseline, not the old one.
5. Publish `docs/eval-baseline-YYYY-MM-DD-refresh.md` explaining
   what changed and why the old numbers are no longer comparable.
6. Update all A/B compare tooling to warn if it's asked to compare
   across a refresh boundary (results from different eval users
   aren't comparable).

**Why this is deliberately painful:** a corpus change is a
*measurement change*. Making it easy to do quietly is how eval
becomes untrustworthy. The ceremony is the feature.

**What we skip in the short term:** no automated corpus diff-and-sync,
no "keep eval and real corpus within N% of each other" job.
Refreshes are human-triggered decisions with human-written writeups.
If that ever becomes a bottleneck (i.e., we need refreshes weekly),
that's a signal to rethink the whole eval-user model — probably by
moving to Postgres snapshot-restore (Phase 4.0.7 territory).

---

## 4. The eval harness

### 4.1 Shape

```python
# eval/run_eval.py

async def run_eval(
    target_url: str,        # e.g. "https://api.braintwin.net"
    bearer_token: str,
    golden_set_path: Path,
    output_dir: Path,
) -> EvalSummary:
    """Walk the golden set, call /recall for each query, score the
    response, write per-query results + an aggregate summary."""
    rows = load_jsonl(golden_set_path)
    sut = await fetch_sut_version(target_url)  # git sha / image_tag,
                                              # embedder, prompt ver,
                                              # RRF + threshold params
    results = []
    for row in rows:
        async with timed_eval(row.id):
            response = await call_recall(target_url, bearer_token, row.query)
            score = score_response(row, response)
            results.append(score)
    summary = aggregate(results)
    summary.sut_version = sut              # stamped so A/B-over-time is comparable
    write_jsonl(output_dir / "results.jsonl", results)
    write_json(output_dir / "summary.json", summary.to_dict())
    return summary
```

> **Determinism (added) — a non-deterministic system can't be
> regression-tested.** The Sonnet re-rank currently runs at the SDK's
> default temperature (the `messages.create` calls in
> `backend/knowledge/llm_client.py` set no `temperature`, so it's
> ≈1.0). Two eval runs on *identical code* will then disagree on
> rankings, and you can't separate a real regression from model noise.
> Fix before M.E.5 baseline: run the eval against the re-rank at
> **`temperature=0`** (and consider pinning it in prod recall too).
> Where non-determinism is unavoidable, run each query **N times and
> report mean ± std** so the variance is visible rather than hidden.

> **SUT versioning (added):** `summary.sut_version` records *what was
> evaluated*. Comparing today's run to last week's baseline is
> meaningless if you don't know the embedder/prompt/params changed in
> between. Expose this via a small `/version` field or read it from the
> deployed `image_tag` SSM param.

### 4.2 Where it runs

Four distinct contexts:

0. **Retrieval-only (in-process)** — `eval/run_retrieval_eval.py`
   calls `RetrievalService.retrieve()` directly (no HTTP, no Sonnet),
   scoring the retrieval-layer metrics over the golden set. Cheap and
   fast — this is the loop you run while tuning the embedder, BM25
   weight, RRF `k`, or chunk size (§2.4).
1. **Ad-hoc on your Mac** — `pytest eval/test_eval_smoke.py` runs
   a tiny subset (~5 queries) against `localhost:8000` or the
   cloud. Fast iteration loop while changing prompts.
2. **Full nightly run** — GitHub Actions hits the cloud `/recall`
   with the entire golden set, writes results to S3, posts a
   summary as a Run Summary + a CloudWatch custom-metric for
   regression alerting.
3. **A/B comparison harness** — `eval/compare.py old_results.jsonl
   new_results.jsonl` — diffs two runs row-by-row and reports deltas
   **with confidence intervals + a paired significance test** (§4.4),
   so "3% better" isn't mistaken for signal. Used when changing
   anything that could affect quality.

> **Isolate eval traffic from prod signals (added).** The nightly run
> hits prod `/recall` 60-80× and (a) shows up in the M.11 dashboard,
> skewing real per-route latency/error stats, and (b) costs Sonnet
> tokens every night. Tag eval requests (e.g. an `X-Eval: 1` header →
> an `is_eval` EMF dimension, or a dedicated path) so prod dashboards
> can exclude them, and track the recurring token cost explicitly.
> §4.3 already says eval doesn't share the ConversationStore; this
> closes the dashboard/cost contamination gap too.

### 4.3 What it does NOT do

- Train, fine-tune, or modify any model
- Generate the golden set automatically (that's a one-time-ish
  Claude-assisted exercise per §3.1)
- Run during normal user traffic (it doesn't share the recall
  ConversationStore, it doesn't share rate-limit budgets — the
  latter is *implemented* in Phase 4.1 M.M.2 as an explicit
  `is_eval` quota exemption; without it the weekly end-to-end run
  of ~90-110 recalls trips 4.1's 50-recalls/day cap mid-run)
- Replace manual judgement — the metrics are inputs to decisions,
  not the decisions themselves

### 4.4 Statistical methodology (added)

**The problem:** with a 60-80 query golden set, a single proportion
like recall@5 has a 95% confidence interval of roughly **±11%**. So a
run-to-run swing of "0.74 → 0.77" is almost certainly noise, not a
real improvement. An eval that reports point estimates without
uncertainty will have you chasing ghosts. What we do instead:

- **Confidence intervals via bootstrap.** Resample the per-query
  results with replacement (≈1000×), recompute each aggregate metric,
  report the 2.5/97.5 percentiles as the 95% CI. Every headline number
  ships with its interval.
- **Paired significance test for A/B.** Because both systems run the
  *same* queries, use a **paired** test, not an unpaired one — for the
  binary hit/miss metrics (recall@k, precision@1) that's **McNemar's
  test** on the discordant pairs (queries one system got and the other
  missed). **At our n (~70), discordant counts will often be small
  (b + c < ~25), where the χ² approximation is unreliable — use the
  exact (binomial) form of McNemar in that regime**; `compare.py`
  should pick exact-vs-χ² automatically from b + c. For continuous
  metrics (latency, judge scores) a paired bootstrap or Wilcoxon
  signed-rank.
- **State the minimum detectable effect (MDE)** at the current n in
  the baseline report, so a reader knows the eval's resolution — e.g.
  "at n=70, we can detect a ≥10pt recall@5 change at p<0.05; smaller
  deltas are below our noise floor → grow the set or run more seeds."

> **Why this matters for the portfolio:** "is that delta real?" is the
> first question any RAG-literate reviewer asks. Answering it with CIs
> and a paired test — and being honest about the noise floor at a small
> n — is a stronger signal than a bigger point-estimate with no error
> bars. It also tells *you* when an A/B result is worth acting on.

---

## 5. Tracing / observability for prompts

> **⛔ Blocking decision — resolve before M.E.1, not during M.E.3.**
> **Langfuse v3 does not fit on the current EC2.** The box is a
> **`t4g.small` — 2 GB RAM total** (confirmed 2026-06-29), already
> running app + bot + caddy + litestream. Langfuse v3's hard
> dependency **ClickHouse alone wants 2-4 GB**, before Postgres, Redis,
> and the S3/MinIO blob store it also needs. There is no configuration
> in which Langfuse v3 + the existing stack coexist in 2 GB. So the
> "self-hosted Langfuse on the same EC2" plan as originally written is
> **not viable** — a decision has to be made first, because it changes
> the topology and the M.E.3 milestone materially.

### 5.0 The decision — three honest options

| Option | What it is | Cost / footprint | Verdict |
|--------|-----------|------------------|---------|
| **A. Bigger box** | Vertical-scale `t4g.small` → `t4g.large` (8 GB) so Langfuse v3 fits alongside the app | ~+$36/mo ongoing; one instance to run; still a CDK instance-type change → EBS-deadlock dance | Defensible only if rich trace UX is genuinely needed *now*. |
| **B. Separate Langfuse-only instance** | A second small instance dedicated to Langfuse v3 | ~+$24-48/mo; doubles the infra surface (a 2nd box, 2nd Caddy/DNS, cross-instance auth); the privacy story now spans two machines | Most infra for the least near-term value at single-user scale. |
| **C. ✅ No Langfuse v3 yet — lightweight tracing** | Capture prompt / response / re-rank reasoning as **structured JSONL → S3** (the eval harness already writes JSONL to S3); inspect with `grep`/`jq`; render the candidate→re-rank flow as **Mermaid** in reports. Optionally emit OTel spans to a tiny collector later. | **$0 new infra, 0 GB extra RAM**; stays single-machine; same privacy story | **Recommended for v1.** |

**Recommendation: Option C.** Reasoning:

- **Right-sized to the product.** Langfuse v3's 4-service footprint is
  built for team-scale trace *volume* and *collaboration*. This is a
  single-user product where the real need is "let me inspect why
  *this* recall picked candidate 3" a handful of times — a `jq` query
  over JSONL answers that without a database cluster.
- **We already have the seam.** The M.11 `timed(...)` spans wrap the
  exact Anthropic calls we'd trace; extending them to log
  `{query, candidates, rerank_reasoning, answer, tokens}` as one JSONL
  line per recall is a small change, and the eval harness already
  ships JSONL to S3 (§6.2). The "trace store" is a prefix in the
  bucket we already own.
- **Same build-vs-buy logic as §2.5 and the EMF §4 call** — don't take
  a heavy dependency for a need a few lines of code we control cover.
- **It doesn't burn the bridge.** If trace inspection becomes frequent
  enough to want a real UI, graduate to Option A (bump the instance)
  *then*, with usage data justifying the cost. The JSONL traces can be
  backfilled into Langfuse later — it ingests OTel.

**What Option C gives up:** the Langfuse UI (timeline view, diffing
traces, saved filters, annotation queues). For a single operator
doing occasional deep-dives, `jq` + a rendered Mermaid diagram is
enough. Revisit when that stops being true.

So the rest of §5 (the Langfuse compose/Caddy/secrets plan) is
**deferred, not deleted** — it's the Option-A/graduation design,
kept for when the decision flips. §5.1-5.4 below read as "the plan
*if* we adopt Langfuse," not "the plan."

### 5.1 Why self-hosted (the Option-A graduation plan)

The privacy story we built for the rest of BrainTwin extends here.
Langfuse cloud sees every prompt and response from your Sonnet
calls — the literal contents of your captures. Self-hosted on the
same EC2 keeps that data on infrastructure we control.

Trade-off: real infra. Langfuse **v3** is Postgres **+ ClickHouse +
Redis + S3/MinIO** (~4 services) — which is exactly why it doesn't fit
the current `t4g.small` and why §5.0 defers it. This plan only
activates under **Option A** (bump to `t4g.large`); until then it's
the documented graduation path, not the active design. Verify the
service list against the live Langfuse self-hosting docs when/if
Option A is taken — v3's footprint moves.

### 5.2 Topology

Add two services to the existing `docker-compose.yml`:

```yaml
langfuse-db:
  image: postgres:16-alpine
  volumes:
    - /var/lib/braintwin/data/langfuse-db:/var/lib/postgresql/data
  environment:
    POSTGRES_USER: langfuse
    POSTGRES_PASSWORD: ${LANGFUSE_DB_PASSWORD}    # from secrets.env
    POSTGRES_DB: langfuse
  # cap_drop ALL + user mapping per §14.6 invariant

langfuse:
  image: langfuse/langfuse:latest
  depends_on:
    langfuse-db: { condition: service_healthy }
  environment:
    DATABASE_URL: postgresql://langfuse:${LANGFUSE_DB_PASSWORD}@langfuse-db:5432/langfuse
    NEXTAUTH_URL: https://langfuse.braintwin.net    # ← new subdomain via Caddy
    NEXTAUTH_SECRET: ${LANGFUSE_NEXTAUTH_SECRET}
    SALT: ${LANGFUSE_SALT}
  ports:
    - "127.0.0.1:3000:3000"    # exposed via Caddy
```

**Caddy** gets a second site block for `langfuse.braintwin.net`
with the same AOP + DNS-01 setup as `api.braintwin.net`. Cloudflare
DNS adds a second A record pointing at the same EIP.

**Secrets** for the new env vars land via the M.10 discovery
pattern — `put-secrets.sh` for each of the three new secrets, no
CDK change needed (the discovery loop in user-data picks them up
automatically). This is the first real use of the friction-free
secret-add path we built.

### 5.3 Backend integration

> **⚠️ The snippet below is illustrative, not copy-paste ready —
> verify against current Langfuse (v3) docs.** Two corrections to the
> original draft:
> - `from langfuse.openai import openai` is the **OpenAI** drop-in
>   wrapper — wrong for our **Anthropic** SDK app. There is no
>   equivalent drop-in for Anthropic.
> - `from langfuse.decorators import observe` is the **v2** import
>   path; v3 moved it (`from langfuse import observe`). The whole
>   decorator API changed between v2 and v3.
>
> For the Anthropic SDK the clean route is **OpenTelemetry
> instrumentation** (point an OTel exporter at Langfuse's OTel
> endpoint) or **manual generation spans** — not a "3-line decorator."
> The "3 lines vs OTel's 20" claim was the part that was wrong;
> budget for the OTel path.

Conceptual shape (names/imports to be confirmed against v3 docs):

```python
# backend/knowledge/llm_client.py — conceptual, verify against v3
from langfuse import Langfuse, observe   # v3 import path

langfuse = Langfuse(
    public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
    secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
    host="http://langfuse:3000",  # docker-compose service name
)

@observe(name="anthropic.messages.create", as_type="generation")
async def _anthropic_call(...):
    # log model, input/output tokens, prompt + response as the
    # generation's attributes
    ...
```

When `LANGFUSE_PUBLIC_KEY` is absent (local dev without Langfuse),
the wrapper must degrade to a **no-op** — traces are an add-on, not a
hard dep. Confirm the chosen integration actually no-ops cleanly when
keys are missing (some paths raise on init).

**Relationship to the M.11 `timed` EMF span.** These calls are
*already* wrapped by the `timed(...)` EMF span (latency → CloudWatch).
Langfuse and EMF are complementary, not duplicative: EMF records the
*number* (latency/error → "is it healthy?"), Langfuse records the
*content* (prompt/response/reasoning → "why did this recall behave
this way?"). Keep both; just don't double-count latency in two
dashboards as if they were different metrics.

### 5.4 What lives in Langfuse vs CloudWatch

| Signal | Where | Why |
|--------|-------|-----|
| Per-route HTTP latency | CloudWatch (M.11) | Cheap, aggregated, dashboard-friendly. |
| Anthropic call latency | CloudWatch (M.11) | Same. |
| **Individual prompt + response text** | Trace JSONL → S3 (Option C) | CloudWatch isn't for full prompt bodies; S3 JSONL is grep/jq-able and free. |
| **Re-rank reasoning** | Trace JSONL → S3 (Option C) | We want to inspect WHY Sonnet picked candidate 3 over 1 — render as Mermaid per `recall_id`. |
| **Eval run results** | S3 + CloudWatch summary | Permanent record; CloudWatch for alerting. |

CloudWatch stays the "is the system healthy?" surface. The trace
JSONL in S3 is the "why did this specific recall behave this way?"
surface. (Under the Option-A graduation, the last two rows move to
Langfuse — that's the only thing adopting Langfuse buys: a UI over
data we're already capturing.)

---

## 6. Scheduled eval via GitHub Actions

### 6.0 Cadence — cost-driven split schedule (decided 2026-06-29)

**The naive "nightly full eval" is ~$120-170/month in Anthropic tokens** —
more than the entire rest of BrainTwin's infrastructure combined
(EC2 + EBS + S3 + ECR + CloudWatch + Anthropic-prod all in). At
personal-project scale, that's the wrong trade. So we split the
schedule by cost-per-run:

| Frequency | Harness | Anthropic cost | Rationale |
|-----------|---------|----------------|-----------|
| **Nightly** | **Retrieval-only** (§2.4) — calls `RetrievalService.retrieve()` directly, no Sonnet | **~$0/mo** (embedding is local; no LLM call) | Catches every retrieval-layer regression (recall@k, MRR, nDCG) within 24 hours. Zero marginal cost lets us keep it always-on. |
| **Weekly** | **End-to-end** — hits `/recall`, judged by **Haiku** (not Sonnet) | **~$9-12/mo** at 60-80 queries × Sonnet SUT + Haiku judge × 4 runs | Catches re-rank + answer-layer regressions the retrieval-only run misses. Haiku-as-judge accepted trade — Cohen's κ probably lower than Sonnet-as-judge but the metric is still useful for regression detection, which is the primary purpose. Absolute-quality claims are made only against Sonnet-judged runs (which we run on demand, not on schedule). |
| **On-demand** | Full end-to-end + Sonnet judge | Whatever it costs for one run (~$4-5) | For baseline reports, A/B experiments, and portfolio artifacts where absolute-number defensibility matters. Triggered manually via `workflow_dispatch`. |

**Total scheduled eval cost budget:** ~$10-15/mo, comfortably inside
the ~$45-50/mo total-project ceiling.

**Consequences for the harness implementation:**

- `eval/run_retrieval_eval.py` (retrieval-only) is the nightly binary.
  Zero Anthropic dependency; it's essentially free to run.
- `eval/run_eval.py --judge haiku` is the weekly binary.
- `eval/run_eval.py --judge sonnet` is the on-demand binary — same
  code path, different judge argument.
- The Cohen's κ judge validation (§2.3) is run twice — once for
  Haiku-as-judge (the scheduled-run trustworthiness) and once for
  Sonnet-as-judge (the on-demand trustworthiness). Both κ numbers
  live in the baseline report.

**When we revisit the cadence:**

- If retrieval-only nightly reliably catches everything the weekly
  full-eval catches → the weekly is redundant; drop it, run
  on-demand only.
- If retrieval-only nightly misses regressions the weekly catches
  → the weekly IS earning its cost; keep it, maybe promote to
  every-3-days.
- If we ever monetize (paid tier) or a real portfolio reviewer asks
  for "sub-24-hour end-to-end regression detection" → promote weekly
  to nightly and accept the ~$70/mo cost. Trigger, not a default.

### 6.1 The unblock — OIDC

Phase 4.0.6.1 M.13 (GitHub Actions CI/CD) was deferred because
your `/release` and `/deploy-image` skills covered the deploy
automation use case for one operator. But the eval needs to run
**on a schedule, without an operator at a laptop**. That's the
use case M.13 was actually shaped for; we just didn't have it
yet. Phase 4.0.5 picks up the deferred OIDC plumbing as a
prerequisite.

Scope:

- IAM OIDC identity provider for `token.actions.githubusercontent.com`
- Eval-runner IAM role with scoped permissions:
  - Read `/braintwin/eval_bearer_token` from SSM (to authenticate to
    /recall). **Created on Day 0 with the current shared bearer as its
    value; when Phase 4.1 lands, the *value* is swapped to the eval
    user's long-lived JWT (§3.5 step 7, 4.1 §7.4) — same param name,
    so no IAM or workflow rework at swap time.**
  - Write objects to `s3://braintwin-state-.../eval-results/`
  - Write CloudWatch custom metrics to `BrainTwin/Eval` namespace
- Trust policy condition: `repo:sabyasachibisoyi/BrainTwin:ref:refs/heads/main`

This is narrower than what a full CI/CD deploy role would need
(no CFN, no ECR, no IAM passrole). Easier to justify.

### 6.2 The workflow

`.github/workflows/eval.yml`:

```yaml
name: scheduled-eval
on:
  schedule:
    - cron: '0 6 * * *'          # 06:00 UTC daily — retrieval-only, no Anthropic cost (§6.0)
    - cron: '0 6 * * 0'          # 06:00 UTC Sunday — end-to-end + Haiku judge (§6.0)
  workflow_dispatch:              # manual trigger; supports `judge=sonnet` for on-demand full runs

jobs:
  eval:
    runs-on: ubuntu-latest
    permissions:
      id-token: write       # OIDC token request
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::494567491756:role/BrainTwin-EvalRunnerRole
          aws-region: us-west-2
      - name: Fetch bearer token
        run: |
          TOKEN=$(aws ssm get-parameter \
            --name /braintwin/eval_bearer_token --with-decryption \
            --query Parameter.Value --output text)
          echo "::add-mask::$TOKEN"   # mask in logs before exporting
          echo "BACKEND_BEARER_TOKEN=$TOKEN" >> $GITHUB_ENV
      - name: Determine run mode
        # Branch on WHICH cron fired (github.event.schedule), NOT on the
        # calendar date: on Sundays BOTH crons fire, and a date-based
        # check would classify both runs as end-to-end — double judge
        # cost, duplicate S3/CloudWatch datapoints skewing the weekly
        # trend, and a Sunday-shaped hole in the nightly retrieval-only
        # series. With the schedule check, Sunday correctly gets one
        # retrieval-only run AND one end-to-end run.
        run: |
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            MODE="${{ inputs.mode || 'end_to_end' }}"
            JUDGE="${{ inputs.judge || 'haiku' }}"
          elif [ "${{ github.event.schedule }}" = "0 6 * * 0" ]; then
            MODE="end_to_end"; JUDGE="haiku"
          else
            MODE="retrieval_only"; JUDGE="none"
          fi
          echo "MODE=$MODE" >> $GITHUB_ENV
          echo "JUDGE=$JUDGE" >> $GITHUB_ENV
      - name: Run eval
        run: |
          if [ "$MODE" = "retrieval_only" ]; then
            python eval/run_retrieval_eval.py \
              --golden-set eval/golden_set.jsonl \
              --output-dir results/$(date -u +%Y-%m-%d)
          else
            python eval/run_eval.py \
              --target https://api.braintwin.net \
              --golden-set eval/golden_set.jsonl \
              --judge $JUDGE \
              --output-dir results/$(date -u +%Y-%m-%d)
          fi
      - name: Upload results to S3
        run: aws s3 cp results/ s3://braintwin-state-.../eval-results/ --recursive
      - name: Publish CloudWatch summary metrics
        run: python eval/publish_metrics.py results/summary.json
```

### 6.3 Alerting

Two CloudWatch alarms on the `BrainTwin/Eval` namespace:

- `recall@5 < 0.7` for 2 consecutive runs → alarm
- `precision@1 < 0.6` for 2 consecutive runs → alarm

Both wire to the same SNS topic as the budget alarms (M.2.h)
delivering to the operator email. Two consecutive runs avoids
single-day noise.

---

## 7. Milestones

In dependency order:

### M.E.1 — Golden set bootstrap (~1 week)

- Write `eval/bootstrap_golden_set.py`: walks every capture,
  Sonnet-generates a candidate Q&A pair per capture (with the
  paraphrase/anti-leakage prompt, §3.1), writes `candidates.jsonl`.
- Build a small CLI review tool (`eval/review.py`) that walks
  candidates with keep/reject/edit choices **and surfaces the
  question↔source token-overlap so leak suspects are obvious** (§3.1).
- Curate 60-80 pairs into `eval/golden_set.jsonl`, using the graded
  `relevant_captures` label (§3.3) — flag a second acceptable capture
  where one exists.
- ~10-15 conversational pairs in `eval/golden_set_conversational.jsonl`.
- ~10-15 **negative / no-answer queries** in
  `eval/golden_set_negative.jsonl` (§3.4).

**Decision after M.E.1:** is the set big enough? Some metrics
(MRR, recall@k) need decent sample size to stabilise. If 60 feels
thin, generate another candidate batch and review more.

### M.E.2 — Scoring + harness (~4-5 days)

- `eval/scoring.py` — pure functions for recall@k, MRR, **nDCG**,
  precision@1, plus the LLM-judge faithfulness/answer-relevancy call
  (judge ≠ ratee, §2.3).
- `eval/run_eval.py` (end-to-end) **and `eval/run_retrieval_eval.py`
  (retrieval-only, §2.4)** — two entry points over one golden set.
- `eval/compare.py` — A/B diff between two runs **with bootstrap CIs +
  McNemar paired test** (§4.4), not bare deltas.
- `eval/validate_judge.py` — score a human-labelled sample, report
  Cohen's κ (§2.3).
- Pin the re-rank to **`temperature=0`** for eval determinism (§4.1).
- Tests: each scorer with synthetic inputs; CI math against a known
  fixture.

> Estimate bumped from ~3 days: nDCG, the retrieval-only second
> harness, the stats layer, and judge validation are real additions —
> but they're the difference between a demo and a defensible eval.

### M.E.3 — Lightweight prompt tracing (Option C, ~1-2 days)

Per the §5.0 decision (Langfuse v3 doesn't fit the `t4g.small`), v1
tracing is JSONL → S3, not Langfuse:

- Extend the existing M.11 `timed(...)` spans (or add a sibling) to
  write one structured **trace line per recall** —
  `{recall_id, query, candidates[], rerank_reasoning, answer,
  confidence, input_tokens, output_tokens, sut_version}` — to a trace
  log.
- Ship the trace log to `s3://braintwin-state-.../traces/` (reuse the
  eval S3 path + IAM; no new infra, no compose change, **no
  EBS-deadlock dance**).
- A small `eval/trace.py` helper: `jq`-style filters + render a
  candidate→re-rank flow as a **Mermaid** diagram for a given
  `recall_id` (drop into reports / PRs).
- Smoke check: run a recall, find its trace line in S3, render the
  Mermaid.

> **No-op when disabled:** trace writing is gated on an env flag so
> local dev and prod can run without it. Privacy: traces contain
> capture contents → same bucket, same encryption, same access scope
> as the rest of BrainTwin state.

**Deferred (graduation — only if Option A is taken):** the full
Langfuse self-host — compose services, `langfuse.braintwin.net` Caddy
block + DNS, the 5 `put-secrets.sh` secrets, backend OTel wiring, and
the EBS-deadlock dance for the user-data change. Design lives in
§5.1-5.4; it activates when trace-inspection frequency justifies a
`t4g.large`.

### M.E.4 — GitHub Actions OIDC + nightly run (~2-3 days)

- CDK construct for the OIDC provider + scoped IAM role.
- `.github/workflows/eval.yml` per §6.2.
- One manual workflow run end-to-end before enabling the cron.
- CloudWatch alarms per §6.3.

### M.E.5 — Baseline + first A/B (~2 days)

- Run the eval against current cloud; capture the baseline
  numbers. **This is the first time we know what good is.**
- Pick ONE small change to A/B against the baseline (e.g., RRF
  k=60 → k=30). Run, compare, write up the result.
- The point isn't the specific change. It's proving the loop
  works end-to-end: change → eval → decision.

---

## 8. What's out of scope

- **Multi-user eval.** All queries authenticate as the default
  user. Phase 4.1 (use case A) brings multi-tenancy; eval for
  that is a different design.
- **Adversarial eval.** Prompt-injection resistance, jailbreak
  attempts, etc. — important eventually, not for a single-user
  personal product right now.
- **A/B testing on live users.** We're a single user. Live A/B
  doesn't apply yet.
- **Cost regression eval.** The harness records token spend per
  query, but we don't gate deploys on cost yet. Tracked as
  follow-up.
- **Local-LLM eval.** The Phase 5L doc (local-first) is a
  separate roadmap. When/if that ships, the SAME eval harness
  runs against the local target — that's why §4.2 has `target_url`
  as a parameter, not hardcoded.

---

## 9. Success criteria

Phase 4.0.5 is done when:

- The eval harness runs against `api.braintwin.net` and produces
  scored results for all golden-set queries in under 10 minutes
- Both layers are scored: a **retrieval-only** baseline (recall@k,
  MRR, nDCG) and an **end-to-end** baseline (precision@1,
  faithfulness, answer relevancy) — so regressions can be attributed
  to a stage (§2.4)
- A baseline exists for each headline metric **with bootstrap
  confidence intervals** (§4.4) — published in
  `docs/eval-baseline-YYYY-MM-DD.md`
- One A/B comparison has been done end-to-end (a non-trivial change
  tested against baseline) and documented **with a paired
  significance test**, not just a point-estimate delta
- The LLM judge has been **validated against human labels** on a
  ~20-30 sample (report the Cohen's κ); if κ is poor we say so rather
  than quoting the judge metric as fact (§2.3)
- Lightweight tracing (Option C, §5.0) is live: a real recall's
  trace line lands in `s3://braintwin-state-.../traces/`, and
  `eval/trace.py` renders its candidate→re-rank Mermaid diagram
  (the Langfuse criterion from the original draft is retired with
  the §5.0 decision — it returns only if Option A is ever taken)
- The nightly GitHub Actions run has executed for ≥7 consecutive
  nights without manual intervention; the CloudWatch alarms
  haven't false-fired
- A new entry §14.11 is added to `phase4.0.6-deployment-design.md`
  capturing any operational invariants we discovered during this
  phase (e.g., "Sonnet-as-judge bias direction is consistent
  across runs which is what enables regression detection")

### 9.1 Portfolio artifacts (added) — the things a reviewer actually opens

The harness is the *capability*; these are the *evidence*. Ship them:

- **A published baseline report** (`docs/eval-baseline-YYYY-MM-DD.md`):
  metrics table **with CIs**, a one-line method note, and — most
  important — **2-3 qualitative failure cases** ("query X should have
  found Y; it ranked Z first; here's the likely cause"). The narrative
  failure analysis is what demonstrates depth; a table of numbers
  alone doesn't.
- **A quality trend over time** — recall@5 per nightly run as a
  committed chart or a CloudWatch dashboard widget. "Quality tracked
  over time" reads as production maturity.
- **Embedder choices referenced against standard benchmarks** — when
  you A/B the embedder, cite **MTEB** (embedding benchmark) and
  **BEIR** (retrieval benchmark) so the choice sits against known
  baselines instead of in a vacuum.
- **(Optional) an eval-gated CI check** — `promptfoo` or a `pytest`
  gate that fails the build (advisory at first) if recall@5 on the
  smoke subset drops below a floor. "Eval-gated deploys" is a strong
  line even kept non-blocking.

---

## 10. Open decisions deferred

Things to decide while building, not in advance:

- **Faithfulness judge model — Sonnet or a different model?**
  Same-model bias is real. A different model (a local 8B, or
  Haiku as judge for Sonnet) reduces bias but changes the rating
  distribution. Probably worth trying both once the harness
  exists and seeing if they agree directionally.
- **How often does the golden set need to refresh?** Captures
  accumulate; queries the eval covers vs queries users actually
  issue can drift. Probably a quarterly review of the set.
- **What's the regression alarm threshold actually?** 0.7
  recall@5 / 0.6 precision@1 are placeholders until we have a
  baseline. Set the real thresholds after M.E.5 baseline.
- **Multi-turn eval ratio.** Single-turn vs multi-turn queries
  in the golden set — currently §3.1/§3.2 split is roughly 75/25.
  Adjust based on what real usage looks like.

---

## 11. References

- Main cloud deployment design: `phase4.0.6-deployment-design.md`
  (§14.10 in particular — bounded-cardinality applies to eval
  custom metrics too)
- Polish phase: `phase4.0.6.1-polish-design.md` (§2.3 captured
  the M.13 OIDC decision that 4.0.5 actually delivers on)
- The recall agent under test: `backend/agent/recaller.py`
- The retrieval pipeline: `backend/agent/retrieval.py`
- The earlier deferment of this phase: §8 of
  `phase4.0.6-deployment-design.md` — "Phase 4.0.5 (eval) — runs
  on the same EC2 or a sidecar." The eval harness runs on the same
  EC2; the *sidecar* (Langfuse) turned out not to fit the
  `t4g.small` (§5.0), so tracing is JSONL→S3 for v1.
- Local-first ideation (out-of-band): `local-first-design.md`
  (the eval harness is the canonical way to compare local vs
  cloud quality if Phase 5L ever graduates from ideation)

---

## Appendix — Worked math for every metric

Every number in the baseline report should be traceable back to
these definitions. Use this appendix when a metric shifts and
you're trying to understand *what actually changed* in the
underlying arithmetic.

### A.1 Setup — a single concrete query

Every metric definition below is walked through on this one query
so the arithmetic is grounded in a specific example.

**Query row from the golden set:**

```json
{
  "id": "q-001",
  "query": "what did I read about baking cakes",
  "relevant_captures": [
    {"capture_id": "A", "grade": "primary"},     // grade 2
    {"capture_id": "B", "grade": "acceptable"}   // grade 1
  ]
}
```

**Grade convention:** `primary` = 2, `acceptable` = 1, not
relevant = 0.

**System returns top-5:** `[D, A, C, B, E]` where D, C, E are not
in `relevant_captures` (grade 0).

### A.2 recall@k

**Formula:**

```
recall@k(q) = 1 if T_k(q) ∩ R(q) ≠ ∅ else 0
recall@k    = (1 / N) · Σ recall@k(q)  for q in all queries
```

Where `T_k(q)` is the system's top-k for query q, and `R(q)` is
the set of relevant capture_ids from the golden set.

**Worked:**

- `recall@1(q-001)`: T₁ = `{D}`. R = `{A, B}`. Intersection = ∅. → **0**
- `recall@3(q-001)`: T₃ = `{D, A, C}`. Intersection = `{A}`. → **1**
- `recall@5(q-001)`: T₅ = `{D, A, C, B, E}`. Intersection = `{A, B}`. → **1**

Aggregate: if 60 of 100 golden-set queries score `recall@5(q) = 1`,
then `recall@5 = 0.60`.

### A.3 precision@1

**Formula:**

```
precision@1(q) = 1 if T_1[0] ∈ R(q) else 0
precision@1    = (1 / N) · Σ precision@1(q)
```

**Worked:**

- `precision@1(q-001)`: top-1 is `D`, not in `R`. → **0**

Note: for our single-primary rows, `precision@1(q) == recall@1(q)`
by construction. They diverge only with graded/multiple relevance.

### A.4 MRR (Mean Reciprocal Rank)

**Formula:**

```
rank(q)             = position (1-indexed) of first relevant capture
                      in the ranked list, or ∞ if none
reciprocal_rank(q)  = 1 / rank(q), or 0 if rank(q) = ∞
MRR                 = (1 / N) · Σ reciprocal_rank(q)
```

**Worked:**

- For q-001: ranking is `[D, A, C, B, E]`. First relevant is `A`
  at position 2. → `rank = 2`, `reciprocal_rank = 0.5`.

Interpretation: MRR = 0.5 means on average the first correct
answer is at position 2. MRR = 1.0 means it's always at position 1.
MRR = 0.25 means it's on average around position 4.

### A.5 nDCG@k — the full walkthrough

Three steps: **DCG@k** (what we got), **IDCG@k** (the ceiling),
**nDCG@k** = ratio.

#### Step 1 — DCG@k (Discounted Cumulative Gain)

**Formula:**

```
DCG@k(q) = Σᵢ (grade_i / log₂(i + 1))    for i = 1..k
```

Where `grade_i` is the relevance grade of the result at position i
(0 if not in `relevant_captures`, else the grade from the golden
set: 1 for `acceptable`, 2 for `primary`).

**Worked for q-001:**

| Position i | Result | Grade | Discount = log₂(i+1) | Contribution = grade / discount |
|-----------|--------|-------|----------------------|--------------------------------|
| 1 | D | 0 | log₂(2) = 1.000 | 0 / 1.000 = 0.000 |
| 2 | A | 2 | log₂(3) = 1.585 | 2 / 1.585 = **1.262** |
| 3 | C | 0 | log₂(4) = 2.000 | 0 / 2.000 = 0.000 |
| 4 | B | 1 | log₂(5) = 2.322 | 1 / 2.322 = **0.431** |
| 5 | E | 0 | log₂(6) = 2.585 | 0 / 2.585 = 0.000 |

`DCG@5(q-001) = 0 + 1.262 + 0 + 0.431 + 0 = 1.693`

**Intuition:** the deeper in the list, the more the contribution
is discounted by `log₂(rank + 1)`. Position 1 has no penalty,
position 2 is discounted 37%, position 5 is discounted 61%.

#### Step 2 — IDCG@k (Ideal DCG — the ceiling)

**Formula:** DCG@k computed on the *ideal ranking* — the one that
puts the highest-graded relevant items first, in descending grade
order, padded with irrelevants for the remaining positions up to k.

For q-001, the ideal top-5 is `[A (grade 2), B (grade 1), _, _, _]`
where `_` means "irrelevant, grade 0."

| Position i | Result | Grade | Discount | Contribution |
|-----------|--------|-------|----------|--------------|
| 1 | A | 2 | 1.000 | 2 / 1.000 = **2.000** |
| 2 | B | 1 | 1.585 | 1 / 1.585 = **0.631** |
| 3 | (irrelevant) | 0 | 2.000 | 0 |
| 4 | (irrelevant) | 0 | 2.322 | 0 |
| 5 | (irrelevant) | 0 | 2.585 | 0 |

`IDCG@5(q-001) = 2.000 + 0.631 + 0 + 0 + 0 = 2.631`

**Intuition:** this is the perfect score achievable for this query.
It's determined entirely by the golden set labels — it doesn't
depend on the system at all. It's the denominator that normalises
nDCG into [0, 1].

#### Step 3 — nDCG@k (Normalized DCG)

**Formula:**

```
nDCG@k(q) = DCG@k(q) / IDCG@k(q)
nDCG@k    = (1 / N) · Σ nDCG@k(q)
```

**Worked for q-001:**

`nDCG@5(q-001) = 1.693 / 2.631 = 0.643`

**Interpretation:** the system captured ~64% of the value available
on this query. If A had been at position 1 instead of 2:

- New DCG@5 = 2.000 + 0.431 = 2.431
- New nDCG@5 = 2.431 / 2.631 = **0.924**

A single-rank improvement of the primary hit shifts the score from
0.64 to 0.92 — big, because the log₂ discount penalises depth
heavily.

**Why nDCG earns its keep only with graded relevance:** with one
`primary` per query and no `acceptable` entries, the ideal ranking
is just `[primary at position 1]` and IDCG collapses to 2. The
math then reduces to `nDCG@k = (2 / log₂(rank_of_primary + 1)) / 2
= 1 / log₂(rank_of_primary + 1)`, which is a fixed monotonic
function of MRR. Only once queries have *multiple graded* relevant
captures do nDCG and MRR say different things.

### A.6 Faithfulness (LLM-as-judge)

**Formula:**

```
For answer A over contexts C:
  claims        = judge.extract_claims(A)
  entailed(c)   = 1 if any context in C entails c, else 0
  faithfulness(q) = Σ entailed(c) / |claims|
```

**Worked (illustrative — actual judge does the arithmetic):**

Answer A: *"The article recommended flour-first mixing, and said
365°F is optimal for pound cakes."*

Judge extracts three claims:
1. "Flour is mixed first"
2. "365°F is recommended"
3. "This applies to pound cakes"

Judge checks each against the retrieved snippets:
- Claim 1: entailed ✓
- Claim 2: entailed ✓
- Claim 3: NOT entailed (snippets talked about sponge cakes) ✗

`faithfulness(q-001) = 2 / 3 = 0.667`

**Aggregate:** `mean(faithfulness(q))` across all queries.

**Cohen's κ against humans:** hand-label ~20-30 (q, A, C) triples
with your own faithfulness scores, compute agreement between your
label and the judge's. κ ≥ 0.6 → judge is trustworthy; κ < 0.4 →
the metric is noise and we report as such.

### A.7 Answer relevancy (LLM-as-judge)

**Formula:**

```
For question Q and answer A:
  generated_Qs  = judge.generate_reverse_questions(A, n=5)
  # "given A, what N questions could A plausibly answer?"
  emb_Q         = embed(Q)
  emb_Gs        = [embed(G) for G in generated_Qs]
  answer_relevancy(q) = mean(cosine_similarity(emb_Q, G) for G in emb_Gs)
```

**Intuition:** if the answer is on-topic, the questions it *could*
answer should look semantically similar to the original question.
An answer that's technically faithful but wanders will produce
generated questions dissimilar from Q, and the score drops.

### A.8 Bootstrap confidence intervals

**Formula (bootstrap resampling):**

```
Given per-query results r_1..r_N (e.g., recall@5(q) ∈ {0, 1}):

For b in 1..1000:
  sample_b = draw N results with replacement from r_1..r_N
  metric_b = aggregate(sample_b)  # e.g., mean

Sort metric_1..metric_1000.
95% CI = (metric[25], metric[975])   # 2.5 and 97.5 percentiles
```

**Why this matters:** at n=70 queries, `recall@5` has a 95% CI of
roughly **±11%** on the aggregate. A run-to-run swing of "0.74 →
0.77" is almost certainly noise. The eval harness always reports
`mean [lower, upper]` so noise is visible instead of hidden.

### A.9 A/B comparison — paired significance test

Two systems (baseline B, variant V) run on the *same* golden set,
producing per-query results `b_1..b_N` and `v_1..v_N`.

**For binary metrics (recall@k, precision@1) — McNemar's test:**

Build a 2×2 contingency table over the N queries:

|                      | V hit | V miss |
|----------------------|-------|--------|
| **B hit**            | a     | b      |
| **B miss**           | c     | d      |

`a` and `d` are agreements (both got it right or both missed);
`b` and `c` are *disagreements*.

```
McNemar statistic (with continuity correction) = (|b - c| - 1)² / (b + c)
p-value = 1 - χ²_cdf(statistic, df=1)
```

**Small-sample regime (b + c < ~25 — likely at our n):** the χ²
approximation above is unreliable; use the **exact binomial test**
instead — under H₀ each discordant pair is a fair coin, so
`p = 2 · P(X ≤ min(b, c))` for `X ~ Binomial(b + c, 0.5)` (capped
at 1). `compare.py` selects exact vs χ² automatically from b + c.

**Interpretation:** if `b == c` the two systems disagree equally
often — no signal. If `b >> c` or `c >> b`, one system is
systematically beating the other on the disagreements. p < 0.05
means the improvement is real, not noise.

**For continuous metrics** (nDCG, faithfulness, latency): use a
**paired bootstrap** on the per-query deltas `d_q = v_q - b_q`
(resample the deltas, take the mean each time, get 95% CI on the
mean delta). If the CI excludes 0, the delta is statistically
significant.

### A.10 Composite scores — deliberately absent

There is no single-number aggregate in this design. The A/B report
is a **table** showing each metric's delta + significance so you
can weigh trade-offs deliberately:

```
                   Baseline   Variant    Delta   p       Verdict
recall@5:          0.82       0.85       +3pt    0.04    ✓ significant
precision@1:       0.48       0.51       +3pt    0.11    (not sig, n too small)
faithfulness:      0.87       0.86       −1pt    0.72    (noise)
latency p95:       2.3s       2.7s       +0.4s   0.001   ⚠ slower
token_spend/q:     4500       6200       +1700   —       ⚠ costs more
```

**How you'd read that table:** quality is marginally up, cost is
noticeably up, latency is worse. Probably not worth shipping
unless the recall@5 improvement matters for a specific user pain
point AND we can bring the cost down.

Compressing five columns into one "quality score" would hide the
cost + latency trade. The whole point of the eval is to *make the
trade explicit*, not to average it away.

---

## 12. Revision log — why the design changed (2026-06-29 review)

After a code-review pass against the actual recall pipeline
(`backend/agent/recaller.py`, `retrieval.py`, `knowledge/llm_client.py`),
the draft was hardened. Each change and its reason:

| Change | Why |
|--------|-----|
| **§2.4 stage-level attribution** (retrieval-only + end-to-end harnesses) | An API-only eval can't tell whether a regression is in the embedder/BM25/RRF/chunking or the re-rank — yet those are §1's tuning levers. Splitting the harness is the change that makes the eval *actionable*, and it's near-free because `RetrievalService` is already a clean seam. |
| **§2.1 RAGAS taxonomy + nDCG; faithfulness split into faithfulness + answer relevancy** | Standard vocabulary reads as field-literate; the original "faithfulness" conflated three distinct RAGAS metrics, hiding faithful-but-irrelevant failures. |
| **§3.3 graded `relevant_captures` list** (was single `expected_capture_id`) | Vague recall can have several right answers; single-truth labels score relevant results as misses and inflate A/B noise. Enables nDCG graded relevance too. |
| **§3.4 negative / no-answer queries** | `no_match rate` alone is ambiguous; without ground truth for "should decline," a system that answers everything looks healthy while being broken. |
| **§3.1 lexical-leakage guard** | Generating questions *from* source text leaks rare terms → inflates BM25/embedding recall. Paraphrase prompt + overlap flag in review. |
| **§2.3 judge rigor** (judge ≠ ratee, pairwise + order-swap, human-κ validation) | An unvalidated self-judge is a portfolio red flag and only supports relative-not-absolute claims. κ-validation is what makes the metric trustworthy. |
| **§4.4 statistical methodology** (bootstrap CIs, McNemar paired test, MDE) | At n≈70 a single proportion's 95% CI is ±~11%; without CIs you chase noise. "Is that delta real?" is the first question a reviewer asks. |
| **§4.1 determinism (`temperature=0`) + SUT versioning** | Re-rank runs at default temp (~1.0) → eval drifts run-to-run independent of code, defeating regression detection. SUT version makes A/B-over-time comparable. |
| **§4.2 eval-traffic isolation** | Nightly prod hits skew the M.11 dashboard and cost Sonnet tokens; tag them so prod signals stay clean. |
| **§5.0 tracing decision — Langfuse v3 deferred, Option C (JSONL→S3) adopted for v1** | **Hard constraint:** the EC2 is a `t4g.small` (2 GB); ClickHouse alone wants 2-4 GB, so Langfuse v3 + existing stack cannot coexist. This is a topology decision that had to be made *before* M.E.1, not discovered during M.E.3. Option C right-sizes tracing to a single-user product at $0/0 GB and keeps the Langfuse plan as a documented graduation path (Option A: bump to `t4g.large`). |
| **§5.1/§5.3 Langfuse corrections** (v3 = Postgres+ClickHouse+Redis+S3; OpenAI-wrapper & v2 decorator imports were wrong for Anthropic) | The original understated the self-host footprint and the integration effort; flagged verify-against-live-docs rather than shipping wrong snippets. Now folded under the deferred Option-A plan. |
| **§9.1 portfolio artifacts** (baseline report w/ CIs + failure cases, trend chart, MTEB/BEIR refs, eval-gated CI) | The harness is the capability; these are the evidence a reviewer opens. |
| **§5.3 EMF↔Langfuse relationship** | Clarified they're complementary (EMF = the number, Langfuse = the content) so latency isn't double-counted. |

Milestone estimates adjusted accordingly (M.E.2 ~3→~4-5d; M.E.3
~3-4d Langfuse → **~1-2d for Option C lightweight tracing**, with the
Langfuse build deferred to a graduation milestone). The M.E.2
additions are the gap between a demo and a defensible eval; the M.E.3
change is right-sizing observability to the box we actually have.

### 12.1 Second pass — clarifications from Sabya's Q&A read-through

Not new-scope additions; explicit answers to real questions that
came up while Sabya was reading the doc. Each maps to a specific
section:

| Addition | Why |
|----------|-----|
| **§2.3 three-roles table** (bootstrap generator, system under test, judge) | The rule "judge ≠ ratee" applies only to one of three model roles. Making the roles explicit prevents "why do we use Sonnet AND another model?" confusion in review. |
| **§2.6 diagnostic tree** (metric → cause → cheap-to-expensive levers) | Reading the baseline number is only half the eval loop; knowing which lever to pull for a given failure mode is the other half. Turns the report into an action plan. |
| **§3.5 eval user provisioning + freeze semantics** | Golden-set labels reference specific `capture_id`s. If the corpus keeps changing under those labels, metrics drift for reasons unrelated to code. Freeze at creation, refresh only as a ceremonial event. Also names the Phase 4.1 dependency (the eval user is provisioned as part of M.M.1). |
| **Appendix — Worked math** (recall@k, MRR, nDCG w/ IDCG, faithfulness, answer relevancy, bootstrap CIs, McNemar) | Every number in the baseline report should be traceable back to a formula worked on a concrete example. Removes "trust me it's right" from the eval story. |
| **§6.0 cost-driven cadence** (nightly retrieval-only + weekly end-to-end w/ Haiku judge + on-demand Sonnet judge) | Naive nightly full-eval is $120-170/mo — more than the entire rest of BrainTwin's infra combined. The split schedule keeps regression detection at 24-hour latency for the retrieval layer (where most tuning happens anyway) and 7-day latency for the answer layer, at ~$10-15/mo. Anchored to the ~$45-50/mo project-wide cost ceiling. |

### 12.2 Third pass — cross-phase integration review (2026-07-02)

A reviewer pass focused on the seams *between* this phase and Phase
4.1 (implemented concurrently), plus doc-internal contradictions:

| Change | Why |
|--------|-----|
| **§9 Langfuse success criterion retired** | Contradicted the §5.0 Option C decision — the criterion predated it. Replaced with the JSONL-trace + Mermaid smoke criterion. |
| **§6.2 Sunday double-fire fix** (branch on `github.event.schedule`) | Both crons fire Sunday 06:00; the date-based check ran end-to-end twice (double judge cost, duplicate datapoints) and skipped retrieval-only entirely on Sundays. |
| **§6.1/§6.2 SSM param → `/braintwin/eval_bearer_token`** | The IAM scope and workflow referenced the old shared `/braintwin/bearer_token` while §3.5 minted to `eval_bearer_token`. One name from Day 0 (value swapped post-4.1) avoids IAM/workflow rework and the "4.1 lands first, nightly breaks" trap (4.1 §7.4). |
| **§3.5 sequencing unblock** (golden set against current capture_ids, remap via lineage) | As written, M.E.1 chained behind 4.1 M.M.1 (eval-user UUIDs). The lineage table doubles as a remap key, so curation — the longest human-paced milestone — starts immediately. |
| **§4.4/A.9 exact McNemar for small discordant counts** | At n≈70, b+c is often < 25 where the χ² approximation misleads — on exactly the number a reviewer probes. |
| **§4.3 quota-exemption cross-ref** | "Doesn't share rate-limit budgets" was an unimplemented promise: the weekly e2e run (~90-110 recalls) exceeds 4.1's 50/day cap. Named the implementing mechanism (4.1 M.M.2 `is_eval` exemption). |
| **§3.5 step 7 revocation warning** | Bumping the eval user's `token_version` silently 401s every nightly run — flagged as an alarm condition with a re-mint procedure. |

---

*Author: Sabya (with Claude as design partner). Created 2026-06-29
after Phase 4.0.6.1 polish closed. The cloud product is now mature
enough that "is it any good" becomes the right next question —
and 4.0.5 builds the harness to answer it empirically rather than
intuitively. Numbered 4.0.5 to honour the original sequencing
intent (eval was meant to land before deploy; we did them out of
order because the eval needed a deploy target to exist first).
Revised 2026-06-29 after a review pass against the live recall
pipeline — see §12 for the what-and-why of each hardening change.
Second-pass 2026-06-29 (same day) after Sabya's Q&A read-through:
added §2.3 three-roles table, §2.6 diagnostic tree, §3.5 eval user
provisioning + freeze semantics, and the Worked-Math Appendix.
See §12.1 for the second-pass mapping.*
