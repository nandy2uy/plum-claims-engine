# Plum AI Claims Processing Engine

An automated health-insurance OPD claims pipeline: document completeness check → structured
extraction → cross-document/member consistency check → deterministic policy adjudication →
fraud/risk scoring, with a fully reconstructable decision trace at every step.

## Architecture

```
Claim submission (JSON or file upload)
        │
        ▼
   DOC_GATE        — is the right set of document types present for this claim category?
        │  (blocks early with a specific, actionable message if not)
        ▼
   EXTRACTOR       — turns each document into structured data (fixture data for evals,
        │            live OpenAI vision extraction for real uploads). A single unreadable/
        │            failed document is flagged, not fatal to the batch.
        ▼
   CONSISTENCY     — do patient names agree across documents, and with the policy member
        │            on record?
        ▼
   POLICY_ENGINE   — deterministic Decimal-math adjudication against policy_terms.json:
        │            exclusions, waiting periods, pre-auth, per-claim limit, line-item
        │            exclusions, network discount, co-pay, annual OPD limit.
        ▼
   FRAUD           — same-day/monthly claim frequency, high-value thresholds.
        │
        ▼
   Confidence/degradation rollup → ClaimDecision (decision, amount, reason, full trace)
```

Every stage above appends to one shared `TraceLedger`; the API response's `trace` field
*is* that ledger, so any decision can be reconstructed from the response alone — see
`src/models/trace.py`.

Each agent in `src/agents/` documents its own input/output/error contract at the top of
its file (per the assignment's "component contracts" deliverable) — read those before
`src/core/orchestrator.py`, which is the thin pipeline wiring.

### Design decisions worth knowing before you read the code

- **`opd_categories.*.sub_limit` is advisory, not a hard cap.** It was implemented as a
  per-claim cap first, then reverted: TC010's own expected output (₹3,240 approved on a
  ₹2,000 consultation sub-limit) falsifies that reading. Correctly enforcing it needs
  per-category year-to-date tracking this system doesn't have. It's now a non-blocking
  `WARN` trace event + a `warnings` entry on the response instead of a guess. See the
  docstring at the top of `src/agents/policy_engine.py`.
- **Fraud frequency checks prefer caller-supplied `claims_history`** (for deterministic
  eval scenarios) **and fall back to the in-memory `ClaimStore`** for real traffic. A
  claim that never passed the document gate does not count toward another claim's
  same-day/monthly frequency — see `src/core/storage.py`.
- **`simulate_component_failure` is an explicit, documented field on the request**, not a
  hardcoded match against a specific claim ID. Any caller can exercise graceful
  degradation; it triggers a simulated failure in the (non-critical) fraud check, and the
  pipeline keeps going with lowered confidence and `manual_review_recommended=true`
  rather than changing the underlying decision.
- **Keyword matching uses word boundaries**, not raw substrings — `"herniation"` does not
  match the `"hernia"` waiting-period keyword. Naive substring matching was a real bug
  caught while verifying this against TC007.
- **Live document extraction supports images (JPEG/PNG) via OpenAI vision.** PDF support
  is not implemented — a scanned PDF would need a page-render step (e.g. `pdf2image` +
  poppler) that adds a system-level dependency out of scope here. This is a known,
  documented gap, not a silent one.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env               # fill in OPENAI_API_KEY to enable live extraction;
                                    # everything else (fixture-based evals, the UI's
                                    # graceful-degradation behavior) works without it
uvicorn src.main:app --reload
```

Open http://127.0.0.1:8000/ for the UI (claim submission + decision/trace review).
API docs: http://127.0.0.1:8000/docs.

## Running the evals

```bash
# In-process, no server needed, also generates EVAL_REPORT.md:
python -m scripts.generate_eval_report

# Against a live server (exercises the real HTTP path):
uvicorn src.main:app &
python scripts/run_evals.py
```

## Tests

```bash
pytest -q
```

Covers every agent in isolation plus full end-to-end runs of all 12 assignment test
cases, including regression tests for three bugs found while building this (see test
docstrings in `tests/`): the original crash on real (non-fixture) document submissions,
a false-positive keyword match, and a cross-test fraud-store contamination issue.

## API

- `POST /api/v1/claims` — JSON body (`ClaimSubmission`); documents carry either
  `pre_extracted_data` (fixture/eval mode, no LLM call) or `content_url` (live
  extraction). Used by the eval scripts.
- `POST /api/v1/claims/upload` — multipart form; real file uploads. Used by the UI.
- `GET /api/v1/claims` — recent decisions (decision review feed).
- `GET /api/v1/claims/{claim_id}` — one claim's full decision + trace.
- `GET /health`

## Known limitations

Documented in `data/interpretation.json` under `assumptions`, and inline in
`src/agents/policy_engine.py`:
- Pre-existing-condition waiting periods (365 days) aren't enforced — distinguishing a
  genuinely pre-existing condition from a newly diagnosed one needs medical history this
  system doesn't have.
- Branded vs. generic drug classification for pharmacy co-pay isn't available from
  extraction (needs a drug database lookup); the flat co-pay rate is used with a `WARN`
  flag instead.
- `pre_authorization.validity_days` can't be checked without a pre-auth issuance
  timestamp, which no input in this system provides.
- The in-memory `ClaimStore` is process-local and resets on restart — see "at 10x load"
  in the architecture document for what replaces it.
