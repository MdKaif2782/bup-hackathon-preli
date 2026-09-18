"""Optimizer against public samples using the reference (ground-truth) directives."""
import pytest

from app.optimizer import optimize, totals
from app.schemas import OptimizeRequest
from app.validator import replay
from tests.conftest import load_cases

CASES = load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_sample_reaches_reference_optimum(case):
    req = OptimizeRequest.model_validate(case["input"])
    directives = case["expected_output"]["directive_interpretation"]
    plan, _, relaxed = optimize(req, directives)
    resp = {"hourly_plan": plan, **totals(req, plan)}
    assert not relaxed
    assert replay(case["input"], directives, resp) == []
    assert resp["total_cost_bdt"] == pytest.approx(case["expected_output"]["total_cost_bdt"], abs=0.01)


def test_reference_plans_pass_our_validator(cases):
    for case in cases:
        exp = case["expected_output"]
        assert replay(case["input"], exp["directive_interpretation"], exp) == [], case["id"]
