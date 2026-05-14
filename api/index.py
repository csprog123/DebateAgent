"""Vercel ASGI entrypoint — wraps the debate orchestrator behind a small HTTP API.

GET  /            → simple HTML form (paste proposal, optional persona overrides)
POST /api/debate  → JSON in, JSON out (full debate_data including markdown_report)
GET  /api/health  → liveness probe

Why this file lives at api/index.py:
    Vercel's Python runtime auto-detects ASGI apps when the module exports
    a top-level `app` symbol. We expose `app = FastAPI()` and the platform
    handles request routing. See vercel.json for function configuration.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Make the repo root importable so we can pull in the orchestrator module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from debate import PROPOSAL_TYPES, run_pipeline

app = FastAPI(title="Adversarial Debate Tool")


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Adversarial Debate Tool</title>
<style>
  body { font: 14px/1.5 -apple-system, system-ui, sans-serif; max-width: 760px;
         margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { color: #1F3864; margin-bottom: 0.25rem; }
  p.sub { color: #555; margin-top: 0; }
  label { display: block; margin-top: 0.75rem; font-weight: 600; }
  textarea, input, select { width: 100%; box-sizing: border-box; padding: 0.5rem;
                            font: inherit; border: 1px solid #ccc; border-radius: 4px; }
  textarea { min-height: 180px; resize: vertical; }
  button { margin-top: 1rem; padding: 0.6rem 1.2rem; background: #1F3864;
           color: #fff; border: 0; border-radius: 4px; font-weight: 600;
           cursor: pointer; }
  button[disabled] { opacity: 0.6; cursor: progress; }
  details { margin-top: 1rem; }
  #status { margin-top: 1rem; color: #555; }
  #out { margin-top: 1.5rem; }
  #score { font-size: 1.5rem; font-weight: 700; padding: 0.75rem 1rem;
           border-radius: 6px; text-align: center; margin-bottom: 1rem; }
  .GREEN    { background: #E2EFDA; color: #375623; }
  .AMBER    { background: #FFF2CC; color: #7D5A00; }
  .RED      { background: #FCE4D6; color: #843C0C; }
  .CRITICAL { background: #F4CCCC; color: #660000; }
  pre { background: #f6f8fa; padding: 1rem; border-radius: 4px; overflow: auto; }
</style>
</head>
<body>
<h1>Adversarial Debate Tool</h1>
<p class="sub">Stress-test a proposal against four adversarial agents.</p>

<form id="f">
  <label>Proposal text
    <textarea name="proposal" required placeholder="Paste your draft proposal here..."></textarea>
  </label>

  <details>
    <summary>Advanced options</summary>
    <label>Title <input name="title" placeholder="Submitted Proposal"></label>
    <label>Proposal type
      <select name="proposal_type">
        <option value="">(auto-classify)</option>
        <option>IT_PROJECT</option>
        <option>FACILITY_CHANGE</option>
        <option>POLICY_REFORM</option>
        <option>BUDGET_REQUEST</option>
        <option>R&amp;D_INITIATIVE</option>
        <option>OTHER</option>
      </select>
    </label>
    <label>Agent 1 persona override <input name="persona_1"></label>
    <label>Agent 2 persona override <input name="persona_2"></label>
    <label>Agent 3 persona override <input name="persona_3"></label>
    <label>Agent 4 persona override <input name="persona_4"></label>
  </details>

  <button id="go" type="submit">Run debate</button>
  <p id="status"></p>
</form>

<div id="out" hidden>
  <div id="score"></div>
  <h3>Markdown report</h3>
  <pre id="md"></pre>
</div>

<script>
const f = document.getElementById('f');
const status = document.getElementById('status');
const out = document.getElementById('out');
const score = document.getElementById('score');
const md = document.getElementById('md');
const go = document.getElementById('go');

f.addEventListener('submit', async (e) => {
  e.preventDefault();
  go.disabled = true;
  status.textContent = 'Running debate (typically 30-90 seconds)...';
  out.hidden = true;
  const fd = new FormData(f);
  const body = {};
  for (const [k, v] of fd.entries()) if (v) body[k] = v;
  try {
    const r = await fetch('/api/debate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(`HTTP ${r.status}: ${t}`);
    }
    const data = await r.json();
    const s = data.resilience_score;
    score.textContent = `Resilience Score: ${s.total}/100 — ${s.band}`;
    score.className = s.band;
    md.textContent = data.markdown_report;
    out.hidden = false;
    status.textContent = 'Done.';
  } catch (err) {
    status.textContent = 'Error: ' + err.message;
  } finally {
    go.disabled = false;
  }
});
</script>
</body>
</html>
"""


class DebateRequest(BaseModel):
    proposal: str = Field(..., min_length=1)
    title: str | None = "Submitted Proposal"
    proposal_type: str | None = None
    persona_1: str | None = None
    persona_2: str | None = None
    persona_3: str | None = None
    persona_4: str | None = None
    # Continue automatically past CRITICAL risks when running headless.
    continue_on_critical: bool = True


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok",
            "anthropic_key_present": "yes" if os.environ.get("ANTHROPIC_API_KEY") else "no"}


@app.post("/api/debate")
async def debate_endpoint(req: DebateRequest) -> JSONResponse:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY is not configured on the server.")
    if req.proposal_type and req.proposal_type not in PROPOSAL_TYPES:
        raise HTTPException(400, f"proposal_type must be one of {PROPOSAL_TYPES}.")

    persona_args = SimpleNamespace(
        persona_1=req.persona_1, persona_2=req.persona_2,
        persona_3=req.persona_3, persona_4=req.persona_4,
        persona_config=None,
    )

    data = await run_pipeline(
        proposal_text=req.proposal,
        title=req.title or "Submitted Proposal",
        source_label="api",
        proposal_type=req.proposal_type,
        persona_args=persona_args,
        continue_on_critical=req.continue_on_critical,
        write_files=False,
    )
    return JSONResponse(data)
