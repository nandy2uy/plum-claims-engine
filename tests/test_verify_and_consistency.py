from src.agents.consistency import ConsistencyAgent
from src.agents.doc_gate import DocumentGateAgent
from src.agents.doc_verifier import DocumentVerificationAgent
from src.agents.intake import IntakeAgent
from src.models.claim import ClaimSubmission
from src.models.medical import DocumentExtraction, ExtractedBill, ExtractedPrescription
from tests.conftest import claim


def _ext(file_id, declared, detected=None, failure=None, flags=(), rx=None, bill=None):
    return DocumentExtraction(file_id=file_id, file_name=f"{file_id}.jpg", declared_type=declared, detected_type=detected or declared,
                              source="fixture", confidence_score=0.95, failure_kind=failure, flags=list(flags),
                              prescription_data=ExtractedPrescription(**rx) if rx is not None else None,
                              bill_data=ExtractedBill(**bill) if bill is not None else None)


def _verifier(policy, interp):
    return DocumentVerificationAgent(DocumentGateAgent(policy, interp), interp)


SUB = ClaimSubmission.model_validate(claim(category="PHARMACY"))


def test_unreadable_required_document_asks_for_that_file(policy, interp, ledger):
    exts = [_ext("rx", "PRESCRIPTION", rx={}), _ext("blurry_bill", "PHARMACY_BILL", failure="MEMBER", flags=["UNREADABLE"])]
    result = _verifier(policy, interp).evaluate(SUB, exts, ledger)
    assert result.status == "NEEDS_MEMBER_ACTION"
    assert "blurry_bill.jpg" in result.message and "re-upload" in result.message and "not been rejected" in result.message
    assert result.actions[0].action == "REUPLOAD_DOCUMENT" and result.actions[0].file_id == "blurry_bill"


def test_prescription_uploaded_as_bill_is_caught_after_extraction(policy, interp, ledger):
    exts = [_ext("rx", "PRESCRIPTION", rx={}), _ext("fake_bill", "PHARMACY_BILL", detected="PRESCRIPTION", rx={})]
    result = _verifier(policy, interp).evaluate(SUB, exts, ledger)
    assert result.status == "NEEDS_MEMBER_ACTION"
    assert "appears to be a prescription" in result.message and result.actions[0].action == "REPLACE_DOCUMENT"


def test_system_failure_on_required_doc_degrades_instead_of_blaming_member(policy, interp, ledger):
    exts = [_ext("rx", "PRESCRIPTION", rx={}), _ext("b", "PHARMACY_BILL", failure="SYSTEM", flags=["EXTRACTION_FAILED"])]
    result = _verifier(policy, interp).evaluate(SUB, exts, ledger)
    assert result.status == "DEGRADED" and result.system_failed_required == ["b.jpg"]


def test_bad_registration_lowers_payout_confidence(policy, interp, ledger):
    exts = [_ext("rx", "PRESCRIPTION", rx={"registration_number": "12345"}), _ext("b", "PHARMACY_BILL", bill={"total": 100})]
    assert _verifier(policy, interp).evaluate(SUB, exts, ledger).status == "OK"
    warn = [e for e in ledger.events if e.rule_id == "doctor_registration"][0]
    assert warn.outcome == "WARN" and warn.confidence_scope == "PAYOUT"


def _context(policy, ledger, member="EMP001", patient=None):
    return IntakeAgent(policy, "t").evaluate(
        ClaimSubmission.model_validate(claim(member=member, patient_id=patient)), ledger).context


def _consistency(policy, interp, ledger, names, member="EMP001", date="2024-11-01", doc_dates=None):
    exts = [_ext(f"d{i}", "PRESCRIPTION", rx={"patient_name": n, "date": (doc_dates or {}).get(i)}) for i, n in enumerate(names)]
    sub = ClaimSubmission.model_validate(claim(member=member, date=date))
    return ConsistencyAgent(interp).evaluate(sub, _context(policy, ledger, member), exts, ledger)


def test_different_patients_names_each_file(policy, interp, ledger):
    result = _consistency(policy, interp, ledger, ["Rajesh Kumar", "Arjun Mehta"])
    assert not result.passed and "Rajesh Kumar" in result.message and "Arjun Mehta" in result.message
    assert "'d1.jpg'" in result.message


def test_father_and_son_sharing_surname_is_a_mismatch(policy, interp, ledger):
    assert not _consistency(policy, interp, ledger, ["Rajesh Kumar", "Arjun Kumar"]).passed


def test_documents_for_someone_else_than_patient_on_record(policy, interp, ledger):
    result = _consistency(policy, interp, ledger, ["Arjun Mehta", "Arjun Mehta"])
    assert not result.passed and "does not match the patient" in result.message


def test_ocr_typo_passes_with_confidence_factor(policy, interp, ledger):
    assert _consistency(policy, interp, ledger, ["Rajesh Kumaar", "Rajesh Kumar"]).passed
    assert any(e.confidence_factor for e in ledger.events if e.component == "CONSISTENCY")


def test_no_names_lowers_payout_confidence(policy, interp, ledger):
    assert _consistency(policy, interp, ledger, [None, None]).passed
    ev = [e for e in ledger.events if e.rule_id == "identity_evidence"][0]
    assert ev.confidence_factor == interp.confidence.identity_unverified and ev.confidence_scope == "PAYOUT"


def test_document_dates_far_from_treatment_are_flagged(policy, interp, ledger):
    _consistency(policy, interp, ledger, ["Rajesh Kumar"], doc_dates={0: "2024-09-01"})
    ev = [e for e in ledger.events if e.rule_id == "document_dates"][0]
    assert ev.outcome == "WARN"
