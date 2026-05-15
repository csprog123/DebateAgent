# Multi-Agent Adversarial Debate Tool

Stress-test a draft proposal through a **structured 3-round adversarial debate**
between four specialised Claude agents. The system is an analytical pressure
chamber — agents interact with each other's specific arguments (cited by label
ID), not just react to the source in isolation.

The output is:
- `output/debate_report.md` — full Markdown transcript + scoring (always generated)
- `output/debate_report.docx` — styled Word document (when Node.js is available)
- `output/debate_data.json` — structured handoff payload

## Architecture

```
            ┌────────────────────────────────────────────┐
            │            Orchestrator (debate.py)         │
            │  ─ validates schemas, enforces rounds       │
            │  ─ detects groupthink, early-termination    │
            │  ─ computes Resilience Score                │
            └─────┬────────────┬────────────┬─────────────┘
                  │            │            │
       ┌──────────▼─┐ ┌────────▼─────┐ ┌────▼────────┐ ┌─────────────┐
       │ Advocate   │ │ Devil's Adv. │ │ Risk & Legal│ │ Adaptive    │
       │ (Agent 1)  │ │ (Agent 2)    │ │ (Agent 3)   │ │ Stakeholder │
       └────────────┘ └──────────────┘ └─────────────┘ └─────────────┘
                              ↓
                  output/debate_data.json
                              ↓
              node generate_report.js  → debate_report.docx
```

All agents are called via **OpenRouter** (OpenAI-compatible endpoint) using
the model slug from the `MODEL_ID` env var. Default: **`anthropic/claude-sonnet-4.5`**.
Any model OpenRouter supports works (e.g. `openai/gpt-4o`, `google/gemini-pro-1.5`).

## Prerequisites
- Python 3.11+
- Node.js 18+ and npm (optional, required only for `.docx` output)

## Installation
```bash
pip install -r requirements.txt
npm install                       # only needed for DOCX output
cp .env.template .env             # then fill in keys
```

## Environment variables (`.env`)
```
OPENROUTER_API_KEY=sk-or-...      # required — get one at https://openrouter.ai/keys
MODEL_ID=anthropic/claude-sonnet-4.5   # optional — override model
NOTION_API_KEY=secret_...         # optional, only for Notion input/output
```

## CLI reference

```
python debate.py \
  [--notion-page <PAGE_ID>]         # Optional: Notion input
  [--file <PATH>]                   # Optional: local .txt/.md/.pdf
  [--text "proposal text"]          # Optional: direct text
  [--rounds 3]                      # Default 3 (fixed)
  [--proposal-type <TYPE>]          # Override classifier
  [--persona-1 "description"]       # Custom Agent 1 persona
  [--persona-2 "description"]       # Custom Agent 2 persona
  [--persona-3 "description"]       # Custom Agent 3 persona
  [--persona-4 "description"]       # Custom Agent 4 persona
  [--persona-config personas.json]  # Bulk persona override
  [--notion-output <PAGE_ID>]       # (Reserved) write report to Notion
```

### Proposal type enum
`IT_PROJECT`, `FACILITY_CHANGE`, `POLICY_REFORM`, `BUDGET_REQUEST`,
`R&D_INITIATIVE`, `OTHER`. The classifier picks one automatically unless
you override with `--proposal-type`. The type determines Agent 4's default
Adaptive Persona (CISO, CFO, COO, etc.).

### Input priority chain
1. `--notion-page` (requires `NOTION_API_KEY`; falls through if absent or fails)
2. `--file` (`.txt` / `.md` / `.pdf`)
3. `--text` (raw string)

### Examples

```bash
# Smallest sanity run
python debate.py --text "Proposal: adopt a 4-day workweek company-wide..."

# Local proposal with type override and one custom persona
python debate.py --file proposals/k8s.md \
  --proposal-type IT_PROJECT \
  --persona-3 "Paranoid CRO who lived through a major breach"

# Bulk persona config + Notion source
python debate.py --notion-page abcd1234 --persona-config personas.json
```

## Personas — schema (`personas.json`)
```json
{
  "agent_1": {
    "name": "Programme Sponsor",
    "role": "Senior policy officer aligned with the proposal author",
    "disposition": "Constructive but evidence-bound",
    "known_priorities": ["strategic alignment", "delivery momentum"],
    "communication_style": "Crisp, citation-heavy, executive register"
  },
  "agent_2": { "...": "..." },
  "agent_3": { "...": "..." },
  "agent_4": { "...": "..." }
}
```
All fields are optional. Omitted agents fall back to defaults. Missing
fields warn but do not fail. **CLI flags (`--persona-N`) override the
config file.**

The persona is layered on top of an immutable Functional Mandate — agents
cannot be talked out of their structural debate obligations.

## Debate protocol

| Round | Title              | What each agent produces                                  |
|-------|--------------------|-----------------------------------------------------------|
| 1     | Diagnostic         | 3 labelled items (ADV-SS / DA-SW / RL-SW / AS-SW)         |
| 2     | Direct Rebuttal    | `CONCEDE [label]` or `REBUT [label]` + position update    |
| 3     | Final Verdict      | `GO` / `NO-GO` + one Critical Condition for Success       |

- **Early termination**: any `CRITICAL` severity flagged by Risk & Legal pauses
  the debate with a red console alert and prompts for confirmation.
- **Anti-drift**: if 3+ agents produce >70% semantically similar weaknesses,
  Agent 2 is regenerated under a stricter orthogonality mandate.
- **Unsubstantiated rebuttals** (very thin justifications) are flagged and
  penalised in scoring.

## Resilience Score (0–100)

| Factor                | Weight | Logic                                  |
|-----------------------|--------|----------------------------------------|
| Unrefuted Weaknesses  | 40%    | –5 per unrefuted weakness              |
| CRITICAL Risks        | 25%    | –20 per CRITICAL flagged risk          |
| Concessions Made      | 20%    | –4 per concession (any agent)          |
| GO/NO-GO Split        | 15%    | 4 GO=+15, 3 GO=+10, 2 GO=+5, <2=+0     |

Bands:
- **80–100 GREEN** — Submit with confidence
- **60–79  AMBER** — Address flagged conditions before submission
- **40–59  RED**   — Significant rework required
- **0–39   CRITICAL** — Unlikely to be approved in current form

## Output files

| File                                | Notes                                         |
|-------------------------------------|-----------------------------------------------|
| `output/debate_report.md`           | Always generated                              |
| `output/debate_report.docx`         | Only if `node` is on `PATH` and `docx` is installed |
| `output/debate_data.json`           | Structured handoff (Python → Node)            |

## Deploying to Vercel

The repo ships with a FastAPI wrapper at `api/index.py` so the tool can run
as a Vercel serverless function.

1. Connect the GitHub repo to a Vercel project (or run `vercel link` locally).
2. In **Project Settings → Environment Variables**, add:
   - `OPENROUTER_API_KEY` (required)
   - `MODEL_ID` (optional, default `anthropic/claude-sonnet-4.5`)
   - `NOTION_API_KEY` (optional, CLI-only)
3. Deploy. The function entrypoint is `api/index.py`, configured via
   `vercel.json` (300s `maxDuration`).

Endpoints:
- `GET /` — minimal HTML form (paste proposal, optional persona overrides)
- `POST /api/debate` — JSON body matching `DebateRequest`; returns the full
  `debate_data` payload including `markdown_report`
- `GET /api/health` — liveness + key-presence probe

Timeout note: a full 3-round debate typically runs 30–90s. Vercel Hobby caps
function execution at 60s regardless of `maxDuration`. For reliable runs use
**Pro** or higher.

DOCX output is **not** generated in the serverless path — Node is unavailable
inside a Python function. The hosted version returns Markdown + structured
JSON; run the CLI locally if you need a `.docx`.

## Limitations
- The Notion adapter uses the canonical Notion REST API with an integration
  token. The MCP transport (`mcp.notion.com/mcp`) is mentioned in the spec
  but is not used here — the REST API is the stable, documented surface for
  programmatic ingestion.
- `--notion-output` is reserved in the CLI surface but not yet implemented.
