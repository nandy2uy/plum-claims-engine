"""Orchestrator: wires the agents into the claim pipeline.

Component contract
-------------------
ClaimsOrchestrator(policy, interpretation, settings, store, backend, today)
    async process(submission: ClaimSubmission) -> ClaimDecision
Errors: none. Every stage runs behind run_stage (timeout + isolation); anything that
still escapes is caught here and turned into a MANUAL_REVIEW decision that keeps the
trace recorded so far.

Pipeline (each arrow is a traced stage):

  INTAKE ─▶ DOC_GATE ─▶ EXTRACTOR ─▶ DOC_VERIFY ─▶ CONSISTENCY ─▶ ┬─ POLICY_ENGINE ─┬─▶ DECISION ─▶ store
  member,    declared    concurrent   detected vs   same patient,  └─ FRAUD ─────────┘
  patient,   types,      per-doc      declared,     same episode      (concurrent, on forked ledgers
  dates      pre-LLM     vision       readability                     merged in fixed order)

Stop points (status NEEDS_MEMBER_ACTION, decision null): INTAKE, DOC_GATE, DOC_VERIFY,
CONSISTENCY. Criticality on failure:
  EXTRACTOR     critical-ish: every document is marked as a SYSTEM failure, DOC_VERIFY
                reports DEGRADED and the claim ends in MANUAL_REVIEW (member not blamed)
  DOC_VERIFY    critical: treated as DEGRADED
  CONSISTENCY   non-critical: skipped, confidence reduced, manual review recommended
  POLICY_ENGINE critical: MANUAL_REVIEW (POLICY_ENGINE_UNAVAILABLE)
  FRAUD         non-critical: decision still issued, failure visible, confidence reduced,
                high-value claims held for review
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Callable, List, Optional

from src.agents.consistency import ConsistencyAgent, ConsistencyResult
from src.agents.decision import DecisionAgent
from src.agents.doc_gate import DocumentGateAgent
from src.agents.doc_verifier import DocumentVerificationAgent, VerificationResult
from src.agents.extractor import ExtractionAgent
from src.agents.fraud import FraudAgent, document_fingerprints
from src.agents.intake import IntakeAgent
from src.agents.policy_engine import PolicyEngine
from src.core.config import Settings, config_fingerprint, get_interpretation, get_policy, get_settings, llm_api_key
from src.core.fault_tolerance import FaultInjector, run_stage
from src.core.llm import DocumentExtractorBackend, VisionExtractor
from src.core.storage import ClaimStore, claim_store
from src.models.claim import ClaimDecision, ClaimSubmission
from src.models.medical import FLAG_EXTRACTION_FAILED, DocumentExtraction
from src.models.policy import Interpretation, PolicyConfig
from src.models.trace import TraceLedger

logger = logging.getLogger("claims.orchestrator")


class ClaimsOrchestrator:
    def __init__(
        self,
        policy: Optional[PolicyConfig] = None,
        interpretation: Optional[Interpretation] = None,
        settings: Optional[Settings] = None,
        store: Optional[ClaimStore] = None,
        backend: Optional[DocumentExtractorBackend] = None,
        today: Callable[[], date] = date.today,
    ):
        self.settings = settings or get_settings()
        self.policy = policy or get_policy()
        self.interp = interpretation or get_interpretation()
        self.store = store if store is not None else claim_store
        if backend is None and llm_api_key(self.settings):
            vocabulary = list(self.policy.waiting_periods.specific_conditions) + list(self.policy.exclusions.conditions)
            backend = VisionExtractor(self.settings, condition_vocabulary=vocabulary)
        fingerprint = config_fingerprint(self.settings.policy_file) if policy is None else "in-memory"

        self.intake = IntakeAgent(self.policy, fingerprint, today)
        self.gate = DocumentGateAgent(self.policy, self.interp)
        self.extractor = ExtractionAgent(self.interp, backend, self.settings.llm_max_concurrency,
                                         allow_fixtures=self.settings.allow_fixture_documents)
        self.verifier = DocumentVerificationAgent(self.gate, self.interp)
        self.consistency = ConsistencyAgent(self.interp)
        self.policy_engine = PolicyEngine(self.policy, self.interp, usage=self.store.paid_between)
        self.fraud = FraudAgent(self.policy, self.interp, self.store)
        self.decider = DecisionAgent(self.policy, self.interp)

    async def process(self, submission: ClaimSubmission) -> ClaimDecision:
        ledger = TraceLedger(submission.claim_id)
        try:
            decision, fingerprints = await self._run(submission, ledger)
        except Exception as exc:  # noqa: BLE001 - final safety net; trace is preserved
            logger.exception("claim=%s unhandled pipeline error", submission.claim_id)
            decision, fingerprints = self.decider.system_failure(submission, ledger, exc), []
        try:
            self.store.record(submission, decision, fingerprints)
        except Exception:  # noqa: BLE001 - storage must never cost the member their decision
            logger.exception("claim=%s could not be stored", submission.claim_id)
        logger.info("claim=%s status=%s decision=%s approved=%s confidence=%s", submission.claim_id,
                    decision.status, decision.decision, decision.approved_amount, decision.confidence_score)
        return decision

    async def _run(self, submission: ClaimSubmission, ledger: TraceLedger):
        s = self.settings
        degraded_factor = self.interp.confidence.degraded_component

        # 1. Intake (its first event, "claim received", is always the first line of the trace)
        intake = self.intake.evaluate(submission, ledger)
        injector = FaultInjector(submission, s, ledger)
        if not intake.ok:
            return self.decider.needs_member_action(submission, ledger, "INTAKE", intake.message, intake.actions), []
        context = intake.context

        # 2. Document gate (declared types, before any LLM spend)
        gate = self.gate.evaluate(submission, ledger)
        if not gate.passed:
            return self.decider.needs_member_action(submission, ledger, "DOC_GATE", gate.message, gate.actions), []

        # 3. Extraction (concurrent per document)
        stage = await run_stage(ledger, "EXTRACTOR", self.extractor.process, submission, ledger,
                                timeout=s.extraction_stage_timeout_seconds, critical=True, injector=injector,
                                degraded_confidence=degraded_factor)
        extractions: List[DocumentExtraction] = stage.value if stage.ok else self._all_failed(submission, stage.error)

        # 4. Post-extraction document verification
        stage = await run_stage(ledger, "DOC_VERIFY", self.verifier.evaluate, submission, extractions, ledger,
                                timeout=s.stage_timeout_seconds, critical=True, injector=injector,
                                fallback=VerificationResult("DEGRADED", system_failed_required=["(verification unavailable)"]),
                                degraded_confidence=degraded_factor)
        verification: VerificationResult = stage.value
        if verification.status == "NEEDS_MEMBER_ACTION":
            return self.decider.needs_member_action(submission, ledger, "DOC_VERIFY", verification.message,
                                                    verification.actions, extractions), []
        usable = [e for e in extractions if e.usable]

        # 5. Cross-document consistency
        stage = await run_stage(ledger, "CONSISTENCY", self.consistency.evaluate, submission, context, usable, ledger,
                                timeout=s.stage_timeout_seconds, critical=False, injector=injector,
                                fallback=ConsistencyResult(True), degraded_confidence=degraded_factor)
        consistency: ConsistencyResult = stage.value
        if not consistency.passed:
            return self.decider.needs_member_action(submission, ledger, "CONSISTENCY", consistency.message,
                                                    consistency.actions, extractions), []

        # 6. Policy and fraud, concurrently, each on its own forked ledger
        fingerprints = document_fingerprints(submission, extractions)
        policy_ledger, fraud_ledger = ledger.fork(), ledger.fork()
        policy_stage, fraud_stage = await asyncio.gather(
            run_stage(policy_ledger, "POLICY_ENGINE", self.policy_engine.evaluate, submission, context, usable,
                      policy_ledger, timeout=s.stage_timeout_seconds, critical=True, injector=injector, offload=True,
                      degraded_confidence=degraded_factor),
            run_stage(fraud_ledger, "FRAUD", self.fraud.evaluate, submission, usable, fingerprints, fraud_ledger,
                      timeout=s.stage_timeout_seconds, critical=False, injector=injector, offload=True,
                      degraded_confidence=degraded_factor),
        )
        ledger.merge(policy_ledger)
        ledger.merge(fraud_ledger)

        # 7. Final decision
        decision = self.decider.decide(submission, extractions, verification, policy_stage, fraud_stage, ledger)
        return decision, fingerprints

    @staticmethod
    def _all_failed(submission: ClaimSubmission, error: Optional[str]) -> List[DocumentExtraction]:
        return [
            DocumentExtraction(file_id=d.file_id, file_name=d.file_name, declared_type=d.file_type, failure_kind="SYSTEM",
                               flags=[FLAG_EXTRACTION_FAILED], error=error or "Extraction stage failed.")
            for d in submission.documents
        ]
