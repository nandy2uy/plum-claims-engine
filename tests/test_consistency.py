from decimal import Decimal
from src.agents.consistency import ConsistencyAgent
from src.models.claim import ClaimSubmission, DocumentSource
from src.models.medical import DocumentExtraction, ExtractedPrescription, ExtractedBill


def _submission(member_id="EMP001", patient_id="EMP001"):
    return ClaimSubmission(
        claim_id="T1", member_id=member_id, patient_id=patient_id, category="CONSULTATION",
        treatment_date="2024-11-01", claimed_amount=Decimal(1000),
        documents=[DocumentSource(file_id="F1", file_type="PRESCRIPTION"), DocumentSource(file_id="F2", file_type="HOSPITAL_BILL")],
    )


def test_doc_to_doc_mismatch_is_blocked(policy_config, ledger):
    agent = ConsistencyAgent(policy_config)
    extractions = [
        DocumentExtraction(file_id="F1", file_type="PRESCRIPTION", confidence_score=0.9,
                            prescription_data=ExtractedPrescription(patient_name="Rajesh Kumar")),
        DocumentExtraction(file_id="F2", file_type="HOSPITAL_BILL", confidence_score=0.9,
                            bill_data=ExtractedBill(patient_name="Arjun Mehta")),
    ]

    consistent, message = agent.evaluate(_submission(), extractions, ledger)

    assert consistent is False
    assert "Rajesh Kumar" in message and "Arjun Mehta" in message


def test_docs_agree_with_each_other_but_not_with_policy_member_is_still_blocked(policy_config, ledger):
    # EMP001 on record is Rajesh Kumar. Both documents agreeing with each
    # other but naming someone else should still be caught -- this is the
    # roster cross-check the previous version didn't have at all.
    agent = ConsistencyAgent(policy_config)
    extractions = [
        DocumentExtraction(file_id="F1", file_type="PRESCRIPTION", confidence_score=0.9,
                            prescription_data=ExtractedPrescription(patient_name="Someone Else")),
        DocumentExtraction(file_id="F2", file_type="HOSPITAL_BILL", confidence_score=0.9,
                            bill_data=ExtractedBill(patient_name="Someone Else")),
    ]

    consistent, message = agent.evaluate(_submission(member_id="EMP001", patient_id="EMP001"), extractions, ledger)

    assert consistent is False
    assert "policy member on file" in message


def test_matching_names_pass(policy_config, ledger):
    agent = ConsistencyAgent(policy_config)
    extractions = [
        DocumentExtraction(file_id="F1", file_type="PRESCRIPTION", confidence_score=0.9,
                            prescription_data=ExtractedPrescription(patient_name="Mr. Rajesh Kumar")),
        DocumentExtraction(file_id="F2", file_type="HOSPITAL_BILL", confidence_score=0.9,
                            bill_data=ExtractedBill(patient_name="Rajesh Kumar")),
    ]

    consistent, message = agent.evaluate(_submission(), extractions, ledger)

    assert consistent is True
    assert message == ""


def test_no_names_extracted_does_not_block(policy_config, ledger):
    agent = ConsistencyAgent(policy_config)
    extractions = [
        DocumentExtraction(file_id="F1", file_type="PRESCRIPTION", confidence_score=0.9),
        DocumentExtraction(file_id="F2", file_type="HOSPITAL_BILL", confidence_score=0.9),
    ]

    consistent, _ = agent.evaluate(_submission(), extractions, ledger)

    assert consistent is True
