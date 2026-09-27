"""HTTP API and UI host.

Endpoints
---------
GET  /                                  claim submission + decision review UI (static/index.html)
GET  /health                            liveness, policy fingerprint, roster integrity warnings, LLM configured?
GET  /api/v1/policy                     members, categories and document requirements (drives the UI form)
POST /api/v1/claims                     JSON ClaimSubmission -> ClaimDecision
POST /api/v1/claims/upload              multipart form with real images/PDFs -> ClaimDecision
GET  /api/v1/claims?limit=50            recent decisions, newest first
GET  /api/v1/claims/{claim_id}          one decision with its full trace
GET  /api/v1/test-cases                 the 12 cases from data/test_cases.json
POST /api/v1/test-cases/{case_id}/run   run one case through the real pipeline (isolated store)

Errors
------
422  invalid input, with a readable `detail` list naming each bad field
     (v1 returned 500 on a bad multipart date or amount)
413  an uploaded file is larger than MAX_UPLOAD_MB
415  an uploaded file is not an image or PDF
404  unknown claim or test case
Claim processing itself never returns 5xx: pipeline failures become MANUAL_REVIEW
decisions that keep the trace recorded up to the failure.

Startup is fail-fast: policy_terms.json and interpretation.json are parsed and
validated in the lifespan hook, so a broken config stops the service from booting
instead of silently running with {} (v1 behaviour).
"""

from __future__ import annotations

import base64
import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from src.core.config import (
    BASE_DIR, ENGINE_VERSION, config_fingerprint, get_interpretation, get_policy, get_settings, llm_api_key, llm_key_env,
    llm_model, load_test_cases,
)
from src.core.orchestrator import ClaimsOrchestrator
from src.core.storage import ClaimStore, claim_store
from src.evals.adapter import case_to_submission
from src.evals.checks import check_case
from src.models.claim import ClaimDecision, ClaimSubmission, DocumentSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("claims.api")

ALLOWED_UPLOAD_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "application/pdf"}
STATIC_DIR = BASE_DIR / "static"

# Test-case runs use their own store so demo runs never pollute real claim history
# (a repeated TC009 run would otherwise count as more same-day claims for EMP008).
test_run_store = ClaimStore(max_records=500)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    policy = get_policy()           # raises ConfigError -> service does not start
    get_interpretation()
    for warning in policy.integrity_warnings():
        logger.warning("policy integrity: %s", warning)
    app.state.orchestrator = ClaimsOrchestrator(settings=settings, store=claim_store)
    logger.info("policy %s loaded (fingerprint %s); live extraction %s; fault injection %s; fixture documents %s",
                policy.policy_id, config_fingerprint(settings.policy_file),
                f"ENABLED ({settings.llm_provider}, {llm_model(settings)})" if llm_api_key(settings)
                else f"DISABLED (no {llm_key_env(settings)})",
                "ENABLED" if settings.allow_fault_injection else "disabled",
                "ENABLED" if settings.allow_fixture_documents else "disabled")
    yield


app = FastAPI(title="Plum OPD Claims Engine", version=ENGINE_VERSION, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": _readable_errors(exc.errors())})


def _readable_errors(errors) -> List[str]:
    out = []
    for err in errors:
        loc = ".".join(str(p) for p in err.get("loc", []) if p not in ("body",))
        out.append(f"{loc or 'request'}: {err.get('msg')}")
    return out


def _orchestrator(request: Request) -> ClaimsOrchestrator:
    return request.app.state.orchestrator


# ------------------------------------------------------------------ health / metadata

@app.get("/health")
async def health():
    settings = get_settings()
    policy = get_policy()
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "policy_id": policy.policy_id,
        "policy_fingerprint": config_fingerprint(settings.policy_file),
        "interpretation_version": get_interpretation().version,
        "live_extraction_configured": bool(llm_api_key(settings)),
        "llm_provider": settings.llm_provider,
        "llm_model": llm_model(settings),
        "llm_key_env": llm_key_env(settings),
        "fault_injection_enabled": settings.allow_fault_injection,
        "policy_integrity_warnings": policy.integrity_warnings(),
    }


@app.get("/api/v1/policy")
async def policy_summary():
    policy = get_policy()
    return {
        "policy_id": policy.policy_id,
        "policy_name": policy.policy_name,
        "policy_period": [policy.policy_holder.policy_start_date, policy.policy_holder.policy_end_date],
        "categories": {
            cat: {"required": req.required, "optional": req.optional,
                  "sub_limit": policy.category_rules(cat).sub_limit if policy.category_rules(cat) else None}
            for cat, req in policy.document_requirements.items()
        },
        "members": [
            {"member_id": m.member_id, "name": m.name, "relationship": m.relationship,
             "dependents": [{"member_id": d, "name": (policy.member(d).name if policy.member(d) else None)}
                            for d in m.dependents]}
            for m in policy.members if m.relationship == "SELF"
        ],
        "network_hospitals": policy.network_hospitals,
    }


# ------------------------------------------------------------------ claims

@app.post("/api/v1/claims", response_model=ClaimDecision)
async def submit_claim(submission: ClaimSubmission, request: Request):
    """JSON path. Documents carry content_base64 / content_url (live extraction), or
    pre_extracted_data (fixture mode) when ALLOW_FIXTURE_DOCUMENTS=true."""
    fixtures = [d.label for d in submission.documents if d.pre_extracted_data is not None]
    if fixtures and not get_settings().allow_fixture_documents:
        raise HTTPException(422, detail=[
            f"documents: pre_extracted_data is not accepted in this environment ({', '.join(fixtures)}); "
            f"send the document content (content_base64 + mime_type, or content_url) so it can be read."])
    return await _orchestrator(request).process(submission)


@app.post("/api/v1/claims/upload", response_model=ClaimDecision)
async def submit_claim_upload(
    request: Request,
    member_id: str = Form(...),
    category: str = Form(...),
    treatment_date: date = Form(...),
    claimed_amount: str = Form(...),
    files: List[UploadFile] = File(...),
    file_types: List[str] = Form(...),
    patient_id: Optional[str] = Form(None),
    hospital_name: Optional[str] = Form(None),
    pre_auth_id: Optional[str] = Form(None),
    submission_date: Optional[date] = Form(None),
    claim_id: Optional[str] = Form(None),
):
    """Multipart path used by the UI: real images/PDFs, one `file_types` entry per file."""
    settings = get_settings()
    if len(file_types) != len(files):
        raise HTTPException(422, detail=[f"file_types: expected {len(files)} entries (one per file), got {len(file_types)}"])
    max_bytes = int(settings.max_upload_mb * 1_000_000)
    documents = []
    for i, (upload, doc_type) in enumerate(zip(files, file_types), start=1):
        mime = (upload.content_type or "").lower()
        if mime not in ALLOWED_UPLOAD_TYPES:
            raise HTTPException(415, detail=[f"'{upload.filename}': {mime or 'unknown type'} is not supported; "
                                             f"upload a JPG, PNG, WebP or PDF."])
        raw = await upload.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise HTTPException(413, detail=[f"'{upload.filename}' is larger than {settings.max_upload_mb:g} MB."])
        if not raw:
            raise HTTPException(422, detail=[f"'{upload.filename}' is empty."])
        documents.append(DocumentSource(file_id=f"F{i:03d}", file_name=upload.filename, file_type=doc_type,
                                        content_base64=base64.b64encode(raw).decode(), mime_type=mime))
    payload = {
        "member_id": member_id, "patient_id": patient_id or None, "category": category,
        "treatment_date": treatment_date, "claimed_amount": claimed_amount.replace(",", "").strip(),
        "hospital_name": hospital_name or None, "pre_auth_id": pre_auth_id or None,
        # The server stamps the submission date so the deadline rule is always evaluable for uploads.
        "submission_date": submission_date or date.today(), "documents": documents,
    }
    if claim_id:
        payload["claim_id"] = claim_id
    try:
        submission = ClaimSubmission.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, detail=_readable_errors(exc.errors()))
    return await _orchestrator(request).process(submission)


@app.get("/api/v1/claims", response_model=List[ClaimDecision])
async def list_claims(limit: int = 50):
    return claim_store.list_recent(min(max(limit, 1), 200))


@app.get("/api/v1/claims/{claim_id}", response_model=ClaimDecision)
async def get_claim(claim_id: str):
    decision = claim_store.get(claim_id) or test_run_store.get(claim_id)
    if decision is None:
        raise HTTPException(404, detail=[f"No claim found with id '{claim_id}'."])
    return decision


# ------------------------------------------------------------------ test cases (demo / eval)

@app.get("/api/v1/test-cases")
async def list_test_cases():
    return [{"case_id": c["case_id"], "case_name": c["case_name"], "description": c["description"],
             "expected": c["expected"]} for c in load_test_cases()]


@app.post("/api/v1/test-cases/{case_id}/run")
async def run_test_case(case_id: str):
    case = next((c for c in load_test_cases() if c["case_id"] == case_id.upper()), None)
    if case is None:
        raise HTTPException(404, detail=[f"Unknown test case '{case_id}'."])
    # Everything this runner submits comes from the server's own test_cases.json, not from the caller
    # (including TC011's simulated failure), so it may use fixtures and fault injection even when both
    # are off for the public claims endpoint.
    settings = get_settings().model_copy(update={"allow_fixture_documents": True, "allow_fault_injection": True})
    submission = case_to_submission(case)
    orchestrator = ClaimsOrchestrator(settings=settings, store=ClaimStore())
    decision = await orchestrator.process(submission)
    test_run_store.record(submission, decision)
    result = jsonable_encoder(decision)
    baseline = None
    if case.get("input", {}).get("simulate_component_failure"):
        baseline = jsonable_encoder(await ClaimsOrchestrator(settings=settings, store=ClaimStore())
                                    .process(case_to_submission(case, simulate_failure=False)))
    checks = check_case(case, result, baseline)
    return {"case": case, "decision": result, "passed": all(c.passed for c in checks),
            "checks": [c.__dict__ for c in checks]}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")
