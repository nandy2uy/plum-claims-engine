"""Cross-document and document-to-member identity consistency checks.

Component contract
-------------------
Input:  ClaimSubmission, List[DocumentExtraction], TraceLedger.
Output: (consistent: bool, message: str). message is empty when consistent.
Errors: none raised — a mismatch is a normal blocking outcome
        (NEEDS_MEMBER_ACTION upstream), not a bug.

Two checks, in order:
1. Doc-to-doc: do the patient names found across documents agree with each
   other (token-set match, so "Mr. Rajesh Kumar" == "Rajesh Kumar")?
2. Doc-to-roster: do those names agree with the policy member on record for
   this claim's member_id/patient_id? This catches the case the doc-to-doc
   check alone would miss — two documents that agree with each other but
   belong to someone other than the policyholder.
"""

import time
import re
from typing import List, Tuple, Dict, Any
from src.models.claim import ClaimSubmission
from src.models.medical import DocumentExtraction
from src.models.trace import TraceLedger

_HONORIFIC_RE = re.compile(r"\b(mr|mrs|ms|dr|shri|smt)\.?\b")


def _normalize(name: str) -> set:
    clean = _HONORIFIC_RE.sub("", name.lower())
    return set(clean.split())


class ConsistencyAgent:
    def __init__(self, policy_config: Dict[str, Any]):
        self.members = {m["member_id"]: m for m in policy_config.get("members", [])}

    def evaluate(self, submission: ClaimSubmission, extractions: List[DocumentExtraction], ledger: TraceLedger) -> Tuple[bool, str]:
        start_time = time.time()
        doc_names: Dict[str, str] = {}
        for ext in extractions:
            name = ext.patient_name()
            if name:
                doc_names[ext.file_type] = name

        if len(doc_names) > 1:
            ref_type, ref_name = next(iter(doc_names.items()))
            ref_tokens = _normalize(ref_name)
            for doc_type, name in doc_names.items():
                if not ref_tokens & _normalize(name):
                    pairs = "; ".join(f"{t.replace('_', ' ').lower()}: {n}" for t, n in doc_names.items())
                    return self._fail(
                        ledger, start_time,
                        f"The patient names on your documents do not match ({pairs}). "
                        f"Please verify you uploaded the right documents for this claim.",
                        {"mismatch": doc_names, "check": "doc_to_doc"},
                    )

        member = self.members.get(submission.patient_id) or self.members.get(submission.member_id)
        if member and doc_names:
            roster_tokens = _normalize(member["name"])
            for doc_type, name in doc_names.items():
                if not roster_tokens & _normalize(name):
                    return self._fail(
                        ledger, start_time,
                        f"The patient name on your {doc_type.replace('_', ' ').lower()} "
                        f"('{name}') does not match the policy member on file "
                        f"('{member['name']}'). Please upload documents belonging to the "
                        f"covered member, or submit under the correct member/dependent ID.",
                        {"mismatch": {"document": name, "policy_member": member["name"]}, "check": "doc_to_roster"},
                    )

        ledger.log(
            component="CONSISTENCY", outcome="PASS", effect="NONE",
            evidence={"patient_names_seen": doc_names},
            duration_ms=int((time.time() - start_time) * 1000),
        )
        return True, ""

    @staticmethod
    def _fail(ledger: TraceLedger, start_time: float, message: str, evidence: dict) -> Tuple[bool, str]:
        ledger.log(
            component="CONSISTENCY", outcome="FAIL", effect="BLOCK",
            evidence=evidence,
            duration_ms=int((time.time() - start_time) * 1000),
        )
        return False, message
