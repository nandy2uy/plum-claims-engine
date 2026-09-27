"""Document verification agent: post-extraction checks on what was actually uploaded.

Component contract
-------------------
Input:  ClaimSubmission, List[DocumentExtraction], TraceLedger.
Output: VerificationResult
          status  OK                  -> continue
                  NEEDS_MEMBER_ACTION -> stop; `message` + `actions` name each file
                                         to re-upload or replace, and why
                  DEGRADED            -> continue, but a REQUIRED document could not
                                         be extracted because of a system fault; the
                                         claim ends in MANUAL_REVIEW
          system_failed_required  labels of those documents
Errors: none raised.

Checks (each logged):
  readability       MEMBER-fault failures on required documents -> re-upload that
                    specific file (TC002). On optional documents -> WARN, continue.
  detected_type     the model's detected type vs the member's declared type. If
                    the mismatch leaves a required type missing (e.g. a prescription
                    uploaded as the hospital bill), the member is asked to replace
                    that file. Otherwise it is accepted with a WARN.
  registration      doctor registration numbers validated against the state /
                    AYUSH formats in interpretation.registration_patterns. Missing
                    or malformed numbers lower payout confidence, but never block
                    on their own: a rubber stamp over the number is common.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal

from src.agents.doc_gate import DocumentGateAgent
from src.core.text import classify_registration, fmt_date, humanize
from src.models.claim import ClaimSubmission, MemberAction
from src.models.medical import FLAG_TYPE_MISMATCH, DocumentExtraction
from src.models.policy import Interpretation
from src.models.trace import Stopwatch, TraceLedger

COMPONENT = "DOC_VERIFY"


@dataclass
class VerificationResult:
    status: Literal["OK", "NEEDS_MEMBER_ACTION", "DEGRADED"] = "OK"
    message: str = ""
    actions: List[MemberAction] = field(default_factory=list)
    system_failed_required: List[str] = field(default_factory=list)


class DocumentVerificationAgent:
    def __init__(self, gate: DocumentGateAgent, interpretation: Interpretation):
        self.gate = gate
        self.interp = interpretation
        self.weights = interpretation.confidence

    def evaluate(self, submission: ClaimSubmission, extractions: List[DocumentExtraction], ledger: TraceLedger) -> VerificationResult:
        watch = Stopwatch()
        category = submission.category
        required = set(self.gate.required_types(category))
        problems: List[str] = []
        actions: List[MemberAction] = []

        # 1. Member-fixable failures (unreadable / unsupported / empty file).
        for ext in extractions:
            if ext.failure_kind != "MEMBER":
                continue
            doc_type = ext.effective_type
            if doc_type in required or ext.declared_type in required:
                problems.append(self._reupload_text(ext))
                actions.append(MemberAction(action="REUPLOAD_DOCUMENT", file_id=ext.file_id, document_type=ext.declared_type,
                                            detail=f"Re-upload a clear copy of '{ext.label}' ({humanize(ext.declared_type)})."))
                ledger.log(COMPONENT, "FAIL", effect="BLOCK", rule_id="readability",
                           summary=f"Required {humanize(ext.declared_type)} '{ext.label}' cannot be used ({', '.join(ext.flags)}); "
                                   f"member asked to re-upload that file.",
                           evidence={"file_id": ext.file_id, "flags": ext.flags, "error": ext.error})
            else:
                ledger.log(COMPONENT, "WARN", rule_id="readability",
                           summary=f"Optional document '{ext.label}' cannot be used ({', '.join(ext.flags)}); continuing without it.",
                           evidence={"file_id": ext.file_id, "flags": ext.flags})

        # 2. Detected vs declared type.
        mismatched: List[DocumentExtraction] = []
        for ext in extractions:
            if ext.usable and ext.detected_type and ext.detected_type != ext.declared_type:
                ext.flags.append(FLAG_TYPE_MISMATCH)
                mismatched.append(ext)
        present = [
            (ext.declared_type if ext.failure_kind == "SYSTEM" else ext.effective_type)
            for ext in extractions if ext.failure_kind != "MEMBER"
        ]
        present += [ext.declared_type for ext in extractions if ext.failure_kind == "MEMBER"]  # already being re-uploaded
        missing = self.gate.missing_types(category, present)
        if missing:
            culprits = [e for e in mismatched if e.declared_type in missing]
            for ext in culprits:
                problems.append(
                    f"'{ext.label}' was uploaded as your {humanize(ext.declared_type)}, but it appears to be a "
                    f"{humanize(ext.effective_type)}. Please upload {self.interp.describe_document(ext.declared_type)} "
                    f"for your treatment on {fmt_date(submission.treatment_date)} in its place."
                )
                actions.append(MemberAction(action="REPLACE_DOCUMENT", file_id=ext.file_id, document_type=ext.declared_type,
                                            detail=f"Replace '{ext.label}' with {self.interp.describe_document(ext.declared_type)}."))
            unexplained = [m for m in missing if m not in {e.declared_type for e in culprits}]
            if unexplained:
                msg, missing_actions = self.gate.missing_message(submission, [(e.label, e.effective_type) for e in extractions], unexplained)
                problems.append(msg)
                actions.extend(missing_actions)
            ledger.log(COMPONENT, "FAIL", effect="BLOCK", rule_id="detected_type",
                       summary=f"After reading the documents, required type(s) missing: {', '.join(humanize(m) for m in missing)}.",
                       policy_ref=f"document_requirements.{category}.required",
                       evidence={"missing": missing, "mismatched": [
                           {"file": e.label, "declared": e.declared_type, "detected": e.detected_type} for e in mismatched]})
        else:
            for ext in mismatched:
                ledger.log(COMPONENT, "WARN", rule_id="detected_type",
                           summary=f"'{ext.label}' was declared {humanize(ext.declared_type)} but reads as "
                                   f"{humanize(ext.detected_type)}; requirements are still met, so it was accepted as detected.",
                           evidence={"file_id": ext.file_id, "declared": ext.declared_type, "detected": ext.detected_type},
                           confidence_factor=self.weights.type_mismatch_accepted, confidence_scope="PAYOUT")
            ledger.log(COMPONENT, "PASS", rule_id="detected_type",
                       summary="Detected document types satisfy the claim's document requirements.",
                       evidence={"types": [{"file": e.label, "declared": e.declared_type, "detected": e.detected_type}
                                           for e in extractions]})

        if problems:
            lead = "We need you to fix " + ("one document" if len(problems) == 1 else f"{len(problems)} things") + \
                   " before we can assess this claim. "
            message = lead + " ".join(problems) + " Your claim has not been rejected — it will continue as soon as the corrected documents are uploaded."
            return VerificationResult("NEEDS_MEMBER_ACTION", message, actions)

        # 3. Doctor registration numbers (non-blocking).
        self._check_registrations(extractions, ledger)

        # 4. System failures on required documents -> degrade, don't blame the member.
        system_failed = [e.label for e in extractions if e.failure_kind == "SYSTEM" and e.declared_type in required]
        if system_failed:
            ledger.log(COMPONENT, "WARN", effect="REVIEW", rule_id="system_extraction_failure",
                       summary=f"Required document(s) {', '.join(system_failed)} could not be extracted due to a system error; "
                               f"claim will be routed to manual review instead of asking the member to re-upload.",
                       evidence={"files": system_failed}, duration_ms=watch.ms())
            return VerificationResult("DEGRADED", system_failed_required=system_failed)
        return VerificationResult("OK")

    def _reupload_text(self, ext: DocumentExtraction) -> str:
        reason = {
            "UNREADABLE": "the image is too blurry or unclear to read",
            "UNSUPPORTED_FILE_TYPE": ext.error or "the file type is not supported",
            "NO_CONTENT_PROVIDED": "the file arrived empty",
            "MISSING_CRITICAL_FIELDS": ext.error or "we could read the document but not its key details",
            "FIXTURE_NOT_ALLOWED": ext.error or "pre-extracted data is not accepted",
        }
        why = next((reason[f] for f in ext.flags if f in reason), ext.error or "it could not be read")
        return (
            f"We couldn't use '{ext.label}', which you uploaded as your {humanize(ext.declared_type)} — {why}. "
            f"Please re-upload a clear photo or scan of this {humanize(ext.declared_type)}: place it flat in good light, keep "
            f"all four corners in frame, and make sure names, dates and amounts are legible (JPG, PNG or PDF)."
        )

    def _check_registrations(self, extractions: List[DocumentExtraction], ledger: TraceLedger) -> None:
        prescriptions = [e for e in extractions if e.usable and e.prescription_data is not None]
        if not prescriptions:
            ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="doctor_registration",
                       summary="No prescription in this claim, so there is no doctor registration number to validate.")
            return
        for ext in prescriptions:
            reg = ext.prescription_data.registration_number
            valid, pattern = classify_registration(reg, self.interp.registration_patterns)
            if valid:
                ledger.log(COMPONENT, "PASS", rule_id="doctor_registration",
                           summary=f"Doctor registration '{reg}' on '{ext.label}' matches the {pattern} format.",
                           interpretation_ref="registration_patterns", evidence={"file_id": ext.file_id, "registration": reg, "format": pattern})
            elif not reg:
                ledger.log(COMPONENT, "WARN", rule_id="doctor_registration",
                           summary=f"No doctor registration number could be read on '{ext.label}'; prescriber is unverified.",
                           evidence={"file_id": ext.file_id}, interpretation_ref="confidence.missing_registration",
                           confidence_factor=self.weights.missing_registration, confidence_scope="PAYOUT")
            else:
                ledger.log(COMPONENT, "WARN", rule_id="doctor_registration",
                           summary=f"Doctor registration '{reg}' on '{ext.label}' does not match any known Indian council format.",
                           evidence={"file_id": ext.file_id, "registration": reg}, interpretation_ref="registration_patterns",
                           confidence_factor=self.weights.invalid_registration, confidence_scope="PAYOUT")
