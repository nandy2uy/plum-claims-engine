from datetime import date

from src.agents.doc_gate import DocumentGateAgent
from src.agents.intake import IntakeAgent
from src.models.claim import ClaimSubmission
from tests.conftest import bill, claim, rx


def _intake(policy, payload, ledger):
    return IntakeAgent(policy, "test").evaluate(ClaimSubmission.model_validate(payload), ledger)


def test_unknown_member_is_member_actionable(policy, ledger):
    result = _intake(policy, claim(member="EMP999"), ledger)
    assert not result.ok and "EMP999" in result.message and result.actions[0].action == "CORRECT_DETAILS"


def test_patient_must_be_linked_to_member(policy, ledger):
    # v1 accepted DEP001 (EMP001's spouse) under EMP002
    result = _intake(policy, claim(member="EMP002", patient_id="DEP001"), ledger)
    assert not result.ok and "not registered as a dependent" in result.message


def test_dependent_inherits_primary_join_date(policy, ledger):
    result = _intake(policy, claim(member="EMP001", patient_id="DEP002"), ledger)
    assert result.ok and result.context.coverage_start == date(2024, 4, 1)
    assert result.context.relationship == "CHILD" and result.context.patient_name == "Arjun Kumar"


def test_linked_dependent_without_roster_record_is_flagged_not_blocked(policy, ledger):
    result = _intake(policy, claim(member="EMP003", patient_id="DEP003"), ledger)
    assert result.ok and result.context.roster_incomplete


def test_policy_id_mismatch_blocks(policy, ledger):
    result = _intake(policy, claim(policy_id="OTHER_POLICY"), ledger)
    assert not result.ok and "PLUM_GHI_2024" in result.message


def test_future_treatment_date_blocks(policy, ledger):
    result = IntakeAgent(policy, "t", today=lambda: date(2024, 10, 1)).evaluate(
        ClaimSubmission.model_validate(claim(date="2024-11-01")), ledger)
    assert not result.ok and "future" in result.message


def test_gate_names_uploaded_and_missing_types(policy, interp, ledger):
    gate = DocumentGateAgent(policy, interp)
    sub = ClaimSubmission.model_validate(claim(documents=[rx("F1"), rx("F2")]))
    result = gate.evaluate(sub, ledger)
    assert not result.passed and result.missing == ["HOSPITAL_BILL"]
    assert "'f1.jpg' (prescription)" in result.message and "hospital bill" in result.message
    assert "in place of 'f2.jpg' (prescription)" in result.message
    assert result.actions[0].document_type == "HOSPITAL_BILL"
    assert ledger.events[-1].outcome == "FAIL" and ledger.events[-1].policy_ref.endswith(".required")


def test_gate_unknown_category(policy, interp, ledger):
    result = DocumentGateAgent(policy, interp).evaluate(ClaimSubmission.model_validate(claim(category="SPA")), ledger)
    assert not result.passed and "not a claim category" in result.message


def test_gate_passes_and_logs(policy, interp, ledger):
    result = DocumentGateAgent(policy, interp).evaluate(ClaimSubmission.model_validate(claim()), ledger)
    assert result.passed and ledger.events[-1].outcome == "PASS"


def test_submission_model_rejects_bad_input():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ClaimSubmission.model_validate(claim(date="15-10-2024"))  # v1 accepted this and skipped waiting periods
    with pytest.raises(ValidationError):
        ClaimSubmission.model_validate(claim(amount=0))
    with pytest.raises(ValidationError):
        ClaimSubmission.model_validate(claim(documents=[rx("A"), bill("A")]))
