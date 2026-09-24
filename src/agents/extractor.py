"""Document data extraction — turns each uploaded document into structured
prescription/bill data.

Component contract
-------------------
Input:  ClaimSubmission, TraceLedger.
Output: List[DocumentExtraction], always exactly one entry per submitted
        document, in submission order. A document that couldn't be read
        (blurry image, LLM failure, missing content) is represented by an
        entry with an empty/partial payload and a `flags` entry
        ("UNREADABLE" | "EXTRACTION_FAILED" | "NO_CONTENT_PROVIDED") — it is
        NOT dropped from the list and does NOT abort the batch. Downstream
        (the orchestrator) decides what a flagged required document means for
        the final decision; this agent's only job is best-effort extraction.
Errors: this method itself is wrapped in @isolate_fault at the batch level as
        a last-resort safety net (e.g. an unexpected bug), in which case it
        returns an empty list and the orchestrator treats every document as
        unextracted. Per-document failures are caught internally and turned
        into flags rather than exceptions, precisely so one bad document
        can't take the whole batch down (this replaces the previous design's
        crash-prone True/False + partial-list contract).
"""

import time
from typing import List
from src.models.claim import ClaimSubmission
from src.models.medical import DocumentExtraction, ExtractedPrescription, ExtractedBill
from src.models.trace import TraceLedger
from src.core.fault_tolerance import isolate_fault
from src.core.llm import extract_prescription_via_llm, extract_bill_via_llm, build_data_url, ExtractionError

PRESCRIPTION_TYPES = {"PRESCRIPTION"}
BILL_TYPES = {"HOSPITAL_BILL", "PHARMACY_BILL"}


class ExtractionAgent:

    @isolate_fault(component_name="EXTRACTOR", fallback_return=[])
    async def process(self, submission: ClaimSubmission, ledger: TraceLedger) -> List[DocumentExtraction]:
        extractions: List[DocumentExtraction] = []

        for doc in submission.documents:
            start_time = time.time()
            extraction = DocumentExtraction(file_id=doc.file_id, file_type=doc.file_type, confidence_score=0.0)

            if doc.pre_extracted_data and doc.pre_extracted_data.get("quality") == "UNREADABLE":
                extraction.flags.append("UNREADABLE")
                ledger.log(
                    component="EXTRACTOR", outcome="FAIL", effect="NONE",
                    evidence={"file_id": doc.file_id, "issue": "Document image is too blurry/unreadable"},
                    duration_ms=int((time.time() - start_time) * 1000),
                )
                extractions.append(extraction)
                continue

            if doc.pre_extracted_data:
                self._apply_fixture_data(extraction, doc)
                extraction.confidence_score = 0.95
                ledger.log(
                    component="EXTRACTOR", outcome="PASS", effect="NONE",
                    evidence={"file_id": doc.file_id, "source": "fixture"},
                    duration_ms=int((time.time() - start_time) * 1000),
                )

            elif doc.content_base64 or doc.content_url:
                image_url = doc.content_url or build_data_url(doc.content_base64, doc.mime_type or "image/jpeg")
                try:
                    if doc.file_type in PRESCRIPTION_TYPES:
                        extraction.prescription_data = await extract_prescription_via_llm(image_url)
                    elif doc.file_type in BILL_TYPES:
                        extraction.bill_data = await extract_bill_via_llm(image_url)
                    extraction.confidence_score = 0.85
                    ledger.log(
                        component="EXTRACTOR", outcome="PASS", effect="NONE",
                        evidence={"file_id": doc.file_id, "source": "live_llm"},
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
                except ExtractionError as e:
                    extraction.flags.append("EXTRACTION_FAILED")
                    ledger.log(
                        component="EXTRACTOR", outcome="FAIL", effect="NONE",
                        evidence={"file_id": doc.file_id, "issue": str(e)},
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
            else:
                extraction.flags.append("NO_CONTENT_PROVIDED")
                ledger.log(
                    component="EXTRACTOR", outcome="FAIL", effect="NONE",
                    evidence={"file_id": doc.file_id, "issue": "No content_url, content_base64, or pre_extracted_data supplied"},
                    duration_ms=int((time.time() - start_time) * 1000),
                )

            extractions.append(extraction)

        return extractions

    @staticmethod
    def _apply_fixture_data(extraction: DocumentExtraction, doc) -> None:
        if doc.file_type in PRESCRIPTION_TYPES:
            extraction.prescription_data = ExtractedPrescription(**doc.pre_extracted_data.get("prescription_data", {}))
        elif doc.file_type in BILL_TYPES:
            extraction.bill_data = ExtractedBill(**doc.pre_extracted_data.get("bill_data", {}))
        extraction.flags = list(doc.pre_extracted_data.get("flags", []))
