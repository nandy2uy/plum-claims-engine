from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Literal
from decimal import Decimal
from datetime import datetime, timezone
from src.models.trace import TraceEvent

DecisionType = Literal["APPROVED", "PARTIAL", "REJECTED", "MANUAL_REVIEW", "NEEDS_MEMBER_ACTION"]
LineItemStatus = Literal["APPROVED", "REJECTED"]


class ClaimHistoryEntry(BaseModel):
    """A prior claim for the same member, supplied as ground truth for fraud-frequency
    checks. In production this would come from our own claim store (see
    src/core/storage.py) rather than being trusted from the caller; it is accepted
    here explicitly so the eval harness can reproduce specific fraud scenarios
    deterministically."""

    claim_id: str
    date: str
    amount: Decimal
    provider: Optional[str] = None


class DocumentSource(BaseModel):
    file_id: str
    file_type: str
    content_url: Optional[str] = None
    # Base64-encoded raw bytes for a real uploaded image/PDF (populated by the
    # multipart /upload endpoint). Mutually exclusive with content_url in practice,
    # but both are supported as alternate ways of pointing at the same bytes.
    content_base64: Optional[str] = None
    mime_type: Optional[str] = None
    # Lets us seamlessly pass the test_cases.json fixtures during evals without
    # spending API credits or needing real files.
    pre_extracted_data: Optional[dict] = None

    @field_validator("file_type")
    @classmethod
    def normalize_file_type(cls, v: str) -> str:
        return v.strip().upper().replace(" ", "_")


class ClaimSubmission(BaseModel):
    claim_id: str
    member_id: str
    patient_id: str
    category: str
    treatment_date: str
    claimed_amount: Decimal
    hospital_name: Optional[str] = None
    pre_auth_id: Optional[str] = None
    policy_id: Optional[str] = None
    documents: List[DocumentSource]

    # Optional signals used for checks that require state we don't otherwise
    # persist across requests in this exercise. See src/agents/fraud.py and
    # src/agents/policy_engine.py component contracts for how each is used,
    # and data/interpretation.json for the documented assumptions.
    submission_date: Optional[str] = None
    ytd_claims_amount: Optional[Decimal] = None
    claims_history: Optional[List[ClaimHistoryEntry]] = None

    # Explicit, documented fault-injection hook. Any caller (not just a test
    # harness matching a magic claim_id) can request a simulated non-critical
    # component failure to exercise graceful-degradation behavior.
    simulate_component_failure: bool = False

    @field_validator("category")
    @classmethod
    def normalize_category(cls, v: str) -> str:
        return v.strip().upper().replace(" ", "_").replace("-", "_")


class LineItemDecision(BaseModel):
    description: str
    amount: Decimal
    status: LineItemStatus
    reason: Optional[str] = None


class FinancialBreakdown(BaseModel):
    raw_eligible_amount: Decimal
    after_network_discount: Decimal
    after_copay: Decimal
    after_sub_limit: Decimal
    after_annual_limit: Decimal
    network_discount_applied: bool = False
    network_discount_percent: Decimal = Decimal(0)
    copay_percent: Decimal = Decimal(0)


class ClaimDecision(BaseModel):
    claim_id: str
    decision: DecisionType
    approved_amount: Decimal
    claimed_amount: Decimal
    reason: str
    confidence_score: float
    manual_review_recommended: bool
    degraded_components: List[str]
    fraud_signals: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    line_item_breakdown: List[LineItemDecision] = Field(default_factory=list)
    financial_breakdown: Optional[FinancialBreakdown] = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trace: List[TraceEvent]
