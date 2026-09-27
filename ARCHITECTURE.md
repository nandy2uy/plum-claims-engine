# Architecture

This document explains what the system is, why it is shaped this way, what I considered and rejected, where it falls short, and what changes at 10x load. Component interfaces are specified separately in [COMPONENT_CONTRACTS.md](COMPONENT_CONTRACTS.md).

## 1. The problem, restated as design constraints

The assignment's non-negotiables translate into five constraints that drive every decision below.

1. **Money decisions must be deterministic and explainable.** An operations reviewer must be able to reconstruct any decision from its trace. So the component that decides eligibility and payout cannot be a language model. It is plain code over `policy_terms.json`, and every rule writes a trace event whether it passes or fails.
2. **Documents are messy, so perception needs a vision model.** Handwriting, stamps and phone photos rule out template OCR. The model is used for perception only: classifying the document, judging its readability, and extracting fields into a validated schema.
3. **Stop early and specifically.** Wrong or unreadable documents must stop the claim with a message that names the file and says what to upload instead. The cheapest check (declared types) runs before any model call.
4. **Failures are expected.** Every stage has a timeout and a declared criticality. A failed stage never crashes the request. It lowers confidence, shows up in the output, and routes the claim to a human when the missing information matters.
5. **Policy is data.** Nothing in the code names a specific category, limit, condition or hospital. Where the policy file is ambiguous or unstructured, the reading the engine uses is written down as data in `interpretation.json`, and trace events cite it.

## 2. Components

The system is a pipeline of single-responsibility agents coordinated by an orchestrator. Each agent has a typed input and output, never raises for bad claim data (bad data is a traced outcome), and writes to the claim's trace ledger.

```
                  ┌──────────── stop: NEEDS_MEMBER_ACTION (decision = null) ─────────────┐
                  │                  │                    │                   │          │
POST /claims ─▶ INTAKE ─▶ DOC_GATE ─▶ EXTRACTOR ─▶ DOC_VERIFY ─▶ CONSISTENCY ─▶ ┬─ POLICY_ENGINE ─┬─▶ DECISION ─▶ ClaimStore
               member,   declared    one vision    detected vs    same patient,   │  (deterministic) │    final decision,
               patient,  types vs    call per doc, declared type, same episode,   └─ FRAUD ─────────┘    confidence,
               coverage  policy      concurrent    readability,   names/dates        (concurrent, own     messages
               start     (no LLM)                  registration                       forked ledgers)
```

| Agent | Responsibility | Uses LLM | Criticality if it fails |
|---|---|---|---|
| **Intake** | Resolve member, patient and link between them; validate dates; compute coverage start (dependents inherit the primary's join date). | No | Not wrapped: deterministic. A crash here is caught by the orchestrator's safety net (MANUAL_REVIEW with partial trace). |
| **Document gate** | Check declared document types against `document_requirements` **before any model call**. Message names every uploaded file and type, what is missing, and what it must contain. | No | Same as intake. |
| **Extractor** | For every document concurrently: classify, assess readability, extract fields, propose clinical codes. Distinguishes member faults (unreadable, unsupported file) from system faults (model down, bad output). | Yes | Critical-ish: all documents are marked as system failures, verification reports DEGRADED, claim ends in MANUAL_REVIEW. The member is never asked to re-upload for our outage. |
| **Document verifier** | Re-check requirements using *detected* types (catches a prescription uploaded as a bill); ask for re-upload of specific unreadable files; validate doctor registration formats. | No | Critical: treated as DEGRADED → MANUAL_REVIEW. |
| **Consistency** | Same patient across all documents (pairwise, per file), documents vs the patient on record, document dates vs treatment date, bill hospital vs entered hospital. | No | Non-critical: skipped with reduced confidence and a manual-review recommendation. |
| **Policy engine** | Every claim-level rule, line-item adjudication, per-claim ceiling, payout waterfall. Produces a *proposal*. | No | Critical: MANUAL_REVIEW (`POLICY_ENGINE_UNAVAILABLE`). |
| **Fraud** | Frequency, value, duplicate claim/document, integrity flags, claimed vs billed. Noisy-OR score plus hard signals. | No | Non-critical: decision still issued, failure visible, confidence × 0.70, claims at or above the high-value threshold held. |
| **Decision** | Combine proposal, fraud result and degradation into the final decision; compute confidence from trace factors; write ops reason and member message. | No | Pure function of its inputs. |

Supporting modules: `TraceLedger` (append-only, thread-safe, forkable), `run_stage` (timeout, isolation, fault injection), `ClaimStore` (bounded in-memory store behind a narrow interface), `VisionExtractor` (the only network-facing component; Gemini by default or OpenAI, both through the OpenAI-compatible API), `text.py` (pure helpers: name matching, money and date parsing, keyword matching).

### Why agents rather than one function

Separating the pipeline into agents is not decoration. It buys three concrete things.

- **Independent failure semantics.** Criticality is a property of the stage, declared in one place (the orchestrator). The fraud check failing and the policy engine failing mean different things, and the code says so explicitly.
- **Concurrency where it is free.** Policy and fraud read the same inputs and do not depend on each other, so they run concurrently on forked ledgers that are merged back in a fixed order. Traces stay deterministic even though execution is not.
- **Testability and live extension.** Every agent can be constructed and tested alone with typed inputs. Adding, say, a "duplicate provider" check means a new rule in one agent plus a test, with no change elsewhere.

## 3. How a decision is made

**Early stops.** Intake, the document gate, the verifier and consistency can each stop the claim with `status = NEEDS_MEMBER_ACTION` and `decision = null`, plus a specific message and machine-readable `required_actions` (e.g. `REUPLOAD_DOCUMENT` for file `F004`). Stopping is not rejecting: the member fixes the problem and resubmits the same claim.

**Clinical matching.** Condition keywords are matched as whole phrases (plurals allowed) and are negation-aware: a match preceded in the same clause by a cue such as "no", "denies", "family history of" or "r/o" is logged as a negated mention and skipped. Abbreviations are expanded in place ("No HTN" becomes "No HTN (hypertension)") so the negation still applies. The cue list and window are data in `interpretation.json`. A prescription-requiring claim with no readable diagnosis cannot be checked for exclusions or waiting periods, so it goes to manual review (`DIAGNOSIS_UNREADABLE`) instead of being approved by default.

**Policy evaluation.** All claim-level rules run, so a rejection lists every reason rather than the first one hit: category, minimum amount, submission deadline, policy status and period, member coverage date, relationship (family floater), initial waiting period, condition exclusions, condition waiting periods (an exclusion supersedes a waiting period for the same condition), and category-specific rules (registered AYUSH practitioner, covered system, sessions, dental report). If nothing rejects the claim, each bill line is adjudicated: exclusion keywords, category procedure lists (with aliases), and pre-authorisation per line using that line's amount (the strictest matching rule wins, so "PET-CT" is treated as PET). Then the per-claim ceiling is checked on the eligible amount, and the payout waterfall runs per line: network discount, then co-pay (branded pharmacy items use the branded rate), then the patient's annual OPD limit and the family floater.

**Final decision.** Policy engine failure or a required document lost to a system fault → MANUAL_REVIEW. A policy rejection stays REJECTED (fraud cannot un-reject, but signals are still shown). Fraud escalation → MANUAL_REVIEW with the computed payout kept as `provisional_amount`. A fraud outage on a high-value claim → MANUAL_REVIEW. Review reasons from the policy engine → MANUAL_REVIEW. Otherwise APPROVED or PARTIAL as proposed.

**Confidence.** `base` is the mean extraction confidence of the usable documents. Every trace event that should lower confidence carries a `confidence_factor`; the score is `base × Π factors`, and `confidence_breakdown` lists each factor with its reason, so the number is reconstructable by hand. Factors have a scope. `ALL` factors (a component failed, a clinical match was keyword-based, a document was partly unreadable) apply to every decision. `PAYOUT` factors (no patient name on any document, an unverified registration number) only apply when money may be paid, because they do not weaken a rejection on a permanent exclusion. TC011 shows this working: 0.95 × 0.85 (no names) × 0.70 (fraud check failed) = 0.565, against 0.807 for the same claim without the failure.

**Messages.** Every decision has two texts: `reason` for operations (codes, amounts, policy references) and `member_message` in plain language with what happens next. Manual-review messages never reveal fraud heuristics to the claimant.

## 4. How the LLM is used

The model is used where only a model can do the job, and nowhere else.

- **Provider-agnostic.** The extractor talks to any OpenAI-compatible Chat Completions endpoint. Gemini is the default (Google's compatibility endpoint); OpenAI is one setting away. Because providers differ in which structured-output modes they accept, the client steps down from strict JSON schema to JSON mode to prompt-only JSON on a 400 and records which mode was used; validation and repair are the same in every mode.
- **One call per document does three jobs**: classify the document by what it is (not what the member said), assess readability, and extract fields. Pages of a PDF are rendered (pypdfium2, no system dependencies) and sent together so multi-page bills are aggregated.
- **Output is structured and validated.** The request uses the provider's strict JSON-schema mode (every field required, nullable where a document may not show it, no extra keys, enums for type, readability and quality flags), so the model cannot return an off-schema object. If an endpoint rejects strict mode, the client falls back to JSON-object mode once and records that in the trace. The response is then parsed into Pydantic models that tolerate real-world formats ("₹1,500/-", "01-Nov-2024", `diagnosis` as a string instead of a list). A bill line without a readable amount is dropped and flagged instead of failing the whole bill. Invalid output gets exactly one repair round-trip with the validation error fed back; after that it is a system failure.
- **Confidence is computed, not trusted.** Model self-reported confidence is poorly calibrated, so a live document's confidence is `0.3 × model score + 0.7 × completeness` (share of the critical fields for its type that were found; weights in `interpretation.json`). Both inputs and the missing fields are in the trace. Readability problems add a separate, visible factor.
- **Minimum evidence.** A document the model could read but that has none of its key fields (a prescription with no patient name, diagnosis or treatment; a bill with no amounts) goes back to the member rather than being adjudicated on empty data.
- **Reproducibility.** Every extraction event records the model, prompt version, schema mode, latency, token usage, attempts and whether a repair was needed.
- **Closed-vocabulary clinical codes.** The model also proposes `condition_tags` chosen from a vocabulary generated from the policy (waiting-period conditions and exclusion names); anything outside it is discarded. The policy engine matches conditions by keyword first. A tag **without** a keyword match never rejects a claim; it routes it to a human (`CLINICAL_CODING_UNCERTAIN`). The model can raise a question but cannot deny a claim on its own inference.
- **Failure handling.** Only retryable errors (timeouts, connection errors, 429, 5xx) are retried, with exponential backoff; the SDK's own retry loop is disabled so retries never nest. Documents are extracted concurrently under a semaphore, and the whole stage has a budget. Input problems (corrupt PDF, wrong file type, too large) become member actions; everything else becomes a system failure that degrades the claim.

## 5. Observability

The trace is the product for the operations team, so it was designed rather than accumulated.

- Every event has `component`, `rule_id`, `outcome` (PASS, FAIL, WARN, SKIP, NOT_EVALUABLE, ERROR, INFO), `effect` (BLOCK, REJECT, REVIEW, ADJUST_AMOUNT, ESCALATE, DEGRADE), a plain-English `summary`, the `policy_ref` or `interpretation_ref` it evaluated, structured `evidence`, amounts before and after for money changes, and an optional confidence factor.
- Rules that pass are logged, not just rules that fail, so a clean approval proves that waiting periods and exclusions were checked. Rules that cannot be evaluated say why (for example, the submission deadline when no submission date was supplied, or pre-existing conditions, which need medical history the system does not receive).
- The first event records the claim, the engine version and a fingerprint of the policy file, so a decision can be tied to the exact policy that produced it.
- If the pipeline itself crashes, the decision still carries every event recorded up to the failure.
- Application logs carry `claim=<id>` on every line, so logs and traces join on claim ID.

## 6. Alternatives considered and rejected

| Alternative | Why I rejected it |
|---|---|
| **LLM decides the claim** (give it policy + documents, ask for a decision). | Not reproducible, not auditable, and it can hallucinate a rule. It fails constraint 1. The LLM proposes clinical codes; code decides. |
| **Separate classification call before extraction**, to stop wrong documents even earlier. | Doubles latency and cost for every claim to save one call on a minority. The declared-type gate already stops obviously incomplete claims with zero model calls, and the combined call stops mis-typed documents before any policy work. |
| **OCR (Tesseract) + regex extraction.** | Breaks on handwriting, stamps and skewed photos, which the brief says are the norm. |
| **Fail fast on the first failing rule.** | Hides information from both the member and the reviewer. v1 did this; a claim rejected for the deadline could not also show it was outside the policy period. |
| **`sub_limit` as a hard per-claim cap.** | Contradicts the ground truth: TC010 approves ₹3,240 on a consultation whose sub-limit is ₹2,000. The adopted reading (ceiling = max(global per-claim limit, category sub-limit), applied to the eligible amount) is the only one consistent with TC006, TC008 and TC010. It is documented in `interpretation.json` with that reasoning and cited in the trace. |
| **Trust caller-supplied claim history.** | A caller could send an empty history and evade frequency checks. History is the union of the caller's list and the store. |
| **Honour `simulate_component_failure` unconditionally.** | Anyone could switch off the fraud check on their own claim. It is gated behind `ALLOW_FAULT_INJECTION`, which defaults to off and is logged either way. |
| **A workflow engine (Temporal, Celery) from day one.** | Right at scale (section 9) but unjustified for a single-process take-home; the orchestrator is written so stages map one-to-one onto workflow activities later. |

## 7. Security and trust boundaries

- **Caller-controlled inputs that could change a decision are gated.** `pre_extracted_data` (supplying extraction output directly) is accepted only when `ALLOW_FIXTURE_DOCUMENTS=true`; otherwise the API returns 422 and the extractor refuses the document as a second line of defence. `simulate_component_failure` works only when `ALLOW_FAULT_INJECTION=true`. Both default to off; the eval harness and the UI's test-case runner enable them for their own runs only.
- **Caller-supplied history cannot lower scrutiny.** Claim history is unioned with the store; year-to-date usage is the larger of the caller's figure and the store's.
- **The model cannot deny a claim.** Model-only clinical tags route to review, and model output is constrained by a strict schema and a closed vocabulary.
- **Document bytes are not retained** after extraction.
- **Not yet done (limitation):** there is no authentication or per-member access control on the API, so anyone who can reach it can read every claim's diagnoses. Medical data also appears in trace evidence and is kept in memory. Production needs auth (member and ops roles), masking of names and diagnoses in logs, and a retention policy.

## 8. Limitations of the current design

- **In-memory store.** Claim history, year-to-date usage and duplicate fingerprints live in process memory: lost on restart and not shared across replicas. The interface (`record`, `prior_claims`, `paid_between`, `find_by_fingerprints`) is narrow on purpose so it can be backed by Postgres without touching callers.
- **Keyword clinical matching.** Deterministic and auditable, but it will miss paraphrases the keyword lists do not cover, and the negation check is a simple prefix window (it will not understand "diabetes was excluded"). The closed-vocabulary model tags turn those misses into manual reviews rather than silent approvals, but the lists still need clinical ownership.
- **Not evaluated by design:** pre-existing conditions (needs medical history), pre-authorisation validity (needs the pre-auth issue date), yearly session totals across claims (needs per-category history). Each is logged NOT_EVALUABLE in every trace and listed in `interpretation.json` assumptions.
- **Per-category year-to-date spend is not tracked**, so sub-limits below the global per-claim limit are advisory warnings rather than enforced.
- **Synchronous request.** A claim with several documents holds the HTTP request open for the duration of the vision calls (seconds). Fine for a demo; wrong at scale.
- **Fixture-based eval.** The 12 test cases describe document content rather than ship images, so the eval exercises every stage except the vision call itself. The vision client is covered by unit tests with a scripted model; `scripts/run_live_samples.py` measures field-level accuracy on six rendered mock documents (clean, phone photo, handwritten shorthand, altered amount, two-page PDF), which is a smoke test rather than a benchmark on real claims.
- **Concurrency.** Two simultaneous claims from the same member both read the store before either writes, so a same-day frequency check or the annual limit can be evaded by racing requests. Acceptable in a single-process demo; at scale these checks become transactional in Postgres.

## 9. At 10x the current load

Today Plum processes about 75,000 claims a year; 10x is roughly 750,000 a year, about 2,000 a day on average with sharp peaks (month ends, post-holiday). Throughput is not the hard part; the vision calls, durability and operations are.

1. **Make processing asynchronous and durable.** `POST /claims` stores the submission and documents (object storage, e.g. S3, with encryption and a retention policy for medical data), enqueues a job and returns `202` with the claim ID. Workers run the pipeline. Each stage becomes a workflow activity (Temporal or a queue per stage) with its own retries and timeouts, so a worker crash resumes the claim instead of losing it. The member sees status updates; a stop for member action becomes a notification.
2. **Postgres behind `ClaimStore`.** Decisions, trace events (JSONB, append-only), fingerprints (unique index for duplicate detection) and a per-patient, per-category usage ledger, which also lets sub-limits and yearly session caps be enforced properly. Frequency checks become indexed queries.
3. **Treat the vision model as a constrained, expensive dependency.** A global concurrency limit and token budget per provider; a fallback model or provider when the primary degrades; a circuit breaker so an outage sends claims straight to a "waiting for extraction" state instead of burning retries; caching by document hash so resubmissions do not re-extract.
4. **Scale workers horizontally by stage.** Extraction is I/O-bound and bursty; policy and fraud are CPU-cheap. Separate worker pools let each scale on its own queue depth.
5. **Operate on the trace.** Ship trace events to the warehouse. Dashboards for decision mix, manual-review rate by reason code, confidence distribution, stage error rates and latency, and override rate (reviewer disagrees with the engine), which is the real accuracy metric. Alert on shifts.
6. **Close the loop on quality.** Sample decided claims for human audit, build a labelled document set from real (consented, de-identified) documents, and gate prompt or model changes on extraction accuracy against it. Version `interpretation.json` and require review for changes, since it is effectively policy code.
7. **Multi-policy.** At 10x there will be many policies. Load `PolicyConfig` by `policy_id` from a policy service with versioning, and store the version on every decision (the fingerprint already does this for one file).

## 10. What I would change with more time

- Replace keyword clinical matching with a reviewed mapping to ICD-10 codes (the model proposes codes, a curated table maps codes to policy conditions), keeping the rule that model output alone never rejects.
- Move the policy rules themselves into declarative rule definitions (condition, effect, reason template, policy path), so a new policy clause is a data change with a test, not a code change.
- An evaluation set of real, messy document images to measure extraction accuracy per field and per quality variation.
