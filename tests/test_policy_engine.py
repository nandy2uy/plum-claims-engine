"""Policy rules, run through the full pipeline so each test also proves the trace records the rule.
Many of these are regressions for defects found in the v1 audit (marked v1)."""

from decimal import Decimal

from tests.conftest import bill, claim, events, line, report, rx

REG = "KA/45678/2015"


async def test_clean_approval_logs_every_rule_it_checked(run):
    d = await run(claim(amount=1000))
    assert d.decision == "APPROVED" and d.approved_amount == Decimal("900.00")
    rules = {e.rule_id for e in events(d, "POLICY_ENGINE")}
    for rule in ("category_covered", "minimum_claim_amount", "submission_deadline", "policy_status", "policy_period",
                 "member_coverage_date", "relationship_covered", "initial_waiting_period", "pre_existing_conditions",
                 "exclusions", "condition_waiting_period", "per_claim_limit", "network_discount", "copay", "annual_limit"):
        assert rule in rules, rule


async def test_payout_never_exceeds_claimed_amount(run):  # v1: claimed 1000, bill 4000 -> 3600
    d = await run(claim(amount=1000, documents=[rx(diagnoses=["Viral Fever"]), bill(line_items=[line("Consultation", 4000)])]))
    assert d.approved_amount == Decimal("900.00")


async def test_child_dependent_is_covered(run):  # v1: CHILD vs CHILDREN rejected every child
    docs = [rx(patient_name="Arjun Kumar", diagnoses=["Viral Fever"]), bill(patient_name="Arjun Kumar", line_items=[line("Consultation", 1000)])]
    d = await run(claim(member="EMP001", patient_id="DEP002", documents=docs))
    assert d.decision == "APPROVED"


async def test_dependent_waiting_period_applies(run):  # v1: dependents skipped waiting periods
    docs = [rx(patient_name="Sunita Kumar", diagnoses=["T2DM"]), bill(patient_name="Sunita Kumar", line_items=[line("Consultation", 1000)])]
    d = await run(claim(member="EMP001", patient_id="DEP001", date="2024-06-01", documents=docs))
    assert d.decision == "REJECTED" and "WAITING_PERIOD" in d.rejection_reasons and "30 Jun 2024" in d.member_message


async def test_initial_waiting_period(run):
    d = await run(claim(member="EMP005", date="2024-09-15"))
    assert d.decision == "REJECTED" and "WAITING_PERIOD" in d.rejection_reasons and "01 Oct 2024" in d.member_message


async def test_outside_policy_period(run):  # v1 approved
    d = await run(claim(date="2025-06-01"))
    assert "OUTSIDE_POLICY_PERIOD" in d.rejection_reasons


async def test_all_claim_level_reasons_are_reported_not_just_the_first(run):
    d = await run(claim(date="2025-06-01", amount=400, submission_date="2025-08-01"))
    assert {"OUTSIDE_POLICY_PERIOD", "BELOW_MINIMUM_AMOUNT", "SUBMISSION_DEADLINE_EXCEEDED"} <= set(d.rejection_reasons)


async def test_submission_deadline_not_evaluable_without_date(run):
    d = await run(claim())
    assert events(d, "POLICY_ENGINE", "submission_deadline")[0].outcome == "NOT_EVALUABLE"


async def test_every_bill_is_used(run):  # v1 used only the first bill
    docs = [rx(diagnoses=["Viral Fever"]), bill(line_items=[line("Consultation", 1000)]),
            bill("PH", "PHARMACY_BILL", line_items=[line("Paracetamol", 800)])]
    d = await run(claim(amount=1800, documents=docs))
    assert len(d.line_item_breakdown) == 2 and d.approved_amount == Decimal("1620.00")


async def test_mri_on_bill_only_still_needs_pre_auth(run):  # v1 approved 15,000
    docs = [rx(diagnoses=["Back pain"]), report(tests=["MRI"]), bill(line_items=[line("MRI Lumbar Spine", 15000)])]
    d = await run(claim(member="EMP007", category="DIAGNOSTIC", amount=15000, documents=docs))
    assert d.decision == "REJECTED" and d.rejection_reasons == ["PRE_AUTH_MISSING"]
    assert any(a.action == "OBTAIN_PRE_AUTH" for a in d.required_actions)


async def test_pre_auth_is_per_line_so_other_items_are_paid(run):
    docs = [rx(diagnoses=["Back pain"]), report(tests=["MRI"]),
            bill(line_items=[line("MRI Lumbar Spine", 12000), line("Consultation", 800)])]
    d = await run(claim(member="EMP007", category="DIAGNOSTIC", amount=12800, documents=docs))
    assert d.decision == "PARTIAL" and d.approved_amount == Decimal("800.00")


async def test_pre_auth_id_allows_high_value_scan(run):
    docs = [rx(diagnoses=["Back pain"]), report(tests=["MRI"]), bill(line_items=[line("MRI Lumbar Spine", 15000)])]
    d = await run(claim(member="EMP007", category="DIAGNOSTIC", amount=15000, pre_auth_id="PA-123", documents=docs))
    assert d.decision == "APPROVED" and d.approved_amount == Decimal("15000.00")


async def test_pet_scan_always_needs_pre_auth(run):
    docs = [rx(diagnoses=["Staging"]), report(tests=["PET"]), bill(line_items=[line("PET-CT Whole Body", 4000)])]
    d = await run(claim(member="EMP007", category="DIAGNOSTIC", amount=4000, documents=docs))
    assert d.rejection_reasons == ["PRE_AUTH_MISSING"]


async def test_lasik_and_braces_are_excluded(run):  # v1 approved both (exact name matching)
    d = await run(claim(category="VISION", amount=4000,
                        documents=[rx(diagnoses=["Myopia"]), bill(line_items=[line("LASIK surgery - both eyes", 4000)])]))
    assert d.rejection_reasons == ["EXCLUDED_PROCEDURE"]
    d = await run(claim(category="DENTAL", amount=6000, documents=[bill(line_items=[line("Braces (metal)", 6000)])]))
    assert d.rejection_reasons == ["EXCLUDED_PROCEDURE"]


async def test_formatted_amounts_are_parsed(run):  # v1: "₹1,000" crashed the batch
    d = await run(claim(amount=1000, documents=[rx(diagnoses=["Viral Fever"]), bill(line_items=[line("Consultation", "₹1,000")])]))
    assert d.decision == "APPROVED"


async def test_category_sub_limit_above_global_limit_is_the_ceiling(run):
    docs = [rx(diagnoses=["Gastritis"]), bill("PH", "PHARMACY_BILL", line_items=[line("Pantoprazole 40mg", 12000, is_branded=False)])]
    d = await run(claim(category="PHARMACY", amount=12000, documents=docs))
    assert d.decision == "APPROVED" and d.approved_amount == Decimal("12000.00")


async def test_branded_drugs_get_branded_copay(run):
    docs = [rx(diagnoses=["Gastritis"]), bill("PH", "PHARMACY_BILL", line_items=[
        line("Pan 40 (branded)", 1000, is_branded=True), line("Pantoprazole generic", 1000, is_branded=False)])]
    d = await run(claim(category="PHARMACY", amount=2000, documents=docs))
    assert d.approved_amount == Decimal("1700.00")


async def test_exclusion_supersedes_waiting_period(run):
    docs = [rx(diagnoses=["Obesity"]), bill(line_items=[line("Consultation", 1000)])]
    d = await run(claim(member="EMP009", date="2024-10-18", documents=docs))
    assert d.rejection_reasons == ["EXCLUDED_CONDITION"]
    assert events(d, "POLICY_ENGINE", "condition_waiting_period")[0].outcome == "SKIP"


async def test_line_level_exclusion_keeps_the_rest(run):
    docs = [rx(diagnoses=["Viral Fever"]), bill(line_items=[line("Consultation", 1000), line("Protein powder supplement", 500)])]
    d = await run(claim(amount=1500, documents=docs))
    assert d.decision == "PARTIAL" and d.approved_amount == Decimal("900.00")


async def test_vaccination_goes_to_review_not_rejection(run):
    docs = [rx(diagnoses=["Dog bite"], treatments=["Anti-rabies vaccination"]), bill(line_items=[line("Consultation", 1000)])]
    d = await run(claim(documents=docs))
    assert d.decision == "MANUAL_REVIEW" and "ITEM_NEEDS_REVIEW" in d.review_reasons


async def test_model_only_clinical_tag_routes_to_review_never_rejects(run):
    docs = [rx(diagnoses=["Sugar problem, on tablets"], condition_tags=["diabetes"]), bill(line_items=[line("Consultation", 1000)])]
    d = await run(claim(member="EMP005", date="2024-10-15", documents=docs))
    assert d.decision == "MANUAL_REVIEW" and "CLINICAL_CODING_UNCERTAIN" in d.review_reasons


async def test_annual_limit_caps_payout(run):
    d = await run(claim(amount=1000, ytd_claims_amount=49500))
    assert d.decision == "PARTIAL" and d.approved_amount == Decimal("500.00")
    d = await run(claim(amount=1000, ytd_claims_amount=50000))
    assert d.rejection_reasons == ["ANNUAL_LIMIT_EXHAUSTED"]


async def test_network_hospital_matched_from_bill(run):
    docs = [rx(diagnoses=["Viral Fever"]), bill(hospital_name="Apollo Hospitals, Jayanagar", line_items=[line("Consultation", 1000)])]
    d = await run(claim(amount=1000, documents=docs))
    assert d.financial_breakdown.network_discount_applied and d.approved_amount == Decimal("720.00")


async def test_alt_medicine_unregistered_practitioner_goes_to_review(run):
    docs = [rx(registration_number="12345", treatments=["Panchakarma"]), bill(line_items=[line("Panchakarma (3 sessions)", 3000)])]
    d = await run(claim(member="EMP006", category="ALTERNATIVE_MEDICINE", amount=3000, documents=docs))
    assert d.decision == "MANUAL_REVIEW" and "PRACTITIONER_UNVERIFIED" in d.review_reasons


async def test_roster_incomplete_dependent_goes_to_review(run):
    d = await run(claim(member="EMP003", patient_id="DEP003"))
    assert d.decision == "MANUAL_REVIEW" and "ROSTER_INCOMPLETE" in d.review_reasons


async def test_negated_condition_does_not_trigger_waiting_period(run):
    docs = [rx(diagnoses=["Viral Fever", "No family history of diabetes"]), bill(line_items=[line("Consultation", 1000)])]
    d = await run(claim(member="EMP005", date="2024-10-15", documents=docs))
    assert d.decision == "APPROVED"
    skip = events(d, "POLICY_ENGINE", "condition_waiting_period", "SKIP")
    assert skip and "negated" in skip[0].summary


async def test_affirmed_condition_still_triggers_waiting_period(run):
    docs = [rx(diagnoses=["Type 2 Diabetes; no hypertension"]), bill(line_items=[line("Consultation", 1000)])]
    d = await run(claim(member="EMP005", date="2024-10-15", documents=docs))
    assert d.rejection_reasons == ["WAITING_PERIOD"] and "diabetes" in d.member_message.lower()


async def test_negated_abbreviation_is_not_a_diagnosis(run):
    docs = [rx(diagnoses=["Viral fever. No HTN"]), bill(line_items=[line("Consultation", 1000)])]
    d = await run(claim(member="EMP005", date="2024-10-15", documents=docs))
    assert d.decision == "APPROVED"


async def test_plural_exclusion_keyword_on_a_bill_line(run):
    docs = [rx(diagnoses=["Viral Fever"]), bill(line_items=[line("Consultation", 1000), line("Calcium supplements", 400)])]
    d = await run(claim(amount=1400, documents=docs))
    assert d.decision == "PARTIAL" and d.approved_amount == Decimal("900.00")


async def test_missing_diagnosis_goes_to_review_when_prescription_required(run):
    docs = [rx(patient_name="Priya Singh"), bill(line_items=[line("Consultation", 1000)])]
    d = await run(claim(documents=docs))
    assert d.decision == "MANUAL_REVIEW" and "DIAGNOSIS_UNREADABLE" in d.review_reasons
    assert d.provisional_amount == Decimal("900.00")


async def test_missing_diagnosis_is_fine_when_prescription_not_required(run):
    d = await run(claim(category="DENTAL", amount=2000, documents=[bill(line_items=[line("Tooth Extraction", 2000)])]))
    assert d.decision == "APPROVED"


async def test_live_empty_prescription_asks_member_instead_of_approving(run):
    from src.core.llm import LLMDocumentResult

    class Backend:
        async def extract(self, doc):
            if doc.file_type == "PRESCRIPTION":
                return LLMDocumentResult(detected_document_type="PRESCRIPTION", confidence=0.9, prescription={})
            return LLMDocumentResult(detected_document_type="HOSPITAL_BILL", confidence=0.9,
                                     bill={"patient_name": "Priya Singh", "line_items": [line("Consultation", 1000)]})

    live = lambda fid, t: {"file_id": fid, "file_name": f"{fid}.jpg", "file_type": t,  # noqa: E731
                           "content_base64": "aGVsbG8=", "mime_type": "image/jpeg"}
    d = await run(claim(documents=[live("rx", "PRESCRIPTION"), live("bill", "HOSPITAL_BILL")]), backend=Backend())
    assert d.status == "NEEDS_MEMBER_ACTION" and d.decision is None
    assert "rx.jpg" in d.member_message and any(a.action == "REUPLOAD_DOCUMENT" for a in d.required_actions)
