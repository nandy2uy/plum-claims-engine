"""Decision agent: turns stage outputs into the final, explainable ClaimDecision.

Component contract
-------------------
needs_member_action(submission, ledger, stage, message, actions, extractions=None) -> ClaimDecision
    status=NEEDS_MEMBER_ACTION, decision=None (test_cases.json expects null for
    TC001-TC003), member_message=the specific message, required_actions=actions.
decide(submission, extractions, verification, policy_stage, fraud_stage, ledger) -> ClaimDecision
    status=DECIDED, decision in APPROVED | PARTIAL | REJECTED | MANUAL_REVIEW.
system_failure(submission, ledger, error) -> ClaimDecision
    Last-resort MANUAL_REVIEW when the orchestrator itself crashed; the trace
    recorded up to that point is preserved (v1 returned trace=[] here).
Errors: none.

Decision rules, in order:
  1. Policy engine failed (critical)                 -> MANUAL_REVIEW, POLICY_ENGINE_UNAVAILABLE
  2. A required document could not be extracted (system fault) -> MANUAL_REVIEW, EXTRACTION_INCOMPLETE
  3. Policy proposal REJECTED                        -> REJECTED (fraud cannot un-reject; signals still shown)
  4. Fraud escalated                                 -> MANUAL_REVIEW, FRAUD_SIGNALS, provisional amount kept
  5. Fraud check failed on a high-value claim        -> MANUAL_REVIEW, FRAUD_CHECK_UNAVAILABLE_HIGH_VALUE
  6. Policy proposal with review reasons             -> MANUAL_REVIEW
  7. otherwise the policy proposal (APPROVED / PARTIAL)
Any failed non-critical stage sets manual_review_recommended and is named in
degraded_components and warnings (TC011).

Confidence (reconstructable from the trace):
  base  = mean confidence of the usable extractions (or confidence.no_extraction_performed)
  score = base x product(confidence_factor of every trace event whose scope applies)
  Scope ALL applies to every decision. Scope PAYOUT (e.g. an unverified patient
  name) applies only when money may be paid (APPROVED / PARTIAL / MANUAL_REVIEW),
  because it does not weaken a rejection on a permanent exclusion.
  Soft fraud signals that did not escalate add a factor of 1 - score x
  confidence.fraud_soft_score_weight. Every factor is listed in confidence_breakdown.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from src.agents.doc_verifier import VerificationResult
from src.agents.fraud import FraudResult
from src.agents.policy_engine import PolicyResult
from src.core.config import ENGINE_VERSION
from src.core.fault_tolerance import StageResult
from src.core.text import fmt_inr
from src.models.claim import ClaimDecision, ClaimSubmission, ConfidenceFactor, MemberAction
from src.models.medical import DocumentExtraction
from src.models.policy import Interpretation, PolicyConfig
from src.models.trace import TraceLedger

COMPONENT = "DECISION"
ZERO = Decimal(0)
PAYOUT_DECISIONS = {"APPROVED", "PARTIAL", "MANUAL_REVIEW"}


class DecisionAgent:
    def __init__(self, policy: PolicyConfig, interpretation: Interpretation):
        self.policy = policy
        self.weights = interpretation.confidence

    # ================================================================ early stop

    def needs_member_action(self, submission: ClaimSubmission, ledger: TraceLedger, stage: str, message: str,
                            actions: List[MemberAction], extractions: Optional[List[DocumentExtraction]] = None) -> ClaimDecision:
        confidence, breakdown = self._confidence(ledger, extractions or [], decision=None)
        ledger.log(COMPONENT, "INFO", rule_id="final_decision",
                   summary=f"Stopped at {stage}: member action required before any claim decision is made. "
                           f"No decision issued; the claim will continue once the member responds.",
                   evidence={"stage": stage, "actions": [a.model_dump() for a in actions]})
        return ClaimDecision(
            claim_id=submission.claim_id, status="NEEDS_MEMBER_ACTION", decision=None, claimed_amount=submission.claimed_amount,
            reason=f"Stopped at {stage} before adjudication: {message}", member_message=message, required_actions=actions,
            confidence_score=confidence, confidence_breakdown=breakdown, degraded_components=ledger.degraded_components(),
            policy_id=self.policy.policy_id, engine_version=ENGINE_VERSION, trace=ledger.events,
        )

    # ================================================================ crash fallback

    def system_failure(self, submission: ClaimSubmission, ledger: TraceLedger, error: BaseException) -> ClaimDecision:
        ledger.log("PIPELINE", "ERROR", effect="ESCALATE", rule_id="unhandled_exception",
                   summary=f"Unexpected pipeline error ({type(error).__name__}: {error}); routed to manual review. "
                           f"The trace above shows every step completed before the failure.",
                   error_details=str(error), confidence_factor=self.weights.degraded_component)
        confidence, breakdown = self._confidence(ledger, [], decision="MANUAL_REVIEW")
        ledger.log(COMPONENT, "INFO", rule_id="final_decision", summary="MANUAL_REVIEW (pipeline error).")
        return ClaimDecision(
            claim_id=submission.claim_id, status="DECIDED", decision="MANUAL_REVIEW", claimed_amount=submission.claimed_amount,
            reason=f"Internal pipeline error ({type(error).__name__}); claim routed to manual review with the partial trace.",
            member_message="We've received your claim. It needs to be checked by our claims team, who will get back to you. "
                           "You don't need to do anything right now.",
            review_reasons=["PIPELINE_ERROR"], confidence_score=confidence, confidence_breakdown=breakdown,
            manual_review_recommended=True, degraded_components=ledger.degraded_components() or ["PIPELINE"],
            policy_id=self.policy.policy_id, engine_version=ENGINE_VERSION, trace=ledger.events,
        )

    # ================================================================ normal decision

    def decide(self, submission: ClaimSubmission, extractions: List[DocumentExtraction], verification: VerificationResult,
               policy_stage: StageResult, fraud_stage: StageResult, ledger: TraceLedger) -> ClaimDecision:
        policy: Optional[PolicyResult] = policy_stage.value if policy_stage.ok else None
        fraud: Optional[FraudResult] = fraud_stage.value if fraud_stage.ok else None
        review: List[str] = []
        warnings: List[str] = list(policy.warnings) if policy else []
        degraded = ledger.degraded_components()

        if policy is None:
            decision = "MANUAL_REVIEW"
            review.append("POLICY_ENGINE_UNAVAILABLE")
        else:
            decision = policy.proposal
            review += policy.review_codes

        if verification.status == "DEGRADED":
            review.append("EXTRACTION_INCOMPLETE")
            warnings.append(f"Required document(s) {', '.join(verification.system_failed_required)} could not be read due "
                            f"to a system error; amounts could not be verified.")
            decision = "MANUAL_REVIEW"

        if fraud is not None and fraud.escalate:
            review.append("FRAUD_SIGNALS")
            if decision != "REJECTED":
                decision = "MANUAL_REVIEW"
        if fraud is None:
            warnings.append("FRAUD check failed and was skipped; manual review is recommended due to incomplete processing.")
            if submission.claimed_amount >= self.policy.fraud_thresholds.high_value_claim_threshold and decision != "REJECTED":
                review.append("FRAUD_CHECK_UNAVAILABLE_HIGH_VALUE")
                decision = "MANUAL_REVIEW"
        for component in degraded:
            if component != "FRAUD":
                warnings.append(f"{component} failed during processing and was skipped or degraded; manual review recommended.")

        if fraud is not None and not fraud.escalate and fraud.score > 0:
            factor = round(1 - fraud.score * self.weights.fraud_soft_score_weight, 3)
            ledger.log(COMPONENT, "WARN", rule_id="fraud_soft_signals", interpretation_ref="confidence.fraud_soft_score_weight",
                       summary=f"Soft fraud signals ({', '.join(fraud.codes)}, score {fraud.score}) did not warrant escalation "
                               f"but lower confidence by a factor of {factor}.",
                       confidence_factor=factor, confidence_scope="ALL")

        payable = policy.approved_amount if policy else ZERO
        approved, provisional = payable, None
        if decision == "MANUAL_REVIEW":
            approved, provisional = ZERO, (payable if payable > 0 else None)
        elif decision == "REJECTED":
            approved = ZERO

        confidence, breakdown = self._confidence(ledger, extractions, decision)
        review = list(dict.fromkeys(review))
        rejections = policy.rejection_codes if policy and decision in ("REJECTED", "PARTIAL") else []
        manual_recommended = decision == "MANUAL_REVIEW" or bool(degraded)
        reason, member_message = self._explain(submission, decision, approved, provisional, policy, fraud, review,
                                               degraded, confidence)
        actions = list(policy.actions) if policy and decision in ("REJECTED", "PARTIAL") else []

        ledger.log(COMPONENT, "INFO", rule_id="final_decision",
                   summary=f"FINAL: {decision}, approved {fmt_inr(approved)}"
                           + (f" (provisional {fmt_inr(provisional)} held for review)" if provisional else "")
                           + f", confidence {confidence}. {reason}",
                   evidence={"decision": decision, "approved_amount": str(approved),
                             "provisional_amount": str(provisional) if provisional is not None else None,
                             "rejection_reasons": rejections, "review_reasons": review, "degraded_components": degraded,
                             "confidence": confidence,
                             "confidence_factors": [f.model_dump() for f in breakdown]})

        return ClaimDecision(
            claim_id=submission.claim_id, status="DECIDED", decision=decision, approved_amount=approved,
            provisional_amount=provisional, claimed_amount=submission.claimed_amount, reason=reason,
            member_message=member_message, rejection_reasons=rejections, review_reasons=review, required_actions=actions,
            confidence_score=confidence, confidence_breakdown=breakdown, manual_review_recommended=manual_recommended,
            degraded_components=degraded, fraud_signals=[s.describe() for s in fraud.signals] if fraud else [],
            fraud_score=fraud.score if fraud else None, warnings=list(dict.fromkeys(warnings)),
            line_item_breakdown=policy.line_items if policy else [], financial_breakdown=policy.financial if policy else None,
            policy_id=self.policy.policy_id, engine_version=ENGINE_VERSION, trace=ledger.events,
        )

    # ================================================================ explanation

    def _explain(self, submission, decision, approved, provisional, policy: Optional[PolicyResult],
                 fraud: Optional[FraudResult], review: List[str], degraded: List[str], confidence: float):
        claimed = fmt_inr(submission.claimed_amount)
        steps = " ".join(policy.financial.steps) if policy and policy.financial else ""
        degraded_note = (f" Degraded components: {', '.join(degraded)} (failed and skipped); manual review is recommended "
                         f"due to incomplete processing." if degraded else "")
        rejects = [f for f in policy.findings if f.kind == "REJECT"] if policy else []
        reviews = [f for f in policy.findings if f.kind == "REVIEW"] if policy else []

        if decision == "APPROVED":
            reason = f"APPROVED {fmt_inr(approved)} of {claimed} claimed. {steps}{degraded_note}".strip()
            member = f"Good news — your claim has been approved for {fmt_inr(approved)} (you claimed {claimed}). {steps}"
            if degraded:
                member += " Some automated checks could not run, so our team may take a quick second look; no action is needed from you."
            return reason, member.strip()

        if decision == "PARTIAL":
            approved_items = [l for l in policy.line_items if l.status == "APPROVED"]
            rejected_items = [l for l in policy.line_items if l.status == "REJECTED"]
            ok = "; ".join(f"{l.description} ({fmt_inr(l.amount)})" for l in approved_items)
            no = "; ".join(f"{l.description} ({fmt_inr(l.amount)}) — {(l.reason or '').rstrip('.')}" for l in rejected_items)
            caps = " ".join(f.member_text for f in rejects if f.code == "ANNUAL_LIMIT_EXHAUSTED")
            reason = (f"PARTIAL: {fmt_inr(approved)} of {claimed} approved. Approved items: {ok or 'none'}. "
                      f"Rejected items: {no or 'none'}. {caps} {steps}{degraded_note}").strip()
            member = (f"Your claim has been partly approved: {fmt_inr(approved)} of the {claimed} you claimed. "
                      f"Covered: {ok or 'none'}. Not covered: {no or 'none'}. {caps} {steps}").strip()
            return reason, member

        if decision == "REJECTED":
            reasons = " ".join(f.ops_text for f in rejects) or "No eligible items."
            member_reasons = " ".join(f.member_text for f in rejects) or "None of the billed items are covered."
            reason = f"REJECTED ({', '.join(policy.rejection_codes) if policy else 'n/a'}): {reasons}"
            if fraud is not None and fraud.escalate:
                reason += f" Fraud signals also present: {', '.join(fraud.codes)}."
            member = f"We're sorry — your claim for {claimed} could not be approved. {member_reasons}"
            if not any(f.code == "PRE_AUTH_MISSING" for f in rejects):
                member += " If you believe this is wrong, you can reply with additional documents and our team will re-check it."
            return reason, member

        # MANUAL_REVIEW
        parts = []
        if "FRAUD_SIGNALS" in review and fraud is not None:
            parts.append("Fraud signals: " + "; ".join(s.describe() for s in fraud.signals) + f" (score {fraud.score}).")
        parts += [f"{f.code}: {f.ops_text}" for f in reviews]
        labels = {
            "POLICY_ENGINE_UNAVAILABLE": "Policy engine failed; no automated adjudication was possible.",
            "EXTRACTION_INCOMPLETE": "A required document could not be extracted due to a system error.",
            "FRAUD_CHECK_UNAVAILABLE_HIGH_VALUE": "Fraud check failed on a high-value claim.",
        }
        parts += [labels[r] for r in review if r in labels]
        held = f" Provisional payable amount if cleared: {fmt_inr(provisional)}." if provisional else ""
        reason = f"MANUAL_REVIEW ({', '.join(review)}): {' '.join(parts)}{held}{degraded_note}".strip()
        # Member-facing text stays neutral: never disclose fraud heuristics to the claimant.
        member_bits = [f.member_text for f in reviews]
        if "EXTRACTION_INCOMPLETE" in review or "POLICY_ENGINE_UNAVAILABLE" in review:
            opener = ("Your claim has been received. Our automated processing could not be completed (a problem on our "
                      "side, not with your documents), so a member of our claims team will assess it")
        else:
            opener = "Your claim has been received. It needs a closer look by our claims team before a final decision"
        member = (opener + (": " + " ".join(member_bits) if member_bits else ".")
                  + " We'll contact you if anything else is needed; you don't need to do anything right now.")
        return reason, member

    # ================================================================ confidence

    def _confidence(self, ledger: TraceLedger, extractions: List[DocumentExtraction], decision: Optional[str]):
        usable = [e for e in extractions if e.usable and e.confidence_score > 0]
        if usable:
            base = sum(e.confidence_score for e in usable) / len(usable)
            base_reason = f"Mean extraction confidence of {len(usable)} document(s)"
        else:
            base = self.weights.no_extraction_performed
            base_reason = "No document extraction confidence available (stopped before or without extraction)"
        breakdown = [ConfidenceFactor(component="EXTRACTOR", rule_id="base", factor=round(base, 3), reason=base_reason)]
        score = base
        for event in ledger.confidence_events():
            if event.confidence_scope == "PAYOUT" and decision not in PAYOUT_DECISIONS:
                continue
            score *= event.confidence_factor
            breakdown.append(ConfidenceFactor(component=event.component, rule_id=event.rule_id,
                                              factor=event.confidence_factor, reason=event.summary))
        return round(max(0.0, min(1.0, score)), 3), breakdown
