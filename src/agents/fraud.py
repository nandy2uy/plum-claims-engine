"""Fraud / risk signal agent.

Component contract
-------------------
Input:  ClaimSubmission, TraceLedger.
Output: (signals: List[str], fraud_score: float in [0, 1]).
        Every threshold checked is written to the ledger regardless of
        outcome (PASS or WARN), so a MANUAL_REVIEW decision is always
        traceable back to a specific, named policy field.
Errors: raises if `submission.simulate_component_failure` is set — this is
        the documented, explicit fault-injection hook (any caller can use
        it, not just a specific test claim_id). The caller wraps this agent
        in @isolate_fault, so a raise here degrades gracefully: the claim
        keeps processing with fraud checks skipped, confidence lowered, and
        manual review recommended, rather than crashing or silently passing.

Frequency checks (same-day / monthly claim counts) prefer
`submission.claims_history` when the caller supplies it — this lets the eval
harness reproduce a specific fraud scenario deterministically without a
live claim history. When it's absent, the agent falls back to the
process-local ClaimStore, which is what real traffic uses.
"""

import time
from decimal import Decimal
from datetime import datetime
from typing import Dict, Any, List, Tuple
from src.models.claim import ClaimSubmission
from src.models.trace import TraceLedger
from src.core.fault_tolerance import isolate_fault
from src.core.storage import claim_store


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class FraudAgent:
    def __init__(self, policy_config: Dict[str, Any]):
        self.thresholds = policy_config.get("fraud_thresholds", {})

    @isolate_fault(component_name="FRAUD", fallback_return=([], 0.0))
    def evaluate(self, submission: ClaimSubmission, ledger: TraceLedger) -> Tuple[List[str], float]:
        start_time = time.time()

        if submission.simulate_component_failure:
            raise RuntimeError("Simulated fraud-engine outage (requested via simulate_component_failure).")

        signals: List[str] = []
        score = 0.0

        same_day_limit = int(self.thresholds.get("same_day_claims_limit", 2))
        monthly_limit = int(self.thresholds.get("monthly_claims_limit", 6))
        high_value = Decimal(str(self.thresholds.get("high_value_claim_threshold", 25000)))
        auto_review_above = Decimal(str(self.thresholds.get("auto_manual_review_above", 25000)))

        same_day_count = self._same_day_count(submission)
        ledger.log(
            component="FRAUD", rule_id="same_day_claims_limit",
            outcome="WARN" if same_day_count > same_day_limit else "PASS", effect="NONE",
            policy_ref="fraud_thresholds.same_day_claims_limit",
            evidence={"count_including_this_claim": same_day_count, "limit": same_day_limit},
            duration_ms=int((time.time() - start_time) * 1000),
        )
        if same_day_count > same_day_limit:
            signals.append(f"{_ordinal(same_day_count)} same-day claim from this member exceeds the limit of {same_day_limit} per day")
            score += 0.5

        monthly_count = self._monthly_count(submission)
        ledger.log(
            component="FRAUD", rule_id="monthly_claims_limit",
            outcome="WARN" if monthly_count > monthly_limit else "PASS", effect="NONE",
            policy_ref="fraud_thresholds.monthly_claims_limit",
            evidence={"count_including_this_claim": monthly_count, "limit": monthly_limit},
            duration_ms=int((time.time() - start_time) * 1000),
        )
        if monthly_count > monthly_limit:
            signals.append(f"{_ordinal(monthly_count)} claim this month from this member exceeds the limit of {monthly_limit} per month")
            score += 0.3

        is_high_value = submission.claimed_amount >= high_value
        ledger.log(
            component="FRAUD", rule_id="high_value_claim_threshold",
            outcome="WARN" if is_high_value else "PASS", effect="NONE",
            policy_ref="fraud_thresholds.high_value_claim_threshold",
            evidence={"claimed_amount": str(submission.claimed_amount), "threshold": str(high_value)},
            duration_ms=int((time.time() - start_time) * 1000),
        )
        if is_high_value:
            score += 0.3

        is_auto_review = submission.claimed_amount >= auto_review_above
        if is_auto_review:
            signals.append(f"Claim amount (₹{submission.claimed_amount}) exceeds the auto-manual-review threshold of ₹{auto_review_above}")

        score = min(score, 1.0)
        ledger.log(
            component="FRAUD", outcome="WARN" if signals else "PASS",
            effect="ESCALATE" if signals else "NONE",
            evidence={"signals": signals, "fraud_score": score},
            confidence_factor=1.0 - score if signals else None,
            duration_ms=int((time.time() - start_time) * 1000),
        )

        return signals, score

    def _same_day_count(self, submission: ClaimSubmission) -> int:
        if submission.claims_history is not None:
            prior = [c for c in submission.claims_history if c.date == submission.treatment_date]
        else:
            prior = claim_store.claims_for_member_on(submission.member_id, submission.treatment_date, exclude_claim_id=submission.claim_id)
        return len(prior) + 1

    def _monthly_count(self, submission: ClaimSubmission) -> int:
        try:
            treatment_dt = datetime.strptime(submission.treatment_date, "%Y-%m-%d")
        except ValueError:
            return 1
        if submission.claims_history is not None:
            prior = [
                c for c in submission.claims_history
                if _same_month(c.date, treatment_dt.year, treatment_dt.month)
            ]
        else:
            prior = claim_store.claims_for_member_in_month(
                submission.member_id, treatment_dt.year, treatment_dt.month, exclude_claim_id=submission.claim_id
            )
        return len(prior) + 1


def _same_month(date_str: str, year: int, month: int) -> bool:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return False
    return d.year == year and d.month == month
