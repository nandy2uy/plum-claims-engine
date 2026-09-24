from typing import List, Tuple
from src.models.medical import DocumentExtraction
from src.models.trace import TraceLedger, TraceEvent
import time
import re

class ConsistencyAgent:
    def evaluate(self, extractions: List[DocumentExtraction], ledger: TraceLedger) -> Tuple[bool, str]:
        start_time = time.time()
        doc_names = {}
        for ext in extractions:
            if ext.prescription_data and ext.prescription_data.patient_name:
                doc_names[ext.file_type] = ext.prescription_data.patient_name
            elif ext.bill_data and ext.bill_data.patient_name:
                doc_names[ext.file_type] = ext.bill_data.patient_name

        if len(doc_names) > 1:
            def normalize(name: str) -> set:
                clean = re.sub(r'\b(mr|mrs|ms|dr|shri|smt)\.?\b', '', name.lower())
                return set(clean.split())

            reference_type, reference_name = list(doc_names.items())[0]
            ref_tokens = normalize(reference_name)

            for doc_type, name in doc_names.items():
                if not ref_tokens.intersection(normalize(name)):
                    msg = [f"{dtype.lower()}: {n}" for dtype, n in doc_names.items()]
                    message = f"The patient names on your documents do not match ({'; '.join(msg)}). Please verify your uploads."
                    
                    ledger.append(TraceEvent(
                        seq=len(ledger.events)+1, component="CONSISTENCY", outcome="FAIL",
                        effect="BLOCK", evidence={"mismatch": doc_names}, duration_ms=int((time.time() - start_time) * 1000)
                    ))
                    return False, message

        ledger.append(TraceEvent(
            seq=len(ledger.events)+1, component="CONSISTENCY", outcome="PASS", effect="NONE",
            duration_ms=int((time.time() - start_time) * 1000)
        ))
        return True, ""