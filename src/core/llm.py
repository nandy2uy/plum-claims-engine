"""Live document extraction via an OpenAI vision-capable chat model.

Component contract
-------------------
Input:  an image URL (http(s):// or a data: URL for uploaded bytes) + which
        schema to fill (ExtractedPrescription | ExtractedBill). Callers build
        the URL (see src/agents/extractor.py::_resolve_image_url) so this
        module doesn't need to know where the bytes came from.
Output: a validated instance of that schema. Fields the model can't read are
        left None / empty rather than guessed.
Errors: raises `ExtractionError` for anything that prevents a usable result
        (missing API key, network/timeout failure, malformed model output).
        Callers (src/agents/extractor.py) are expected to catch this per
        document and flag that document, never let it crash the batch.

This module intentionally has no knowledge of claims, policies, or the
pipeline — it only turns bytes into one of two typed structures.
"""

import asyncio
import json

from pydantic import ValidationError

from src.core.config import get_settings
from src.models.medical import ExtractedBill, ExtractedPrescription


class ExtractionError(Exception):
    """Raised when a document could not be reliably extracted via the LLM."""


PRESCRIPTION_SCHEMA_HINT = """
{
  "doctor_name": string or null,
  "registration_number": string or null,
  "patient_name": string or null,
  "diagnoses": [string, ...],
  "medicines": [string, ...],
  "tests_ordered": [string, ...]
}
"""

BILL_SCHEMA_HINT = """
{
  "hospital_name": string or null,
  "gstin": string or null,
  "bill_number": string or null,
  "patient_name": string or null,
  "total_amount": number or null,
  "line_items": [{"description": string, "quantity": integer, "amount": number}, ...]
}
"""

SYSTEM_PROMPT = (
    "You are a medical document OCR/extraction engine for an Indian health "
    "insurance claims system. You will be shown a photo or scan of a real "
    "medical document (prescription or bill). These documents are often "
    "handwritten, stamped over, skewed phone photos, or partially cut off. "
    "Extract only what is legible. Do not guess or hallucinate values you "
    "cannot actually read — leave them null/empty instead. "
    "Respond with ONLY a single JSON object matching the given schema, no "
    "prose, no markdown fences."
)


async def _call_vision_model(image_url: str, schema_hint: str, doc_label: str) -> dict:
    settings = get_settings()
    if not settings.openai_api_key:
        raise ExtractionError(
            "OPENAI_API_KEY is not configured. Set it in .env to enable live "
            "document extraction; until then, submissions without pre-extracted "
            "fixture data cannot be processed automatically."
        )

    try:
        from openai import AsyncOpenAI
    except ImportError as e:
        raise ExtractionError(f"openai package is not installed: {e}") from e

    client = AsyncOpenAI(api_key=settings.openai_api_key)

    last_error: Exception | None = None
    for attempt in range(settings.llm_max_retries + 1):
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.openai_model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Extract this {doc_label}. JSON schema:\n{schema_hint}",
                                },
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        },
                    ],
                    temperature=0,
                ),
                timeout=settings.llm_timeout_seconds,
            )
            raw = response.choices[0].message.content
            return json.loads(raw)
        except asyncio.TimeoutError as e:
            last_error = e
        except json.JSONDecodeError as e:
            last_error = e
        except Exception as e:  # openai SDK error types (rate limit, connection, etc.)
            last_error = e
        if attempt < settings.llm_max_retries:
            await asyncio.sleep(0.5 * (2 ** attempt))

    raise ExtractionError(f"Vision extraction failed for {doc_label} after retries: {last_error}") from last_error


def _coerce_decimal_fields(payload: dict) -> dict:
    """The model returns JSON numbers; Decimal fields need string coercion
    to avoid binary-float precision issues once Pydantic parses them."""
    if "total_amount" in payload and payload["total_amount"] is not None:
        payload["total_amount"] = str(payload["total_amount"])
    for item in payload.get("line_items", []) or []:
        if "amount" in item and item["amount"] is not None:
            item["amount"] = str(item["amount"])
    return payload


async def extract_prescription_via_llm(image_url: str) -> ExtractedPrescription:
    payload = await _call_vision_model(image_url, PRESCRIPTION_SCHEMA_HINT, "prescription")
    try:
        return ExtractedPrescription(**payload)
    except ValidationError as e:
        raise ExtractionError(f"Model output did not match prescription schema: {e}") from e


async def extract_bill_via_llm(image_url: str) -> ExtractedBill:
    payload = _coerce_decimal_fields(await _call_vision_model(image_url, BILL_SCHEMA_HINT, "bill"))
    try:
        return ExtractedBill(**payload)
    except ValidationError as e:
        raise ExtractionError(f"Model output did not match bill schema: {e}") from e


def build_data_url(content_base64: str, mime_type: str) -> str:
    return f"data:{mime_type};base64,{content_base64}"
