import pytest
from decimal import Decimal
from src.agents.extractor import ExtractionAgent
from src.models.claim import ClaimSubmission, DocumentSource


def _submission(documents):
    return ClaimSubmission(
        claim_id="T1", member_id="EMP001", patient_id="EMP001", category="CONSULTATION",
        treatment_date="2024-11-01", claimed_amount=Decimal(1000), documents=documents,
    )


@pytest.mark.asyncio
async def test_fixture_extraction_never_touches_the_network(ledger):
    submission = _submission([
        DocumentSource(
            file_id="F1", file_type="PRESCRIPTION",
            pre_extracted_data={"prescription_data": {"patient_name": "Rajesh Kumar", "diagnoses": ["Viral Fever"]}},
        )
    ])

    extractions = await ExtractionAgent().process(submission, ledger=ledger)

    assert len(extractions) == 1
    assert extractions[0].prescription_data.patient_name == "Rajesh Kumar"
    assert extractions[0].confidence_score == 0.95
    assert extractions[0].flags == []


@pytest.mark.asyncio
async def test_unreadable_document_is_flagged_not_dropped(ledger):
    submission = _submission([
        DocumentSource(file_id="F1", file_type="PHARMACY_BILL", pre_extracted_data={"quality": "UNREADABLE"})
    ])

    extractions = await ExtractionAgent().process(submission, ledger=ledger)

    # Must still return exactly one entry (this is the fix for the crash bug:
    # the previous version returned an empty list here, which the caller then
    # indexed into and crashed with IndexError).
    assert len(extractions) == 1
    assert "UNREADABLE" in extractions[0].flags


@pytest.mark.asyncio
async def test_no_content_provided_is_flagged_not_dropped(ledger):
    submission = _submission([DocumentSource(file_id="F1", file_type="PRESCRIPTION")])

    extractions = await ExtractionAgent().process(submission, ledger=ledger)

    assert len(extractions) == 1
    assert "NO_CONTENT_PROVIDED" in extractions[0].flags


@pytest.mark.asyncio
async def test_live_extraction_without_api_key_flags_instead_of_crashing(ledger, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    from src.core.config import get_settings
    get_settings.cache_clear()

    submission = _submission([
        DocumentSource(file_id="F1", file_type="PRESCRIPTION", content_url="https://example.com/rx.jpg")
    ])

    extractions = await ExtractionAgent().process(submission, ledger=ledger)

    assert len(extractions) == 1
    assert "EXTRACTION_FAILED" in extractions[0].flags
    assert any(e.outcome == "FAIL" and e.component == "EXTRACTOR" for e in ledger.events)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_one_bad_document_does_not_take_down_the_batch(ledger):
    submission = _submission([
        DocumentSource(file_id="F1", file_type="PRESCRIPTION", pre_extracted_data={"quality": "UNREADABLE"}),
        DocumentSource(file_id="F2", file_type="HOSPITAL_BILL", pre_extracted_data={"bill_data": {"total_amount": 500}}),
    ])

    extractions = await ExtractionAgent().process(submission, ledger=ledger)

    assert len(extractions) == 2
    assert "UNREADABLE" in extractions[0].flags
    assert extractions[1].bill_data.total_amount == 500
