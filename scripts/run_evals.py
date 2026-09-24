"""Runs data/test_cases.json against a live server and prints pass/fail per
case, including the qualitative "system_must" assertions the previous
version silently ignored (it only ever checked decision + approved_amount).

Usage: start the API (`uvicorn src.main:app`), then `python scripts/run_evals.py`.
For a fully in-process run (no server needed) plus a full trace dump per
case, see tests/test_eval_cases.py / EVAL_REPORT.md instead — this script
exists to exercise the real HTTP path end to end.
"""

import json
import sys
import requests
from pathlib import Path
from decimal import Decimal

API_URL = "http://127.0.0.1:8000/api/v1/claims"
BASE_DIR = Path(__file__).resolve().parent.parent
TEST_CASES_FILE = BASE_DIR / "data" / "test_cases.json"


def map_plum_test_to_api_payload(case: dict) -> dict:
    case_input = case.get("input", {})

    payload = {
        "claim_id": case.get("case_id", "UNKNOWN"),
        "member_id": case_input.get("member_id", "UNKNOWN"),
        "patient_id": case_input.get("member_id", "UNKNOWN"),
        "category": case_input.get("claim_category", "UNKNOWN"),
        "treatment_date": case_input.get("treatment_date", "2024-01-01"),
        "claimed_amount": case_input.get("claimed_amount", 0),
        "hospital_name": case_input.get("hospital_name"),
        "policy_id": case_input.get("policy_id"),
        "documents": [],
    }

    # These used to be silently dropped here, which meant the backend could
    # never see them regardless of whether it was coded to use them.
    if "ytd_claims_amount" in case_input:
        payload["ytd_claims_amount"] = case_input["ytd_claims_amount"]
    if "claims_history" in case_input:
        payload["claims_history"] = case_input["claims_history"]
    if "simulate_component_failure" in case_input:
        payload["simulate_component_failure"] = case_input["simulate_component_failure"]

    for doc in case_input.get("documents", []):
        mapped_doc = {
            "file_id": doc.get("file_name") or doc.get("file_id", "doc"),
            "file_type": doc.get("actual_type", "UNKNOWN"),
            "pre_extracted_data": {},
        }

        if "quality" in doc:
            mapped_doc["pre_extracted_data"]["quality"] = doc["quality"]

        if "patient_name_on_doc" in doc:
            dtype = doc.get("actual_type", "")
            if dtype == "PRESCRIPTION":
                mapped_doc["pre_extracted_data"]["prescription_data"] = {"patient_name": doc["patient_name_on_doc"]}
            else:
                mapped_doc["pre_extracted_data"]["bill_data"] = {"patient_name": doc["patient_name_on_doc"]}

        if "content" in doc:
            content = dict(doc["content"])
            dtype = doc.get("actual_type", "")

            if dtype == "PRESCRIPTION":
                if "diagnosis" in content and "diagnoses" not in content:
                    content["diagnoses"] = [content.pop("diagnosis")]
                if "treatment" in content and "tests_ordered" not in content:
                    content["tests_ordered"] = [content.pop("treatment")]
                if "doctor_registration" in content and "registration_number" not in content:
                    content["registration_number"] = content.pop("doctor_registration")
                mapped_doc["pre_extracted_data"]["prescription_data"] = content
            else:
                # ExtractedBill's field is `total_amount`, not `total` — the
                # fixture content uses `total`, so it must be renamed or it's
                # silently dropped by Pydantic.
                if "total" in content and "total_amount" not in content:
                    content["total_amount"] = content.pop("total")
                existing = mapped_doc["pre_extracted_data"].get("bill_data", {})
                mapped_doc["pre_extracted_data"]["bill_data"] = {**existing, **content}

        payload["documents"].append(mapped_doc)

    return payload


def _check_system_must(case: dict, result: dict) -> list:
    """Best-effort structural checks for the qualitative 'system_must'
    bullets test_cases.json specifies. Not a substitute for reading the
    actual message text, but catches the class of bug where a case
    "passes" on decision+amount while structurally violating its own
    requirements (this is exactly how the previous version's TC011 bug hid)."""
    notes = []
    case_id = case.get("case_id")

    if case_id == "TC011":
        if result.get("confidence_score", 1.0) >= 0.9:
            notes.append(f"expected confidence noticeably below a clean approval, got {result.get('confidence_score')}")
        if not result.get("degraded_components"):
            notes.append("expected at least one degraded_component, got none")
        if not result.get("manual_review_recommended"):
            notes.append("expected manual_review_recommended=true")

    if case_id == "TC009":
        if not result.get("fraud_signals"):
            notes.append("expected non-empty fraud_signals")

    if case_id == "TC006":
        rejected = [li for li in result.get("line_item_breakdown", []) if li.get("status") == "REJECTED"]
        if not rejected:
            notes.append("expected at least one REJECTED line item in line_item_breakdown")

    if case_id == "TC010":
        fb = result.get("financial_breakdown") or {}
        if not fb.get("network_discount_applied"):
            notes.append("expected financial_breakdown.network_discount_applied=true")

    expected_reason_codes = case.get("expected", {}).get("rejection_reasons")
    if expected_reason_codes:
        reason_text = result.get("reason", "")
        for code in expected_reason_codes:
            if code not in reason_text:
                notes.append(f"expected rejection reason code '{code}' to appear in the reason text, got: {reason_text!r}")

    return notes


def run_evaluations():
    if not TEST_CASES_FILE.exists():
        print(f"Could not find test cases at {TEST_CASES_FILE}")
        sys.exit(1)

    with open(TEST_CASES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    test_cases = data.get("test_cases", [])
    if not test_cases:
        return

    print(f"\nRunning {len(test_cases)} test cases against {API_URL}\n" + "=" * 60)
    passed_count = 0

    for case in test_cases:
        claim_id = case.get("case_id", "UNKNOWN")
        expected = case.get("expected", {})

        expected_decision = "NEEDS_MEMBER_ACTION" if expected.get("decision") is None else expected.get("decision")

        if "approved_amount" in expected:
            expected_amount = Decimal(str(expected["approved_amount"]))
        elif expected_decision == "APPROVED":
            expected_amount = Decimal(str(case.get("input", {}).get("claimed_amount", 0)))
        else:
            expected_amount = Decimal(0)

        payload = map_plum_test_to_api_payload(case)
        print(f"\nEvaluating: {claim_id} - {case.get('case_name')}")

        try:
            response = requests.post(API_URL, json=payload, timeout=15)
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.RequestException as e:
            print(f"   FAIL: API request failed: {e}")
            continue

        actual_decision = result.get("decision")
        actual_amount = Decimal(str(result.get("approved_amount", 0)))

        decision_match = actual_decision == expected_decision
        amount_match = True
        if actual_decision in ["APPROVED", "PARTIAL"]:
            amount_match = actual_amount == expected_amount

        qualitative_notes = _check_system_must(case, result)

        if decision_match and amount_match and not qualitative_notes:
            print(f"   PASS: decision={actual_decision}" + (f", amount=₹{actual_amount}" if actual_decision in ["APPROVED", "PARTIAL"] else ""))
            print(f"      Reason: {result.get('reason')}")
            passed_count += 1
        else:
            print("   FAIL")
            if not decision_match:
                print(f"      Expected decision: {expected_decision}, got: {actual_decision}")
            if not amount_match:
                print(f"      Expected amount: ₹{expected_amount}, got: ₹{actual_amount}")
            for note in qualitative_notes:
                print(f"      Qualitative check failed: {note}")
            print(f"      Reason given: {result.get('reason')}")
            if result.get("degraded_components"):
                print(f"      Degraded agents: {result.get('degraded_components')}")

    print("\n" + "=" * 60)
    print(f"FINAL SCORE: {passed_count}/{len(test_cases)} passed")


if __name__ == "__main__":
    run_evaluations()
