"""
Multi-Agent Adversarial Debate Tool.

Orchestrates a structured 3-round adversarial debate between four specialised
Claude agents to stress-test a draft proposal. See README.md for full usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from anthropic import AsyncAnthropic

MODEL_ID = "claude-sonnet-4-6"
MAX_TOKENS_PER_RESPONSE = 300
OUTPUT_DIR = Path("output")
DEBATE_DATA_PATH = OUTPUT_DIR / "debate_data.json"
MARKDOWN_PATH = OUTPUT_DIR / "debate_report.md"
DOCX_PATH = OUTPUT_DIR / "debate_report.docx"

PROPOSAL_TYPES = [
    "IT_PROJECT", "FACILITY_CHANGE", "POLICY_REFORM",
    "BUDGET_REQUEST", "R&D_INITIATIVE", "OTHER",
]

ADAPTIVE_PERSONA_BY_TYPE = {
    "IT_PROJECT":      ("CISO / Digital Transformation Lead",
                        "security posture, technical debt, integration risk"),
    "FACILITY_CHANGE": ("Sustainability Lead / HR Director",
                        "ESG compliance, workforce disruption, transition costs"),
    "BUDGET_REQUEST":  ("CFO",
                        "OpEx vs CapEx tension, ROI horizon, budget displacement"),
    "R&D_INITIATIVE":  ("Chief Science Officer",
                        "methodology rigour, IP ownership, commercialisation"),
    "POLICY_REFORM":   ("Legal Counsel / Regulatory Affairs",
                        "jurisdictional exposure, enforcement gaps, buy-in"),
    "OTHER":           ("COO",
                        "operational feasibility, resources, timeline realism"),
}

AGENT_PREFIXES = {
    "agent_1": "ADV",
    "agent_2": "DA",
    "agent_3": "RL",
    "agent_4": "AS",
}

AGENT_DISPLAY_NAMES = {
    "agent_1": "Advocate",
    "agent_2": "Devil's Advocate",
    "agent_3": "Risk & Legal",
    "agent_4": "Adaptive Stakeholder",
}

DEFAULT_PERSONAS = {
    "agent_1": {
        "name": "Senior Policy Officer / Programme Sponsor",
        "description": "Senior policy officer or programme sponsor aligned with the proposal author.",
    },
    "agent_2": {
        "name": "Academic Peer Reviewer",
        "description": "Rigorous academic peer reviewer mandated to find failure modes.",
    },
    "agent_3": {
        "name": "General Counsel / CRO",
        "description": "Cautious general counsel or Chief Risk Officer.",
    },
    "agent_4": {
        "name": "Adaptive Stakeholder (assigned by classifier)",
        "description": "Adaptive operational stakeholder assigned by the Proposal Classifier.",
    },
}


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def load_local_file(path: str) -> tuple[str, str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    ext = p.suffix.lower()
    if ext in {".txt", ".md"}:
        return p.read_text(encoding="utf-8", errors="replace"), p.name
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("pypdf required for PDF input. pip install pypdf")
        reader = PdfReader(str(p))
        return "\n".join((page.extract_text() or "") for page in reader.pages), p.name
    raise ValueError(f"Unsupported file extension: {ext}. Use .txt, .md, or .pdf.")


async def load_notion_page(page_id: str) -> tuple[str, str]:
    """Pull a Notion page (and inline child blocks) via the Notion REST API.

    The spec mentions the Notion MCP endpoint; we use the documented REST API
    here because it is the canonical programmatic surface for a Notion
    integration token (NOTION_API_KEY). Returns (text, title).
    """
    import httpx

    token = os.environ.get("NOTION_API_KEY")
    if not token:
        raise RuntimeError("NOTION_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        page = (await client.get(
            f"https://api.notion.com/v1/pages/{page_id}", headers=headers
        )).raise_for_status().json()

        title = "Notion Page"
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title" and prop.get("title"):
                title = "".join(t.get("plain_text", "") for t in prop["title"]) or title
                break

        # Recursively walk blocks (one level of nesting is usually enough).
        async def walk(block_id: str, depth: int = 0) -> list[str]:
            if depth > 3:
                return []
            cursor: str | None = None
            chunks: list[str] = []
            while True:
                params = {"page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                r = (await client.get(
                    f"https://api.notion.com/v1/blocks/{block_id}/children",
                    headers=headers, params=params,
                )).raise_for_status().json()
                for blk in r.get("results", []):
                    btype = blk.get("type")
                    payload = blk.get(btype, {}) if btype else {}
                    rich = payload.get("rich_text") or payload.get("text") or []
                    text = "".join(t.get("plain_text", "") for t in rich)
                    if text:
                        chunks.append(text)
                    if blk.get("has_children"):
                        chunks.extend(await walk(blk["id"], depth + 1))
                if not r.get("has_more"):
                    break
                cursor = r.get("next_cursor")
            return chunks

        body = "\n".join(await walk(page_id))
        return body, title


def resolve_proposal(args: argparse.Namespace) -> tuple[str, str, str]:
    """Return (proposal_text, source_label, title). Strict priority order."""
    # Priority 1 — Notion
    if args.notion_page:
        if not os.environ.get("NOTION_API_KEY"):
            print("[WARN] --notion-page given but NOTION_API_KEY missing. Falling through.")
        else:
            try:
                text, title = asyncio.run(load_notion_page(args.notion_page))
                print(f"[INFO] Notion source loaded: {title}")
                return text, "notion", title
            except Exception as exc:
                print(f"[WARN] Notion load failed ({exc}). Falling through.")

    # Priority 2 — Local file
    if args.file:
        text, fname = load_local_file(args.file)
        print(f"[INFO] Local file loaded: {fname}")
        return text, "file", fname

    # Priority 3 — Direct text
    if args.text:
        print(f"[INFO] Direct text input received. Character count: {len(args.text)}")
        return args.text, "text", "Direct Text Proposal"

    print("[ERROR] No proposal source provided. Use --notion-page, --file, or --text.")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Proposal classifier
# ---------------------------------------------------------------------------

async def classify_proposal(client: AsyncAnthropic, proposal: str) -> str:
    prompt = (
        "Classify the following proposal into exactly ONE of these categories:\n"
        f"{', '.join(PROPOSAL_TYPES)}.\n"
        "Reply with ONLY the category token, nothing else.\n\n"
        f"PROPOSAL:\n{proposal[:6000]}"
    )
    resp = await client.messages.create(
        model=MODEL_ID, max_tokens=20,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip().upper()
    for t in PROPOSAL_TYPES:
        if t in raw:
            return t
    return "OTHER"


# ---------------------------------------------------------------------------
# Persona configuration
# ---------------------------------------------------------------------------

@dataclass
class PersonaConfig:
    agent_id: str
    name: str
    description: str
    is_custom: bool
    raw: dict[str, Any] = field(default_factory=dict)


def build_persona_config(args: argparse.Namespace, proposal_type: str) -> dict[str, PersonaConfig]:
    """Resolve per-agent personas. Precedence: CLI flag > config file > default."""
    file_cfg: dict[str, Any] = {}
    if args.persona_config:
        try:
            file_cfg = json.loads(Path(args.persona_config).read_text())
        except Exception as exc:
            print(f"[WARN] Could not load --persona-config: {exc}. Using defaults.")
            file_cfg = {}

    expected_fields = {"name", "role", "disposition", "known_priorities", "communication_style"}
    cli_overrides = {
        "agent_1": args.persona_1, "agent_2": args.persona_2,
        "agent_3": args.persona_3, "agent_4": args.persona_4,
    }

    result: dict[str, PersonaConfig] = {}
    for agent_id in ("agent_1", "agent_2", "agent_3", "agent_4"):
        cli = cli_overrides[agent_id]
        if cli:
            result[agent_id] = PersonaConfig(
                agent_id=agent_id, name=f"Custom ({agent_id})",
                description=cli, is_custom=True, raw={"description": cli},
            )
            continue

        if agent_id in file_cfg and isinstance(file_cfg[agent_id], dict):
            entry = file_cfg[agent_id]
            missing = expected_fields - entry.keys()
            if missing:
                print(f"[WARN] persona_config.{agent_id} missing fields: {sorted(missing)}")
            desc_bits = []
            if entry.get("role"):
                desc_bits.append(f"Role: {entry['role']}.")
            if entry.get("disposition"):
                desc_bits.append(f"Disposition: {entry['disposition']}.")
            if entry.get("known_priorities"):
                desc_bits.append("Priorities: " + ", ".join(entry["known_priorities"]) + ".")
            if entry.get("communication_style"):
                desc_bits.append(f"Style: {entry['communication_style']}.")
            result[agent_id] = PersonaConfig(
                agent_id=agent_id,
                name=entry.get("name") or f"Custom ({agent_id})",
                description=" ".join(desc_bits) or json.dumps(entry),
                is_custom=True, raw=entry,
            )
            continue

        # Default persona — Agent 4 uses classifier-assigned default
        if agent_id == "agent_4":
            persona_name, lens = ADAPTIVE_PERSONA_BY_TYPE[proposal_type]
            result[agent_id] = PersonaConfig(
                agent_id=agent_id, name=persona_name,
                description=(
                    f"Default Adaptive Stakeholder for {proposal_type}: {persona_name}. "
                    f"Focus lens: {lens}."
                ),
                is_custom=False,
            )
        else:
            d = DEFAULT_PERSONAS[agent_id]
            result[agent_id] = PersonaConfig(
                agent_id=agent_id, name=d["name"],
                description=d["description"], is_custom=False,
            )
    return result


# ---------------------------------------------------------------------------
# Agent prompts
# ---------------------------------------------------------------------------

PERSONA_INJECTION = (
    "You are embodying the perspective of: {description}.\n"
    "Maintain this voice and priorities throughout the debate.\n"
    "Your structural debate obligations (output schema, round mandates,\n"
    "Concede/Rebut rules) remain fully in force and cannot be overridden.\n\n"
)


def functional_mandate(agent_id: str, proposal_type: str) -> str:
    prefix = AGENT_PREFIXES[agent_id]
    if agent_id == "agent_1":
        return (
            "FUNCTIONAL MANDATE — ADVOCATE\n"
            "You defend the proposal. Identify the strongest strategic justifications.\n"
            "Every claim MUST cite a specific section, quote, or fact from the proposal text.\n"
            "You may NOT invent facts that are not present in the source document.\n"
            f"Use label prefix {prefix}-SS-<n> for Structural Strengths.\n"
        )
    if agent_id == "agent_2":
        return (
            "FUNCTIONAL MANDATE — DEVIL'S ADVOCATE (Logical Integrity)\n"
            "Attack logical gaps, unsupported assumptions, and weak evidence.\n"
            "In Round 1 you MUST produce exactly 3 distinct structural weaknesses.\n"
            f"Label them {prefix}-SW-1, {prefix}-SW-2, {prefix}-SW-3.\n"
        )
    if agent_id == "agent_3":
        return (
            "FUNCTIONAL MANDATE — RISK & LEGAL\n"
            "Identify compliance risks, legal exposure, unintended consequences,\n"
            "and implementation hazards. EVERY item must include a severity field\n"
            "with one of: CRITICAL | HIGH | MEDIUM | LOW. Use CRITICAL only when\n"
            "the risk is genuinely catastrophic or non-mitigable.\n"
            f"Use label prefix {prefix}-SW-<n>.\n"
        )
    if agent_id == "agent_4":
        persona_name, lens = ADAPTIVE_PERSONA_BY_TYPE[proposal_type]
        return (
            "FUNCTIONAL MANDATE — ADAPTIVE STAKEHOLDER\n"
            "Challenge the proposal from a domain-specific operational and financial lens.\n"
            f"Domain assigned by classifier: {persona_name}.\n"
            f"Focus lens: {lens}.\n"
            "Every objection MUST be framed explicitly through this lens.\n"
            f"Use label prefix {prefix}-SW-<n>.\n"
        )
    return ""


def build_system_prompt(agent_id: str, persona: PersonaConfig, proposal_type: str, proposal: str) -> str:
    parts = []
    if persona.is_custom:
        parts.append(PERSONA_INJECTION.format(description=persona.description))
    else:
        parts.append(f"Default persona: {persona.name}. {persona.description}\n\n")
    parts.append(functional_mandate(agent_id, proposal_type))
    parts.append(
        "\nGLOBAL RULES:\n"
        "- Respond ONLY with a single valid JSON object matching the round schema.\n"
        "- No prose outside the JSON. No markdown fences.\n"
        f"- Hard ceiling: {MAX_TOKENS_PER_RESPONSE} tokens.\n"
        "- Be concrete and grounded in the proposal text.\n\n"
        "PROPOSAL TEXT (authoritative source):\n"
        "------------------------------------\n"
        f"{proposal[:12000]}\n"
        "------------------------------------\n"
    )
    return "".join(parts)


# Round-specific user prompts -------------------------------------------------

def round1_user_prompt(agent_id: str) -> str:
    prefix = AGENT_PREFIXES[agent_id]
    if agent_id == "agent_1":
        return (
            "ROUND 1 — DIAGNOSTIC.\n"
            "Identify EXACTLY 3 Structural Strengths of the proposal.\n"
            f"Label them {prefix}-SS-1, {prefix}-SS-2, {prefix}-SS-3.\n"
            "Respond with JSON only, schema:\n"
            '{ "round": 1, "agent": "Advocate", "persona_applied": "<string>",\n'
            '  "items": [{"id": "ADV-SS-1", "description": "..."}, ...],\n'
            '  "confidence": <0-100> }\n'
            "You may NOT reference any other agent."
        )
    if agent_id == "agent_3":
        return (
            "ROUND 1 — DIAGNOSTIC.\n"
            "Identify EXACTLY 3 Structural Weaknesses (risk-framed).\n"
            f"Label them {prefix}-SW-1, {prefix}-SW-2, {prefix}-SW-3.\n"
            "Each item MUST include a 'severity' field: CRITICAL | HIGH | MEDIUM | LOW.\n"
            "Respond with JSON only:\n"
            '{ "round": 1, "agent": "Risk & Legal", "persona_applied": "<string>",\n'
            '  "items": [{"id": "RL-SW-1", "description": "...", "severity": "HIGH"}, ...],\n'
            '  "confidence": <0-100> }\n'
            "You may NOT reference any other agent."
        )
    name = AGENT_DISPLAY_NAMES[agent_id]
    return (
        "ROUND 1 — DIAGNOSTIC.\n"
        "Identify EXACTLY 3 distinct Structural Weaknesses.\n"
        f"Label them {prefix}-SW-1, {prefix}-SW-2, {prefix}-SW-3.\n"
        "Respond with JSON only:\n"
        f'{{ "round": 1, "agent": "{name}", "persona_applied": "<string>",\n'
        f'  "items": [{{"id": "{prefix}-SW-1", "description": "..."}}, ...],\n'
        '  "confidence": <0-100> }\n'
        "You may NOT reference any other agent."
    )


def round2_user_prompt(agent_id: str, peer_labels: list[str], peer_summary: str) -> str:
    name = AGENT_DISPLAY_NAMES[agent_id]
    return (
        "ROUND 2 — DIRECT REBUTTAL (CROSS-EXAMINATION).\n"
        "Select EXACTLY ONE weakness raised by a PEER agent in Round 1.\n"
        f"Available peer labels you may target: {', '.join(peer_labels) or '(none)'}.\n"
        "Peer Round-1 summary follows:\n"
        f"{peer_summary}\n\n"
        "You MUST declare exactly one of:\n"
        '  "CONCEDE [label]" — acknowledge validity; provide logical proof it cannot be mitigated.\n'
        '  "REBUT   [label]" — challenge with concrete evidence or reasoning.\n'
        "Also provide a position_update amplifying or revising your Round 1 stance.\n\n"
        "Respond with JSON only:\n"
        f'{{ "round": 2, "agent": "{name}",\n'
        '  "cross_examination": {"target_label": "...", "action": "CONCEDE|REBUT", "justification": "..."},\n'
        '  "position_update": "..." }'
    )


def round3_user_prompt(agent_id: str) -> str:
    name = AGENT_DISPLAY_NAMES[agent_id]
    return (
        "ROUND 3 — FINAL VERDICT.\n"
        "Issue a binary GO or NO-GO recommendation, and state ONE Critical Condition for Success —\n"
        "the single change that would most improve approval likelihood from your perspective.\n\n"
        "Respond with JSON only:\n"
        f'{{ "round": 3, "agent": "{name}", "verdict": "GO|NO-GO", "critical_condition": "..." }}'
    )


# ---------------------------------------------------------------------------
# Agent runtime
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    agent_id: str
    persona: PersonaConfig
    system_prompt: str
    history: list[dict[str, Any]] = field(default_factory=list)


def extract_json(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    try:
        return json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
    return None


async def call_agent(
    client: AsyncAnthropic, agent: Agent, user_msg: str,
    validator, max_retries: int = 1,
) -> dict[str, Any]:
    """Call agent, parse JSON, validate. One retry on failure."""
    messages = agent.history + [{"role": "user", "content": user_msg}]
    last_err = None
    for attempt in range(max_retries + 1):
        resp = await client.messages.create(
            model=MODEL_ID,
            max_tokens=MAX_TOKENS_PER_RESPONSE,
            system=agent.system_prompt,
            messages=messages,
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
        parsed = extract_json(raw)
        ok, err = (False, "no json") if parsed is None else validator(parsed)
        if ok:
            agent.history.append({"role": "user", "content": user_msg})
            agent.history.append({"role": "assistant", "content": raw})
            return parsed
        last_err = err
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": f"Your output was rejected: {err}. Regenerate as valid JSON, no prose."},
        ]
    # Final fallback: return a stub so the pipeline doesn't crash.
    print(f"[WARN] {agent.agent_id} failed validation after retry: {last_err}")
    return {"_invalid": True, "_error": last_err}


# Validators ------------------------------------------------------------------

def make_r1_validator(agent_id: str):
    prefix = AGENT_PREFIXES[agent_id]
    def v(obj):
        if obj.get("round") != 1: return False, "round!=1"
        items = obj.get("items")
        if not isinstance(items, list) or len(items) != 3:
            return False, "need exactly 3 items"
        for it in items:
            if not isinstance(it, dict) or "id" not in it or "description" not in it:
                return False, "item missing id/description"
            if not it["id"].startswith(prefix + "-"):
                return False, f"label must start with {prefix}-"
        if agent_id == "agent_3":
            for it in items:
                sev = (it.get("severity") or "").upper()
                if sev not in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}:
                    return False, "Risk & Legal item missing severity"
        return True, ""
    return v


def make_r2_validator(allowed_labels: set[str]):
    def v(obj):
        if obj.get("round") != 2: return False, "round!=2"
        cx = obj.get("cross_examination")
        if not isinstance(cx, dict): return False, "cross_examination missing"
        if cx.get("action") not in {"CONCEDE", "REBUT"}: return False, "action must be CONCEDE or REBUT"
        if cx.get("target_label") not in allowed_labels:
            return False, f"target_label must be one of peer labels {sorted(allowed_labels)}"
        if not cx.get("justification"): return False, "justification missing"
        if not obj.get("position_update"): return False, "position_update missing"
        return True, ""
    return v


def r3_validator(obj):
    if obj.get("round") != 3: return False, "round!=3"
    if obj.get("verdict") not in {"GO", "NO-GO"}: return False, "verdict must be GO or NO-GO"
    if not obj.get("critical_condition"): return False, "critical_condition missing"
    return True, ""


# ---------------------------------------------------------------------------
# Anti-drift detection
# ---------------------------------------------------------------------------

def detect_groupthink(round1: list[dict[str, Any]]) -> bool:
    """Return True if 3+ agents produced >70% semantically similar weaknesses."""
    weakness_texts: list[tuple[str, str]] = []
    for r in round1:
        if r.get("agent") == "Advocate":
            continue
        for it in r.get("items", []):
            weakness_texts.append((r["agent"], it.get("description", "")))

    similar_agents: set[str] = set()
    for i in range(len(weakness_texts)):
        for j in range(i + 1, len(weakness_texts)):
            a, ta = weakness_texts[i]
            b, tb = weakness_texts[j]
            if a == b:
                continue
            if SequenceMatcher(None, ta.lower(), tb.lower()).ratio() > 0.70:
                similar_agents.add(a)
                similar_agents.add(b)
    return len(similar_agents) >= 3


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_debate(
    client: AsyncAnthropic, agents: dict[str, Agent], proposal_type: str,
    continue_on_critical: bool | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Run the 3-round debate.

    continue_on_critical:
      None  → ask interactively (only if stdin is a TTY); otherwise stop.
      True  → continue past CRITICAL risks without prompting.
      False → stop at the first CRITICAL risk and return what we have.
    """

    transcript: dict[str, list[dict[str, Any]]] = {"round_1": [], "round_2": [], "round_3": []}

    # ---- ROUND 1 ----
    print("[INFO] Round 1 — Diagnostic (parallel)...")
    r1_tasks = [
        call_agent(client, agents[aid], round1_user_prompt(aid), make_r1_validator(aid))
        for aid in ("agent_1", "agent_2", "agent_3", "agent_4")
    ]
    r1 = await asyncio.gather(*r1_tasks)
    for aid, obj in zip(("agent_1", "agent_2", "agent_3", "agent_4"), r1):
        obj.setdefault("agent", AGENT_DISPLAY_NAMES[aid])
        obj.setdefault("persona_applied",
                       f"{'custom' if agents[aid].persona.is_custom else 'default'}: {agents[aid].persona.name}")
        transcript["round_1"].append(obj)

    # Early termination check — CRITICAL risk from Agent 3
    critical_items = [
        it for it in (transcript["round_1"][2].get("items") or [])
        if (it.get("severity") or "").upper() == "CRITICAL"
    ]
    if critical_items:
        print("\n\033[91m" + "=" * 72)
        print("RED ALERT — CRITICAL RISK FLAGGED BY RISK & LEGAL AGENT")
        for it in critical_items:
            print(f"  [{it.get('id')}] {it.get('description')}")
        print("=" * 72 + "\033[0m")
        proceed = False
        if continue_on_critical is True:
            proceed = True
        elif continue_on_critical is False:
            proceed = False
        elif sys.stdin.isatty():
            proceed = input("CRITICAL risk identified. Continue debate? (Y/N): ").strip().lower() == "y"
        if not proceed:
            print("[INFO] Skipping to final synthesis with CRITICAL flag.")
            return transcript

    # Groupthink detection
    if detect_groupthink(transcript["round_1"]):
        print("[WARN] GROUPTHINK detected. Regenerating Agent 2 with stricter mandate.")
        agents["agent_2"].history.clear()
        stricter = (
            "STRICTER ADVERSARIAL MANDATE: your weaknesses must be ORTHOGONAL to those raised by\n"
            "other agents. Avoid restating shared themes. Attack from angles no peer has used.\n\n"
        )
        agents["agent_2"].system_prompt = stricter + agents["agent_2"].system_prompt
        new_r1 = await call_agent(
            client, agents["agent_2"], round1_user_prompt("agent_2"), make_r1_validator("agent_2"),
        )
        new_r1.setdefault("agent", "Devil's Advocate")
        new_r1.setdefault(
            "persona_applied",
            f"{'custom' if agents['agent_2'].persona.is_custom else 'default'}: {agents['agent_2'].persona.name}",
        )
        new_r1["_regenerated_for_groupthink"] = True
        transcript["round_1"][1] = new_r1

    # ---- ROUND 2 ----
    print("[INFO] Round 2 — Direct Rebuttal (parallel)...")
    # Allowed labels per agent = labels raised by PEERS
    all_labels_by_agent: dict[str, list[str]] = {}
    for aid, obj in zip(("agent_1", "agent_2", "agent_3", "agent_4"), transcript["round_1"]):
        all_labels_by_agent[aid] = [it.get("id") for it in obj.get("items", []) if it.get("id")]

    def peer_pkg(self_aid: str) -> tuple[set[str], str]:
        labels: set[str] = set()
        lines: list[str] = []
        for aid in ("agent_1", "agent_2", "agent_3", "agent_4"):
            if aid == self_aid:
                continue
            labels.update(all_labels_by_agent[aid])
            for it in transcript["round_1"][("agent_1","agent_2","agent_3","agent_4").index(aid)].get("items", []):
                lines.append(f"  [{it.get('id')}] {it.get('description','')}")
        return labels, "\n".join(lines)

    r2_tasks = []
    for aid in ("agent_1", "agent_2", "agent_3", "agent_4"):
        labels, summary = peer_pkg(aid)
        r2_tasks.append(call_agent(
            client, agents[aid], round2_user_prompt(aid, sorted(labels), summary),
            make_r2_validator(labels),
        ))
    r2 = await asyncio.gather(*r2_tasks)
    for aid, obj in zip(("agent_1", "agent_2", "agent_3", "agent_4"), r2):
        obj.setdefault("agent", AGENT_DISPLAY_NAMES[aid])
        transcript["round_2"].append(obj)

    # Check for CRITICAL surfacing again in R2 (Risk & Legal might escalate via position_update)
    # We rely only on Round-1 R3 severities; no schema for severity in R2 here.

    # ---- ROUND 3 ----
    print("[INFO] Round 3 — Final Verdict (parallel)...")
    r3_tasks = [
        call_agent(client, agents[aid], round3_user_prompt(aid), r3_validator)
        for aid in ("agent_1", "agent_2", "agent_3", "agent_4")
    ]
    r3 = await asyncio.gather(*r3_tasks)
    for aid, obj in zip(("agent_1", "agent_2", "agent_3", "agent_4"), r3):
        obj.setdefault("agent", AGENT_DISPLAY_NAMES[aid])
        transcript["round_3"].append(obj)

    return transcript


# ---------------------------------------------------------------------------
# Resilience Score + synthesis
# ---------------------------------------------------------------------------

def synthesise(
    transcript: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute resilience score + synthesis blocks."""
    # Gather all weaknesses raised (exclude Advocate strengths)
    weaknesses: dict[str, dict[str, Any]] = {}
    for r in transcript["round_1"]:
        if r.get("agent") == "Advocate":
            continue
        for it in r.get("items", []):
            label = it.get("id")
            if label:
                weaknesses[label] = {
                    "label": label,
                    "description": it.get("description", ""),
                    "severity": (it.get("severity") or "").upper(),
                    "raised_by": r.get("agent"),
                }

    # Concessions and rebuts
    concessions: list[dict[str, Any]] = []
    rebut_targets: set[str] = set()
    unsubstantiated_rebuts: list[dict[str, Any]] = []
    for r in transcript.get("round_2", []):
        cx = r.get("cross_examination") or {}
        if cx.get("action") == "CONCEDE":
            concessions.append({
                "agent": r.get("agent"),
                "label": cx.get("target_label"),
                "justification": cx.get("justification", ""),
            })
        elif cx.get("action") == "REBUT":
            rebut_targets.add(cx.get("target_label"))
            j = cx.get("justification") or ""
            # Heuristic: very short justification or no specifics → unsubstantiated
            if len(j.split()) < 12:
                unsubstantiated_rebuts.append({
                    "agent": r.get("agent"),
                    "label": cx.get("target_label"),
                    "justification": j,
                })

    # Citation frequency per label (cited_by = how many R2 cross-examinations targeted it)
    cited_by: dict[str, int] = {lbl: 0 for lbl in weaknesses}
    for r in transcript.get("round_2", []):
        tgt = (r.get("cross_examination") or {}).get("target_label")
        if tgt in cited_by:
            cited_by[tgt] += 1

    conceded_labels = {c["label"] for c in concessions}
    unrefuted = [
        {"label": lbl, "description": w["description"], "cited_by": cited_by.get(lbl, 0)}
        for lbl, w in weaknesses.items()
        if lbl not in conceded_labels and lbl not in rebut_targets
    ]
    unrefuted.sort(key=lambda x: x["cited_by"], reverse=True)

    critical_count = sum(1 for w in weaknesses.values() if w["severity"] == "CRITICAL")
    concession_count = len(concessions)
    unrefuted_count = len(unrefuted)
    go_count = sum(1 for r in transcript.get("round_3", []) if r.get("verdict") == "GO")

    # Deductions
    ded_unrefuted = unrefuted_count * 5
    ded_critical = critical_count * 20
    ded_concessions = concession_count * 4
    if go_count >= 4: go_bonus = 15
    elif go_count == 3: go_bonus = 10
    elif go_count == 2: go_bonus = 5
    else: go_bonus = 0

    # Penalty: unsubstantiated rebuts treat the "rebutted" labels as half-unrefuted.
    half_penalty = len(unsubstantiated_rebuts) * 2  # 2 pts each (half of -5/weakness scale)

    total = 100 - ded_unrefuted - ded_critical - ded_concessions - half_penalty + go_bonus
    # Cap go bonus inside [0, 100]
    total = max(0, min(100, total))

    if total >= 80: band = "GREEN"
    elif total >= 60: band = "AMBER"
    elif total >= 40: band = "RED"
    else: band = "CRITICAL"

    critical_conditions = [
        {"agent": r.get("agent"), "condition": r.get("critical_condition", "")}
        for r in transcript.get("round_3", [])
    ]

    # Recommended amendments — synthesised from unrefuted + concessions + critical conditions
    recs: list[str] = []
    for w in unrefuted[:3]:
        recs.append(
            f"Address unrefuted weakness [{w['label']}]: {w['description']} "
            f"— provide evidence or mitigation before resubmission."
        )
    for c in concessions[:2]:
        recs.append(
            f"Resolve conceded issue [{c['label']}] raised by {c['agent']}: {c['justification']}"
        )
    for cc in critical_conditions:
        if cc.get("condition"):
            recs.append(f"({cc['agent']}) Critical condition: {cc['condition']}")
    # Guarantee minimum 3
    while len(recs) < 3:
        recs.append("Strengthen evidentiary base and re-circulate to a second adversarial review.")

    score = {
        "total": total,
        "band": band,
        "breakdown": {
            "unrefuted_weaknesses": {"count": unrefuted_count, "deduction": ded_unrefuted},
            "critical_risks":       {"count": critical_count, "deduction": ded_critical},
            "concessions":          {"count": concession_count, "deduction": ded_concessions},
            "go_nogo_split":        {"go_count": go_count, "score": go_bonus},
            "unsubstantiated_rebut_penalty": {"count": len(unsubstantiated_rebuts), "deduction": half_penalty},
        },
    }
    synthesis = {
        "unrefuted_weaknesses": unrefuted,
        "concessions_log": concessions,
        "recommended_amendments": recs[:max(3, len(recs))],
        "critical_conditions": critical_conditions,
    }
    return score, synthesis


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def render_markdown(data: dict[str, Any]) -> str:
    md: list[str] = []
    m = data["metadata"]
    s = data["resilience_score"]
    p = data["personas"]

    md.append(f"# Adversarial Debate Report — {m['proposal_title']}\n")
    md.append("## 1. Proposal Summary\n")
    md.append(data.get("proposal_summary", "_(no summary)_") + "\n")
    md.append("## 2. Input Source\n")
    md.append(f"- Source: **{m['input_source']}**\n")
    md.append("## 3. Proposal Type & Adaptive Persona\n")
    md.append(f"- Type: **{m['proposal_type']}**\n- Adaptive Persona: **{m['adaptive_persona']}**\n")
    md.append("## 4. Persona Configuration\n")
    for aid in ("agent_1", "agent_2", "agent_3", "agent_4"):
        md.append(f"- **{AGENT_DISPLAY_NAMES[aid]}** — {p[aid]['name']} ({p[aid]['type']})\n")
    md.append(f"\n## 5. Resilience Score\n**{s['total']}/100 — {s['band']}**\n")
    md.append("\n## 6. Resilience Score Breakdown\n")
    md.append("| Factor | Count | Deduction / Score |\n|---|---|---|\n")
    b = s["breakdown"]
    md.append(f"| Unrefuted Weaknesses | {b['unrefuted_weaknesses']['count']} | -{b['unrefuted_weaknesses']['deduction']} |\n")
    md.append(f"| CRITICAL Risks | {b['critical_risks']['count']} | -{b['critical_risks']['deduction']} |\n")
    md.append(f"| Concessions | {b['concessions']['count']} | -{b['concessions']['deduction']} |\n")
    md.append(f"| GO/NO-GO Split | {b['go_nogo_split']['go_count']} GO | +{b['go_nogo_split']['score']} |\n")
    md.append(f"| Unsubstantiated Rebut Penalty | {b['unsubstantiated_rebut_penalty']['count']} | -{b['unsubstantiated_rebut_penalty']['deduction']} |\n")

    md.append("\n## 7. Round-by-Round Transcript\n")
    for rnd_key, rnd_title in [("round_1", "Round 1 — Diagnostic"),
                                ("round_2", "Round 2 — Direct Rebuttal"),
                                ("round_3", "Round 3 — Final Verdict")]:
        md.append(f"\n### {rnd_title}\n")
        for entry in data.get(rnd_key, []):
            agent = entry.get("agent", "?")
            persona = entry.get("persona_applied", "")
            md.append(f"\n**{agent}** _{persona}_\n\n")
            md.append("```json\n" + json.dumps(entry, indent=2) + "\n```\n")

    md.append("\n## 8. Unrefuted Weaknesses (ranked by citation frequency)\n")
    if not data["synthesis"]["unrefuted_weaknesses"]:
        md.append("_None._\n")
    else:
        md.append("| Label | Description | Cited By |\n|---|---|---|\n")
        for w in data["synthesis"]["unrefuted_weaknesses"]:
            md.append(f"| {w['label']} | {w['description']} | {w['cited_by']} |\n")

    md.append("\n## 9. Concessions Log\n")
    if not data["synthesis"]["concessions_log"]:
        md.append("_None._\n")
    else:
        md.append("| Agent | Label | Justification |\n|---|---|---|\n")
        for c in data["synthesis"]["concessions_log"]:
            md.append(f"| {c['agent']} | {c['label']} | {c['justification']} |\n")

    md.append("\n## 10. Critical Conditions for Success\n")
    for cc in data["synthesis"]["critical_conditions"]:
        md.append(f"- **{cc['agent']}**: {cc['condition']}\n")

    md.append("\n## 11. Recommended Amendments\n")
    for i, rec in enumerate(data["synthesis"]["recommended_amendments"], 1):
        md.append(f"{i}. {rec}\n")

    md.append("\n## 12. GO/NO-GO Summary Table\n")
    md.append("| Agent | Persona | Verdict | Critical Condition |\n|---|---|---|---|\n")
    persona_lookup = {AGENT_DISPLAY_NAMES[aid]: p[aid]["name"] for aid in p}
    for r in data.get("round_3", []):
        a = r.get("agent", "?")
        md.append(f"| {a} | {persona_lookup.get(a, '')} | {r.get('verdict','')} | {r.get('critical_condition','')} |\n")

    return "".join(md)


def write_markdown(data: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MARKDOWN_PATH.write_text(render_markdown(data), encoding="utf-8")
    print(f"[INFO] Markdown report written: {MARKDOWN_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Multi-agent adversarial debate tool")
    p.add_argument("--notion-page", help="Notion page ID (requires NOTION_API_KEY)")
    p.add_argument("--file", help="Local .txt, .md, or .pdf path")
    p.add_argument("--text", help="Direct proposal text")
    p.add_argument("--rounds", type=int, default=3, help="Default 3 (currently fixed at 3)")
    p.add_argument("--proposal-type", choices=PROPOSAL_TYPES, help="Override classifier")
    p.add_argument("--persona-1", help="Custom Agent 1 persona description")
    p.add_argument("--persona-2", help="Custom Agent 2 persona description")
    p.add_argument("--persona-3", help="Custom Agent 3 persona description")
    p.add_argument("--persona-4", help="Custom Agent 4 persona description")
    p.add_argument("--persona-config", help="JSON file with bulk persona overrides")
    p.add_argument("--notion-output", help="Notion page ID to write report into (optional)")
    return p.parse_args()


async def run_pipeline(
    proposal_text: str,
    title: str,
    source_label: str,
    *,
    proposal_type: str | None = None,
    persona_args: Any = None,
    continue_on_critical: bool | None = None,
    write_files: bool = False,
) -> dict[str, Any]:
    """Run the full debate pipeline and return the structured `debate_data` dict.

    Used by both the CLI (`amain`) and the FastAPI handler (`api/index.py`).

    Parameters
    ----------
    proposal_text : raw proposal text.
    title : display title for the report.
    source_label : "notion" | "file" | "text" | "api".
    proposal_type : optional override; if None, the classifier picks one.
    persona_args : duck-typed namespace exposing the persona_1..4 and
        persona_config fields used by `build_persona_config`. If None,
        all defaults are used.
    continue_on_critical : see `run_debate`. The CLI passes None (interactive
        TTY prompt); the API passes True/False to control non-interactive flow.
    write_files : when True, mirrors the legacy CLI side-effects
        (debate_data.json, debate_report.md).

    Returns
    -------
    dict with keys: metadata, personas, resilience_score, round_1/2/3,
    synthesis, proposal_summary, markdown_report.
    """
    client = AsyncAnthropic()

    if proposal_type is None:
        proposal_type = await classify_proposal(client, proposal_text)
        print(f"[INFO] Proposal classified as: {proposal_type}")
    else:
        print(f"[INFO] Proposal type set explicitly: {proposal_type}")

    if persona_args is None:
        persona_args = SimpleNamespace(
            persona_1=None, persona_2=None, persona_3=None, persona_4=None,
            persona_config=None,
        )
    personas = build_persona_config(persona_args, proposal_type)
    for aid, pc in personas.items():
        print(f"[INFO] {AGENT_DISPLAY_NAMES[aid]} persona: "
              f"{'custom' if pc.is_custom else 'default'} → {pc.name}")

    agents: dict[str, Agent] = {}
    for aid, pc in personas.items():
        agents[aid] = Agent(
            agent_id=aid, persona=pc,
            system_prompt=build_system_prompt(aid, pc, proposal_type, proposal_text),
        )

    transcript = await run_debate(client, agents, proposal_type,
                                  continue_on_critical=continue_on_critical)
    score, synthesis = synthesise(transcript)

    proposal_summary = (
        proposal_text[:600] + ("…" if len(proposal_text) > 600 else "")
    ).replace("\n", " ").strip()

    debate_data: dict[str, Any] = {
        "metadata": {
            "proposal_title": title,
            "input_source": source_label,
            "proposal_type": proposal_type,
            "adaptive_persona": personas["agent_4"].name,
            "debate_date": date.today().isoformat(),
            "rounds_completed": sum(1 for k in ("round_1", "round_2", "round_3") if transcript.get(k)),
        },
        "personas": {
            aid: {"name": personas[aid].name,
                  "type": "custom" if personas[aid].is_custom else "default"}
            for aid in personas
        },
        "resilience_score": score,
        "round_1": transcript.get("round_1", []),
        "round_2": transcript.get("round_2", []),
        "round_3": transcript.get("round_3", []),
        "synthesis": synthesis,
        "proposal_summary": proposal_summary,
    }
    debate_data["markdown_report"] = render_markdown(debate_data)

    if write_files:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        DEBATE_DATA_PATH.write_text(json.dumps(debate_data, indent=2), encoding="utf-8")
        print(f"[INFO] Debate data written: {DEBATE_DATA_PATH}")
        MARKDOWN_PATH.write_text(debate_data["markdown_report"], encoding="utf-8")
        print(f"[INFO] Markdown report written: {MARKDOWN_PATH}")

    return debate_data


async def amain() -> int:
    args = parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[ERROR] ANTHROPIC_API_KEY not set.")
        return 2

    proposal, source_label, title = resolve_proposal(args)
    if not proposal.strip():
        print("[ERROR] Proposal text is empty.")
        return 2

    debate_data = await run_pipeline(
        proposal_text=proposal,
        title=title,
        source_label=source_label,
        proposal_type=args.proposal_type,
        persona_args=args,
        continue_on_critical=None,
        write_files=True,
    )

    # DOCX generation via Node (CLI-only — Node is not available in the Vercel runtime)
    if shutil.which("node"):
        try:
            subprocess.run(
                ["node", "generate_report.js", str(DEBATE_DATA_PATH)],
                check=True,
            )
            print(f"[INFO] DOCX report written: {DOCX_PATH}")
        except subprocess.CalledProcessError as exc:
            print(f"[WARN] DOCX generation failed: {exc}")
    else:
        print("[WARN] Node.js not found. Falling back to .md only.")

    s = debate_data["resilience_score"]
    print(f"\n=== RESILIENCE SCORE: {s['total']}/100 — {s['band']} ===\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
