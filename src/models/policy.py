"""Typed views over data/policy_terms.json and data/interpretation.json.

Why this exists: the first draft read policy values with `dict.get(key, default)`
everywhere. A typo in the JSON (or a renamed key) silently fell back to a default
— e.g. a missing `per_claim_limit` became 0 and would have rejected every claim.
Parsing the files into these models at startup means a malformed policy fails
loudly once, at boot, with a precise error, instead of mis-adjudicating claims.

`extra="allow"` keeps forward compatibility: unknown keys are preserved, not
rejected, so the insurer can add fields without breaking the engine.

policy_terms.json      = what the insurer's contract says (source of truth).
interpretation.json    = how this engine operationalises ambiguous/unstructured
                         parts of it (keyword lists, aliases, the per-claim-limit
                         reading, confidence weights). Kept separate so that every
                         judgment call is reviewable data, not buried in code, and
                         every trace event can cite the exact interpretation key.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


# ============================================================ policy_terms.json

class PolicyHolder(_Loose):
    company_name: str
    employee_count: Optional[int] = None
    policy_start_date: date
    policy_end_date: date
    renewal_status: str


class FamilyFloater(_Loose):
    enabled: bool = False
    combined_limit: Optional[Decimal] = None
    covered_relationships: List[str] = Field(default_factory=list)


class Coverage(_Loose):
    sum_insured_per_employee: Decimal
    annual_opd_limit: Decimal
    per_claim_limit: Decimal
    family_floater: FamilyFloater = Field(default_factory=FamilyFloater)


class CategoryRules(_Loose):
    covered: bool = True
    sub_limit: Decimal
    copay_percent: Decimal = Decimal(0)
    network_discount_percent: Decimal = Decimal(0)
    requires_prescription: bool = False
    requires_pre_auth: bool = False
    pre_auth_threshold: Optional[Decimal] = None
    high_value_tests_requiring_pre_auth: List[str] = Field(default_factory=list)
    branded_drug_copay_percent: Optional[Decimal] = None
    generic_mandatory: bool = False
    requires_dental_report: bool = False
    covered_procedures: List[str] = Field(default_factory=list)
    excluded_procedures: List[str] = Field(default_factory=list)
    covered_items: List[str] = Field(default_factory=list)
    excluded_items: List[str] = Field(default_factory=list)
    requires_registered_practitioner: bool = False
    max_sessions_per_year: Optional[int] = None
    covered_systems: List[str] = Field(default_factory=list)

    @property
    def covered_list(self) -> List[str]:
        return self.covered_procedures + self.covered_items

    @property
    def excluded_list(self) -> List[str]:
        return self.excluded_procedures + self.excluded_items


class WaitingPeriods(_Loose):
    initial_waiting_period_days: int = 0
    pre_existing_conditions_days: Optional[int] = None
    specific_conditions: Dict[str, int] = Field(default_factory=dict)


class Exclusions(_Loose):
    conditions: List[str] = Field(default_factory=list)
    dental_exclusions: List[str] = Field(default_factory=list)
    vision_exclusions: List[str] = Field(default_factory=list)


class PreAuthorization(_Loose):
    required_for: List[str] = Field(default_factory=list)
    validity_days: Optional[int] = None


class SubmissionRules(_Loose):
    deadline_days_from_treatment: int
    minimum_claim_amount: Decimal
    currency: str = "INR"


class DocumentRequirement(_Loose):
    required: List[str]
    optional: List[str] = Field(default_factory=list)


class FraudThresholds(_Loose):
    same_day_claims_limit: int
    monthly_claims_limit: int
    high_value_claim_threshold: Decimal
    auto_manual_review_above: Decimal
    fraud_score_manual_review_threshold: float


class Member(_Loose):
    member_id: str
    name: str
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    relationship: str
    join_date: Optional[date] = None
    dependents: List[str] = Field(default_factory=list)
    primary_member_id: Optional[str] = None


class PolicyConfig(_Loose):
    policy_id: str
    policy_name: str
    insurer: str
    policy_holder: PolicyHolder
    coverage: Coverage
    opd_categories: Dict[str, CategoryRules]
    waiting_periods: WaitingPeriods
    exclusions: Exclusions
    pre_authorization: PreAuthorization = Field(default_factory=PreAuthorization)
    network_hospitals: List[str] = Field(default_factory=list)
    submission_rules: SubmissionRules
    document_requirements: Dict[str, DocumentRequirement]
    fraud_thresholds: FraudThresholds
    members: List[Member]

    _index: Dict[str, Member] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context) -> None:
        self._index = {m.member_id: m for m in self.members}

    def member(self, member_id: Optional[str]) -> Optional[Member]:
        return self._index.get(member_id or "")

    def category_rules(self, category: str) -> Optional[CategoryRules]:
        return self.opd_categories.get(category.lower())

    def integrity_warnings(self) -> List[str]:
        """Roster/config inconsistencies worth surfacing at boot (and on /health)."""
        warnings = []
        for m in self.members:
            for dep in m.dependents:
                if dep not in self._index:
                    warnings.append(f"{m.member_id} lists dependent {dep}, which has no roster record.")
            if m.primary_member_id and m.primary_member_id not in self._index:
                warnings.append(f"{m.member_id} references unknown primary member {m.primary_member_id}.")
        for category in self.document_requirements:
            if category.lower() not in self.opd_categories:
                warnings.append(f"document_requirements.{category} has no matching opd_categories entry.")
        return warnings


# ============================================================ interpretation.json

class ExclusionRule(BaseModel):
    keywords: List[str]
    on_match: Literal["REJECT", "REVIEW"] = "REJECT"


class PreAuthRule(BaseModel):
    test: str
    keywords: List[str]
    always_required: bool = False


class PerClaimLimitRule(BaseModel):
    rule: Literal["max_of_global_and_category_sub_limit", "global_only"]
    applied_to: Literal["eligible_amount", "claimed_amount"] = "eligible_amount"
    rationale: str = ""


class ConfidenceWeights(BaseModel):
    """Multiplicative confidence factors. Each one that fires is recorded as a
    trace event, so the final confidence score is reconstructable from the trace."""

    fixture_extraction: float = 0.95
    partially_unreadable: float = 0.85
    no_extraction_performed: float = 0.99
    missing_registration: float = 0.90
    invalid_registration: float = 0.85
    identity_unverified: float = 0.85
    fuzzy_name_match: float = 0.95
    partial_name_match: float = 0.85
    keyword_clinical_match: float = 0.97
    document_date_mismatch: float = 0.90
    degraded_component: float = 0.70
    unlisted_procedure: float = 0.95
    bill_total_mismatch: float = 0.90
    missing_supporting_report: float = 0.95
    medicine_condition_hint: float = 0.90
    type_mismatch_accepted: float = 0.95
    fraud_soft_score_weight: float = 0.50
    llm_only_clinical_code: float = 0.90
    diagnosis_missing: float = 0.85
    extraction_model_weight: float = Field(default=0.30, ge=0.0, le=1.0)


class Interpretation(_Loose):
    version: str
    per_claim_limit: PerClaimLimitRule
    relationship_aliases: Dict[str, str] = Field(default_factory=dict)
    waiting_period_condition_keywords: Dict[str, List[str]]
    medicine_condition_hints: Dict[str, List[str]] = Field(default_factory=dict)
    exclusion_condition_keywords: Dict[str, ExclusionRule]
    procedure_aliases: Dict[str, List[str]] = Field(default_factory=dict)
    pre_auth_rules: List[PreAuthRule]
    medical_abbreviations: Dict[str, str] = Field(default_factory=dict)
    negation_cues: List[str] = Field(default_factory=list)
    negation_window_words: int = 5
    registration_patterns: Dict[str, str]
    alternative_medicine_registration_patterns: List[str] = Field(default_factory=list)
    alternative_medicine_system_keywords: Dict[str, List[str]] = Field(default_factory=dict)
    session_count_pattern: str = r"(\d+)\s*sessions?"
    document_type_descriptions: Dict[str, str] = Field(default_factory=dict)
    extraction_minimum_evidence: Dict[str, List[str]] = Field(default_factory=dict)
    extraction_critical_fields: Dict[str, List[str]] = Field(default_factory=dict)
    document_date_tolerance_days: int = 7
    name_match_fuzzy_threshold: float = 0.85
    claim_to_bill_inflation_tolerance: Decimal = Decimal("1.10")
    fraud_signal_weights: Dict[str, float] = Field(default_factory=dict)
    confidence: ConfidenceWeights = Field(default_factory=ConfidenceWeights)
    assumptions: Dict[str, str] = Field(default_factory=dict)

    def aliases_for(self, canonical: str) -> List[str]:
        return [canonical] + self.procedure_aliases.get(canonical, [])

    def describe_document(self, doc_type: str) -> str:
        return self.document_type_descriptions.get(doc_type, doc_type.replace("_", " ").lower())
