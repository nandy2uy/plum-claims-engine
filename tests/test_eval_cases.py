"""All 12 assignment cases through the real pipeline with the strict checks used for EVAL_REPORT.md."""

import pytest

from src.core.config import load_test_cases
from src.core.orchestrator import ClaimsOrchestrator
from src.core.storage import ClaimStore
from src.evals.adapter import case_to_submission
from src.evals.checks import check_case

CASES = load_test_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["case_id"] for c in CASES])
async def test_case(case, settings):
    decision = await ClaimsOrchestrator(settings=settings, store=ClaimStore()).process(case_to_submission(case))
    baseline = None
    if case["input"].get("simulate_component_failure"):
        baseline = (await ClaimsOrchestrator(settings=settings, store=ClaimStore())
                    .process(case_to_submission(case, simulate_failure=False))).model_dump(mode="json")
    failed = [c for c in check_case(case, decision.model_dump(mode="json"), baseline) if not c.passed]
    assert not failed, [(c.name, c.detail) for c in failed]


async def test_every_decided_case_has_a_complete_trace(settings):
    for case in CASES:
        d = await ClaimsOrchestrator(settings=settings, store=ClaimStore()).process(case_to_submission(case))
        assert d.trace[0].component == "INTAKE" and d.trace[-1].component == "DECISION"
        assert all(e.summary for e in d.trace)
        assert [e.seq for e in d.trace] == list(range(1, len(d.trace) + 1))
