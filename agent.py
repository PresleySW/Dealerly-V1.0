"""
dealerly/agent.py
=================
Sprint 16: Dealerly Agent Loop

Adds an adaptive intelligence layer on top of the existing Phase 1 pipeline.
Instead of executing a fixed preset search once, the agent observes the growing
listing pool, calls Claude (or OpenAI) to decide the next action, executes
pipeline tools, and repeats until it has enough buy signals or exhausts its
step budget.

Architecture (three layers per the agent instruction):
  1. Tool layer  — thin wrappers around existing pipeline/adapter functions
  2. Agent loop  — observe → think → act → update → repeat
  3. Model layer — Claude/OpenAI JSON reasoning via offers.py

Three actions (minimal viable):
  "search"  — fetch listings from all enabled adapters using agent-chosen params
  "score"   — lightweight price-band heuristic to count potential buy signals
  "finish"  — accept current pool and hand off to Phase 2+

Activation:
  cfg.agent_mode = True  (set by --agent CLI flag)

Integration:
  pipeline.run() calls run_dealerly_agent() in place of the normal Phase 1
  concurrent block when cfg.agent_mode is True.  The returned AgentState.listings
  becomes the `listings` variable that feeds Phase 2+ unchanged.

Depends on:
  - dealerly.models  (Listing)
  - dealerly.offers  (_claude_request, _openai_request, claude_api_key, openai_api_key)
  - dealerly.config  (QUERY_PRESETS)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dealerly.models import Listing

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_AGENT_STEPS: int = 10
MIN_BUY_TARGET: int  = 3   # agent keeps searching until it estimates this many potential buys

# ---------------------------------------------------------------------------
# Agent state
# ---------------------------------------------------------------------------

@dataclass
class AgentState:
    """Mutable state passed between agent steps."""
    task: str
    listings: List[Listing]         = field(default_factory=list)
    buy_count: int                  = 0
    offer_count: int                = 0
    search_history: List[Dict[str, Any]] = field(default_factory=list)
    step: int                       = 0
    done: bool                      = False
    final_reason: str               = ""


# ---------------------------------------------------------------------------
# Tool layer
# ---------------------------------------------------------------------------

def _tool_search(
    state: AgentState,
    params: Dict[str, Any],
    adapters: list,
    cfg,
) -> int:
    """
    Fetch listings from all enabled adapters using agent-specified params.

    Merges results into state.listings (deduplicates by item_id).
    Returns the number of new listings added.
    """
    from dealerly.ebay import merge_dedupe

    queries   = params.get("queries") or _default_queries(cfg)
    price_min = int(params.get("price_min", cfg.price_min))
    price_max = int(params.get("price_max", cfg.price_max))
    pages     = max(1, min(int(params.get("pages", 2)), 3))

    existing_ids = {l.item_id for l in state.listings}
    new_batches: List[List[Listing]] = []

    for adapter in adapters:
        try:
            batch = adapter.fetch_listings(
                queries=queries,
                price_min=price_min,
                price_max=price_max,
                pages=pages,
                buyer_postcode=getattr(cfg, "buyer_postcode", ""),
                sort=getattr(cfg, "sort", "endingSoonest"),
            )
            new_batches.append(batch)
        except Exception as exc:
            print(f"  [agent] {adapter.platform_name()} error: {exc}")

    merged = merge_dedupe(new_batches) if new_batches else []
    truly_new = [l for l in merged if l.item_id not in existing_ids]
    state.listings.extend(truly_new)

    state.search_history.append({
        "step":      state.step,
        "queries":   queries,
        "price_min": price_min,
        "price_max": price_max,
        "added":     len(truly_new),
        "total":     len(state.listings),
    })
    return len(truly_new)


def _tool_score(state: AgentState, cfg) -> None:
    """
    Lightweight price-band heuristic — no DB or comps required.

    Counts listings at or below 65 % of price_max as "potential buys",
    those between 65–85 % as "potential offers".  Updates state.buy_count
    and state.offer_count so Claude can decide whether to search further.
    """
    if not state.listings:
        return

    prices        = [l.price_gbp for l in state.listings]
    buy_ceiling   = cfg.price_max * 0.65
    offer_ceiling = cfg.price_max * 0.85

    state.buy_count   = sum(1 for p in prices if p <= buy_ceiling)
    state.offer_count = sum(1 for p in prices if buy_ceiling < p <= offer_ceiling)

    avg_price = sum(prices) / len(prices) if prices else 0.0
    state.search_history.append({
        "step":           state.step,
        "type":           "score_result",
        "total_listings": len(state.listings),
        "buy_signals":    state.buy_count,
        "offer_signals":  state.offer_count,
        "avg_price":      round(avg_price, 0),
    })


def _tool_finish(state: AgentState, reason: str) -> None:
    """Mark the agent as done."""
    state.done = True
    state.final_reason = reason


# ---------------------------------------------------------------------------
# Model layer
# ---------------------------------------------------------------------------

def _build_prompt(state: AgentState, cfg, max_steps: int) -> str:
    """Construct the Claude/OpenAI decision prompt from current agent state."""
    recent_history = state.search_history[-4:] if state.search_history else []
    return (
        f"You are Dealerly Agent — an adaptive car-flip intelligence system.\n\n"
        f"Task: {state.task}\n\n"
        f"Current state:\n"
        f"  listings_gathered: {len(state.listings)}\n"
        f"  buy_signals (heuristic): {state.buy_count}\n"
        f"  offer_signals (heuristic): {state.offer_count}\n"
        f"  step: {state.step + 1} of {max_steps}\n"
        f"  recent_history: {json.dumps(recent_history, separators=(',', ':'))}\n\n"
        f"Config budget: price_min=£{cfg.price_min}, price_max=£{cfg.price_max}\n"
        f"Target: at least {MIN_BUY_TARGET} buy signals before finishing.\n\n"
        f"Available actions:\n"
        f'  "search" — fetch listings. params: {{"queries":["make model",...], '
        f'"price_min":int, "price_max":int, "pages":1-3}}\n'
        f'  "score"  — estimate buy/offer counts. params: {{}}\n'
        f'  "finish" — accept current pool. params: {{}}\n\n'
        f"Decision rules:\n"
        f"  - If no listings yet → search\n"
        f"  - If listings exist but not scored → score\n"
        f"  - If buy_signals >= {MIN_BUY_TARGET} → finish\n"
        f"  - If step >= {max_steps - 2} → finish\n"
        f"  - If last search added 0 listings → finish or try different queries\n"
        f"  - Otherwise → search (refine queries or widen price band by ≤20%)\n\n"
        f"Respond ONLY with valid JSON, no markdown fences:\n"
        f'{{"action":"search"|"score"|"finish","reason":"short explanation",'
        f'"parameters":{{...}}}}'
    )


def _call_model(state: AgentState, cfg, max_steps: int) -> Dict[str, Any]:
    """
    Ask Claude (preferred) or OpenAI to decide the next agent action.

    Falls back to a deterministic heuristic if no AI backend is configured.
    Returns a dict with keys: action, reason, parameters.
    """
    try:
        from dealerly.offers import (
            _claude_request,
            _openai_request,
            claude_api_key,
            openai_api_key,
        )
    except ImportError:
        return _fallback_decision(state, cfg, max_steps)

    prompt   = _build_prompt(state, cfg, max_steps)
    messages = [{"role": "user", "content": prompt}]
    system   = "You are a JSON-only decision agent. Output only valid JSON."

    raw: Optional[str] = None
    if claude_api_key():
        raw = _claude_request(messages, system=system, max_tokens=320)
    # Fall through to OpenAI if Claude key is exhausted/invalid (raw is None)
    if raw is None and openai_api_key():
        raw = _openai_request(messages, system=system, max_tokens=320)

    if not raw:
        return _fallback_decision(state, cfg, max_steps)

    # Strip markdown code fences if the model wraps its output
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw   = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        print(f"  [agent] JSON parse error — raw: {raw[:120]!r}")
        return _fallback_decision(state, cfg, max_steps)


def _fallback_decision(state: AgentState, cfg, max_steps: int) -> Dict[str, Any]:
    """
    Deterministic decision when no AI backend is available.

    Implements the same rules described in the prompt so the agent still
    behaves sensibly without Claude or OpenAI.
    """
    if not state.listings:
        return {"action": "search", "reason": "no listings — start search (fallback)",
                "parameters": {}}
    if not any(e.get("type") == "score_result" for e in state.search_history):
        return {"action": "score", "reason": "listings unscored (fallback)",
                "parameters": {}}
    if state.buy_count >= MIN_BUY_TARGET or state.step >= max_steps - 2:
        return {"action": "finish",
                "reason": f"buy_signals={state.buy_count} / step={state.step} (fallback)",
                "parameters": {}}
    last_added = next(
        (e.get("added", 0) for e in reversed(state.search_history)
         if e.get("type") != "score_result"),
        0,
    )
    if last_added == 0:
        return {"action": "finish", "reason": "last search returned 0 (fallback)",
                "parameters": {}}
    # Widen price band slightly to find more listings
    return {
        "action":     "search",
        "reason":     "expanding price band (fallback)",
        "parameters": {"price_max": int(cfg.price_max * 1.15)},
    }


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_dealerly_agent(
    cfg,
    adapters: list,
    task: Optional[str] = None,
    max_steps: int      = MAX_AGENT_STEPS,
) -> AgentState:
    """
    Adaptive Phase 1 agent loop: observe → think → act → update → repeat.

    Replaces the fixed-preset Phase 1 concurrent block in pipeline.run() when
    cfg.agent_mode is True.  After this function returns, pipeline.run() uses
    state.listings as the Phase 1 output and continues with Phase 2+.

    Args:
        cfg:       Dealerly Config (price_min, price_max, preset, pages, …).
        adapters:  Pre-built list of BaseIngestionAdapter instances.
        task:      Optional task description; auto-generated from cfg if None.
        max_steps: Hard cap on the number of observe-think-act cycles.

    Returns:
        AgentState with final listings and lightweight buy/offer estimates.
    """
    if task is None:
        task = (
            f"Find profitable car flips between £{cfg.price_min} and "
            f"£{cfg.price_max}. Target at least {MIN_BUY_TARGET} buy signals."
        )

    state = AgentState(task=task)
    adapter_names = [a.platform_name() for a in adapters]
    print(f"\n  [agent] Dealerly Agent starting")
    print(f"  [agent] task:     {task}")
    print(f"  [agent] adapters: {adapter_names}")
    print(f"  [agent] budget:   £{cfg.price_min}–£{cfg.price_max}, max_steps={max_steps}\n")

    for step in range(max_steps):
        state.step = step

        decision = _call_model(state, cfg, max_steps)
        action   = str(decision.get("action", "finish")).lower().strip()
        params   = decision.get("parameters") or {}
        reason   = str(decision.get("reason", ""))

        print(f"  [agent] step {step + 1:>2}/{max_steps}  {action.upper():<8}  {reason}")

        # Never accept an empty pool before the last step — models sometimes emit
        # "finish" when adapters returned 0 rows (blocked / misconfigured).
        if action == "finish" and not state.listings and step < max_steps - 1:
            print(
                "  [agent] Ignoring finish — no listings gathered yet; "
                "forcing search with default queries"
            )
            action = "search"
            params = {}

        if action == "search":
            added = _tool_search(state, params, adapters, cfg)
            print(f"  [agent]          → +{added} new  (total {len(state.listings)})")

        elif action == "score":
            _tool_score(state, cfg)
            print(
                f"  [agent]          → buy≈{state.buy_count}"
                f"  offer≈{state.offer_count}"
                f"  total={len(state.listings)}"
            )

        elif action == "finish":
            _tool_finish(state, reason)
            print(f"  [agent] Finished — {reason}")
            break

        else:
            print(f"  [agent] Unknown action '{action}' — stopping.")
            _tool_finish(state, f"unknown action: {action}")
            break

        time.sleep(0.05)

    if not state.done:
        _tool_finish(state, f"step budget ({max_steps}) exhausted")
        print(f"  [agent] Step budget reached — closing with {len(state.listings)} listings.")

    print(
        f"\n  [agent] Done: {len(state.listings)} listings"
        f" | buy≈{state.buy_count} | offer≈{state.offer_count}"
        f" | {state.final_reason}\n"
    )
    return state


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _default_queries(cfg) -> List[str]:
    """Return config preset queries when the agent hasn't specified any.

    QUERY_PRESETS values are dicts: {"mode": ..., "qs": [...], "desc": ...}
    We extract the "qs" list, not the dict keys.
    """
    from dealerly.config import QUERY_PRESETS
    preset = str(getattr(cfg, "preset", "") or "6")
    entry  = QUERY_PRESETS.get(preset) or QUERY_PRESETS.get("6", {})
    queries = entry.get("qs", []) if isinstance(entry, dict) else []
    return list(queries)[:8]
