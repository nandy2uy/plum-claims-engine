"""Deterministic policy adjudication.

Component contract
-------------------
Input:  ClaimSubmission, List[DocumentExtraction], TraceLedger.
Output: PolicyResult(decision, approved_amount, reason, line_items,
        financial_breakdown, warnings). `decision` is one of APPROVED,
        PARTIAL, REJECTED, MANUAL_REVIEW (unknown member).
Errors: none raised — every branch here is a normal, tracked outcome. There
        is nothing probabilistic in this component; it is pure Decimal
        arithmetic and policy_terms.json/interpretation.json lookups, which
        is why it isn't wrapped in @isolate_fault like the LLM-touching
        agents are (a bug here is a bug, not a transient fault to degrade
        around).

Every branch appends a TraceEvent with a `policy_ref` pointing at the exact
policy_terms.json path it evaluated, before returning — this is the fix for
the previous version's biggest observability gap (this agent used to return
without writing to the ledger at all).

Design note on `opd_categories.*.sub_limit`: this was initially implemented
as a hard per-claim cap, then reverted after checking it against the
assignment's own ground truth. TC010 (network discount test) expects
₹3,240 approved on a `consultation` sub_limit of ₹2,000 — so sub_limit
cannot mean "per-claim ceiling" in this policy's intended reading, and
capping on it would silently misadjudicate a real approval. Enforcing it
correctly would need per-category year-to-date tracking, which nothing in
this system's inputs provides (only a blanket, category-agnostic
`ytd_claims_amount`). Rather than guess, it's surfaced as a non-blocking WARN
for a human reviewer. `coverage.annual_opd_limit`, by contrast, IS enforced
as a hard cap against `ytd_claims_amount` — no test case's numbers contradict
that reading, and it's the one limit this system has the right granularity
of input to apply safely.
"""

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from src.models.claim import ClaimSubmission, LineItemDecision, FinancialBreakdown
from src.models.medical import DocumentExtraction, ExtractedBill, ExtractedPrescription
from src.models.trace import TraceLedger

CENTS = Decimal("0.01")


@dataclass
class PolicyResult:
    decision: str
    approved_amount: Decimal
    reason: str
    line_items: List[LineItemDecision] = field(default_factory=list)
    financial_breakdown: Optional[FinancialBreakdown] = None
    warnings: List[str] = field(default_factory=list)


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def _keyword_match(text: str, keywords: List[str]) -> bool:
    """Whole-phrase, word-boundary match — plain substring matching would let
    'hernia' match inside 'herniation' (a disc problem, not the hernia
    condition with its own 365-day wait), which is a real false positive this
    was built to fix."""
    return any(re.search(r"\b" + re.escape(kw) + r"\b", text) for kw in keywords)


class PolicyEngineAgent:
    def __init__(self, policy_config: Dict[str, Any], interpretation_config: Dict[str, Any]):
        self.config = policy_config
        self.interpretation = interpretation_config.get("assumptions", {})
        self.members = {m["member_id"]: m for m in policy_config.get("members", [])}
        self.network_hospitals = [h.lower() for h in policy_config.get("network_hospitals", [])]

    def evaluate(self, submission: ClaimSubmission, extractions: List[DocumentExtraction], ledger: TraceLedger) -> PolicyResult:
        start_time = time.time()
        category = submission.category
        cat_key = category.lower()
        cat_rules = self.config.get("opd_categories", {}).get(cat_key, {})

        bill_data, prescription_data = self._merge_extractions(extractions)
        combined_text = self._combined_text(prescription_data)

        if cat_rules and not cat_rules.get("covered", True):
            return self._reject(ledger, start_time, "category_not_covered",
                                 f"{category.replace('_', ' ').title()} is not a covered category under this policy.",
                                 policy_ref=f"opd_categories.{cat_key}.covered")

        min_amount = Decimal(str(self.config.get("submission_rules", {}).get("minimum_claim_amount", 0)))
        if submission.claimed_amount < min_amount:
            return self._reject(ledger, start_time, "minimum_claim_amount",
                                 f"Claimed amount ₹{submission.claimed_amount} is below the minimum claimable amount of ₹{min_amount}.",
                                 policy_ref="submission_rules.minimum_claim_amount")

        deadline_check = self._check_submission_deadline(submission)
        if deadline_check:
            return self._reject(ledger, start_time, "submission_deadline", deadline_check,
                                 policy_ref="submission_rules.deadline_days_from_treatment")

        member = self.members.get(submission.patient_id) or self.members.get(submission.member_id)
        if not member:
            ledger.log(
                component="POLICY_ENGINE", rule_id="member_lookup", outcome="FAIL", effect="ESCALATE",
                evidence={"member_id": submission.member_id, "patient_id": submission.patient_id},
                duration_ms=int((time.time() - start_time) * 1000),
            )
            return PolicyResult(
                decision="MANUAL_REVIEW", approved_amount=Decimal(0),
                reason="Member/patient could not be matched to a policy record; routing for manual eligibility verification.",
            )

        covered_relationships = self.config.get("coverage", {}).get("family_floater", {}).get("covered_relationships", [])
        if covered_relationships and member.get("relationship") not in covered_relationships:
            return self._reject(ledger, start_time, "relationship_not_covered",
                                 f"'{member.get('relationship')}' is not a covered relationship under this policy's family floater.",
                                 policy_ref="coverage.family_floater.covered_relationships",
                                 evidence={"relationship": member.get("relationship")})

        # Exclusions are checked before waiting periods deliberately: policy_terms.json
        # lists "obesity_treatment"/"bariatric" under both a specific_conditions waiting
        # period AND an outright exclusion. A permanent exclusion should win — there's no
        # point telling a member "you're covered after 365 days" for something that's
        # actually never covered. (Verified against TC012's ground truth, which expects
        # EXCLUDED_CONDITION, not WAITING_PERIOD, for exactly this overlap.)
        exclusion_check = self._check_exclusions(combined_text, ledger, start_time)
        if exclusion_check:
            return exclusion_check

        waiting_check = self._check_waiting_periods(submission, member, combined_text, ledger, start_time)
        if waiting_check:
            return waiting_check

        if cat_key == "diagnostic":
            preauth_check = self._check_diagnostic_pre_auth(submission, cat_rules, combined_text, ledger, start_time)
            if preauth_check:
                return preauth_check

        applies_to = self.interpretation.get("per_claim_limit_applies_to", [])
        if cat_key in applies_to:
            per_claim_limit = Decimal(str(self.config.get("coverage", {}).get("per_claim_limit", 0)))
            if submission.claimed_amount > per_claim_limit:
                return self._reject(ledger, start_time, "per_claim_exceeded",
                                     f"Claimed amount ₹{submission.claimed_amount} exceeds the per-claim limit of ₹{per_claim_limit}.",
                                     policy_ref="coverage.per_claim_limit",
                                     evidence={"claimed_amount": str(submission.claimed_amount), "limit": str(per_claim_limit)})

        line_items, eligible_amount, line_items_rejected = self._adjudicate_line_items(
            submission, bill_data, cat_key, cat_rules, ledger, start_time
        )
        warnings = self._quality_warnings(bill_data, cat_key, cat_rules, extractions)

        hospital_name = (submission.hospital_name or (bill_data.hospital_name if bill_data else "") or "").lower()
        is_network = bool(hospital_name) and any(net in hospital_name for net in self.network_hospitals)
        discount_pct = Decimal(str(cat_rules.get("network_discount_percent", 0))) if is_network else Decimal(0)
        after_discount = eligible_amount * (Decimal(1) - discount_pct / Decimal(100))

        copay_pct = Decimal(str(cat_rules.get("copay_percent", 0)))
        after_copay = after_discount * (Decimal(1) - copay_pct / Decimal(100))

        sub_limit = cat_rules.get("sub_limit")
        if sub_limit is not None and after_copay > Decimal(str(sub_limit)):
            warnings.append(
                f"Post-discount/copay amount (₹{_q(after_copay)}) exceeds the {category.replace('_', ' ').title()} "
                f"category's stated sub-limit of ₹{sub_limit}. Not auto-capped — correctly enforcing sub_limit "
                f"requires per-category year-to-date tracking this system doesn't have; flagged for reviewer awareness."
            )
            ledger.log(
                component="POLICY_ENGINE", rule_id="sub_limit_advisory", outcome="WARN", effect="NONE",
                policy_ref=f"opd_categories.{cat_key}.sub_limit",
                evidence={"amount": str(_q(after_copay)), "sub_limit": str(sub_limit)},
                duration_ms=int((time.time() - start_time) * 1000),
            )

        ytd = submission.ytd_claims_amount or Decimal(0)
        annual_limit = Decimal(str(self.config.get("coverage", {}).get("annual_opd_limit", 0)))
        remaining_annual = annual_limit - ytd
        annual_capped = remaining_annual < after_copay
        after_annual_limit = max(Decimal(0), remaining_annual) if annual_capped else after_copay
        ledger.log(
            component="POLICY_ENGINE", rule_id="annual_opd_limit", outcome="WARN" if annual_capped else "PASS",
            effect="ADJUST_AMOUNT" if annual_capped else "NONE",
            policy_ref="coverage.annual_opd_limit",
            evidence={"ytd_claims_amount": str(ytd), "annual_limit": str(annual_limit), "remaining": str(remaining_annual)},
            amount_before=_q(after_copay), amount_after=_q(after_annual_limit),
            duration_ms=int((time.time() - start_time) * 1000),
        )

        final_amount = _q(after_annual_limit)

        financial_breakdown = FinancialBreakdown(
            raw_eligible_amount=_q(eligible_amount),
            after_network_discount=_q(after_discount),
            after_copay=_q(after_copay),
            after_sub_limit=_q(after_copay),  # sub_limit is advisory-only; see docstring
            after_annual_limit=final_amount,
            network_discount_applied=is_network and discount_pct > 0,
            network_discount_percent=discount_pct,
            copay_percent=copay_pct,
        )

        ledger.log(
            component="POLICY_ENGINE", rule_id="financial_waterfall", outcome="PASS", effect="ADJUST_AMOUNT",
            policy_ref=f"opd_categories.{cat_key}", interpretation_ref="financial_waterfall_order",
            evidence={
                "is_network_hospital": is_network,
                "network_discount_percent": str(discount_pct),
                "copay_percent": str(copay_pct),
            },
            amount_before=submission.claimed_amount, amount_after=final_amount,
            duration_ms=int((time.time() - start_time) * 1000),
        )

        if final_amount <= 0:
            return PolicyResult(
                decision="REJECTED", approved_amount=Decimal(0),
                reason="Claim value reduced to zero after policy adjustments (exclusions/limits).",
                line_items=line_items, warnings=warnings,
            )

        decision = "APPROVED"
        reason_parts = []
        if line_items_rejected:
            decision = "PARTIAL"
            reason_parts.append("some line items were excluded under policy terms")
        if annual_capped:
            decision = "PARTIAL"
            reason_parts.append(f"amount capped by the remaining annual OPD limit (₹{remaining_annual} of ₹{annual_limit} left)")

        reason = (
            "Claim approved after applicable network discount and co-pay."
            if not reason_parts else
            "Claim partially approved: " + "; ".join(reason_parts) + "."
        )

        return PolicyResult(decision, final_amount, reason, line_items, financial_breakdown, warnings)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _merge_extractions(extractions: List[DocumentExtraction]) -> Tuple[Optional[ExtractedBill], Optional[ExtractedPrescription]]:
        bill_data, prescription_data = None, None
        for ext in extractions:
            if ext.bill_data and bill_data is None:
                bill_data = ext.bill_data
            if ext.prescription_data and prescription_data is None:
                prescription_data = ext.prescription_data
        return bill_data, prescription_data

    @staticmethod
    def _combined_text(prescription_data: Optional[ExtractedPrescription]) -> str:
        if not prescription_data:
            return ""
        parts = prescription_data.diagnoses + prescription_data.tests_ordered
        return " ".join(parts).lower()

    def _check_submission_deadline(self, submission: ClaimSubmission) -> Optional[str]:
        deadline_days = int(self.config.get("submission_rules", {}).get("deadline_days_from_treatment", 30))
        submission_date_str = submission.submission_date or submission.treatment_date
        try:
            treatment_dt = datetime.strptime(submission.treatment_date, "%Y-%m-%d")
            submission_dt = datetime.strptime(submission_date_str, "%Y-%m-%d")
        except ValueError:
            return None
        days_elapsed = (submission_dt - treatment_dt).days
        if days_elapsed > deadline_days:
            return (
                f"Claim submitted {days_elapsed} days after treatment, exceeding the "
                f"{deadline_days}-day submission deadline."
            )
        return None

    def _check_waiting_periods(self, submission: ClaimSubmission, member: dict, combined_text: str, ledger: TraceLedger, start_time: float) -> Optional[PolicyResult]:
        try:
            join_dt = datetime.strptime(member["join_date"], "%Y-%m-%d")
            treatment_dt = datetime.strptime(submission.treatment_date, "%Y-%m-%d")
        except (ValueError, KeyError):
            return None
        days_active = (treatment_dt - join_dt).days

        initial_wait = int(self.config.get("waiting_periods", {}).get("initial_waiting_period_days", 0))
        if days_active < initial_wait:
            eligible_date = (join_dt + timedelta(days=initial_wait)).date().isoformat()
            return self._reject(
                ledger, start_time, "initial_waiting_period",
                f"This member is within the {initial_wait}-day initial waiting period. "
                f"Eligible for claims from {eligible_date}.",
                policy_ref="waiting_periods.initial_waiting_period_days",
                evidence={"days_active": days_active, "join_date": member["join_date"]},
            )

        condition_keywords = self.interpretation.get("waiting_period_condition_keywords", {})
        specific_periods = self.config.get("waiting_periods", {}).get("specific_conditions", {})
        for condition_key, keywords in condition_keywords.items():
            if not _keyword_match(combined_text, keywords):
                continue
            required_days = specific_periods.get(condition_key)
            if required_days and days_active < required_days:
                eligible_date = (join_dt + timedelta(days=required_days)).date().isoformat()
                return self._reject(
                    ledger, start_time, f"waiting_period_{condition_key}",
                    f"{condition_key.replace('_', ' ').title()}-related claims have a {required_days}-day waiting "
                    f"period. This member will be eligible for such claims from {eligible_date}.",
                    policy_ref=f"waiting_periods.specific_conditions.{condition_key}",
                    interpretation_ref="waiting_period_condition_keywords",
                    evidence={"matched_condition": condition_key, "days_active": days_active},
                    decision="REJECTED",
                    rule_id_override="WAITING_PERIOD",
                )
        return None

    def _check_exclusions(self, combined_text: str, ledger: TraceLedger, start_time: float) -> Optional[PolicyResult]:
        exclusion_keywords = self.interpretation.get("exclusion_condition_keywords", {})
        for excl_name, keywords in exclusion_keywords.items():
            if _keyword_match(combined_text, keywords):
                return self._reject(
                    ledger, start_time, "excluded_condition",
                    f"Treatment for '{excl_name}' is excluded under this policy.",
                    policy_ref="exclusions.conditions",
                    interpretation_ref="exclusion_condition_keywords",
                    evidence={"matched_exclusion": excl_name},
                    rule_id_override="EXCLUDED_CONDITION",
                )
        return None

    def _check_diagnostic_pre_auth(self, submission: ClaimSubmission, cat_rules: dict, combined_text: str, ledger: TraceLedger, start_time: float) -> Optional[PolicyResult]:
        pre_auth_keywords = self.interpretation.get("diagnostic_pre_auth_keywords", {})
        threshold = Decimal(str(cat_rules.get("pre_auth_threshold", 0)))
        for test_name, keywords in pre_auth_keywords.items():
            if not _keyword_match(combined_text, keywords):
                continue
            if submission.claimed_amount > threshold and not submission.pre_auth_id:
                return self._reject(
                    ledger, start_time, "pre_auth_missing",
                    f"{test_name} above ₹{threshold} requires pre-authorization, which was not provided. "
                    f"Please obtain pre-authorization and resubmit with the pre_auth_id, or attach it now if you "
                    f"already have one.",
                    policy_ref="opd_categories.diagnostic.high_value_tests_requiring_pre_auth",
                    interpretation_ref="diagnostic_pre_auth_keywords",
                    evidence={"matched_test": test_name, "claimed_amount": str(submission.claimed_amount), "threshold": str(threshold)},
                    rule_id_override="PRE_AUTH_MISSING",
                )
            break
        return None

    def _adjudicate_line_items(self, submission: ClaimSubmission, bill_data: Optional[ExtractedBill], cat_key: str, cat_rules: dict, ledger: TraceLedger, start_time: float) -> Tuple[List[LineItemDecision], Decimal, bool]:
        exclusion_keywords = self.interpretation.get("exclusion_condition_keywords", {})
        dental_excluded = [p.lower() for p in cat_rules.get("excluded_procedures", [])]
        vision_excluded = [p.lower() for p in cat_rules.get("excluded_items", [])]

        if not (bill_data and bill_data.line_items):
            return [], submission.claimed_amount, False

        line_items: List[LineItemDecision] = []
        eligible_amount = Decimal(0)
        any_rejected = False

        for item in bill_data.line_items:
            desc_lower = item.description.lower()
            amount = Decimal(str(item.amount))
            reason = None

            if cat_key == "dental" and _keyword_match(desc_lower, dental_excluded):
                reason = f"'{item.description}' is an excluded dental procedure under this policy."
            elif cat_key == "vision" and _keyword_match(desc_lower, vision_excluded):
                reason = f"'{item.description}' is an excluded vision item/procedure under this policy."
            else:
                for excl_name, keywords in exclusion_keywords.items():
                    if _keyword_match(desc_lower, keywords):
                        reason = f"'{item.description}' falls under the policy exclusion '{excl_name}'."
                        break

            if reason:
                any_rejected = True
                line_items.append(LineItemDecision(description=item.description, amount=amount, status="REJECTED", reason=reason))
            else:
                eligible_amount += amount
                line_items.append(LineItemDecision(description=item.description, amount=amount, status="APPROVED"))

        ledger.log(
            component="POLICY_ENGINE", rule_id="line_item_adjudication",
            outcome="WARN" if any_rejected else "PASS", effect="ADJUST_AMOUNT" if any_rejected else "NONE",
            policy_ref=f"opd_categories.{cat_key}",
            evidence={"rejected_items": [li.description for li in line_items if li.status == "REJECTED"]},
            duration_ms=int((time.time() - start_time) * 1000),
        )
        return line_items, eligible_amount, any_rejected

    def _quality_warnings(self, bill_data: Optional[ExtractedBill], cat_key: str, cat_rules: dict, extractions: List[DocumentExtraction]) -> List[str]:
        warnings: List[str] = []

        if cat_key == "pharmacy" and cat_rules.get("generic_mandatory"):
            warnings.append(
                "Branded vs. generic drug classification is not available from extraction; the flat pharmacy "
                "co-pay was applied instead of the branded-drug rate. Requires a drug database lookup, out of "
                "scope for this system."
            )

        if cat_key == "dental" and cat_rules.get("requires_dental_report"):
            has_report = any(ext.file_type == "DENTAL_REPORT" for ext in extractions)
            if not has_report:
                warnings.append(
                    "Dental report was not provided; approval is based on the itemized bill only "
                    "(policy marks a missing dental report as WARN, not blocking)."
                )

        if bill_data and bill_data.total_amount is not None and bill_data.line_items:
            computed_sum = sum((Decimal(str(li.amount)) for li in bill_data.line_items), Decimal(0))
            if abs(computed_sum - Decimal(str(bill_data.total_amount))) > Decimal("1"):
                warnings.append(
                    f"Bill line items sum to ₹{_q(computed_sum)} but the bill states a total of "
                    f"₹{bill_data.total_amount}. Please verify against the original document."
                )

        return warnings

    def _reject(
        self, ledger: TraceLedger, start_time: float, rule_id: str, reason: str,
        policy_ref: Optional[str] = None, interpretation_ref: Optional[str] = None,
        evidence: Optional[dict] = None, decision: str = "REJECTED",
        rule_id_override: Optional[str] = None,
    ) -> PolicyResult:
        ledger.log(
            component="POLICY_ENGINE", rule_id=rule_id_override or rule_id, outcome="FAIL",
            effect="REJECT" if decision == "REJECTED" else "ESCALATE",
            policy_ref=policy_ref, interpretation_ref=interpretation_ref,
            evidence=evidence or {}, amount_after=Decimal(0),
            duration_ms=int((time.time() - start_time) * 1000),
        )
        tag = rule_id_override or rule_id.upper()
        return PolicyResult(decision=decision, approved_amount=Decimal(0), reason=f"{tag}: {reason}")
