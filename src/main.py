import base64
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from fastapi import FastAPI, Form, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles

from src.models.claim import ClaimSubmission, ClaimDecision, DocumentSource
from src.core.config import get_policy_config, get_interpretation_config, BASE_DIR
from src.core.orchestrator import ClaimOrchestrator
from src.core.storage import claim_store

app = FastAPI(title="Plum AI Claims Pipeline", version="2.0.0")

POLICY_CONFIG = get_policy_config()
INTERPRETATION_CONFIG = get_interpretation_config()
orchestrator = ClaimOrchestrator(POLICY_CONFIG, INTERPRETATION_CONFIG)


@app.get("/health")
async def health_check():
    return {"status": "operational", "policy_loaded": bool(POLICY_CONFIG)}


@app.post("/api/v1/claims", response_model=ClaimDecision)
async def process_claim(submission: ClaimSubmission):
    """JSON submission path. Documents carry either `pre_extracted_data`
    (fixture/eval mode — no LLM call) or `content_url` (live extraction).
    This is what scripts/run_evals.py and any programmatic client use."""
    return await _process_safely(submission)


@app.post("/api/v1/claims/upload", response_model=ClaimDecision)
async def submit_claim_with_files(
    claim_id: str = Form(...),
    member_id: str = Form(...),
    patient_id: str = Form(...),
    category: str = Form(...),
    treatment_date: str = Form(...),
    claimed_amount: str = Form(...),
    hospital_name: Optional[str] = Form(None),
    pre_auth_id: Optional[str] = Form(None),
    file_types: List[str] = Form(...),
    files: List[UploadFile] = File(...),
):
    """Real-world submission path: actual uploaded images/PDFs. This is what
    the UI's claim form uses. `file_types` must have one entry per file, in
    the same order (e.g. ["PRESCRIPTION", "HOSPITAL_BILL"])."""
    if len(file_types) != len(files):
        raise HTTPException(status_code=400, detail="file_types must have exactly one entry per uploaded file.")
    try:
        amount = Decimal(claimed_amount)
    except InvalidOperation:
        raise HTTPException(status_code=400, detail=f"claimed_amount '{claimed_amount}' is not a valid number.")

    documents = []
    for i, upload in enumerate(files):
        raw = await upload.read()
        documents.append(DocumentSource(
            file_id=upload.filename or f"file_{i}",
            file_type=file_types[i],
            content_base64=base64.b64encode(raw).decode("utf-8"),
            mime_type=upload.content_type or "image/jpeg",
        ))

    submission = ClaimSubmission(
        claim_id=claim_id, member_id=member_id, patient_id=patient_id, category=category,
        treatment_date=treatment_date, claimed_amount=amount, hospital_name=hospital_name,
        pre_auth_id=pre_auth_id, documents=documents,
    )
    return await _process_safely(submission)


@app.get("/api/v1/claims")
async def list_claims(limit: int = 50):
    """Decision review feed for the UI — most recent claims first."""
    return [record["decision"] for record in claim_store.list_recent(limit)]


@app.get("/api/v1/claims/{claim_id}", response_model=ClaimDecision)
async def get_claim(claim_id: str):
    record = claim_store.get(claim_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"No claim found with id '{claim_id}'.")
    return record["decision"]


async def _process_safely(submission: ClaimSubmission) -> ClaimDecision:
    """Last line of defense: even a genuine bug in the pipeline must not
    crash the request (assignment requirement: 'the system must not crash').
    Every agent already isolates its own faults via @isolate_fault; this is
    the outermost safety net in case something entirely unanticipated slips
    through the orchestrator itself."""
    try:
        return await orchestrator.process(submission)
    except Exception as e:
        return ClaimDecision(
            claim_id=submission.claim_id,
            decision="MANUAL_REVIEW",
            approved_amount=Decimal(0),
            claimed_amount=submission.claimed_amount,
            reason=f"Unexpected pipeline error ({type(e).__name__}); routed for manual review rather than failing the request.",
            confidence_score=0.1,
            manual_review_recommended=True,
            degraded_components=["PIPELINE"],
            trace=[],
        )


STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
