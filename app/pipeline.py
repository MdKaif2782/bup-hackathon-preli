"""End-to-end pipeline: LLM interpretation -> guardrails -> optimizer -> replay check."""
from __future__ import annotations

import logging

from app.guardrails import GuardrailError, no_op, to_directive
from app.llm.client import LLMUnavailable, interpret_notes
from app.llm.fallback import interpret_fallback
from app.optimizer import ScheduleInfeasible, optimize, totals
from app.schemas import OptimizeRequest
from app.validator import replay

log = logging.getLogger("gridwise.pipeline")


class InfeasibleScenario(Exception):
    """Scenario cannot be scheduled under the base battery rules (HTTP 422)."""


def interpret(req: OptimizeRequest) -> tuple[list[dict], str]:
    notes = req.operator_notes
    try:
        items, source = interpret_notes(notes)
    except Exception as exc:  # LLMUnavailable or anything unexpected: degrade, never 5xx
        reason = str(exc) if isinstance(exc, LLMUnavailable) else type(exc).__name__
        log.warning("LLM interpretation unavailable (%s); using fallback parser", reason)
        items, source = [interpret_fallback(n) for n in notes], "fallback"

    directives = []
    for i, item in enumerate(items):
        try:
            directives.append(to_directive(i, item, req.battery.capacity_kwh))
        except GuardrailError as exc:
            # Never invent a constraint from output that failed validation.
            log.warning("guardrail rejected note %d: %s", i, exc)
            directives.append(no_op(i, "Interpretation rejected by guardrails; no constraint applied."))
    return directives, source


def summarize(directives: list[dict], plan: list[dict], tot: dict, relaxed: bool) -> str:
    applied = [d["directive_type"] for d in directives if d["applies"]]
    ignored = sum(1 for d in directives if not d["applies"])
    charge_h = [p["hour"] for p in plan if p["battery_action"] == "charge"]
    dis_h = [p["hour"] for p in plan if p["battery_action"] == "discharge"]
    parts = [
        f"Applied {len(applied)} operator directive(s)"
        + (f" ({', '.join(applied)})" if applied else "")
        + (f"; {ignored} note(s) ignored as no_op." if ignored else "."),
        "Cost-minimising LP schedule: battery charges in cheaper hours"
        + (f" ({_span(charge_h)})" if charge_h else "")
        + " and discharges in expensive hours"
        + (f" ({_span(dis_h)})" if dis_h else "")
        + ", using available solar and returning to the initial state of charge.",
        f"Total grid import {tot['total_grid_kwh']} kWh, cost {tot['total_cost_bdt']} BDT, peak {tot['peak_grid_kwh']} kWh.",
    ]
    if relaxed:
        parts.append("Warning: directives were infeasible together; minimal violation was applied.")
    return " ".join(parts)


def _span(hours: list[int]) -> str:
    return "hours " + ",".join(str(h) for h in hours)


def run_pipeline(req: OptimizeRequest) -> dict:
    directives, source = interpret(req)
    try:
        plan, _, relaxed = optimize(req, directives)
    except ScheduleInfeasible as exc:
        raise InfeasibleScenario(str(exc)) from exc
    tot = totals(req, plan)
    response = {
        "scenario_id": req.scenario_id,
        "directive_interpretation": directives,
        "hourly_plan": plan,
        **tot,
        "plan_summary": summarize(directives, plan, tot, relaxed),
    }
    errors = replay(req.model_dump(), directives, response)
    if errors and not relaxed:
        log.error("self-check failed for %s: %s", req.scenario_id, errors[:3])
    log.info("scenario=%s source=%s relaxed=%s cost=%s", req.scenario_id, source, relaxed, tot["total_cost_bdt"])
    return response
