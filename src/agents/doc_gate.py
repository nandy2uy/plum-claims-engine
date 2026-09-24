from typing import Dict, Any, Tuple
from src.models.claim import ClaimSubmission
from src.models.trace import TraceLedger, TraceEvent
import time

class DocumentGateAgent:
    def __init__(self, policy_config: Dict[str, Any]):
        # Pulls the strict document requirements straight from the policy file
        self.requirements = policy_config.get("document_requirements", {})

    def evaluate(self, submission: ClaimSubmission, ledger: TraceLedger) -> Tuple[bool, dict]:
        start_time = time.time()
        category_reqs = self.requirements.get(submission.category.upper())
        
        if not category_reqs:
            ledger.append(TraceEvent(
                seq=len(ledger.events) + 1,
                component="DOC_GATE",
                outcome="FAIL",
                effect="BLOCK",
                evidence={"category": submission.category, "issue": "Unknown category"},
                duration_ms=int((time.time() - start_time) * 1000)
            ))
            return False, {
                "decision": "NEEDS_MEMBER_ACTION",
                "message": f"We do not recognize the claim category '{submission.category}'."
            }

        required_types = set(category_reqs.get("required", []))
        uploaded_types = {doc.file_type.upper() for doc in submission.documents}
        
        missing_docs = required_types - uploaded_types
        
        if missing_docs:
            uploaded_names = [doc.file_id for doc in submission.documents]
            missing_str = " and ".join(missing_docs).replace("_", " ").lower()
            uploaded_str = ", ".join(uploaded_names)
            
            message = (
                f"You uploaded {len(submission.documents)} document(s) ({uploaded_str}). "
                f"A {submission.category.lower()} claim needs a {missing_str}. "
                f"Please upload the missing document(s) to proceed."
            )
            
            ledger.append(TraceEvent(
                seq=len(ledger.events) + 1,
                component="DOC_GATE",
                rule_id=f"doc_req_{submission.category}",
                outcome="FAIL",
                effect="BLOCK",
                policy_ref=f"document_requirements.{submission.category.upper()}.required",
                evidence={"missing": list(missing_docs), "uploaded": list(uploaded_types)},
                duration_ms=int((time.time() - start_time) * 1000)
            ))
            
            return False, {
                "decision": "NEEDS_MEMBER_ACTION",
                "message": message
            }
            
        ledger.append(TraceEvent(
            seq=len(ledger.events) + 1,
            component="DOC_GATE",
            rule_id=f"doc_req_{submission.category}",
            outcome="PASS",
            effect="NONE",
            evidence={"uploaded": list(uploaded_types)},
            duration_ms=int((time.time() - start_time) * 1000)
        ))
        
        return True, {}