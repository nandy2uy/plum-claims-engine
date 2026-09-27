"""Maps a test_cases.json case onto the public ClaimSubmission contract.

Component contract
-------------------
case_to_payload(case, simulate_failure=None) -> dict   (valid ClaimSubmission JSON)
case_to_submission(case, simulate_failure=None) -> ClaimSubmission
Errors: pydantic.ValidationError if the case cannot form a valid submission.

The adapter is deliberately thin. Document `content` is passed through as the
fixture extraction output unchanged, because the extraction models accept the
field names the test data uses (diagnosis, doctor_registration, total, treatment,
test_name) through validation aliases. v1's adapter rewrote fields, including
mapping `treatment` onto `tests_ordered` so that a rule would fire; that hid
the fact the engine did not understand treatments at all.

Per document:
  file_type          = actual_type (what the member uploaded it as)
  pre_extracted_data = {detected_type: actual_type, quality, <kind>_data: content (+ patient_name_on_doc)}
"""

from __future__ import annotations

from typing import Optional

from src.models.claim import ClaimSubmission
from src.models.medical import BILL_TYPES, PRESCRIPTION_TYPES, REPORT_TYPES

_PASSTHROUGH = ("policy_id", "treatment_date", "claimed_amount", "hospital_name", "ytd_claims_amount",
                "claims_history", "simulate_component_failure", "pre_auth_id", "submission_date", "patient_id")


def _data_key(doc_type: str) -> Optional[str]:
    if doc_type in PRESCRIPTION_TYPES:
        return "prescription_data"
    if doc_type in BILL_TYPES:
        return "bill_data"
    if doc_type in REPORT_TYPES:
        return "report_data"
    return None


def case_to_payload(case: dict, simulate_failure: Optional[bool] = None) -> dict:
    inp = case.get("input", {})
    payload = {"claim_id": case["case_id"], "member_id": inp["member_id"], "category": inp["claim_category"]}
    payload.update({k: inp[k] for k in _PASSTHROUGH if k in inp})
    if simulate_failure is not None:
        payload["simulate_component_failure"] = simulate_failure
    documents = []
    for doc in inp.get("documents", []):
        doc_type = doc.get("actual_type", "OTHER")
        fixture = {"detected_type": doc_type, "quality": doc.get("quality", "GOOD")}
        key = _data_key(doc_type)
        if key:
            content = dict(doc.get("content") or {})
            if doc.get("patient_name_on_doc") and "patient_name" not in content:
                content["patient_name"] = doc["patient_name_on_doc"]
            fixture[key] = content
        documents.append({"file_id": doc["file_id"], "file_name": doc.get("file_name"), "file_type": doc_type,
                          "pre_extracted_data": fixture})
    payload["documents"] = documents
    return payload


def case_to_submission(case: dict, simulate_failure: Optional[bool] = None) -> ClaimSubmission:
    return ClaimSubmission.model_validate(case_to_payload(case, simulate_failure))
