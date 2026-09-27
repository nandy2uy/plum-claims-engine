# Component contracts

Each contract is meant to be precise enough to reimplement the component without reading its code. Types refer to the Pydantic models in `src/models/`. Unless stated otherwise:

- Every component writes to the claim's `TraceLedger` and **never raises for bad claim data**: bad data is a traced outcome. Raised exceptions mean a bug or an infrastructure fault, and are caught by `run_stage` or the orchestrator.
- Money is `Decimal`, rounded half-up to 2 places at each waterfall step.
- "Logs" means appends a `TraceEvent` with a non-empty `summary`.

---

## Data contracts

### ClaimSubmission (input, `src/models/claim.py`)
| Field | Type | Rules |
|---|---|---|
| `claim_id` | str | Optional; generated `CLM-XXXXXXXXXX` if absent. Resubmitting the same id replaces the earlier decision (the resubmission flow). |
| `member_id` | str | Required. |
| `patient_id` | str | Defaults to `member_id`. |
| `policy_id` | str? | If given, must equal the loaded policy. |
| `category` | str | Normalised to upper snake case (`consultation` → `CONSULTATION`). |
| `treatment_date` | date | Strict ISO `YYYY-MM-DD`. Anything else is a 422. |
| `claimed_amount` | Decimal | > 0, max 2 decimals. |
| `hospital_name`, `pre_auth_id` | str? | |
| `documents` | DocumentSource[] | `file_id` unique within the claim. |
| `submission_date` | date? | Upload endpoint stamps server date when absent. |
| `ytd_claims_amount` | Decimal? | ≥ 0. A floor, never a replacement, for stored usage. |
| `claims_history` | ClaimHistoryEntry[]? | Merged with stored history, never replaces it. |
| `simulate_component_failure`, `simulate_failure_component` | bool, str | Component ∈ EXTRACTOR, DOC_VERIFY, CONSISTENCY, POLICY_ENGINE, FRAUD (default FRAUD). Honoured only when `ALLOW_FAULT_INJECTION=true`. |

**DocumentSource**: `file_id`, `file_type` (what the member declared), `file_name?`, and exactly one content source: `content_base64` + `mime_type`, `content_url`, or `pre_extracted_data` (fixture mode: `{detected_type, quality: GOOD|POOR|UNREADABLE, confidence?, flags?, unreadable_fields?, prescription_data|bill_data|report_data}`). Fixture mode is honoured only when `ALLOW_FIXTURE_DOCUMENTS=true`: the JSON endpoint returns 422 otherwise, and the extractor refuses the document (MEMBER failure, flag `FIXTURE_NOT_ALLOWED`) as a second line of defence.

### ClaimDecision (output)
| Field | Meaning |
|---|---|
| `status` | `DECIDED` or `NEEDS_MEMBER_ACTION`. |
| `decision` | `APPROVED`, `PARTIAL`, `REJECTED`, `MANUAL_REVIEW`, or `null` when status is `NEEDS_MEMBER_ACTION`. |
| `approved_amount` | Payable now. 0 for REJECTED, MANUAL_REVIEW and early stops. |
| `provisional_amount` | For MANUAL_REVIEW: what would be paid if the reviewer clears it. |
| `reason` / `member_message` | Operations explanation / plain-language member text. |
| `rejection_reasons` | Codes, for REJECTED and PARTIAL: CATEGORY_NOT_COVERED, BELOW_MINIMUM_AMOUNT, SUBMISSION_DEADLINE_EXCEEDED, POLICY_INACTIVE, OUTSIDE_POLICY_PERIOD, MEMBER_NOT_COVERED_ON_DATE, RELATIONSHIP_NOT_COVERED, WAITING_PERIOD, EXCLUDED_CONDITION, EXCLUDED_PROCEDURE, PRE_AUTH_MISSING, PER_CLAIM_EXCEEDED, ANNUAL_LIMIT_EXHAUSTED. |
| `review_reasons` | ROSTER_INCOMPLETE, DIAGNOSIS_UNREADABLE, PRACTITIONER_UNVERIFIED, SESSION_LIMIT_EXCEEDED, ITEM_NEEDS_REVIEW, CLINICAL_CODING_UNCERTAIN, FRAUD_SIGNALS, EXTRACTION_INCOMPLETE, POLICY_ENGINE_UNAVAILABLE, FRAUD_CHECK_UNAVAILABLE_HIGH_VALUE, PIPELINE_ERROR. |
| `required_actions` | MemberAction[]: `action` ∈ UPLOAD_DOCUMENT, REUPLOAD_DOCUMENT, REPLACE_DOCUMENT, CORRECT_DETAILS, OBTAIN_PRE_AUTH, CONTACT_SUPPORT; optional `file_id`, `document_type`. |
| `confidence_score`, `confidence_breakdown` | Score = product of all breakdown factors (first factor is the base). |
| `manual_review_recommended`, `degraded_components`, `warnings` | Degradation visibility. |
| `fraud_signals`, `fraud_score` | `"CODE: detail"` strings; noisy-OR score, null if fraud did not run. |
| `line_item_breakdown`, `financial_breakdown` | Per line status and reason; ordered payout steps. |
| `trace` | TraceEvent[] with contiguous `seq` from 1. First event is INTAKE `claim_received`; last is DECISION `final_decision`. |

### TraceEvent
`seq`, `timestamp`, `component`, `rule_id?`, `outcome` ∈ PASS FAIL WARN SKIP NOT_EVALUABLE ERROR INFO, `effect` ∈ NONE BLOCK REJECT REVIEW ADJUST_AMOUNT ESCALATE DEGRADE, `summary`, `policy_ref?`, `interpretation_ref?`, `evidence{}`, `amount_before?`, `amount_after?`, `confidence_factor?` (0–1), `confidence_scope` ∈ ALL PAYOUT, `duration_ms`, `error_details?`.

### DocumentExtraction (between extractor and everything downstream)
`file_id`, `file_name?`, `declared_type`, `detected_type?`, `source` ∈ fixture llm none, `readability` ∈ GOOD POOR UNREADABLE, `confidence_score`, one of `prescription_data | bill_data | report_data`, `flags[]`, `unreadable_fields[]`, `error?`, `failure_kind` ∈ null MEMBER SYSTEM. `usable` ⇔ `failure_kind is null`; `effective_type` = detected, else declared.

---

## Agents

### IntakeAgent — `src/agents/intake.py`
- **Construct:** `IntakeAgent(policy: PolicyConfig, policy_fingerprint: str, today: () -> date = date.today)`
- **Call:** `evaluate(submission, ledger) -> IntakeResult`
- **Output:** `ok=True` with `MemberContext{member, patient?, patient_id, patient_name?, relationship, coverage_start, family_ids, roster_incomplete}`; or `ok=False` with `message` and one `CORRECT_DETAILS` action.
- **Blocks when:** policy_id mismatch; member not on roster; patient neither the member nor linked to that member (in `dependents` or via `primary_member_id`); treatment date after `today`; submission date before treatment date.
- **Does not block:** a linked dependent with no roster record (`roster_incomplete=True`, WARN logged).
- **Coverage start:** patient `join_date` → primary member `join_date` → policy start date.
- **Errors:** none.

### DocumentGateAgent — `src/agents/doc_gate.py`
- **Construct:** `DocumentGateAgent(policy, interpretation)`
- **Call:** `evaluate(submission, ledger) -> GateResult{passed, message, actions, missing}`. Uses declared `file_type` only; makes no model call.
- **Blocks when:** category not in `document_requirements`; any required type absent.
- **Message must contain:** every uploaded file label with its declared type, the missing type(s), the full requirement for the category, a description of each missing document (`interpretation.document_type_descriptions`), the treatment date, and which uploaded files the new one replaces (types that are neither required nor optional, and repeats of a required type). One `UPLOAD_DOCUMENT` action per missing type.
- **Helpers used by the verifier:** `required_types(category)`, `missing_types(category, present_types)`, `missing_message(submission, files, missing)`.
- **Errors:** none.

### ExtractionAgent — `src/agents/extractor.py`
- **Construct:** `ExtractionAgent(interpretation, backend: DocumentExtractorBackend | None, max_concurrency: int, allow_fixtures: bool = False)`
- **Call:** `async process(submission, ledger) -> List[DocumentExtraction]`: exactly one per document, in submission order.
- **Behaviour:** documents processed concurrently (semaphore = `max_concurrency`) on forked ledgers merged in document order.
  - Fixture: trusted as extraction output. `quality=UNREADABLE` → MEMBER failure. Schema failure → SYSTEM failure.
  - Live: `ExtractionError(kind=INPUT)` → MEMBER (`UNSUPPORTED_FILE_TYPE`); any other failure → SYSTEM (`EXTRACTION_FAILED`, ERROR event, factor `degraded_component`); readability UNREADABLE → MEMBER.
  - Live minimum evidence: if none of `extraction_minimum_evidence[detected type]` is present (prescription: patient_name, diagnoses, treatments; bills: total_amount, line_items; reports: patient_name, tests) → MEMBER (`MISSING_CRITICAL_FIELDS`), error text naming the missing fields.
  - Live confidence = `w × model-reported + (1 − w) × completeness`, where completeness = share of `extraction_critical_fields[type]` present and `w = confidence.extraction_model_weight` (0.30). The extraction event's evidence holds `confidence{computed, model_reported, completeness, missing_critical_fields}` and `model_call{model, prompt_version, schema_mode, latency_ms, input_tokens, output_tokens, attempts, repaired, pages}`.
  - Fixture refused (allow_fixtures False) → MEMBER (`FIXTURE_NOT_ALLOWED`).
  - No content at all → MEMBER (`NO_CONTENT_PROVIDED`). No backend configured → SYSTEM.
  - Readability POOR, illegible fields, or dropped bill lines → one WARN `partial_readability` event per document with factor `confidence.partially_unreadable` (scope ALL).
- **Errors:** none per document; one bad document never fails the batch.

### DocumentExtractorBackend / VisionExtractor — `src/core/llm.py`
- **Protocol:** `async extract(doc: DocumentSource) -> LLMDocumentResult{detected_document_type, readability, confidence, unreadable_fields, quality_flags, prescription?, bill?, report?, meta: ExtractionMeta}`
- **VisionExtractor(settings, condition_vocabulary: List[str] = [])** (alias `OpenAIVisionExtractor`)
  - Provider from `LLM_PROVIDER` (gemini default, or openai), both through the OpenAI-compatible Chat Completions API: key `GEMINI_API_KEY` / `OPENAI_API_KEY`, model `LLM_MODEL` or the provider default, endpoint `LLM_BASE_URL` or the provider default (Gemini: `https://generativelanguage.googleapis.com/v1beta/openai/`).
  - Images (JPEG/PNG/WebP/GIF) are sent as data URLs; PDFs are rendered to PNG per page (max `MAX_PDF_PAGES`) and sent in one call; http(s) image URLs pass through.
  - One chat-completions call, temperature 0, `response_format` = strict JSON schema (`STRICT_SCHEMA`). On HTTP 400 it steps down `json_schema_strict` → `json_object` → `prompt_only` (no response_format) and remembers the first mode that succeeds; a request refused in every mode is a SYSTEM error and does not change the remembered mode. `meta.schema_mode` records the mode used, `meta.provider` the provider. The response is validated; one repair round-trip on invalid output (`meta.repaired=True`). A `meta` key in model output is discarded.
  - Line items without a readable amount are dropped and counted in `bill.dropped_line_items`.
  - `prescription.condition_tags` are filtered to `condition_vocabulary` (case-insensitive) after parsing.
  - Retries only APITimeoutError, APIConnectionError, RateLimitError, InternalServerError, TimeoutError; `LLM_MAX_RETRIES` extra attempts with exponential backoff; SDK retries disabled.
- **Errors:** `ExtractionError(kind="INPUT")` for invalid base64, unsupported MIME type, file larger than `MAX_UPLOAD_MB`, corrupt or empty PDF, unsupported URL scheme. `ExtractionError(kind="SYSTEM")` for missing API key, non-retryable API error, retries exhausted, output invalid after repair.

### DocumentVerificationAgent — `src/agents/doc_verifier.py`
- **Construct:** `DocumentVerificationAgent(gate: DocumentGateAgent, interpretation)`
- **Call:** `evaluate(submission, extractions, ledger) -> VerificationResult{status: OK|NEEDS_MEMBER_ACTION|DEGRADED, message, actions, system_failed_required}`
- **NEEDS_MEMBER_ACTION when:**
  - A MEMBER-fault failure affects a required document. One `REUPLOAD_DOCUMENT` per file; the message names the file, its type, why it failed and how to photograph it, and says the claim is not rejected.
  - Using detected types, a required type is missing. If a mismatched file explains it, `REPLACE_DOCUMENT` for that file; otherwise the gate's missing-document message.
- **DEGRADED when:** a required document failed for a SYSTEM reason.
- **Otherwise:** mismatched but sufficient types → WARN with factor `type_mismatch_accepted` (PAYOUT). Doctor registration is checked against `registration_patterns`: valid → PASS; missing → WARN factor `missing_registration`; malformed → WARN factor `invalid_registration` (both PAYOUT); no prescription → NOT_EVALUABLE.
- **Errors:** none.

### ConsistencyAgent — `src/agents/consistency.py`
- **Construct:** `ConsistencyAgent(interpretation)`
- **Call:** `evaluate(submission, context, usable_extractions, ledger) -> ConsistencyResult{passed, message, actions}`
- **Name matching** (`match_names`): EXACT, FUZZY (first and last name both ≥ `name_match_fuzzy_threshold`), INITIALS ("R. Kumar"), PARTIAL (first name only) count as matches; a shared surname alone is MISMATCH.
- **Blocks when:** any pair of named documents mismatch (message lists the name on every file and which file to replace), or named documents do not match the patient on record.
- **Logs with factors (PAYOUT):** fuzzy/initials (`fuzzy_name_match`), partial (`partial_name_match`), no names anywhere (`identity_unverified`), document dates more than `document_date_tolerance_days` from treatment (`document_date_mismatch`). Bill hospital differing from the entered hospital → WARN.
- **Errors:** none.

### PolicyEngine — `src/agents/policy_engine.py`
- **Construct:** `PolicyEngine(policy, interpretation, usage: (patient_ids, start, end, exclude_claim_id) -> Decimal)`
- **Call:** `evaluate(submission, context, usable_extractions, ledger) -> PolicyResult{proposal, approved_amount, findings[], line_items[], financial?, warnings[], actions[]}`. `Finding{code, kind: REJECT|REVIEW, ops_text, member_text, policy_ref}`.
- **Claim-level rules (all evaluated, each logged):** category covered; `claimed ≥ minimum_claim_amount`; `submission_date − treatment_date ≤ deadline` (NOT_EVALUABLE without submission date); renewal ACTIVE; treatment within policy period; treatment ≥ coverage start; relationship covered (aliases via `relationship_aliases`, NOT_EVALUABLE + ROSTER_INCOMPLETE review when roster incomplete); treatment ≥ coverage start + initial waiting days; pre-existing conditions logged NOT_EVALUABLE.
- **Clinical rules:** text = prescription diagnoses + treatments, with abbreviations expanded in place. Matching is whole-phrase with optional plural suffix and negation-aware (`find_mentions`: a match preceded within `negation_window_words` words of the same clause by any `negation_cues` entry is negated; clause breaks are `. ; :` newline, "but", "however", "except"). A condition mentioned only negatively → SKIP event listing the negated snippets.
  - Category requires a prescription and there is no diagnosis, treatment or tag → DIAGNOSIS_UNREADABLE review (factor `diagnosis_missing`). Not required → NOT_EVALUABLE.
  - A REJECT-type exclusion keyword hit → one EXCLUDED_CONDITION finding (factor `keyword_clinical_match`, applied once).
  - A REVIEW-type exclusion hit → ITEM_NEEDS_REVIEW.
  - A condition waiting-period keyword hit inside the window → WAITING_PERIOD with the eligible-from date (coverage start + days). It is skipped (SKIP) when the same condition is permanently excluded.
  - Medicine hints only → WARN factor `medicine_condition_hint`.
  - A model `condition_tag` without a keyword hit → CLINICAL_CODING_UNCERTAIN review (factor `llm_only_clinical_code`). It never rejects.
- **Category rules:** registered practitioner (registration matches `alternative_medicine_registration_patterns`, else PRACTITIONER_UNVERIFIED review); covered system (keywords in `alternative_medicine_system_keywords`; NOT_EVALUABLE if unidentified; CATEGORY_NOT_COVERED if identified and not covered); sessions in this claim > `max_sessions_per_year` → SESSION_LIMIT_EXCEEDED review; dental report missing → WARN factor `missing_supporting_report`.
- **Lines:** every usable bill's line items; a bill with only a total → one synthetic line; no readable bill → synthetic line of the claimed amount (review). Per line:
  - exclusion keywords → EXCLUDED_CONDITION (REJECT) or NEEDS_REVIEW;
  - category `excluded_*` + `exclusions.<category>_exclusions` via `procedure_aliases` → EXCLUDED_PROCEDURE;
  - `covered_*` match → PASS, otherwise WARN `unlisted_procedure`;
  - pre-auth: all rules in `pre_auth_rules` whose keywords match are considered and an `always_required` rule wins; required if always, or line amount > category `pre_auth_threshold` (else the lowest threshold in the policy). Required without `pre_auth_id` → PRE_AUTH_MISSING plus an `OBTAIN_PRE_AUTH` action; with it → the line is marked pre-authorised.
- **Ceiling:** `eligible = min(sum(approved lines), claimed)`. The compared amount is eligible minus pre-authorised lines, and ceiling = max(`coverage.per_claim_limit`, category `sub_limit`). Compared > ceiling → PER_CLAIM_EXCEEDED (whole claim). A category sub_limit below the global limit that is exceeded → advisory WARN.
- **Waterfall per approved line** (scaled by eligible/approved sum): network discount if a bill's hospital (else the entered hospital) matches a network hospital by brand tokens; co-pay (`branded_drug_copay_percent` when `is_branded=true`); then remaining annual OPD limit = limit − max(caller YTD, stored patient usage) and remaining family floater, each capping the payout (ANNUAL_LIMIT_EXHAUSTED; PARTIAL if capped, REJECTED if nothing remains).
- **Proposal:** REJECTED if any REJECT-type claim finding or payable = 0 with rejections; PARTIAL if some lines rejected or capped; APPROVED otherwise; MANUAL_REVIEW if any REVIEW finding and not REJECTED.
- **Errors:** none for valid input.

### FraudAgent — `src/agents/fraud.py`
- **Construct:** `FraudAgent(policy, interpretation, store)`; the store needs `prior_claims(member_id, exclude_claim_id)` and `find_by_fingerprints(fps, exclude_claim_id)`.
- **Call:** `evaluate(submission, usable_extractions, fingerprints, ledger) -> FraudResult{score, signals[FraudSignal{code, detail, weight, hard}], escalate}`
- **History:** stored DECIDED claims of the member ∪ caller `claims_history` (by claim_id, excluding this claim).
- **Signals:**
  - SAME_DAY_LIMIT_EXCEEDED (claims on treatment date incl. this > limit, HARD, detail lists the earlier claim ids);
  - MONTHLY_LIMIT_EXCEEDED (30-day window, HARD);
  - HIGH_VALUE_AUTO_REVIEW (> `auto_manual_review_above`, HARD) else HIGH_VALUE_CLAIM (> `high_value_claim_threshold`, soft);
  - POSSIBLE_DUPLICATE_CLAIM (same date and amount, HARD);
  - DUPLICATE_DOCUMENT (fingerprint seen on an earlier claim, HARD);
  - DOCUMENT_ALTERATION / DUPLICATE_STAMP (extraction flags, soft);
  - CLAIM_EXCEEDS_BILL (claimed > billed × `claim_to_bill_inflation_tolerance`, soft).
- **Score and escalation:** weights from `fraud_signal_weights`; score = 1 − Π(1 − w); `escalate` = any HARD or score ≥ `fraud_score_manual_review_threshold`.
- **`document_fingerprints(submission, extractions)`:** sha256 of uploaded bytes or URL, plus `bill:<provider>|<bill number>`; fixture documents are not content-hashed.
- **Errors:** none for valid input.

### DecisionAgent — `src/agents/decision.py`
- **Construct:** `DecisionAgent(policy, interpretation)`
- **Calls:**
  - `needs_member_action(submission, ledger, stage, message, actions, extractions?) -> ClaimDecision` (status NEEDS_MEMBER_ACTION, decision null).
  - `decide(submission, extractions, verification, policy_stage: StageResult, fraud_stage: StageResult, ledger) -> ClaimDecision`.
  - `system_failure(submission, ledger, error) -> ClaimDecision` (MANUAL_REVIEW, PIPELINE_ERROR, trace preserved).
- **Precedence:** policy failed → MANUAL_REVIEW; verification DEGRADED → MANUAL_REVIEW; REJECTED stays REJECTED; fraud escalate → MANUAL_REVIEW; fraud failed and claimed ≥ high-value threshold → MANUAL_REVIEW; policy review findings → MANUAL_REVIEW; else the proposal. MANUAL_REVIEW sets `approved_amount=0` and `provisional_amount` = computed payout.
- **Confidence:** base = mean `confidence_score` of usable extractions with confidence > 0, else `no_extraction_performed`. Multiply every ledger event factor with scope ALL, and PAYOUT factors only when the decision is APPROVED, PARTIAL or MANUAL_REVIEW. Soft fraud signals that did not escalate add a factor of 1 − score × `fraud_soft_score_weight`. Round to 3 places.
- **Member message for MANUAL_REVIEW** never mentions fraud; when the cause is a system failure it says so explicitly.
- **Errors:** none.

---

## Infrastructure

### ClaimsOrchestrator — `src/core/orchestrator.py`
- **Construct:** `ClaimsOrchestrator(policy?, interpretation?, settings?, store?, backend?, today?)`. Defaults load from config; a backend is created only if the active provider's key (`GEMINI_API_KEY` or `OPENAI_API_KEY`) is set.
- **Call:** `async process(submission) -> ClaimDecision`. Never raises.
- **Order:** intake → gate → extractor (critical; failure marks all documents SYSTEM) → verifier (critical; failure = DEGRADED) → consistency (non-critical) → policy (critical) ∥ fraud (non-critical) → decision → `store.record`.
- **Budgets:** `EXTRACTION_STAGE_TIMEOUT_SECONDS` for extraction, `STAGE_TIMEOUT_SECONDS` for the rest.

### run_stage / FaultInjector — `src/core/fault_tolerance.py`
- `async run_stage(ledger, component, fn, *args, timeout, critical, fallback=None, injector=None, offload=False, degraded_confidence=0.70, **kwargs) -> StageResult{ok, value, error, timed_out}`
  - Coroutines are awaited under `wait_for`; sync functions run inline or, with `offload=True`, in a thread under `wait_for`.
  - On any exception or timeout it logs one ERROR event (`rule_id="stage_failure"`, effect ESCALATE if critical else DEGRADE, confidence factor `degraded_confidence`, evidence includes `injected`) and returns `ok=False, value=fallback`. It never raises.
- `FaultInjector(submission, settings, ledger)`: arms only when `settings.allow_fault_injection`; logs INFO when armed and WARN when a request is ignored. `should_fail(component) -> bool`.

### ClaimStore — `src/core/storage.py`
- `record(submission, decision, fingerprints)` — idempotent per claim_id; evicts oldest beyond `STORE_MAX_RECORDS`. Stores no document bytes.
- `get(claim_id)`, `list_recent(limit)` (newest first)
- `prior_claims(member_id, exclude_claim_id)` (DECIDED only)
- `paid_between(patient_ids, start, end, exclude_claim_id)` (sum of approved amounts of APPROVED/PARTIAL claims with treatment date in range)
- `find_by_fingerprints(fps, exclude_claim_id)`
- `clear()`
- Thread-safe. Errors: none.

### Config — `src/core/config.py`
- `get_settings() -> Settings` (env / `.env`)
- `get_policy() -> PolicyConfig`
- `get_interpretation() -> Interpretation`
- `config_fingerprint(filename) -> str` (12 hex chars of sha256)
- `load_test_cases() -> list`
- `reset_caches()`
- **Errors:** `ConfigError` for a missing file, invalid JSON, or a schema violation. The API calls these at startup, so the service refuses to boot on a broken policy.

### HTTP API — `src/main.py`
| Method & path | Input | Output | Errors |
|---|---|---|---|
| `GET /health` | — | status, engine version, policy id + fingerprint, interpretation version, live extraction configured, LLM provider / model / key env var, fault injection enabled, roster integrity warnings | — |
| `GET /api/v1/policy` | — | members with dependents, categories with required/optional documents and sub-limits, network hospitals, policy period | — |
| `POST /api/v1/claims` | ClaimSubmission JSON | ClaimDecision | 422 with `detail: ["field: message", …]`, including fixture documents when `ALLOW_FIXTURE_DOCUMENTS` is off |
| `POST /api/v1/claims/upload` | multipart: `member_id, category, treatment_date, claimed_amount, files[], file_types[]` (+ optional `patient_id, hospital_name, pre_auth_id, submission_date, claim_id`) | ClaimDecision | 422 invalid field or file/type count mismatch or empty file; 413 file > `MAX_UPLOAD_MB`; 415 not an image/PDF |
| `GET /api/v1/claims?limit=` | — | ClaimDecision[] newest first (1–200) | — |
| `GET /api/v1/claims/{id}` | — | ClaimDecision | 404 |
| `GET /api/v1/test-cases` | — | the 12 cases (id, name, description, expected) | — |
| `POST /api/v1/test-cases/{id}/run` | — | `{case, decision, passed, checks[]}` using an isolated store; always allows its own fixtures and simulated failure | 404 |

### Eval harness — `src/evals/`
- `case_to_payload(case, simulate_failure=None) -> dict`, `case_to_submission(...)`: `file_type = detected_type = actual_type`; content passed through unchanged; `patient_name_on_doc` becomes `patient_name`.
- `check_case(case, result_json, baseline_json=None) -> List[Check{name, passed, detail}]`: status/decision, approved amount, confidence threshold, structured rejection codes, and a concrete check per `system_must` bullet.
- `async run_all(settings?, http_url?) -> List[CaseResult]`, `render_markdown(results) -> str`.
- `samples.py`: `SAMPLES`, `render_sample(sample) -> bytes` (PNG or 2-page PDF), `write_samples(dir)` (+ manifest.json), `compare(expected, extraction_json) -> List[FieldCheck{field, expected, actual, ok}]`.

### text helpers — `src/core/text.py`
- `keyword_hits(text, keywords) -> List[str]`: whole-phrase, case/Unicode-insensitive, optional plural suffix.
- `find_mentions(text, keywords, negation_cues, window_words=5) -> Mentions{hits, negated{keyword: snippet}}`.
- `expand_abbreviations(text, mapping) -> str`: in place, "No HTN" → "No HTN (hypertension)".
- `match_names(a, b, threshold) -> NameMatch{level, score}`; `parse_money`, `parse_date_lenient`, `fmt_inr`, `fmt_date`, `classify_registration`.
