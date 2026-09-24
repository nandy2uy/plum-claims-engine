"""In-process end-to-end tests against the FastAPI app (no live server
needed — this is what generates EVAL_REPORT.md). Covers all 12 assignment
test cases plus two regressions found while building this: the extractor
crash on real (non-fixture) documents, and cross-test fraud-store
contamination when two unrelated claims happen to share a member_id and
treatment_date.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.main import app
from scripts.run_evals import map_plum_test_to_api_payload, _check_system_must

client = TestClient(app)
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_cases():
    data = json.loads((DATA_DIR / "test_cases.json").read_text())
    return data["test_cases"]


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["case_id"])
def test_assignment_case(case):
    expected = case.get("expected", {})
    expected_decision = "NEEDS_MEMBER_ACTION" if expected.get("decision") is None else expected["decision"]
    if "approved_amount" in expected:
        expected_amount = Decimal(str(expected["approved_amount"]))
    elif expected_decision == "APPROVED":
        expected_amount = Decimal(str(case["input"].get("claimed_amount", 0)))
    else:
        expected_amount = Decimal(0)

    payload = map_plum_test_to_api_payload(case)
    response = client.post("/api/v1/claims", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()

    assert result["decision"] == expected_decision, f"{case['case_id']}: {result['reason']}"
    if result["decision"] in ("APPROVED", "PARTIAL"):
        assert Decimal(str(result["approved_amount"])) == expected_amount, f"{case['case_id']}: {result['reason']}"

    notes = _check_system_must(case, result)
    assert not notes, f"{case['case_id']} qualitative check failed: {notes}"


def test_real_document_submission_never_crashes():
    """Regression test for the original fatal bug: any claim using real
    content_url documents (not the eval-harness pre_extracted_data fixture
    format) crashed the server with a 500 (IndexError on an empty list)."""
    response = client.post("/api/v1/claims", json={
        "claim_id": "CLM-REAL-001", "member_id": "EMP001", "patient_id": "EMP001",
        "category": "CONSULTATION", "treatment_date": "2024-11-01", "claimed_amount": 1500,
        "documents": [
            {"file_id": "F1", "file_type": "PRESCRIPTION", "content_url": "https://example.com/rx.jpg"},
            {"file_id": "F2", "file_type": "HOSPITAL_BILL", "content_url": "https://example.com/bill.jpg"},
        ],
    })

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "NEEDS_MEMBER_ACTION"
    assert body["trace"]  # must be traceable, not a silent failure


def test_blocked_claims_do_not_pollute_fraud_history_for_other_claims():
    """Regression test: a claim that never got past the document gate must
    not count toward another, unrelated claim's same-day fraud frequency
    just because they share a member_id and treatment_date."""
    blocked_payload = {
        "claim_id": "BLOCKED-1", "member_id": "EMP001", "patient_id": "EMP001",
        "category": "CONSULTATION", "treatment_date": "2024-11-01", "claimed_amount": 1500,
        "documents": [{"file_id": "F1", "file_type": "PRESCRIPTION"}],  # missing required HOSPITAL_BILL
    }
    for i in range(2):
        blocked_payload["claim_id"] = f"BLOCKED-{i}"
        r = client.post("/api/v1/claims", json=blocked_payload)
        assert r.json()["decision"] == "NEEDS_MEMBER_ACTION"

    clean_payload = {
        "claim_id": "CLEAN-1", "member_id": "EMP001", "patient_id": "EMP001",
        "category": "CONSULTATION", "treatment_date": "2024-11-01", "claimed_amount": 1500,
        "documents": [
            {"file_id": "F1", "file_type": "PRESCRIPTION",
             "pre_extracted_data": {"prescription_data": {"diagnoses": ["Viral Fever"]}}},
            {"file_id": "F2", "file_type": "HOSPITAL_BILL",
             "pre_extracted_data": {"bill_data": {"line_items": [{"description": "Consultation Fee", "amount": 1500}]}}},
        ],
    }
    r = client.post("/api/v1/claims", json=clean_payload)
    result = r.json()
    assert result["decision"] == "APPROVED", result["reason"]


def test_claim_is_retrievable_after_submission_for_decision_review():
    payload = {
        "claim_id": "REVIEWABLE-1", "member_id": "EMP002", "patient_id": "EMP002",
        "category": "DENTAL", "treatment_date": "2024-10-15", "claimed_amount": 8000,
        "documents": [{"file_id": "F1", "file_type": "HOSPITAL_BILL",
                       "pre_extracted_data": {"bill_data": {"line_items": [{"description": "Root Canal Treatment", "amount": 8000}]}}}],
    }
    client.post("/api/v1/claims", json=payload)

    response = client.get("/api/v1/claims/REVIEWABLE-1")
    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"

    listing = client.get("/api/v1/claims").json()
    assert any(c["claim_id"] == "REVIEWABLE-1" for c in listing)
