# `docs/diagrams/` — architecture HLD

Canonical source of truth for the BrainTwin / DigitalTwin cloud
architecture. See `docs/phase4.0.6-deployment-design.md` §6 for the
design rationale; this folder holds the actual artifacts.

## Layout

```
docs/diagrams/
├── README.md                 # this file
├── architecture.py           # Python `diagrams` topology source
├── architecture.png          # ← regenerated from architecture.py
├── flow-capture.md           # Mermaid: capture path
├── flow-recall.md            # Mermaid: recall happy path
├── flow-refinement.md        # Mermaid: multi-turn refinement (U.3)
├── flow-failure-modes.md     # Mermaid: degraded-mode behavior
└── flow-backup-restore.md    # Mermaid: backup + restore drill
```

After Phase 4.0.6 M.2 (CDK lands), one more file gets auto-generated:

```
└── cdk-generated.png         # ← `cdk-dia` output; compare against architecture.png
```

## Three-layer strategy (recap)

| Layer | Tool | What it captures |
|-------|------|------------------|
| Topology | `architecture.py` → PNG (Python `diagrams`, official AWS icons) | The big static picture |
| Flows | `flow-*.md` (Mermaid, renders inline on GitHub) | Per-request lifecycles |
| Verification | `cdk-dia` (post-M.2) | Auto-generated from CDK; catches drift |

## How to regenerate `architecture.png`

One-time setup on your Mac:

```bash
brew install graphviz
pip install diagrams
```

Then any time you change `architecture.py`:

```bash
cd /path/to/BrainTwin
python docs/diagrams/architecture.py
```

This writes `docs/diagrams/architecture.png`. Commit both files in
the same PR so the picture and its source never drift.

## How to view the Mermaid flows

Just open the `.md` files on GitHub — they render natively. Locally,
any IDE with Mermaid preview (VS Code + Markdown Preview Mermaid
Support, JetBrains, Obsidian) will render them inline.

## Maintenance contract

A PR that adds or removes infrastructure resources MUST also update:

- `architecture.py` (and regenerate the PNG) if a topology node /
  edge changes
- The relevant `flow-*.md` if a request path / failure path changes

A future CI check (Phase 4.0.6.1, with the GitHub Actions pipeline)
will fail if `architecture.png` is older than `architecture.py`.

## Why not Miro / Lucidchart / drawio

- **Miro / Lucidchart** — not version-controlled, not PR-reviewable,
  not greppable. Fine for ad-hoc whiteboards; rejected as canonical.
- **drawio** — the XML *can* be committed, but the editor is GUI-only
  so PR review shows blob diffs, not human-readable changes.

The picture-as-code approach above is the smallest setup that gives us
a diagram every reviewer can read AND a diff every reviewer can review.
