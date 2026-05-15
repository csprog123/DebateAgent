# CLAUDE.md — Multi-Agent Adversarial Debate Tool

## Purpose
Stress-test a draft proposal through a structured 3-round adversarial debate
between four specialised Claude agents (Advocate, Devil's Advocate,
Risk & Legal, Adaptive Stakeholder). The output is a Markdown and DOCX
"analytical pressure chamber" report with a Resilience Score (0–100).

## Stack
- **Python 3.11+** — orchestrator, agents, scoring, Markdown writer
- **OpenAI Python SDK pointed at OpenRouter** — `AsyncOpenAI` with `base_url=https://openrouter.ai/api/v1`, no agent frameworks. Default model `anthropic/claude-sonnet-4.5` (override via `MODEL_ID` env var).
- **Node.js + `docx` ^8.x** — DOCX report generator (`generate_report.js`)
- **pypdf** — local PDF ingestion
- **httpx** — Notion REST API client (fallback chain)

All four agents run via OpenRouter on the model slug set by `MODEL_ID`
(default: `anthropic/claude-sonnet-4.5`).

## Environment variables
- `OPENROUTER_API_KEY` — required
- `MODEL_ID`           — optional, default `anthropic/claude-sonnet-4.5`
- `NOTION_API_KEY`     — required only if you use `--notion-page` or `--notion-output`

Copy `.env.template` to `.env` and fill in. `.env` is gitignored.

## CLI usage examples
```bash
# Direct text
python debate.py --text "We propose migrating all internal apps to Kubernetes by Q3..."

# Local file (Markdown / TXT / PDF)
python debate.py --file ./proposals/k8s-migration.md

# Notion page (requires NOTION_API_KEY)
python debate.py --notion-page 1234567890abcdef --proposal-type IT_PROJECT

# Custom persona overrides
python debate.py --file proposal.md \
  --persona-1 "Optimistic VP of Product who values speed-to-market" \
  --persona-3 "Paranoid CRO who lived through a major data breach"

# Bulk persona config
python debate.py --file proposal.md --persona-config personas.json
```

## Commit conventions
- `feat:`     new capability
- `fix:`      bug resolution
- `docs:`     documentation only
- `refactor:` structural change, no behaviour change

## Branch strategy
- All development happens on `feature/*` branches.
- Never commit directly to `main` / `master`.
- Never commit `.env` or any file containing API keys.

## File layout
```
.
├── debate.py              # Orchestrator + 4 agents + scoring + MD writer
├── generate_report.js     # Node DOCX generator
├── personas.json          # Optional bulk persona config (example)
├── requirements.txt       # Python deps
├── package.json           # Node deps (docx ^8)
├── README.md              # User docs
├── CLAUDE.md              # (this file)
├── .gitignore
├── .env.template
└── output/                # Created at runtime; gitignored
    ├── debate_data.json
    ├── debate_report.md
    └── debate_report.docx
```
