"""End-to-end pipeline: LLM interpretation -> guardrails -> optimizer -> replay check."""
from __future__ import annotations

from app.schemas import OptimizeRequest


class InfeasibleScenario(Exception):
    """Scenario cannot be scheduled even with directive relaxation (HTTP 422)."""


def run_pipeline(req: OptimizeRequest) -> dict:
    raise NotImplementedError
