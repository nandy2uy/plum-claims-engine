"""Document completeness gate — the first real check in the pipeline.

Component contract
-------------------
Input:  ClaimSubmission (category + list of DocumentSource, each with a
        caller-declared file_type), TraceLedger.
Output: (passed: bool, payload: dict).
        When passed is False, payload = {"decision": "NEEDS_MEMBER_ACTION",
        "message": <specific, actionable string>}.
        When passed is True, payload = {}.
Errors: none raised — an unrecognized category is treated as a normal
        blocking outcome (NEEDS_MEMBER_ACTION), not an exception, since it's
        caller input, not a bug.

Note: this agent trusts the member's self-declared file_type per document
(there's no reference-free way to know a JPEG is "actually" a prescription
without running extraction on it first). What it verifies is completeness —
do the required *categories* of document exist for this claim type — which
is exactly what the assignment's wrong-document example describes (a
consultation claim missing its required hospital bill because two
prescriptions were uploaded instead).
"""

import time
from typing import Dict, Any, Tuple
from src.models.claim import ClaimSubmission
from src.models.trace import TraceLedger


class DocumentGateAgent:
    def __init__(self, policy_config: Dict[str, Any]):
        self.requirements = policy_config.get("document_requirements", {})

    def evaluate(self, submission: ClaimSubmission, ledger: TraceLedger) -> Tuple[bool, dict]:
        start_time = time.time()
        category = submission.category  # already normalized (upper, underscores) by the model
        category_reqs = self.requirements.get(category)

        if not category_reqs:
            ledger.log(
                component="DOC_GATE",
                outcome="FAIL",
                effect="BLOCK",
                evidence={"category": submission.category, "issue": "Unknown category"},
                duration_ms=int((time.time() - start_time) * 1000),
            )
            known = ", ".join(sorted(self.requirements.keys())).lower()
            return False, {
                "decision": "NEEDS_MEMBER_ACTION",
                "message": (
                    f"'{submission.category}' is not a recognized claim category. "
                    f"Supported categories are: {known}."
                ),
            }

        required_types = set(category_reqs.get("required", []))
        uploaded_types = [doc.file_type for doc in submission.documents]
        uploaded_set = set(uploaded_types)

        missing_docs = required_types - uploaded_set

        if missing_docs:
            uploaded_display = ", ".join(t.replace("_", " ").lower() for t in uploaded_types) or "no documents"
            missing_display = " and ".join(sorted(t.replace("_", " ").lower() for t in missing_docs))

            message = (
                f"This {category.replace('_', ' ').lower()} claim requires: "
                f"{', '.join(t.replace('_', ' ').lower() for t in sorted(required_types))}. "
                f"You uploaded {len(submission.documents)} document(s) ({uploaded_display}), "
                f"which does not include a {missing_display}. "
                f"Please upload the missing {missing_display} to proceed."
            )

            ledger.log(
                component="DOC_GATE",
                rule_id=f"doc_req_{category}",
                outcome="FAIL",
                effect="BLOCK",
                policy_ref=f"document_requirements.{category}.required",
                evidence={"missing": sorted(missing_docs), "uploaded": uploaded_types},
                duration_ms=int((time.time() - start_time) * 1000),
            )

            return False, {"decision": "NEEDS_MEMBER_ACTION", "message": message}

        ledger.log(
            component="DOC_GATE",
            rule_id=f"doc_req_{category}",
            outcome="PASS",
            effect="NONE",
            policy_ref=f"document_requirements.{category}.required",
            evidence={"uploaded": uploaded_types},
            duration_ms=int((time.time() - start_time) * 1000),
        )

        return True, {}
