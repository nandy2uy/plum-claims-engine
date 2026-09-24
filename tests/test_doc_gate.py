from decimal import Decimal
from src.agents.doc_gate import DocumentGateAgent
from src.models.claim import ClaimSubmission, DocumentSource


def _submission(category, doc_types, **overrides):
    defaults = dict(
        claim_id="T1", member_id="EMP001", patient_id="EMP001",
        category=category, treatment_date="2024-11-01", claimed_amount=Decimal(1000),
        documents=[DocumentSource(file_id=f"F{i}", file_type=t) for i, t in enumerate(doc_types)],
    )
    defaults.update(overrides)
    return ClaimSubmission(**defaults)


def test_blocks_when_required_document_missing(policy_config, ledger):
    agent = DocumentGateAgent(policy_config)
    submission = _submission("CONSULTATION", ["PRESCRIPTION", "PRESCRIPTION"])

    passed, payload = agent.evaluate(submission, ledger)

    assert passed is False
    assert payload["decision"] == "NEEDS_MEMBER_ACTION"
    assert "hospital bill" in payload["message"].lower()
    assert ledger.events[-1].outcome == "FAIL"
    assert ledger.events[-1].effect == "BLOCK"


def test_passes_when_all_required_documents_present(policy_config, ledger):
    agent = DocumentGateAgent(policy_config)
    submission = _submission("CONSULTATION", ["PRESCRIPTION", "HOSPITAL_BILL"])

    passed, payload = agent.evaluate(submission, ledger)

    assert passed is True
    assert payload == {}
    assert ledger.events[-1].outcome == "PASS"


def test_unknown_category_is_a_clean_block_not_a_crash(policy_config, ledger):
    agent = DocumentGateAgent(policy_config)
    submission = _submission("SOMETHING_MADE_UP", ["PRESCRIPTION"])

    passed, payload = agent.evaluate(submission, ledger)

    assert passed is False
    assert payload["decision"] == "NEEDS_MEMBER_ACTION"
    assert "not a recognized claim category" in payload["message"]


def test_category_normalization_handles_spaces_and_case(policy_config, ledger):
    # "Alternative Medicine" (spaces, mixed case) must resolve to the same
    # bucket as ALTERNATIVE_MEDICINE in policy_terms.json — the original
    # implementation's bare .upper() would not have matched this.
    agent = DocumentGateAgent(policy_config)
    submission = _submission("Alternative Medicine", ["PRESCRIPTION", "HOSPITAL_BILL"])

    passed, _ = agent.evaluate(submission, ledger)

    assert passed is True
