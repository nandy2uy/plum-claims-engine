"""Extraction agent: every uploaded document -> DocumentExtraction.

Component contract
-------------------
Input:  ClaimSubmission, TraceLedger.
Output: List[DocumentExtraction], exactly one per submitted document, in
        submission order. It never drops a document and never raises per document.
        Each entry carries:
          detected_type   what the document actually is (model or fixture)
          readability     GOOD / POOR / UNREADABLE
          confidence      computed for live extractions (see below), or the fixture's value
          failure_kind    None | MEMBER (unreadable, unsupported, empty, no usable content)
                               | SYSTEM (LLM unavailable, bad output)
          flags           e.g. UNREADABLE, EXTRACTION_FAILED, MISSING_CRITICAL_FIELDS, DOCUMENT_ALTERATION
Errors: none per document. A per-document SYSTEM failure is logged as an ERROR
        event (the claim is degraded) and a MEMBER failure as a FAIL event, so
        the member is only asked to act on problems they can fix.

Documents are processed CONCURRENTLY (asyncio.gather, bounded by
settings.llm_max_concurrency). Each task writes to a forked ledger that is merged
back in document order, so traces stay deterministic.

Two sources:
  fixture: DocumentSource.pre_extracted_data (eval harness; no network). Only honoured when
           allow_fixtures=True (settings.allow_fixture_documents). Otherwise the document is
           refused: a caller must not be able to skip extraction and submit invented data.
           Keys: detected_type, quality, confidence, flags, unreadable_fields,
                 prescription_data | bill_data | report_data
  live:    content_base64 / content_url -> DocumentExtractorBackend (vision model: Gemini or OpenAI).
           After a successful call:
             - minimum evidence: a readable document with none of
               interpretation.extraction_minimum_evidence[type] (e.g. a prescription with no
               patient name, diagnosis or treatment) is returned to the member as unusable;
             - confidence = w x model-reported + (1 - w) x completeness, where completeness is
               the share of interpretation.extraction_critical_fields[type] present and
               w = confidence.extraction_model_weight. Both inputs are in the trace;
             - model name, prompt version, schema mode, latency, tokens and attempts are in the trace.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

from pydantic import ValidationError

from src.core.llm import DocumentExtractorBackend, ExtractionError
from src.core.text import humanize
from src.models.claim import ClaimSubmission, DocumentSource
from src.models.medical import (
    BILL_TYPES, FLAG_EXTRACTION_FAILED, FLAG_MISSING_CRITICAL_FIELDS, FLAG_NO_CONTENT, FLAG_UNREADABLE,
    FLAG_UNSUPPORTED_FILE, KNOWN_DOCUMENT_TYPES, PRESCRIPTION_TYPES, REPORT_TYPES, DocumentExtraction,
    ExtractedBill, ExtractedPrescription, ExtractedReport,
)
from src.models.policy import Interpretation
from src.models.trace import Stopwatch, TraceLedger

COMPONENT = "EXTRACTOR"
FLAG_FIXTURE_NOT_ALLOWED = "FIXTURE_NOT_ALLOWED"


class ExtractionAgent:
    def __init__(self, interpretation: Interpretation, backend: Optional[DocumentExtractorBackend],
                 max_concurrency: int = 4, allow_fixtures: bool = False):
        self.interp = interpretation
        self.weights = interpretation.confidence
        self.backend = backend
        self.max_concurrency = max(1, max_concurrency)
        self.allow_fixtures = allow_fixtures

    async def process(self, submission: ClaimSubmission, ledger: TraceLedger) -> List[DocumentExtraction]:
        semaphore = asyncio.Semaphore(self.max_concurrency)
        children = [ledger.fork() for _ in submission.documents]
        results = await asyncio.gather(
            *(self._guarded(doc, child, semaphore) for doc, child in zip(submission.documents, children))
        )
        for child in children:
            ledger.merge(child)
        return list(results)

    async def _guarded(self, doc: DocumentSource, ledger: TraceLedger, semaphore: asyncio.Semaphore) -> DocumentExtraction:
        watch = Stopwatch()
        try:
            async with semaphore:
                return await self._extract_one(doc, ledger, watch)
        except Exception as exc:  # noqa: BLE001 - one bad document must never sink the batch
            return self._system_failure(doc, ledger, watch, f"Unexpected {type(exc).__name__}: {exc}")

    async def _extract_one(self, doc: DocumentSource, ledger: TraceLedger, watch: Stopwatch) -> DocumentExtraction:
        if doc.pre_extracted_data is not None:
            if not self.allow_fixtures:
                return self._fixture_refused(doc, ledger, watch)
            return self._from_fixture(doc, ledger, watch)
        if doc.content_base64 or doc.content_url:
            return await self._from_llm(doc, ledger, watch)
        extraction = self._base(doc, source="none")
        extraction.failure_kind, extraction.flags = "MEMBER", [FLAG_NO_CONTENT]
        extraction.error = "No file content was received for this document."
        ledger.log(COMPONENT, "FAIL", rule_id="document_content", summary=f"'{doc.label}': no file content received.",
                   evidence={"file_id": doc.file_id}, duration_ms=watch.ms())
        return extraction

    # ------------------------------------------------------------ fixture path

    def _fixture_refused(self, doc: DocumentSource, ledger: TraceLedger, watch: Stopwatch) -> DocumentExtraction:
        extraction = self._base(doc, source="none")
        extraction.failure_kind, extraction.flags = "MEMBER", [FLAG_FIXTURE_NOT_ALLOWED]
        extraction.error = "pre-extracted data is not accepted here; please upload the document image or PDF"
        ledger.log(COMPONENT, "FAIL", rule_id="fixture_documents", interpretation_ref="assumptions.fixture_documents",
                   summary=f"'{doc.label}' carried pre-extracted data, which this environment does not accept "
                           f"(ALLOW_FIXTURE_DOCUMENTS=false); the document was not used.",
                   evidence={"file_id": doc.file_id}, duration_ms=watch.ms())
        return extraction

    def _from_fixture(self, doc: DocumentSource, ledger: TraceLedger, watch: Stopwatch) -> DocumentExtraction:
        data = doc.pre_extracted_data or {}
        extraction = self._base(doc, source="fixture")
        detected = str(data.get("detected_type") or doc.file_type).upper()
        extraction.detected_type = detected if detected in KNOWN_DOCUMENT_TYPES else "OTHER"
        extraction.flags = list(data.get("flags", []))
        if str(data.get("quality", "GOOD")).upper() == "UNREADABLE":
            extraction.readability, extraction.failure_kind = "UNREADABLE", "MEMBER"
            extraction.flags.append(FLAG_UNREADABLE)
            extraction.error = "The image is too blurry or unclear to read."
            ledger.log(COMPONENT, "FAIL", rule_id="readability",
                       summary=f"'{doc.label}' ({humanize(extraction.detected_type)}) is unreadable.",
                       evidence={"file_id": doc.file_id, "source": "fixture", "readability": "UNREADABLE"},
                       duration_ms=watch.ms())
            return extraction
        try:
            kind = extraction.effective_type
            if kind in PRESCRIPTION_TYPES:
                extraction.prescription_data = ExtractedPrescription.model_validate(data.get("prescription_data", {}))
            elif kind in BILL_TYPES:
                extraction.bill_data = ExtractedBill.model_validate(data.get("bill_data", {}))
            elif kind in REPORT_TYPES:
                extraction.report_data = ExtractedReport.model_validate(data.get("report_data", {}))
        except ValidationError as exc:
            return self._system_failure(doc, ledger, watch, f"Fixture data failed schema validation: {exc}")
        extraction.confidence_score = float(data.get("confidence", self.weights.fixture_extraction))
        if str(data.get("quality", "GOOD")).upper() == "POOR":
            extraction.readability = "POOR"
        extraction.unreadable_fields = list(data.get("unreadable_fields", []))
        ledger.log(COMPONENT, "PASS", rule_id="extraction",
                   summary=f"'{doc.label}' read as {humanize(extraction.effective_type)} (fixture data).",
                   evidence={"file_id": doc.file_id, "source": "fixture", "detected_type": extraction.detected_type,
                             "confidence": extraction.confidence_score, "fields": self._field_summary(extraction)},
                   duration_ms=watch.ms())
        self._log_partial_readability(extraction, ledger)
        return extraction

    # ------------------------------------------------------------ live path

    async def _from_llm(self, doc: DocumentSource, ledger: TraceLedger, watch: Stopwatch) -> DocumentExtraction:
        extraction = self._base(doc, source="llm")
        if self.backend is None:
            return self._system_failure(doc, ledger, watch, "No live extraction backend is configured.")
        try:
            result = await self.backend.extract(doc)
        except ExtractionError as exc:
            if exc.kind == "INPUT":
                extraction.failure_kind, extraction.flags, extraction.error = "MEMBER", [FLAG_UNSUPPORTED_FILE], str(exc)
                ledger.log(COMPONENT, "FAIL", rule_id="file_input", summary=f"'{doc.label}' cannot be processed: {exc}",
                           evidence={"file_id": doc.file_id, "source": "llm"}, duration_ms=watch.ms())
                return extraction
            return self._system_failure(doc, ledger, watch, str(exc))

        extraction.detected_type = result.detected_document_type
        extraction.readability = result.readability
        extraction.unreadable_fields = result.unreadable_fields
        extraction.flags = list(result.quality_flags)
        extraction.prescription_data, extraction.bill_data, extraction.report_data = result.prescription, result.bill, result.report
        model_meta = result.meta.model_dump()

        if result.readability == "UNREADABLE":
            extraction.failure_kind, extraction.error = "MEMBER", "The document could not be read."
            extraction.flags.append(FLAG_UNREADABLE)
            ledger.log(COMPONENT, "FAIL", rule_id="readability", summary=f"'{doc.label}' is unreadable per the vision model.",
                       evidence={"file_id": doc.file_id, "source": "llm", "unreadable_fields": result.unreadable_fields,
                                 "model_call": model_meta},
                       duration_ms=watch.ms())
            return extraction

        missing_evidence = self._missing_minimum_evidence(extraction)
        if missing_evidence:
            doc_type = humanize(extraction.effective_type)
            extraction.failure_kind = "MEMBER"
            extraction.error = (f"we could read the {doc_type} but could not find its "
                                f"{' or '.join(f.replace('_', ' ') for f in missing_evidence)}")
            extraction.flags.append(FLAG_MISSING_CRITICAL_FIELDS)
            ledger.log(COMPONENT, "FAIL", rule_id="minimum_evidence",
                       interpretation_ref=f"extraction_minimum_evidence.{extraction.effective_type}",
                       summary=f"'{doc.label}' was read as a {doc_type} but none of its key fields "
                               f"({', '.join(missing_evidence)}) could be extracted, so it cannot be used for a decision.",
                       evidence={"file_id": doc.file_id, "unreadable_fields": result.unreadable_fields,
                                 "fields": self._field_summary(extraction), "model_call": model_meta},
                       duration_ms=watch.ms())
            return extraction

        score, completeness, missing_fields = self._computed_confidence(extraction, result.confidence)
        extraction.confidence_score = score
        ledger.log(COMPONENT, "PASS" if result.readability == "GOOD" else "WARN", rule_id="extraction",
                   interpretation_ref="assumptions.extraction_confidence",
                   summary=f"'{doc.label}' read by the vision model as {humanize(extraction.effective_type)} "
                           f"(readability {result.readability}, confidence {score}: model-reported "
                           f"{round(result.confidence, 3)}, field completeness {completeness})."
                           + (f" Missing: {', '.join(missing_fields)}." if missing_fields else ""),
                   evidence={"file_id": doc.file_id, "source": "llm", "detected_type": extraction.detected_type,
                             "readability": result.readability, "unreadable_fields": result.unreadable_fields,
                             "quality_flags": result.quality_flags, "fields": self._field_summary(extraction),
                             "confidence": {"computed": score, "model_reported": round(result.confidence, 3),
                                            "completeness": completeness, "missing_critical_fields": missing_fields,
                                            "model_weight": self.weights.extraction_model_weight},
                             "model_call": model_meta},
                   duration_ms=watch.ms())
        self._log_partial_readability(extraction, ledger)
        return extraction

    # ------------------------------------------------------------ evidence and confidence

    @staticmethod
    def _part(extraction: DocumentExtraction):
        return extraction.prescription_data or extraction.bill_data or extraction.report_data

    @classmethod
    def _present(cls, extraction: DocumentExtraction, field: str) -> bool:
        part = cls._part(extraction)
        if part is None:
            return False
        value = getattr(part, field, None)
        return value not in (None, "", [], {})

    def _missing_minimum_evidence(self, extraction: DocumentExtraction) -> List[str]:
        """[] if the document has at least one of its minimum-evidence fields, else the list of them."""
        required = self.interp.extraction_minimum_evidence.get(extraction.effective_type, [])
        if not required:
            return []
        if any(self._present(extraction, f) for f in required):
            return []
        return list(required)

    def _computed_confidence(self, extraction: DocumentExtraction, model_reported: float) -> Tuple[float, float, List[str]]:
        critical = self.interp.extraction_critical_fields.get(extraction.effective_type, [])
        missing = [f for f in critical if not self._present(extraction, f)]
        completeness = round(1 - len(missing) / len(critical), 3) if critical else 1.0
        w = self.weights.extraction_model_weight
        score = round(max(0.0, min(1.0, w * model_reported + (1 - w) * completeness)), 3)
        return score, completeness, missing

    def _log_partial_readability(self, extraction: DocumentExtraction, ledger: TraceLedger) -> None:
        """Requirement 5: a confidence drop caused by a partly unreadable document must be visible.
        One WARN event per affected document, carrying the confidence factor."""
        dropped = extraction.bill_data.dropped_line_items if extraction.bill_data else 0
        if extraction.readability != "POOR" and not extraction.unreadable_fields and not dropped:
            return
        parts = []
        if extraction.readability == "POOR":
            parts.append("readability is POOR")
        if extraction.unreadable_fields:
            parts.append(f"illegible field(s): {', '.join(extraction.unreadable_fields)}")
        if dropped:
            parts.append(f"{dropped} bill line(s) listed without a readable amount were ignored")
        ledger.log(COMPONENT, "WARN", rule_id="partial_readability", interpretation_ref="confidence.partially_unreadable",
                   summary=f"'{extraction.label}' was only partly readable ({'; '.join(parts)}); extracted fields are used "
                           f"with reduced confidence.",
                   evidence={"file_id": extraction.file_id, "readability": extraction.readability,
                             "unreadable_fields": extraction.unreadable_fields, "dropped_line_items": dropped},
                   confidence_factor=self.weights.partially_unreadable, confidence_scope="ALL")

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _base(doc: DocumentSource, source: str) -> DocumentExtraction:
        return DocumentExtraction(file_id=doc.file_id, file_name=doc.file_name, declared_type=doc.file_type, source=source)

    def _system_failure(self, doc: DocumentSource, ledger: TraceLedger, watch: Stopwatch, error: str) -> DocumentExtraction:
        extraction = DocumentExtraction(file_id=doc.file_id, file_name=doc.file_name, declared_type=doc.file_type,
                                        failure_kind="SYSTEM", flags=[FLAG_EXTRACTION_FAILED], error=error)
        ledger.log(COMPONENT, "ERROR", effect="DEGRADE", rule_id="extraction",
                   summary=f"Could not extract '{doc.label}' due to a system error (not the member's fault): {error}",
                   evidence={"file_id": doc.file_id, "error": error}, error_details=error, duration_ms=watch.ms(),
                   confidence_factor=self.weights.degraded_component, confidence_scope="ALL")
        return extraction

    @classmethod
    def _field_summary(cls, extraction: DocumentExtraction) -> dict:
        part = cls._part(extraction)
        if part is None:
            return {}
        return {k: v for k, v in part.model_dump(mode="json").items() if v not in (None, [], "", 0)}
