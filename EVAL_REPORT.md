# Eval Report — 12 assignment test cases

Generated 2026-09-26 17:12 UTC by `python scripts/generate_eval_report.py` · engine 2.0.0 · policy fingerprint `4677721d6656`

**Result: 12/12 cases pass every check.**

A case passes only if the decision (or the early stop for TC001–TC003), the approved amount, the confidence threshold, the structured `rejection_reasons`, and a concrete check for each `system_must` bullet all hold. TC011's confidence is compared with a real run of the same claim without the injected failure. The same checks run in CI as `tests/test_eval_cases.py`.

Documents are supplied as fixture extractions (`pre_extracted_data`), because the test cases describe document content rather than ship images. Every other stage is the production code path.

## Interpretation calls

Where the policy is ambiguous, the reading used is data in `data/interpretation.json` and is cited by the trace events that rely on it. The calls that decide test cases:

- **Per-claim ceiling (TC006, TC008, TC010).** Ceiling = max(`coverage.per_claim_limit`, category `sub_limit`), compared with the *eligible* amount. TC008: consultation ₹7,500 > ₹5,000 → REJECTED. TC006: dental ₹12,000 billed, ₹8,000 eligible ≤ ₹10,000 → PARTIAL. TC010: ₹4,500 ≤ ₹5,000. Treating the ₹2,000 consultation sub-limit as a hard cap would contradict TC010's ₹3,240, so sub-limits below the global limit are advisory warnings (`per_claim_limit`, `assumptions.sub_limit`).
- **Pre-authorisation before limits (TC007).** Pre-auth is evaluated per bill line first; the rejected MRI line leaves nothing eligible, so the reason is PRE_AUTH_MISSING rather than PER_CLAIM_EXCEEDED (`assumptions.per_item_pre_auth`).
- **Exclusion supersedes waiting period (TC012).** Obesity is both excluded and has a 365-day wait; the waiting period is logged SKIP, because telling the member they become eligible later would be false (`assumptions.exclusion_precedence`).
- **Which component fails (TC011).** The case does not name one; the default is FRAUD (non-critical), so the claim is still decided, the failure is visible, and confidence drops (`simulate_failure_component`).
- **Identity with no names on documents (TC007, TC009, TC011, TC012).** Not blocking; a PAYOUT-scoped confidence factor, so it lowers confidence on payouts but not on rejections (`confidence.identity_unverified`).

| Case | Name | Expected | Got | Approved | Confidence | Result |
|---|---|---|---|---|---|---|
| TC001 | Wrong Document Uploaded | stop (null)  | stopped (NEEDS_MEMBER_ACTION) | ₹0 | 0.99 | ✅ PASS |
| TC002 | Unreadable Document | stop (null)  | stopped (NEEDS_MEMBER_ACTION) | ₹0 | 0.95 | ✅ PASS |
| TC003 | Documents Belong to Different Patients | stop (null)  | stopped (NEEDS_MEMBER_ACTION) | ₹0 | 0.95 | ✅ PASS |
| TC004 | Clean Consultation — Full Approval | APPROVED / ₹1,350 | APPROVED | ₹1,350 | 0.95 | ✅ PASS |
| TC005 | Waiting Period — Diabetes | REJECTED  | REJECTED | ₹0 | 0.921 | ✅ PASS |
| TC006 | Dental Partial Approval — Cosmetic Exclusion | PARTIAL / ₹8,000 | PARTIAL | ₹8,000 | 0.902 | ✅ PASS |
| TC007 | MRI Without Pre-Authorization | REJECTED  | REJECTED | ₹0 | 0.95 | ✅ PASS |
| TC008 | Per-Claim Limit Exceeded | REJECTED  | REJECTED | ₹0 | 0.95 | ✅ PASS |
| TC009 | Fraud Signal — Multiple Same-Day Claims | MANUAL_REVIEW  | MANUAL_REVIEW | ₹0 | 0.727 | ✅ PASS |
| TC010 | Network Hospital — Discount Applied | APPROVED / ₹3,240 | APPROVED | ₹3,240 | 0.95 | ✅ PASS |
| TC011 | Component Failure — Graceful Degradation | APPROVED  | APPROVED | ₹4,000 | 0.565 | ✅ PASS |
| TC012 | Excluded Treatment | REJECTED  | REJECTED | ₹0 | 0.921 | ✅ PASS |

---

## TC001 — Wrong Document Uploaded

_Member submits two prescriptions for a consultation claim that requires a prescription and a hospital bill._

**Expected:** `{"decision": null, "system_must": ["Stop before making any claim decision", "Tell the member specifically what document type was uploaded and what is needed instead", "Not return a generic error — the message must name the uploaded document type and the required document type"]}`

**Decision:** none (stopped for member action) · status NEEDS_MEMBER_ACTION · approved ₹0 · confidence 0.99


**Member message:** We can't process this consultation claim yet because it is missing a hospital bill. A consultation claim needs a prescription and a hospital bill. You uploaded 2 documents: 'dr_sharma_prescription.jpg' (prescription) and 'another_prescription.jpg' (prescription). Please upload a hospital bill or clinic invoice (itemised charges, bill number and the total amount paid) for your treatment on 01 Nov 2024 in place of 'another_prescription.jpg' (prescription).

**Ops reason:** Stopped at DOC_GATE before adjudication: We can't process this consultation claim yet because it is missing a hospital bill. A consultation claim needs a prescription and a hospital bill. You uploaded 2 documents: 'dr_sharma_prescription.jpg' (prescription) and 'another_prescription.jpg' (prescription). Please upload a hospital bill or clinic invoice (itemised charges, bill number and the total amount paid) for your treatment on 01 Nov 2024 in place of 'another_prescription.jpg' (prescription).

**Confidence breakdown:** 0.99 (EXTRACTOR/base) = 0.99

**Checks:**

- ✅ stopped before a decision (status NEEDS_MEMBER_ACTION, decision null)
- ✅ message mentions prescription, hospital bill, dr_sharma_prescription.jpg, another_prescription.jpg
- ✅ required_actions has UPLOAD_DOCUMENT

<details><summary>Full trace (7 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC001 received: consultation claim for ₹1,500 with 2 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP001 (Rajesh Kumar) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP001 is covered under member EMP001 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP001 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | FAIL | BLOCK | We can't process this consultation claim yet because it is missing a hospital bill. A consultation claim needs a prescription and a hospital bill. You uploaded 2 documents: 'dr_sharma_prescription.jpg' (prescription) and 'another_prescription.jpg' (prescription). Please upload a hospital bill or clinic invoice (itemised charges, bill number and the total amount paid) for your treatment on 01 Nov 2024 in place of 'another_prescription.jpg' (prescription). | document_requirements.CONSULTATION.required |  |
| 7 | DECISION | final_decision | INFO | NONE | Stopped at DOC_GATE: member action required before any claim decision is made. No decision issued; the claim will continue once the member responds. |  |  |

</details>

---

## TC002 — Unreadable Document

_Member uploads a valid prescription but a blurry, unreadable photo of their pharmacy bill._

**Expected:** `{"decision": null, "system_must": ["Identify that the pharmacy bill cannot be read", "Ask the member to re-upload that specific document", "Not reject the claim outright"]}`

**Decision:** none (stopped for member action) · status NEEDS_MEMBER_ACTION · approved ₹0 · confidence 0.95


**Member message:** We need you to fix one document before we can assess this claim. We couldn't use 'blurry_bill.jpg', which you uploaded as your pharmacy bill — the image is too blurry or unclear to read. Please re-upload a clear photo or scan of this pharmacy bill: place it flat in good light, keep all four corners in frame, and make sure names, dates and amounts are legible (JPG, PNG or PDF). Your claim has not been rejected — it will continue as soon as the corrected documents are uploaded.

**Ops reason:** Stopped at DOC_VERIFY before adjudication: We need you to fix one document before we can assess this claim. We couldn't use 'blurry_bill.jpg', which you uploaded as your pharmacy bill — the image is too blurry or unclear to read. Please re-upload a clear photo or scan of this pharmacy bill: place it flat in good light, keep all four corners in frame, and make sure names, dates and amounts are legible (JPG, PNG or PDF). Your claim has not been rejected — it will continue as soon as the corrected documents are uploaded.

**Confidence breakdown:** 0.95 (EXTRACTOR/base) = 0.95

**Checks:**

- ✅ stopped before a decision (status NEEDS_MEMBER_ACTION, decision null)
- ✅ message mentions blurry_bill.jpg, pharmacy bill, re-upload
- ✅ required_actions has REUPLOAD_DOCUMENT for F004
- ✅ not rejected

<details><summary>Full trace (11 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC002 received: pharmacy claim for ₹800 with 2 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP004 (Sneha Reddy) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP004 is covered under member EMP004 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP004 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | PASS | NONE | All required document types present for pharmacy: prescription, pharmacy bill (as declared by the member; verified against detected types after extraction). | document_requirements.PHARMACY.required |  |
| 7 | EXTRACTOR | extraction | PASS | NONE | 'prescription.jpg' read as prescription (fixture data). |  |  |
| 8 | EXTRACTOR | readability | FAIL | NONE | 'blurry_bill.jpg' (pharmacy bill) is unreadable. |  |  |
| 9 | DOC_VERIFY | readability | FAIL | BLOCK | Required pharmacy bill 'blurry_bill.jpg' cannot be used (UNREADABLE); member asked to re-upload that file. |  |  |
| 10 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 11 | DECISION | final_decision | INFO | NONE | Stopped at DOC_VERIFY: member action required before any claim decision is made. No decision issued; the claim will continue once the member responds. |  |  |

</details>

---

## TC003 — Documents Belong to Different Patients

_The prescription is for Rajesh Kumar but the hospital bill is for a different patient, Arjun Mehta._

**Expected:** `{"decision": null, "system_must": ["Detect that the documents belong to different people", "Surface this to the member with the specific names found on each document", "Not proceed to a claim decision"]}`

**Decision:** none (stopped for member action) · status NEEDS_MEMBER_ACTION · approved ₹0 · confidence 0.95


**Member message:** The documents in this claim belong to different people: 'prescription_rajesh.jpg' (prescription) is for Rajesh Kumar; 'bill_arjun.jpg' (hospital bill) is for Arjun Mehta. This claim is for Rajesh Kumar, so every document must be in their name. Please replace 'bill_arjun.jpg' with the hospital bill issued to Rajesh Kumar. If Arjun Mehta is a covered family member, submit a separate claim for them.

**Ops reason:** Stopped at CONSISTENCY before adjudication: The documents in this claim belong to different people: 'prescription_rajesh.jpg' (prescription) is for Rajesh Kumar; 'bill_arjun.jpg' (hospital bill) is for Arjun Mehta. This claim is for Rajesh Kumar, so every document must be in their name. Please replace 'bill_arjun.jpg' with the hospital bill issued to Rajesh Kumar. If Arjun Mehta is a covered family member, submit a separate claim for them.

**Confidence breakdown:** 0.95 (EXTRACTOR/base) = 0.95

**Checks:**

- ✅ stopped before a decision (status NEEDS_MEMBER_ACTION, decision null)
- ✅ message mentions Rajesh Kumar, Arjun Mehta

<details><summary>Full trace (12 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC003 received: consultation claim for ₹1,500 with 2 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP001 (Rajesh Kumar) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP001 is covered under member EMP001 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP001 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | PASS | NONE | All required document types present for consultation: prescription, hospital bill (as declared by the member; verified against detected types after extraction). | document_requirements.CONSULTATION.required |  |
| 7 | EXTRACTOR | extraction | PASS | NONE | 'prescription_rajesh.jpg' read as prescription (fixture data). |  |  |
| 8 | EXTRACTOR | extraction | PASS | NONE | 'bill_arjun.jpg' read as hospital bill (fixture data). |  |  |
| 9 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 10 | DOC_VERIFY | doctor_registration | WARN | NONE | No doctor registration number could be read on 'prescription_rajesh.jpg'; prescriber is unverified. | confidence.missing_registration | 0.9 (PAYOUT) |
| 11 | CONSISTENCY | patient_names_across_documents | FAIL | BLOCK | The documents in this claim belong to different people: 'prescription_rajesh.jpg' (prescription) is for Rajesh Kumar; 'bill_arjun.jpg' (hospital bill) is for Arjun Mehta. This claim is for Rajesh Kumar, so every document must be in their name. Please replace 'bill_arjun.jpg' with the hospital bill issued to Rajesh Kumar. If Arjun Mehta is a covered family member, submit a separate claim for them. |  |  |
| 12 | DECISION | final_decision | INFO | NONE | Stopped at CONSISTENCY: member action required before any claim decision is made. No decision issued; the claim will continue once the member responds. |  |  |

</details>

---

## TC004 — Clean Consultation — Full Approval

_Complete, valid consultation claim with correct documents, valid member, covered treatment, within all limits._

**Expected:** `{"decision": "APPROVED", "approved_amount": 1350, "notes": "10% co-pay applied on consultation category (₹150 deducted)", "confidence_score": "above 0.85"}`

**Decision:** APPROVED · status DECIDED · approved ₹1,350 · confidence 0.95


**Member message:** Good news — your claim has been approved for ₹1,350 (you claimed ₹1,500). Eligible amount: ₹1,500 (of ₹1,500 billed). Co-pay 10% on ₹1,500: -₹150 = ₹1,350. Final payable: ₹1,350.

**Ops reason:** APPROVED ₹1,350 of ₹1,500 claimed. Eligible amount: ₹1,500 (of ₹1,500 billed). Co-pay 10% on ₹1,500: -₹150 = ₹1,350. Final payable: ₹1,350.

| Line item | Amount | Status | Reason |
|---|---|---|---|
| Consultation Fee | ₹1,000 | APPROVED |  |
| CBC Test | ₹300 | APPROVED |  |
| Dengue NS1 Test | ₹200 | APPROVED |  |

**Financial breakdown:** Eligible amount: ₹1,500 (of ₹1,500 billed). → Co-pay 10% on ₹1,500: -₹150 = ₹1,350. → Final payable: ₹1,350.

**Confidence breakdown:** 0.95 (EXTRACTOR/base) = 0.95

**Checks:**

- ✅ decision APPROVED
- ✅ approved_amount 1350
- ✅ confidence > 0.85
- ✅ co-pay of 150 shown
- ✅ message mentions ₹150

<details><summary>Full trace (46 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC004 received: consultation claim for ₹1,500 with 2 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP001 (Rajesh Kumar) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP001 is covered under member EMP001 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP001 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | PASS | NONE | All required document types present for consultation: prescription, hospital bill (as declared by the member; verified against detected types after extraction). | document_requirements.CONSULTATION.required |  |
| 7 | EXTRACTOR | extraction | PASS | NONE | 'F007' read as prescription (fixture data). |  |  |
| 8 | EXTRACTOR | extraction | PASS | NONE | 'F008' read as hospital bill (fixture data). |  |  |
| 9 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 10 | DOC_VERIFY | doctor_registration | PASS | NONE | Doctor registration 'KA/45678/2015' on 'F007' matches the STATE_MEDICAL_COUNCIL format. | registration_patterns |  |
| 11 | CONSISTENCY | patient_names_across_documents | PASS | NONE | Patient names agree across 2 documents (exact match). |  |  |
| 12 | CONSISTENCY | patient_name_vs_roster | PASS | NONE | Documents name the patient on record, Rajesh Kumar (exact match). |  |  |
| 13 | CONSISTENCY | document_dates | PASS | NONE | Document dates are consistent with the treatment date 01 Nov 2024. |  |  |
| 14 | CONSISTENCY | consistency | PASS | NONE | Documents are consistent with each other and the claim. |  |  |
| 15 | POLICY_ENGINE | category_covered | PASS | NONE | Consultation is a covered OPD category. | opd_categories.consultation.covered |  |
| 16 | POLICY_ENGINE | minimum_claim_amount | PASS | NONE | Claimed ₹1,500 meets the ₹500 minimum. | submission_rules.minimum_claim_amount |  |
| 17 | POLICY_ENGINE | submission_deadline | NOT_EVALUABLE | NONE | No submission date supplied, so the 30-day submission deadline could not be checked. | submission_rules.deadline_days_from_treatment |  |
| 18 | POLICY_ENGINE | policy_status | PASS | NONE | Policy PLUM_GHI_2024 is ACTIVE. | policy_holder.renewal_status |  |
| 19 | POLICY_ENGINE | policy_period | PASS | NONE | Treatment on 01 Nov 2024 falls within the policy period 01 Apr 2024 to 31 Mar 2025. | policy_holder.policy_start_date..policy_end_date |  |
| 20 | POLICY_ENGINE | member_coverage_date | PASS | NONE | Patient was covered on the treatment date (cover started 01 Apr 2024). | members[].join_date |  |
| 21 | POLICY_ENGINE | relationship_covered | PASS | NONE | Relationship SELF (policy term SELF) is covered. | coverage.family_floater.covered_relationships |  |
| 22 | POLICY_ENGINE | initial_waiting_period | PASS | NONE | Initial 30-day waiting period ended 01 May 2024, before treatment. | waiting_periods.initial_waiting_period_days |  |
| 23 | POLICY_ENGINE | pre_existing_conditions | NOT_EVALUABLE | NONE | Pre-existing condition waiting period not evaluated: distinguishing a pre-existing condition needs medical history this system does not receive (documented assumption). | waiting_periods.pre_existing_conditions_days |  |
| 24 | POLICY_ENGINE | exclusions | PASS | NONE | Diagnosis/treatment 'Viral Fever' matches none of the 10 excluded conditions. | exclusions.conditions |  |
| 25 | POLICY_ENGINE | condition_waiting_period | PASS | NONE | No condition with a specific waiting period (diabetes, hypertension, thyroid disorders, joint replacement, maternity, mental health, obesity treatment, hernia, cataract) was found in the diagnosis or medicines. | waiting_periods.specific_conditions |  |
| 26 | POLICY_ENGINE | billed_amount | INFO | NONE | 3 bill line(s) from 1 bill(s), totalling ₹1,500 (claimed ₹1,500). |  |  |
| 27 | POLICY_ENGINE | line_item | PASS | NONE | Line 'Consultation Fee' (₹1,000) is eligible. |  |  |
| 28 | POLICY_ENGINE | line_item | PASS | NONE | Line 'CBC Test' (₹300) is eligible. |  |  |
| 29 | POLICY_ENGINE | line_item | PASS | NONE | Line 'Dengue NS1 Test' (₹200) is eligible. |  |  |
| 30 | POLICY_ENGINE | per_claim_limit | PASS | NONE | Eligible amount ₹1,500 is within the per-claim ceiling of ₹5,000. | coverage.per_claim_limit |  |
| 31 | POLICY_ENGINE | network_discount | INFO | NONE | Provider 'City Clinic, Bengaluru' is not a network hospital; no network discount. | network_hospitals |  |
| 32 | POLICY_ENGINE | copay | PASS | ADJUST_AMOUNT | Co-pay 10% applied after the network discount: ₹150 deducted (₹1,500 -> ₹1,350). | opd_categories.consultation.copay_percent |  |
| 33 | POLICY_ENGINE | ytd_usage | INFO | NONE | Year-to-date OPD usage for EMP001: ₹5,000 (caller-supplied ₹5,000, recorded in this system ₹0; the larger is used). Remaining annual OPD limit: ₹45,000 of ₹50,000. | assumptions.annual_limits |  |
| 34 | POLICY_ENGINE | annual_limit | PASS | NONE | Payout ₹1,350 is within the remaining annual OPD limit (₹45,000). | coverage.annual_opd_limit |  |
| 35 | POLICY_ENGINE | annual_limit | PASS | NONE | Payout ₹1,350 is within the remaining family floater limit (₹1,45,000). | coverage.family_floater.combined_limit |  |
| 36 | POLICY_ENGINE | policy_summary | INFO | NONE | Policy evaluation: APPROVED, payable ₹1,350. 3 line(s) approved, 0 rejected. |  |  |
| 37 | FRAUD | claim_history | INFO | NONE | 0 earlier claim(s) on record for EMP001 (caller-supplied history merged with this system's records). | assumptions.fraud_history |  |
| 38 | FRAUD | same_day_frequency | PASS | NONE | 1 claim(s) on 01 Nov 2024, within the limit of 2. | fraud_thresholds.same_day_claims_limit |  |
| 39 | FRAUD | monthly_frequency | PASS | NONE | 1 claim(s) in the last 30 days, within the limit of 6. | fraud_thresholds.monthly_claims_limit |  |
| 40 | FRAUD | claim_value | PASS | NONE | Claimed ₹1,500 is below the high-value threshold of ₹25,000. | fraud_thresholds.high_value_claim_threshold |  |
| 41 | FRAUD | duplicate_claim | PASS | NONE | No earlier claim with the same date and amount. |  |  |
| 42 | FRAUD | duplicate_document | NOT_EVALUABLE | NONE | No file hashes or bill numbers available (fixture documents), so document reuse could not be checked. |  |  |
| 43 | FRAUD | document_integrity | PASS | NONE | No alteration or duplicate-stamp flags on any document. |  |  |
| 44 | FRAUD | claim_vs_bill | PASS | NONE | Claimed ₹1,500 is consistent with the ₹1,500 billed. |  |  |
| 45 | FRAUD | fraud_score | PASS | NONE | Fraud score 0.0 is below the manual-review threshold of 0.8; no signals. | fraud_thresholds.fraud_score_manual_review_threshold |  |
| 46 | DECISION | final_decision | INFO | NONE | FINAL: APPROVED, approved ₹1,350, confidence 0.95. APPROVED ₹1,350 of ₹1,500 claimed. Eligible amount: ₹1,500 (of ₹1,500 billed). Co-pay 10% on ₹1,500: -₹150 = ₹1,350. Final payable: ₹1,350. |  |  |

</details>

---

## TC005 — Waiting Period — Diabetes

_Member joined 2024-09-01. Claims for diabetes treatment on 2024-10-15, which is within the 90-day waiting period for diabetes._

**Expected:** `{"decision": "REJECTED", "rejection_reasons": ["WAITING_PERIOD"], "system_must": ["State the date from which the member will be eligible for diabetes-related claims"]}`

**Decision:** REJECTED · status DECIDED · approved ₹0 · confidence 0.921

**Rejection reasons:** WAITING_PERIOD  

**Member message:** We're sorry — your claim for ₹3,000 could not be approved. The diagnosis indicates diabetes (diabetes, type 2 diabetes), which has a 90-day waiting period from the start of cover (01 Sep 2024). Treatment on 15 Oct 2024 is inside it. You will be eligible for diabetes-related claims from 30 Nov 2024. If you believe this is wrong, you can reply with additional documents and our team will re-check it.

**Ops reason:** REJECTED (WAITING_PERIOD): The diagnosis indicates diabetes (diabetes, type 2 diabetes), which has a 90-day waiting period from the start of cover (01 Sep 2024). Treatment on 15 Oct 2024 is inside it. You will be eligible for diabetes-related claims from 30 Nov 2024.

| Line item | Amount | Status | Reason |
|---|---|---|---|
| Total billed on 'F010' (not itemised) | ₹3,000 | REJECTED | Whole claim rejected (WAITING_PERIOD). |

**Confidence breakdown:** 0.95 (EXTRACTOR/base) × 0.97 (POLICY_ENGINE/condition_waiting_period) = 0.921

**Checks:**

- ✅ decision REJECTED
- ✅ rejection_reasons contains WAITING_PERIOD
- ✅ message mentions 30 Nov 2024, diabetes

<details><summary>Full trace (37 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC005 received: consultation claim for ₹3,000 with 2 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP005 (Vikram Joshi) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP005 is covered under member EMP005 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP005 started 01 Sep 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | PASS | NONE | All required document types present for consultation: prescription, hospital bill (as declared by the member; verified against detected types after extraction). | document_requirements.CONSULTATION.required |  |
| 7 | EXTRACTOR | extraction | PASS | NONE | 'F009' read as prescription (fixture data). |  |  |
| 8 | EXTRACTOR | extraction | PASS | NONE | 'F010' read as hospital bill (fixture data). |  |  |
| 9 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 10 | DOC_VERIFY | doctor_registration | PASS | NONE | Doctor registration 'GJ/56789/2014' on 'F009' matches the STATE_MEDICAL_COUNCIL format. | registration_patterns |  |
| 11 | CONSISTENCY | patient_names_across_documents | PASS | NONE | Patient names agree across 2 documents (exact match). |  |  |
| 12 | CONSISTENCY | patient_name_vs_roster | PASS | NONE | Documents name the patient on record, Vikram Joshi (exact match). |  |  |
| 13 | CONSISTENCY | document_dates | PASS | NONE | Document dates are consistent with the treatment date 15 Oct 2024. |  |  |
| 14 | CONSISTENCY | consistency | PASS | NONE | Documents are consistent with each other and the claim. |  |  |
| 15 | POLICY_ENGINE | category_covered | PASS | NONE | Consultation is a covered OPD category. | opd_categories.consultation.covered |  |
| 16 | POLICY_ENGINE | minimum_claim_amount | PASS | NONE | Claimed ₹3,000 meets the ₹500 minimum. | submission_rules.minimum_claim_amount |  |
| 17 | POLICY_ENGINE | submission_deadline | NOT_EVALUABLE | NONE | No submission date supplied, so the 30-day submission deadline could not be checked. | submission_rules.deadline_days_from_treatment |  |
| 18 | POLICY_ENGINE | policy_status | PASS | NONE | Policy PLUM_GHI_2024 is ACTIVE. | policy_holder.renewal_status |  |
| 19 | POLICY_ENGINE | policy_period | PASS | NONE | Treatment on 15 Oct 2024 falls within the policy period 01 Apr 2024 to 31 Mar 2025. | policy_holder.policy_start_date..policy_end_date |  |
| 20 | POLICY_ENGINE | member_coverage_date | PASS | NONE | Patient was covered on the treatment date (cover started 01 Sep 2024). | members[].join_date |  |
| 21 | POLICY_ENGINE | relationship_covered | PASS | NONE | Relationship SELF (policy term SELF) is covered. | coverage.family_floater.covered_relationships |  |
| 22 | POLICY_ENGINE | initial_waiting_period | PASS | NONE | Initial 30-day waiting period ended 01 Oct 2024, before treatment. | waiting_periods.initial_waiting_period_days |  |
| 23 | POLICY_ENGINE | pre_existing_conditions | NOT_EVALUABLE | NONE | Pre-existing condition waiting period not evaluated: distinguishing a pre-existing condition needs medical history this system does not receive (documented assumption). | waiting_periods.pre_existing_conditions_days |  |
| 24 | POLICY_ENGINE | exclusions | PASS | NONE | Diagnosis/treatment 'Type 2 Diabetes Mellitus' matches none of the 10 excluded conditions. | exclusions.conditions |  |
| 25 | POLICY_ENGINE | condition_waiting_period | FAIL | REJECT | The diagnosis indicates diabetes (diabetes, type 2 diabetes), which has a 90-day waiting period from the start of cover (01 Sep 2024). Treatment on 15 Oct 2024 is inside it. You will be eligible for diabetes-related claims from 30 Nov 2024. | waiting_periods.specific_conditions.diabetes | 0.97 (ALL) |
| 26 | POLICY_ENGINE | billed_amount | INFO | NONE | 1 bill line(s) from 1 bill(s), totalling ₹3,000 (claimed ₹3,000). |  |  |
| 27 | POLICY_ENGINE | policy_summary | INFO | NONE | Policy evaluation: REJECTED, payable ₹0; rejection reasons WAITING_PERIOD. 0 line(s) approved, 1 rejected. |  |  |
| 28 | FRAUD | claim_history | INFO | NONE | 0 earlier claim(s) on record for EMP005 (caller-supplied history merged with this system's records). | assumptions.fraud_history |  |
| 29 | FRAUD | same_day_frequency | PASS | NONE | 1 claim(s) on 15 Oct 2024, within the limit of 2. | fraud_thresholds.same_day_claims_limit |  |
| 30 | FRAUD | monthly_frequency | PASS | NONE | 1 claim(s) in the last 30 days, within the limit of 6. | fraud_thresholds.monthly_claims_limit |  |
| 31 | FRAUD | claim_value | PASS | NONE | Claimed ₹3,000 is below the high-value threshold of ₹25,000. | fraud_thresholds.high_value_claim_threshold |  |
| 32 | FRAUD | duplicate_claim | PASS | NONE | No earlier claim with the same date and amount. |  |  |
| 33 | FRAUD | duplicate_document | NOT_EVALUABLE | NONE | No file hashes or bill numbers available (fixture documents), so document reuse could not be checked. |  |  |
| 34 | FRAUD | document_integrity | PASS | NONE | No alteration or duplicate-stamp flags on any document. |  |  |
| 35 | FRAUD | claim_vs_bill | PASS | NONE | Claimed ₹3,000 is consistent with the ₹3,000 billed. |  |  |
| 36 | FRAUD | fraud_score | PASS | NONE | Fraud score 0.0 is below the manual-review threshold of 0.8; no signals. | fraud_thresholds.fraud_score_manual_review_threshold |  |
| 37 | DECISION | final_decision | INFO | NONE | FINAL: REJECTED, approved ₹0, confidence 0.921. REJECTED (WAITING_PERIOD): The diagnosis indicates diabetes (diabetes, type 2 diabetes), which has a 90-day waiting period from the start of cover (01 Sep 2024). Treatment on 15 Oct 2024 is inside it. You will be eligible for diabetes-related claims from 30 Nov 2024. |  |  |

</details>

---

## TC006 — Dental Partial Approval — Cosmetic Exclusion

_Bill includes root canal treatment (covered) and teeth whitening (cosmetic, excluded). System must approve only the covered procedure._

**Expected:** `{"decision": "PARTIAL", "approved_amount": 8000, "system_must": ["Itemize which line items were approved and which were rejected", "State the reason for each rejection at the line-item level"]}`

**Decision:** PARTIAL · status DECIDED · approved ₹8,000 · confidence 0.902

**Rejection reasons:** EXCLUDED_PROCEDURE  

**Member message:** Your claim has been partly approved: ₹8,000 of the ₹12,000 you claimed. Covered: Root Canal Treatment (₹8,000). Not covered: Teeth Whitening (₹4,000) — 'Teeth Whitening' is an excluded dental procedure (Teeth Whitening) under the policy.  Eligible amount: ₹8,000 (of ₹12,000 billed). Final payable: ₹8,000.

**Ops reason:** PARTIAL: ₹8,000 of ₹12,000 approved. Approved items: Root Canal Treatment (₹8,000). Rejected items: Teeth Whitening (₹4,000) — 'Teeth Whitening' is an excluded dental procedure (Teeth Whitening) under the policy.  Eligible amount: ₹8,000 (of ₹12,000 billed). Final payable: ₹8,000.

| Line item | Amount | Status | Reason |
|---|---|---|---|
| Root Canal Treatment | ₹8,000 | APPROVED |  |
| Teeth Whitening | ₹4,000 | REJECTED | 'Teeth Whitening' is an excluded dental procedure (Teeth Whitening) under the policy. |

**Financial breakdown:** Eligible amount: ₹8,000 (of ₹12,000 billed). → Final payable: ₹8,000.

**Confidence breakdown:** 0.95 (EXTRACTOR/base) × 0.95 (POLICY_ENGINE/dental_report) = 0.902

**Checks:**

- ✅ decision PARTIAL
- ✅ approved_amount 8000
- ✅ root canal APPROVED
- ✅ whitening REJECTED with a line-level reason

<details><summary>Full trace (46 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC006 received: dental claim for ₹12,000 with 1 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP002 (Priya Singh) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP002 is covered under member EMP002 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP002 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | PASS | NONE | All required document types present for dental: hospital bill (as declared by the member; verified against detected types after extraction). | document_requirements.DENTAL.required |  |
| 7 | EXTRACTOR | extraction | PASS | NONE | 'F011' read as hospital bill (fixture data). |  |  |
| 8 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 9 | DOC_VERIFY | doctor_registration | NOT_EVALUABLE | NONE | No prescription in this claim, so there is no doctor registration number to validate. |  |  |
| 10 | CONSISTENCY | patient_name_vs_roster | PASS | NONE | Documents name the patient on record, Priya Singh (exact match). |  |  |
| 11 | CONSISTENCY | document_dates | NOT_EVALUABLE | NONE | No dates could be read on the documents. |  |  |
| 12 | CONSISTENCY | consistency | PASS | NONE | Documents are consistent with each other and the claim. |  |  |
| 13 | POLICY_ENGINE | category_covered | PASS | NONE | Dental is a covered OPD category. | opd_categories.dental.covered |  |
| 14 | POLICY_ENGINE | minimum_claim_amount | PASS | NONE | Claimed ₹12,000 meets the ₹500 minimum. | submission_rules.minimum_claim_amount |  |
| 15 | POLICY_ENGINE | submission_deadline | NOT_EVALUABLE | NONE | No submission date supplied, so the 30-day submission deadline could not be checked. | submission_rules.deadline_days_from_treatment |  |
| 16 | POLICY_ENGINE | policy_status | PASS | NONE | Policy PLUM_GHI_2024 is ACTIVE. | policy_holder.renewal_status |  |
| 17 | POLICY_ENGINE | policy_period | PASS | NONE | Treatment on 15 Oct 2024 falls within the policy period 01 Apr 2024 to 31 Mar 2025. | policy_holder.policy_start_date..policy_end_date |  |
| 18 | POLICY_ENGINE | member_coverage_date | PASS | NONE | Patient was covered on the treatment date (cover started 01 Apr 2024). | members[].join_date |  |
| 19 | POLICY_ENGINE | relationship_covered | PASS | NONE | Relationship SELF (policy term SELF) is covered. | coverage.family_floater.covered_relationships |  |
| 20 | POLICY_ENGINE | initial_waiting_period | PASS | NONE | Initial 30-day waiting period ended 01 May 2024, before treatment. | waiting_periods.initial_waiting_period_days |  |
| 21 | POLICY_ENGINE | pre_existing_conditions | NOT_EVALUABLE | NONE | Pre-existing condition waiting period not evaluated: distinguishing a pre-existing condition needs medical history this system does not receive (documented assumption). | waiting_periods.pre_existing_conditions_days |  |
| 22 | POLICY_ENGINE | diagnosis_present | NOT_EVALUABLE | NONE | No extracted prescription is available, so the diagnosis could not be checked. | opd_categories.dental.requires_prescription |  |
| 23 | POLICY_ENGINE | exclusions | NOT_EVALUABLE | NONE | No diagnosis or treatment could be read, so condition exclusions were checked on bill line items only. | exclusions.conditions |  |
| 24 | POLICY_ENGINE | condition_waiting_period | PASS | NONE | No diagnosis or medicines could be read; specific waiting periods could not be matched. | waiting_periods.specific_conditions |  |
| 25 | POLICY_ENGINE | dental_report | WARN | NONE | The policy expects a dental report, but none was uploaded (it is optional in document_requirements, so the claim is not blocked; procedures are taken from the bill). | opd_categories.dental.requires_dental_report | 0.95 (PAYOUT) |
| 26 | POLICY_ENGINE | billed_amount | INFO | NONE | 2 bill line(s) from 1 bill(s), totalling ₹12,000 (claimed ₹12,000). |  |  |
| 27 | POLICY_ENGINE | procedure_coverage | PASS | NONE | 'Root Canal Treatment' is a covered dental item (Root Canal Treatment). | opd_categories.dental.covered_procedures |  |
| 28 | POLICY_ENGINE | line_item | PASS | NONE | Line 'Root Canal Treatment' (₹8,000) is eligible. |  |  |
| 29 | POLICY_ENGINE | procedure_coverage | FAIL | REJECT | 'Teeth Whitening' is an excluded dental procedure (Teeth Whitening) under the policy. | opd_categories.dental.excluded_procedures |  |
| 30 | POLICY_ENGINE | per_claim_limit | PASS | NONE | Eligible amount ₹8,000 is within the per-claim ceiling of ₹10,000. | coverage.per_claim_limit |  |
| 31 | POLICY_ENGINE | network_discount | INFO | NONE | Provider 'Smile Dental Clinic' is not a network hospital; no network discount. | network_hospitals |  |
| 32 | POLICY_ENGINE | copay | PASS | NONE | No co-pay applies to dental (0%). | opd_categories.dental.copay_percent |  |
| 33 | POLICY_ENGINE | ytd_usage | INFO | NONE | Year-to-date OPD usage for EMP002: ₹0 (caller-supplied ₹0, recorded in this system ₹0; the larger is used). Remaining annual OPD limit: ₹50,000 of ₹50,000. | assumptions.annual_limits |  |
| 34 | POLICY_ENGINE | annual_limit | PASS | NONE | Payout ₹8,000 is within the remaining annual OPD limit (₹50,000). | coverage.annual_opd_limit |  |
| 35 | POLICY_ENGINE | annual_limit | PASS | NONE | Payout ₹8,000 is within the remaining family floater limit (₹1,50,000). | coverage.family_floater.combined_limit |  |
| 36 | POLICY_ENGINE | policy_summary | INFO | NONE | Policy evaluation: PARTIAL, payable ₹8,000; rejection reasons EXCLUDED_PROCEDURE. 1 line(s) approved, 1 rejected. |  |  |
| 37 | FRAUD | claim_history | INFO | NONE | 0 earlier claim(s) on record for EMP002 (caller-supplied history merged with this system's records). | assumptions.fraud_history |  |
| 38 | FRAUD | same_day_frequency | PASS | NONE | 1 claim(s) on 15 Oct 2024, within the limit of 2. | fraud_thresholds.same_day_claims_limit |  |
| 39 | FRAUD | monthly_frequency | PASS | NONE | 1 claim(s) in the last 30 days, within the limit of 6. | fraud_thresholds.monthly_claims_limit |  |
| 40 | FRAUD | claim_value | PASS | NONE | Claimed ₹12,000 is below the high-value threshold of ₹25,000. | fraud_thresholds.high_value_claim_threshold |  |
| 41 | FRAUD | duplicate_claim | PASS | NONE | No earlier claim with the same date and amount. |  |  |
| 42 | FRAUD | duplicate_document | NOT_EVALUABLE | NONE | No file hashes or bill numbers available (fixture documents), so document reuse could not be checked. |  |  |
| 43 | FRAUD | document_integrity | PASS | NONE | No alteration or duplicate-stamp flags on any document. |  |  |
| 44 | FRAUD | claim_vs_bill | PASS | NONE | Claimed ₹12,000 is consistent with the ₹12,000 billed. |  |  |
| 45 | FRAUD | fraud_score | PASS | NONE | Fraud score 0.0 is below the manual-review threshold of 0.8; no signals. | fraud_thresholds.fraud_score_manual_review_threshold |  |
| 46 | DECISION | final_decision | INFO | NONE | FINAL: PARTIAL, approved ₹8,000, confidence 0.902. PARTIAL: ₹8,000 of ₹12,000 approved. Approved items: Root Canal Treatment (₹8,000). Rejected items: Teeth Whitening (₹4,000) — 'Teeth Whitening' is an excluded dental procedure (Teeth Whitening) under the policy.  Eligible amount: ₹8,000 (of ₹12,000 billed). Final payable: ₹8,000. |  |  |

</details>

---

## TC007 — MRI Without Pre-Authorization

_MRI scan costing ₹15,000 submitted without pre-authorization. Policy requires pre-auth for MRI above ₹10,000._

**Expected:** `{"decision": "REJECTED", "rejection_reasons": ["PRE_AUTH_MISSING"], "system_must": ["Explain that pre-authorization was required and not obtained", "Tell the member what they should do to resubmit with pre-auth"]}`

**Decision:** REJECTED · status DECIDED · approved ₹0 · confidence 0.95

**Rejection reasons:** PRE_AUTH_MISSING  

**Member message:** We're sorry — your claim for ₹15,000 could not be approved. MRI Lumbar Spine (₹15,000): MRI above ₹10,000 requires pre-authorisation; none was obtained. Pre-authorisation must be approved by ICICI Lombard General Insurance BEFORE the MRI. To resubmit: ask your doctor or the diagnostic centre to raise a pre-authorisation request (with the prescription and clinical notes) through Plum or the insurer; once approved (valid for 30 days), submit this claim again with the pre-authorisation number in the 'Pre-auth ID' field. If the scan was an emergency, contact support for a retrospective review.

**Ops reason:** REJECTED (PRE_AUTH_MISSING): MRI Lumbar Spine (₹15,000): MRI above ₹10,000 requires pre-authorisation; none was obtained.

| Line item | Amount | Status | Reason |
|---|---|---|---|
| MRI Lumbar Spine | ₹15,000 | REJECTED | MRI above ₹10,000 requires pre-authorisation; none was obtained. |

**Confidence breakdown:** 0.95 (EXTRACTOR/base) = 0.95

**Checks:**

- ✅ decision REJECTED
- ✅ rejection_reasons contains PRE_AUTH_MISSING
- ✅ message mentions pre-authorisation, resubmit
- ✅ required_actions has OBTAIN_PRE_AUTH

<details><summary>Full trace (38 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC007 received: diagnostic claim for ₹15,000 with 3 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP007 (Suresh Patil) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP007 is covered under member EMP007 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP007 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | PASS | NONE | All required document types present for diagnostic: prescription, lab report, hospital bill (as declared by the member; verified against detected types after extraction). | document_requirements.DIAGNOSTIC.required |  |
| 7 | EXTRACTOR | extraction | PASS | NONE | 'F012' read as prescription (fixture data). |  |  |
| 8 | EXTRACTOR | extraction | PASS | NONE | 'F013' read as lab report (fixture data). |  |  |
| 9 | EXTRACTOR | extraction | PASS | NONE | 'F014' read as hospital bill (fixture data). |  |  |
| 10 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 11 | DOC_VERIFY | doctor_registration | PASS | NONE | Doctor registration 'AP/67890/2017' on 'F012' matches the STATE_MEDICAL_COUNCIL format. | registration_patterns |  |
| 12 | CONSISTENCY | identity_evidence | WARN | NONE | No patient name could be found on any document; identity rests on the member-entered ID only. | confidence.identity_unverified | 0.85 (PAYOUT) |
| 13 | CONSISTENCY | document_dates | NOT_EVALUABLE | NONE | No dates could be read on the documents. |  |  |
| 14 | CONSISTENCY | consistency | PASS | NONE | Documents are consistent with each other and the claim. |  |  |
| 15 | POLICY_ENGINE | category_covered | PASS | NONE | Diagnostic is a covered OPD category. | opd_categories.diagnostic.covered |  |
| 16 | POLICY_ENGINE | minimum_claim_amount | PASS | NONE | Claimed ₹15,000 meets the ₹500 minimum. | submission_rules.minimum_claim_amount |  |
| 17 | POLICY_ENGINE | submission_deadline | NOT_EVALUABLE | NONE | No submission date supplied, so the 30-day submission deadline could not be checked. | submission_rules.deadline_days_from_treatment |  |
| 18 | POLICY_ENGINE | policy_status | PASS | NONE | Policy PLUM_GHI_2024 is ACTIVE. | policy_holder.renewal_status |  |
| 19 | POLICY_ENGINE | policy_period | PASS | NONE | Treatment on 02 Nov 2024 falls within the policy period 01 Apr 2024 to 31 Mar 2025. | policy_holder.policy_start_date..policy_end_date |  |
| 20 | POLICY_ENGINE | member_coverage_date | PASS | NONE | Patient was covered on the treatment date (cover started 01 Apr 2024). | members[].join_date |  |
| 21 | POLICY_ENGINE | relationship_covered | PASS | NONE | Relationship SELF (policy term SELF) is covered. | coverage.family_floater.covered_relationships |  |
| 22 | POLICY_ENGINE | initial_waiting_period | PASS | NONE | Initial 30-day waiting period ended 01 May 2024, before treatment. | waiting_periods.initial_waiting_period_days |  |
| 23 | POLICY_ENGINE | pre_existing_conditions | NOT_EVALUABLE | NONE | Pre-existing condition waiting period not evaluated: distinguishing a pre-existing condition needs medical history this system does not receive (documented assumption). | waiting_periods.pre_existing_conditions_days |  |
| 24 | POLICY_ENGINE | exclusions | PASS | NONE | Diagnosis/treatment 'Suspected Lumbar Disc Herniation' matches none of the 10 excluded conditions. | exclusions.conditions |  |
| 25 | POLICY_ENGINE | condition_waiting_period | PASS | NONE | No condition with a specific waiting period (diabetes, hypertension, thyroid disorders, joint replacement, maternity, mental health, obesity treatment, hernia, cataract) was found in the diagnosis or medicines. | waiting_periods.specific_conditions |  |
| 26 | POLICY_ENGINE | billed_amount | INFO | NONE | 1 bill line(s) from 1 bill(s), totalling ₹15,000 (claimed ₹15,000). |  |  |
| 27 | POLICY_ENGINE | pre_authorization | FAIL | REJECT | 'MRI Lumbar Spine' (₹15,000) is a MRI; MRI above ₹10,000 requires pre-authorisation; none was obtained. | pre_authorization.required_for |  |
| 28 | POLICY_ENGINE | policy_summary | INFO | NONE | Policy evaluation: REJECTED, payable ₹0; rejection reasons PRE_AUTH_MISSING. 0 line(s) approved, 1 rejected. |  |  |
| 29 | FRAUD | claim_history | INFO | NONE | 0 earlier claim(s) on record for EMP007 (caller-supplied history merged with this system's records). | assumptions.fraud_history |  |
| 30 | FRAUD | same_day_frequency | PASS | NONE | 1 claim(s) on 02 Nov 2024, within the limit of 2. | fraud_thresholds.same_day_claims_limit |  |
| 31 | FRAUD | monthly_frequency | PASS | NONE | 1 claim(s) in the last 30 days, within the limit of 6. | fraud_thresholds.monthly_claims_limit |  |
| 32 | FRAUD | claim_value | PASS | NONE | Claimed ₹15,000 is below the high-value threshold of ₹25,000. | fraud_thresholds.high_value_claim_threshold |  |
| 33 | FRAUD | duplicate_claim | PASS | NONE | No earlier claim with the same date and amount. |  |  |
| 34 | FRAUD | duplicate_document | NOT_EVALUABLE | NONE | No file hashes or bill numbers available (fixture documents), so document reuse could not be checked. |  |  |
| 35 | FRAUD | document_integrity | PASS | NONE | No alteration or duplicate-stamp flags on any document. |  |  |
| 36 | FRAUD | claim_vs_bill | PASS | NONE | Claimed ₹15,000 is consistent with the ₹15,000 billed. |  |  |
| 37 | FRAUD | fraud_score | PASS | NONE | Fraud score 0.0 is below the manual-review threshold of 0.8; no signals. | fraud_thresholds.fraud_score_manual_review_threshold |  |
| 38 | DECISION | final_decision | INFO | NONE | FINAL: REJECTED, approved ₹0, confidence 0.95. REJECTED (PRE_AUTH_MISSING): MRI Lumbar Spine (₹15,000): MRI above ₹10,000 requires pre-authorisation; none was obtained. |  |  |

</details>

---

## TC008 — Per-Claim Limit Exceeded

_Claimed amount of ₹7,500 exceeds the per-claim limit of ₹5,000._

**Expected:** `{"decision": "REJECTED", "rejection_reasons": ["PER_CLAIM_EXCEEDED"], "system_must": ["State the per-claim limit and the claimed amount clearly in the rejection message"]}`

**Decision:** REJECTED · status DECIDED · approved ₹0 · confidence 0.95

**Rejection reasons:** PER_CLAIM_EXCEEDED  

**Member message:** We're sorry — your claim for ₹7,500 could not be approved. The claimed amount of ₹7,500 exceeds the per-claim limit of ₹5,000 for a single consultation claim. If you believe this is wrong, you can reply with additional documents and our team will re-check it.

**Ops reason:** REJECTED (PER_CLAIM_EXCEEDED): The claimed amount of ₹7,500 exceeds the per-claim limit of ₹5,000 for a single consultation claim.

| Line item | Amount | Status | Reason |
|---|---|---|---|
| Consultation Fee | ₹2,000 | REJECTED | Claim exceeds the per-claim limit. |
| Medicines | ₹5,500 | REJECTED | Claim exceeds the per-claim limit. |

**Confidence breakdown:** 0.95 (EXTRACTOR/base) = 0.95

**Checks:**

- ✅ decision REJECTED
- ✅ rejection_reasons contains PER_CLAIM_EXCEEDED
- ✅ message mentions ₹5,000, ₹7,500

<details><summary>Full trace (39 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC008 received: consultation claim for ₹7,500 with 2 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP003 (Amit Verma) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP003 is covered under member EMP003 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP003 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | PASS | NONE | All required document types present for consultation: prescription, hospital bill (as declared by the member; verified against detected types after extraction). | document_requirements.CONSULTATION.required |  |
| 7 | EXTRACTOR | extraction | PASS | NONE | 'F015' read as prescription (fixture data). |  |  |
| 8 | EXTRACTOR | extraction | PASS | NONE | 'F016' read as hospital bill (fixture data). |  |  |
| 9 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 10 | DOC_VERIFY | doctor_registration | PASS | NONE | Doctor registration 'DL/34567/2016' on 'F015' matches the STATE_MEDICAL_COUNCIL format. | registration_patterns |  |
| 11 | CONSISTENCY | identity_evidence | WARN | NONE | No patient name could be found on any document; identity rests on the member-entered ID only. | confidence.identity_unverified | 0.85 (PAYOUT) |
| 12 | CONSISTENCY | document_dates | NOT_EVALUABLE | NONE | No dates could be read on the documents. |  |  |
| 13 | CONSISTENCY | consistency | PASS | NONE | Documents are consistent with each other and the claim. |  |  |
| 14 | POLICY_ENGINE | category_covered | PASS | NONE | Consultation is a covered OPD category. | opd_categories.consultation.covered |  |
| 15 | POLICY_ENGINE | minimum_claim_amount | PASS | NONE | Claimed ₹7,500 meets the ₹500 minimum. | submission_rules.minimum_claim_amount |  |
| 16 | POLICY_ENGINE | submission_deadline | NOT_EVALUABLE | NONE | No submission date supplied, so the 30-day submission deadline could not be checked. | submission_rules.deadline_days_from_treatment |  |
| 17 | POLICY_ENGINE | policy_status | PASS | NONE | Policy PLUM_GHI_2024 is ACTIVE. | policy_holder.renewal_status |  |
| 18 | POLICY_ENGINE | policy_period | PASS | NONE | Treatment on 20 Oct 2024 falls within the policy period 01 Apr 2024 to 31 Mar 2025. | policy_holder.policy_start_date..policy_end_date |  |
| 19 | POLICY_ENGINE | member_coverage_date | PASS | NONE | Patient was covered on the treatment date (cover started 01 Apr 2024). | members[].join_date |  |
| 20 | POLICY_ENGINE | relationship_covered | PASS | NONE | Relationship SELF (policy term SELF) is covered. | coverage.family_floater.covered_relationships |  |
| 21 | POLICY_ENGINE | initial_waiting_period | PASS | NONE | Initial 30-day waiting period ended 01 May 2024, before treatment. | waiting_periods.initial_waiting_period_days |  |
| 22 | POLICY_ENGINE | pre_existing_conditions | NOT_EVALUABLE | NONE | Pre-existing condition waiting period not evaluated: distinguishing a pre-existing condition needs medical history this system does not receive (documented assumption). | waiting_periods.pre_existing_conditions_days |  |
| 23 | POLICY_ENGINE | exclusions | PASS | NONE | Diagnosis/treatment 'Gastroenteritis' matches none of the 10 excluded conditions. | exclusions.conditions |  |
| 24 | POLICY_ENGINE | condition_waiting_period | PASS | NONE | No condition with a specific waiting period (diabetes, hypertension, thyroid disorders, joint replacement, maternity, mental health, obesity treatment, hernia, cataract) was found in the diagnosis or medicines. | waiting_periods.specific_conditions |  |
| 25 | POLICY_ENGINE | billed_amount | INFO | NONE | 2 bill line(s) from 1 bill(s), totalling ₹7,500 (claimed ₹7,500). |  |  |
| 26 | POLICY_ENGINE | line_item | PASS | NONE | Line 'Consultation Fee' (₹2,000) is eligible. |  |  |
| 27 | POLICY_ENGINE | line_item | PASS | NONE | Line 'Medicines' (₹5,500) is eligible. |  |  |
| 28 | POLICY_ENGINE | per_claim_limit | FAIL | REJECT | The claimed amount of ₹7,500 exceeds the per-claim limit of ₹5,000 for a single consultation claim. | coverage.per_claim_limit |  |
| 29 | POLICY_ENGINE | policy_summary | INFO | NONE | Policy evaluation: REJECTED, payable ₹0; rejection reasons PER_CLAIM_EXCEEDED. 0 line(s) approved, 2 rejected. |  |  |
| 30 | FRAUD | claim_history | INFO | NONE | 0 earlier claim(s) on record for EMP003 (caller-supplied history merged with this system's records). | assumptions.fraud_history |  |
| 31 | FRAUD | same_day_frequency | PASS | NONE | 1 claim(s) on 20 Oct 2024, within the limit of 2. | fraud_thresholds.same_day_claims_limit |  |
| 32 | FRAUD | monthly_frequency | PASS | NONE | 1 claim(s) in the last 30 days, within the limit of 6. | fraud_thresholds.monthly_claims_limit |  |
| 33 | FRAUD | claim_value | PASS | NONE | Claimed ₹7,500 is below the high-value threshold of ₹25,000. | fraud_thresholds.high_value_claim_threshold |  |
| 34 | FRAUD | duplicate_claim | PASS | NONE | No earlier claim with the same date and amount. |  |  |
| 35 | FRAUD | duplicate_document | NOT_EVALUABLE | NONE | No file hashes or bill numbers available (fixture documents), so document reuse could not be checked. |  |  |
| 36 | FRAUD | document_integrity | PASS | NONE | No alteration or duplicate-stamp flags on any document. |  |  |
| 37 | FRAUD | claim_vs_bill | PASS | NONE | Claimed ₹7,500 is consistent with the ₹7,500 billed. |  |  |
| 38 | FRAUD | fraud_score | PASS | NONE | Fraud score 0.0 is below the manual-review threshold of 0.8; no signals. | fraud_thresholds.fraud_score_manual_review_threshold |  |
| 39 | DECISION | final_decision | INFO | NONE | FINAL: REJECTED, approved ₹0, confidence 0.95. REJECTED (PER_CLAIM_EXCEEDED): The claimed amount of ₹7,500 exceeds the per-claim limit of ₹5,000 for a single consultation claim. |  |  |

</details>

---

## TC009 — Fraud Signal — Multiple Same-Day Claims

_Member EMP008 has already submitted 3 claims today before this one arrives. This is the 4th claim from the same member on the same day._

**Expected:** `{"decision": "MANUAL_REVIEW", "system_must": ["Flag the unusual same-day claim pattern", "Route to manual review rather than auto-rejecting", "Include the specific signals that triggered the flag in the output"]}`

**Decision:** MANUAL_REVIEW · status DECIDED · approved ₹0 · provisional ₹4,320 · confidence 0.727

**Review reasons:** FRAUD_SIGNALS  
**Fraud signals:** SAME_DAY_LIMIT_EXCEEDED: This is claim #4 for 30 Oct 2024 (limit 2 per day); earlier same-day claims: CLM_0081 ₹1,200, CLM_0082 ₹1,800, CLM_0083 ₹2,100 at City Clinic A, City Clinic B, Wellness Center.  

**Member message:** Your claim has been received. It needs a closer look by our claims team before a final decision. We'll contact you if anything else is needed; you don't need to do anything right now.

**Ops reason:** MANUAL_REVIEW (FRAUD_SIGNALS): Fraud signals: SAME_DAY_LIMIT_EXCEEDED: This is claim #4 for 30 Oct 2024 (limit 2 per day); earlier same-day claims: CLM_0081 ₹1,200, CLM_0082 ₹1,800, CLM_0083 ₹2,100 at City Clinic A, City Clinic B, Wellness Center. (score 0.6). Provisional payable amount if cleared: ₹4,320.

| Line item | Amount | Status | Reason |
|---|---|---|---|
| Total billed on 'F018' (not itemised) | ₹4,800 | APPROVED |  |

**Financial breakdown:** Eligible amount: ₹4,800 (of ₹4,800 billed). → Co-pay 10% on ₹4,800: -₹480 = ₹4,320. → Final payable: ₹4,320.

**Confidence breakdown:** 0.95 (EXTRACTOR/base) × 0.9 (DOC_VERIFY/doctor_registration) × 0.85 (CONSISTENCY/identity_evidence) = 0.727

**Checks:**

- ✅ decision MANUAL_REVIEW
- ✅ SAME_DAY_LIMIT_EXCEEDED signal present
- ✅ signals name the earlier claims
- ✅ not auto-rejected

<details><summary>Full trace (44 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC009 received: consultation claim for ₹4,800 with 2 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP008 (Ravi Menon) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP008 is covered under member EMP008 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP008 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | PASS | NONE | All required document types present for consultation: prescription, hospital bill (as declared by the member; verified against detected types after extraction). | document_requirements.CONSULTATION.required |  |
| 7 | EXTRACTOR | extraction | PASS | NONE | 'F017' read as prescription (fixture data). |  |  |
| 8 | EXTRACTOR | extraction | PASS | NONE | 'F018' read as hospital bill (fixture data). |  |  |
| 9 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 10 | DOC_VERIFY | doctor_registration | WARN | NONE | No doctor registration number could be read on 'F017'; prescriber is unverified. | confidence.missing_registration | 0.9 (PAYOUT) |
| 11 | CONSISTENCY | identity_evidence | WARN | NONE | No patient name could be found on any document; identity rests on the member-entered ID only. | confidence.identity_unverified | 0.85 (PAYOUT) |
| 12 | CONSISTENCY | document_dates | NOT_EVALUABLE | NONE | No dates could be read on the documents. |  |  |
| 13 | CONSISTENCY | consistency | PASS | NONE | Documents are consistent with each other and the claim. |  |  |
| 14 | POLICY_ENGINE | category_covered | PASS | NONE | Consultation is a covered OPD category. | opd_categories.consultation.covered |  |
| 15 | POLICY_ENGINE | minimum_claim_amount | PASS | NONE | Claimed ₹4,800 meets the ₹500 minimum. | submission_rules.minimum_claim_amount |  |
| 16 | POLICY_ENGINE | submission_deadline | NOT_EVALUABLE | NONE | No submission date supplied, so the 30-day submission deadline could not be checked. | submission_rules.deadline_days_from_treatment |  |
| 17 | POLICY_ENGINE | policy_status | PASS | NONE | Policy PLUM_GHI_2024 is ACTIVE. | policy_holder.renewal_status |  |
| 18 | POLICY_ENGINE | policy_period | PASS | NONE | Treatment on 30 Oct 2024 falls within the policy period 01 Apr 2024 to 31 Mar 2025. | policy_holder.policy_start_date..policy_end_date |  |
| 19 | POLICY_ENGINE | member_coverage_date | PASS | NONE | Patient was covered on the treatment date (cover started 01 Apr 2024). | members[].join_date |  |
| 20 | POLICY_ENGINE | relationship_covered | PASS | NONE | Relationship SELF (policy term SELF) is covered. | coverage.family_floater.covered_relationships |  |
| 21 | POLICY_ENGINE | initial_waiting_period | PASS | NONE | Initial 30-day waiting period ended 01 May 2024, before treatment. | waiting_periods.initial_waiting_period_days |  |
| 22 | POLICY_ENGINE | pre_existing_conditions | NOT_EVALUABLE | NONE | Pre-existing condition waiting period not evaluated: distinguishing a pre-existing condition needs medical history this system does not receive (documented assumption). | waiting_periods.pre_existing_conditions_days |  |
| 23 | POLICY_ENGINE | exclusions | PASS | NONE | Diagnosis/treatment 'Migraine' matches none of the 10 excluded conditions. | exclusions.conditions |  |
| 24 | POLICY_ENGINE | condition_waiting_period | PASS | NONE | No condition with a specific waiting period (diabetes, hypertension, thyroid disorders, joint replacement, maternity, mental health, obesity treatment, hernia, cataract) was found in the diagnosis or medicines. | waiting_periods.specific_conditions |  |
| 25 | POLICY_ENGINE | billed_amount | INFO | NONE | 1 bill line(s) from 1 bill(s), totalling ₹4,800 (claimed ₹4,800). |  |  |
| 26 | POLICY_ENGINE | line_item | PASS | NONE | Line 'Total billed on 'F018' (not itemised)' (₹4,800) is eligible. |  |  |
| 27 | POLICY_ENGINE | per_claim_limit | PASS | NONE | Eligible amount ₹4,800 is within the per-claim ceiling of ₹5,000. | coverage.per_claim_limit |  |
| 28 | POLICY_ENGINE | category_sub_limit | WARN | NONE | Eligible amount ₹4,800 is above the consultation sub_limit of ₹2,000; treated as advisory per the documented interpretation (see interpretation.assumptions.sub_limit). | opd_categories.consultation.sub_limit |  |
| 29 | POLICY_ENGINE | network_discount | INFO | NONE | Provider 'unknown' is not a network hospital; no network discount. | network_hospitals |  |
| 30 | POLICY_ENGINE | copay | PASS | ADJUST_AMOUNT | Co-pay 10% applied after the network discount: ₹480 deducted (₹4,800 -> ₹4,320). | opd_categories.consultation.copay_percent |  |
| 31 | POLICY_ENGINE | ytd_usage | INFO | NONE | Year-to-date OPD usage for EMP008: ₹0 (caller-supplied ₹0, recorded in this system ₹0; the larger is used). Remaining annual OPD limit: ₹50,000 of ₹50,000. | assumptions.annual_limits |  |
| 32 | POLICY_ENGINE | annual_limit | PASS | NONE | Payout ₹4,320 is within the remaining annual OPD limit (₹50,000). | coverage.annual_opd_limit |  |
| 33 | POLICY_ENGINE | annual_limit | PASS | NONE | Payout ₹4,320 is within the remaining family floater limit (₹1,50,000). | coverage.family_floater.combined_limit |  |
| 34 | POLICY_ENGINE | policy_summary | INFO | NONE | Policy evaluation: APPROVED, payable ₹4,320. 1 line(s) approved, 0 rejected. |  |  |
| 35 | FRAUD | claim_history | INFO | NONE | 3 earlier claim(s) on record for EMP008 (caller-supplied history merged with this system's records). | assumptions.fraud_history |  |
| 36 | FRAUD | same_day_limit_exceeded | FAIL | ESCALATE | Fraud signal SAME_DAY_LIMIT_EXCEEDED (hard, weight 0.6): This is claim #4 for 30 Oct 2024 (limit 2 per day); earlier same-day claims: CLM_0081 ₹1,200, CLM_0082 ₹1,800, CLM_0083 ₹2,100 at City Clinic A, City Clinic B, Wellness Center. | fraud_thresholds.same_day_claims_limit |  |
| 37 | FRAUD | monthly_frequency | PASS | NONE | 4 claim(s) in the last 30 days, within the limit of 6. | fraud_thresholds.monthly_claims_limit |  |
| 38 | FRAUD | claim_value | PASS | NONE | Claimed ₹4,800 is below the high-value threshold of ₹25,000. | fraud_thresholds.high_value_claim_threshold |  |
| 39 | FRAUD | duplicate_claim | PASS | NONE | No earlier claim with the same date and amount. |  |  |
| 40 | FRAUD | duplicate_document | NOT_EVALUABLE | NONE | No file hashes or bill numbers available (fixture documents), so document reuse could not be checked. |  |  |
| 41 | FRAUD | document_integrity | PASS | NONE | No alteration or duplicate-stamp flags on any document. |  |  |
| 42 | FRAUD | claim_vs_bill | PASS | NONE | Claimed ₹4,800 is consistent with the ₹4,800 billed. |  |  |
| 43 | FRAUD | fraud_score | FAIL | ESCALATE | Fraud score 0.6 with 1 signal(s); escalating to manual review (hard signal(s) SAME_DAY_LIMIT_EXCEEDED). | fraud_thresholds.fraud_score_manual_review_threshold |  |
| 44 | DECISION | final_decision | INFO | NONE | FINAL: MANUAL_REVIEW, approved ₹0 (provisional ₹4,320 held for review), confidence 0.727. MANUAL_REVIEW (FRAUD_SIGNALS): Fraud signals: SAME_DAY_LIMIT_EXCEEDED: This is claim #4 for 30 Oct 2024 (limit 2 per day); earlier same-day claims: CLM_0081 ₹1,200, CLM_0082 ₹1,800, CLM_0083 ₹2,100 at City Clinic A, City Clinic B, Wellness Center. (score 0.6). Provisional payable amount if cleared: ₹4,320. |  |  |

</details>

---

## TC010 — Network Hospital — Discount Applied

_Valid claim at Apollo Hospitals, a network hospital. Network discount must be applied before co-pay._

**Expected:** `{"decision": "APPROVED", "approved_amount": 3240, "notes": "Network discount (20%) applied first on ₹4,500 = ₹3,600. Co-pay (10%) applied on ₹3,600 = ₹360 deducted. Final: ₹3,240.", "system_must": ["Apply network discount before co-pay, not after", "Show the breakdown of discount and co-pay in the decision output"]}`

**Decision:** APPROVED · status DECIDED · approved ₹3,240 · confidence 0.95


**Member message:** Good news — your claim has been approved for ₹3,240 (you claimed ₹4,500). Eligible amount: ₹4,500 (of ₹4,500 billed). Network discount 20% (Apollo Hospitals): ₹4,500 - ₹900 = ₹3,600. Co-pay 10% on ₹3,600: -₹360 = ₹3,240. Final payable: ₹3,240.

**Ops reason:** APPROVED ₹3,240 of ₹4,500 claimed. Eligible amount: ₹4,500 (of ₹4,500 billed). Network discount 20% (Apollo Hospitals): ₹4,500 - ₹900 = ₹3,600. Co-pay 10% on ₹3,600: -₹360 = ₹3,240. Final payable: ₹3,240.

| Line item | Amount | Status | Reason |
|---|---|---|---|
| Consultation Fee | ₹1,500 | APPROVED |  |
| Medicines | ₹3,000 | APPROVED |  |

**Financial breakdown:** Eligible amount: ₹4,500 (of ₹4,500 billed). → Network discount 20% (Apollo Hospitals): ₹4,500 - ₹900 = ₹3,600. → Co-pay 10% on ₹3,600: -₹360 = ₹3,240. → Final payable: ₹3,240.

**Confidence breakdown:** 0.95 (EXTRACTOR/base) = 0.95

**Checks:**

- ✅ decision APPROVED
- ✅ approved_amount 3240
- ✅ network discount 900
- ✅ co-pay 360
- ✅ discount applied before co-pay (breakdown order)

<details><summary>Full trace (47 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC010 received: consultation claim for ₹4,500 with 2 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP010 (Deepak Shah) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP010 is covered under member EMP010 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP010 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | PASS | NONE | All required document types present for consultation: prescription, hospital bill (as declared by the member; verified against detected types after extraction). | document_requirements.CONSULTATION.required |  |
| 7 | EXTRACTOR | extraction | PASS | NONE | 'F019' read as prescription (fixture data). |  |  |
| 8 | EXTRACTOR | extraction | PASS | NONE | 'F020' read as hospital bill (fixture data). |  |  |
| 9 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 10 | DOC_VERIFY | doctor_registration | PASS | NONE | Doctor registration 'TN/56789/2013' on 'F019' matches the STATE_MEDICAL_COUNCIL format. | registration_patterns |  |
| 11 | CONSISTENCY | patient_names_across_documents | PASS | NONE | Patient names agree across 2 documents (exact match). |  |  |
| 12 | CONSISTENCY | patient_name_vs_roster | PASS | NONE | Documents name the patient on record, Deepak Shah (exact match). |  |  |
| 13 | CONSISTENCY | document_dates | NOT_EVALUABLE | NONE | No dates could be read on the documents. |  |  |
| 14 | CONSISTENCY | consistency | PASS | NONE | Documents are consistent with each other and the claim. |  |  |
| 15 | POLICY_ENGINE | category_covered | PASS | NONE | Consultation is a covered OPD category. | opd_categories.consultation.covered |  |
| 16 | POLICY_ENGINE | minimum_claim_amount | PASS | NONE | Claimed ₹4,500 meets the ₹500 minimum. | submission_rules.minimum_claim_amount |  |
| 17 | POLICY_ENGINE | submission_deadline | NOT_EVALUABLE | NONE | No submission date supplied, so the 30-day submission deadline could not be checked. | submission_rules.deadline_days_from_treatment |  |
| 18 | POLICY_ENGINE | policy_status | PASS | NONE | Policy PLUM_GHI_2024 is ACTIVE. | policy_holder.renewal_status |  |
| 19 | POLICY_ENGINE | policy_period | PASS | NONE | Treatment on 03 Nov 2024 falls within the policy period 01 Apr 2024 to 31 Mar 2025. | policy_holder.policy_start_date..policy_end_date |  |
| 20 | POLICY_ENGINE | member_coverage_date | PASS | NONE | Patient was covered on the treatment date (cover started 01 Apr 2024). | members[].join_date |  |
| 21 | POLICY_ENGINE | relationship_covered | PASS | NONE | Relationship SELF (policy term SELF) is covered. | coverage.family_floater.covered_relationships |  |
| 22 | POLICY_ENGINE | initial_waiting_period | PASS | NONE | Initial 30-day waiting period ended 01 May 2024, before treatment. | waiting_periods.initial_waiting_period_days |  |
| 23 | POLICY_ENGINE | pre_existing_conditions | NOT_EVALUABLE | NONE | Pre-existing condition waiting period not evaluated: distinguishing a pre-existing condition needs medical history this system does not receive (documented assumption). | waiting_periods.pre_existing_conditions_days |  |
| 24 | POLICY_ENGINE | exclusions | PASS | NONE | Diagnosis/treatment 'Acute Bronchitis' matches none of the 10 excluded conditions. | exclusions.conditions |  |
| 25 | POLICY_ENGINE | condition_waiting_period | PASS | NONE | No condition with a specific waiting period (diabetes, hypertension, thyroid disorders, joint replacement, maternity, mental health, obesity treatment, hernia, cataract) was found in the diagnosis or medicines. | waiting_periods.specific_conditions |  |
| 26 | POLICY_ENGINE | billed_amount | INFO | NONE | 2 bill line(s) from 1 bill(s), totalling ₹4,500 (claimed ₹4,500). |  |  |
| 27 | POLICY_ENGINE | line_item | PASS | NONE | Line 'Consultation Fee' (₹1,500) is eligible. |  |  |
| 28 | POLICY_ENGINE | line_item | PASS | NONE | Line 'Medicines' (₹3,000) is eligible. |  |  |
| 29 | POLICY_ENGINE | per_claim_limit | PASS | NONE | Eligible amount ₹4,500 is within the per-claim ceiling of ₹5,000. | coverage.per_claim_limit |  |
| 30 | POLICY_ENGINE | category_sub_limit | WARN | NONE | Eligible amount ₹4,500 is above the consultation sub_limit of ₹2,000; treated as advisory per the documented interpretation (see interpretation.assumptions.sub_limit). | opd_categories.consultation.sub_limit |  |
| 31 | POLICY_ENGINE | network_discount | PASS | ADJUST_AMOUNT | Apollo Hospitals is a network hospital: 20% network discount applied first. | opd_categories.consultation.network_discount_percent |  |
| 32 | POLICY_ENGINE | network_discount_amount | INFO | ADJUST_AMOUNT | Network discount: ₹4,500 -> ₹3,600. | opd_categories.consultation.network_discount_percent |  |
| 33 | POLICY_ENGINE | copay | PASS | ADJUST_AMOUNT | Co-pay 10% applied after the network discount: ₹360 deducted (₹3,600 -> ₹3,240). | opd_categories.consultation.copay_percent |  |
| 34 | POLICY_ENGINE | ytd_usage | INFO | NONE | Year-to-date OPD usage for EMP010: ₹8,000 (caller-supplied ₹8,000, recorded in this system ₹0; the larger is used). Remaining annual OPD limit: ₹42,000 of ₹50,000. | assumptions.annual_limits |  |
| 35 | POLICY_ENGINE | annual_limit | PASS | NONE | Payout ₹3,240 is within the remaining annual OPD limit (₹42,000). | coverage.annual_opd_limit |  |
| 36 | POLICY_ENGINE | annual_limit | PASS | NONE | Payout ₹3,240 is within the remaining family floater limit (₹1,42,000). | coverage.family_floater.combined_limit |  |
| 37 | POLICY_ENGINE | policy_summary | INFO | NONE | Policy evaluation: APPROVED, payable ₹3,240. 2 line(s) approved, 0 rejected. |  |  |
| 38 | FRAUD | claim_history | INFO | NONE | 0 earlier claim(s) on record for EMP010 (caller-supplied history merged with this system's records). | assumptions.fraud_history |  |
| 39 | FRAUD | same_day_frequency | PASS | NONE | 1 claim(s) on 03 Nov 2024, within the limit of 2. | fraud_thresholds.same_day_claims_limit |  |
| 40 | FRAUD | monthly_frequency | PASS | NONE | 1 claim(s) in the last 30 days, within the limit of 6. | fraud_thresholds.monthly_claims_limit |  |
| 41 | FRAUD | claim_value | PASS | NONE | Claimed ₹4,500 is below the high-value threshold of ₹25,000. | fraud_thresholds.high_value_claim_threshold |  |
| 42 | FRAUD | duplicate_claim | PASS | NONE | No earlier claim with the same date and amount. |  |  |
| 43 | FRAUD | duplicate_document | NOT_EVALUABLE | NONE | No file hashes or bill numbers available (fixture documents), so document reuse could not be checked. |  |  |
| 44 | FRAUD | document_integrity | PASS | NONE | No alteration or duplicate-stamp flags on any document. |  |  |
| 45 | FRAUD | claim_vs_bill | PASS | NONE | Claimed ₹4,500 is consistent with the ₹4,500 billed. |  |  |
| 46 | FRAUD | fraud_score | PASS | NONE | Fraud score 0.0 is below the manual-review threshold of 0.8; no signals. | fraud_thresholds.fraud_score_manual_review_threshold |  |
| 47 | DECISION | final_decision | INFO | NONE | FINAL: APPROVED, approved ₹3,240, confidence 0.95. APPROVED ₹3,240 of ₹4,500 claimed. Eligible amount: ₹4,500 (of ₹4,500 billed). Network discount 20% (Apollo Hospitals): ₹4,500 - ₹900 = ₹3,600. Co-pay 10% on ₹3,600: -₹360 = ₹3,240. Final payable: ₹3,240. |  |  |

</details>

---

## TC011 — Component Failure — Graceful Degradation

_One component of your system fails mid-processing (simulate with the flag below). The overall pipeline must continue, produce a decision, and make the failure visible in the output with an appropriately reduced confidence score._

**Expected:** `{"decision": "APPROVED", "system_must": ["Not crash or return a 500 error", "Indicate in the output that a component failed and was skipped", "Return a confidence score lower than a normal full-pipeline approval", "Include a note that manual review is recommended due to incomplete processing"]}`

**Decision:** APPROVED · status DECIDED · approved ₹4,000 · confidence 0.565

**Degraded components:** FRAUD  

**Member message:** Good news — your claim has been approved for ₹4,000 (you claimed ₹4,000). Eligible amount: ₹4,000 (of ₹4,000 billed). Final payable: ₹4,000. Some automated checks could not run, so our team may take a quick second look; no action is needed from you.

**Ops reason:** APPROVED ₹4,000 of ₹4,000 claimed. Eligible amount: ₹4,000 (of ₹4,000 billed). Final payable: ₹4,000. Degraded components: FRAUD (failed and skipped); manual review is recommended due to incomplete processing.

| Line item | Amount | Status | Reason |
|---|---|---|---|
| Panchakarma Therapy (5 sessions) | ₹3,000 | APPROVED |  |
| Consultation | ₹1,000 | APPROVED |  |

**Financial breakdown:** Eligible amount: ₹4,000 (of ₹4,000 billed). → Final payable: ₹4,000.

**Confidence breakdown:** 0.95 (EXTRACTOR/base) × 0.85 (CONSISTENCY/identity_evidence) × 0.7 (FRAUD/stage_failure) = 0.565

Baseline without the injected failure: APPROVED at confidence 0.807.

**Checks:**

- ✅ decision APPROVED
- ✅ failed component visible
- ✅ manual review recommended
- ✅ message mentions manual review
- ✅ confidence below the full-pipeline baseline

<details><summary>Full trace (40 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC011 received: alternative medicine claim for ₹4,000 with 2 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP006 (Kavita Nair) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP006 is covered under member EMP006 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP006 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | FAULT_INJECTION |  | INFO | NONE | Fault injection armed: FRAUD will be forced to fail. |  |  |
| 7 | DOC_GATE | required_documents | PASS | NONE | All required document types present for alternative medicine: prescription, hospital bill (as declared by the member; verified against detected types after extraction). | document_requirements.ALTERNATIVE_MEDICINE.required |  |
| 8 | EXTRACTOR | extraction | PASS | NONE | 'F021' read as prescription (fixture data). |  |  |
| 9 | EXTRACTOR | extraction | PASS | NONE | 'F022' read as hospital bill (fixture data). |  |  |
| 10 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 11 | DOC_VERIFY | doctor_registration | PASS | NONE | Doctor registration 'AYUR/KL/2345/2019' on 'F021' matches the AYUSH format. | registration_patterns |  |
| 12 | CONSISTENCY | identity_evidence | WARN | NONE | No patient name could be found on any document; identity rests on the member-entered ID only. | confidence.identity_unverified | 0.85 (PAYOUT) |
| 13 | CONSISTENCY | document_dates | NOT_EVALUABLE | NONE | No dates could be read on the documents. |  |  |
| 14 | CONSISTENCY | consistency | PASS | NONE | Documents are consistent with each other and the claim. |  |  |
| 15 | POLICY_ENGINE | category_covered | PASS | NONE | Alternative medicine is a covered OPD category. | opd_categories.alternative_medicine.covered |  |
| 16 | POLICY_ENGINE | minimum_claim_amount | PASS | NONE | Claimed ₹4,000 meets the ₹500 minimum. | submission_rules.minimum_claim_amount |  |
| 17 | POLICY_ENGINE | submission_deadline | NOT_EVALUABLE | NONE | No submission date supplied, so the 30-day submission deadline could not be checked. | submission_rules.deadline_days_from_treatment |  |
| 18 | POLICY_ENGINE | policy_status | PASS | NONE | Policy PLUM_GHI_2024 is ACTIVE. | policy_holder.renewal_status |  |
| 19 | POLICY_ENGINE | policy_period | PASS | NONE | Treatment on 28 Oct 2024 falls within the policy period 01 Apr 2024 to 31 Mar 2025. | policy_holder.policy_start_date..policy_end_date |  |
| 20 | POLICY_ENGINE | member_coverage_date | PASS | NONE | Patient was covered on the treatment date (cover started 01 Apr 2024). | members[].join_date |  |
| 21 | POLICY_ENGINE | relationship_covered | PASS | NONE | Relationship SELF (policy term SELF) is covered. | coverage.family_floater.covered_relationships |  |
| 22 | POLICY_ENGINE | initial_waiting_period | PASS | NONE | Initial 30-day waiting period ended 01 May 2024, before treatment. | waiting_periods.initial_waiting_period_days |  |
| 23 | POLICY_ENGINE | pre_existing_conditions | NOT_EVALUABLE | NONE | Pre-existing condition waiting period not evaluated: distinguishing a pre-existing condition needs medical history this system does not receive (documented assumption). | waiting_periods.pre_existing_conditions_days |  |
| 24 | POLICY_ENGINE | exclusions | PASS | NONE | Diagnosis/treatment 'Chronic Joint Pain ; Panchakarma Therapy' matches none of the 10 excluded conditions. | exclusions.conditions |  |
| 25 | POLICY_ENGINE | condition_waiting_period | PASS | NONE | No condition with a specific waiting period (diabetes, hypertension, thyroid disorders, joint replacement, maternity, mental health, obesity treatment, hernia, cataract) was found in the diagnosis or medicines. | waiting_periods.specific_conditions |  |
| 26 | POLICY_ENGINE | registered_practitioner | PASS | NONE | Practitioner registration AYUR/KL/2345/2019 matches a recognised AYUSH format. | opd_categories.alternative_medicine.requires_registered_practitioner |  |
| 27 | POLICY_ENGINE | covered_system | PASS | NONE | Treatment identified as Ayurveda, a covered system. | opd_categories.alternative_medicine.covered_systems |  |
| 28 | POLICY_ENGINE | session_limit | PASS | NONE | 5 session(s) billed, within the yearly maximum of 20 (prior sessions this year are not tracked; documented limitation). | opd_categories.alternative_medicine.max_sessions_per_year |  |
| 29 | POLICY_ENGINE | billed_amount | INFO | NONE | 2 bill line(s) from 1 bill(s), totalling ₹4,000 (claimed ₹4,000). |  |  |
| 30 | POLICY_ENGINE | line_item | PASS | NONE | Line 'Panchakarma Therapy (5 sessions)' (₹3,000) is eligible. |  |  |
| 31 | POLICY_ENGINE | line_item | PASS | NONE | Line 'Consultation' (₹1,000) is eligible. |  |  |
| 32 | POLICY_ENGINE | per_claim_limit | PASS | NONE | Eligible amount ₹4,000 is within the per-claim ceiling of ₹8,000. | coverage.per_claim_limit |  |
| 33 | POLICY_ENGINE | network_discount | INFO | NONE | Provider 'Ayur Wellness Centre' is not a network hospital; no network discount. | network_hospitals |  |
| 34 | POLICY_ENGINE | copay | PASS | NONE | No co-pay applies to alternative medicine (0%). | opd_categories.alternative_medicine.copay_percent |  |
| 35 | POLICY_ENGINE | ytd_usage | INFO | NONE | Year-to-date OPD usage for EMP006: ₹0 (caller-supplied ₹0, recorded in this system ₹0; the larger is used). Remaining annual OPD limit: ₹50,000 of ₹50,000. | assumptions.annual_limits |  |
| 36 | POLICY_ENGINE | annual_limit | PASS | NONE | Payout ₹4,000 is within the remaining annual OPD limit (₹50,000). | coverage.annual_opd_limit |  |
| 37 | POLICY_ENGINE | annual_limit | PASS | NONE | Payout ₹4,000 is within the remaining family floater limit (₹1,50,000). | coverage.family_floater.combined_limit |  |
| 38 | POLICY_ENGINE | policy_summary | INFO | NONE | Policy evaluation: APPROVED, payable ₹4,000. 2 line(s) approved, 0 rejected. |  |  |
| 39 | FRAUD | stage_failure | ERROR | DEGRADE | FRAUD failed (InjectedFault: Simulated FRAUD outage (requested via simulate_component_failure).) — stage skipped; pipeline continued with reduced confidence and manual review recommended. |  | 0.7 (ALL) |
| 40 | DECISION | final_decision | INFO | NONE | FINAL: APPROVED, approved ₹4,000, confidence 0.565. APPROVED ₹4,000 of ₹4,000 claimed. Eligible amount: ₹4,000 (of ₹4,000 billed). Final payable: ₹4,000. Degraded components: FRAUD (failed and skipped); manual review is recommended due to incomplete processing. |  |  |

</details>

---

## TC012 — Excluded Treatment

_Member claims for bariatric consultation and a diet program. Obesity treatment is explicitly excluded under the policy._

**Expected:** `{"decision": "REJECTED", "rejection_reasons": ["EXCLUDED_CONDITION"], "confidence_score": "above 0.90"}`

**Decision:** REJECTED · status DECIDED · approved ₹0 · confidence 0.921

**Rejection reasons:** EXCLUDED_CONDITION  

**Member message:** We're sorry — your claim for ₹8,000 could not be approved. The diagnosis/treatment (Morbid Obesity — BMI 37 ; Bariatric Consultation and Customised Diet Plan) falls under the policy exclusions 'Obesity and weight loss programs' and 'Bariatric surgery' (matched: obesity, diet plan, bariatric). Excluded conditions are not covered at any point in the policy, so the whole claim is rejected. If you believe this is wrong, you can reply with additional documents and our team will re-check it.

**Ops reason:** REJECTED (EXCLUDED_CONDITION): The diagnosis/treatment (Morbid Obesity — BMI 37 ; Bariatric Consultation and Customised Diet Plan) falls under the policy exclusions 'Obesity and weight loss programs' and 'Bariatric surgery' (matched: obesity, diet plan, bariatric). Excluded conditions are not covered at any point in the policy, so the whole claim is rejected.

| Line item | Amount | Status | Reason |
|---|---|---|---|
| Bariatric Consultation | ₹3,000 | REJECTED | Whole claim rejected (EXCLUDED_CONDITION). |
| Personalised Diet and Nutrition Program | ₹5,000 | REJECTED | Whole claim rejected (EXCLUDED_CONDITION). |

**Confidence breakdown:** 0.95 (EXTRACTOR/base) × 0.97 (POLICY_ENGINE/exclusions) = 0.921

**Checks:**

- ✅ decision REJECTED
- ✅ confidence > 0.9
- ✅ rejection_reasons contains EXCLUDED_CONDITION
- ✅ message mentions obesity

<details><summary>Full trace (36 events)</summary>

| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |
|---|---|---|---|---|---|---|---|
| 1 | INTAKE | claim_received | INFO | NONE | Claim TC012 received: consultation claim for ₹8,000 with 2 document(s). |  |  |
| 2 | INTAKE | member_lookup | PASS | NONE | Member EMP009 (Anita Desai) found on the roster. |  |  |
| 3 | INTAKE | patient_link | PASS | NONE | Patient EMP009 is covered under member EMP009 as SELF. |  |  |
| 4 | INTAKE | dates | PASS | NONE | Treatment and submission dates are valid. |  |  |
| 5 | INTAKE | coverage_start | PASS | NONE | Coverage for EMP009 started 01 Apr 2024 (patient join_date). |  |  |
| 6 | DOC_GATE | required_documents | PASS | NONE | All required document types present for consultation: prescription, hospital bill (as declared by the member; verified against detected types after extraction). | document_requirements.CONSULTATION.required |  |
| 7 | EXTRACTOR | extraction | PASS | NONE | 'F023' read as prescription (fixture data). |  |  |
| 8 | EXTRACTOR | extraction | PASS | NONE | 'F024' read as hospital bill (fixture data). |  |  |
| 9 | DOC_VERIFY | detected_type | PASS | NONE | Detected document types satisfy the claim's document requirements. |  |  |
| 10 | DOC_VERIFY | doctor_registration | PASS | NONE | Doctor registration 'WB/34567/2015' on 'F023' matches the STATE_MEDICAL_COUNCIL format. | registration_patterns |  |
| 11 | CONSISTENCY | identity_evidence | WARN | NONE | No patient name could be found on any document; identity rests on the member-entered ID only. | confidence.identity_unverified | 0.85 (PAYOUT) |
| 12 | CONSISTENCY | document_dates | NOT_EVALUABLE | NONE | No dates could be read on the documents. |  |  |
| 13 | CONSISTENCY | consistency | PASS | NONE | Documents are consistent with each other and the claim. |  |  |
| 14 | POLICY_ENGINE | category_covered | PASS | NONE | Consultation is a covered OPD category. | opd_categories.consultation.covered |  |
| 15 | POLICY_ENGINE | minimum_claim_amount | PASS | NONE | Claimed ₹8,000 meets the ₹500 minimum. | submission_rules.minimum_claim_amount |  |
| 16 | POLICY_ENGINE | submission_deadline | NOT_EVALUABLE | NONE | No submission date supplied, so the 30-day submission deadline could not be checked. | submission_rules.deadline_days_from_treatment |  |
| 17 | POLICY_ENGINE | policy_status | PASS | NONE | Policy PLUM_GHI_2024 is ACTIVE. | policy_holder.renewal_status |  |
| 18 | POLICY_ENGINE | policy_period | PASS | NONE | Treatment on 18 Oct 2024 falls within the policy period 01 Apr 2024 to 31 Mar 2025. | policy_holder.policy_start_date..policy_end_date |  |
| 19 | POLICY_ENGINE | member_coverage_date | PASS | NONE | Patient was covered on the treatment date (cover started 01 Apr 2024). | members[].join_date |  |
| 20 | POLICY_ENGINE | relationship_covered | PASS | NONE | Relationship SELF (policy term SELF) is covered. | coverage.family_floater.covered_relationships |  |
| 21 | POLICY_ENGINE | initial_waiting_period | PASS | NONE | Initial 30-day waiting period ended 01 May 2024, before treatment. | waiting_periods.initial_waiting_period_days |  |
| 22 | POLICY_ENGINE | pre_existing_conditions | NOT_EVALUABLE | NONE | Pre-existing condition waiting period not evaluated: distinguishing a pre-existing condition needs medical history this system does not receive (documented assumption). | waiting_periods.pre_existing_conditions_days |  |
| 23 | POLICY_ENGINE | exclusions | FAIL | REJECT | The diagnosis/treatment (Morbid Obesity — BMI 37 ; Bariatric Consultation and Customised Diet Plan) falls under the policy exclusions 'Obesity and weight loss programs' and 'Bariatric surgery' (matched: obesity, diet plan, bariatric). Excluded conditions are not covered at any point in the policy, so the whole claim is rejected. | exclusions.conditions | 0.97 (ALL) |
| 24 | POLICY_ENGINE | condition_waiting_period | SKIP | NONE | Obesity treatment waiting period not applied: the condition is permanently excluded, which supersedes a waiting period. | waiting_periods.specific_conditions.obesity_treatment |  |
| 25 | POLICY_ENGINE | billed_amount | INFO | NONE | 2 bill line(s) from 1 bill(s), totalling ₹8,000 (claimed ₹8,000). |  |  |
| 26 | POLICY_ENGINE | policy_summary | INFO | NONE | Policy evaluation: REJECTED, payable ₹0; rejection reasons EXCLUDED_CONDITION. 0 line(s) approved, 2 rejected. |  |  |
| 27 | FRAUD | claim_history | INFO | NONE | 0 earlier claim(s) on record for EMP009 (caller-supplied history merged with this system's records). | assumptions.fraud_history |  |
| 28 | FRAUD | same_day_frequency | PASS | NONE | 1 claim(s) on 18 Oct 2024, within the limit of 2. | fraud_thresholds.same_day_claims_limit |  |
| 29 | FRAUD | monthly_frequency | PASS | NONE | 1 claim(s) in the last 30 days, within the limit of 6. | fraud_thresholds.monthly_claims_limit |  |
| 30 | FRAUD | claim_value | PASS | NONE | Claimed ₹8,000 is below the high-value threshold of ₹25,000. | fraud_thresholds.high_value_claim_threshold |  |
| 31 | FRAUD | duplicate_claim | PASS | NONE | No earlier claim with the same date and amount. |  |  |
| 32 | FRAUD | duplicate_document | NOT_EVALUABLE | NONE | No file hashes or bill numbers available (fixture documents), so document reuse could not be checked. |  |  |
| 33 | FRAUD | document_integrity | PASS | NONE | No alteration or duplicate-stamp flags on any document. |  |  |
| 34 | FRAUD | claim_vs_bill | PASS | NONE | Claimed ₹8,000 is consistent with the ₹8,000 billed. |  |  |
| 35 | FRAUD | fraud_score | PASS | NONE | Fraud score 0.0 is below the manual-review threshold of 0.8; no signals. | fraud_thresholds.fraud_score_manual_review_threshold |  |
| 36 | DECISION | final_decision | INFO | NONE | FINAL: REJECTED, approved ₹0, confidence 0.921. REJECTED (EXCLUDED_CONDITION): The diagnosis/treatment (Morbid Obesity — BMI 37 ; Bariatric Consultation and Customised Diet Plan) falls under the policy exclusions 'Obesity and weight loss programs' and 'Bariatric surgery' (matched: obesity, diet plan, bariatric). Excluded conditions are not covered at any point in the policy, so the whole claim is rejected. |  |  |

</details>
