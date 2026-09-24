from decimal import Decimal
from src.agents.fraud import FraudAgent
from src.models.claim import ClaimSubmission, DocumentSource, ClaimHistoryEntry


def _submission(**overrides):
    defaults = dict(
        claim_id="T1", member_id="EMP008", patient_id="EMP008", category="CONSULTATION",
        treatment_date="2024-10-30", claimed_amount=Decimal(4800),
        documents=[DocumentSource(file_id="F1", file_type="PRESCRIPTION")],
    )
    defaults.update(overrides)
    return ClaimSubmission(**defaults)


def test_same_day_limit_exceeded_via_explicit_claims_history(policy_config, ledger):
    submission = _submission(claims_history=[
        ClaimHistoryEntry(claim_id="C1", date="2024-10-30", amount=Decimal(1200)),
        ClaimHistoryEntry(claim_id="C2", date="2024-10-30", amount=Decimal(1800)),
        ClaimHistoryEntry(claim_id="C3", date="2024-10-30", amount=Decimal(2100)),
    ])

    signals, score = FraudAgent(policy_config).evaluate(submission, ledger=ledger)

    assert any("same-day" in s for s in signals)
    assert score > 0


def test_no_history_no_signal(policy_config, ledger):
    submission = _submission(claims_history=[])

    signals, score = FraudAgent(policy_config).evaluate(submission, ledger=ledger)

    assert signals == []


def test_high_value_claim_is_flagged(policy_config, ledger):
    submission = _submission(claimed_amount=Decimal(30000), claims_history=[])

    signals, score = FraudAgent(policy_config).evaluate(submission, ledger=ledger)

    assert any("auto-manual-review threshold" in s for s in signals)


def test_simulated_failure_degrades_gracefully_instead_of_crashing(policy_config, ledger):
    submission = _submission(simulate_component_failure=True)

    # FraudAgent.evaluate is wrapped in @isolate_fault, so this must not raise.
    signals, score = FraudAgent(policy_config).evaluate(submission, ledger=ledger)

    assert signals == []
    assert score == 0.0
    assert any(e.component == "FRAUD" and e.outcome == "ERROR" for e in ledger.events)


def test_every_threshold_is_logged_even_when_clean(policy_config, ledger):
    submission = _submission(claims_history=[])

    FraudAgent(policy_config).evaluate(submission, ledger=ledger)

    rule_ids = {e.rule_id for e in ledger.events}
    assert "same_day_claims_limit" in rule_ids
    assert "monthly_claims_limit" in rule_ids
    assert "high_value_claim_threshold" in rule_ids
