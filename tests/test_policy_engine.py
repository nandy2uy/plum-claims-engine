from decimal import Decimal
from src.agents.policy_engine import PolicyEngineAgent
from src.models.claim import ClaimSubmission, DocumentSource
from src.models.medical import DocumentExtraction, ExtractedPrescription, ExtractedBill, ExtractedLineItem


def _submission(**overrides):
    defaults = dict(
        claim_id="T1", member_id="EMP001", patient_id="EMP001", category="CONSULTATION",
        treatment_date="2024-11-01", claimed_amount=Decimal(1500),
        documents=[DocumentSource(file_id="F1", file_type="PRESCRIPTION"), DocumentSource(file_id="F2", file_type="HOSPITAL_BILL")],
    )
    defaults.update(overrides)
    return ClaimSubmission(**defaults)


def _agent(policy_config, interpretation_config):
    return PolicyEngineAgent(policy_config, interpretation_config)


def test_network_discount_applied_before_copay(policy_config, interpretation_config, ledger):
    # ₹4,500 at a network hospital: 20% discount -> ₹3,600, then 10% copay -> ₹3,240.
    # Applying these in the wrong order (copay first) would give a different number,
    # which is exactly what this test guards against.
    submission = _submission(claimed_amount=Decimal(4500), hospital_name="Apollo Hospitals")
    extractions = [
        DocumentExtraction(file_id="F2", file_type="HOSPITAL_BILL", confidence_score=0.9, bill_data=ExtractedBill(
            hospital_name="Apollo Hospitals",
            line_items=[ExtractedLineItem(description="Consultation Fee", amount=Decimal(1500)),
                        ExtractedLineItem(description="Medicines", amount=Decimal(3000))],
        )),
    ]

    result = _agent(policy_config, interpretation_config).evaluate(submission, extractions, ledger)

    assert result.decision == "APPROVED"
    assert result.approved_amount == Decimal("3240.00")
    assert result.financial_breakdown.network_discount_applied is True


def test_category_sub_limit_does_not_silently_cap_a_valid_approval(policy_config, interpretation_config, ledger):
    # Regression test: consultation's sub_limit (₹2,000) is lower than this
    # claim's correct post-copay amount (₹3,240). An earlier draft of this
    # engine capped on sub_limit here and broke the assignment's own expected
    # output for this exact scenario. sub_limit must be advisory-only.
    submission = _submission(claimed_amount=Decimal(4500), hospital_name="Apollo Hospitals")
    extractions = [
        DocumentExtraction(file_id="F2", file_type="HOSPITAL_BILL", confidence_score=0.9, bill_data=ExtractedBill(
            hospital_name="Apollo Hospitals",
            line_items=[ExtractedLineItem(description="Consultation Fee", amount=Decimal(1500)),
                        ExtractedLineItem(description="Medicines", amount=Decimal(3000))],
        )),
    ]

    result = _agent(policy_config, interpretation_config).evaluate(submission, extractions, ledger)

    assert result.approved_amount == Decimal("3240.00")
    assert result.decision == "APPROVED"
    assert any("sub-limit" in w for w in result.warnings)


def test_dental_line_item_exclusion_is_partial_not_full_reject(policy_config, interpretation_config, ledger):
    submission = _submission(category="DENTAL", claimed_amount=Decimal(12000),
                              documents=[DocumentSource(file_id="F1", file_type="HOSPITAL_BILL")])
    extractions = [
        DocumentExtraction(file_id="F1", file_type="HOSPITAL_BILL", confidence_score=0.9, bill_data=ExtractedBill(
            line_items=[ExtractedLineItem(description="Root Canal Treatment", amount=Decimal(8000)),
                        ExtractedLineItem(description="Teeth Whitening", amount=Decimal(4000))],
        )),
    ]

    result = _agent(policy_config, interpretation_config).evaluate(submission, extractions, ledger)

    assert result.decision == "PARTIAL"
    assert result.approved_amount == Decimal("8000.00")
    statuses = {li.description: li.status for li in result.line_items}
    assert statuses["Root Canal Treatment"] == "APPROVED"
    assert statuses["Teeth Whitening"] == "REJECTED"


def test_waiting_period_states_the_eligibility_date(policy_config, interpretation_config, ledger):
    submission = _submission(
        member_id="EMP005", patient_id="EMP005", treatment_date="2024-10-15",
        documents=[DocumentSource(file_id="F1", file_type="PRESCRIPTION"), DocumentSource(file_id="F2", file_type="HOSPITAL_BILL")],
    )
    extractions = [
        DocumentExtraction(file_id="F1", file_type="PRESCRIPTION", confidence_score=0.9,
                            prescription_data=ExtractedPrescription(diagnoses=["Type 2 Diabetes Mellitus"])),
    ]

    result = _agent(policy_config, interpretation_config).evaluate(submission, extractions, ledger)

    assert result.decision == "REJECTED"
    assert "WAITING_PERIOD" in result.reason
    assert "2024-11-30" in result.reason  # join_date 2024-09-01 + 90 days


def test_herniation_does_not_false_positive_as_hernia_waiting_period(policy_config, interpretation_config, ledger):
    # Regression test: naive substring matching let "hernia" match inside
    # "herniation" (an unrelated disc condition), wrongly triggering hernia's
    # 365-day waiting period instead of the pre-auth check this case is
    # actually about.
    submission = _submission(
        member_id="EMP007", patient_id="EMP007", category="DIAGNOSTIC",
        claimed_amount=Decimal(15000), treatment_date="2024-11-02",
        documents=[DocumentSource(file_id="F1", file_type="PRESCRIPTION"),
                   DocumentSource(file_id="F2", file_type="LAB_REPORT"),
                   DocumentSource(file_id="F3", file_type="HOSPITAL_BILL")],
    )
    extractions = [
        DocumentExtraction(file_id="F1", file_type="PRESCRIPTION", confidence_score=0.9,
                            prescription_data=ExtractedPrescription(
                                diagnoses=["Suspected Lumbar Disc Herniation"], tests_ordered=["MRI Lumbar Spine"])),
    ]

    result = _agent(policy_config, interpretation_config).evaluate(submission, extractions, ledger)

    assert result.decision == "REJECTED"
    assert "PRE_AUTH_MISSING" in result.reason
    assert "hernia" not in result.reason.lower()


def test_exclusion_takes_precedence_over_overlapping_waiting_period(policy_config, interpretation_config, ledger):
    # Regression test: "obesity"/"bariatric" appear in BOTH the exclusions
    # list and the obesity_treatment waiting-period keywords. An outright
    # exclusion must win -- there's no point quoting a wait date for
    # something that's never covered.
    submission = _submission(category="CONSULTATION", claimed_amount=Decimal(8000), member_id="EMP009", patient_id="EMP009")
    extractions = [
        DocumentExtraction(file_id="F1", file_type="PRESCRIPTION", confidence_score=0.9,
                            prescription_data=ExtractedPrescription(
                                diagnoses=["Morbid Obesity - BMI 37"],
                                tests_ordered=["Bariatric Consultation and Customised Diet Plan"])),
    ]

    result = _agent(policy_config, interpretation_config).evaluate(submission, extractions, ledger)

    assert result.decision == "REJECTED"
    assert "EXCLUDED_CONDITION" in result.reason
    assert "WAITING_PERIOD" not in result.reason


def test_per_claim_limit_rejects_before_touching_line_items(policy_config, interpretation_config, ledger):
    submission = _submission(claimed_amount=Decimal(7500), member_id="EMP003", patient_id="EMP003")
    extractions = [
        DocumentExtraction(file_id="F1", file_type="PRESCRIPTION", confidence_score=0.9,
                            prescription_data=ExtractedPrescription(diagnoses=["Gastroenteritis"])),
    ]

    result = _agent(policy_config, interpretation_config).evaluate(submission, extractions, ledger)

    assert result.decision == "REJECTED"
    assert "PER_CLAIM_EXCEEDED" in result.reason
    assert result.approved_amount == Decimal(0)


def test_annual_opd_limit_caps_remaining_balance(policy_config, interpretation_config, ledger):
    submission = _submission(claimed_amount=Decimal(1000), ytd_claims_amount=Decimal(49500))
    extractions = [
        DocumentExtraction(file_id="F2", file_type="HOSPITAL_BILL", confidence_score=0.9,
                            bill_data=ExtractedBill(line_items=[ExtractedLineItem(description="Consultation Fee", amount=Decimal(1000))])),
    ]

    result = _agent(policy_config, interpretation_config).evaluate(submission, extractions, ledger)

    # copay 10% -> 900 eligible, but only 500 remains in the annual OPD limit
    assert result.decision == "PARTIAL"
    assert result.approved_amount == Decimal("500.00")
