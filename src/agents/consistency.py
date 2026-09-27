"""Consistency agent: do the documents describe one patient, one treatment episode?

Component contract
-------------------
Input:  ClaimSubmission, MemberContext, List[DocumentExtraction] (usable ones), TraceLedger.
Output: ConsistencyResult(passed, message, actions)
        passed=False -> NEEDS_MEMBER_ACTION with the exact name found on each file
        and which file(s) disagree with the patient on record.
Errors: none raised.

Checks (each logged):
  patient_names_across_documents  every pair of documents, keyed by file_id (v1
      keyed by document TYPE, so a second prescription silently overwrote the
      first). Uses match_names(): a shared surname alone is a MISMATCH ("Rajesh
      Kumar" vs his son "Arjun Kumar" passed in v1 because they share "kumar").
  patient_name_vs_roster          names on documents vs the patient on record.
  identity_evidence               no name on any document -> WARN + PAYOUT
                                  confidence factor (TC007/TC009/TC011/TC012).
  document_dates                  dates on documents vs treatment_date
                                  (tolerance: interpretation.document_date_tolerance_days) -> WARN.
  hospital_name                   bill's hospital vs the hospital the member typed -> WARN.
Fuzzy / initials / first-name-only matches pass with a WARN and a confidence factor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, List, Optional

from src.agents.intake import MemberContext
from src.core.text import fmt_date, humanize, match_names, normalize_text
from src.models.claim import ClaimSubmission, MemberAction
from src.models.medical import DocumentExtraction
from src.models.policy import Interpretation
from src.models.trace import Stopwatch, TraceLedger

COMPONENT = "CONSISTENCY"


@dataclass
class ConsistencyResult:
    passed: bool = True
    message: str = ""
    actions: List[MemberAction] = field(default_factory=list)


class ConsistencyAgent:
    def __init__(self, interpretation: Interpretation):
        self.interp = interpretation
        self.weights = interpretation.confidence
        self.threshold = interpretation.name_match_fuzzy_threshold

    def evaluate(self, submission: ClaimSubmission, context: MemberContext, extractions: List[DocumentExtraction],
                 ledger: TraceLedger) -> ConsistencyResult:
        watch = Stopwatch()
        named: Dict[str, DocumentExtraction] = {e.file_id: e for e in extractions if e.patient_name()}
        names = {fid: e.patient_name() for fid, e in named.items()}

        # 1. Document vs document.
        weakest = "EXACT"
        for a, b in combinations(named.values(), 2):
            match = match_names(a.patient_name(), b.patient_name(), self.threshold)
            if not match.is_match:
                return self._name_mismatch(submission, context, named, ledger, watch)
            weakest = self._weaker(weakest, match.level)
        if len(named) > 1:
            self._log_name_pass(ledger, "patient_names_across_documents", weakest,
                                f"Patient names agree across {len(named)} documents ({weakest.lower()} match).", names)

        # 2. Documents vs roster.
        if named and context.patient_name:
            worst = "EXACT"
            for ext in named.values():
                match = match_names(ext.patient_name(), context.patient_name, self.threshold)
                if not match.is_match:
                    return self._roster_mismatch(submission, context, named, ledger, watch)
                worst = self._weaker(worst, match.level)
            self._log_name_pass(ledger, "patient_name_vs_roster", worst,
                                f"Documents name the patient on record, {context.patient_name} ({worst.lower()} match).",
                                {**names, "roster": context.patient_name})
        elif named:
            ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="patient_name_vs_roster",
                       summary="Patient has no roster record, so document names cannot be checked against it.",
                       evidence=names)
        else:
            ledger.log(COMPONENT, "WARN", rule_id="identity_evidence",
                       summary="No patient name could be found on any document; identity rests on the member-entered ID only.",
                       evidence={"documents": [e.label for e in extractions]},
                       interpretation_ref="confidence.identity_unverified",
                       confidence_factor=self.weights.identity_unverified, confidence_scope="PAYOUT")

        # 3. Document dates vs treatment date.
        tolerance = self.interp.document_date_tolerance_days
        dated = [(e, e.document_date()) for e in extractions if e.document_date()]
        off = [(e, d) for e, d in dated if abs((d - submission.treatment_date).days) > tolerance]
        if not dated:
            ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="document_dates", summary="No dates could be read on the documents.")
        elif off:
            ledger.log(COMPONENT, "WARN", rule_id="document_dates",
                       summary="Document date(s) differ from the treatment date by more than "
                               f"{tolerance} days: " + "; ".join(f"'{e.label}' dated {fmt_date(d)}" for e, d in off),
                       evidence={"treatment_date": submission.treatment_date.isoformat(),
                                 "documents": {e.label: d.isoformat() for e, d in dated}},
                       interpretation_ref="document_date_tolerance_days",
                       confidence_factor=self.weights.document_date_mismatch, confidence_scope="PAYOUT")
        else:
            ledger.log(COMPONENT, "PASS", rule_id="document_dates",
                       summary=f"Document dates are consistent with the treatment date {fmt_date(submission.treatment_date)}.",
                       evidence={e.label: d.isoformat() for e, d in dated})

        # 4. Hospital named by the member vs on the bill.
        bill_hospitals = {e.bill_data.hospital_name for e in extractions if e.bill_data and e.bill_data.hospital_name}
        if submission.hospital_name and bill_hospitals:
            entered = normalize_text(submission.hospital_name)
            if not any(entered in normalize_text(h) or normalize_text(h) in entered for h in bill_hospitals):
                ledger.log(COMPONENT, "WARN", rule_id="hospital_name",
                           summary=f"Member entered '{submission.hospital_name}' but the bill is from "
                                   f"{', '.join(sorted(bill_hospitals))}; the bill is used as the source of truth.",
                           evidence={"entered": submission.hospital_name, "on_bill": sorted(bill_hospitals)})

        ledger.log(COMPONENT, "PASS", rule_id="consistency", summary="Documents are consistent with each other and the claim.",
                   duration_ms=watch.ms())
        return ConsistencyResult(True)

    # ------------------------------------------------------------ outcomes

    def _name_mismatch(self, submission, context: MemberContext, named, ledger, watch) -> ConsistencyResult:
        listing = "; ".join(f"'{e.label}' ({humanize(e.effective_type)}) is for {e.patient_name()}" for e in named.values())
        wrong = self._disagreeing_with_roster(context, named)
        if wrong and len(wrong) < len(named):
            fix = " ".join(
                f"Please replace '{e.label}' with the {humanize(e.effective_type)} issued to {context.patient_name}."
                for e in wrong)
            actions = [MemberAction(action="REPLACE_DOCUMENT", file_id=e.file_id, document_type=e.effective_type,
                                    detail=f"Replace '{e.label}' with a document issued to {context.patient_name}.") for e in wrong]
        else:
            fix = "Please upload documents that all belong to the same patient."
            actions = [MemberAction(action="REPLACE_DOCUMENT", detail="Upload documents that all belong to one patient.")]
        other = sorted({e.patient_name() for e in (wrong or [])})
        message = (
            f"The documents in this claim belong to different people: {listing}. "
            f"This claim is for {context.patient_name or context.patient_id}, so every document must be in their name. {fix}"
            + (f" If {', '.join(other)} is a covered family member, submit a separate claim for them." if other else "")
        )
        ledger.log(COMPONENT, "FAIL", effect="BLOCK", rule_id="patient_names_across_documents", summary=message,
                   evidence={"names_by_file": {e.label: e.patient_name() for e in named.values()},
                             "patient_on_record": context.patient_name,
                             "disagreeing_files": [e.label for e in wrong]},
                   duration_ms=watch.ms())
        return ConsistencyResult(False, message, actions)

    def _roster_mismatch(self, submission, context: MemberContext, named, ledger, watch) -> ConsistencyResult:
        wrong = self._disagreeing_with_roster(context, named)
        names = sorted({e.patient_name() for e in wrong})
        message = (
            f"The patient named on your documents ({', '.join(names)}) does not match the patient for this claim, "
            f"{context.patient_name} ({context.patient_id}). Please upload documents issued to {context.patient_name}, "
            f"or, if the treatment was for a covered family member, resubmit the claim with that person selected as the patient."
        )
        ledger.log(COMPONENT, "FAIL", effect="BLOCK", rule_id="patient_name_vs_roster", summary=message,
                   evidence={"names_by_file": {e.label: e.patient_name() for e in named.values()},
                             "patient_on_record": context.patient_name}, duration_ms=watch.ms())
        return ConsistencyResult(False, message, [MemberAction(action="CORRECT_DETAILS",
                                                               detail="Upload the patient's own documents or pick the right patient.")])

    def _disagreeing_with_roster(self, context: MemberContext, named) -> List[DocumentExtraction]:
        if not context.patient_name:
            return []
        return [e for e in named.values() if not match_names(e.patient_name(), context.patient_name, self.threshold).is_match]

    def _log_name_pass(self, ledger, rule_id, level, summary, evidence):
        factor: Optional[float] = None
        if level in ("FUZZY", "INITIALS"):
            factor = self.weights.fuzzy_name_match
        elif level == "PARTIAL":
            factor = self.weights.partial_name_match
        ledger.log(COMPONENT, "PASS" if factor is None else "WARN", rule_id=rule_id, summary=summary, evidence=evidence,
                   confidence_factor=factor, confidence_scope="PAYOUT")

    @staticmethod
    def _weaker(a: str, b: str) -> str:
        order = ["EXACT", "FUZZY", "INITIALS", "PARTIAL"]
        return a if order.index(a) >= order.index(b) else b
