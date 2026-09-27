import asyncio

import pytest

from src.agents.extractor import ExtractionAgent
from src.core.llm import ExtractionError, LLMDocumentResult
from src.models.claim import ClaimSubmission
from tests.conftest import bill, claim, line, rx


def _sub(documents):
    return ClaimSubmission.model_validate(claim(documents=documents))


def _live(file_id, file_type="HOSPITAL_BILL"):
    return {"file_id": file_id, "file_name": f"{file_id}.jpg", "file_type": file_type,
            "content_base64": "aGVsbG8=", "mime_type": "image/jpeg"}


class FakeBackend:
    def __init__(self, results, delay=0.0):
        self.results, self.delay, self.active, self.peak = results, delay, 0, 0

    async def extract(self, doc):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        outcome = self.results[doc.file_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_fixture_extraction_and_unreadable(interp, ledger):
    docs = [rx("F1", patient_name="A B"), {**bill("F2"), "pre_extracted_data": {"quality": "UNREADABLE"}}]
    out = await ExtractionAgent(interp, None, allow_fixtures=True).process(_sub(docs), ledger)
    assert [e.file_id for e in out] == ["F1", "F2"]
    assert out[0].usable and out[0].prescription_data.patient_name == "A B"
    assert out[1].failure_kind == "MEMBER" and "UNREADABLE" in out[1].flags


async def test_poor_readability_adds_visible_confidence_factor(interp, ledger):
    doc = {**rx("F1"), "pre_extracted_data": {"quality": "POOR", "unreadable_fields": ["registration_number"],
                                              "prescription_data": {"diagnoses": ["Viral Fever"]}}}
    await ExtractionAgent(interp, None, allow_fixtures=True).process(_sub([doc, bill("F2", line_items=[line("x", 100)])]), ledger)
    warn = [e for e in ledger.events if e.rule_id == "partial_readability"]
    assert len(warn) == 1 and warn[0].confidence_factor == interp.confidence.partially_unreadable
    assert "registration_number" in warn[0].summary


async def test_line_items_without_amount_are_dropped_not_fatal(interp, ledger):
    doc = bill("F2", line_items=[line("Consultation", "₹1,000"), {"description": "Smudged", "amount": None}])
    out = await ExtractionAgent(interp, None, allow_fixtures=True).process(_sub([rx("F1"), doc]), ledger)
    assert out[1].usable and len(out[1].bill_data.line_items) == 1 and out[1].bill_data.dropped_line_items == 1


async def test_live_path_classifies_member_vs_system_faults(interp, ledger):
    good = LLMDocumentResult(detected_document_type="PRESCRIPTION", confidence=0.9,
                             prescription={"patient_name": "Priya Singh", "diagnosis": "Viral Fever"})
    backend = FakeBackend({
        "a": good,
        "b": ExtractionError("unsupported file", kind="INPUT"),
        "c": ExtractionError("model timeout", kind="SYSTEM"),
    })
    out = await ExtractionAgent(interp, backend).process(_sub([_live("a", "PRESCRIPTION"), _live("b"), _live("c")]), ledger)
    assert out[0].usable and out[0].detected_type == "PRESCRIPTION"
    assert out[1].failure_kind == "MEMBER"
    assert out[2].failure_kind == "SYSTEM"
    error = [e for e in ledger.events if e.outcome == "ERROR"]
    assert error and "not the member's fault" in error[0].summary


async def test_live_extraction_runs_concurrently(interp, ledger):
    result = LLMDocumentResult(detected_document_type="HOSPITAL_BILL", bill={"total": 100})
    backend = FakeBackend({f"d{i}": result for i in range(4)}, delay=0.05)
    agent = ExtractionAgent(interp, backend, max_concurrency=4)
    await agent.process(_sub([_live(f"d{i}") for i in range(4)]), ledger)
    assert backend.peak == 4


async def test_bill_with_no_readable_amounts_is_member_actionable(interp, ledger):
    backend = FakeBackend({"b": LLMDocumentResult(detected_document_type="HOSPITAL_BILL", bill={"hospital_name": "X"})})
    out = await ExtractionAgent(interp, backend).process(_sub([_live("b")]), ledger)
    assert out[0].failure_kind == "MEMBER" and "MISSING_CRITICAL_FIELDS" in out[0].flags


async def test_no_backend_is_system_failure(interp, ledger):
    out = await ExtractionAgent(interp, None, allow_fixtures=True).process(_sub([_live("x")]), ledger)
    assert out[0].failure_kind == "SYSTEM"


async def test_fixture_documents_are_refused_unless_enabled(interp, ledger):
    out = await ExtractionAgent(interp, None).process(_sub([rx("F1", diagnoses=["Viral Fever"])]), ledger)
    assert out[0].failure_kind == "MEMBER" and "FIXTURE_NOT_ALLOWED" in out[0].flags
    assert out[0].prescription_data is None


async def test_readable_but_empty_prescription_is_not_usable(interp, ledger):
    empty = LLMDocumentResult(detected_document_type="PRESCRIPTION", confidence=0.9,
                              prescription={"doctor_name": "Dr. X", "medicines": ["Paracetamol"]})
    out = await ExtractionAgent(interp, FakeBackend({"a": empty})).process(_sub([_live("a", "PRESCRIPTION")]), ledger)
    assert out[0].failure_kind == "MEMBER" and "MISSING_CRITICAL_FIELDS" in out[0].flags
    assert "patient name" in out[0].error
    assert [e for e in ledger.events if e.rule_id == "minimum_evidence"][0].outcome == "FAIL"


async def test_live_confidence_is_computed_from_completeness_not_trusted(interp, ledger):
    complete = LLMDocumentResult(detected_document_type="PRESCRIPTION", confidence=0.9, prescription={
        "patient_name": "Priya Singh", "doctor_name": "Dr. A", "registration_number": "KA/45678/2015",
        "date": "2024-11-01", "diagnoses": ["Viral Fever"]})
    sparse = LLMDocumentResult(detected_document_type="PRESCRIPTION", confidence=0.9, prescription={
        "patient_name": "Priya Singh", "diagnoses": ["Viral Fever"]})
    agent = ExtractionAgent(interp, FakeBackend({"a": complete, "b": sparse}))
    out = await agent.process(_sub([_live("a", "PRESCRIPTION"), _live("b", "PRESCRIPTION")]), ledger)
    w = interp.confidence.extraction_model_weight
    assert out[0].confidence_score == round(w * 0.9 + (1 - w) * 1.0, 3)
    assert out[1].confidence_score == round(w * 0.9 + (1 - w) * 0.4, 3)   # 2 of 5 critical fields
    event = [e for e in ledger.events if e.rule_id == "extraction" and e.evidence["file_id"] == "b"][0]
    assert event.evidence["confidence"]["missing_critical_fields"] == ["doctor_name", "registration_number", "date"]


async def test_model_call_metadata_is_in_the_trace(interp, ledger):
    result = LLMDocumentResult(detected_document_type="HOSPITAL_BILL", bill={"total": 100, "patient_name": "A"},
                               meta={"model": "gpt-x", "prompt_version": "extract-v3", "latency_ms": 812,
                                     "input_tokens": 1500, "output_tokens": 210, "attempts": 1})
    await ExtractionAgent(interp, FakeBackend({"b": result})).process(_sub([_live("b")]), ledger)
    call = [e for e in ledger.events if e.rule_id == "extraction"][0].evidence["model_call"]
    assert call["model"] == "gpt-x" and call["prompt_version"] == "extract-v3" and call["input_tokens"] == 1500
