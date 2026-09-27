# Plum OPD claims engine

Automates health-insurance OPD claim adjudication: a member submits a claim with documents, and the system checks the documents, reads them with a vision model, applies the policy in `data/policy_terms.json`, and returns `APPROVED`, `PARTIAL`, `REJECTED` or `MANUAL_REVIEW` with the amount, the reason, a confidence score and a full decision trace. When the documents themselves are the problem, it stops before deciding and tells the member exactly what to fix.

- **[ARCHITECTURE.md](ARCHITECTURE.md)**: components, design reasoning, rejected alternatives, limitations, 10x plan
- **[COMPONENT_CONTRACTS.md](COMPONENT_CONTRACTS.md)**: inputs, outputs and errors for every component
- **[EVAL_REPORT.md](EVAL_REPORT.md)**: all 12 test cases with decisions, checks and full traces (12/12)
- **[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)**: outline for the demo video
- **docs/LIVE_EXTRACTION.md**: field-level results of the real vision model on the mock documents in `samples/` (generate it with your key, see below)

## Run it

Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional: add GEMINI_API_KEY for live document reading
uvicorn src.main:app --reload
```

Open http://127.0.0.1:8000. The **Test cases** tab runs any of the 12 assignment cases through the real pipeline and shows the decision, the check against the expected outcome, and the trace. The **New claim** tab uploads real images or PDFs (needs `GEMINI_API_KEY`, or `LLM_PROVIDER=openai` with `OPENAI_API_KEY`; without a key, uploads go to manual review with the reason shown). Any file in `samples/` works as an upload.

### Vision model

Gemini is the default (`LLM_PROVIDER=gemini`, `GEMINI_API_KEY`), called through Google's OpenAI-compatible endpoint with the `openai` SDK. To use OpenAI instead set `LLM_PROVIDER=openai` and `OPENAI_API_KEY`. `LLM_MODEL` overrides the model; check Google AI Studio for current model names. `/health` shows which provider and model are active.

## Test and evaluate

```bash
pytest                                  # 144 tests: every agent, the LLM client, the API, samples, and the 12 cases
python scripts/run_evals.py             # 12 cases in-process, pass/fail per check
python scripts/run_evals.py --url http://127.0.0.1:8000   # same, over HTTP (server needs ALLOW_FAULT_INJECTION=true and ALLOW_FIXTURE_DOCUMENTS=true)
python scripts/generate_eval_report.py  # rewrites EVAL_REPORT.md
```

### Live extraction on real images

The 12 test cases describe document content rather than ship images, so they run with fixture extractions. To measure the vision model itself:

```bash
python scripts/make_samples.py                       # renders 6 mock documents into samples/ (clean, phone photo, handwritten, altered, 2-page PDF)
python scripts/run_live_samples.py                    # uses GEMINI_API_KEY from .env; writes docs/LIVE_EXTRACTION.md and samples/live_results.json
```

You can also upload any file from `samples/` in the UI's **New claim** tab.

## API in one example

```bash
curl -s localhost:8000/api/v1/claims -H 'content-type: application/json' -d '{
  "member_id": "EMP010", "category": "CONSULTATION", "treatment_date": "2024-11-03",
  "claimed_amount": 4500, "hospital_name": "Apollo Hospitals",
  "documents": [
    {"file_id": "F1", "file_type": "PRESCRIPTION", "pre_extracted_data": {"prescription_data": {
       "patient_name": "Deepak Shah", "doctor_registration": "TN/56789/2013", "diagnosis": "Acute Bronchitis"}}},
    {"file_id": "F2", "file_type": "HOSPITAL_BILL", "pre_extracted_data": {"bill_data": {
       "patient_name": "Deepak Shah", "hospital_name": "Apollo Hospitals",
       "line_items": [{"description": "Consultation Fee", "amount": 1500}, {"description": "Medicines", "amount": 3000}]}}}
  ]}' | python -m json.tool
```

Returns `APPROVED`, ₹3,240, with the network discount (₹900) applied before the 10% co-pay (₹360). Documents normally carry `content_base64` + `mime_type` (or use the multipart `/api/v1/claims/upload`). `pre_extracted_data` is the fixture path the eval uses; the API only accepts it when `ALLOW_FIXTURE_DOCUMENTS=true` (it is in `.env.example` for local use, and must stay off in production).

## Layout

```
src/
  agents/      intake, doc_gate, extractor, doc_verifier, consistency, policy_engine, fraud, decision
  core/        orchestrator, fault_tolerance (run_stage), llm (vision client), storage, config, text helpers
  models/      claim (API contract), medical (extraction), policy (typed policy + interpretation), trace
  evals/       test-case adapter, strict checks, runner/report renderer, mock documents + field comparison
  main.py      FastAPI app
data/          policy_terms.json (insurer contract), interpretation.json (how ambiguous clauses are read), test_cases.json
static/        UI
tests/         pytest suite
scripts/       run_evals.py, generate_eval_report.py, make_samples.py, run_live_samples.py
samples/       mock documents (regenerate with scripts/make_samples.py) and live results
```

## Assumptions

Every judgment call about an ambiguous or unstructured policy clause is data in `data/interpretation.json` (with its rationale under `assumptions`) and is cited by the trace events that use it. The main ones:

- **Per-claim ceiling** is the larger of `coverage.per_claim_limit` and the category `sub_limit`, applied to the eligible amount. It is the only reading consistent with TC006, TC008 and TC010.
- **Clinical matching** uses whole-phrase keywords (plurals allowed) and is negation-aware: "No family history of diabetes" does not trigger the diabetes waiting period. The vision model also proposes condition tags from a vocabulary generated from the policy, but a tag without a keyword match only routes to manual review, never rejects.
- **No diagnosis, no auto-approval**: a category that requires a prescription with no readable diagnosis goes to manual review, and a live document that is readable but has none of its key fields is sent back to the member.
- **Extraction confidence** for live documents is computed from field completeness blended with the model's own score, not taken on trust.
- **Not evaluable** without data this system does not receive, and logged as such in every trace: pre-existing conditions, pre-authorisation validity window, session totals across claims.
- **Fault injection** (`simulate_component_failure`) only works when `ALLOW_FAULT_INJECTION=true`, and fixture documents only when `ALLOW_FIXTURE_DOCUMENTS=true`.
