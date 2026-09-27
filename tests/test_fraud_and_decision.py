from decimal import Decimal

from src.core.config import Settings
from tests.conftest import bill, claim, events, line, rx

HISTORY = [{"claim_id": f"CLM_{i}", "date": "2024-11-01", "amount": 1000 + i, "provider": "Clinic"} for i in range(3)]


async def test_same_day_signal_routes_to_review_with_provisional_amount(run):
    d = await run(claim(claims_history=HISTORY))
    assert d.decision == "MANUAL_REVIEW" and "FRAUD_SIGNALS" in d.review_reasons
    assert d.approved_amount == 0 and d.provisional_amount == Decimal("900.00")
    assert any("SAME_DAY_LIMIT_EXCEEDED" in s and "CLM_0" in s for s in d.fraud_signals)
    assert "fraud" not in d.member_message.lower()  # never tip off the claimant


async def test_empty_caller_history_cannot_hide_stored_claims(run):  # v1 used caller history INSTEAD of the store
    for i in range(3):
        await run(claim(claim_id=f"C{i}", amount=1000 + i))
    d = await run(claim(claim_id="C9", amount=1200, claims_history=[]))
    assert "FRAUD_SIGNALS" in d.review_reasons


async def test_reused_document_is_a_hard_signal(run):
    docs = lambda: [rx(diagnoses=["Viral Fever"]),  # noqa: E731
                    bill(bill_number="B-77", hospital_name="City Clinic", line_items=[line("Consultation", 1000)])]
    await run(claim(claim_id="A1", documents=docs()))
    d = await run(claim(claim_id="A2", date="2024-11-05", documents=docs()))
    assert any(s.startswith("DUPLICATE_DOCUMENT") for s in d.fraud_signals) and d.decision == "MANUAL_REVIEW"


async def test_soft_signal_lowers_confidence_without_escalating(run):
    docs = [rx(diagnoses=["Viral Fever"]),
            {**bill(line_items=[line("Consultation", 1000)]), "pre_extracted_data": {
                "flags": ["DOCUMENT_ALTERATION"], "bill_data": {"line_items": [line("Consultation", 1000)]}}}]
    d = await run(claim(documents=docs))
    assert d.decision == "APPROVED" and d.fraud_score == 0.5
    assert any(f.rule_id == "fraud_soft_signals" for f in d.confidence_breakdown)


async def test_fault_injection_is_ignored_unless_enabled(run):  # v1: TC009 + flag -> APPROVED 4,320
    d = await run(claim(claims_history=HISTORY, simulate_component_failure=True),
                  settings_override=Settings(gemini_api_key="", allow_fault_injection=False,
                                             allow_fixture_documents=True))
    assert d.decision == "MANUAL_REVIEW" and "FRAUD" not in d.degraded_components
    assert events(d, "FAULT_INJECTION")[0].outcome == "WARN"


async def test_fraud_failure_degrades_and_is_visible(run):
    normal = await run(claim(claim_id="N"))
    failed = await run(claim(claim_id="F", simulate_component_failure=True))
    assert failed.decision == "APPROVED" and failed.degraded_components == ["FRAUD"]
    assert failed.manual_review_recommended and failed.confidence_score < normal.confidence_score
    assert any("manual review" in w for w in failed.warnings)




async def test_policy_engine_failure_is_critical(run):
    d = await run(claim(simulate_component_failure=True, simulate_failure_component="POLICY_ENGINE"))
    assert d.decision == "MANUAL_REVIEW" and "POLICY_ENGINE_UNAVAILABLE" in d.review_reasons
    assert "problem on our side" in d.member_message


async def test_extractor_failure_never_blames_member(run):
    d = await run(claim(simulate_component_failure=True, simulate_failure_component="EXTRACTOR"))
    assert d.decision == "MANUAL_REVIEW" and "EXTRACTION_INCOMPLETE" in d.review_reasons
    assert "re-upload" not in d.member_message.lower()


async def test_confidence_is_reconstructable_from_breakdown(run):
    d = await run(claim(simulate_component_failure=True))
    product = 1.0
    for f in d.confidence_breakdown:
        product *= f.factor
    assert round(product, 3) == d.confidence_score


async def test_payout_scoped_factors_do_not_weaken_rejections(run, interp):
    d = await run(claim(date="2025-06-01"))  # no patient names -> identity factor is PAYOUT-scoped
    assert d.decision == "REJECTED"
    assert all(f.rule_id != "identity_evidence" for f in d.confidence_breakdown)


def _preauth_mri(amount):
    from tests.conftest import report
    return dict(member="EMP007", category="DIAGNOSTIC", amount=amount, pre_auth_id="PA-9",
                documents=[rx(diagnoses=["Back pain"]), report(tests=["MRI"]), bill(line_items=[line("MRI Brain", amount)])])


async def test_high_value_claim_is_auto_reviewed(run):
    d = await run(claim(**_preauth_mri(26000)))
    assert d.decision == "MANUAL_REVIEW" and any(s.startswith("HIGH_VALUE_AUTO_REVIEW") for s in d.fraud_signals)


async def test_fraud_outage_on_high_value_claim_holds_it(run):
    d = await run(claim(**_preauth_mri(26000), simulate_component_failure=True))
    assert d.decision == "MANUAL_REVIEW" and "FRAUD_CHECK_UNAVAILABLE_HIGH_VALUE" in d.review_reasons
