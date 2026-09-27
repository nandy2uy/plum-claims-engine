"""VisionExtractor without the network: `_complete(messages, response_format)` is the seam."""

import base64
import io
import json

import pytest

from src.core.config import Settings
from src.core.llm import (
    PROMPT_VERSION, STRICT_SCHEMA, Completion, ExtractionError, VisionExtractor, prepare_images,
)
from src.models.claim import DocumentSource

PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")).decode()


def _doc(**kw):
    return DocumentSource(file_id="F1", file_name="bill.png", file_type="HOSPITAL_BILL", **kw)


IMG = dict(content_base64=PNG_1PX, mime_type="image/png")


def _pdf_b64(pages=2):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(200, 300)
    buf = io.BytesIO()
    pdf.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


class Scripted(VisionExtractor):
    """Replays scripted replies; records the response_format of every call."""

    def __init__(self, replies, **kw):
        super().__init__(Settings(gemini_api_key="test-key", llm_max_retries=2, llm_model="test-model"), **kw)
        self.replies, self.formats = list(replies), []

    async def _complete(self, messages, response_format):
        self.formats.append(response_format["type"] if response_format else "none")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


GOOD = json.dumps({"detected_document_type": "hospital bill", "readability": "GOOD", "confidence": 0.92,
                   "bill": {"hospital_name": "City Clinic", "total": "₹1,500", "line_items": [{"description": "Consult", "amount": 1500}]}})


class APITimeoutError(Exception):
    pass


class AuthenticationError(Exception):
    pass


class BadRequestError(Exception):
    pass


async def _no_sleep(*_):
    return None


# ---------------------------------------------------------------- inputs

def test_images_pass_through_and_pdfs_are_rendered_per_page():
    assert prepare_images(_doc(**IMG), 10**7, 5)[0].startswith("data:image/png")
    pages = prepare_images(_doc(content_base64=_pdf_b64(3), mime_type="application/pdf"), 10**7, 2)
    assert len(pages) == 2 and all(p.startswith("data:image/png;base64,") for p in pages)


@pytest.mark.parametrize("doc", [
    _doc(content_base64="!!notbase64!!", mime_type="image/png"),
    _doc(content_base64=base64.b64encode(b"hello").decode(), mime_type="text/plain"),
    _doc(content_base64=base64.b64encode(b"%PDF-1.4 broken").decode(), mime_type="application/pdf"),
    _doc(content_url="ftp://x/y.png"),
])
def test_bad_member_files_are_input_errors(doc):
    with pytest.raises(ExtractionError) as err:
        prepare_images(doc, 10**7, 5)
    assert err.value.kind == "INPUT"


def test_oversized_file_is_input_error():
    with pytest.raises(ExtractionError) as err:
        prepare_images(_doc(**IMG), 10, 5)
    assert err.value.kind == "INPUT"


# ---------------------------------------------------------------- structured output

def test_strict_schema_is_strict_everywhere():
    """Strict mode requires every object to list all its properties as required and forbid extras."""
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(STRICT_SCHEMA)


async def test_valid_output_is_parsed_with_strict_schema_and_metadata():
    ext = Scripted([Completion(GOOD, input_tokens=1200, output_tokens=180)])
    result = await ext.extract(_doc(**IMG))
    assert result.detected_document_type == "HOSPITAL_BILL" and str(result.bill.total_amount) == "1500"
    assert ext.formats == ["json_schema"]
    meta = result.meta
    assert (meta.provider, meta.model, meta.prompt_version, meta.schema_mode) == (
        "gemini", "test-model", PROMPT_VERSION, "json_schema_strict")
    assert (meta.input_tokens, meta.output_tokens, meta.attempts, meta.pages, meta.repaired) == (1200, 180, 1, 1, False)


async def test_rejected_strict_mode_steps_down_and_is_remembered():
    ext = Scripted([BadRequestError("Invalid schema for response_format"), GOOD, GOOD])
    first = await ext.extract(_doc(**IMG))
    second = await ext.extract(_doc(**IMG))
    assert ext.formats == ["json_schema", "json_object", "json_object"]  # no strict retry after the step-down
    assert first.meta.schema_mode == second.meta.schema_mode == "json_object"


async def test_steps_down_to_prompt_only_json_when_no_response_format_is_accepted():
    ext = Scripted([BadRequestError("json_schema unsupported"), BadRequestError("json_object unsupported"), GOOD])
    result = await ext.extract(_doc(**IMG))
    assert ext.formats == ["json_schema", "json_object", "none"] and result.meta.schema_mode == "prompt_only"


async def test_a_request_rejected_in_every_mode_is_a_system_error():
    ext = Scripted([BadRequestError("image too large")] * 3)
    with pytest.raises(ExtractionError) as err:
        await ext.extract(_doc(**IMG))
    assert err.value.kind == "SYSTEM" and ext.mode_index == 0  # a bad request must not downgrade later calls


def test_gemini_is_the_default_provider_via_its_openai_compatible_endpoint():
    from src.core.config import llm_base_url, llm_key_env
    s = Settings(gemini_api_key="k")
    assert s.llm_provider == "gemini" and llm_key_env(s) == "GEMINI_API_KEY"
    assert llm_base_url(s) == "https://generativelanguage.googleapis.com/v1beta/openai/"
    openai = Settings(llm_provider="openai", openai_api_key="k")
    assert llm_base_url(openai) == "" and llm_key_env(openai) == "OPENAI_API_KEY"


async def test_fenced_output_and_model_supplied_meta_are_handled():
    raw = json.loads(GOOD)
    raw["meta"] = {"model": "spoofed"}
    result = await Scripted(["```json\n" + json.dumps(raw) + "\n```"]).extract(_doc(**IMG))
    assert result.meta.model == "test-model"


async def test_invalid_output_gets_one_repair_round_trip():
    ext = Scripted(["not json", GOOD])
    result = await ext.extract(_doc(**IMG))
    assert len(ext.formats) == 2 and result.meta.repaired and result.bill.hospital_name == "City Clinic"


async def test_repeated_invalid_output_is_system_error():
    with pytest.raises(ExtractionError) as err:
        await Scripted(["nope", "still nope"]).extract(_doc(**IMG))
    assert err.value.kind == "SYSTEM"


async def test_line_with_illegible_amount_is_dropped_not_fatal():
    raw = json.dumps({"detected_document_type": "HOSPITAL_BILL", "bill": {"line_items": [
        {"description": "Consultation", "amount": 1000}, {"description": "Smudged", "amount": None}]}})
    result = await Scripted([raw]).extract(_doc(**IMG))
    assert len(result.bill.line_items) == 1 and result.bill.dropped_line_items == 1


# ---------------------------------------------------------------- failures

async def test_only_retryable_errors_are_retried(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    ext = Scripted([APITimeoutError("slow"), APITimeoutError("slow"), GOOD])
    result = await ext.extract(_doc(**IMG))
    assert result.bill and result.meta.attempts == 3
    ext = Scripted([AuthenticationError("bad key")])
    with pytest.raises(ExtractionError):
        await ext.extract(_doc(**IMG))
    assert len(ext.formats) == 1


async def test_condition_tags_are_restricted_to_policy_vocabulary():
    raw = json.dumps({"detected_document_type": "PRESCRIPTION", "prescription": {
        "diagnosis": "DM-II", "condition_tags": ["DIABETES", "invented_condition"]}})
    result = await Scripted([raw], condition_vocabulary=["diabetes", "hypertension"]).extract(_doc(**IMG))
    assert result.prescription.condition_tags == ["diabetes"]


async def test_missing_api_key_is_system_error():
    ext = VisionExtractor(Settings(gemini_api_key=""))
    with pytest.raises(ExtractionError) as err:
        await ext.extract(_doc(**IMG))
    assert err.value.kind == "SYSTEM"
