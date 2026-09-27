"""Fraud agent: behavioural and document-integrity signals for one claim.

Component contract
-------------------
Input:  ClaimSubmission, List[DocumentExtraction], fingerprints (List[str]), TraceLedger.
        Constructor takes the PolicyConfig, the Interpretation and a claim store
        (anything with prior_claims(member_id, exclude_claim_id) and
        find_by_fingerprints(fps, exclude_claim_id)).
Output: FraudResult(score, signals, escalate)
          signals   FraudSignal(code, detail, weight, hard) for every signal that fired
          score     noisy-OR of signal weights: 1 - product(1 - w)
          escalate  True if any HARD signal fired or score >= fraud_score_manual_review_threshold
Errors: none for valid input. The orchestrator runs this stage as NON-critical: if it
        fails, the claim still gets a decision, but the failure is visible in the trace,
        confidence drops, manual review is recommended, and claims at or above the
        high-value threshold are held for manual review.

Signals (each check is logged PASS or FAIL, so a clean claim shows what was checked):
  SAME_DAY_LIMIT_EXCEEDED   claims on the treatment date, this one included > same_day_claims_limit   (HARD)
  MONTHLY_LIMIT_EXCEEDED    claims in the 30 days up to the treatment date > monthly_claims_limit     (HARD)
  HIGH_VALUE_AUTO_REVIEW    claimed > auto_manual_review_above                                        (HARD)
  HIGH_VALUE_CLAIM          claimed > high_value_claim_threshold (below the auto-review line)         (soft)
  POSSIBLE_DUPLICATE_CLAIM  an earlier claim with the same date and amount                            (HARD)
  DUPLICATE_DOCUMENT        a document hash / bill number already used on an earlier claim           (HARD)
  DOCUMENT_ALTERATION, DUPLICATE_STAMP   quality flags raised during extraction                    (soft)
  CLAIM_EXCEEDS_BILL        claimed > billed x claim_to_bill_inflation_tolerance                     (soft)

History = caller-supplied claims_history UNION the claim store (by claim_id). v1 used
the caller's list INSTEAD of the store whenever one was sent, so sending an empty
history hid every earlier claim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from src.core.text import fmt_date, fmt_inr, normalize_text
from src.models.claim import ClaimSubmission
from src.models.medical import BILL_TYPES, FLAG_ALTERATION, FLAG_DUPLICATE_STAMP, DocumentExtraction
from src.models.policy import Interpretation, PolicyConfig
from src.models.trace import Stopwatch, TraceLedger

COMPONENT = "FRAUD"


@dataclass(frozen=True)
class FraudSignal:
    code: str
    detail: str
    weight: float
    hard: bool

    def describe(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class FraudResult:
    score: float = 0.0
    signals: List[FraudSignal] = field(default_factory=list)
    escalate: bool = False

    @property
    def codes(self) -> List[str]:
        return [s.code for s in self.signals]


@dataclass(frozen=True)
class _PastClaim:
    claim_id: str
    day: date
    amount: Decimal
    provider: Optional[str]
    source: str


def document_fingerprints(submission: ClaimSubmission, extractions: List[DocumentExtraction]) -> List[str]:
    """Stable identifiers for duplicate detection: a hash of each uploaded file's bytes
    (or URL), plus provider+bill-number for every bill that has one. Fixture documents
    (eval data) are not content-hashed: identical fixtures re-run are not re-submissions."""
    fps = []
    for doc in submission.documents:
        if doc.content_base64:
            fps.append("sha256:" + hashlib.sha256(doc.content_base64.encode()).hexdigest()[:24])
        elif doc.content_url:
            fps.append("url:" + hashlib.sha256(doc.content_url.encode()).hexdigest()[:24])
    for ext in extractions:
        bill = ext.bill_data
        if ext.usable and bill is not None and bill.bill_number:
            provider = normalize_text(bill.hospital_name or "")
            fps.append(f"bill:{provider}|{normalize_text(bill.bill_number)}")
    return sorted(set(fps))


class FraudAgent:
    def __init__(self, policy: PolicyConfig, interpretation: Interpretation, store):
        self.thresholds = policy.fraud_thresholds
        self.interp = interpretation
        self.weights: Dict[str, float] = interpretation.fraud_signal_weights
        self.store = store

    def evaluate(self, submission: ClaimSubmission, extractions: List[DocumentExtraction], fingerprints: List[str],
                 ledger: TraceLedger) -> FraudResult:
        watch = Stopwatch()
        t = self.thresholds
        signals: List[FraudSignal] = []
        history = self._history(submission)
        ledger.log(COMPONENT, "INFO", rule_id="claim_history",
                   summary=f"{len(history)} earlier claim(s) on record for {submission.member_id} "
                           f"(caller-supplied history merged with this system's records).",
                   interpretation_ref="assumptions.fraud_history",
                   evidence={"claims": [{"claim_id": c.claim_id, "date": c.day.isoformat(), "amount": str(c.amount),
                                         "provider": c.provider, "source": c.source} for c in history]})

        # 1. Same-day frequency
        same_day = [c for c in history if c.day == submission.treatment_date]
        count = len(same_day) + 1
        if count > t.same_day_claims_limit:
            providers = ", ".join(sorted({c.provider for c in same_day if c.provider})) or "unknown providers"
            signals.append(self._signal(ledger, "SAME_DAY_LIMIT_EXCEEDED", True, "fraud_thresholds.same_day_claims_limit",
                                        f"This is claim #{count} for {fmt_date(submission.treatment_date)} (limit "
                                        f"{t.same_day_claims_limit} per day); earlier same-day claims: "
                                        f"{', '.join(f'{c.claim_id} {fmt_inr(c.amount)}' for c in same_day)} at {providers}.",
                                        {"count_including_this": count, "limit": t.same_day_claims_limit,
                                         "same_day_claim_ids": [c.claim_id for c in same_day]}))
        else:
            self._pass(ledger, "same_day_frequency", "fraud_thresholds.same_day_claims_limit",
                       f"{count} claim(s) on {fmt_date(submission.treatment_date)}, within the limit of {t.same_day_claims_limit}.")

        # 2. Rolling 30-day frequency
        window_start = submission.treatment_date - timedelta(days=29)
        monthly = [c for c in history if window_start <= c.day <= submission.treatment_date]
        count = len(monthly) + 1
        if count > t.monthly_claims_limit:
            signals.append(self._signal(ledger, "MONTHLY_LIMIT_EXCEEDED", True, "fraud_thresholds.monthly_claims_limit",
                                        f"{count} claims in the 30 days to {fmt_date(submission.treatment_date)} "
                                        f"(limit {t.monthly_claims_limit}).",
                                        {"count_including_this": count, "limit": t.monthly_claims_limit}))
        else:
            self._pass(ledger, "monthly_frequency", "fraud_thresholds.monthly_claims_limit",
                       f"{count} claim(s) in the last 30 days, within the limit of {t.monthly_claims_limit}.")

        # 3. Value thresholds
        amount = submission.claimed_amount
        if amount > t.auto_manual_review_above:
            signals.append(self._signal(ledger, "HIGH_VALUE_AUTO_REVIEW", True, "fraud_thresholds.auto_manual_review_above",
                                        f"Claimed {fmt_inr(amount)} is above the automatic manual-review line of "
                                        f"{fmt_inr(t.auto_manual_review_above)}.", {"claimed": str(amount)}))
        elif amount > t.high_value_claim_threshold:
            signals.append(self._signal(ledger, "HIGH_VALUE_CLAIM", False, "fraud_thresholds.high_value_claim_threshold",
                                        f"Claimed {fmt_inr(amount)} is above the high-value threshold of "
                                        f"{fmt_inr(t.high_value_claim_threshold)}.", {"claimed": str(amount)}))
        else:
            self._pass(ledger, "claim_value", "fraud_thresholds.high_value_claim_threshold",
                       f"Claimed {fmt_inr(amount)} is below the high-value threshold of {fmt_inr(t.high_value_claim_threshold)}.")

        # 4. Duplicate claim (same date + same amount)
        dupes = [c for c in history if c.day == submission.treatment_date and c.amount == amount]
        if dupes:
            signals.append(self._signal(ledger, "POSSIBLE_DUPLICATE_CLAIM", True, None,
                                        f"Earlier claim(s) {', '.join(c.claim_id for c in dupes)} have the same treatment "
                                        f"date and amount ({fmt_inr(amount)}).", {"claim_ids": [c.claim_id for c in dupes]}))
        else:
            self._pass(ledger, "duplicate_claim", None, "No earlier claim with the same date and amount.")

        # 5. Duplicate documents
        reused = self.store.find_by_fingerprints(fingerprints, submission.claim_id) if fingerprints else []
        if reused:
            signals.append(self._signal(ledger, "DUPLICATE_DOCUMENT", True, None,
                                        f"A document or bill number in this claim was already submitted with claim(s) "
                                        f"{', '.join(c.claim_id for c in reused)}.",
                                        {"claim_ids": [c.claim_id for c in reused], "fingerprints": fingerprints}))
        elif fingerprints:
            self._pass(ledger, "duplicate_document", None,
                       f"None of this claim's {len(fingerprints)} document fingerprint(s) were seen before.")
        else:
            ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="duplicate_document",
                       summary="No file hashes or bill numbers available (fixture documents), so document reuse could "
                               "not be checked.")

        # 6. Document integrity flags from extraction
        flagged = False
        for ext in extractions:
            for flag, code in ((FLAG_ALTERATION, "DOCUMENT_ALTERATION"), (FLAG_DUPLICATE_STAMP, "DUPLICATE_STAMP")):
                if flag in ext.flags:
                    flagged = True
                    signals.append(self._signal(ledger, code, False, None,
                                                f"'{ext.label}' shows {code.replace('_', ' ').lower()} (flagged during extraction).",
                                                {"file_id": ext.file_id}))
        if not flagged:
            self._pass(ledger, "document_integrity", None, "No alteration or duplicate-stamp flags on any document.")

        # 7. Claimed vs billed
        billed = sum((self._bill_total(e) for e in extractions if e.usable and e.effective_type in BILL_TYPES and e.bill_data),
                     Decimal(0))
        tolerance = self.interp.claim_to_bill_inflation_tolerance
        if billed > 0 and amount > billed * tolerance:
            signals.append(self._signal(ledger, "CLAIM_EXCEEDS_BILL", False, None,
                                        f"Claimed {fmt_inr(amount)} is more than the {fmt_inr(billed)} shown on the bills.",
                                        {"claimed": str(amount), "billed": str(billed), "tolerance": str(tolerance)}))
        elif billed > 0:
            self._pass(ledger, "claim_vs_bill", None, f"Claimed {fmt_inr(amount)} is consistent with the {fmt_inr(billed)} billed.")
        else:
            ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="claim_vs_bill", summary="No bill amounts to compare with the claim.")

        # Score
        remaining = 1.0
        for s in signals:
            remaining *= (1.0 - s.weight)
        score = round(1.0 - remaining, 3)
        hard = [s for s in signals if s.hard]
        threshold = t.fraud_score_manual_review_threshold
        escalate = bool(hard) or score >= threshold
        if escalate:
            why = f"hard signal(s) {', '.join(s.code for s in hard)}" if hard else f"score {score} >= {threshold}"
            ledger.log(COMPONENT, "FAIL", effect="ESCALATE", rule_id="fraud_score",
                       policy_ref="fraud_thresholds.fraud_score_manual_review_threshold",
                       summary=f"Fraud score {score} with {len(signals)} signal(s); escalating to manual review ({why}).",
                       evidence={"score": score, "threshold": threshold, "signals": [s.code for s in signals]},
                       duration_ms=watch.ms())
        else:
            ledger.log(COMPONENT, "PASS" if not signals else "WARN", rule_id="fraud_score",
                       policy_ref="fraud_thresholds.fraud_score_manual_review_threshold",
                       summary=f"Fraud score {score} is below the manual-review threshold of {threshold}"
                               + (f" (soft signals: {', '.join(s.code for s in signals)})." if signals else "; no signals."),
                       evidence={"score": score, "threshold": threshold, "signals": [s.code for s in signals]},
                       duration_ms=watch.ms())
        return FraudResult(score=score, signals=signals, escalate=escalate)

    # ------------------------------------------------------------ helpers

    def _history(self, submission: ClaimSubmission) -> List[_PastClaim]:
        merged: Dict[str, _PastClaim] = {}
        for c in self.store.prior_claims(submission.member_id, submission.claim_id):
            merged[c.claim_id] = _PastClaim(c.claim_id, c.treatment_date, c.claimed_amount, c.provider, "store")
        for h in submission.claims_history or []:
            if h.claim_id != submission.claim_id:
                merged.setdefault(h.claim_id, _PastClaim(h.claim_id, h.date, h.amount, h.provider, "caller"))
        return sorted(merged.values(), key=lambda c: (c.day, c.claim_id))

    @staticmethod
    def _bill_total(ext: DocumentExtraction) -> Decimal:
        bill = ext.bill_data
        if bill.line_items:
            return sum((li.amount for li in bill.line_items), Decimal(0))
        return bill.total_amount or Decimal(0)

    def _signal(self, ledger, code, hard, policy_ref, detail, evidence) -> FraudSignal:
        weight = float(self.weights.get(code, 0.3))
        ledger.log(COMPONENT, "FAIL", effect="ESCALATE" if hard else "NONE", rule_id=code.lower(), policy_ref=policy_ref,
                   interpretation_ref=f"fraud_signal_weights.{code}",
                   summary=f"Fraud signal {code} ({'hard' if hard else 'soft'}, weight {weight}): {detail}",
                   evidence={**evidence, "weight": weight, "hard": hard})
        return FraudSignal(code, detail, weight, hard)

    @staticmethod
    def _pass(ledger, rule_id, policy_ref, summary) -> None:
        ledger.log(COMPONENT, "PASS", rule_id=rule_id, policy_ref=policy_ref, summary=summary)
