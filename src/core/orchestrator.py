"""Wires the five agents into the actual claim-decision pipeline.

Component contract
-------------------
Input:  ClaimSubmission.
Output: ClaimDecision — always. This method does not raise for business
        outcomes (missing docs, exclusions, fraud, etc. are all normal
        return paths). The only way to get an exception out of `process` is
        a genuine bug; main.py wraps the call anyway as a last line of
        defense so that even a bug here degrades to a MANUAL_REVIEW response
        instead of a 500 (belt-and-suspenders on top of each agent's own
        @isolate_fault).

Pipeline order: INTAKE -> DOC_GATE -> EXTRACTOR -> [required-doc readability
check] -> CONSISTENCY -> POLICY_ENGINE + FRAUD -> confidence/degradation
rollup. Every stage writes to one shared TraceLedger, which is returned
verbatim as `ClaimDecision.trace`.
"""

from decimal import Decimal
from typing import Dict, Any, List

from src.models.claim import ClaimSubmission, ClaimDecision
from src.models.trace import TraceLedger
from src.agents.doc_gate import DocumentGateAgent
from src.agents.extractor import ExtractionAgent
from src.agents.consistency import ConsistencyAgent
from src.agents.fraud import FraudAgent
from src.agents.policy_engine import PolicyEngineAgent
from src.core.storage import claim_store


class ClaimOrchestrator:
    def __init__(self, policy_config: Dict[str, Any], interpretation_config: Dict[str, Any]):
        self.doc_gate = DocumentGateAgent(policy_config)
        self.extractor = ExtractionAgent()
        self.consistency = ConsistencyAgent(policy_config)
        self.fraud = FraudAgent(policy_config)
        self.policy_engine = PolicyEngineAgent(policy_config, interpretation_config)

    async def process(self, submission: ClaimSubmission) -> ClaimDecision:
        ledger = TraceLedger()
        ledger.log(
            component="INTAKE", outcome="PASS", effect="NONE",
            evidence={"member_id": submission.member_id, "category": submission.category, "claim_id": submission.claim_id},
        )

        passed_gate, gate_payload = self.doc_gate.evaluate(submission, ledger)
        if not passed_gate:
            return self._finalize(submission, ledger, gate_payload["decision"], Decimal(0), gate_payload["message"], confidence=1.0)

        extractions = await self.extractor.process(submission, ledger=ledger)

        readability_issue = self._find_blocking_readability_issue(submission, extractions)
        if readability_issue:
            return self._finalize(submission, ledger, "NEEDS_MEMBER_ACTION", Decimal(0), readability_issue, confidence=1.0)

        is_consistent, consistency_msg = self.consistency.evaluate(submission, extractions, ledger)
        if not is_consistent:
            return self._finalize(submission, ledger, "NEEDS_MEMBER_ACTION", Decimal(0), consistency_msg, confidence=1.0)

        policy_result = self.policy_engine.evaluate(submission, extractions, ledger)
        fraud_signals, fraud_score = self.fraud.evaluate(submission, ledger=ledger)

        decision_type = policy_result.decision
        approved_amount = policy_result.approved_amount
        reason = policy_result.reason

        if fraud_signals and decision_type in ("APPROVED", "PARTIAL"):
            decision_type = "MANUAL_REVIEW"
            approved_amount = Decimal(0)
            reason = f"Flagged for manual review: {'; '.join(fraud_signals)}"

        degraded = ledger.get_degraded_components()
        if degraded and decision_type not in ("MANUAL_REVIEW", "NEEDS_MEMBER_ACTION"):
            reason += f" (Note: {', '.join(degraded)} check could not be completed this run; manual review is recommended due to incomplete processing.)"

        confidence = self._compute_confidence(extractions, degraded, fraud_score)

        return self._finalize(
            submission, ledger, decision_type, approved_amount, reason,
            confidence=confidence,
            manual_review_recommended=bool(fraud_signals) or bool(degraded),
            fraud_signals=fraud_signals,
            warnings=policy_result.warnings,
            line_item_breakdown=policy_result.line_items,
            financial_breakdown=policy_result.financial_breakdown,
        )

    def _find_blocking_readability_issue(self, submission: ClaimSubmission, extractions: List) -> str:
        if len(extractions) < len(submission.documents):
            return "One or more documents could not be processed due to a system error. Please try re-uploading them."

        required_types = set(self.doc_gate.requirements.get(submission.category, {}).get("required", []))
        for ext in extractions:
            if ext.flags and ext.file_type in required_types:
                issue = ext.flags[0].replace("_", " ").lower()
                return (
                    f"Your {ext.file_type.replace('_', ' ').lower()} ({ext.file_id}) {issue} and could not be read. "
                    f"Please re-upload a clearer copy of this document."
                )
        return ""

    def _compute_confidence(self, extractions: List, degraded: List[str], fraud_score: float) -> float:
        if extractions:
            base = sum(e.confidence_score for e in extractions) / len(extractions)
        else:
            base = 0.5
        if degraded:
            base *= 0.6
        base -= fraud_score * 0.1
        return max(0.05, min(1.0, round(base, 3)))

    def _finalize(
        self, submission: ClaimSubmission, ledger: TraceLedger, decision: str, approved_amount: Decimal,
        reason: str, confidence: float, manual_review_recommended: bool = False,
        fraud_signals: List[str] = None, warnings: List[str] = None,
        line_item_breakdown=None, financial_breakdown=None,
    ) -> ClaimDecision:
        result = ClaimDecision(
            claim_id=submission.claim_id,
            decision=decision,
            approved_amount=approved_amount,
            claimed_amount=submission.claimed_amount,
            reason=reason,
            confidence_score=confidence,
            manual_review_recommended=manual_review_recommended,
            degraded_components=ledger.get_degraded_components(),
            fraud_signals=fraud_signals or [],
            warnings=warnings or [],
            line_item_breakdown=line_item_breakdown or [],
            financial_breakdown=financial_breakdown,
            trace=ledger.events,
        )
        claim_store.record(submission, result)
        return result
