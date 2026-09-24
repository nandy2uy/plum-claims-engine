from fastapi import FastAPI
from src.models.claim import ClaimSubmission, ClaimDecision
from src.models.trace import TraceLedger, TraceEvent
from src.core.config import get_policy_config
from src.agents.doc_gate import DocumentGateAgent
from src.agents.extractor import ExtractionAgent
from src.agents.policy_engine import PolicyEngineAgent
from src.agents.consistency import ConsistencyAgent
from src.agents.fraud import FraudAgent
from decimal import Decimal
import time

app = FastAPI(title="Plum AI Claims Pipeline", version="1.0.0")

# Load configs and initialize agents
POLICY_CONFIG = get_policy_config()
doc_gate_agent = DocumentGateAgent(policy_config=POLICY_CONFIG)
extractor_agent = ExtractionAgent()
consistency_agent = ConsistencyAgent()
fraud_agent = FraudAgent(policy_config=POLICY_CONFIG)
policy_agent = PolicyEngineAgent(policy_config=POLICY_CONFIG)

@app.get("/health")
async def health_check():
    return {"status": "operational", "policy_loaded": bool(POLICY_CONFIG)}

@app.post("/api/v1/claims/process", response_model=ClaimDecision)
async def process_claim(submission: ClaimSubmission):
    start_time = time.time()
    ledger = TraceLedger()
    
    # 1. Intake
    ledger.append(TraceEvent(seq=1, component="INTAKE", outcome="PASS", effect="NONE", evidence={"member_id": submission.member_id}, duration_ms=1))
    
    # 2. Document Gate
    passed_gate, gate_payload = doc_gate_agent.evaluate(submission, ledger)
    if not passed_gate:
        return _build_response(submission, ledger, gate_payload["decision"], Decimal(0), gate_payload["message"])
    
    # 3. Extraction
    passed_ext, extractions = await extractor_agent.process(submission, ledger)
    if not passed_ext:
        bad_file = extractions[0]["file_id"]
        return _build_response(submission, ledger, "NEEDS_MEMBER_ACTION", Decimal(0), f"Document ({bad_file}) is unreadable.")
        
    # 4. Consistency Check (New!)
    is_consistent, consistency_msg = consistency_agent.evaluate(extractions, ledger)
    if not is_consistent:
        return _build_response(submission, ledger, "NEEDS_MEMBER_ACTION", Decimal(0), consistency_msg)
        
    # 5. Policy Adjudication
    decision, approved_amount, reason = policy_agent.evaluate(submission, extractions, ledger)
    
    # 6. Fraud & Risk Check (New!)
    fraud_signals = fraud_agent.evaluate(submission, ledger)
    if fraud_signals:
        decision = "MANUAL_REVIEW"
        reason = f"Flagged for manual review: {', '.join(fraud_signals)}"
        approved_amount = Decimal(0)
    
    # Calculate System Health
    degraded = ledger.get_degraded_components()
    confidence = 1.0 if not degraded else 0.5
    
    if degraded and decision in ["APPROVED", "PARTIAL"]:
        decision = "MANUAL_REVIEW"
        reason = "System degraded during processing. Routing for human review."
        approved_amount = Decimal(0)

    return _build_response(submission, ledger, decision, approved_amount, reason, confidence, bool(degraded or fraud_signals))

def _build_response(submission, ledger, decision, approved, reason, confidence=1.0, review_rec=False):
    return ClaimDecision(
        claim_id=submission.claim_id,
        decision=decision,
        approved_amount=approved,
        claimed_amount=submission.claimed_amount,
        reason=reason,
        confidence_score=confidence,
        manual_review_recommended=review_rec,
        degraded_components=ledger.get_degraded_components(),
        trace=ledger.events
    )