"""API contract: what a claim submission looks like and what the system returns.

Key changes vs v1 (each fixes an audited defect):
- treatment_date / submission_date are real `date`s. v1 used strings and every
  rule wrapped strptime in `except ValueError: return None`, so "15-10-2024"
  silently skipped waiting periods and approved a claim inside the waiting period.
- claimed_amount must be > 0; file_ids must be unique; patient_id defaults to
  member_id; claim_id is generated server-side when omitted.
- `decision` is one of the four decisions the assignment allows, or null when
  the pipeline stopped early for member action (test_cases.json expects null for
  TC001-TC003). v1 invented a fifth decision value, NEEDS_MEMBER_ACTION.
  That state now lives in `status`.
- Structured `rejection_reasons` / `review_reasons` codes, a member-facing
  `member_message`, and machine-readable `required_actions`, instead of reason
  codes embedded in a free-text string.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from src.models.trace import TraceEvent

DecisionType = Literal["APPROVED", "PARTIAL", "REJECTED", "MANUAL_REVIEW"]
ClaimStatus = Literal["DECIDED", "NEEDS_MEMBER_ACTION"]
LineItemStatus = Literal["APPROVED", "REJECTED", "NEEDS_REVIEW"]
ActionType = Literal[
    "UPLOAD_DOCUMENT", "REUPLOAD_DOCUMENT", "REPLACE_DOCUMENT", "CORRECT_DETAILS",
    "OBTAIN_PRE_AUTH", "CONTACT_SUPPORT",
]
FAULT_INJECTABLE_COMPONENTS = ("EXTRACTOR", "DOC_VERIFY", "CONSISTENCY", "POLICY_ENGINE", "FRAUD")


def _norm_code(v: str) -> str:
    return v.strip().upper().replace(" ", "_").replace("-", "_")


class ClaimHistoryEntry(BaseModel):
    claim_id: str
    date: date
    amount: Decimal = Field(ge=0)
    provider: Optional[str] = None


class DocumentSource(BaseModel):
    file_id: str
    file_type: str  # the type the MEMBER declared; the extractor detects the actual type
    file_name: Optional[str] = None
    content_url: Optional[str] = None
    content_base64: Optional[str] = None
    mime_type: Optional[str] = None
    # Fixture/eval mode: the extraction output, supplied directly (no LLM call).
    pre_extracted_data: Optional[dict] = None

    _normalize = field_validator("file_type")(classmethod(lambda cls, v: _norm_code(v)))

    @property
    def label(self) -> str:
        return self.file_name or self.file_id


class ClaimSubmission(BaseModel):
    claim_id: str = Field(default_factory=lambda: f"CLM-{uuid4().hex[:10].upper()}")
    member_id: str
    patient_id: Optional[str] = None
    policy_id: Optional[str] = None
    category: str
    treatment_date: date
    claimed_amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    hospital_name: Optional[str] = None
    pre_auth_id: Optional[str] = None
    documents: List[DocumentSource] = Field(default_factory=list)

    submission_date: Optional[date] = None
    ytd_claims_amount: Optional[Decimal] = Field(default=None, ge=0)
    claims_history: Optional[List[ClaimHistoryEntry]] = None

    # Fault injection — honoured only when settings.allow_fault_injection is on.
    simulate_component_failure: bool = False
    simulate_failure_component: str = "FRAUD"

    _normalize = field_validator("category")(classmethod(lambda cls, v: _norm_code(v)))

    @field_validator("simulate_failure_component")
    @classmethod
    def _known_component(cls, v: str) -> str:
        v = _norm_code(v)
        if v not in FAULT_INJECTABLE_COMPONENTS:
            raise ValueError(f"must be one of {', '.join(FAULT_INJECTABLE_COMPONENTS)}")
        return v

    @model_validator(mode="after")
    def _defaults_and_integrity(self) -> "ClaimSubmission":
        if not self.patient_id:
            self.patient_id = self.member_id
        ids = [d.file_id for d in self.documents]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"document file_id values must be unique; duplicated: {dupes}")
        return self


class MemberAction(BaseModel):
    action: ActionType
    detail: str
    file_id: Optional[str] = None
    document_type: Optional[str] = None


class LineItemDecision(BaseModel):
    description: str
    amount: Decimal
    status: LineItemStatus
    reason: Optional[str] = None
    reason_code: Optional[str] = None
    policy_ref: Optional[str] = None
    source_file_id: Optional[str] = None


class FinancialBreakdown(BaseModel):
    """Every step of the payout arithmetic, in the order it was applied."""

    claimed_amount: Decimal
    billed_amount: Decimal
    eligible_amount: Decimal
    network_hospital: Optional[str] = None
    network_discount_applied: bool = False
    network_discount_percent: Decimal = Decimal(0)
    network_discount_amount: Decimal = Decimal(0)
    after_network_discount: Decimal
    copay_percent: Decimal = Decimal(0)
    copay_amount: Decimal = Decimal(0)
    after_copay: Decimal
    annual_limit_remaining: Optional[Decimal] = None
    family_limit_remaining: Optional[Decimal] = None
    final_amount: Decimal
    steps: List[str] = Field(default_factory=list)


class ConfidenceFactor(BaseModel):
    component: str
    rule_id: Optional[str] = None
    factor: float
    reason: str


class ClaimDecision(BaseModel):
    claim_id: str
    status: ClaimStatus
    decision: Optional[DecisionType] = None
    approved_amount: Decimal = Decimal(0)
    provisional_amount: Optional[Decimal] = None  # computed payout held back by MANUAL_REVIEW
    claimed_amount: Decimal
    reason: str  # for the operations team
    member_message: str  # for the member: plain language, what happens next
    rejection_reasons: List[str] = Field(default_factory=list)
    review_reasons: List[str] = Field(default_factory=list)
    required_actions: List[MemberAction] = Field(default_factory=list)
    confidence_score: float
    confidence_breakdown: List[ConfidenceFactor] = Field(default_factory=list)
    manual_review_recommended: bool = False
    degraded_components: List[str] = Field(default_factory=list)
    fraud_signals: List[str] = Field(default_factory=list)
    fraud_score: Optional[float] = None
    warnings: List[str] = Field(default_factory=list)
    line_item_breakdown: List[LineItemDecision] = Field(default_factory=list)
    financial_breakdown: Optional[FinancialBreakdown] = None
    policy_id: Optional[str] = None
    engine_version: Optional[str] = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trace: List[TraceEvent]
