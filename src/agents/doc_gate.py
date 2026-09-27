"""Document gate: are the right KINDS of document present for this claim type?

Component contract
-------------------
Input:  ClaimSubmission, TraceLedger.
Output: GateResult(passed, message, actions, missing)
        passed=False -> NEEDS_MEMBER_ACTION. `message` names every uploaded file
        with the type it was uploaded as, the required types that are missing,
        and what each missing document must contain. `actions` holds one
        UPLOAD_DOCUMENT per missing type.
Errors: none raised; an unknown category is a normal blocking outcome.

Runs BEFORE any LLM call (assignment: "before any processing happens"), using the
types the member declared, so obviously incomplete claims cost nothing. Those
declarations are not trusted blindly: after extraction, DocumentVerificationAgent
re-runs `missing_types()` on the types the model actually DETECTED, which
catches a prescription uploaded as a "hospital bill".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

from src.core.text import fmt_date, humanize
from src.models.claim import ClaimSubmission, MemberAction
from src.models.policy import Interpretation, PolicyConfig
from src.models.trace import Stopwatch, TraceLedger

COMPONENT = "DOC_GATE"


@dataclass
class GateResult:
    passed: bool
    message: str = ""
    actions: List[MemberAction] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)


def describe_uploads(files: Sequence[Tuple[str, str]]) -> str:
    """[('a.jpg','PRESCRIPTION'), ...] -> "'a.jpg' (prescription) and 'b.jpg' (prescription)"."""
    parts = [f"'{label}' ({humanize(t)})" for label, t in files]
    if not parts:
        return "no documents"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


class DocumentGateAgent:
    def __init__(self, policy: PolicyConfig, interpretation: Interpretation):
        self.policy = policy
        self.interp = interpretation

    def required_types(self, category: str) -> List[str]:
        req = self.policy.document_requirements.get(category)
        return list(req.required) if req else []

    def missing_types(self, category: str, present: Iterable[str]) -> List[str]:
        present_set = set(present)
        return [t for t in self.required_types(category) if t not in present_set]

    def missing_message(self, submission: ClaimSubmission, files: Sequence[Tuple[str, str]], missing: List[str],
                        lead: str = "") -> Tuple[str, List[MemberAction]]:
        category = humanize(submission.category)
        required = self.required_types(submission.category)
        needs = " and ".join(f"a {humanize(t)}" for t in required)
        missing_desc = "; ".join(f"a {self.interp.describe_document(t)}" for t in missing)
        count = len(files)
        msg = (
            f"{lead}We can't process this {category} claim yet because it is missing "
            f"{' and '.join('a ' + humanize(t) for t in missing)}. "
            f"A {category} claim needs {needs}. You uploaded {count} document{'s' if count != 1 else ''}: "
            f"{describe_uploads(files)}. "
            f"Please upload {missing_desc} for your treatment on {fmt_date(submission.treatment_date)}"
        )
        req = self.policy.document_requirements.get(submission.category)
        surplus = surplus_files(files, required, req.optional if req else [])
        msg += f" in place of {describe_uploads(surplus)}." if surplus else "."
        actions = [
            MemberAction(action="UPLOAD_DOCUMENT", document_type=t,
                         detail=f"Upload {self.interp.describe_document(t)}.")
            for t in missing
        ]
        return msg, actions

    def evaluate(self, submission: ClaimSubmission, ledger: TraceLedger) -> GateResult:
        watch = Stopwatch()
        category = submission.category
        if category not in self.policy.document_requirements:
            known = ", ".join(humanize(c) for c in sorted(self.policy.document_requirements))
            message = f"'{humanize(category)}' is not a claim category under this policy. Supported categories: {known}."
            ledger.log(COMPONENT, "FAIL", effect="BLOCK", rule_id="known_category", summary=message,
                       policy_ref="document_requirements", evidence={"category": category}, duration_ms=watch.ms())
            return GateResult(False, message, [MemberAction(action="CORRECT_DETAILS", detail="Choose a supported claim category.")])

        files = [(d.label, d.file_type) for d in submission.documents]
        missing = self.missing_types(category, [t for _, t in files])
        ref = f"document_requirements.{category}"
        if missing:
            message, actions = self.missing_message(submission, files, missing)
            ledger.log(COMPONENT, "FAIL", effect="BLOCK", rule_id="required_documents", summary=message,
                       policy_ref=f"{ref}.required",
                       evidence={"required": self.required_types(category), "missing": missing,
                                 "uploaded": [{"file": label, "declared_type": t} for label, t in files]},
                       duration_ms=watch.ms())
            return GateResult(False, message, actions, missing)

        req = self.policy.document_requirements[category]
        unused = [label for label, t in files if t not in req.required and t not in req.optional]
        ledger.log(COMPONENT, "PASS", rule_id="required_documents",
                   summary=f"All required document types present for {humanize(category)}: "
                           f"{', '.join(humanize(t) for t in req.required)} (as declared by the member; verified "
                           f"against detected types after extraction).",
                   policy_ref=f"{ref}.required",
                   evidence={"uploaded": [{"file": label, "declared_type": t} for label, t in files],
                             "not_used_for_this_category": unused},
                   duration_ms=watch.ms())
        return GateResult(True)



def surplus_files(files: Sequence[Tuple[str, str]], required: List[str], optional: List[str]) -> List[Tuple[str, str]]:
    """Uploads that don't help this claim: types neither required nor optional,
    plus every repeat of a required type after its first occurrence."""
    seen, surplus = set(), []
    for label, doc_type in files:
        if (doc_type not in required and doc_type not in optional) or (doc_type in required and doc_type in seen):
            surplus.append((label, doc_type))
        seen.add(doc_type)
    return surplus
