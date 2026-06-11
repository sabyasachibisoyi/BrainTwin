# `BrainTwin/docs/diagrams/` — per-flow Mermaid diagrams

This folder holds **application-behavior** diagrams (what happens
inside the request lifecycle). AWS topology + the backup/restore
drill live in the companion repo at
**[sabyasachibisoyi/BrainTwinCDK/diagrams/](https://github.com/sabyasachibisoyi/BrainTwinCDK/tree/main/diagrams)**.

## What's here

| File | Captures |
|------|----------|
| `flow-capture.md` | Capture path: Chrome → /capture → SQLite + Chroma + S3 |
| `flow-recall.md` | Recall happy path: RetrievalService + RRF + Sonnet rerank |
| `flow-refinement.md` | Multi-turn refinement (U.3) — "no fresh retrieval" branch |
| `flow-failure-modes.md` | Degraded behavior per component (Chroma / Sonnet / EBS / …) |

## What moved to BrainTwinCDK

| File | Why it moved |
|------|--------------|
| `architecture.py` / `architecture.png` | AWS topology — belongs next to the CDK that creates the resources |
| `flow-backup-restore.md` | Pure infra operation; runs against AWS |

## Why the split

Two-repo design: `BrainTwin` is the **application**, `BrainTwinCDK` is
the **cloud infrastructure**. A reader who only cares about "what does
this product do" reads BrainTwin and sees the request flows. A reader
who cares about "how is it deployed" reads BrainTwinCDK and sees the
topology + the restore drill.

Design rationale in `phase4.0.6-deployment-design.md` §6.

## Viewing Mermaid

All files in this folder are Mermaid. To preview locally:

- **GitHub** — renders natively in the web UI; just push the branch
- **Cursor / VS Code** — install the `bierner.markdown-mermaid`
  extension, then `Cmd+Shift+V` on any `.md` file
- **CLI** — `npx -p @mermaid-js/mermaid-cli mmdc -i flow-capture.md -o /tmp/x.png`

## Maintenance contract

A PR that changes application request flow MUST update the relevant
`flow-*.md` here. A PR that changes AWS topology MUST update
`architecture.py` over in BrainTwinCDK. PRs that change both
(e.g. introduce a new outbound dependency) require coordinated commits
to both repos.
