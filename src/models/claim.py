from pydantic import BaseModel
from typing import List, Optional, Literal
from decimal import Decimal
from src.models.trace import TraceEvent

class DocumentSource(BaseModel):
    file_id: str
    file_type: str
    content_url: Optional[str] = None
    # This lets us seamlessly pass the test_cases.json fixtures during evals
    pre_extracted_data: Optional[dict] = None  

class ClaimSubmission(BaseModel):
    claim_id: str
    member_id: str
    patient_id: str
    category: str
    treatment_date: str 
    claimed_amount: Decimal
    hospital_name: Optional[str] = None
    pre_auth_id: Optional[str] = None
    documents: List[DocumentSource]

class ClaimDecision(BaseModel):
    claim_id: str
    decision: Literal["APPROVED", "PARTIAL", "REJECTED", "MANUAL_REVIEW", "NEEDS_MEMBER_ACTION"]
    approved_amount: Decimal
    claimed_amount: Decimal
    reason: str
    confidence_score: float
    manual_review_recommended: bool
    degraded_components: List[str]
    trace: List[TraceEvent]